"""Integración de las tres tools contra Postgres real + el stub de saldos.

Requiere `docker compose -f docker-compose.dev.yml up -d`. Si Postgres o el
stub no están alcanzables, estos tests se saltan con mensaje explícito
(mismo patrón que `tests/connectors/test_postgres_integration.py`).
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator

import httpx
import psycopg
import pytest

from mcp_corp.connectors.http import HTTP_INFRA_EXCEPTIONS, HttpConnector
from mcp_corp.connectors.postgres import PostgresConnector
from mcp_corp.connectors.resilience import ResilienceConfig, ResilientExecutor
from mcp_corp.tools import _consultar_cliente_logic, _consultar_saldo_logic, _resumen_cliente_logic

POSTGRES_DSN = os.environ.get(
    "MCP_CORP_TEST_POSTGRES_DSN",
    "postgresql://mcp_corp:mcp_corp@localhost:5432/mcp_corp",
)
SALDO_API_BASE_URL = os.environ.get("MCP_CORP_TEST_SALDO_API_URL", "http://localhost:8080")


async def _postgres_reachable() -> bool:
    try:
        async with await psycopg.AsyncConnection.connect(POSTGRES_DSN, connect_timeout=2):
            return True
    except Exception:
        return False


async def _saldo_api_reachable() -> bool:
    try:
        async with httpx.AsyncClient(base_url=SALDO_API_BASE_URL, timeout=2.0) as client:
            response = await client.get("/health")
            return response.status_code == 200
    except Exception:
        return False


def _resilience_config(source_name: str, **overrides: object) -> ResilienceConfig:
    defaults: dict[str, object] = dict(
        source_name=source_name,
        max_concurrency=5,
        acquire_timeout_seconds=2.0,
        operation_timeout_seconds=5.0,
        failure_threshold=3,
        reset_timeout_seconds=10.0,
    )
    defaults.update(overrides)
    return ResilienceConfig(**defaults)


@pytest.fixture
async def postgres_executor() -> AsyncIterator[ResilientExecutor]:
    if not await _postgres_reachable():
        pytest.skip(
            f"Postgres de desarrollo no alcanzable en {POSTGRES_DSN!r}; "
            "levántalo con `docker compose -f docker-compose.dev.yml up -d`."
        )
    connector = PostgresConnector(
        "postgres", POSTGRES_DSN, min_pool_size=1, max_pool_size=5, pool_open_timeout_seconds=10.0
    )
    await connector.connect()
    try:
        yield ResilientExecutor(connector, _resilience_config("postgres"))
    finally:
        await connector.close()


@pytest.fixture
async def saldo_executor() -> AsyncIterator[ResilientExecutor]:
    if not await _saldo_api_reachable():
        pytest.skip(
            f"Stub de saldos no alcanzable en {SALDO_API_BASE_URL!r}; "
            "levántalo con `docker compose -f docker-compose.dev.yml up -d`."
        )
    connector = HttpConnector("saldo_api", base_url=SALDO_API_BASE_URL, request_timeout_seconds=5.0)
    await connector.connect()
    try:
        yield ResilientExecutor(
            connector, _resilience_config("saldo_api", infra_exceptions=HTTP_INFRA_EXCEPTIONS)
        )
    finally:
        await connector.close()


async def test_consultar_cliente_real(postgres_executor: ResilientExecutor) -> None:
    result = await _consultar_cliente_logic("V16760320", postgres_executor)
    assert result["nombre"] == "Ana María Restrepo"
    assert result["estado"] == "activo"


async def test_consultar_cliente_real_formato_con_puntos_y_guiones(postgres_executor: ResilientExecutor) -> None:
    # Mismo cliente que arriba, formato distinto: prueba la normalización
    # de punta a punta contra Postgres real, no solo en unitarios.
    result = await _consultar_cliente_logic("V-16.760.320", postgres_executor)
    assert result["nombre"] == "Ana María Restrepo"


async def test_consultar_saldo_real(saldo_executor: ResilientExecutor) -> None:
    result = await _consultar_saldo_logic("V16760320", saldo_executor)
    assert result["saldo"] == 1500000.50
    assert result["moneda"] == "COP"


async def test_resumen_cliente_real_caso_feliz(
    postgres_executor: ResilientExecutor, saldo_executor: ResilientExecutor
) -> None:
    result = await _resumen_cliente_logic("16.760.320", postgres_executor, saldo_executor)
    assert result["resumen_completo"] is True
    assert result["cliente"]["datos"]["nombre"] == "Ana María Restrepo"
    assert result["saldo"]["datos"]["saldo"] == 1500000.50


async def test_resumen_cliente_real_parcial_sin_saldo(
    postgres_executor: ResilientExecutor, saldo_executor: ResilientExecutor
) -> None:
    # V16760322 existe en Postgres pero NO en el stub de saldos (a
    # propósito, ver deploy/dev/postgres-seed.sql y saldo_api_stub.py).
    result = await _resumen_cliente_logic("V16760322", postgres_executor, saldo_executor)
    assert result["resumen_completo"] is False
    assert result["cliente"]["disponible"] is True
    assert result["saldo"]["disponible"] is False


async def test_resumen_cliente_real_saldo_api_error_simulado(
    postgres_executor: ResilientExecutor, saldo_executor: ResilientExecutor
) -> None:
    # La cédula reservada V90000001 hace que el stub responda 500 (fallo
    # real de infraestructura, no "no encontrado"): debe marcar el saldo
    # como no disponible sin tumbar la tool ni filtrar el 500 al resultado.
    result = await _resumen_cliente_logic("V90000001", postgres_executor, saldo_executor)
    assert result["saldo"]["disponible"] is False
    assert result["saldo"]["motivo"] == "el servicio de saldos no está disponible en este momento"
