"""Persistent, agent-optimized bash session tool for Lisa."""

from __future__ import annotations

from kernel.models import ToolExecutionContext, ToolResult
from pydantic import BaseModel, Field

from lisa_packs.bash import read_authored_text
from lisa_packs.bash.executor import run_in_session
from lisa_packs.bash.session import _sandbox_available, bash_available, resolve_or_create_session

TOOL_ID = "bash_run"


class Parameters(BaseModel):
    """Structured input for bash_run."""

    command: str = Field(..., min_length=1, max_length=10_000,
                         description="The shell command to execute.")
    timeout_seconds: int = Field(default=30, ge=1, le=300)
    persist: bool = Field(
        default=False,
        description="Keep the sandbox overlay layer after this run (only when sandboxed).",
    )
    layer_id: str = Field(
        default="",
        description="Explicit sandbox overlay layer id (only when sandboxed).",
    )
    network: bool = Field(
        default=False,
        description="Allow network access (only when sandboxed).",
    )


async def execute(ctx: ToolExecutionContext) -> ToolResult:
    """Execute a command in the persistent, sandbox-aware bash session."""
    try:
        params = Parameters.model_validate(ctx.parameters)
    except Exception as exc:
        return ToolResult(tool_id=TOOL_ID, error=f"Invalid parameters: {exc}")

    if not bash_available():
        install_doc = read_authored_text("prompts/bash_missing.md")
        return ToolResult(
            tool_id=TOOL_ID,
            error="bash is not installed or not on PATH; cannot start a shell session.",
            output_json={"missing": ["bash"], "doc": install_doc},
        )

    sandboxed = _sandbox_available()
    session = resolve_or_create_session(ctx, sandboxed=sandboxed)
    degraded_doc = (
        read_authored_text("prompts/bash_no_sandbox.md") if not sandboxed else ""
    )

    try:
        result = await run_in_session(
            ctx,
            session,
            params.command,
            timeout_seconds=params.timeout_seconds,
            network=params.network,
            persist=params.persist,
            layer_id=params.layer_id,
        )
    except Exception as exc:
        return ToolResult(tool_id=TOOL_ID, error=f"bash_run failed: {exc}")

    if result.get("error"):
        return ToolResult(
            tool_id=TOOL_ID,
            error=str(result["error"]),
            output_json={"sandboxed": result.get("sandboxed", sandboxed)},
        )

    output_json = {
        "return_code": result["return_code"],
        "stderr": result["stderr"],
        "sandboxed": result["sandboxed"],
        "timed_out": result["timed_out"],
        "truncated": result["truncated"],
        "log_path": str(session.log_path),
    }
    if result.get("backend"):
        output_json["backend"] = result["backend"]
        output_json["layer_id"] = result.get("layer_id")
        output_json["persistence"] = result.get("persistence")
    if result.get("degraded"):
        output_json["degraded"] = True
        output_json["note"] = result.get("note")
    if not sandboxed:
        output_json["note"] = (
            "bwrap pack not installed: running unsandboxed on the host. "
            "See sandboxing_degraded.md to enable the sandbox."
        )

    return ToolResult(
        tool_id=TOOL_ID,
        output_text=result["stdout"],
        output_json=output_json,
    )
