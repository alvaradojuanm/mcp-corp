"""Logging estructurado en JSON hacia stdout.

Decisiones:
- JSON a stdout (no a archivo) porque el proceso es stateless y corre en
  contenedores efímeros; el agregador de logs (Docker/Portainer hoy,
  OpenShift/Kubernetes mañana) es responsable de recolectar stdout.
- Se deja preparado un `correlation_id` por request vía `contextvars`, aunque
  todavía no hay tools ni requests de negocio: la infraestructura de
  auditoría debe existir desde el andamiaje, no añadirse después como parche.
"""

from __future__ import annotations

import json
import logging
import sys
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any

# Correlation id de la request/invocación en curso. Vacío fuera de un request.
# Los conectores y tools de fases futuras deben fijarlo al entrar y limpiarlo
# al salir (p. ej. vía middleware o un context manager).
correlation_id_var: ContextVar[str] = ContextVar("correlation_id", default="")


class JSONFormatter(logging.Formatter):
    """Formatea cada registro de log como una línea JSON."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(
                record.created, tz=timezone.utc
            ).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        correlation_id = correlation_id_var.get()
        if correlation_id:
            payload["correlation_id"] = correlation_id

        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)

        return json.dumps(payload, ensure_ascii=False)


def configure_logging(log_level: str) -> None:
    """Configura el logging raíz para emitir JSON a stdout.

    Idempotente: reemplaza los handlers existentes en vez de acumularlos, para
    que pueda llamarse una sola vez en el arranque sin efectos secundarios si
    se invoca más de una vez (por ejemplo, en tests).
    """
    root_logger = logging.getLogger()
    root_logger.setLevel(log_level.upper())

    handler = logging.StreamHandler(stream=sys.stdout)
    handler.setFormatter(JSONFormatter())

    root_logger.handlers.clear()
    root_logger.addHandler(handler)
