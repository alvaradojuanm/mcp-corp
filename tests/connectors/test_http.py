"""Unitarios del conector HTTP, con un transporte falso (sin red real).

Verifica dos cosas separadas: que `HttpConnector` por sí solo habla bien
con `httpx.AsyncClient` (health, run), y que la capa de resiliencia de la
Fase 2 —diseñada sin saber nada de HTTP— efectivamente lo envuelve: el
timeout dispara, el circuito abre ante fallos de transporte, y el
semáforo limita concurrencia real. Esto es lo que valida que la
abstracción de conectores sirve para algo que no es Postgres.
"""

from __future__ import annotations

import asyncio
import time

import httpx
import pytest

from mcp_corp.connectors.http import HTTP_INFRA_EXCEPTIONS, HttpConnector
from mcp_corp.connectors.resilience import (
    CircuitState,
    ConcurrencyLimitExceededError,
    OperationTimeoutError,
    ResilienceConfig,
    ResilientExecutor,
    SourceUnavailableError,
)


class StaticTransport(httpx.AsyncBaseTransport):
    """Responde siempre lo mismo, sin tocar la red."""

    def __init__(self, status_code: int = 200, json_body: dict | None = None) -> None:
        self._status_code = status_code
        self._json_body = json_body if json_body is not None else {}

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        return httpx.Response(self._status_code, json=self._json_body, request=request)


class SlowTransport(httpx.AsyncBaseTransport):
    """Tarda `delay_seconds` en responder — para probar timeout y concurrencia reales."""

    def __init__(self, delay_seconds: float, status_code: int = 200) -> None:
        self._delay_seconds = delay_seconds
        self._status_code = status_code

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(self._delay_seconds)
        return httpx.Response(self._status_code, json={}, request=request)


class FailingTransport(httpx.AsyncBaseTransport):
    """Simula un fallo de infraestructura real (conexión rechazada)."""

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("conexión rechazada", request=request)


def _connector_with_transport(transport: httpx.AsyncBaseTransport, name: str = "saldo_api") -> HttpConnector:
    connector = HttpConnector(name, base_url="http://stub.local", request_timeout_seconds=5.0)
    # `connect()` crearía un cliente real sin transporte inyectable; para
    # los unitarios montamos el cliente directamente con el transporte falso,
    # que es exactamente lo mismo que hace `connect()` salvo por esa pieza.
    connector._client = httpx.AsyncClient(base_url="http://stub.local", transport=transport)
    return connector


def make_config(**overrides: object) -> ResilienceConfig:
    defaults: dict[str, object] = dict(
        source_name="saldo_api",
        max_concurrency=5,
        acquire_timeout_seconds=1.0,
        operation_timeout_seconds=1.0,
        failure_threshold=3,
        reset_timeout_seconds=10.0,
        infra_exceptions=HTTP_INFRA_EXCEPTIONS,
    )
    defaults.update(overrides)
    return ResilienceConfig(**defaults)


# --- HttpConnector por sí solo -------------------------------------------


async def test_health_true_on_200() -> None:
    connector = _connector_with_transport(StaticTransport(200, {"status": "ok"}))
    assert await connector.health() is True
    await connector.close()


async def test_health_false_on_error_status() -> None:
    connector = _connector_with_transport(StaticTransport(500))
    assert await connector.health() is False
    await connector.close()


async def test_health_false_before_connect() -> None:
    connector = HttpConnector("saldo_api", base_url="http://stub.local", request_timeout_seconds=5.0)
    assert await connector.health() is False


async def test_run_returns_operation_result() -> None:
    connector = _connector_with_transport(StaticTransport(200, {"saldo": 42}))

    async def op(client: httpx.AsyncClient) -> dict:
        response = await client.get("/saldos/123")
        return response.json()

    assert await connector.run(op) == {"saldo": 42}
    await connector.close()


async def test_run_before_connect_raises() -> None:
    connector = HttpConnector("saldo_api", base_url="http://stub.local", request_timeout_seconds=5.0)
    with pytest.raises(RuntimeError):
        await connector.run(lambda client: client.get("/health"))


# --- Envuelto en ResilientExecutor: la resiliencia de la Fase 2 aplica ---


async def test_resilience_timeout_triggers_on_slow_http_call() -> None:
    connector = _connector_with_transport(SlowTransport(delay_seconds=1.0))
    executor = ResilientExecutor(connector, make_config(operation_timeout_seconds=0.05))

    async def op(client: httpx.AsyncClient) -> None:
        await client.get("/saldos/123")

    with pytest.raises(OperationTimeoutError):
        await executor.run(op)
    await connector.close()


async def test_resilience_semaphore_limits_real_concurrency() -> None:
    connector = _connector_with_transport(SlowTransport(delay_seconds=0.1))
    executor = ResilientExecutor(connector, make_config(max_concurrency=1, operation_timeout_seconds=2.0))

    timeline: list[tuple[str, float]] = []

    async def op(client: httpx.AsyncClient) -> None:
        timeline.append(("start", time.monotonic()))
        await client.get("/saldos/123")
        timeline.append(("end", time.monotonic()))

    await asyncio.gather(executor.run(op), executor.run(op))

    first_end = timeline[1][1]
    second_start = timeline[2][1]
    assert second_start >= first_end
    await connector.close()


async def test_resilience_saturation_fails_clean() -> None:
    connector = _connector_with_transport(SlowTransport(delay_seconds=0.3))
    executor = ResilientExecutor(
        connector,
        make_config(max_concurrency=1, acquire_timeout_seconds=0.05, operation_timeout_seconds=2.0),
    )

    async def op(client: httpx.AsyncClient) -> None:
        await client.get("/saldos/123")

    holder = asyncio.create_task(executor.run(op))
    await asyncio.sleep(0.02)

    with pytest.raises(ConcurrencyLimitExceededError):
        await executor.run(op)

    await holder
    await connector.close()


async def test_resilience_circuit_opens_on_transport_failures() -> None:
    connector = _connector_with_transport(FailingTransport())
    executor = ResilientExecutor(connector, make_config(failure_threshold=2))

    async def op(client: httpx.AsyncClient) -> None:
        await client.get("/saldos/123")

    for _ in range(2):
        with pytest.raises(SourceUnavailableError):
            await executor.run(op)

    assert executor.snapshot()["circuit_state"] == CircuitState.OPEN.value
    await connector.close()


async def test_resilience_circuit_opens_on_5xx_status() -> None:
    """Un 500 real del servicio también debe contar como fallo de infraestructura.

    A diferencia de un 404 (que las tools tratan como "no encontrado", caso
    de negocio, y nunca llega a `raise_for_status()`), un 5xx sí llega a
    `raise_for_status()` dentro de la operación y debe abrir el circuito
    igual que un fallo de transporte.
    """
    connector = _connector_with_transport(StaticTransport(500, {"error": "boom"}))
    executor = ResilientExecutor(connector, make_config(failure_threshold=1))

    async def op(client: httpx.AsyncClient) -> None:
        response = await client.get("/saldos/123")
        response.raise_for_status()

    with pytest.raises(SourceUnavailableError):
        await executor.run(op)

    assert executor.snapshot()["circuit_state"] == CircuitState.OPEN.value
    await connector.close()
