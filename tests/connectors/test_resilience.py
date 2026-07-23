"""Tests unitarios de la capa de resiliencia, contra un conector falso.

No tocan Postgres ni ninguna fuente real: `FakeConnector` es un doble de
prueba que cumple el protocolo `Connector`, así que estos tests verifican
únicamente lo que le corresponde a `ResilientExecutor` (semáforo, timeout,
circuit breaker) de forma rápida y determinista.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Any

import pytest

from mcp_corp.connectors.resilience import (
    CircuitOpenError,
    CircuitState,
    ConcurrencyLimitExceededError,
    OperationTimeoutError,
    ResilienceConfig,
    ResilientExecutor,
    SourceUnavailableError,
)


class FakeConnector:
    """Doble de prueba: cumple `Connector` sin hablar con ninguna fuente real."""

    def __init__(self, name: str = "fake") -> None:
        self.name = name
        self.connected = False
        self.call_count = 0

    async def connect(self) -> None:
        self.connected = True

    async def close(self) -> None:
        self.connected = False

    async def health(self) -> bool:
        return self.connected

    async def run(self, operation: Callable[[Any], Awaitable[Any]]) -> Any:
        self.call_count += 1
        return await operation(None)


class FakeClock:
    """Reloj controlable a mano para probar transiciones del breaker sin sleeps reales."""

    def __init__(self) -> None:
        self._now = 0.0

    def __call__(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds


def make_config(**overrides: Any) -> ResilienceConfig:
    defaults: dict[str, Any] = dict(
        source_name="fake",
        max_concurrency=5,
        acquire_timeout_seconds=1.0,
        operation_timeout_seconds=1.0,
        failure_threshold=3,
        reset_timeout_seconds=10.0,
        success_threshold=1,
    )
    defaults.update(overrides)
    return ResilienceConfig(**defaults)


async def ok_operation(_conn: Any) -> str:
    return "ok"


def failing_operation(exc: BaseException) -> Callable[[Any], Awaitable[Any]]:
    async def _operation(_conn: Any) -> Any:
        raise exc

    return _operation


# --- Semáforo: límite de concurrencia ---


async def test_semaphore_limits_real_concurrency() -> None:
    connector = FakeConnector()
    executor = ResilientExecutor(connector, make_config(max_concurrency=1, operation_timeout_seconds=2.0))

    timeline: list[tuple[str, float]] = []

    async def slow_operation(_conn: Any) -> None:
        timeline.append(("start", time.monotonic()))
        await asyncio.sleep(0.1)
        timeline.append(("end", time.monotonic()))

    await asyncio.gather(executor.run(slow_operation), executor.run(slow_operation))

    # Con max_concurrency=1, la segunda operación solo puede empezar después
    # de que la primera termine: el segundo "start" debe ser posterior al
    # primer "end".
    first_end = timeline[1][1]
    second_start = timeline[2][1]
    assert second_start >= first_end


async def test_semaphore_saturated_fails_clean_instead_of_queueing_forever() -> None:
    connector = FakeConnector()
    executor = ResilientExecutor(
        connector,
        make_config(max_concurrency=1, acquire_timeout_seconds=0.05, operation_timeout_seconds=2.0),
    )

    async def hold_slot(_conn: Any) -> None:
        await asyncio.sleep(0.3)

    holder = asyncio.create_task(executor.run(hold_slot))
    await asyncio.sleep(0.02)  # deja que el holder tome el único slot

    with pytest.raises(ConcurrencyLimitExceededError):
        await executor.run(ok_operation)

    await holder


# --- Timeout por operación ---


async def test_operation_timeout_raises_and_counts_as_failure() -> None:
    connector = FakeConnector()
    config = make_config(operation_timeout_seconds=0.05, failure_threshold=5)
    executor = ResilientExecutor(connector, config)

    async def slow_operation(_conn: Any) -> None:
        await asyncio.sleep(1.0)

    with pytest.raises(OperationTimeoutError):
        await executor.run(slow_operation)

    assert executor.snapshot()["circuit_state"] == CircuitState.CLOSED.value  # 1 fallo, umbral es 5


# --- Circuit breaker: abre, medio-abierto, cierra ---


async def test_circuit_opens_after_consecutive_infra_failures() -> None:
    connector = FakeConnector()
    config = make_config(failure_threshold=3)
    executor = ResilientExecutor(connector, config)

    for _ in range(3):
        with pytest.raises(SourceUnavailableError):
            await executor.run(failing_operation(ConnectionError("infra caída")))

    assert executor.snapshot()["circuit_state"] == CircuitState.OPEN.value

    # Con el circuito abierto, ni siquiera se llama al conector subyacente.
    calls_before = connector.call_count
    with pytest.raises(CircuitOpenError):
        await executor.run(ok_operation)
    assert connector.call_count == calls_before


async def test_circuit_half_opens_after_reset_and_closes_on_success() -> None:
    connector = FakeConnector()
    clock = FakeClock()
    config = make_config(failure_threshold=2, reset_timeout_seconds=30.0, success_threshold=2)
    executor = ResilientExecutor(connector, config, clock=clock)

    for _ in range(2):
        with pytest.raises(SourceUnavailableError):
            await executor.run(failing_operation(OSError("caída")))
    assert executor.snapshot()["circuit_state"] == CircuitState.OPEN.value

    # Antes de que pase el tiempo de reset, sigue rechazando sin intentar.
    with pytest.raises(CircuitOpenError):
        await executor.run(ok_operation)

    clock.advance(31.0)

    # Primer éxito en medio-abierto: no alcanza el success_threshold (2) aún.
    assert await executor.run(ok_operation) == "ok"
    assert executor.snapshot()["circuit_state"] == CircuitState.HALF_OPEN.value

    # Segundo éxito: cierra el circuito.
    assert await executor.run(ok_operation) == "ok"
    assert executor.snapshot()["circuit_state"] == CircuitState.CLOSED.value


async def test_half_open_failure_reopens_circuit() -> None:
    connector = FakeConnector()
    clock = FakeClock()
    config = make_config(failure_threshold=1, reset_timeout_seconds=10.0, success_threshold=1)
    executor = ResilientExecutor(connector, config, clock=clock)

    with pytest.raises(SourceUnavailableError):
        await executor.run(failing_operation(ConnectionError()))
    assert executor.snapshot()["circuit_state"] == CircuitState.OPEN.value

    clock.advance(11.0)

    with pytest.raises(SourceUnavailableError):
        await executor.run(failing_operation(ConnectionError()))
    assert executor.snapshot()["circuit_state"] == CircuitState.OPEN.value


# --- Errores de negocio no abren el circuito ---


async def test_business_error_never_opens_circuit() -> None:
    connector = FakeConnector()
    config = make_config(failure_threshold=2)
    executor = ResilientExecutor(connector, config)

    for _ in range(10):
        with pytest.raises(ValueError):
            await executor.run(failing_operation(ValueError("constraint violado")))

    assert executor.snapshot()["circuit_state"] == CircuitState.CLOSED.value
