"""Unitarios de `config.Settings`: fail-closed del HMAC (Fase 4) y secretos
montados como archivo para Docker Swarm/Kubernetes (Fase 5)."""

from __future__ import annotations

import os

import pytest
from pydantic import ValidationError

from mcp_corp.config import Settings, _load_file_secrets_into_environ, get_settings


def test_production_without_hmac_secret_fails_closed() -> None:
    with pytest.raises(ValidationError, match="AUDIT_HMAC_SECRET"):
        Settings(environment="production", audit_hmac_secret="")


def test_production_with_hmac_secret_starts_fine() -> None:
    settings = Settings(environment="production", audit_hmac_secret="una-clave-larga-y-aleatoria")
    assert settings.audit_hmac_secret == "una-clave-larga-y-aleatoria"


@pytest.mark.parametrize("environment", ["local", "staging"])
def test_non_production_without_hmac_secret_is_allowed(environment: str) -> None:
    """Fuera de producción, una clave vacía es aceptable (solo warning en server.py)."""
    settings = Settings(environment=environment, audit_hmac_secret="")
    assert settings.audit_hmac_secret == ""


# --- Secretos montados como archivo (Fase 5): patrón MCP_CORP_X_FILE ------


def test_file_suffix_var_is_loaded_into_plain_var(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    secret_file = tmp_path / "audit_hmac_secret"
    secret_file.write_text("clave-secreta-de-archivo\n", encoding="utf-8")
    monkeypatch.setenv("MCP_CORP_AUDIT_HMAC_SECRET_FILE", str(secret_file))
    monkeypatch.delenv("MCP_CORP_AUDIT_HMAC_SECRET", raising=False)

    _load_file_secrets_into_environ()
    try:
        # El contenido se recorta (sin el salto de línea final del archivo).
        assert os.environ["MCP_CORP_AUDIT_HMAC_SECRET"] == "clave-secreta-de-archivo"
    finally:
        monkeypatch.delenv("MCP_CORP_AUDIT_HMAC_SECRET", raising=False)


def test_nested_field_file_suffix_is_loaded(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """El mecanismo opera sobre el nombre de la variable ya resuelta, así que
    cubre campos anidados (postgres.dsn, saldo_api.*) sin tratamiento especial."""
    dsn_file = tmp_path / "postgres_dsn"
    dsn_file.write_text("postgresql://usuario:clave@host:5432/db", encoding="utf-8")
    monkeypatch.setenv("MCP_CORP_POSTGRES__DSN_FILE", str(dsn_file))
    monkeypatch.delenv("MCP_CORP_POSTGRES__DSN", raising=False)

    _load_file_secrets_into_environ()
    try:
        assert os.environ["MCP_CORP_POSTGRES__DSN"] == "postgresql://usuario:clave@host:5432/db"
    finally:
        monkeypatch.delenv("MCP_CORP_POSTGRES__DSN", raising=False)


def test_explicit_env_var_takes_precedence_over_file(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    secret_file = tmp_path / "audit_hmac_secret"
    secret_file.write_text("valor-del-archivo", encoding="utf-8")
    monkeypatch.setenv("MCP_CORP_AUDIT_HMAC_SECRET_FILE", str(secret_file))
    monkeypatch.setenv("MCP_CORP_AUDIT_HMAC_SECRET", "valor-explicito-ya-presente")

    _load_file_secrets_into_environ()

    # La variable explícita gana; el archivo se ignora en silencio.
    assert os.environ["MCP_CORP_AUDIT_HMAC_SECRET"] == "valor-explicito-ya-presente"


def test_unrelated_file_suffix_var_is_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    """Solo se procesan variables con el prefijo MCP_CORP_; cualquier otra
    variable *_FILE del entorno (de otra herramienta) se deja intacta."""
    monkeypatch.setenv("SOME_OTHER_TOOL_TOKEN_FILE", "/no/existe/nada")

    _load_file_secrets_into_environ()  # no debe lanzar ni tocar nada ajeno

    assert "SOME_OTHER_TOOL_TOKEN" not in os.environ


def test_missing_file_raises_clear_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCP_CORP_AUDIT_HMAC_SECRET_FILE", "/ruta/que/no/existe")
    monkeypatch.delenv("MCP_CORP_AUDIT_HMAC_SECRET", raising=False)

    with pytest.raises(RuntimeError, match="MCP_CORP_AUDIT_HMAC_SECRET_FILE"):
        _load_file_secrets_into_environ()


def test_get_settings_resolves_file_secret_end_to_end(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Prueba de integración del mecanismo completo: get_settings() ya
    resuelto, listo para pasar por el fail-closed de producción."""
    secret_file = tmp_path / "audit_hmac_secret"
    secret_file.write_text("clave-de-produccion-real", encoding="utf-8")
    monkeypatch.setenv("MCP_CORP_ENVIRONMENT", "production")
    monkeypatch.setenv("MCP_CORP_AUDIT_HMAC_SECRET_FILE", str(secret_file))
    monkeypatch.delenv("MCP_CORP_AUDIT_HMAC_SECRET", raising=False)

    try:
        settings = get_settings()
        assert settings.audit_hmac_secret == "clave-de-produccion-real"
    finally:
        monkeypatch.delenv("MCP_CORP_AUDIT_HMAC_SECRET", raising=False)
