"""Bug 2 (Fase 6): detalle específico de Postgres — `connect()` no debe

propagar un `PoolTimeout`. Usa un doble de `AsyncConnectionPool` (no
Postgres real) para verificar exactamente la secuencia: intento con
`wait=True` que agota el timeout (queda cerrado, según el comportamiento
documentado de psycopg_pool), descartado, y un pool nuevo abierto con
`wait=False` que sí queda asignado a `self._pool` sin lanzar nada.
"""

from __future__ import annotations

import psycopg
import pytest
from psycopg_pool import PoolTimeout

import mcp_corp.connectors.postgres as postgres_module
from mcp_corp.connectors.postgres import POSTGRES_INFRA_EXCEPTIONS, PostgresConnector


class FakePool:
    """Doble de `AsyncConnectionPool`: el primero que se crea agota el
    timeout con `wait=True`; los siguientes abren con éxito en `wait=False`."""

    instances: list["FakePool"] = []

    def __init__(self, *, conninfo: str, min_size: int, max_size: int, open: bool, name: str) -> None:
        self.conninfo = conninfo
        self.opened_wait_true = False
        self.opened_wait_false = False
        self.closed = False
        FakePool.instances.append(self)

    async def open(self, wait: bool = True, timeout: float | None = None) -> None:
        if wait:
            self.opened_wait_true = True
            raise PoolTimeout("pool initialization incomplete after timeout")
        self.opened_wait_false = True

    async def close(self) -> None:
        self.closed = True


@pytest.fixture(autouse=True)
def _reset_fake_pool_instances() -> None:
    FakePool.instances.clear()


async def test_connect_no_propaga_pooltimeout_y_cae_a_modo_degradado(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(postgres_module, "AsyncConnectionPool", FakePool)
    connector = PostgresConnector(
        "postgres", "postgresql://x", min_pool_size=1, max_pool_size=5, pool_open_timeout_seconds=1.0
    )

    await connector.connect()  # NO debe lanzar PoolTimeout

    assert len(FakePool.instances) == 2
    primero, segundo = FakePool.instances
    assert primero.opened_wait_true is True
    assert primero.closed is True  # descartado tras el PoolTimeout
    assert segundo.opened_wait_false is True
    assert connector._pool is segundo  # el pool degradado queda asignado


async def test_connect_camino_feliz_no_crea_un_segundo_pool(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakePoolSano(FakePool):
        async def open(self, wait: bool = True, timeout: float | None = None) -> None:
            self.opened_wait_true = True  # nunca agota el timeout

    monkeypatch.setattr(postgres_module, "AsyncConnectionPool", FakePoolSano)
    connector = PostgresConnector(
        "postgres", "postgresql://x", min_pool_size=1, max_pool_size=5, pool_open_timeout_seconds=1.0
    )

    await connector.connect()

    assert len(FakePoolSano.instances) == 1
    assert connector._pool is FakePoolSano.instances[0]
    assert FakePoolSano.instances[0].closed is False


def test_pooltimeout_no_hereda_de_excepciones_estandar_de_python() -> None:
    """Regresión: si esto alguna vez cambiara en una versión de psycopg_pool,
    POSTGRES_INFRA_EXCEPTIONS dejaría de ser necesario para este caso —
    pero hoy (verificado contra el código fuente) PoolTimeout hereda de
    psycopg.OperationalError, no de TimeoutError/OSError/ConnectionError."""
    assert not issubclass(PoolTimeout, TimeoutError)
    assert not issubclass(PoolTimeout, OSError)
    assert not issubclass(PoolTimeout, ConnectionError)
    assert issubclass(PoolTimeout, psycopg.OperationalError)


def test_postgres_infra_exceptions_cubre_pooltimeout() -> None:
    assert issubclass(PoolTimeout, POSTGRES_INFRA_EXCEPTIONS)
