"""bash_run handler tests (executor mocked; output parsing + flags asserted)."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from kernel.capability_binding import ExecutionFloorView
from kernel.models import ToolExecutionContext
from pydantic import ValidationError

from lisa_packs.bash import executor as _executor
from lisa_packs.bash.session import BashSession
from lisa_packs.bash.tools.bash_run import bash_run

#: Number of children in the two-command batch fixtures.
_BATCH_CHILD_COUNT = 2


def _ctx(
    tmp_path: Path, command: str = "echo hi", *, execution: str = "bare"
) -> ToolExecutionContext:
    return ToolExecutionContext(
        tool_id="bash_run",
        workspace_id="ws",
        user_slug="u",
        surface_id="surf",
        session_plan_digest="d",
        parameters={"command": command},
        data_dir=str(tmp_path),
        execution_floor_for=lambda _tool_id: ExecutionFloorView(execution=execution),
    )


@pytest.fixture(autouse=True)
def _fresh_sessions() -> Iterator[None]:
    _executor._reset_for_tests()
    yield
    _executor._reset_for_tests()


@pytest.mark.asyncio
async def test_missing_bash_returns_hard_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(bash_run, "bash_available", lambda: False)
    result = await bash_run.execute(_ctx(tmp_path))
    assert result.error
    assert "bash" in result.error
    assert result.output_json["missing"] == ["bash"]


@pytest.mark.asyncio
async def test_unsandboxed_reports_sandboxed_false(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(bash_run, "bash_available", lambda: True)
    monkeypatch.setattr(bash_run, "_sandbox_available", lambda: False)

    async def _fake_run_in_session(
        ctx: ToolExecutionContext,
        session: BashSession,
        command: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
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
    assert "permits bare execution" in result.output_json["note"]
    assert result.output_json["log_path"].endswith(".log")


@pytest.mark.asyncio
async def test_sandboxed_composition_threaded(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(bash_run, "bash_available", lambda: True)
    monkeypatch.setattr(bash_run, "_sandbox_available", lambda: True)

    seen: dict[str, Any] = {}

    async def _fake_run_in_session(
        ctx: ToolExecutionContext,
        session: BashSession,
        command: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
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
            "identity": {
                "sandbox_uid": 0,
                "host_uid": 1000,
                "note": "overflow-uid display artifact; see sandbox_ownership.md",
            },
        }

    monkeypatch.setattr(bash_run, "run_in_session", _fake_run_in_session)
    ctx = _ctx(tmp_path, command="touch /tmp/x", execution="sandbox")
    ctx.parameters["overrides"] = {"network": "allow"}
    ctx.parameters["persist"] = True
    result = await bash_run.execute(ctx)
    assert result.error == ""
    assert result.output_json["sandboxed"] is True
    assert result.output_json["backend"] == "tmpfs"
    assert seen["command"] == "touch /tmp/x"
    assert seen["kwargs"]["overrides"] == {"network": "allow"}
    assert seen["kwargs"]["persist"] is True
    # The bwrap identity stamp is forwarded to the model surface.
    assert result.output_json["identity"]["sandbox_uid"] == 0
    assert "sandbox_ownership.md" in result.output_json["identity"]["note"]


@pytest.mark.asyncio
async def test_invalid_params_rejected(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    ctx.parameters = {"command": ""}  # violates min_length
    result = await bash_run.execute(ctx)
    assert "Invalid parameters" in result.error


@pytest.mark.asyncio
async def test_command_and_commands_mutually_exclusive(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    ctx.parameters = {"command": "echo hi", "commands": ["echo a"]}
    result = await bash_run.execute(ctx)
    assert "Invalid parameters" in result.error


@pytest.mark.asyncio
async def test_commands_cap_enforced(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    ctx.parameters = {"commands": [f"echo {i}" for i in range(9)]}
    result = await bash_run.execute(ctx)
    assert "Invalid parameters" in result.error
    assert "capped" in result.error


@pytest.mark.asyncio
async def test_commands_json_string_is_coerced_to_list() -> None:
    """A JSON-encoded string for ``commands`` validates as a real list.

    Regression for the 2026-10-18 production incident: under a JSON-mode
    (strict=False) tool schema the model emitted ``commands`` as a STRING
    containing the JSON array and four consecutive identical
    ``Input should be a valid list`` validation errors burned turn
    iterations (the model's retries reproduced the same shape each time —
    its error message contained no explanation of the expected shape).
    The lenient validator parses a string that decodes to a list; the
    existing exclusivity/cap/emptiness rules still apply after coercion.
    """
    from lisa_packs.bash.tools.bash_run.bash_run import Parameters

    params = Parameters.model_validate({"commands": '["echo a", "echo b"]'})
    assert params.commands == ["echo a", "echo b"]
    assert params.command is None


@pytest.mark.asyncio
async def test_commands_non_json_string_still_rejected() -> None:
    """A plain (non-JSON) string for ``commands`` keeps the list_type error."""
    from lisa_packs.bash.tools.bash_run.bash_run import Parameters

    with pytest.raises(ValidationError):
        Parameters.model_validate({"commands": "echo a"})


@pytest.mark.asyncio
async def test_commands_json_non_list_string_still_rejected() -> None:
    """A JSON scalar (not a list) for ``commands`` keeps the list_type error."""
    from lisa_packs.bash.tools.bash_run.bash_run import Parameters

    with pytest.raises(ValidationError):
        Parameters.model_validate({"commands": '"just a string"'})


@pytest.mark.asyncio
async def test_backgrounded_short_circuit(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(bash_run, "bash_available", lambda: True)
    monkeypatch.setattr(bash_run, "_sandbox_available", lambda: False)

    async def _fake_run_in_session(
        ctx: ToolExecutionContext,
        session: BashSession,
        command: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        return {
            "backgrounded": True,
            "job_id": "jabc123",
            "log_path": str(tmp_path / "jabc123.log"),
            "rc_path": str(tmp_path / "jabc123.rc"),
            "status_path": str(tmp_path / "jabc123.status.json"),
            "status": "running",
            "sandboxed": False,
        }

    monkeypatch.setattr(bash_run, "run_in_session", _fake_run_in_session)
    ctx = _ctx(tmp_path, command="make build")
    ctx.parameters["run_in_background"] = True
    result = await bash_run.execute(ctx)
    assert result.error == ""
    assert result.output_json["backgrounded"] is True
    assert result.output_json["job_id"] == "jabc123"
    assert result.output_json["status"] == "running"


@pytest.mark.asyncio
async def test_batch_result_short_circuit(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(bash_run, "bash_available", lambda: True)
    monkeypatch.setattr(bash_run, "_sandbox_available", lambda: False)

    async def _fake_batch(
        ctx: ToolExecutionContext,
        session: BashSession,
        commands: list[str],
        **kwargs: Any,
    ) -> dict[str, Any]:
        return {
            "batch": True,
            "when": "parallel",
            "requested_when": "auto",
            "results": [
                {"index": 0, "command": commands[0], "stdout": "a", "return_code": 0},
                {"index": 1, "command": commands[1], "stdout": "b", "return_code": 0},
            ],
            "return_code": 0,
            "sandboxed": False,
            "timed_out": False,
            "truncated": False,
            "note": "session cwd/env unchanged",
        }

    monkeypatch.setattr(bash_run, "run_batch_in_session", _fake_batch)
    ctx = _ctx(tmp_path)
    ctx.parameters = {"commands": ["echo a", "echo b"]}
    result = await bash_run.execute(ctx)
    assert result.error == ""
    assert result.output_json["batch"] is True
    assert result.output_json["when"] == "parallel"
    assert len(result.output_json["results"]) == _BATCH_CHILD_COUNT


@pytest.mark.asyncio
async def test_batch_overlay_downgrades_to_serial(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(bash_run, "bash_available", lambda: True)
    monkeypatch.setattr(bash_run, "_sandbox_available", lambda: True)
    monkeypatch.setattr(bash_run, "_sandbox_degraded", lambda: [])

    seen: dict[str, Any] = {}

    async def _fake_batch(
        ctx: ToolExecutionContext,
        session: BashSession,
        commands: list[str],
        **kwargs: Any,
    ) -> dict[str, Any]:
        seen["when"] = kwargs.get("when")
        seen["persist"] = kwargs.get("persist")
        return {
            "batch": True,
            "when": "serial",
            "requested_when": kwargs.get("when"),
            "reason": "overlay capture is per-call; parallel batch downgraded to serial",
            "results": [
                {"index": i, "command": c, "stdout": "", "return_code": 0}
                for i, c in enumerate(commands)
            ],
            "return_code": 0,
            "sandboxed": True,
            "timed_out": False,
            "truncated": False,
            "note": "overlay capture is per-call; parallel batch downgraded to serial",
        }

    monkeypatch.setattr(bash_run, "run_batch_in_session", _fake_batch)
    ctx = _ctx(tmp_path)
    ctx.parameters = {
        "commands": ["echo a", "echo b"],
        "when": "parallel",
        "persist": True,
    }
    result = await bash_run.execute(ctx)
    assert result.error == ""
    assert seen["when"] == "parallel"
    assert seen["persist"] is True
    assert result.output_json["when"] == "serial"
    assert "overlay" in result.output_json["reason"]


@pytest.mark.asyncio
async def test_rc_recovers_truthful_exit_code(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
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
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The rc marker must stay on its own line; final output line survives."""
    monkeypatch.setattr(bash_run, "bash_available", lambda: True)
    monkeypatch.setattr(bash_run, "_sandbox_available", lambda: False)

    ctx = _ctx(tmp_path, command="printf 'no-trailing-newline'")
    result = await bash_run.execute(ctx)
    assert result.error == ""
    assert result.output_text == "no-trailing-newline"
    assert result.output_json["return_code"] == 0


@pytest.mark.asyncio
async def test_missing_bwrap_binary_sandbox_error_surfaces_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Regression: pack installed but bwrap binary absent must fail loudly.

    The 2026-09-05 tumbleweed3 incident: ``_sandbox_available()`` checks pack
    registration, not the host binary, so the session routed through
    ``bwrap_run``, which errored - and the model only saw an opaque failure
    with a bare ``sandboxed`` stamp and no remediation. The error result must
    now carry the missing prerequisites plus the remediation doc.
    """
    monkeypatch.setattr(bash_run, "bash_available", lambda: True)
    monkeypatch.setattr(bash_run, "_sandbox_available", lambda: True)
    monkeypatch.setattr(
        bash_run,
        "_sandbox_degraded",
        lambda: ["bubblewrap (bwrap) binary not found on PATH"],
    )

    async def _fake_run_in_session(
        ctx: ToolExecutionContext,
        session: BashSession,
        command: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        return {
            "error": (
                "sandbox execution failed: bubblewrap sandbox is not available: "
                "bubblewrap (bwrap) binary not found on PATH"
            ),
            "sandboxed": True,
            "missing": ["bubblewrap (bwrap) binary not found on PATH"],
        }

    monkeypatch.setattr(bash_run, "run_in_session", _fake_run_in_session)
    result = await bash_run.execute(_ctx(tmp_path, execution="sandbox"))
    assert "requires contained execution" in result.error
    assert result.output_json["sandboxed"] is False
    assert "bubblewrap" in " ".join(result.output_json["missing"])
    assert "zypper install bubblewrap" in result.output_json["doc"]


@pytest.mark.asyncio
async def test_missing_bwrap_binary_refuses_execution(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A registered-but-broken sandbox must not execute or report success."""
    monkeypatch.setattr(bash_run, "bash_available", lambda: True)
    monkeypatch.setattr(bash_run, "_sandbox_available", lambda: True)
    monkeypatch.setattr(
        bash_run,
        "_sandbox_degraded",
        lambda: ["bubblewrap (bwrap) binary not found on PATH"],
    )

    async def _fake_run_in_session(
        ctx: ToolExecutionContext,
        session: BashSession,
        command: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        raise AssertionError("a missing sandbox must prevent execution")

    monkeypatch.setattr(bash_run, "run_in_session", _fake_run_in_session)
    result = await bash_run.execute(_ctx(tmp_path, execution="sandbox"))
    assert "requires contained execution" in result.error
    assert result.output_json["sandboxed"] is False
    assert "bubblewrap" in " ".join(result.output_json["missing"])


@pytest.mark.asyncio
async def test_executor_exception_stamps_truthful_sandboxed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A raised (rather than error-result) sandbox failure stamps sandboxed=True."""
    monkeypatch.setattr(bash_run, "bash_available", lambda: True)
    monkeypatch.setattr(bash_run, "_sandbox_available", lambda: True)
    monkeypatch.setattr(bash_run, "_sandbox_degraded", lambda: [])

    async def _raise_run_in_session(
        ctx: ToolExecutionContext,
        session: BashSession,
        command: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        raise RuntimeError("bwrap_run blew up")

    monkeypatch.setattr(bash_run, "run_in_session", _raise_run_in_session)
    result = await bash_run.execute(_ctx(tmp_path, execution="sandbox"))
    assert result.error
    assert result.output_json["sandboxed"] is True
