"""bash_run handler tests (executor mocked; output parsing + flags asserted)."""

from __future__ import annotations

from pathlib import Path

import pytest

from kernel.models import ToolExecutionContext

from lisa_packs.bash import executor as _executor
from lisa_packs.bash.tools.bash_run import bash_run


def _ctx(tmp_path: Path, command: str = "echo hi") -> ToolExecutionContext:
    return ToolExecutionContext(
        tool_id="bash_run",
        workspace_id="ws",
        user_slug="u",
        surface_id="surf",
        session_plan_digest="d",
        parameters={"command": command},
        data_dir=str(tmp_path),
    )


@pytest.fixture(autouse=True)
def _fresh_sessions():
    _executor._reset_for_tests()
    yield
    _executor._reset_for_tests()


@pytest.mark.asyncio
async def test_missing_bash_returns_hard_error(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(bash_run, "bash_available", lambda: False)
    result = await bash_run.execute(_ctx(tmp_path))
    assert result.error
    assert "bash" in result.error
    assert result.output_json["missing"] == ["bash"]


@pytest.mark.asyncio
async def test_unsandboxed_reports_sandboxed_false(monkeypatch, tmp_path: Path) -> None:
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
    result = await bash_run.execute(_ctx(tmp_path))
    assert result.error == ""
    assert result.output_text == "hi"
    assert result.output_json["sandboxed"] is False
    assert "bwrap" in result.output_json["note"]
    assert result.output_json["log_path"].endswith(".log")


@pytest.mark.asyncio
async def test_sandboxed_composition_threaded(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(bash_run, "bash_available", lambda: True)
    monkeypatch.setattr(bash_run, "_sandbox_available", lambda: True)

    seen: dict = {}

    async def _fake_run_in_session(ctx, session, command, **kwargs):
        seen["command"] = command
        seen["kwargs"] = kwargs
        return {
            "stdout": "ok",
            "stderr": "",
            "return_code": 0,
            "timed_out": False,
            "truncated": False,
            "sandboxed": True,
            "backend": "tmpfs",
            "layer_id": "session-x",
            "persistence": "ephemeral",
        }

    monkeypatch.setattr(bash_run, "run_in_session", _fake_run_in_session)
    ctx = _ctx(tmp_path, command="touch /tmp/x")
    ctx.parameters["network"] = True
    ctx.parameters["persist"] = True
    result = await bash_run.execute(ctx)
    assert result.error == ""
    assert result.output_json["sandboxed"] is True
    assert result.output_json["backend"] == "tmpfs"
    assert seen["command"] == "touch /tmp/x"
    assert seen["kwargs"]["network"] is True
    assert seen["kwargs"]["persist"] is True


@pytest.mark.asyncio
async def test_invalid_params_rejected(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    ctx.parameters = {"command": ""}  # violates min_length
    result = await bash_run.execute(ctx)
    assert "Invalid parameters" in result.error


@pytest.mark.asyncio
async def test_rc_recovers_truthful_exit_code(monkeypatch, tmp_path: Path) -> None:
    """Regression: failing commands must surface their real exit code.

    The rc marker is emitted by the wrapped command (prompts never render
    under ``bash -c``), and the executor must not mask a missing marker with
    a fallback 0.
    """
    monkeypatch.setattr(bash_run, "bash_available", lambda: True)
    monkeypatch.setattr(bash_run, "_sandbox_available", lambda: False)

    for command, expected_rc in (
        ("false", 1),
        ("exit 7", 7),
        ("printf 'x'; exit 3", 3),
    ):
        ctx = _ctx(tmp_path, command=command)
        result = await bash_run.execute(ctx)
        assert result.error == ""
        assert result.output_json["return_code"] == expected_rc, (
            f"{command!r}: expected rc={expected_rc}"
        )


@pytest.mark.asyncio
async def test_output_without_trailing_newline_preserved(
    monkeypatch, tmp_path: Path
) -> None:
    """The rc marker must stay on its own line; final output line survives."""
    monkeypatch.setattr(bash_run, "bash_available", lambda: True)
    monkeypatch.setattr(bash_run, "_sandbox_available", lambda: False)

    ctx = _ctx(tmp_path, command="printf 'no-trailing-newline'")
    result = await bash_run.execute(ctx)
    assert result.error == ""
    assert result.output_text == "no-trailing-newline"
    assert result.output_json["return_code"] == 0
