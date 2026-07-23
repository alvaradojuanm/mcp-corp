"""Unitarios de la lógica de las tools, sin infraestructura real.

Usa el mismo patrón de `FakeConnector` de `tests/connectors/test_resilience.py`:
un doble mínimo que cumple `Connector`, cuyo `run()` puede fallar ANTES de
invocar la operación (simula una fuente caída) o delegar en ella (caso
feliz). Las funciones `_query_cliente` / `_query_saldo` se parchean para no
depender de un cursor de psycopg o un cliente httpx reales — lo que se
prueba aquí es la ORQUESTACIÓN (`tools.py`), no los conectores (ya cubiertos
en `tests/connectors/`).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from fastmcp.exceptions import ToolError

import mcp_corp.tools as tools_module
from mcp_corp.connectors.resilience import ResilienceConfig, ResilientExecutor
from mcp_corp.tools import (
    _consultar_cliente_logic,
    _consultar_saldo_logic,
    _resumen_cliente_logic,
)

CLIENTE_ANA = {
    "cedula": "1000000001",
    "nombre": "Ana María Restrepo",
    "email": "ana.restrepo@example.com",
    "estado": "activo",
}
SALDO_ANA = {"cedula": "1000000001", "saldo": 1500000.50, "moneda": "COP"}


class FakeConnector:
    """Si `fail_with` está seteado, `run()` falla ANTES de llamar a `operation`
    (simula la fuente caída); si no, delega en `operation(None)`."""

    def __init__(self, name: str, fail_with: BaseException | None = None) -> None:
        self.name = name
        self.fail_with = fail_with

    async def connect(self) -> None: ...

    async def close(self) -> None: ...

    async def health(self) -> bool:
        return self.fail_with is None

    async def run(self, operation: Callable[[Any], Awaitable[Any]]) -> Any:
        if self.fail_with is not None:
            raise self.fail_with
        return await operation(None)


def make_executor(source_name: str, fail_with: BaseException | None = None) -> ResilientExecutor:
    connector = FakeConnector(source_name, fail_with=fail_with)
    config = ResilienceConfig(
        source_name=source_name,
        max_concurrency=5,
        acquire_timeout_seconds=1.0,
        operation_timeout_seconds=1.0,
        failure_threshold=1,
        reset_timeout_seconds=30.0,
    )
    return ResilientExecutor(connector, config)


@pytest.fixture(autouse=True)
def _patch_queries(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_query_cliente(_conn: Any, cedula: str) -> dict[str, Any] | None:
        return CLIENTE_ANA if cedula == "1000000001" else None

    async def fake_query_saldo(_client: Any, cedula: str) -> dict[str, Any] | None:
        return SALDO_ANA if cedula == "1000000001" else None

    monkeypatch.setattr(tools_module, "_query_cliente", fake_query_cliente)
    monkeypatch.setattr(tools_module, "_query_saldo", fake_query_saldo)


# --- Caso feliz y "no encontrado" (negocio, no infraestructura) ----------


async def test_consultar_cliente_found() -> None:
    executor = make_executor("postgres")
    assert await _consultar_cliente_logic("1000000001", executor) == CLIENTE_ANA


async def test_consultar_cliente_not_found_raises_tool_error() -> None:
    executor = make_executor("postgres")
    with pytest.raises(ToolError, match="No se encontró"):
        await _consultar_cliente_logic("9999999999", executor)


async def test_consultar_saldo_found() -> None:
    executor = make_executor("saldo_api")
    assert await _consultar_saldo_logic("1000000001", executor) == SALDO_ANA


async def test_consultar_saldo_not_found_raises_tool_error() -> None:
    executor = make_executor("saldo_api")
    with pytest.raises(ToolError, match="No se encontró"):
        await _consultar_saldo_logic("9999999999", executor)


# --- Fuente caída: la tool simple falla limpio, la compuesta NO falla ----


async def test_consultar_cliente_source_down_raises_clean_tool_error() -> None:
    secret = "password authentication failed for user secretadmin at db-internal.corp:5432"
    executor = make_executor("postgres", fail_with=ConnectionError(secret))

    with pytest.raises(ToolError) as exc_info:
        await _consultar_cliente_logic("1000000001", executor)

    message = str(exc_info.value)
    assert "secretadmin" not in message
    assert "db-internal" not in message
    assert "no está disponible" in message


async def test_resumen_cliente_partial_when_saldo_source_down() -> None:
    postgres_executor = make_executor("postgres")
    saldo_executor = make_executor("saldo_api", fail_with=ConnectionError("boom interno"))

    result = await _resumen_cliente_logic("1000000001", postgres_executor, saldo_executor)

    assert result["resumen_completo"] is False
    assert result["cliente"] == {"disponible": True, "datos": CLIENTE_ANA, "motivo": None}
    assert result["saldo"]["disponible"] is False
    assert result["saldo"]["datos"] is None
    assert result["saldo"]["motivo"] == "el servicio de saldos no está disponible en este momento"


async def test_resumen_cliente_partial_when_postgres_source_down() -> None:
    postgres_executor = make_executor("postgres", fail_with=ConnectionError("boom interno"))
    saldo_executor = make_executor("saldo_api")

    result = await _resumen_cliente_logic("1000000001", postgres_executor, saldo_executor)

    assert result["resumen_completo"] is False
    assert result["cliente"]["disponible"] is False
    assert result["cliente"]["datos"] is None
    assert result["saldo"] == {"disponible": True, "datos": SALDO_ANA, "motivo": None}


async def test_resumen_cliente_both_sources_down_still_does_not_raise() -> None:
    postgres_executor = make_executor("postgres", fail_with=ConnectionError("db down"))
    saldo_executor = make_executor("saldo_api", fail_with=ConnectionError("api down"))

    result = await _resumen_cliente_logic("1000000001", postgres_executor, saldo_executor)

    assert result["resumen_completo"] is False
    assert result["cliente"]["disponible"] is False
    assert result["saldo"]["disponible"] is False


async def test_resumen_cliente_happy_path_complete() -> None:
    postgres_executor = make_executor("postgres")
    saldo_executor = make_executor("saldo_api")

    result = await _resumen_cliente_logic("1000000001", postgres_executor, saldo_executor)

    assert result["resumen_completo"] is True
    assert result["cliente"]["datos"] == CLIENTE_ANA
    assert result["saldo"]["datos"] == SALDO_ANA


# --- No filtración de internals hacia el modelo --------------------------


async def test_no_internals_leak_in_resumen_cliente_result() -> None:
    secret = "FATAL: password authentication failed 10.0.0.5:5432"
    postgres_executor = make_executor("postgres", fail_with=ConnectionError(secret))
    saldo_executor = make_executor("saldo_api")

    result = await _resumen_cliente_logic("1000000001", postgres_executor, saldo_executor)

    serialized = str(result)
    assert "10.0.0.5" not in serialized
    assert "FATAL" not in serialized


async def test_no_internals_leak_in_consultar_saldo_tool_error() -> None:
    secret = "connect ETIMEDOUT 10.20.30.40:443 saldo-api-internal.corp"
    executor = make_executor("saldo_api", fail_with=ConnectionError(secret))

    with pytest.raises(ToolError) as exc_info:
        await _consultar_saldo_logic("1000000001", executor)

    message = str(exc_info.value)
    assert "10.20.30.40" not in message
    assert "saldo-api-internal" not in message
