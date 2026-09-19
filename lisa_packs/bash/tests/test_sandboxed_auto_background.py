"""Sandboxed auto-background on timeout (Phase 3).

Unit tests: a mocked ``_run_via_bwrap`` proves the race → adopt-as-job flow
without launching bubblewrap. Live round-trips stay in test_bash_run_live.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest

from kernel.models import ToolExecutionContext

from lisa_packs.bash import executor
from lisa_packs.bash.tools.bash_run import bash_run


def _ctx(tmp_path: Path, **params) -> ToolExecutionContext:
    return ToolExecutionContext(
        tool_id="bash_run",
        workspace_id="ws",
        parameters={"command": "true", **params},
        data_dir=str(tmp_path),
    )


@pytest.fixture(autouse=True)
def _fresh_sessions():
    executor._reset_for_tests()
    yield
    executor._reset_for_tests()


@pytest.mark.asyncio
async def test_sandboxed_staged_layer_result_carries_review_note(
    monkeypatch, tmp_path: Path
) -> None:
    """A composed overlay run is stamped STAGED + the layer_review.md pointer."""
    monkeypatch.setattr(bash_run, "bash_available", lambda: True)
    monkeypatch.setattr(bash_run, "_sandbox_available", lambda: True)

    async def _staged_bwrap(ctx, inner_argv, *, timeout_seconds, overrides, persist, layer_id):
        return {
            "return_code": 0,
            "stdout_raw": "done\n",
            "stderr": "",
            "timed_out": False,
            "truncated": False,
            "sandboxed": True,
            "backend": "overlay",
            "persistence": "overlay",
            "layer_id": "L-staged",
        }

    monkeypatch.setattr(executor, "_run_via_bwrap", _staged_bwrap)
    monkeypatch.setattr(executor, "is_auto_background_guarded", lambda cmd: True)
    ctx = _ctx(tmp_path, command="echo staged", persist=True)
    session = executor.resolve_or_create_session_for_tests(sandboxed=True) if hasattr(
        executor, "resolve_or_create_session_for_tests"
    ) else None
    # Use the session the tool itself would create: compose via run_in_session.
    from lisa_packs.bash.session import resolve_or_create_session

    session = resolve_or_create_session(ctx, sandboxed=True)
    result = await executor.run_in_session(
        ctx, session, "echo staged", timeout_seconds=10, persist=True,
    )
    assert result.get("error") is None
    assert result["backend"] == "overlay"
    assert result["layer_id"] == "L-staged"
    assert "STAGED" in result["note"]
    assert "layer_review.md" in result["note"]
    # Non-degraded overlay runs only: the ephemeral default carries no note.
    assert "degraded" not in result


@pytest.mark.asyncio
async def test_sandboxed_slow_command_auto_backgrounds(
    monkeypatch, tmp_path: Path
) -> None:
    """A slow sandboxed command becomes a job instead of dying with the turn."""
    monkeypatch.setattr(bash_run, "bash_available", lambda: True)
    monkeypatch.setattr(bash_run, "_sandbox_available", lambda: True)

    release = asyncio.Event()

    async def _slow_bwrap(ctx, inner_argv, *, timeout_seconds, overrides, persist, layer_id):
        await asyncio.wait_for(release.wait(), timeout=30)
        return {
            "return_code": 0,
            "stdout_raw": "sandbox-later\n",
            "stderr": "",
            "timed_out": False,
            "truncated": False,
            "sandboxed": True,
            "backend": "tmpfs",
            "persistence": "ephemeral",
        }

    monkeypatch.setattr(executor, "_run_via_bwrap", _slow_bwrap)
    ctx = _ctx(tmp_path, command="echo sandbox-later", timeout_seconds=1)
    result = await bash_run.execute(ctx)
    assert result.error == ""
    out = result.output_json
    assert out["backgrounded"] is True
    assert out["auto_backgrounded_on_timeout"] is True
    assert out["sandboxed"] is True
    assert out["job_id"]

    # The adopted job completes when the mocked bwrap call finishes.
    rc_path = Path(out["rc_path"])
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and not rc_path.is_file():
        await asyncio.sleep(0.05)
    release.set()
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if rc_path.is_file() and rc_path.read_text(encoding="utf-8").strip() == "0":
            break
        await asyncio.sleep(0.05)
    assert rc_path.read_text(encoding="utf-8").strip() == "0"
    status = json.loads(Path(out["status_path"]).read_text(encoding="utf-8"))
    assert status["state"] in ("done", "killed")


@pytest.mark.asyncio
async def test_sandboxed_git_command_is_killed_not_backgrounded(
    monkeypatch, tmp_path: Path
) -> None:
    """Guarded commands (git) get the bounded T+30 timeout, never the grace."""
    monkeypatch.setattr(bash_run, "bash_available", lambda: True)
    monkeypatch.setattr(bash_run, "_sandbox_available", lambda: True)

    seen: dict = {}

    async def _bounded_bwrap(ctx, inner_argv, *, timeout_seconds, overrides, persist, layer_id):
        seen["timeout_seconds"] = timeout_seconds
        # Child reports a bwrap-level timeout kill, as the real tool does at T+30.
        return {"return_code": -1, "stdout_raw": "", "stderr": "", "timed_out": True}

    monkeypatch.setattr(executor, "_run_via_bwrap", _bounded_bwrap)
    ctx = _ctx(tmp_path, command="git status", timeout_seconds=1)
    result = await bash_run.execute(ctx)
    assert result.error == ""
    assert result.output_json.get("backgrounded") is not True
    assert result.output_json["timed_out"] is True
    # Bounded path: model timeout + 30s, NOT the auto-background grace window.
    assert seen["timeout_seconds"] == 31


@pytest.mark.asyncio
async def test_sandboxed_fast_command_not_backgrounded(
    monkeypatch, tmp_path: Path
) -> None:
    """A fast sandboxed command completes within the wait; no job is created."""
    monkeypatch.setattr(bash_run, "bash_available", lambda: True)
    monkeypatch.setattr(bash_run, "_sandbox_available", lambda: True)

    async def _fast_bwrap(ctx, inner_argv, *, timeout_seconds, overrides, persist, layer_id):
        return {"return_code": 0, "stdout_raw": "quick\n", "stderr": "", "timed_out": False}

    monkeypatch.setattr(executor, "_run_via_bwrap", _fast_bwrap)
    ctx = _ctx(tmp_path, command="echo quick", timeout_seconds=5)
    result = await bash_run.execute(ctx)
    assert result.error == ""
    assert result.output_text == "quick"
    assert result.output_json.get("backgrounded") is not True


@pytest.mark.asyncio
async def test_sandboxed_auto_bg_adopts_error_payload(monkeypatch, tmp_path: Path) -> None:
    """A sandbox error surfacing after the model timeout still lands as a job."""
    monkeypatch.setattr(bash_run, "bash_available", lambda: True)
    monkeypatch.setattr(bash_run, "_sandbox_available", lambda: True)

    async def _err_bwrap(ctx, inner_argv, *, timeout_seconds, overrides, persist, layer_id):
        await asyncio.sleep(2)
        return {"error": "boom", "stdout_raw": "", "stderr": "", "timed_out": False}

    monkeypatch.setattr(executor, "_run_via_bwrap", _err_bwrap)
    ctx = _ctx(tmp_path, command="notsleep 9", timeout_seconds=1)
    result = await bash_run.execute(ctx)
    assert result.error == ""
    assert result.output_json.get("backgrounded") is True
    job_id = result.output_json["job_id"]
    log_path = Path(result.output_json["log_path"])
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if log_path.is_file() and "[bash-job error]" in log_path.read_text(encoding="utf-8"):
            break
        await asyncio.sleep(0.05)
    assert "[bash-job error] boom" in log_path.read_text(encoding="utf-8")
    assert job_id


def test_auto_bg_bwrap_timeout_helper() -> None:
    """Grace window is model timeout + 600s, capped at 3600s."""
    assert executor._auto_bg_bwrap_timeout(30) == 630
    assert executor._auto_bg_bwrap_timeout(1) == 601
    assert executor._auto_bg_bwrap_timeout(4000) == 3600