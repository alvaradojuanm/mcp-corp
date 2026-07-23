"""Contrato común que debe cumplir cualquier conector de datos.

La capa de resiliencia (`resilience.py`) envuelve cualquier objeto que
cumpla este protocolo sin saber nada de la fuente concreta (Postgres, un
core bancario por REST, un sistema legacy, etc.). Un conector nuevo solo
necesita implementar estos cuatro métodos para heredar límite de
concurrencia, timeout y circuit breaker gratis.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Protocol, TypeVar, runtime_checkable

ConnT = TypeVar("ConnT")
R = TypeVar("R")


@runtime_checkable
class Connector(Protocol[ConnT]):
    """Contrato mínimo de un conector de datos concreto.

    `ConnT` es el tipo de recurso subyacente que `run()` entrega a la
    operación invocada (p. ej. una conexión de psycopg, un cliente httpx).
    """

    name: str

    async def connect(self) -> None:
        """Abre el recurso subyacente (pool, cliente HTTP, sesión, etc.)."""
        ...

    async def close(self) -> None:
        """Cierra el recurso subyacente de forma limpia."""
        ...

    async def health(self) -> bool:
        """Verifica la salud de la fuente con una operación barata y real."""
        ...

    async def run(self, operation: Callable[[ConnT], Awaitable[R]]) -> R:
        """Ejecuta `operation` contra el recurso subyacente y retorna su resultado.

        No aplica resiliencia por sí mismo: eso es responsabilidad de quien
        envuelve el conector (ver `resilience.ResilientExecutor`).
        """
        ...
