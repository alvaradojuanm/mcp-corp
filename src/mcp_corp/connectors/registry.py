"""Registro de conectores activos: ciclo de vida y diagnóstico agregado.

Agrupa, por nombre de fuente, un `Connector` concreto junto con el
`ResilientExecutor` que lo envuelve. El server lo usa para: (1) abrir todos
los conectores al arrancar y cerrarlos al apagar, atado al lifespan que ya
existe; y (2) exponer `/diagnostics` sin acoplar `/health` ni `/ready` a la
salud de las fuentes (ver decisión en README).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from mcp_corp.connectors.base import Connector
from mcp_corp.connectors.resilience import ResilientExecutor

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ManagedConnector:
    connector: Connector[Any]
    executor: ResilientExecutor


class ConnectorRegistry:
    def __init__(self) -> None:
        self._connectors: dict[str, ManagedConnector] = {}

    def register(self, connector: Connector[Any], executor: ResilientExecutor) -> None:
        self._connectors[connector.name] = ManagedConnector(connector, executor)

    def get(self, name: str) -> ManagedConnector:
        return self._connectors[name]

    def __contains__(self, name: str) -> bool:
        return name in self._connectors

    async def connect_all(self) -> None:
        """Conecta cada fuente; una fuente caída NO tumba el arranque (Fase 6, Bug 2).

        Antes, si `connector.connect()` lanzaba (p. ej. Postgres
        inalcanzable), la excepción se propagaba hasta el lifespan del
        server y el proceso entero moría — contradiciendo la razón de ser
        de la capa de resiliencia, que protege durante las operaciones
        pero no protegía el arranque. Ahora, si una fuente falla al
        conectar (o conecta pero su propio `health()` reporta que no está
        sana — p. ej. el conector de Postgres cayendo a modo degradado sin
        lanzar, ver `postgres.py`), esa fuente queda con el circuito
        forzado a `open` desde el primer momento (visible en
        `/diagnostics`) y el registro sigue con las demás.

        La recuperación no necesita ningún reintento propio de este
        método: en cuanto una operación real contra esa fuente tenga
        éxito (el propio conector reintenta internamente — ver
        `postgres.py` — o el circuito deja pasar un intento de prueba tras
        `reset_timeout_seconds`), el `CircuitBreaker` ya existente la
        cierra sola, sin reiniciar el proceso.
        """
        for name, managed in self._connectors.items():
            try:
                await managed.connector.connect()
            except Exception:
                logger.error(
                    "connector_startup_failed",
                    extra={
                        "source": name,
                        "detail": "arranca en modo degradado; el circuito queda abierto",
                    },
                    exc_info=True,
                )
                await managed.executor.force_open()
                continue

            if await managed.connector.health():
                logger.info("connector_startup_ok", extra={"source": name})
            else:
                logger.warning(
                    "connector_startup_degraded",
                    extra={
                        "source": name,
                        "detail": "connect() no lanzó, pero health() no está sano; el circuito queda abierto",
                    },
                )
                await managed.executor.force_open()

    async def close_all(self) -> None:
        for managed in self._connectors.values():
            await managed.connector.close()

    async def diagnostics(self) -> dict[str, Any]:
        """Estado de cada conector: breaker, uso del pool y salud actual."""
        report: dict[str, Any] = {}
        for name, managed in self._connectors.items():
            healthy = await managed.connector.health()
            snapshot = managed.executor.snapshot()
            pool_stats = managed.connector.pool_stats() if hasattr(managed.connector, "pool_stats") else {}
            report[name] = {**snapshot, "healthy": healthy, "pool": pool_stats}
        return report
