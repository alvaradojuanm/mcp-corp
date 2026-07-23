"""Entrypoint del proceso: `python -m mcp_corp`.

Arranca el server FastMCP como ASGI app sobre uvicorn, manejado
explícitamente (en vez de `mcp.run(...)`) para controlar el timeout de
apagado graceful ante SIGTERM/SIGINT vía `Config.timeout_graceful_shutdown`.
Uvicorn instala los signal handlers por nosotros: al recibir SIGTERM deja de
aceptar conexiones nuevas, espera a que terminen las conexiones en curso
(hasta el timeout configurado) y solo entonces dispara el shutdown del
lifespan del server (donde marcamos `ready = False`, ver `server.py`).
"""

from __future__ import annotations

import asyncio
import logging
import sys

import uvicorn

from mcp_corp.config import Settings, get_settings
from mcp_corp.connectors.http import HTTP_INFRA_EXCEPTIONS, HttpConnector
from mcp_corp.connectors.postgres import PostgresConnector
from mcp_corp.connectors.registry import ConnectorRegistry
from mcp_corp.connectors.resilience import ResilienceConfig, ResilientExecutor
from mcp_corp.logging_setup import configure_logging
from mcp_corp.server import create_server

logger = logging.getLogger(__name__)


def _build_connector_registry(settings: Settings) -> ConnectorRegistry:
    """Instancia y registra los conectores habilitados por configuración."""
    registry = ConnectorRegistry()

    if settings.postgres.enabled:
        pg_settings = settings.postgres
        connector = PostgresConnector(
            name="postgres",
            dsn=pg_settings.dsn,
            min_pool_size=pg_settings.min_pool_size,
            max_pool_size=pg_settings.max_pool_size,
            pool_open_timeout_seconds=pg_settings.pool_open_timeout_seconds,
        )
        resilience_config = ResilienceConfig(
            source_name="postgres",
            max_concurrency=pg_settings.max_concurrency,
            acquire_timeout_seconds=pg_settings.acquire_timeout_seconds,
            operation_timeout_seconds=pg_settings.operation_timeout_seconds,
            failure_threshold=pg_settings.circuit_failure_threshold,
            reset_timeout_seconds=pg_settings.circuit_reset_timeout_seconds,
            success_threshold=pg_settings.circuit_success_threshold,
            rate_limit_per_second=pg_settings.rate_limit_per_second,
        )
        registry.register(connector, ResilientExecutor(connector, resilience_config))

    if settings.saldo_api.enabled:
        api_settings = settings.saldo_api
        connector = HttpConnector(
            name="saldo_api",
            base_url=api_settings.base_url,
            request_timeout_seconds=api_settings.request_timeout_seconds,
        )
        resilience_config = ResilienceConfig(
            source_name="saldo_api",
            max_concurrency=api_settings.max_concurrency,
            acquire_timeout_seconds=api_settings.acquire_timeout_seconds,
            operation_timeout_seconds=api_settings.operation_timeout_seconds,
            failure_threshold=api_settings.circuit_failure_threshold,
            reset_timeout_seconds=api_settings.circuit_reset_timeout_seconds,
            success_threshold=api_settings.circuit_success_threshold,
            infra_exceptions=HTTP_INFRA_EXCEPTIONS,
            rate_limit_per_second=api_settings.rate_limit_per_second,
        )
        registry.register(connector, ResilientExecutor(connector, resilience_config))

    return registry


def main() -> None:
    if sys.platform == "win32":
        # psycopg3 en modo async no soporta el ProactorEventLoop, que es el
        # default de asyncio en Windows. Solo importa para desarrollo local
        # con el conector Postgres habilitado; el contenedor de producción
        # corre en Linux y no necesita esto.
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    settings = get_settings()
    configure_logging(settings.log_level)

    registry = _build_connector_registry(settings)
    mcp = create_server(settings, registry)
    app = mcp.http_app()

    config = uvicorn.Config(
        app=app,
        host=settings.host,
        port=settings.port,
        log_config=None,  # logging propio (JSON estructurado), no el default de uvicorn
        timeout_graceful_shutdown=int(settings.graceful_shutdown_timeout_seconds),
    )
    server = uvicorn.Server(config)

    logger.info(
        "starting_server",
        extra={
            "service": settings.service_name,
            "host": settings.host,
            "port": settings.port,
            "environment": settings.environment,
        },
    )
    server.run()


if __name__ == "__main__":
    main()
