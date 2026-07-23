"""Tools, Resource y Prompt de negocio de las Fases 3 y 4.

Demuestra el patrón heterogéneo: `consultar_cliente` habla con PostgreSQL
(driver nativo), `consultar_saldo` habla con una API REST, y ambas quedan
detrás de la MISMA interfaz de tool — el modelo que las invoca no puede
(ni necesita) distinguir de dónde sale cada dato. Toda la heterogeneidad
vive encapsulada en `connectors/`; este módulo solo orquesta.

Disciplina de tools: tres tools por INTENCIÓN DE NEGOCIO, no una por
endpoint. Las descripciones de abajo las lee un modelo, no un humano — son
la interfaz real del server, y son el punto donde más se gana o se pierde
calidad de decisión del agente.

Identificadores (Fase 4): el parámetro que reciben las tres tools acepta
cualquier formato común de cédula/RIF venezolano — la normalización vive
en `identifiers.py`, no aquí. Un identificador inválido se rechaza ANTES
de invocar cualquier `ResilientExecutor.run()`: nunca consume un slot de
concurrencia ni abre una conexión.
"""

import asyncio
import json
import logging
from typing import Annotated, Any

import httpx
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from pydantic import Field
from psycopg import AsyncConnection
from psycopg.rows import dict_row

from mcp_corp.audit import audited_tool
from mcp_corp.connectors.registry import ConnectorRegistry
from mcp_corp.connectors.resilience import ConnectorError, ResilientExecutor
from mcp_corp.identifiers import (
    PREFIJOS_BASE,
    IdentidadFiscal,
    IdentificadorInvalidoError,
    digito_verificador_es_valido,
    normalizar,
)

logger = logging.getLogger(__name__)

Identificador = Annotated[
    str,
    Field(
        description=(
            "Cédula o RIF venezolano en cualquier formato común: con o sin puntos de millar, "
            "con o sin guiones, con o sin letra de prefijo (V, E, J, G, P), en mayúscula o "
            "minúscula. No hace falta limpiarlo antes de pasarlo. Ejemplos, todos equivalentes: "
            "'16760320', '16.760.320', 'V16760320', 'V-16.760.320'. También acepta un RIF "
            "completo con dígito verificador, p. ej. 'J-16760320-2'."
        ),
        max_length=20,
    ),
]


def _resolve_identidad(
    valor: str,
    *,
    prefijos: frozenset[str],
    validar_checksum: bool,
) -> IdentidadFiscal:
    """Normaliza y (opcionalmente) valida el checksum, sin tocar ninguna fuente.

    Es la primera línea de defensa del semáforo/pool: un identificador mal
    tipeado se rechaza aquí, antes de que cualquier `ResilientExecutor`
    reserve un slot de concurrencia o abra una conexión.
    """
    try:
        identidad = normalizar(valor, prefijos=prefijos)
    except IdentificadorInvalidoError:
        raise ToolError(
            "El identificador no tiene un formato reconocido. Verifica que sea una cédula o "
            "RIF venezolano (con o sin puntos, guiones o letra de prefijo)."
        ) from None

    if validar_checksum and not digito_verificador_es_valido(identidad):
        raise ToolError(
            "El dígito verificador del identificador no es válido. Revisa que el RIF esté bien tipeado."
        ) from None

    return identidad


# --- Postgres: consultar_cliente ---------------------------------------


async def _query_cliente(conn: AsyncConnection, cedula: str) -> dict[str, Any] | None:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT cedula, nombre, email, estado FROM clientes WHERE cedula = %s",
            (cedula,),
        )
        return await cur.fetchone()


async def _consultar_cliente_logic(
    valor: str,
    postgres_executor: ResilientExecutor,
    *,
    prefijos: frozenset[str] = PREFIJOS_BASE,
    validar_checksum: bool = True,
) -> dict[str, Any]:
    identidad = _resolve_identidad(valor, prefijos=prefijos, validar_checksum=validar_checksum)
    try:
        row = await postgres_executor.run(lambda conn: _query_cliente(conn, identidad.cedula))
    except ConnectorError:
        # Mensaje de negocio, nunca el detalle técnico (ver ConnectorError:
        # ya llega sanitizado desde resilience.py, pero igual no lo
        # reenviamos tal cual — el texto es nuestro, no el de la excepción).
        raise ToolError(
            "La base de datos de clientes no está disponible en este momento. Intenta de nuevo en unos segundos."
        ) from None
    if row is None:
        raise ToolError(f"No se encontró ningún cliente con el identificador {identidad.cedula}.")
    return row


# --- HTTP: consultar_saldo ----------------------------------------------


async def _query_saldo(client: httpx.AsyncClient, cedula: str) -> dict[str, Any] | None:
    response = await client.get(f"/saldos/{cedula}")
    if response.status_code == 404:
        return None
    response.raise_for_status()
    return response.json()


async def _consultar_saldo_logic(
    valor: str,
    saldo_executor: ResilientExecutor,
    *,
    prefijos: frozenset[str] = PREFIJOS_BASE,
    validar_checksum: bool = True,
) -> dict[str, Any]:
    identidad = _resolve_identidad(valor, prefijos=prefijos, validar_checksum=validar_checksum)
    try:
        data = await saldo_executor.run(lambda client: _query_saldo(client, identidad.cedula))
    except ConnectorError:
        raise ToolError(
            "El servicio de saldos no está disponible en este momento. Intenta de nuevo en unos segundos."
        ) from None
    if data is None:
        raise ToolError(f"No se encontró saldo registrado para el identificador {identidad.cedula}.")
    return data


# --- Compuesta: resumen_cliente -----------------------------------------


async def _resumen_cliente_logic(
    valor: str,
    postgres_executor: ResilientExecutor,
    saldo_executor: ResilientExecutor,
    *,
    prefijos: frozenset[str] = PREFIJOS_BASE,
    validar_checksum: bool = True,
) -> dict[str, Any]:
    """Consulta ambas fuentes en paralelo sin fallar entera si una cae.

    Ninguna de las dos corrutinas internas deja escapar una excepción
    "esperada" (de negocio o de infraestructura ya sanitizada por
    `ConnectorError`): la atrapan y la traducen a `disponible=False` +
    `motivo`. Por eso `asyncio.TaskGroup` nunca cancela a la tarea hermana
    por un fallo esperado (una fuente caída) — solo lo haría ante un bug
    real no contemplado (una excepción que sí escapa), que es exactamente
    el caso en el que SÍ queremos que la tool falle fuerte en vez de
    fingir un resultado parcial.
    """
    identidad = _resolve_identidad(valor, prefijos=prefijos, validar_checksum=validar_checksum)

    cliente: dict[str, Any] = {"disponible": False, "datos": None, "motivo": None}
    saldo: dict[str, Any] = {"disponible": False, "datos": None, "motivo": None}

    async def _fetch_cliente() -> None:
        try:
            row = await postgres_executor.run(lambda conn: _query_cliente(conn, identidad.cedula))
        except ConnectorError:
            cliente["motivo"] = "la base de datos de clientes no está disponible en este momento"
            return
        if row is None:
            cliente["motivo"] = "no existe un cliente registrado con ese identificador"
            return
        cliente["disponible"] = True
        cliente["datos"] = row

    async def _fetch_saldo() -> None:
        try:
            data = await saldo_executor.run(lambda client: _query_saldo(client, identidad.cedula))
        except ConnectorError:
            saldo["motivo"] = "el servicio de saldos no está disponible en este momento"
            return
        if data is None:
            saldo["motivo"] = "no hay saldo registrado para ese identificador"
            return
        saldo["disponible"] = True
        saldo["datos"] = data

    async with asyncio.TaskGroup() as tg:
        tg.create_task(_fetch_cliente())
        tg.create_task(_fetch_saldo())

    return {
        "identificador": identidad.cedula,
        "cliente": cliente,
        "saldo": saldo,
        "resumen_completo": cliente["disponible"] and saldo["disponible"],
    }


# --- Registro sobre el server FastMCP -----------------------------------


def register_tools(
    mcp: FastMCP,
    registry: ConnectorRegistry,
    audit_hmac_secret: bytes,
    *,
    identifiers_prefijos: frozenset[str] = PREFIJOS_BASE,
    identifiers_validar_checksum: bool = True,
) -> None:
    """Registra las tres tools de negocio, si sus conectores existen.

    Si Postgres o la API de saldos no están habilitados en esta réplica
    (`settings.postgres.enabled` / `settings.saldo_api.enabled`), no
    registra tools en vez de fallar: un server sin ambas fuentes no puede
    ofrecer estas tools con sentido.

    `audit_hmac_secret`: clave del HMAC usado por `audit.audited_tool` para
    enmascarar el identificador en el log (ver `Settings.audit_hmac_secret`).

    `identifiers_prefijos` / `identifiers_validar_checksum`: vienen de
    `Settings.identifiers` (ver `identifiers.py` y `config.py`).
    """
    if "postgres" not in registry or "saldo_api" not in registry:
        logger.warning(
            "tools_not_registered_missing_connectors",
            extra={
                "postgres_registrado": "postgres" in registry,
                "saldo_api_registrado": "saldo_api" in registry,
            },
        )
        return

    postgres_executor = registry.get("postgres").executor
    saldo_executor = registry.get("saldo_api").executor

    @mcp.tool
    @audited_tool("consultar_cliente", identifier_param="identificador", secret=audit_hmac_secret)
    async def consultar_cliente(identificador: Identificador) -> dict[str, Any]:
        """Consulta los datos de identidad de un cliente por cédula o RIF (fuente: PostgreSQL).

        Devuelve nombre, email y estado del cliente. Úsala cuando necesites SOLO esos datos, sin
        el saldo. Si también necesitas el saldo, usa `resumen_cliente` en su lugar: es una sola
        llamada en vez de dos, y consulta ambas fuentes en paralelo.
        """
        return await _consultar_cliente_logic(
            identificador,
            postgres_executor,
            prefijos=identifiers_prefijos,
            validar_checksum=identifiers_validar_checksum,
        )

    @mcp.tool
    @audited_tool("consultar_saldo", identifier_param="identificador", secret=audit_hmac_secret)
    async def consultar_saldo(identificador: Identificador) -> dict[str, Any]:
        """Consulta el saldo actual de un cliente por cédula o RIF (fuente: API REST de saldos).

        Devuelve el saldo y su moneda. Úsala cuando necesites SOLO el saldo, sin los datos de
        identidad del cliente. Si también necesitas esos datos, usa `resumen_cliente` en su lugar.
        """
        return await _consultar_saldo_logic(
            identificador,
            saldo_executor,
            prefijos=identifiers_prefijos,
            validar_checksum=identifiers_validar_checksum,
        )

    @mcp.tool
    @audited_tool(
        "resumen_cliente",
        identifier_param="identificador",
        secret=audit_hmac_secret,
        outcome_of=lambda result: "success" if result["resumen_completo"] else "partial",
    )
    async def resumen_cliente(identificador: Identificador) -> dict[str, Any]:
        """Obtiene, en una sola llamada, los datos de identidad Y el saldo de un cliente por cédula o RIF.

        Consulta en paralelo la base de clientes (PostgreSQL) y el servicio de saldos (API REST).
        Es la tool preferida para una visión completa del cliente: úsala en vez de llamar
        `consultar_cliente` y `consultar_saldo` por separado.

        El resultado puede venir PARCIAL: si una de las dos fuentes no está disponible ahora
        mismo, esta tool NO falla — devuelve lo que sí pudo obtener y marca explícitamente, en
        `cliente.disponible` / `saldo.disponible` (con su `motivo`), qué parte falta y por qué.
        Un dato ausente NUNCA es cero ni un valor válido: revisa siempre los campos `disponible`
        antes de usar `cliente.datos` o `saldo.datos`; si alguno es `false`, dile al usuario cuál
        parte no está disponible en vez de inventarla, asumirla en cero o callarla.
        """
        return await _resumen_cliente_logic(
            identificador,
            postgres_executor,
            saldo_executor,
            prefijos=identifiers_prefijos,
            validar_checksum=identifiers_validar_checksum,
        )


# --- Resource: catálogo de estados de cliente ---------------------------

ESTADOS_CLIENTE: dict[str, str] = {
    "activo": "El cliente está al día; puede operar sin restricciones.",
    "moroso": "El cliente tiene obligaciones vencidas; algunas operaciones pueden estar restringidas.",
    "inactivo": "El cliente no tiene actividad reciente; puede requerir reactivación antes de operar.",
}


def register_resources(mcp: FastMCP) -> None:
    """Registra el catálogo de estados como Resource de solo lectura.

    Diferencia con una Tool: esto es contexto que la aplicación cliente
    puede cargar sin que el modelo decida invocar una acción — p. ej. para
    mostrarlo en una UI o para que el modelo lo tenga disponible sin gastar
    un turno de tool-call. Una Tool es una acción que el modelo elige
    ejecutar; un Resource es un dato que ya está ahí.
    """

    @mcp.resource(
        "data://clientes/estados",
        name="Catálogo de estados de cliente",
        description=(
            "Diccionario de los códigos de `estado` que puede traer `consultar_cliente` / "
            "`resumen_cliente`, con su significado de negocio."
        ),
        mime_type="application/json",
    )
    def estados_cliente() -> str:
        return json.dumps(ESTADOS_CLIENTE, ensure_ascii=False)


# --- Prompt: flujo de atención al cliente -------------------------------


def register_prompts(mcp: FastMCP) -> None:
    @mcp.prompt
    def atencion_cliente(identificador: Identificador) -> str:
        """Flujo de atención al cliente por cédula/RIF: qué tool usar y cómo manejar datos parciales.

        Args:
            identificador: Cédula o RIF del cliente que se va a atender, en cualquier formato común.
        """
        return (
            f"Vas a atender una consulta sobre el cliente con identificador {identificador}. "
            "Sigue este flujo:\n"
            "1. Usa la tool `resumen_cliente` con ese identificador: obtiene de una sola vez los "
            "datos del cliente y su saldo.\n"
            "2. Revisa `cliente.disponible` y `saldo.disponible` en el resultado. Si alguno es "
            "`false`, informa al usuario EXACTAMENTE cuál parte no está disponible y por qué "
            "(usa el campo `motivo`); nunca asumas que el dato faltante es cero ni lo inventes.\n"
            "3. Si necesitas profundizar en un solo aspecto, puedes usar `consultar_cliente` o "
            "`consultar_saldo` por separado en vez de `resumen_cliente`.\n"
            "4. Si necesitas explicar qué significa el campo `estado` del cliente, consulta el "
            "resource `data://clientes/estados`."
        )
