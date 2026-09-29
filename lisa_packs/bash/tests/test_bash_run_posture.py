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
from kernel.capability_binding import ExecutionFloorView
from kernel.models import ToolExecutionContext

from lisa_packs.bash import executor as _executor
from lisa_packs.bash.tools.bash_run import bash_run


def _ctx(
    tmp_path: Path,
    command: str = "echo hi",
    pack_config: dict | None = None,
    floor_execution: str | None = "sandbox",
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
        execution_floor_for=(
            (lambda _tool_id: ExecutionFloorView(execution=floor_execution))
            if floor_execution is not None
            else None
        ),
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
    result = await bash_run.execute(
        _ctx(tmp_path, pack_config=_floor("bare"), floor_execution="bare")
    )
    assert result.error == ""
    assert result.output_json["sandboxed"] is False
    assert result.output_json["mode"] == "bare"
    assert "permits bare execution" in result.output_json["note"]


@pytest.mark.asyncio
async def test_floor_bare_with_healthy_sandbox_runs_bare(
    monkeypatch, tmp_path: Path
) -> None:
    """Floor execution=bare + WORKING bwrap + no tightening → deliberate bare.

    A bare floor is the harness's chosen posture, not a fallback for a broken
    sandbox: while bwrap is healthy and the model requested no containment,
    the shell runs on the host (2026-09-16 incident: bare floor authored but
    every turn still composed through bwrap).
    """
    monkeypatch.setattr(bash_run, "bash_available", lambda: True)
    monkeypatch.setattr(bash_run, "_sandbox_available", lambda: True)
    monkeypatch.setattr(bash_run, "_sandbox_degraded", list)

    async def _fake_run_in_session(ctx, session, command, **kwargs):
        assert session.sandboxed is False
        return {
            "stdout": "hi",
            "stderr": "",
            "return_code": 0,
            "timed_out": False,
            "truncated": False,
            "sandboxed": False,
        }

    monkeypatch.setattr(bash_run, "run_in_session", _fake_run_in_session)
    result = await bash_run.execute(
        _ctx(tmp_path, pack_config=_floor("bare"), floor_execution="bare")
    )
    assert result.error == ""
    assert result.output_json["sandboxed"] is False
    assert result.output_json["mode"] == "bare"
    assert "permits bare execution" in result.output_json["note"]


@pytest.mark.asyncio
async def test_floor_bare_persist_opts_into_sandbox(
    monkeypatch, tmp_path: Path
) -> None:
    """Bare floor + persist request → the call composes through the sandbox."""
    monkeypatch.setattr(bash_run, "bash_available", lambda: True)
    monkeypatch.setattr(bash_run, "_sandbox_available", lambda: True)
    monkeypatch.setattr(bash_run, "_sandbox_degraded", list)

    async def _fake_run_in_session(ctx, session, command, **kwargs):
        assert session.sandboxed is True, "persist must opt into the sandbox"
        return {
            "stdout": "hi",
            "stderr": "",
            "return_code": 0,
            "timed_out": False,
            "truncated": False,
            "sandboxed": True,
        }

    monkeypatch.setattr(bash_run, "run_in_session", _fake_run_in_session)
    ctx = _ctx(tmp_path, pack_config=_floor("bare"), floor_execution="bare")
    ctx.parameters = {"command": "echo hi", "persist": True}
    result = await bash_run.execute(ctx)
    assert result.error == ""
    assert result.output_json["sandboxed"] is True


@pytest.mark.asyncio
async def test_floor_bare_override_opts_into_sandbox(
    monkeypatch, tmp_path: Path
) -> None:
    """Bare floor + overrides entry → the call composes through the sandbox."""
    monkeypatch.setattr(bash_run, "bash_available", lambda: True)
    monkeypatch.setattr(bash_run, "_sandbox_available", lambda: True)
    monkeypatch.setattr(bash_run, "_sandbox_degraded", list)

    async def _fake_run_in_session(ctx, session, command, **kwargs):
        assert session.sandboxed is True, "overrides must opt into the sandbox"
        return {
            "stdout": "hi",
            "stderr": "",
            "return_code": 0,
            "timed_out": False,
            "truncated": False,
            "sandboxed": True,
        }

    monkeypatch.setattr(bash_run, "run_in_session", _fake_run_in_session)
    ctx = _ctx(tmp_path, pack_config=_floor("bare"), floor_execution="bare")
    ctx.parameters = {
        "command": "echo hi",
        "overrides": {"network": "deny"},
    }
    result = await bash_run.execute(ctx)
    assert result.error == ""
    assert result.output_json["sandboxed"] is True


@pytest.mark.asyncio
async def test_floor_bare_read_via_global_bwrap_config(
    monkeypatch, tmp_path: Path
) -> None:
    """Bwrap's projected bare floor permits host execution without bash policy."""

    monkeypatch.setattr(bash_run, "bash_available", lambda: True)
    monkeypatch.setattr(bash_run, "_sandbox_available", lambda: True)
    monkeypatch.setattr(bash_run, "_sandbox_degraded", list)

    async def _fake_run_in_session(ctx, session, command, **kwargs):
        assert session.sandboxed is False
        return {
            "stdout": "hi",
            "stderr": "",
            "return_code": 0,
            "timed_out": False,
            "truncated": False,
            "sandboxed": False,
        }

    monkeypatch.setattr(bash_run, "run_in_session", _fake_run_in_session)
    ctx = _ctx(tmp_path, pack_config={}, floor_execution="bare")
    result = await bash_run.execute(ctx)
    assert result.error == ""
    assert result.output_json["sandboxed"] is False
    assert result.output_json["mode"] == "bare"


@pytest.mark.asyncio
async def test_floor_bare_global_sandbox_floor_still_mandates_sandbox(
    monkeypatch, tmp_path: Path
) -> None:
    """A bwrap-owned sandbox floor still fails loudly when bwrap is absent."""

    monkeypatch.setattr(bash_run, "bash_available", lambda: True)
    monkeypatch.setattr(bash_run, "_sandbox_available", lambda: False)
    ctx = _ctx(tmp_path, pack_config={})
    result = await bash_run.execute(ctx)
    assert result.error
    assert "requires contained execution" in result.error


@pytest.mark.asyncio
async def test_bash_floor_cannot_loosen_bwrap_owner_floor(monkeypatch, tmp_path: Path) -> None:
    """A bash-keyed bare floor cannot override the bwrap owner's sandbox floor."""

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
    ctx = _ctx(tmp_path, pack_config=_floor("bare"), floor_execution="sandbox")
    result = await bash_run.execute(ctx)
    assert result.error
    assert "requires contained execution" in result.error


@pytest.mark.asyncio
async def test_no_floor_fails_closed_when_sandbox_absent(
    monkeypatch, tmp_path: Path
) -> None:
    """Missing bwrap policy is not permission to run on the host."""
    monkeypatch.setattr(bash_run, "bash_available", lambda: True)
    monkeypatch.setattr(bash_run, "_sandbox_available", lambda: False)

    result = await bash_run.execute(_ctx(tmp_path, pack_config={}, floor_execution=None))
    assert "requires contained execution" in result.error
    assert result.output_json["sandboxed"] is False
