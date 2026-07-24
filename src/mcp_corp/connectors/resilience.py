"""Capa genérica de resiliencia para conectores de datos.

Esta capa no sabe nada de Postgres, HTTP ni de ninguna fuente concreta: solo
sabe limitar concurrencia, cortar por timeout y abrir/cerrar un circuito.
Se escribe UNA vez aquí y envuelve a cualquier `Connector` (ver `base.py`).
Los conectores concretos (Postgres hoy, REST/legacy mañana) solo necesitan
saber hablarle a su fuente; la resiliencia la reciben envuelta.

Decisiones de diseño (ver README para el detalle extendido):
- Circuit breaker propio, no una librería de terceros: las semánticas
  (cerrado / abierto / medio-abierto, umbral de fallos, tiempo de reset) son
  simples y acotadas; cada dependencia externa en el camino crítico es
  superficie de supply chain que no necesitamos.
- `asyncio.BoundedSemaphore` (no `Semaphore`) por fuente: atrapa bugs de
  over-release (más `release()` que `acquire()`) con un `ValueError`
  explícito en vez de corromper el contador silenciosamente.
- El estado del breaker es POR RÉPLICA (en memoria de este proceso), a
  propósito: cada réplica descubre una fuente caída de forma independiente.
  No hay estado compartido vía Redis u otro backend en esta fase; queda
  como opción futura si la detección coordinada entre réplicas se vuelve
  necesaria.
- `asyncio.timeout()` (no `asyncio.wait_for`) para los cortes por tiempo:
  es la API estructurada recomendada desde Python 3.11, compone mejor con
  cancelación y no envuelve la corrutina en una tarea adicional.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, TypeVar

from mcp_corp.connectors.base import Connector

logger = logging.getLogger(__name__)

R = TypeVar("R")


# --- Errores hacia afuera: nunca filtran internals (stack traces, DSNs, SQL) ---


class ConnectorError(Exception):
    """Base de los errores que la capa de resiliencia expone hacia afuera."""


class CircuitOpenError(ConnectorError):
    """El circuito de la fuente está abierto: no se intenta la operación."""

    def __init__(self, source_name: str) -> None:
        super().__init__(f"fuente '{source_name}' no disponible temporalmente (circuito abierto)")


class ConcurrencyLimitExceededError(ConnectorError):
    """No se consiguió un slot de concurrencia dentro del tiempo de espera."""

    def __init__(self, source_name: str) -> None:
        super().__init__(f"fuente '{source_name}' saturada: límite de concurrencia excedido")


class OperationTimeoutError(ConnectorError):
    """La operación excedió su timeout configurado."""

    def __init__(self, source_name: str) -> None:
        super().__init__(f"operación contra '{source_name}' excedió el timeout")


class SourceUnavailableError(ConnectorError):
    """Un fallo de infraestructura impidió completar la operación."""

    def __init__(self, source_name: str) -> None:
        super().__init__(f"fuente '{source_name}' no disponible")


class CircuitState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass(frozen=True)
class ResilienceConfig:
    """Configuración de resiliencia de UNA fuente de datos."""

    source_name: str

    # Concurrencia: cuántas operaciones simultáneas se permiten hacia esta
    # fuente desde esta réplica, y cuánto se espera por un slot libre antes
    # de fallar limpio (nunca se encola infinito).
    max_concurrency: int
    acquire_timeout_seconds: float

    # Timeout de cada operación individual contra la fuente.
    operation_timeout_seconds: float

    # Circuit breaker: fallos de infraestructura consecutivos para abrir,
    # segundos antes de pasar a medio-abierto, y éxitos consecutivos en
    # medio-abierto para volver a cerrar.
    failure_threshold: int
    reset_timeout_seconds: float
    success_threshold: int = 1

    # Qué excepciones cuentan como fallo de infraestructura (abren el
    # circuito). Todo lo demás se considera error de negocio legítimo
    # (p. ej. una violación de constraint) y se propaga sin tocar el breaker.
    infra_exceptions: tuple[type[BaseException], ...] = (
        OSError,
        ConnectionError,
        TimeoutError,
    )

    # NOTA (hueco conocido, no resuelto en esta fase): el semáforo limita
    # CONCURRENCIA, no TASA. Si una fuente concreta declara un techo en
    # req/s (no en conexiones simultáneas), este campo queda como el punto
    # de extensión: hará falta un token bucket por encima del semáforo.
    rate_limit_per_second: float | None = None


class CircuitBreaker:
    """Circuit breaker de tres estados: cerrado, abierto, medio-abierto.

    Estado en memoria del proceso (por réplica), protegido por un
    `asyncio.Lock` porque las transiciones se disparan desde corrutinas
    concurrentes que comparten esta instancia.
    """

    def __init__(
        self,
        config: ResilienceConfig,
        *,
        clock: Callable[[], float] = time.monotonic,
        on_state_change: Callable[[str, CircuitState, CircuitState], None] | None = None,
    ) -> None:
        self._config = config
        self._clock = clock
        self._on_state_change = on_state_change
        self._lock = asyncio.Lock()

        self._state = CircuitState.CLOSED
        self._consecutive_failures = 0
        self._consecutive_successes = 0
        self._opened_at: float | None = None

    @property
    def state(self) -> CircuitState:
        return self._state

    async def before_call(self) -> None:
        """Levanta `CircuitOpenError` si el circuito no admite la llamada."""
        async with self._lock:
            if self._state is CircuitState.OPEN:
                assert self._opened_at is not None
                if self._clock() - self._opened_at >= self._config.reset_timeout_seconds:
                    self._transition(CircuitState.HALF_OPEN)
                else:
                    raise CircuitOpenError(self._config.source_name)

    async def record_success(self) -> None:
        async with self._lock:
            if self._state is CircuitState.HALF_OPEN:
                self._consecutive_successes += 1
                if self._consecutive_successes >= self._config.success_threshold:
                    self._transition(CircuitState.CLOSED)
            else:
                self._consecutive_failures = 0

    async def record_failure(self) -> None:
        async with self._lock:
            self._consecutive_successes = 0
            if self._state is CircuitState.HALF_OPEN:
                # Un solo fallo en medio-abierto reabre: la fuente todavía
                # no demostró estar recuperada.
                self._transition(CircuitState.OPEN)
                return
            self._consecutive_failures += 1
            if self._consecutive_failures >= self._config.failure_threshold:
                self._transition(CircuitState.OPEN)

    async def force_open(self) -> None:
        """Abre el circuito manualmente, sin que haya habido una operación
        real que cuente como fallo (Fase 6, Bug 2).

        Se usa cuando el conector no logra conectar (o no pasa su propio
        `health()`) al arrancar: sin esto, `/diagnostics` mostraría el
        circuito `closed` durante los primeros `failure_threshold`
        intentos reales, aunque la fuente ya se sabía caída desde el
        arranque. El resto del ciclo de vida del breaker (medio-abierto
        tras `reset_timeout_seconds`, cierre tras `success_threshold`
        éxitos) sigue funcionando igual después de esto — no hace falta
        ningún método de "recuperación" simétrico.
        """
        async with self._lock:
            if self._state is not CircuitState.OPEN:
                self._transition(CircuitState.OPEN)

    def _transition(self, new_state: CircuitState) -> None:
        old_state = self._state
        self._state = new_state
        if new_state is CircuitState.OPEN:
            self._opened_at = self._clock()
            self._consecutive_failures = 0
        elif new_state is CircuitState.HALF_OPEN:
            self._consecutive_successes = 0
        elif new_state is CircuitState.CLOSED:
            self._consecutive_failures = 0
            self._consecutive_successes = 0
            self._opened_at = None

        if old_state is not new_state:
            logger.info(
                "circuit_breaker_state_change",
                extra={
                    "source": self._config.source_name,
                    "from_state": old_state.value,
                    "to_state": new_state.value,
                },
            )
            if self._on_state_change is not None:
                self._on_state_change(self._config.source_name, old_state, new_state)


@dataclass
class ResilientExecutor:
    """Envuelve un `Connector` con límite de concurrencia, timeout y breaker.

    Es la ÚNICA pieza que sabe combinar las tres protecciones; no sabe nada
    de la fuente concreta que hay detrás del `Connector` que recibe.
    """

    connector: Connector[Any]
    config: ResilienceConfig
    clock: Callable[[], float] = field(default=time.monotonic)
    _semaphore: asyncio.BoundedSemaphore = field(init=False, repr=False)
    _breaker: CircuitBreaker = field(init=False, repr=False)
    _in_flight: int = field(default=0, init=False, repr=False)

    def __post_init__(self) -> None:
        self._semaphore = asyncio.BoundedSemaphore(self.config.max_concurrency)
        self._breaker = CircuitBreaker(self.config, clock=self.clock)

    async def run(self, operation: Callable[[Any], Awaitable[R]]) -> R:
        """Ejecuta `operation` contra la fuente con las tres protecciones.

        Orden: primero se consulta el breaker (barato, no toca la fuente);
        luego se reserva un slot de concurrencia con timeout; recién ahí se
        ejecuta la operación real con su propio timeout.
        """
        await self._breaker.before_call()

        try:
            async with asyncio.timeout(self.config.acquire_timeout_seconds):
                await self._semaphore.acquire()
        except TimeoutError:
            logger.warning(
                "concurrency_limit_saturated",
                extra={
                    "source": self.config.source_name,
                    "max_concurrency": self.config.max_concurrency,
                },
            )
            raise ConcurrencyLimitExceededError(self.config.source_name) from None

        self._in_flight += 1
        try:
            async with asyncio.timeout(self.config.operation_timeout_seconds):
                result = await self.connector.run(operation)
        except TimeoutError:
            await self._breaker.record_failure()
            logger.warning(
                "operation_timeout",
                extra={
                    "source": self.config.source_name,
                    "timeout_seconds": self.config.operation_timeout_seconds,
                },
            )
            raise OperationTimeoutError(self.config.source_name) from None
        except self.config.infra_exceptions:
            await self._breaker.record_failure()
            logger.error(
                "connector_infrastructure_failure",
                extra={"source": self.config.source_name},
                exc_info=True,
            )
            raise SourceUnavailableError(self.config.source_name) from None
        except Exception:
            # Error de negocio (constraint violado, validación, etc.): la
            # fuente SÍ respondió, solo que con un resultado que no nos
            # gusta. Es evidencia de que está viva, así que cuenta como
            # éxito para el breaker (no lo abre; en medio-abierto ayuda a
            # cerrarlo) y se propaga sin modificar al llamador.
            await self._breaker.record_success()
            raise
        else:
            await self._breaker.record_success()
            return result
        finally:
            self._in_flight -= 1
            self._semaphore.release()

    def snapshot(self) -> dict[str, Any]:
        """Estado actual para el endpoint de diagnóstico."""
        return {
            "source": self.config.source_name,
            "circuit_state": self._breaker.state.value,
            "in_flight": self._in_flight,
            "max_concurrency": self.config.max_concurrency,
        }

    async def force_open(self) -> None:
        """Abre el circuito manualmente (Fase 6, Bug 2) — ver `CircuitBreaker.force_open`."""
        await self._breaker.force_open()
