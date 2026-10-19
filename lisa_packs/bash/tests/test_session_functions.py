"""Session state carryover: policy, function capture, and replay.

Exercises the Phase 2 surface: baseline function snapshot, policy modes
(off / allowlist / blocklist + fnmatch patterns), state-file function
sections, batch cwd adoption that preserves bodies, and live bash
round-trips (marked ``live``).
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

import pytest
from kernel.capability_binding import ExecutionFloorView

from kernel.models import ToolExecutionContext

from lisa_packs.bash import executor as _executor
from lisa_packs.bash.tools.bash_run import bash_run


@pytest.fixture(autouse=True)
def _fresh_sessions():
    _executor._reset_for_tests()
    yield
    _executor._reset_for_tests()


def _ctx(
    tmp_path: Path, pack_config: dict | None = None, **params
) -> ToolExecutionContext:
    return ToolExecutionContext(
        tool_id="bash_run",
        workspace_id="ws",
        parameters={"command": "true", **params},
        data_dir=str(tmp_path),
        pack_config=pack_config or {},
        execution_floor_for=lambda _tool_id: ExecutionFloorView(execution="bare"),
    )


def _data_dir() -> Path:
    return Path(tempfile.mkdtemp(prefix="lisa-bash-fn-test-"))


def _session(policy: dict, data_dir: Path | None = None):
    from lisa_packs.bash.priming import SessionPriming
    from lisa_packs.bash.session import BashSession

    data_dir = data_dir or _data_dir()
    priming = SessionPriming(data_dir=data_dir, session_key="k")
    priming.render()
    return BashSession(
        session_key="k",
        data_dir=data_dir,
        priming=priming,
        sandboxed=False,
        state_policy=policy,
    )


def test_policy_floor_defaults() -> None:
    """Floor is cwd+env on, functions off, no patterns."""
    from lisa_packs.bash.session import _DEFAULT_SESSION_STATE, _session_state_policy

    assert _DEFAULT_SESSION_STATE["functions"] == "off"
    assert _session_state_policy(_ctx(Path(tempfile.mkdtemp()))) == {
        "cwd": True,
        "env": True,
        "functions": "off",
        "patterns": [],
    }


def test_policy_parses_all_modes_and_ignores_unknown_keys(tmp_path: Path) -> None:
    """functions mode + patterns parse; unknown keys and bad values ignored."""
    from lisa_packs.bash.session import _session_state_policy

    policy = _session_state_policy(
        _ctx(
            tmp_path,
            pack_config={
                "session_state": {
                    "functions": "allowlist",
                    "patterns": ["build*", "deploy_*"],
                    "cwd": False,
                    "env": False,
                    "unknown_key": "ignored",
                }
            },
        )
    )
    assert policy["functions"] == "allowlist"
    assert policy["patterns"] == ["build*", "deploy_*"]
    assert policy["cwd"] is False
    assert policy["env"] is False
    assert "unknown_key" not in policy

    blocked = _session_state_policy(
        _ctx(tmp_path, pack_config={"session_state": {"functions": "blocklist"}})
    )
    assert blocked["functions"] == "blocklist"

    bogus = _session_state_policy(
        _ctx(tmp_path, pack_config={"session_state": {"functions": "bogus", "patterns": 7}})
    )
    assert bogus["functions"] == "off"
    assert bogus["patterns"] == []


def test_select_functions_modes() -> None:
    """allowlist/blocklist + fnmatch patterns select the replayed set."""
    funcs = {"build_all": "b", "lint": "l", "deploy_prod": "d"}

    allow = _session({"functions": "allowlist", "patterns": ["build*"]})
    assert allow._select_functions(funcs) == {"build_all": "b"}

    block = _session({"functions": "blocklist", "patterns": ["*prod*"]})
    assert block._select_functions(funcs) == {"build_all": "b", "lint": "l"}

    off = _session({"functions": "off", "patterns": []})
    assert off._select_functions(funcs) == {}


def test_wrapper_emits_base64_func_marker(tmp_path: Path) -> None:
    """The wrapper contains a base64 declare -f emitter guarded by base64."""
    session = _session({})
    argv = session.build_inner_argv("true")
    script = argv[argv.index("-c") + 1]
    assert "compgen -A function" in script
    assert "base64" in script
    assert "func " in script


def test_record_state_writes_function_section() -> None:
    """Policy-permitted bodies land under the function section marker."""
    session = _session({"functions": "allowlist", "patterns": ["greet*"]})
    body = "greet_world() { echo hi; }"
    session.record_post_run_state(
        cwd="",
        exported={},
        functions={"greet_world": body, "secret_fn": "secret_fn() { :; }"},
    )
    text = session.priming.state_path.read_text(encoding="utf-8")
    assert "# === lisa function replay ===" in text
    assert body in text
    assert "secret_fn" not in text


def test_record_state_default_off_writes_no_functions() -> None:
    """With functions off, no function section is written."""
    session = _session({})
    session.record_post_run_state(
        cwd="", exported={}, functions={"greet_world": "greet_world() { :; }"}
    )
    text = session.priming.state_path.read_text(encoding="utf-8")
    assert "# === lisa function replay ===" not in text


def test_record_state_excludes_baseline_functions() -> None:
    """Functions the rc defines are delta-filtered out at record time."""
    session = _session({"functions": "allowlist", "patterns": ["*"]})
    session._base_funcs = frozenset({"rc_managed_fn"})
    session.record_post_run_state(
        cwd="",
        exported={},
        functions={"rc_managed_fn": "rc_managed_fn() { :; }", "new_fn": "new_fn() { :; }"},
    )
    text = session.priming.state_path.read_text(encoding="utf-8")
    assert "rc_managed_fn" not in text
    assert "new_fn() { :; }" in text


def test_apply_cwd_preserves_function_section() -> None:
    """Batch cwd adoption must not drop or corrupt the function section."""
    session = _session({"functions": "allowlist", "patterns": ["*"]})
    body = "deploy() {\n  cd /opt/app\n  echo deploying\n}"
    session.record_post_run_state(cwd="/tmp/one", exported={}, functions={"deploy": body})
    session.apply_cwd("/tmp/two")
    text = session.priming.state_path.read_text(encoding="utf-8")
    assert "cd /tmp/two" in text
    assert "cd /tmp/one" not in text
    # The function body kept its own internal cd statement.
    assert "  cd /opt/app" in text
    assert body in text


def test_apply_cwd_noop_when_policy_off() -> None:
    """cwd: false policy means apply_cwd does nothing."""
    session = _session({"functions": "off", "patterns": [], "cwd": False})
    session.record_post_run_state(cwd="/tmp/one", exported={}, functions={})
    session.apply_cwd("/tmp/two")
    assert "cd /tmp/two" not in session.priming.state_path.read_text(encoding="utf-8")


def test_apply_cwd_on_missing_state_file() -> None:
    """apply_cwd works when no state file exists yet."""
    session = _session({})
    session.apply_cwd("/tmp/fresh")
    text = session.priming.state_path.read_text(encoding="utf-8")
    assert "cd /tmp/fresh" in text


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not installed")
@pytest.mark.asyncio
async def test_bash_run_function_replay_live(tmp_path: Path) -> None:
    """A function defined in call 1 runs in call 2 when policy permits."""
    pack_config = {
        "session_state": {
            "functions": "allowlist",
            "patterns": ["lisa_fn_*"],
        }
    }
    first = ToolExecutionContext(
        tool_id="bash_run",
        workspace_id="ws",
        parameters={"command": "lisa_fn_hello() { echo func-ran; }; lisa_fn_hello"},
        data_dir=str(tmp_path),
        pack_config=pack_config,
        execution_floor_for=lambda _tool_id: ExecutionFloorView(execution="bare"),
    )
    r1 = await bash_run.execute(first)
    assert r1.error == ""
    assert "func-ran" in r1.output_text

    second = ToolExecutionContext(
        tool_id="bash_run",
        workspace_id="ws",
        parameters={"command": "lisa_fn_hello"},
        data_dir=str(tmp_path),
        pack_config=pack_config,
        execution_floor_for=lambda _tool_id: ExecutionFloorView(execution="bare"),
    )
    r2 = await bash_run.execute(second)
    assert r2.error == ""
    assert "func-ran" in r2.output_text


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not installed")
@pytest.mark.asyncio
async def test_bash_run_function_replay_default_off(tmp_path: Path) -> None:
    """With the default policy, a function defined in call 1 is gone in call 2."""
    first = ToolExecutionContext(
        tool_id="bash_run",
        workspace_id="ws",
        parameters={"command": "lisa_fn_bye() { echo one; }; lisa_fn_bye"},
        data_dir=str(tmp_path),
        execution_floor_for=lambda _tool_id: ExecutionFloorView(execution="bare"),
    )
    r1 = await bash_run.execute(first)
    assert r1.error == ""
    assert "one" in r1.output_text

    second = ToolExecutionContext(
        tool_id="bash_run",
        workspace_id="ws",
        parameters={"command": "lisa_fn_bye"},
        data_dir=str(tmp_path),
        execution_floor_for=lambda _tool_id: ExecutionFloorView(execution="bare"),
    )
    r2 = await bash_run.execute(second)
    assert r2.error == ""
    assert r2.output_json["return_code"] != 0


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not installed")
@pytest.mark.asyncio
async def test_bash_run_function_replay_pattern_filtering(tmp_path: Path) -> None:
    """Only pattern-matching names replay when the allowlist misses one."""
    pack_config = {
        "session_state": {
            "functions": "allowlist",
            "patterns": ["lisa_fn_keep*"],
        }
    }
    first = ToolExecutionContext(
        tool_id="bash_run",
        workspace_id="ws",
        parameters={
            "command": (
                "lisa_fn_keep() { echo kept; }; "
                "lisa_fn_drop() { echo dropped; }; lisa_fn_keep"
            )
        },
        data_dir=str(tmp_path),
        pack_config=pack_config,
        execution_floor_for=lambda _tool_id: ExecutionFloorView(execution="bare"),
    )
    r1 = await bash_run.execute(first)
    assert r1.error == ""
    assert "kept" in r1.output_text

    second = ToolExecutionContext(
        tool_id="bash_run",
        workspace_id="ws",
        parameters={"command": "lisa_fn_keep; lisa_fn_drop"},
        data_dir=str(tmp_path),
        pack_config=pack_config,
        execution_floor_for=lambda _tool_id: ExecutionFloorView(execution="bare"),
    )
    r2 = await bash_run.execute(second)
    assert r2.error == ""
    assert "kept" in r2.output_text
    assert r2.output_json["return_code"] != 0


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not installed")
@pytest.mark.asyncio
async def test_bash_run_baseline_snapshot_returns_funcs(tmp_path: Path) -> None:
    """The host baseline probe captures the rc's export AND function sets."""
    from lisa_packs.bash.session import _baseline_snapshot, resolve_or_create_session

    ctx = ToolExecutionContext(
        tool_id="bash_run",
        workspace_id="ws",
        parameters={"command": "true"},
        data_dir=str(tmp_path),
        pack_config={"session_state": {"functions": "blocklist"}},
    )
    session = resolve_or_create_session(ctx, sandboxed=False)
    env, funcs = _baseline_snapshot(session.priming.rc_path.read_text(encoding="utf-8"))
    assert isinstance(env, frozenset)
    assert isinstance(funcs, frozenset)
    # The rc defines no functions itself, but the probe must not crash and
    # must exclude noise; an empty set is the correct baseline here.
    assert "lisa_fn_bye" not in funcs