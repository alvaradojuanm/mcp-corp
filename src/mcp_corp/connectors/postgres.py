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

import logging
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

logger = logging.getLogger(__name__)

R = TypeVar("R")


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

    async def connect(self) -> None:
        """Crea y abre el pool, atado al lifespan del server.

        El pool se crea con `open=False` y se abre explícitamente con
        `await pool.open(wait=True, ...)`: abrir el pool dentro del
        constructor está deprecado en versiones recientes de psycopg_pool
        (emite `RuntimeWarning`) y depende de que exista un event loop
        corriendo, que no es el caso en el momento de instanciar el objeto.
        """
        self._pool = AsyncConnectionPool(
            conninfo=self._dsn,
            min_size=self._min_pool_size,
            max_size=self._max_pool_size,
            open=False,
            name=self.name,
        )
        await self._pool.open(wait=True, timeout=self._pool_open_timeout_seconds)
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
        """`SELECT 1` real contra una conexión prestada del pool."""
        if self._pool is None:
            return False
        try:
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
