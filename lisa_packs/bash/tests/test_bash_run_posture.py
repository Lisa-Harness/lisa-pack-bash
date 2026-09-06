"""Harness-floor posture tests for bash_run (bare-vs-none elimination).

The harness floor (session ``resources.packs.configuration`` → ctx.pack_config)
decides whether bare execution is permitted. When the floor mandates the sandbox
and bwrap cannot run, the shell must fail loudly with remediation - never drop
silently to the host. When the floor permits bare and the sandbox is absent, the
shell runs deliberately bare and stamps that choice distinctly.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kernel.models import ToolExecutionContext

from lisa_packs.bash import executor as _executor
from lisa_packs.bash.tools.bash_run import bash_run


def _ctx(
    tmp_path: Path, command: str = "echo hi", pack_config: dict | None = None
) -> ToolExecutionContext:
    return ToolExecutionContext(
        tool_id="bash_run",
        workspace_id="ws",
        user_slug="u",
        surface_id="surf",
        session_plan_digest="d",
        parameters={"command": command},
        data_dir=str(tmp_path),
        pack_config=pack_config or {},
    )


@pytest.fixture(autouse=True)
def _fresh_sessions():
    _executor._reset_for_tests()
    yield
    _executor._reset_for_tests()


def _floor(execution: str) -> dict:
    return {"sandbox": {"floor": {"execution": execution}}}


@pytest.mark.asyncio
async def test_floor_requires_sandbox_and_pack_absent_fails_loudly(
    monkeypatch, tmp_path: Path
) -> None:
    """Floor execution=sandbox + no bwrap pack → hard error, not a host run."""
    monkeypatch.setattr(bash_run, "bash_available", lambda: True)
    monkeypatch.setattr(bash_run, "_sandbox_available", lambda: False)
    result = await bash_run.execute(_ctx(tmp_path, pack_config=_floor("sandbox")))
    assert result.error
    assert "requires contained execution" in result.error
    assert result.output_json["sandboxed"] is False
    assert result.output_json["missing"] == ["sandbox pack not installed"]
    assert "doc" in result.output_json


@pytest.mark.asyncio
async def test_floor_requires_sandbox_and_bwrap_degraded_fails_loudly(
    monkeypatch, tmp_path: Path
) -> None:
    """Floor execution=sandbox + bwrap present-but-broken → hard error."""
    monkeypatch.setattr(bash_run, "bash_available", lambda: True)
    monkeypatch.setattr(bash_run, "_sandbox_available", lambda: True)
    monkeypatch.setattr(
        bash_run,
        "_sandbox_degraded",
        lambda: ["bubblewrap (bwrap) binary not found on PATH"],
    )
    result = await bash_run.execute(_ctx(tmp_path, pack_config=_floor("sandbox")))
    assert result.error
    assert "requires contained execution" in result.error
    assert result.output_json["sandboxed"] is False
    assert "bubblewrap" in result.output_json["missing"][0]


@pytest.mark.asyncio
async def test_floor_permits_bare_runs_deliberately_bare(
    monkeypatch, tmp_path: Path
) -> None:
    """Floor execution=bare + no sandbox → runs bare, stamped as a choice."""
    monkeypatch.setattr(bash_run, "bash_available", lambda: True)
    monkeypatch.setattr(bash_run, "_sandbox_available", lambda: False)

    async def _fake_run_in_session(ctx, session, command, **kwargs):
        return {
            "stdout": "hi",
            "stderr": "",
            "return_code": 0,
            "timed_out": False,
            "truncated": False,
            "sandboxed": False,
        }

    monkeypatch.setattr(bash_run, "run_in_session", _fake_run_in_session)
    result = await bash_run.execute(_ctx(tmp_path, pack_config=_floor("bare")))
    assert result.error == ""
    assert result.output_json["sandboxed"] is False
    assert result.output_json["mode"] == "bare"
    assert "permits bare execution" in result.output_json["note"]


@pytest.mark.asyncio
async def test_no_floor_preserves_availability_behavior(
    monkeypatch, tmp_path: Path
) -> None:
    """No floor configured → availability decides, as before (no regression)."""
    monkeypatch.setattr(bash_run, "bash_available", lambda: True)
    monkeypatch.setattr(bash_run, "_sandbox_available", lambda: False)

    async def _fake_run_in_session(ctx, session, command, **kwargs):
        return {
            "stdout": "hi",
            "stderr": "",
            "return_code": 0,
            "timed_out": False,
            "truncated": False,
            "sandboxed": False,
        }

    monkeypatch.setattr(bash_run, "run_in_session", _fake_run_in_session)
    result = await bash_run.execute(_ctx(tmp_path, pack_config={}))
    assert result.error == ""
    assert result.output_json["sandboxed"] is False
    # Unset floor + absent pack is NOT deliberate bare: it's the old 'none'.
    assert "mode" not in result.output_json
    assert "not installed" in result.output_json["note"]
