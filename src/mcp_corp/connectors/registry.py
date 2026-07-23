"""Registro de conectores activos: ciclo de vida y diagnóstico agregado.

Agrupa, por nombre de fuente, un `Connector` concreto junto con el
`ResilientExecutor` que lo envuelve. El server lo usa para: (1) abrir todos
los conectores al arrancar y cerrarlos al apagar, atado al lifespan que ya
existe; y (2) exponer `/diagnostics` sin acoplar `/health` ni `/ready` a la
salud de las fuentes (ver decisión en README).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from mcp_corp.connectors.base import Connector
from mcp_corp.connectors.resilience import ResilientExecutor


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
        for managed in self._connectors.values():
            await managed.connector.connect()

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
