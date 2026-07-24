"""Bug 2 (Fase 6): el servidor moría si una fuente estaba caída al arrancar.

Un `PoolTimeout` de Postgres en `connect_all()` propagaba y tumbaba el
proceso entero — contradice la tesis de la capa de resiliencia (protege
DURANTE las operaciones, pero no protegía el arranque). Estos tests usan
un `FakeConnector` genérico (no Postgres real) para probar el mecanismo a
nivel de `ConnectorRegistry`/`ResilientExecutor`: arranque degradado,
`/diagnostics` reflejando el circuito abierto, tools de fuentes sanas
funcionando, tools de la fuente caída fallando limpio, y recuperación en
caliente sin reiniciar el proceso.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from fastmcp.exceptions import ToolError

from mcp_corp.connectors.registry import ConnectorRegistry
from mcp_corp.connectors.resilience import CircuitState, ResilienceConfig, ResilientExecutor
from mcp_corp.tools import _consultar_cliente_logic


class FakeConnector:
    """`connect()` falla si `fail_connect` está activo; `run()` falla si
    nunca llegó a conectar."""

    def __init__(self, name: str, *, fail_connect: bool = False) -> None:
        self.name = name
        self.fail_connect = fail_connect
        self.connected = False
        self.connect_calls = 0

    async def connect(self) -> None:
        self.connect_calls += 1
        if self.fail_connect:
            raise ConnectionError(f"{self.name} inalcanzable")
        self.connected = True

    async def close(self) -> None:
        self.connected = False

    async def health(self) -> bool:
        return self.connected

    async def run(self, operation: Callable[[Any], Awaitable[Any]]) -> Any:
        if not self.connected:
            raise ConnectionError(f"{self.name} no conectado")
        return await operation(None)


def _config(name: str, **overrides: Any) -> ResilienceConfig:
    defaults: dict[str, Any] = dict(
        source_name=name,
        max_concurrency=5,
        acquire_timeout_seconds=1.0,
        operation_timeout_seconds=1.0,
        failure_threshold=3,
        reset_timeout_seconds=30.0,
    )
    defaults.update(overrides)
    return ResilienceConfig(**defaults)


async def test_connect_all_no_muere_si_una_fuente_falla() -> None:
    registry = ConnectorRegistry()
    postgres = FakeConnector("postgres", fail_connect=True)
    saldo = FakeConnector("saldo_api")
    registry.register(postgres, ResilientExecutor(postgres, _config("postgres")))
    registry.register(saldo, ResilientExecutor(saldo, _config("saldo_api")))

    await registry.connect_all()  # NO debe lanzar

    assert saldo.connected is True


async def test_fuente_caida_al_arrancar_queda_con_circuito_abierto() -> None:
    registry = ConnectorRegistry()
    postgres = FakeConnector("postgres", fail_connect=True)
    registry.register(postgres, ResilientExecutor(postgres, _config("postgres")))

    await registry.connect_all()
    diag = await registry.diagnostics()

    assert diag["postgres"]["circuit_state"] == CircuitState.OPEN.value
    assert diag["postgres"]["healthy"] is False


async def test_fuente_sana_no_se_ve_afectada_por_la_caida_de_otra() -> None:
    registry = ConnectorRegistry()
    postgres = FakeConnector("postgres", fail_connect=True)
    saldo = FakeConnector("saldo_api")
    registry.register(postgres, ResilientExecutor(postgres, _config("postgres")))
    registry.register(saldo, ResilientExecutor(saldo, _config("saldo_api")))

    await registry.connect_all()
    diag = await registry.diagnostics()

    assert diag["saldo_api"]["circuit_state"] == CircuitState.CLOSED.value
    assert diag["saldo_api"]["healthy"] is True


async def test_tool_de_fuente_caida_falla_limpio_sin_reiniciar_el_proceso() -> None:
    registry = ConnectorRegistry()
    postgres = FakeConnector("postgres", fail_connect=True)
    registry.register(postgres, ResilientExecutor(postgres, _config("postgres")))

    await registry.connect_all()  # el proceso "arrancó" igual

    executor = registry.get("postgres").executor
    with pytest.raises(ToolError, match="no está disponible"):
        await _consultar_cliente_logic("V16760320", executor)


async def test_recuperacion_en_caliente_sin_reiniciar_el_proceso() -> None:
    """Cuando la fuente vuelve y pasa el tiempo de reset del breaker, una
    operación exitosa cierra el circuito sola — sin reiniciar el proceso."""

    class FakeClock:
        def __init__(self) -> None:
            self.t = 0.0

        def __call__(self) -> float:
            return self.t

        def advance(self, seconds: float) -> None:
            self.t += seconds

    registry = ConnectorRegistry()
    postgres = FakeConnector("postgres", fail_connect=True)
    clock = FakeClock()
    executor = ResilientExecutor(
        postgres,
        _config("postgres", failure_threshold=1, reset_timeout_seconds=10.0, success_threshold=1),
        clock=clock,
    )
    registry.register(postgres, executor)

    await registry.connect_all()
    assert executor.snapshot()["circuit_state"] == CircuitState.OPEN.value

    # La fuente "vuelve": un intento de conexión posterior tiene éxito.
    postgres.fail_connect = False
    await postgres.connect()
    assert postgres.connected is True

    clock.advance(11.0)  # pasa el reset_timeout_seconds

    async def op(_conn: Any) -> str:
        return "ok"

    result = await executor.run(op)  # primer intento tras el reset (medio-abierto)

    assert result == "ok"
    assert executor.snapshot()["circuit_state"] == CircuitState.CLOSED.value
