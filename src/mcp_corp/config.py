"""Configuración del servicio, cargada exclusivamente desde variables de entorno.

Sigue el principio 12-factor de "config en el entorno": no hay valores de
configuración hardcodeados en el código ni archivos de config locales; todo
se puede sobreescribir vía variables de entorno (o un `.env` en desarrollo).
"""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Configuración del servidor MCP, con defaults sensatos para local/dev."""

    model_config = SettingsConfigDict(
        env_prefix="MCP_CORP_",
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

    # Conectores (placeholders para la Fase 2)
    default_connector_pool_size: int = Field(
        default=10,
        description="Tamaño de pool de conexiones por defecto para futuros conectores de datos.",
    )
    default_connector_timeout_seconds: float = Field(
        default=5.0,
        description="Timeout por defecto (segundos) para llamadas a fuentes de datos externas.",
    )

    # Apagado
    graceful_shutdown_timeout_seconds: float = Field(
        default=10.0,
        description="Segundos de gracia para drenar conexiones en curso al recibir SIGTERM.",
    )


def get_settings() -> Settings:
    """Construye la configuración desde el entorno.

    No se cachea a nivel de módulo a propósito: mantiene la función libre de
    estado oculto y trivial de testear con distintos entornos.
    """
    return Settings()
