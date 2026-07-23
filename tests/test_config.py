"""Unitarios de `config.Settings`: fail-closed del HMAC en modo producción (Fase 4, Parte B)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from mcp_corp.config import Settings


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
