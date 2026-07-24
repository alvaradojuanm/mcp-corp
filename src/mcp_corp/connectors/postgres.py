"""Conector concreto de PostgreSQL.

Solo sabe hablarle a Postgres: abrir y cerrar un pool async de psycopg3,
ejecutar una operación contra una conexión prestada del pool, y responder si
la base está sana. No implementa límite de concurrencia, timeout ni circuit
breaker — eso lo añade `resilience.ResilientExecutor` por fuera, envolviendo
cualquier objeto que cumpla el protocolo `Connector`.

Driver: psycopg3 (`psycopg[binary]==3.3.4`) + `psycopg_pool==3.3.1`, NO
asyncpg. Razón: compatibilidad con PgBouncer en modo "transaction" cuando
escalemos réplicas. asyncpg usa prepared statements automáticamente, lo que
choca con ese modo de PgBouncer (no soporta prepared statements por
conexión) y produce `DuplicatePreparedStatementError` — un fallo que solo
aparece bajo presión real del pool, no en desarrollo. psycopg3 tiene un
`prepare_threshold` adaptativo pensado para convivir con poolers externos.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

import psycopg
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool, PoolTimeout

logger = logging.getLogger(__name__)

R = TypeVar("R")

# Qué excepciones cuentan como fallo de infraestructura para el circuit
# breaker de Postgres (Fase 6, Bug 2). `PoolTimeout` (lo que se levanta
# al agotarse el pool o al no conseguir abrirlo a tiempo) NO hereda de
# `TimeoutError`/`OSError`/`ConnectionError` de la stdlib — hereda de
# `psycopg.OperationalError` (verificado contra el código fuente de
# psycopg_pool, ver README). Sin este override explícito, `resilience.py`
# usaría su tupla por defecto, que no incluye `PoolTimeout`: el breaker
# nunca la contaría como fallo, y el error se propagaría tal cual hacia
# `tools.py`, donde NO es un `ConnectorError` — no se traduciría al
# mensaje de negocio limpio y podría filtrar detalle interno.
POSTGRES_INFRA_EXCEPTIONS: tuple[type[BaseException], ...] = (
    OSError,
    ConnectionError,
    TimeoutError,
    psycopg.OperationalError,
)

# Timeout corto y fijo para health(): un pool en modo degradado (sin
# ninguna conexión disponible) no debe dejar /diagnostics colgado
# esperando el timeout por defecto del pool (30s).
_HEALTH_CHECK_TIMEOUT_SECONDS = 3.0


class PostgresConnector:
    """Conector de Postgres sobre un `AsyncConnectionPool` de psycopg3."""

    def __init__(
        self,
        name: str,
        dsn: str,
        *,
        min_pool_size: int,
        max_pool_size: int,
        pool_open_timeout_seconds: float,
    ) -> None:
        self.name = name
        self._dsn = dsn
        self._min_pool_size = min_pool_size
        self._max_pool_size = max_pool_size
        self._pool_open_timeout_seconds = pool_open_timeout_seconds
        self._pool: AsyncConnectionPool | None = None

    def _new_pool(self) -> AsyncConnectionPool:
        return AsyncConnectionPool(
            conninfo=self._dsn,
            min_size=self._min_pool_size,
            max_size=self._max_pool_size,
            open=False,
            name=self.name,
        )

    async def connect(self) -> None:
        """Abre el pool. Arranca en modo degradado si Postgres no responde
        a tiempo (Fase 6, Bug 2) — nunca deja que esto tumbe el proceso.

        El pool se crea con `open=False` y se abre explícitamente con
        `await pool.open(wait=True, ...)`: abrir el pool dentro del
        constructor está deprecado en versiones recientes de psycopg_pool
        (emite `RuntimeWarning`) y depende de que exista un event loop
        corriendo, que no es el caso en el momento de instanciar el objeto.

        Si `open(wait=True, timeout=X)` agota el timeout, levanta
        `PoolTimeout` — y, según el código fuente de psycopg_pool, ese
        mismo timeout CIERRA el pool (`wait()` llama a `close()` antes de
        lanzar la excepción). Un pool cerrado no se puede reabrir
        (`PoolClosed` si se intenta) — hay que descartarlo y crear uno
        nuevo. Por eso, ante ese caso: se descarta el pool fallido y se
        abre uno NUEVO en modo NO bloqueante (`wait=False`), que arranca
        sus workers en background sin esperar ni lanzar nada. No hace
        falta ningún bucle de reintento propio aquí: cada operación real
        contra el pool (`run()`, `health()`) ya reintenta obtener una
        conexión por su cuenta, contando como fallo de infraestructura
        mientras la fuente siga caída (ver `POSTGRES_INFRA_EXCEPTIONS`) —
        eso es lo que mantiene el circuit breaker abierto y lo que permite
        que se cierre solo en cuanto Postgres vuelva a responder.
        """
        pool = self._new_pool()
        try:
            await pool.open(wait=True, timeout=self._pool_open_timeout_seconds)
        except PoolTimeout:
            with contextlib.suppress(Exception):
                await pool.close()
            logger.warning(
                "connector_pool_degraded_start",
                extra={
                    "source": self.name,
                    "detail": "Postgres no respondió a tiempo; arranca en modo degradado",
                },
            )
            pool = self._new_pool()
            await pool.open(wait=False)
            self._pool = pool
            return

        self._pool = pool
        logger.info(
            "connector_pool_opened",
            extra={
                "source": self.name,
                "min_pool_size": self._min_pool_size,
                "max_pool_size": self._max_pool_size,
            },
        )

    async def close(self) -> None:
        """Cierra el pool de forma limpia (parte del graceful shutdown)."""
        if self._pool is not None:
            await self._pool.close()
            logger.info("connector_pool_closed", extra={"source": self.name})
            self._pool = None

    async def health(self) -> bool:
        """`SELECT 1` real contra una conexión prestada del pool, acotado
        a `_HEALTH_CHECK_TIMEOUT_SECONDS` para no colgar `/diagnostics`
        cuando el pool está en modo degradado (sin conexiones)."""
        if self._pool is None:
            return False
        try:
            async with asyncio.timeout(_HEALTH_CHECK_TIMEOUT_SECONDS):
                async with self._pool.connection() as conn:
                    await conn.execute("SELECT 1")
            return True
        except Exception:
            logger.warning(
                "connector_health_check_failed",
                extra={"source": self.name},
                exc_info=True,
            )
            return False

    async def run(self, operation: Callable[[AsyncConnection], Awaitable[R]]) -> R:
        """Presta una conexión del pool y ejecuta `operation` sobre ella."""
        if self._pool is None:
            raise RuntimeError(f"conector '{self.name}' no está conectado (llama a connect() primero)")
        async with self._pool.connection() as conn:
            return await operation(conn)

    def pool_stats(self) -> dict[str, Any]:
        """Estadísticas crudas del pool (`get_stats()` de psycopg_pool)."""
        if self._pool is None:
            return {}
        return self._pool.get_stats()
