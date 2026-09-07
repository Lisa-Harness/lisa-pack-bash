"""Real round-trip test for bash_run in unsandboxed mode.

Marked ``live``: launches a real primed bash process, parses the sentinel, and
writes the audit log. Skipped when bash is absent.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import time
from pathlib import Path

import pytest

from kernel.models import ToolExecutionContext

from lisa_packs.bash import executor as _executor
from lisa_packs.bash.tools.bash_run import bash_run

pytestmark = pytest.mark.live


@pytest.fixture(autouse=True)
def _fresh_sessions():
    _executor._reset_for_tests()
    yield
    _executor._reset_for_tests()


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not installed")
@pytest.mark.asyncio
async def test_bash_run_roundtrip_unsandboxed(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(bash_run, "_sandbox_available", lambda: False)
    ctx = ToolExecutionContext(
        tool_id="bash_run",
        workspace_id="ws",
        parameters={"command": "echo roundtrip-$((40+2))"},
        data_dir=str(tmp_path),
    )
    result = await bash_run.execute(ctx)
    assert result.error == ""
    assert "roundtrip-42" in result.output_text
    assert result.output_json["return_code"] == 0
    assert result.output_json["sandboxed"] is False
    log_path = Path(result.output_json["log_path"])
    assert log_path.is_file()
    assert "roundtrip" in log_path.read_text(encoding="utf-8")


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not installed")
@pytest.mark.asyncio
async def test_bash_run_cwd_carries_over(monkeypatch, tmp_path: Path) -> None:
    """cd in one call must be visible in the next call (state carryover)."""
    monkeypatch.setattr(bash_run, "_sandbox_available", lambda: False)
    subdir = tmp_path / "workdir"
    subdir.mkdir()
    first = ToolExecutionContext(
        tool_id="bash_run",
        workspace_id="ws",
        parameters={"command": f"cd '{subdir}'"},
        data_dir=str(tmp_path),
    )
    r1 = await bash_run.execute(first)
    assert r1.error == ""
    second = ToolExecutionContext(
        tool_id="bash_run",
        workspace_id="ws",
        parameters={"command": "pwd"},
        data_dir=str(tmp_path),
    )
    r2 = await bash_run.execute(second)
    assert r2.error == ""
    assert str(subdir) in r2.output_text


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not installed")
@pytest.mark.asyncio
async def test_bash_run_export_carries_over(monkeypatch, tmp_path: Path) -> None:
    """An exported var from one call reaches the next."""
    monkeypatch.setattr(bash_run, "_sandbox_available", lambda: False)
    first = ToolExecutionContext(
        tool_id="bash_run",
        workspace_id="ws",
        parameters={"command": "export LISA_CARRY_TEST=from_first_call"},
        data_dir=str(tmp_path),
    )
    r1 = await bash_run.execute(first)
    assert r1.error == ""
    second = ToolExecutionContext(
        tool_id="bash_run",
        workspace_id="ws",
        parameters={"command": "echo \"val=$LISA_CARRY_TEST\""},
        data_dir=str(tmp_path),
    )
    r2 = await bash_run.execute(second)
    assert r2.error == ""
    assert "val=from_first_call" in r2.output_text


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not installed")
@pytest.mark.asyncio
async def test_bash_run_pipe_statuses_recorded(monkeypatch, tmp_path: Path) -> None:
    """PIPESTATUS surfaces for a multi-stage pipeline."""
    monkeypatch.setattr(bash_run, "_sandbox_available", lambda: False)
    ctx = ToolExecutionContext(
        tool_id="bash_run",
        workspace_id="ws",
        parameters={"command": "false | cat | true"},
        data_dir=str(tmp_path),
    )
    result = await bash_run.execute(ctx)
    assert result.error == ""
    assert result.output_json["pipe_statuses"] == [1, 0, 0]
    assert "shaping_note" in result.output_json


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not installed")
@pytest.mark.asyncio
async def test_bash_run_empty_output_explicit(monkeypatch, tmp_path: Path) -> None:
    """A silent success carries the explicit no-output note."""
    monkeypatch.setattr(bash_run, "_sandbox_available", lambda: False)
    ctx = ToolExecutionContext(
        tool_id="bash_run",
        workspace_id="ws",
        parameters={"command": "true"},
        data_dir=str(tmp_path),
    )
    result = await bash_run.execute(ctx)
    assert result.error == ""
    assert result.output_json["return_code"] == 0
    assert "no output" in result.output_json.get("shaping_note", "")


async def _wait_for_rc(path: Path, *, timeout: float = 8.0) -> int:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.is_file():
            text = path.read_text(encoding="utf-8").strip()
            if text:
                return int(text)
        await asyncio.sleep(0.05)
    raise AssertionError(f"rc file not written at {path}")


async def _wait_for_status(path: Path, *, timeout: float = 8.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.is_file():
            data = json.loads(path.read_text(encoding="utf-8"))
            if data.get("state") in ("done", "killed"):
                return data
        await asyncio.sleep(0.05)
    raise AssertionError(f"status file not completed at {path}")


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not installed")
@pytest.mark.asyncio
async def test_bash_run_background_roundtrip(monkeypatch, tmp_path: Path) -> None:
    """Explicit background returns a job_id; the rc file flips when done."""
    monkeypatch.setattr(bash_run, "_sandbox_available", lambda: False)
    ctx = ToolExecutionContext(
        tool_id="bash_run",
        workspace_id="ws",
        parameters={
            "command": "printf 'bg-done\\n'",
            "run_in_background": True,
        },
        data_dir=str(tmp_path),
    )
    result = await bash_run.execute(ctx)
    assert result.error == ""
    assert result.output_json["backgrounded"] is True
    job_id = result.output_json["job_id"]
    rc_path = Path(result.output_json["rc_path"])
    status_path = Path(result.output_json["status_path"])
    log_path = Path(result.output_json["log_path"])
    rc = await _wait_for_rc(rc_path)
    assert rc == 0
    assert "bg-done" in log_path.read_text(encoding="utf-8")
    status = await _wait_for_status(status_path)
    assert status["job_id"] == job_id
    assert status["state"] == "done"


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not installed")
@pytest.mark.asyncio
async def test_bash_run_auto_background_on_timeout(monkeypatch, tmp_path: Path) -> None:
    """A slow unguarded command is moved to background instead of killed."""
    monkeypatch.setattr(bash_run, "_sandbox_available", lambda: False)
    ctx = ToolExecutionContext(
        tool_id="bash_run",
        workspace_id="ws",
        parameters={
            "command": "bash -c 'sleep 2; printf later'",
            "timeout_seconds": 1,
        },
        data_dir=str(tmp_path),
    )
    result = await bash_run.execute(ctx)
    assert result.error == ""
    assert result.output_json.get("backgrounded") is True
    assert result.output_json.get("auto_backgrounded_on_timeout") is True
    rc = await _wait_for_rc(Path(result.output_json["rc_path"]), timeout=10.0)
    assert rc == 0
    log = Path(result.output_json["log_path"]).read_text(encoding="utf-8")
    assert "later" in log


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not installed")
@pytest.mark.asyncio
async def test_bash_run_sleep_timeout_is_killed(monkeypatch, tmp_path: Path) -> None:
    """Commands starting with sleep are never auto-backgrounded."""
    monkeypatch.setattr(bash_run, "_sandbox_available", lambda: False)
    ctx = ToolExecutionContext(
        tool_id="bash_run",
        workspace_id="ws",
        parameters={"command": "sleep 5", "timeout_seconds": 1},
        data_dir=str(tmp_path),
    )
    result = await bash_run.execute(ctx)
    assert result.error == ""
    assert result.output_json.get("backgrounded") is not True
    assert result.output_json["timed_out"] is True


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not installed")
@pytest.mark.asyncio
async def test_bash_run_batch_parallel(monkeypatch, tmp_path: Path) -> None:
    """Independent commands run as a batch and keep their own stdout/rc."""
    monkeypatch.setattr(bash_run, "_sandbox_available", lambda: False)
    ctx = ToolExecutionContext(
        tool_id="bash_run",
        workspace_id="ws",
        parameters={"commands": ["echo alpha-batch", "echo beta-batch"]},
        data_dir=str(tmp_path),
    )
    result = await bash_run.execute(ctx)
    assert result.error == ""
    assert result.output_json["batch"] is True
    assert result.output_json["when"] == "parallel"
    bodies = {item["stdout"] for item in result.output_json["results"]}
    assert any("alpha-batch" in body for body in bodies)
    assert any("beta-batch" in body for body in bodies)
    assert all(item["return_code"] == 0 for item in result.output_json["results"])


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not installed")
@pytest.mark.asyncio
async def test_bash_run_batch_does_not_carry_env(monkeypatch, tmp_path: Path) -> None:
    """Env deltas from a batch are discarded; a later call does not see them."""
    monkeypatch.setattr(bash_run, "_sandbox_available", lambda: False)
    batch = ToolExecutionContext(
        tool_id="bash_run",
        workspace_id="ws",
        parameters={"commands": ["export LISA_BATCH_ENV=nope"]},
        data_dir=str(tmp_path),
    )
    r1 = await bash_run.execute(batch)
    assert r1.error == ""
    follow = ToolExecutionContext(
        tool_id="bash_run",
        workspace_id="ws",
        parameters={"command": "echo val=${LISA_BATCH_ENV-unset}"},
        data_dir=str(tmp_path),
    )
    r2 = await bash_run.execute(follow)
    assert r2.error == ""
    assert "val=unset" in r2.output_text


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not installed")
@pytest.mark.asyncio
async def test_bash_run_batch_adopts_agreed_cwd(monkeypatch, tmp_path: Path) -> None:
    """When every batch child ends in the same cwd, the session adopts it."""
    monkeypatch.setattr(bash_run, "_sandbox_available", lambda: False)
    subdir = tmp_path / "batchcwd"
    subdir.mkdir()
    batch = ToolExecutionContext(
        tool_id="bash_run",
        workspace_id="ws",
        parameters={"commands": [f"cd '{subdir}'", f"cd '{subdir}'"]},
        data_dir=str(tmp_path),
    )
    r1 = await bash_run.execute(batch)
    assert r1.error == ""
    follow = ToolExecutionContext(
        tool_id="bash_run",
        workspace_id="ws",
        parameters={"command": "pwd"},
        data_dir=str(tmp_path),
    )
    r2 = await bash_run.execute(follow)
    assert r2.error == ""
    assert str(subdir) in r2.output_text
