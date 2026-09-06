"""Sandbox-facts prewarm operation (shell mode pre-turn).

Runs before every shell-mode model call (bound via the mode's
``pre_turn_operation_ids``). It resolves — creating if needed — the bash session
for this context identity, which both primes the session and publishes the
per-session sandbox containment fact to ``<data_dir>/sandbox-facts/<key>.json``.

That pre-warm matters: without it, the first ``bash_run`` call would hit the
approval gate *before* any session existed, so the policy engine's
sandbox-boundary check would read a missing fact and prompt even though the
command would have run sandboxed. Resolving here closes that inversion so the
very first gate sees the true containment state. The pack only reports the
fact; the host approval engine owns the approve/ask/deny decision.

Not wired as ``entry_operation_ids``: the kernel's work-entry-op contract
ends the turn with the entry result's ``output_text`` before any model call,
so an entry-op binding turned every shell turn into the literal prewarm
string ("shell session ready (contained)") and swallowed the operator's
query. Pre-turn binding keeps the same session priming and fact publication
while the turn proceeds to the model normally.
"""

from __future__ import annotations

import json

from kernel.models import (
    OperationExecutionContext,
    OperationResult,
    ToolExecutionContext,
)

from lisa_packs.bash.session import SANDBOX_FACTS_DIRNAME, resolve_or_create_session

__all__ = ["execute"]


async def execute(ctx: OperationExecutionContext) -> OperationResult:
    """Resolve the bash session and report its sandbox containment state."""
    tool_ctx = ToolExecutionContext(
        tool_id="bash_run",
        workspace_id=ctx.workspace_id,
        user_id=ctx.user_id,
        user_slug=ctx.user_slug,
        surface_type=ctx.surface_type,
        surface_id=ctx.surface_id,
        memory_scope=ctx.memory_scope,
        mode_id=ctx.mode_id,
        query=ctx.query,
        session_plan_digest=ctx.session_plan_digest,
        data_dir=ctx.data_dir,
    )
    session = resolve_or_create_session(tool_ctx)
    fact_path = session.data_dir / SANDBOX_FACTS_DIRNAME / f"{session.session_key}.json"
    try:
        fact = json.loads(fact_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        fact = {}
    containment = str(fact.get("containment") or "unknown")
    reasons = fact.get("reasons") if isinstance(fact.get("reasons"), list) else []
    return OperationResult(
        output_text=f"shell session ready ({containment})",
        output_json={
            "containment": containment,
            "reasons": reasons,
            "sandboxed": containment == "contained",
            "session_key": session.session_key,
        },
    )
