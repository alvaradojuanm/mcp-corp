"""Unitarios de `audit.audited_tool`: enmascaramiento y forma del log."""

from __future__ import annotations

import logging

import pytest

from mcp_corp.audit import audited_tool


async def test_masks_identifier_and_never_logs_raw_value(caplog: pytest.LogCaptureFixture) -> None:
    @audited_tool("mi_tool", identifier_param="cedula")
    async def mi_tool(cedula: str) -> dict:
        return {"ok": True}

    with caplog.at_level(logging.INFO, logger="mcp_corp.audit"):
        await mi_tool(cedula="1000000001")

    assert "1000000001" not in caplog.text

    started = next(r for r in caplog.records if r.message == "tool_invocation_started")
    completed = next(r for r in caplog.records if r.message == "tool_invocation_completed")

    assert started.tool == "mi_tool"
    assert started.identifier.startswith("sha256:")
    assert completed.result == "success"
    assert completed.duration_ms >= 0


async def test_same_identifier_value_masks_to_the_same_hash(caplog: pytest.LogCaptureFixture) -> None:
    @audited_tool("mi_tool", identifier_param="cedula")
    async def mi_tool(cedula: str) -> dict:
        return {}

    with caplog.at_level(logging.INFO, logger="mcp_corp.audit"):
        await mi_tool(cedula="1000000001")
        await mi_tool(cedula="1000000001")
        await mi_tool(cedula="2000000002")

    started = [r for r in caplog.records if r.message == "tool_invocation_started"]
    identifiers = [r.identifier for r in started]

    assert identifiers[0] == identifiers[1]  # misma cédula -> mismo hash, correlacionable
    assert identifiers[0] != identifiers[2]  # cédula distinta -> hash distinto


async def test_reports_failure_with_reason_and_reraises(caplog: pytest.LogCaptureFixture) -> None:
    @audited_tool("mi_tool_falla")
    async def mi_tool_falla() -> None:
        raise ValueError("algo salió mal")

    with caplog.at_level(logging.INFO, logger="mcp_corp.audit"):
        with pytest.raises(ValueError, match="algo salió mal"):
            await mi_tool_falla()

    completed = next(r for r in caplog.records if r.message == "tool_invocation_completed")
    assert completed.result == "failure"
    assert completed.reason == "algo salió mal"


async def test_outcome_of_marks_partial_result() -> None:
    @audited_tool("compuesta", outcome_of=lambda r: "success" if r["ok"] else "partial")
    async def compuesta(ok: bool) -> dict:
        return {"ok": ok}

    assert await compuesta(ok=False) == {"ok": False}
    assert await compuesta(ok=True) == {"ok": True}
