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
# Los conectores y tools lo fijan al entrar y lo limpian al salir (ver
# `audit.audited_tool`), para que todo lo que se loguee durante esa
# invocación —incluidos los eventos de connectors/resilience.py— quede
# correlacionado bajo el mismo id en el agregador de logs.
correlation_id_var: ContextVar[str] = ContextVar("correlation_id", default="")

# Atributos propios de un `logging.LogRecord` "vacío": todo lo que NO esté
# en este conjunto vino de `extra={...}` en la llamada al logger y debe
# volcarse al JSON. Sin esto, `logger.info(msg, extra={"source": ...})` se
# registra pero el campo `source` desaparece en silencio — exactamente el
# bug que tenían los logs de conectores y de auditoría de tools hasta que
# se detectó probando la Fase 3 de punta a punta.
_RECORD_RESERVED_ATTRS: frozenset[str] = frozenset(
    vars(logging.LogRecord("", 0, "", 0, "", (), None)).keys()
) | {"message", "asctime"}


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

        for key, value in record.__dict__.items():
            if key not in _RECORD_RESERVED_ATTRS and key not in payload:
                payload[key] = value

        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)

        return json.dumps(payload, ensure_ascii=False, default=str)


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
