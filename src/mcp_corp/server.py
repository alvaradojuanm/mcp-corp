"""Bootstrap del servidor FastMCP: instancia, rutas HTTP y ciclo de vida.

Decisiones clave (ver README para el detalle):
- Streamable HTTP como transporte, vía `FastMCP.http_app()`, para exponer un
  ASGI app que corremos con uvicorn y así controlar explícitamente host,
  puerto y el timeout de apagado graceful.
- `/health` (liveness) y `/ready` (readiness) son endpoints DISTINTOS:
  * `/health` responde 200 mientras el proceso esté vivo y pueda atender
    HTTP; no depende de ningún recurso externo. Lo usa el healthcheck de
    Docker/Swarm y la liveness probe de OpenShift/Kubernetes.
  * `/ready` responde 200 solo cuando el server terminó su arranque y no
    está en proceso de apagado; lo usa la readiness probe de
    OpenShift/Kubernetes para decidir si debe recibir tráfico nuevo. En esta
    fase no hay conectores que verificar, así que readiness == "arrancado y
    no apagándose"; en fases futuras se ampliará para chequear el estado de
    los conectores (pools, circuit breakers).
- No hay estado de negocio en el proceso: el único estado mutable aquí es la
  bandera de disponibilidad (`AppState.ready`), que es infraestructura de
  ciclo de vida, no estado de aplicación. Esto es compatible con el diseño
  stateless-por-invocación.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

from fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse

from mcp_corp.config import Settings

logger = logging.getLogger(__name__)


@dataclass
class AppState:
    """Estado de ciclo de vida del proceso (no de negocio)."""

    ready: bool = False


def create_server(settings: Settings) -> FastMCP:
    """Construye el server FastMCP con lifecycle y rutas de salud registradas.

    No registra ninguna tool: eso es alcance de fases futuras.
    """
    state = AppState()

    @asynccontextmanager
    async def lifespan(_app: FastMCP) -> AsyncIterator[None]:
        logger.info(
            "startup_complete",
            extra={"service": settings.service_name, "environment": settings.environment},
        )
        state.ready = True
        try:
            yield
        finally:
            # A partir de aquí el proceso está en apagado: dejamos de
            # anunciarnos como listos para que la readiness probe / el
            # balanceador retire esta réplica del pool antes de que termine
            # de drenar sus conexiones en curso.
            state.ready = False
            logger.info("shutdown_initiated", extra={"service": settings.service_name})

    mcp: FastMCP = FastMCP(name=settings.service_name, lifespan=lifespan)

    @mcp.custom_route("/health", methods=["GET"])
    async def health(_request: Request) -> JSONResponse:
        """Liveness: el proceso está vivo y puede responder HTTP."""
        return JSONResponse({"status": "ok", "service": settings.service_name})

    @mcp.custom_route("/ready", methods=["GET"])
    async def ready(_request: Request) -> JSONResponse:
        """Readiness: el proceso terminó de arrancar y no se está apagando."""
        if state.ready:
            return JSONResponse({"status": "ready", "service": settings.service_name})
        return JSONResponse(
            {"status": "not_ready", "service": settings.service_name},
            status_code=503,
        )

    return mcp
