"""History-semantics eval: bash_run's observation claims on real results.

Drives ``cd → ls → cat → ls → touch → ls → [pwd, head | wc]`` through a live
(unsandboxed) bash session and feeds every real ``ToolResult`` through the
kernel's history ledger exactly as ``run_turn`` does. Asserts that a re-run
observation supersedes the earlier one, that a mutating command invalidates
every resident observation and folds at turn end, and that the snapshot hook
keeps the session's cwd resident after the folds.
"""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from kernel.capability_binding import ExecutionFloorView
from kernel.context.mcp_catalog import _deserialize_tool
from kernel.history import HistoryLedger, HistoryRecord, resolve_history_keys
from packstest.catalog import _ensure_source_importable, build_tool_catalog
from packstest.context import ToolContextFactory
from packstest.runner import run_tool

from lisa_packs.bash import executor as _executor
from lisa_packs.bash.history import history_snapshot
from lisa_packs.bash.tools.bash_run import bash_run

PACK_PATH = Path(__file__).resolve().parents[4]


@pytest.fixture(autouse=True)
def _fresh_sessions() -> Iterator[None]:
    _executor._reset_for_tests()
    yield
    _executor._reset_for_tests()


@pytest.fixture()
def catalog() -> dict[str, dict[str, object]]:
    _ensure_source_importable(PACK_PATH)
    return build_tool_catalog(PACK_PATH)


class _Driver:
    """Run real bash_run calls and record their claims like the turn runner."""

    def __init__(
        self, catalog: dict[str, dict[str, object]], factory: ToolContextFactory
    ) -> None:
        self.catalog = catalog
        self.factory = factory
        self.ledger = HistoryLedger()

    async def call(self, call_id: str, params: dict[str, Any]) -> None:
        run = await run_tool(self.catalog, "bash_run", self.factory, params)
        assert run.result.error == "", run.result.error
        definition = _deserialize_tool(self.catalog["bash_run"])
        claims = resolve_history_keys(
            definition.history, definition.implementation_module, params, run.result
        )
        assert claims is not None
        self.ledger.record(
            HistoryRecord(
                call_id=call_id,
                tool_id="bash_run",
                owner_id=definition.pack_id,
                keys=list(claims.keys),
                invalidates=list(claims.invalidates),
                scope=claims.scope,
                anchor=claims.anchor,
            )
        )

    def record(self, call_id: str) -> HistoryRecord:
        return next(r for r in self.ledger.records if r.call_id == call_id)


@pytest.mark.eval
@pytest.mark.live
@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not installed")
@pytest.mark.asyncio
async def test_bash_history_claims_fold_only_stale_observations(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    catalog: dict[str, dict[str, object]],
) -> None:
    monkeypatch.setattr(bash_run, "_sandbox_available", lambda: False)
    work = tmp_path / "work"
    work.mkdir()
    (work / "notes.txt").write_text("hello\n", encoding="utf-8")
    factory = ToolContextFactory(
        root=work,
        pack_config={},
        data_dir=tmp_path / "data",
        execution_floor_for=lambda _tool_id: ExecutionFloorView(execution="bare"),
    )
    drive = _Driver(catalog, factory)

    await drive.call("c0", {"command": f"cd {work}"})
    await drive.call("l1", {"command": "ls"})
    await drive.call("n1", {"command": "cat notes.txt"})
    await drive.call("l2", {"command": "ls"})
    assert drive.record("l1").dead
    assert drive.record("l1").stale_reason == "superseded"
    assert drive.record("l1").stale_by == "l2"
    assert not drive.record("n1").dead

    await drive.call("t1", {"command": "touch new.txt"})
    await drive.call("l3", {"command": "ls   "})
    await drive.call("b1", {"commands": ["pwd", "head -1 notes.txt | wc -c"]})

    for call_id in ("n1", "l2"):
        assert drive.record(call_id).dead
        assert drive.record(call_id).stale_reason == "invalidated"
        assert drive.record(call_id).stale_by == "t1"
    assert not drive.record("l3").dead
    assert drive.record("l3").keys == ["bash.obs:-:ls"]

    turn_end = {r.call_id for r in drive.ledger.foldable(turn_finished=True)}
    assert turn_end == {"c0", "l1", "n1", "l2", "t1", "b1"}
    mid_turn = {r.call_id for r in drive.ledger.foldable(turn_finished=False)}
    assert mid_turn == {"l1", "n1", "l2"}

    snapshot = await history_snapshot(factory.context(tool_id="bash_run"), [])
    assert snapshot == f"bash session: cwd={work}; sandboxed=false; running jobs=none"
