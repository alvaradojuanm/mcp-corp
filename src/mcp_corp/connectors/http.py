"""Conector concreto de una API REST (el stub de saldos de esta fase).

Solo sabe hablarle a un servicio HTTP: abrir y cerrar un `httpx.AsyncClient`,
ejecutar una operación contra él, y responder si el servicio está sano. No
implementa límite de concurrencia, timeout ni circuit breaker — eso lo
añade `resilience.ResilientExecutor` por fuera, exactamente igual que para
`postgres.PostgresConnector`: mismo protocolo `Connector` (`base.py`),
misma envoltura de resiliencia.

Es notablemente más simple que el conector de Postgres porque no hay un
pool que gestionar a mano: `httpx.AsyncClient` ya mantiene internamente su
propio pool de conexiones keep-alive, así que "abrir el conector" aquí es
solo instanciar el cliente. Esto valida que la capa de resiliencia de la
Fase 2 (diseñada sin conocer HTTP) efectivamente sirve para una fuente de
naturaleza distinta a Postgres.

`httpx==0.28.1` pineado (misma versión que ya traía `fastmcp`/`mcp` como
transitiva; pasa a dependencia directa porque ahora se usa en runtime, no
solo en tests).
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import TypeVar

import httpx

logger = logging.getLogger(__name__)

R = TypeVar("R")

# Qué excepciones de httpx cuentan como fallo de infraestructura para el
# circuit breaker de una fuente HTTP. `TransportError` cubre problemas de
# red/conexión/timeout de transporte; `HTTPStatusError` solo se levanta
# cuando el código de la operación llama a `raise_for_status()` — las
# tools de esta fase ya manejan el 404 ("no encontrado") como caso de
# negocio ANTES de llegar a `raise_for_status()`, así que lo que quede acá
# son fallos reales del servicio (5xx) o de la petición, no negocio.
HTTP_INFRA_EXCEPTIONS: tuple[type[BaseException], ...] = (
    httpx.TransportError,
    httpx.HTTPStatusError,
)


class HttpConnector:
    """Conector HTTP genérico sobre `httpx.AsyncClient`."""

    def __init__(
        self,
        name: str,
        base_url: str,
        *,
        request_timeout_seconds: float,
    ) -> None:
        self.name = name
        self._base_url = base_url
        self._request_timeout_seconds = request_timeout_seconds
        self._client: httpx.AsyncClient | None = None

    async def connect(self) -> None:
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=self._request_timeout_seconds,
        )
        logger.info("connector_http_client_opened", extra={"source": self.name})

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            logger.info("connector_http_client_closed", extra={"source": self.name})
            self._client = None

    async def health(self) -> bool:
        """GET `/health` real contra el servicio."""
        if self._client is None:
            return False
        try:
            response = await self._client.get("/health")
            return response.status_code == 200
        except Exception:
            logger.warning(
                "connector_health_check_failed",
                extra={"source": self.name},
                exc_info=True,
            )
            return False

    async def run(self, operation: Callable[[httpx.AsyncClient], Awaitable[R]]) -> R:
        """Ejecuta `operation` contra el cliente httpx ya abierto."""
        if self._client is None:
            raise RuntimeError(f"conector '{self.name}' no está conectado (llama a connect() primero)")
        return await operation(self._client)
