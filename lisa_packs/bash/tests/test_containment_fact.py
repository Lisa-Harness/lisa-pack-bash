"""Containment-fact publication tests (sandbox boundary → approval engine).

The bash pack reports its per-session sandbox containment state as a neutral
fact file for the host approval engine. These tests cover the three states
(contained / degraded / none) against ``resolve_or_create_session`` and the
entry operation's pre-warm path, asserting the file lands at
``<data_dir>/sandbox-facts/<session_key>.json`` and fails closed correctly.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path

import pytest
from kernel.models import OperationExecutionContext, ToolExecutionContext

from lisa_packs.bash import session as _session
from lisa_packs.bash.operations.sandbox_facts import sandbox_facts as prewarm_op
from lisa_packs.bash.session import SANDBOX_FACTS_DIRNAME, resolve_or_create_session


def _ctx(tmp_path: Path, pack_config: dict | None = None) -> ToolExecutionContext:
    return ToolExecutionContext(
        tool_id="bash_run",
        workspace_id="ws",
        user_slug="u",
        surface_id="surf",
        session_plan_digest="d",
        parameters={"command": "echo hi"},
        data_dir=str(tmp_path),
        pack_config=pack_config or {},
    )


@pytest.fixture(autouse=True)
def _fresh_sessions():
    _session._reset_sessions()
    yield
    _session._reset_sessions()


def _read_fact(tmp_path: Path, session) -> dict:
    path = tmp_path / SANDBOX_FACTS_DIRNAME / f"{session.session_key}.json"
    assert path.is_file(), f"containment fact not written at {path}"
    return json.loads(path.read_text(encoding="utf-8"))


def test_contained_state_published_when_sandboxed(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(_session, "_sandbox_available", lambda: True)
    monkeypatch.setattr(_session, "_sandbox_degraded", list)
    session = resolve_or_create_session(_ctx(tmp_path), sandboxed=True)
    fact = _read_fact(tmp_path, session)
    assert fact["containment"] == "contained"
    assert fact["tool_id"] == "bash_run"
    assert fact["reasons"] == []
    assert fact["probed_at"]


def test_degraded_state_published_with_reasons(monkeypatch, tmp_path: Path) -> None:
    """Registered-but-unrunnable sandbox -> degraded with reasons to surface."""
    monkeypatch.setattr(_session, "_sandbox_available", lambda: True)
    monkeypatch.setattr(
        _session,
        "_sandbox_degraded",
        lambda: ["bubblewrap (bwrap) binary not found on PATH"],
    )
    session = resolve_or_create_session(_ctx(tmp_path), sandboxed=False)
    fact = _read_fact(tmp_path, session)
    assert fact["containment"] == "degraded"
    assert any("bwrap" in r for r in fact["reasons"])


def test_none_state_published_when_no_sandbox(monkeypatch, tmp_path: Path) -> None:
    """No sandbox pack at all -> none (fail-closed: the engine must prompt)."""
    monkeypatch.setattr(_session, "_sandbox_available", lambda: False)
    monkeypatch.setattr(_session, "_sandbox_degraded", list)
    session = resolve_or_create_session(_ctx(tmp_path), sandboxed=False)
    fact = _read_fact(tmp_path, session)
    assert fact["containment"] == "none"
    assert fact["reasons"]


def test_deliberate_bare_published_when_floor_permits_bare(
    monkeypatch, tmp_path: Path
) -> None:
    """Bare floor + healthy sandbox -> deliberate_bare, NOT contained.

    The engine must never auto-approve host execution off a stale
    ``contained`` fact when the harness floor deliberately chose bare
    (fail closed: deliberate_bare is not a _CONTAINED_STATES member).
    """
    monkeypatch.setattr(_session, "_sandbox_available", lambda: True)
    monkeypatch.setattr(_session, "_sandbox_degraded", list)
    ctx = _ctx(tmp_path, pack_config={"sandbox": {"floor": {"execution": "bare"}}})
    session = resolve_or_create_session(ctx, sandboxed=False)
    fact = _read_fact(tmp_path, session)
    assert fact["containment"] == "deliberate_bare"
    assert fact["reasons"] == []


def test_fact_flip_to_deliberate_bare_on_reuse(monkeypatch, tmp_path: Path) -> None:
    """A cached contained session flips to deliberate_bare when the floor changes."""
    monkeypatch.setattr(_session, "_sandbox_available", lambda: True)
    monkeypatch.setattr(_session, "_sandbox_degraded", list)
    no_floor = _ctx(tmp_path)
    session = resolve_or_create_session(no_floor, sandboxed=True)
    assert _read_fact(tmp_path, session)["containment"] == "contained"
    bare = _ctx(tmp_path, pack_config={"sandbox": {"floor": {"execution": "bare"}}})
    again = resolve_or_create_session(bare)  # cached session reuse path
    fact = _read_fact(tmp_path, again)
    assert fact["containment"] == "deliberate_bare"


def test_fact_is_refreshed_on_session_reuse(monkeypatch, tmp_path: Path) -> None:
    """A cached session re-publishes the fact so a mid-session flip is picked up."""
    monkeypatch.setattr(_session, "_sandbox_available", lambda: True)
    monkeypatch.setattr(_session, "_sandbox_degraded", list)
    session = resolve_or_create_session(_ctx(tmp_path), sandboxed=True)
    first = _read_fact(tmp_path, session)["probed_at"]
    # Change the environment: the sandbox can no longer run. The engine must
    # not keep approving off a stale "contained" fact.
    monkeypatch.setattr(_session, "_sandbox_available", lambda: False)
    monkeypatch.setattr(_session, "_sandbox_degraded", list)
    again = resolve_or_create_session(_ctx(tmp_path))  # cached session reuse path
    fact = _read_fact(tmp_path, again)
    assert fact["probed_at"] >= first
    assert fact["containment"] == "none"


def test_session_key_matches_engine_recompute(tmp_path: Path) -> None:
    """The fact path must align with the host engine's recomputed identity key."""
    ctx = _ctx(tmp_path)
    session = resolve_or_create_session(ctx, sandboxed=False)
    # The engine recomputes sha256("ws/u/d/surf")[:16] from the approval identity.
    expected = hashlib.sha256(b"ws/u/d/surf").hexdigest()[:16]
    assert session.session_key == expected
    assert (tmp_path / SANDBOX_FACTS_DIRNAME / f"{expected}.json").is_file()


def test_pre_turn_operation_prewarms_fact(monkeypatch, tmp_path: Path) -> None:
    """The shell pre-turn op resolves the session (writing the fact) before the model call."""
    monkeypatch.setattr(_session, "_sandbox_available", lambda: True)
    monkeypatch.setattr(_session, "_sandbox_degraded", list)
    op_ctx = OperationExecutionContext(
        workspace_id="ws",
        user_slug="u",
        surface_id="surf",
        session_plan_digest="d",
        data_dir=str(tmp_path),
    )
    result = asyncio.run(prewarm_op.execute(op_ctx))
    assert "contained" in result.output_text
    assert result.output_json["containment"] == "contained"
    assert result.output_json["sandboxed"] is True
    # The fact file landed even though bash_run was never invoked.
    assert list((tmp_path / SANDBOX_FACTS_DIRNAME).glob("*.json"))
