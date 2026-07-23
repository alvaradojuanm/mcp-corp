"""Logging de auditoría por invocación de tool.

El `correlation_id` (ver `logging_setup.py`) se preparó desde la Fase 1
esperando este momento: cada invocación de tool le asigna uno nuevo, así
que todo lo que se loguee durante esa invocación — incluidos los eventos
de `connectors/resilience.py` (circuito abierto, timeout, etc.) — queda
correlacionado bajo el mismo id en el agregador de logs.

Criterio de enmascaramiento (léelo si eres el auditor de este código):
los VALORES de los parámetros de negocio NUNCA se registran en claro. Un
log con cédulas, nombres o saldos de clientes es, en sí mismo, un problema
de cumplimiento, porque estos logs viajan a un agregador externo. Lo único
que se conserva del identificador principal (p. ej. la cédula) es un hash
truncado (`sha256`, 12 hex) — suficiente para correlacionar invocaciones
del mismo cliente entre líneas de log sin poder recuperar el valor
original a partir del log.
"""

from __future__ import annotations

import functools
import hashlib
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from mcp_corp.logging_setup import correlation_id_var

logger = logging.getLogger("mcp_corp.audit")

R = TypeVar("R")


def _mask(value: Any) -> str:
    digest = hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:12]
    return f"sha256:{digest}"


def audited_tool(
    tool_name: str,
    *,
    identifier_param: str | None = None,
    outcome_of: Callable[[R], str] | None = None,
) -> Callable[[Callable[..., Awaitable[R]]], Callable[..., Awaitable[R]]]:
    """Envuelve una tool para dejar auditoría de cada invocación en el log.

    `identifier_param`: nombre del parámetro que identifica al sujeto de
    negocio (p. ej. `cedula`); si se indica, su valor se enmascara con un
    hash truncado en vez de registrarse u omitirse por completo.

    `outcome_of`: función opcional que, a partir del resultado exitoso de
    la tool, decide si el resultado fue "success" o "partial" (p. ej. para
    `resumen_cliente`, cuando una de las dos fuentes no estuvo disponible).
    Por defecto, cualquier retorno sin excepción cuenta como "success".
    """

    def decorator(func: Callable[..., Awaitable[R]]) -> Callable[..., Awaitable[R]]:
        @functools.wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> R:
            token = correlation_id_var.set(uuid.uuid4().hex)
            started_at = time.monotonic()
            masked_identifier = (
                _mask(kwargs[identifier_param])
                if identifier_param is not None and identifier_param in kwargs
                else None
            )
            logger.info(
                "tool_invocation_started",
                extra={"tool": tool_name, "identifier": masked_identifier},
            )
            try:
                result = await func(*args, **kwargs)
            except Exception as exc:
                logger.info(
                    "tool_invocation_completed",
                    extra={
                        "tool": tool_name,
                        "identifier": masked_identifier,
                        "duration_ms": round((time.monotonic() - started_at) * 1000, 1),
                        "result": "failure",
                        "reason": str(exc),
                    },
                )
                raise
            else:
                outcome = outcome_of(result) if outcome_of is not None else "success"
                logger.info(
                    "tool_invocation_completed",
                    extra={
                        "tool": tool_name,
                        "identifier": masked_identifier,
                        "duration_ms": round((time.monotonic() - started_at) * 1000, 1),
                        "result": outcome,
                    },
                )
                return result
            finally:
                correlation_id_var.reset(token)

        return wrapper

    return decorator
