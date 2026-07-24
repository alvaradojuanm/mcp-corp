"""Bug 1 (Fase 6): el registro de tools era todo-o-nada.

Con Postgres sano y `saldo_api` deshabilitado, el servidor real no
registraba NINGUNA tool — ni `consultar_cliente`, que solo depende de
Postgres. Estos tests consultan `tools/list` / `resources/list` /
`prompts/list` por el protocolo MCP real (`fastmcp.Client` en memoria
contra el server, sin transporte de red), no inspeccionan variables
internas: es la clase de test que debió existir desde la Fase 3 y que
habría atrapado este bug antes de llegar a producción.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from fastmcp import Client

from mcp_corp.config import Settings
from mcp_corp.connectors.registry import ConnectorRegistry
from mcp_corp.connectors.resilience import ResilienceConfig, ResilientExecutor
from mcp_corp.server import create_server


class FakeConnector:
    """Conector falso mínimo: siempre sano, `run()` delega en `operation(None)`."""

    def __init__(self, name: str) -> None:
        self.name = name

    async def connect(self) -> None: ...

    async def close(self) -> None: ...

    async def health(self) -> bool:
        return True

    async def run(self, operation: Callable[[Any], Awaitable[Any]]) -> Any:
        return await operation(None)


def _resilience_config(source_name: str) -> ResilienceConfig:
    return ResilienceConfig(
        source_name=source_name,
        max_concurrency=5,
        acquire_timeout_seconds=1.0,
        operation_timeout_seconds=1.0,
        failure_threshold=3,
        reset_timeout_seconds=10.0,
    )


def _make_registry(*, postgres: bool, saldo_api: bool) -> ConnectorRegistry:
    registry = ConnectorRegistry()
    if postgres:
        connector = FakeConnector("postgres")
        registry.register(connector, ResilientExecutor(connector, _resilience_config("postgres")))
    if saldo_api:
        connector = FakeConnector("saldo_api")
        registry.register(connector, ResilientExecutor(connector, _resilience_config("saldo_api")))
    return registry


def _settings() -> Settings:
    return Settings(environment="local", audit_hmac_secret="clave-de-test-suficientemente-larga")


# --- Matriz de registro de tools -----------------------------------------


@pytest.mark.parametrize(
    ("postgres", "saldo_api", "esperadas"),
    [
        (True, True, {"consultar_cliente", "consultar_saldo", "resumen_cliente"}),
        (True, False, {"consultar_cliente"}),
        (False, True, {"consultar_saldo"}),
        (False, False, set()),
    ],
)
async def test_tools_registradas_segun_conectores_disponibles(
    postgres: bool, saldo_api: bool, esperadas: set[str]
) -> None:
    registry = _make_registry(postgres=postgres, saldo_api=saldo_api)
    mcp = create_server(_settings(), registry)

    async with Client(mcp) as client:
        tools = await client.list_tools()

    assert {t.name for t in tools} == esperadas


async def test_caso_real_del_bug_postgres_sano_saldo_deshabilitado() -> None:
    """Reproduce exactamente el escenario visto en el despliegue real:
    Postgres con el circuito cerrado y sano, saldo_api deshabilitado —
    `consultar_cliente` debe seguir disponible por el protocolo MCP."""
    registry = _make_registry(postgres=True, saldo_api=False)
    assert await registry.get("postgres").connector.health() is True

    mcp = create_server(_settings(), registry)

    async with Client(mcp) as client:
        tools = await client.list_tools()

    names = {t.name for t in tools}
    assert names == {"consultar_cliente"}, (
        f"esperaba solo consultar_cliente (depende únicamente de Postgres, que está sano); "
        f"obtuve {names!r}"
    )


# --- Resource y Prompt: mismo criterio ------------------------------------


async def test_resource_estados_solo_si_postgres_disponible() -> None:
    mcp_con = create_server(_settings(), _make_registry(postgres=True, saldo_api=False))
    async with Client(mcp_con) as client:
        resources = await client.list_resources()
    assert any(str(r.uri) == "data://clientes/estados" for r in resources)

    mcp_sin = create_server(_settings(), _make_registry(postgres=False, saldo_api=True))
    async with Client(mcp_sin) as client:
        resources = await client.list_resources()
    assert not any(str(r.uri) == "data://clientes/estados" for r in resources)


async def test_prompt_atencion_cliente_solo_si_ambos_conectores_disponibles() -> None:
    mcp_ambos = create_server(_settings(), _make_registry(postgres=True, saldo_api=True))
    async with Client(mcp_ambos) as client:
        prompts = await client.list_prompts()
    assert any(p.name == "atencion_cliente" for p in prompts)

    mcp_parcial = create_server(_settings(), _make_registry(postgres=True, saldo_api=False))
    async with Client(mcp_parcial) as client:
        prompts = await client.list_prompts()
    assert not any(p.name == "atencion_cliente" for p in prompts)


# --- Log de arranque: claro sobre qué se registró y qué no, y por qué ----


async def test_log_de_arranque_explica_que_se_registro_y_que_no(
    caplog: pytest.LogCaptureFixture,
) -> None:
    registry = _make_registry(postgres=True, saldo_api=False)
    with caplog.at_level(logging.INFO, logger="mcp_corp.tools"):
        create_server(_settings(), registry)

    resumen = next(
        (r for r in caplog.records if r.message == "tools_registration_summary"), None
    )
    assert resumen is not None, "esperaba un log 'tools_registration_summary' al registrar tools"
    assert "consultar_cliente" in resumen.registradas
    assert "resumen_cliente" in resumen.omitidas
    # El motivo debe nombrar la fuente que falta, no ser genérico.
    assert "saldo_api" in resumen.omitidas["resumen_cliente"]
