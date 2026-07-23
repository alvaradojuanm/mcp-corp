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
    OpenShift/Kubernetes para decidir si debe recibir tráfico nuevo.
    `/ready` NUNCA se acopla a la salud de los conectores (Postgres, APIs,
    etc.) — ver "Decisiones de diseño" en el README para el razonamiento
    completo. En corto: si el breaker de una fuente se abre y eso tumbara
    `/ready`, el balanceador sacaría la réplica ENTERA de rotación,
    incluidas las tools que no dependen de esa fuente y sí funcionan. El
    estado de los conectores se expone aparte, en `/diagnostics`.
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
from mcp_corp.connectors.registry import ConnectorRegistry
from mcp_corp.tools import register_prompts, register_resources, register_tools

logger = logging.getLogger(__name__)


@dataclass
class AppState:
    """Estado de ciclo de vida del proceso (no de negocio)."""

    ready: bool = False


def create_server(settings: Settings, registry: ConnectorRegistry | None = None) -> FastMCP:
    """Construye el server FastMCP: rutas de salud, tools, Resource y Prompt.

    `registry` agrupa los conectores de datos (Postgres, API de saldos);
    si se pasa, sus fuentes se conectan al arrancar y se cierran al
    apagar, atado al mismo lifespan que ya maneja `AppState.ready` — sin
    acoplar `/ready` a su salud. Las tools de negocio (`tools.py`) solo se
    registran si sus conectores están presentes en el registry.

    `mask_error_details=True`: defensa en profundidad además del manejo
    explícito de errores en `tools.py` — cualquier excepción que NO sea un
    `ToolError` (es decir, un bug no contemplado) se enmascara con un
    mensaje genérico hacia el cliente en vez de reenviar el traceback.
    """
    state = AppState()
    connector_registry = registry if registry is not None else ConnectorRegistry()

    @asynccontextmanager
    async def lifespan(_app: FastMCP) -> AsyncIterator[None]:
        await connector_registry.connect_all()
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
            await connector_registry.close_all()

    mcp: FastMCP = FastMCP(name=settings.service_name, lifespan=lifespan, mask_error_details=True)

    register_tools(mcp, connector_registry)
    register_resources(mcp)
    register_prompts(mcp)

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

    @mcp.custom_route("/diagnostics", methods=["GET"])
    async def diagnostics(_request: Request) -> JSONResponse:
        """Estado de cada conector (breaker, pool, última salud). No afecta
        el balanceo: solo observabilidad manual/alertas, separado de
        `/health` y `/ready` a propósito (ver docstring del módulo)."""
        return JSONResponse({"connectors": await connector_registry.diagnostics()})

    return mcp
