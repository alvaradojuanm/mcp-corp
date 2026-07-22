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

import logging

import uvicorn

from mcp_corp.config import get_settings
from mcp_corp.logging_setup import configure_logging
from mcp_corp.server import create_server

logger = logging.getLogger(__name__)


def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)

    mcp = create_server(settings)
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
