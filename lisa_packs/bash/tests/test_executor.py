"""Output-parsing unit tests for the executor sentinel protocol."""

from __future__ import annotations

import base64

import pytest
from kernel.models import ToolExecutionContext, ToolResult

from lisa_packs.bash import executor

#: Exit codes used by the sentinel-parsing fixtures. ``127`` is the
#: shell's "command not found" code; ``3`` is an arbitrary nonzero code.
_COMMAND_NOT_FOUND_RC = 127
_NONZERO_RC = 3


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {"sandboxed": False, "return_code": 0},
        {"sandboxed": True, "degraded": True, "return_code": 0},
        {"sandboxed": True},
    ],
)
async def test_bwrap_dispatch_rejects_unverified_result(payload: dict[str, object]) -> None:
    async def dispatch(_tool_id: str, _parameters: object) -> ToolResult:
        return ToolResult(output_text="unverified", output_json=payload)

    ctx = ToolExecutionContext(delegated_tool_dispatch=dispatch)
    result = await executor._run_via_bwrap(
        ctx, ["true"], timeout_seconds=5, overrides={}, persist=False, layer_id=""
    )
    assert result.get("error")
    assert result.get("sandboxed") is False


def test_parse_output_strips_sentinel_and_rc() -> None:
    sentinel = "LISA_ABC>> "
    raw = f"hello world\nsecond line\n{sentinel}rc=0\n"
    parsed = executor._parse_output(raw, sentinel)
    assert parsed.return_code == 0
    assert "hello world" in parsed.body
    assert "rc=0" not in parsed.body
    assert sentinel not in parsed.body
    assert parsed.pipe_statuses == []


def test_parse_output_nonzero_rc() -> None:
    sentinel = "LISA_X>> "
    raw = f"err output\n{sentinel}rc={_COMMAND_NOT_FOUND_RC}\n"
    parsed = executor._parse_output(raw, sentinel)
    assert parsed.return_code == _COMMAND_NOT_FOUND_RC
    assert "err output" in parsed.body


def test_parse_output_prompt_only_lines_dropped() -> None:
    sentinel = "LISA_P>> "
    raw = f"{sentinel}\nvalue\n{sentinel}rc={_NONZERO_RC}\n{sentinel}"
    parsed = executor._parse_output(raw, sentinel)
    assert parsed.return_code == _NONZERO_RC
    assert parsed.body.strip() == "value"


def test_parse_output_captures_pipe_statuses() -> None:
    sentinel = "LISA_PIPE>> "
    raw = f"output\n{sentinel}rc=0 pipe=0 1 0\n"
    parsed = executor._parse_output(raw, sentinel)
    assert parsed.return_code == 0
    assert parsed.pipe_statuses == [0, 1, 0]
    assert "pipe=" not in parsed.body


def test_parse_output_captures_state_lines() -> None:
    sentinel = "LISA_S>> "
    raw = (
        "body\n"
        f"{sentinel}state=cwd=/home/adam\n"
        f"{sentinel}var FOO=bar\n"
        f"{sentinel}var QUOTED='hello world'\n"
        f"{sentinel}funcs myfn otherfn\n"
        f"{sentinel}rc=0 pipe=0\n"
    )
    parsed = executor._parse_output(raw, sentinel)
    assert parsed.return_code == 0
    assert parsed.state["cwd"] == "/home/adam"
    assert parsed.state["exported"] == {"FOO": "bar", "QUOTED": "hello world"}
    assert "funcs" not in parsed.state
    assert "state=" not in parsed.body
    assert "var " not in parsed.body
    assert "funcs " not in parsed.body


def test_parse_output_tolerates_missing_pipe_and_state() -> None:
    sentinel = "LISA_OLD>> "
    raw = f"output\n{sentinel}rc=0\n"
    parsed = executor._parse_output(raw, sentinel)
    assert parsed.return_code == 0
    assert parsed.pipe_statuses == []
    assert parsed.state == {}


def test_parse_output_captures_base64_function_bodies() -> None:
    sentinel = "LISA_FN>> "
    body = "myfn() {\n  echo inner\n}"
    b64 = base64.b64encode(body.encode("utf-8")).decode("ascii")
    raw = (
        f"{sentinel}func myfn {b64}\n"
        f"{sentinel}state=cwd=/tmp\n"
        f"{sentinel}var FOO=bar\n"
        f"{sentinel}rc=0\n"
    )
    parsed = executor._parse_output(raw, sentinel)
    assert parsed.return_code == 0
    assert parsed.state["functions"] == {"myfn": body}
    assert "func " not in parsed.body
    assert "echo inner" not in parsed.body


def test_parse_output_drops_corrupt_base64_function_marker() -> None:
    sentinel = "LISA_FN>> "
    # Emitted format is `<SENTINEL>func NAME <b64>` with no space after the
    # stripped sentinel; a non-base64 payload must be dropped, not leaked.
    raw = f"{sentinel.strip()}func broken !!!notb64!!!\n{sentinel}rc=0\n"
    parsed = executor._parse_output(raw, sentinel)
    assert parsed.return_code == 0
    assert "functions" not in parsed.state
    assert "func broken" not in parsed.body


def test_shape_empty_output_returns_explicit_note() -> None:
    result = executor.shape_empty_output("", "", 0)
    assert result != ""
    assert "no output" in result


def test_shape_empty_output_noop_on_output_or_failure() -> None:
    assert executor.shape_empty_output("hello", "", 0) == "hello"
    assert executor.shape_empty_output("", "err", 1) == ""


def test_benign_rc1_note_for_search_tools() -> None:
    assert executor.benign_rc1_note("grep -r foo src/", 1) is not None
    assert executor.benign_rc1_note("git diff", 1) is not None
    assert executor.benign_rc1_note("rm -rf /", 1) is None
    assert executor.benign_rc1_note("grep foo", 0) is None


def test_pipe_failure_note_middle_stage_nonzero() -> None:
    assert executor.pipe_failure_note([0, 1, 0], 0) is not None


def test_pipe_failure_note_none_when_all_zero_or_last_nonzero() -> None:
    assert executor.pipe_failure_note([0, 0, 0], 0) is None
    assert executor.pipe_failure_note([0, 1, 1], 1) is None


def test_is_auto_background_guarded() -> None:
    assert executor.is_auto_background_guarded("sleep 300")
    assert executor.is_auto_background_guarded("git status")
    assert executor.is_auto_background_guarded("foo && git commit")
    assert not executor.is_auto_background_guarded("make build")
    assert not executor.is_auto_background_guarded("pytest -q")


def test_resolve_batch_schedule_auto_parallel_when_bare() -> None:
    schedule, reason = executor.resolve_batch_schedule("auto", sandboxed=False)
    assert schedule == "parallel"
    assert reason is None


def test_resolve_batch_schedule_overlay_downgrades_parallel() -> None:
    schedule, reason = executor.resolve_batch_schedule("parallel", sandboxed=True, persist=True)
    assert schedule == "serial"
    assert reason is not None
    assert "overlay" in reason


def test_resolve_batch_schedule_capture_paths_downgrade_parallel() -> None:
    """Capture-path overrides are per-call overlay capture: serial always."""
    schedule, reason = executor.resolve_batch_schedule(
        "parallel",
        sandboxed=True,
        overrides={"paths": {"/etc": "capture"}},
    )
    assert schedule == "serial"
    assert reason is not None
    assert "overlay" in reason


def test_resolve_batch_schedule_plain_rw_paths_stay_parallel() -> None:
    """Non-capture path overrides do not force overlay serialization."""
    schedule, reason = executor.resolve_batch_schedule(
        "auto",
        sandboxed=False,
        overrides={"paths": {"/tmp/x": "rw"}},
    )
    assert schedule == "parallel"
    assert reason is None


def test_resolve_batch_schedule_layer_id_is_overlay() -> None:
    schedule, reason = executor.resolve_batch_schedule("auto", sandboxed=True, layer_id="layer-1")
    assert schedule == "serial"
    assert reason is not None


def test_resolve_batch_schedule_explicit_serial() -> None:
    schedule, reason = executor.resolve_batch_schedule("serial", sandboxed=False)
    assert schedule == "serial"
    assert reason is None


def test_floor_posture_payload_extracts_axes() -> None:
    """The containment fact's posture payload mirrors the authored floor."""
    from kernel.capability_binding import ExecutionFloorView

    from lisa_packs.bash.session import _floor_posture_payload

    payload = _floor_posture_payload(
        ExecutionFloorView(
            execution="sandbox",
            persistence="overlay",
            paths={"/etc": "capture", "/var/log": "ro"},
        )
    )
    assert payload["execution"] == "sandbox"
    assert payload["persistence"] == "overlay"
    assert payload["capture_paths"] == ["/etc"]

    # Unset floor -> empty payload (never guessed).
    assert _floor_posture_payload(None) == {}
    # Aliased persistence ('direct') is NOT the bwrap token set: excluded.
    assert "persistence" not in _floor_posture_payload(ExecutionFloorView(persistence="direct"))
