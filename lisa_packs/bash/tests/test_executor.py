"""Output-parsing unit tests for the executor sentinel protocol."""

from __future__ import annotations

from lisa_packs.bash import executor


def test_parse_output_strips_sentinel_and_rc() -> None:
    sentinel = "LISA_ABC>> "
    raw = (
        "hello world\n"
        "second line\n"
        f"{sentinel}rc=0\n"
    )
    parsed = executor._parse_output(raw, sentinel)
    assert parsed.return_code == 0
    assert "hello world" in parsed.body
    assert "rc=0" not in parsed.body
    assert sentinel not in parsed.body
    assert parsed.pipe_statuses == []


def test_parse_output_nonzero_rc() -> None:
    sentinel = "LISA_X>> "
    raw = f"err output\n{sentinel}rc=127\n"
    parsed = executor._parse_output(raw, sentinel)
    assert parsed.return_code == 127
    assert "err output" in parsed.body


def test_parse_output_prompt_only_lines_dropped() -> None:
    sentinel = "LISA_P>> "
    raw = f"{sentinel}\nvalue\n{sentinel}rc=3\n{sentinel}"
    parsed = executor._parse_output(raw, sentinel)
    assert parsed.return_code == 3
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
    schedule, reason = executor.resolve_batch_schedule(
        "auto", sandboxed=False
    )
    assert schedule == "parallel"
    assert reason is None


def test_resolve_batch_schedule_overlay_downgrades_parallel() -> None:
    schedule, reason = executor.resolve_batch_schedule(
        "parallel", sandboxed=True, persist=True
    )
    assert schedule == "serial"
    assert reason is not None
    assert "overlay" in reason


def test_resolve_batch_schedule_layer_id_is_overlay() -> None:
    schedule, reason = executor.resolve_batch_schedule(
        "auto", sandboxed=True, layer_id="layer-1"
    )
    assert schedule == "serial"
    assert reason is not None


def test_resolve_batch_schedule_explicit_serial() -> None:
    schedule, reason = executor.resolve_batch_schedule(
        "serial", sandboxed=False
    )
    assert schedule == "serial"
    assert reason is None
