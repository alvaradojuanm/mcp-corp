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
que se conserva del identificador principal (p. ej. la cédula) es un
HMAC-SHA256 truncado (12 hex) — suficiente para correlacionar invocaciones
del mismo cliente entre líneas de log sin poder recuperar el valor
original a partir del log.

**Por qué HMAC-SHA256 y no un hash plano (`sha256(valor)`):** un hash
plano NO es irreversible aquí. El espacio de cédulas (6 a 10 dígitos) es
pequeño y enumerable — computar `sha256` de los ~10 mil millones de
cédulas posibles y armar una tabla arcoíris toma segundos en cualquier
laptop. Cualquiera con acceso al log (el agregador externo, un auditor,
un atacante) podría revertir el identificador sin ningún secreto. HMAC
con una clave (`audit_hmac_secret`, ver `config.py`) rompe ese ataque:
sin la clave, ni siquiera se puede empezar a construir la tabla arcoíris,
porque `HMAC(secreto, cédula)` no se puede precomputar sin conocer el
secreto. Sigue siendo determinista (mismo valor + misma clave = mismo
hash) y por lo tanto correlacionable, pero deja de ser reversible por
fuerza bruta desde fuera del server.

**Qué pasa si la clave rota:** `HMAC(clave_nueva, cédula) != HMAC(clave_vieja, cédula)`
para el mismo valor — es el comportamiento esperado, no un bug. Rotar la
clave rompe la correlación entre logs de ANTES y DESPUÉS de la rotación
para el mismo cliente (dos invocaciones de la misma cédula, una a cada
lado de la rotación, quedan con hashes distintos y no se pueden enlazar
solo mirando el log). Es exactamente el trade-off que se busca al rotar
por sospecha de compromiso: invalida la posibilidad de correlacionar
hacia atrás con la clave filtrada. Si se necesita continuidad de
correlación durante una ventana de rotación planificada (no por
incidente), la única forma es calcular el hash con AMBAS claves durante
esa ventana — no implementado en esta fase.
"""

from __future__ import annotations

import functools
import hmac
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from mcp_corp.logging_setup import correlation_id_var

logger = logging.getLogger("mcp_corp.audit")

R = TypeVar("R")


def _mask(value: Any, secret: bytes) -> str:
    digest = hmac.new(secret, str(value).encode("utf-8"), digestmod="sha256").hexdigest()[:12]
    return f"hmac-sha256:{digest}"


def audited_tool(
    tool_name: str,
    *,
    identifier_param: str | None = None,
    secret: bytes = b"",
    outcome_of: Callable[[R], str] | None = None,
) -> Callable[[Callable[..., Awaitable[R]]], Callable[..., Awaitable[R]]]:
    """Envuelve una tool para dejar auditoría de cada invocación en el log.

    `identifier_param`: nombre del parámetro que identifica al sujeto de
    negocio (p. ej. `cedula`); si se indica, su valor se enmascara con
    HMAC-SHA256 (ver docstring del módulo) en vez de registrarse u
    omitirse por completo.

    `secret`: clave del HMAC. En producción viene de
    `Settings.audit_hmac_secret` (ver `config.py`) y debe ser IGUAL en
    todas las réplicas para que la correlación entre logs de distintas
    réplicas funcione. El default `b""` solo existe para poder decorar
    tools que no tienen `identifier_param` sin tener que pasar una clave
    que no van a usar; nunca debe quedar vacío cuando `identifier_param`
    sí se indica en un entorno real.

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
                _mask(kwargs[identifier_param], secret)
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
