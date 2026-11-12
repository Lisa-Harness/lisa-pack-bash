"""bash_run history semantics: the observation classifier and the kernel hooks."""

from __future__ import annotations

import json
import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest
from kernel.capability_binding import ExecutionFloorView
from kernel.context.mcp_catalog import _deserialize_tool
from kernel.history import HistoryKeys, HistoryPolicy, resolve_history_keys
from kernel.models import ToolExecutionContext, ToolResult
from sdk.content import (
    build_tool_catalog_json,
    invalidate_catalog_cache,
    resolve_tool_roots,
)

from lisa_packs.bash import executor as _executor
from lisa_packs.bash.history import (
    OBSERVATION_PREFIX,
    history_keys,
    history_snapshot,
    observation_key,
    resolve_history_anchor,
)
from lisa_packs.bash.session import _SESSIONS
from lisa_packs.bash.tools.bash_run import bash_run

PACK_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _fresh_sessions() -> Iterator[None]:
    _executor._reset_for_tests()
    yield
    _executor._reset_for_tests()


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("ls -la", "ls -la"),
        ("  git   status  ", "git status"),
        ("git log --oneline | head -5", "git log --oneline | head -5"),
        ("grep -rn 'a b' src", "grep -rn 'a b' src"),
        ("find . -name '*.py'", "find . -name '*.py'"),
        ("env", "env"),
        ("git log --oneline -n5", "git log --oneline -n5"),
        ("sort -k2 -u data.txt", "sort -k2 -u data.txt"),
        ("tree -L 2", "tree -L 2"),
    ],
)
def test_observation_commands_normalize(command: str, expected: str) -> None:
    assert observation_key(command) == expected


@pytest.mark.parametrize(
    "command",
    [
        "cd src",
        "make test",
        "ls > out.txt",
        "cat a && rm a",
        "ls; touch x",
        "cat a || true",
        "echo $(id)",
        "ls `pwd`",
        "sleep 9 &",
        "cat a | tee b",
        "find . -delete",
        "find . -exec rm {} +",
        "find . -fprintf out %p",
        "sort -o out in",
        "sort -uo out in",
        "sort --o=out in",
        "sort --out out in",
        "sort --compress-program=gzip in",
        "git diff --outp=patch",
        "tree -R -H . -L 1",
        "file -C -m magic",
        "git diff --output=patch",
        "git checkout main",
        "git -c x=y status",
        "rg --pre ./run pattern",
        "tree -o out",
        "env FOO=1 make",
        "cat 'unterminated",
        "",
    ],
)
def test_effectful_or_unparseable_commands_are_not_observations(command: str) -> None:
    assert observation_key(command) == ""


def _result(**output: object) -> ToolResult:
    return ToolResult(tool_id="bash_run", output_text="x", output_json=output)


def test_observation_claims_key_and_spill_anchor() -> None:
    claims = history_keys(
        {"command": "cat log.txt", "layer_id": ""},
        _result(stdout_path="/run/jobs/stdout-1.log"),
    )
    assert claims == {
        "keys": [f"{OBSERVATION_PREFIX}-:cat log.txt"],
        "anchor": "bash_run:file:/run/jobs/stdout-1.log",
    }
    layered = history_keys({"command": "ls", "layer_id": "L1"}, _result())
    assert layered == {"keys": [f"{OBSERVATION_PREFIX}L1:ls"]}


def test_mutations_invalidate_observations_and_fold_at_turn_end() -> None:
    assert history_keys({"command": "cd src"}, _result()) == {
        "scope": "turn",
        "invalidates": [OBSERVATION_PREFIX],
    }
    timed_out = history_keys({"command": "cat fifo"}, _result(timed_out=True))
    assert timed_out == {"scope": "turn", "invalidates": []}


def test_batches_are_turn_scoped_and_background_jobs_make_no_claims() -> None:
    reads = history_keys({"commands": ["ls", "git status"]}, _result(batch=True))
    assert reads == {"scope": "turn", "invalidates": []}
    mixed = history_keys({"commands": ["ls", "make"]}, _result(batch=True))
    assert mixed == {"scope": "turn", "invalidates": [OBSERVATION_PREFIX]}
    # Background jobs keep their descriptor resident; a writer still stales observations.
    writer = {"invalidates": [OBSERVATION_PREFIX]}
    assert history_keys({"command": "make", "run_in_background": True}, _result()) == writer
    assert history_keys({"command": "make"}, _result(backgrounded=True)) == writer
    tail = {"command": "tail -f log", "run_in_background": True}
    assert history_keys(tail, _result()) is None


def test_catalog_declares_the_hook_and_kernel_resolves_through_it() -> None:
    invalidate_catalog_cache()
    catalog = json.loads(build_tool_catalog_json(resolve_tool_roots([PACK_ROOT])))
    definition = _deserialize_tool(catalog["bash_run"])
    assert definition.history == HistoryPolicy(hook=True, snapshot=True)
    # The kernel fills Parameters defaults before calling the hook.
    claims = resolve_history_keys(
        definition.history,
        definition.implementation_module,
        {"command": "pwd"},
        _result(),
    )
    assert claims == HistoryKeys(keys=(f"{OBSERVATION_PREFIX}-:pwd",))


def _ctx(tmp_path: Path, command: str) -> ToolExecutionContext:
    return ToolExecutionContext(
        tool_id="bash_run",
        workspace_id="ws",
        parameters={"command": command},
        data_dir=str(tmp_path),
        execution_floor_for=lambda _tool_id: ExecutionFloorView(execution="bare"),
    )


@pytest.mark.live
@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not installed")
@pytest.mark.asyncio
async def test_snapshot_and_anchor_read_the_live_session(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(bash_run, "_sandbox_available", lambda: False)
    ctx = _ctx(tmp_path, "")
    assert await history_snapshot(ctx, []) == ""
    workdir = tmp_path / "workdir"
    workdir.mkdir()
    result = await bash_run.execute(_ctx(tmp_path, f"cd {workdir}"))
    assert result.error == "", result.error

    snapshot = await history_snapshot(ctx, [])
    assert snapshot == (f"bash session: cwd={workdir}; sandboxed=false; running jobs=none")

    (session,) = _SESSIONS.values()
    session.jobs_dir.mkdir(parents=True, exist_ok=True)
    spill = session.jobs_dir / "stdout-abc.log"
    spill.write_text("full output\n", encoding="utf-8")
    assert await resolve_history_anchor(ctx, f"bash_run:file:{spill}") == "full output\n"
    outside = tmp_path / "secret.txt"
    outside.write_text("nope", encoding="utf-8")
    assert await resolve_history_anchor(ctx, f"bash_run:file:{outside}") is None
    escape = session.jobs_dir / ".." / ".." / ".." / ".." / "secret.txt"
    assert await resolve_history_anchor(ctx, f"bash_run:file:{escape}") is None
    assert await resolve_history_anchor(ctx, "bash_run:other:x") is None
