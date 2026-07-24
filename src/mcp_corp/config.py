"""Configuración del servicio, cargada exclusivamente desde variables de entorno.

Sigue el principio 12-factor de "config en el entorno": no hay valores de
configuración hardcodeados en el código ni archivos de config locales; todo
se puede sobreescribir vía variables de entorno (o un `.env` en desarrollo).

**Secretos de Docker Swarm / Kubernetes (Fase 5).** Swarm monta cada
secreto como un ARCHIVO en `/run/secrets/<nombre>`, no como variable de
entorno — y pydantic-settings solo sabe leer variables de entorno. Se
resuelve con el patrón `<VAR>_FILE` (el mismo que usan las imágenes
oficiales de Postgres/MySQL/Redis en Docker Hub): si existe
`MCP_CORP_AUDIT_HMAC_SECRET_FILE=/run/secrets/audit_hmac_secret`, su
contenido se vuelca a `MCP_CORP_AUDIT_HMAC_SECRET` antes de construir
`Settings` (ver `_load_file_secrets_into_environ`).

**¿Por qué `_FILE` y no `secrets_dir` de pydantic-settings?**
pydantic-settings sí trae soporte nativo (`SettingsConfigDict(secrets_dir=...)`),
pero resuelve los nombres de archivo esperados a partir del nombre de cada
CAMPO, y su comportamiento documentado para modelos anidados con
`env_nested_delimiter` (nuestro caso: `postgres.dsn`, `saldo_api.*`) no es
consistente ni está bien cubierto en la documentación — habría que
adivinar o probar caso por caso qué nombre de archivo espera para cada
campo anidado. El patrón `_FILE` opera sobre el NOMBRE DE LA VARIABLE DE
ENTORNO tal cual pydantic-settings ya la resuelve (incluyendo el prefijo
`MCP_CORP_` y el delimitador `__`), así que cubre automáticamente
CUALQUIER variable, anidada o no, presente o futura, sin tener que
enumerar cuáles son sensibles ni conocer los detalles internos de
resolución de pydantic-settings. Es exactamente el mismo mecanismo que ya
resolvimos a mano en decenas de imágenes Docker de terceros — predecible,
fácil de auditar, y trivial de probar de forma aislada (ver
`tests/test_config.py`).
"""

from __future__ import annotations

import os
from pathlib import Path

from pydantic import BaseModel, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_ENV_PREFIX = "MCP_CORP_"
_FILE_SUFFIX = "_FILE"


def _load_file_secrets_into_environ(environ: "os._Environ[str] | dict[str, str]" = os.environ) -> None:
    """Resuelve `MCP_CORP_*_FILE` leyendo el archivo que apuntan, en `environ`.

    Para cada `MCP_CORP_X_FILE=/ruta/al/secreto` presente, deja
    `MCP_CORP_X=<contenido del archivo, sin espacios/saltos de línea al
    borde>` en `environ`, ÚNICAMENTE si `MCP_CORP_X` no existe ya.

    Precedencia resultante (de mayor a menor): variable de entorno real
    explícita > archivo referenciado por `_FILE` > `.env` de desarrollo >
    default del campo. Así, si `MCP_CORP_X` ya viene seteada (p. ej. desde
    un `.env` cargado por pydantic-settings más tarde, o exportada a mano),
    la variante `_FILE` se ignora en silencio — el flujo de desarrollo
    local con `.env` sigue funcionando exactamente igual que antes de esta
    fase.

    Recibe `environ` como parámetro (por defecto `os.environ`) para poder
    probarse sin depender de mutar el entorno real del proceso de test.
    """
    for key, file_path in list(environ.items()):
        if not key.startswith(_ENV_PREFIX) or not key.endswith(_FILE_SUFFIX):
            continue
        target_key = key[: -len(_FILE_SUFFIX)]
        if target_key in environ:
            continue  # variable real explícita gana, ver docstring
        try:
            environ[target_key] = Path(file_path).read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise RuntimeError(f"{key} apunta a '{file_path}' pero no se pudo leer: {exc}") from exc


class IdentifiersSettings(BaseModel):
    """Config de la normalización/validación de identificadores (Fase 4).

    Ver `identifiers.py` para el módulo completo y el README para el
    detalle de cómo se verificó el algoritmo del dígito verificador.
    """

    incluir_prefijo_c: bool = Field(
        default=False,
        description=(
            "Habilita el prefijo 'C' (comunas/consejos comunales/organizaciones del Poder "
            "Popular). Existe desde un anuncio oficial de 2015, pero las fuentes consultadas "
            "difieren sobre si sigue vigente en el set actual del SENIAT — por eso queda "
            "detrás de este flag en vez de cableado por defecto."
        ),
    )
    validar_digito_verificador: bool = Field(
        default=True,
        description=(
            "Si True, cuando el identificador trae dígito verificador (RIF completo) se "
            "valida el checksum módulo 11 antes de tocar cualquier conector. Algoritmo "
            "verificado cruzando 3 implementaciones independientes y un ejemplo real conocido "
            "(V-13222105-3); ver README. Si el verificador falta (cédula sin RIF completo), "
            "NO se rechaza — es el caso normal."
        ),
    )


class PostgresSettings(BaseModel):
    """Config de la fuente Postgres: conexión + resiliencia propia.

    Todos los campos de resiliencia (concurrencia, timeouts, breaker) son
    por-fuente a propósito: la saturación de una fuente nunca debe robarle
    presupuesto de espera o de circuito a otra (ver `connectors/resilience.py`).
    """

    enabled: bool = Field(
        default=False,
        description="Si es False, el conector no se instancia ni se conecta al arrancar.",
    )
    dsn: str = Field(
        default="",
        description="Cadena de conexión libpq completa. Solo desde entorno/.env, nunca hardcodeada.",
    )

    # Pool de conexiones (psycopg_pool.AsyncConnectionPool)
    min_pool_size: int = Field(default=1, description="Conexiones mínimas mantenidas abiertas por réplica.")
    max_pool_size: int = Field(default=10, description="Conexiones máximas por réplica.")
    pool_open_timeout_seconds: float = Field(
        default=10.0,
        description="Segundos a esperar a que el pool alcance min_pool_size al arrancar.",
    )

    # Resiliencia (ver ResilienceConfig en connectors/resilience.py)
    max_concurrency: int = Field(
        default=10,
        description="Operaciones simultáneas permitidas hacia Postgres desde esta réplica.",
    )
    acquire_timeout_seconds: float = Field(
        default=2.0,
        description="Segundos a esperar por un slot de concurrencia antes de fallar limpio.",
    )
    operation_timeout_seconds: float = Field(
        default=5.0,
        description="Timeout por operación individual contra Postgres.",
    )
    circuit_failure_threshold: int = Field(
        default=5,
        description="Fallos de infraestructura consecutivos para abrir el circuito.",
    )
    circuit_reset_timeout_seconds: float = Field(
        default=30.0,
        description="Segundos que el circuito permanece abierto antes de pasar a medio-abierto.",
    )
    circuit_success_threshold: int = Field(
        default=2,
        description="Éxitos consecutivos en medio-abierto requeridos para volver a cerrar el circuito.",
    )

    # Hueco conocido, no resuelto en esta fase: el semáforo de arriba limita
    # CONCURRENCIA (operaciones simultáneas), no TASA (operaciones por
    # segundo). Si Postgres (o el PgBouncer delante) declara un techo en
    # req/s, este campo es el punto de extensión: hará falta un token
    # bucket por encima del semáforo, que hoy no existe.
    rate_limit_per_second: float | None = Field(
        default=None,
        description="Reservado para un futuro limitador de tasa; no aplicado todavía.",
    )


class SaldoApiSettings(BaseModel):
    """Config de la fuente de saldos (API REST) + resiliencia propia.

    Misma filosofía que `PostgresSettings`: concurrencia, timeouts y breaker
    son por-fuente, nunca compartidos con Postgres ni con ninguna otra API
    que se sume después.
    """

    enabled: bool = Field(
        default=False,
        description="Si es False, el conector no se instancia ni se conecta al arrancar.",
    )
    base_url: str = Field(
        default="",
        description="URL base del servicio de saldos (esquema, host y puerto), p. ej. http://localhost:8080.",
    )
    request_timeout_seconds: float = Field(
        default=5.0,
        description=(
            "Timeout del cliente httpx a nivel de transporte. Es una defensa adicional al "
            "operation_timeout_seconds de la capa de resiliencia, no un sustituto."
        ),
    )

    # Resiliencia (ver ResilienceConfig en connectors/resilience.py)
    max_concurrency: int = Field(
        default=10,
        description="Operaciones simultáneas permitidas hacia el servicio de saldos desde esta réplica.",
    )
    acquire_timeout_seconds: float = Field(
        default=2.0,
        description="Segundos a esperar por un slot de concurrencia antes de fallar limpio.",
    )
    operation_timeout_seconds: float = Field(
        default=5.0,
        description="Timeout por operación individual contra el servicio de saldos.",
    )
    circuit_failure_threshold: int = Field(
        default=5,
        description="Fallos de infraestructura consecutivos para abrir el circuito.",
    )
    circuit_reset_timeout_seconds: float = Field(
        default=30.0,
        description="Segundos que el circuito permanece abierto antes de pasar a medio-abierto.",
    )
    circuit_success_threshold: int = Field(
        default=2,
        description="Éxitos consecutivos en medio-abierto requeridos para volver a cerrar el circuito.",
    )

    # Hueco conocido, no resuelto en esta fase: el semáforo limita
    # CONCURRENCIA, no TASA. Si el servicio de saldos declara un techo en
    # req/s, este campo es el punto de extensión: hará falta un token
    # bucket por encima del semáforo, que hoy no existe.
    rate_limit_per_second: float | None = Field(
        default=None,
        description="Reservado para un futuro limitador de tasa; no aplicado todavía.",
    )


class Settings(BaseSettings):
    """Configuración del servidor MCP, con defaults sensatos para local/dev."""

    model_config = SettingsConfigDict(
        env_prefix="MCP_CORP_",
        env_nested_delimiter="__",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Identidad del servicio
    service_name: str = Field(
        default="mcp-corp",
        description="Nombre lógico del servicio, usado en logs y como nombre del server MCP.",
    )
    environment: str = Field(
        default="local",
        description="Entorno de ejecución: local | staging | production.",
    )

    # Red / transporte HTTP
    host: str = Field(
        default="0.0.0.0",
        description="Interfaz de red donde escucha el servidor Streamable HTTP.",
    )
    port: int = Field(
        default=8000,
        description="Puerto donde escucha el servidor Streamable HTTP.",
    )

    # Logging
    log_level: str = Field(
        default="INFO",
        description="Nivel de log estándar de Python (DEBUG, INFO, WARNING, ERROR, CRITICAL).",
    )

    # Conectores: un bloque de config por fuente. Cada fuente nueva (API
    # REST, sistema legacy) suma su propio `*Settings` aquí, con su propia
    # concurrencia, timeout y breaker — nunca comparten presupuesto.
    postgres: PostgresSettings = Field(default_factory=PostgresSettings)
    saldo_api: SaldoApiSettings = Field(default_factory=SaldoApiSettings)

    # Normalización/validación de identificadores venezolanos (Fase 4).
    identifiers: IdentifiersSettings = Field(default_factory=IdentifiersSettings)

    # Auditoría (ver audit.py): clave del HMAC-SHA256 usado para
    # enmascarar identificadores de negocio (p. ej. cédulas) en el log.
    # DEBE ser igual en todas las réplicas — si cada una tuviera una
    # clave distinta, la misma cédula produciría hashes distintos según
    # qué réplica atendió la invocación, rompiendo la correlación entre
    # logs de réplicas diferentes. Genera un valor con:
    # `python -c "import secrets; print(secrets.token_hex(32))"`
    audit_hmac_secret: str = Field(
        default="",
        description=(
            "Clave secreta para el HMAC-SHA256 del log de auditoría. Vacío en el default de "
            "desarrollo (el enmascaramiento sigue funcionando, pero con clave débil); en "
            "producción debe ser un secreto largo y aleatorio, igual en todas las réplicas."
        ),
    )

    # Apagado
    graceful_shutdown_timeout_seconds: float = Field(
        default=10.0,
        description="Segundos de gracia para drenar conexiones en curso al recibir SIGTERM.",
    )

    @model_validator(mode="after")
    def _fail_closed_en_produccion_sin_clave_hmac(self) -> "Settings":
        """Fail-closed (Fase 4, Parte B): en producción, sin clave el proceso NO arranca.

        En cualquier otro entorno (`local`, `staging`), una clave vacía solo
        registra un warning al construir el server (ver `server.py`) — el
        enmascaramiento sigue siendo determinista pero con clave débil, algo
        aceptable para desarrollo y no para el entorno que sí produce logs
        que se auditan de verdad. `environment` se decide por variable de
        entorno (`MCP_CORP_ENVIRONMENT`), igual que el resto de la config.
        """
        if self.environment == "production" and not self.audit_hmac_secret:
            raise ValueError(
                "MCP_CORP_AUDIT_HMAC_SECRET es obligatorio cuando MCP_CORP_ENVIRONMENT=production. "
                "Un HMAC con clave vacía es determinista y públicamente reproducible: el "
                "enmascaramiento de auditoría no protegería nada. El proceso no arranca así."
            )
        return self


def get_settings() -> Settings:
    """Construye la configuración desde el entorno.

    Antes de construir `Settings`, resuelve cualquier secreto montado como
    archivo (`MCP_CORP_*_FILE`, ver `_load_file_secrets_into_environ`) —
    necesario para correr bajo Docker Swarm/Kubernetes, donde los secretos
    llegan como archivos, no como variables de entorno.

    No se cachea a nivel de módulo a propósito: mantiene la función libre de
    estado oculto y trivial de testear con distintos entornos.
    """
    _load_file_secrets_into_environ()
    return Settings()
