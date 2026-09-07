"""Persistent, agent-optimized bash session tool for Lisa."""

from __future__ import annotations

from kernel.models import ToolExecutionContext, ToolResult
from pydantic import BaseModel, Field, model_validator

from lisa_packs.bash import read_authored_text
from lisa_packs.bash.executor import MAX_BATCH_COMMANDS, run_batch_in_session, run_in_session
from lisa_packs.bash.session import (
    SandboxRequiredButUnavailable,
    _floor_requires_sandbox,
    _sandbox_available,
    _sandbox_degraded,
    bash_available,
    resolve_or_create_session,
)

TOOL_ID = "bash_run"


class Parameters(BaseModel):
    """Structured input for bash_run."""

    command: str | None = Field(
        default=None,
        min_length=1,
        max_length=10_000,
        description="The shell command to execute. Mutually exclusive with `commands`.",
    )
    commands: list[str] | None = Field(
        default=None,
        description=(
            "Independent shell commands to run as one batch (cap "
            f"{MAX_BATCH_COMMANDS}). Mutually exclusive with `command`. "
            "Prefer this for independent read-only fan-outs; in-shell `&`/`wait` "
            "is still the tool for data-parallel work inside one command. "
            "Batch children share a snapshot of session state; env "
            "deltas are discarded, and cwd is adopted only if every child agrees."
        ),
    )
    when: str = Field(
        default="auto",
        description=(
            "Batch schedule: `auto` (parallel in bare/ephemeral/readonly, "
            "serial otherwise), `parallel`, or `serial`. Overlay capture always "
            "serializes, even if parallel is requested."
        ),
    )
    timeout_seconds: int = Field(default=30, ge=1, le=300)
    run_in_background: bool = Field(
        default=False,
        description=(
            "Run this command in the background and return a job_id immediately, "
            "instead of waiting. Useful for long-running builds, tests, or "
            "servers - the model continues while they run and reads the job's "
            "log/status files later. A timed-out foreground command is "
            "auto-backgrounded (instead of killed) unless it starts with `sleep` "
            "or contains `git`, so work is never silently lost - bare and "
            "sandboxed alike. Not valid with `commands`."
        ),
    )
    persist: bool = Field(
        default=False,
        description=(
            "Keep the captured overlay layer after the session ends (only when "
            "sandboxed). Mutations to the workspace are captured either way; "
            "persist just retains the layer beyond the session."
        ),
    )
    layer_id: str = Field(
        default="",
        description=(
            "Explicit sandbox overlay layer id: opt in to capturing workspace "
            "changes (only when sandboxed)."
        ),
    )
    overrides: dict = Field(
        default_factory=dict,
        description=(
            "Tightening overrides for the sandbox (only when sandboxed), keyed "
            "by axis: persistence (ephemeral|overlay|readonly), network "
            "(allow|deny), paths ({path: rw|ro|hidden}). Overrides may only "
            "raise integrity above the harness floor, never loosen it."
        ),
    )

    @model_validator(mode="after")
    def _exclusive_command_or_commands(self) -> "Parameters":
        has_command = bool(self.command)
        has_commands = bool(self.commands)
        if has_command == has_commands:
            raise ValueError("provide exactly one of `command` or `commands`")
        if self.commands is not None:
            if self.run_in_background:
                raise ValueError("`run_in_background` is not valid with `commands`")
            if len(self.commands) > MAX_BATCH_COMMANDS:
                raise ValueError(
                    f"commands is capped at {MAX_BATCH_COMMANDS} "
                    f"(got {len(self.commands)})"
                )
            if any(not isinstance(item, str) or not item.strip() for item in self.commands):
                raise ValueError("every item in `commands` must be a non-empty string")
        if self.when not in ("auto", "parallel", "serial"):
            raise ValueError("`when` must be auto, parallel, or serial")
        return self


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

    available = _sandbox_available()
    degraded_reasons = _sandbox_degraded() if available else []
    requires = _floor_requires_sandbox(ctx)
    sandboxed = available
    deliberate_bare = False
    if requires is True:
        if not available:
            exc = SandboxRequiredButUnavailable(["sandbox pack not installed"])
            return ToolResult(
                tool_id=TOOL_ID,
                error=str(exc),
                output_json={
                    "sandboxed": False,
                    "missing": exc.reasons,
                    "doc": read_authored_text("prompts/bwrap_missing.md"),
                },
            )
        if degraded_reasons:
            exc = SandboxRequiredButUnavailable(degraded_reasons)
            return ToolResult(
                tool_id=TOOL_ID,
                error=str(exc),
                output_json={
                    "sandboxed": False,
                    "missing": degraded_reasons,
                    "doc": read_authored_text("prompts/bwrap_missing.md"),
                },
            )
    elif requires is False and not available:
        # The floor deliberately permits bare execution (e.g. on a host without
        # bwrap): run bare by choice, stamped distinctly from a broken sandbox.
        sandboxed = False
        deliberate_bare = True

    session = resolve_or_create_session(ctx, sandboxed=sandboxed)
    degraded_doc = ""
    if not sandboxed and not deliberate_bare:
        degraded_doc = read_authored_text("prompts/bash_no_sandbox.md")
    elif degraded_reasons:
        degraded_doc = read_authored_text("prompts/bwrap_missing.md")

    try:
        if params.commands is not None:
            result = await run_batch_in_session(
                ctx,
                session,
                params.commands,
                timeout_seconds=params.timeout_seconds,
                when=params.when,
                overrides=params.overrides,
                persist=params.persist,
                layer_id=params.layer_id,
            )
        else:
            result = await run_in_session(
                ctx,
                session,
                params.command or "",
                timeout_seconds=params.timeout_seconds,
                run_in_background=params.run_in_background,
                overrides=params.overrides,
                persist=params.persist,
                layer_id=params.layer_id,
            )
    except Exception as exc:
        return ToolResult(
            tool_id=TOOL_ID,
            error=f"bash_run failed: {exc}",
            output_json={
                "sandboxed": sandboxed,
                "missing": degraded_reasons,
                **({"doc": degraded_doc} if degraded_doc else {}),
            },
        )

    if result.get("error"):
        output_json = {"sandboxed": result.get("sandboxed", sandboxed)}
        if result.get("missing"):
            output_json["missing"] = result["missing"]
        elif degraded_reasons:
            output_json["missing"] = degraded_reasons
        if degraded_doc:
            output_json["doc"] = degraded_doc
        return ToolResult(
            tool_id=TOOL_ID,
            error=str(result["error"]),
            output_json=output_json,
        )

    # Backgrounded short-circuit: return the job descriptor as output_json.
    if result.get("backgrounded"):
        output_json = {
            "backgrounded": True,
            "job_id": result["job_id"],
            "log_path": result["log_path"],
            "rc_path": result["rc_path"],
            "status_path": result.get("status_path"),
            "status": "running",
            "sandboxed": result.get("sandboxed", sandboxed),
        }
        if result.get("background_jobs"):
            output_json["background_jobs"] = result["background_jobs"]
        if result.get("auto_backgrounded_on_timeout"):
            output_json["auto_backgrounded_on_timeout"] = True
            output_json["note"] = (
                f"Command did not complete within {result['timeout_seconds']}s "
                "and was moved to the background. Poll rc_path / log_path for "
                "completion; the rc file will contain the exit code when done."
            )
        else:
            output_json["note"] = (
                "Running in the background. Poll rc_path / log_path for "
                "completion; the rc file will contain the exit code when done."
            )
        return ToolResult(
            tool_id=TOOL_ID,
            output_text="",
            output_json=output_json,
        )

    if result.get("batch"):
        output_json = {
            "batch": True,
            "when": result.get("when"),
            "requested_when": result.get("requested_when"),
            "results": result.get("results") or [],
            "return_code": result.get("return_code", 0),
            "sandboxed": result.get("sandboxed", sandboxed),
            "timed_out": result.get("timed_out", False),
            "truncated": result.get("truncated", False),
            "log_path": str(session.log_path),
        }
        if result.get("reason"):
            output_json["reason"] = result["reason"]
        if result.get("note"):
            output_json["shaping_note"] = result["note"]
        if result.get("background_jobs"):
            output_json["background_jobs"] = result["background_jobs"]
        if deliberate_bare:
            output_json["mode"] = "bare"
            output_json["note"] = (
                "Running unsandboxed on the host because the sandbox floor for this "
                "session permits bare execution. Changes are permanent - confirm "
                "before mutating."
            )
        elif not sandboxed:
            output_json["note"] = (
                "bwrap pack not installed: running unsandboxed on the host. "
                "See sandboxing_degraded.md to enable the sandbox."
            )
        return ToolResult(
            tool_id=TOOL_ID,
            output_text="",
            output_json=output_json,
        )

    output_json = {
        "return_code": result["return_code"],
        "stderr": result["stderr"],
        "sandboxed": result["sandboxed"],
        "timed_out": result["timed_out"],
        "truncated": result["truncated"],
        "log_path": str(session.log_path),
    }
    if result.get("background_jobs"):
        output_json["background_jobs"] = result["background_jobs"]
    # Spill-to-file: surfaced when stdout/stderr were truncated, giving the
    # model a stable path to inspect rather than re-running the command.
    for spill_key in ("stdout_path", "stderr_path", "stdout_bytes", "stderr_bytes"):
        if result.get(spill_key):
            output_json[spill_key] = result[spill_key]
    # Shaping: pipe-stage failures and benign informational conditions the
    # model must not mistake for a hard failure.
    if result.get("pipe_statuses"):
        output_json["pipe_statuses"] = result["pipe_statuses"]
    if result.get("backend"):
        output_json["backend"] = result["backend"]
        output_json["layer_id"] = result.get("layer_id")
        output_json["persistence"] = result.get("persistence")
        if result.get("capture_root"):
            output_json["capture_root"] = result["capture_root"]
        if result.get("scratch_dir"):
            output_json["scratch_dir"] = result["scratch_dir"]
    if result.get("degraded"):
        output_json["degraded"] = True
        output_json["note"] = result.get("note")
    if result.get("note"):
        # Shaping note (benign rc=1, pipe-stage failure, empty output) goes on
        # its own field; `note` is reserved for sandbox/posture messages.
        output_json["shaping_note"] = result["note"]
    if deliberate_bare:
        output_json["mode"] = "bare"
        output_json["note"] = (
            "Running unsandboxed on the host because the sandbox floor for this "
            "session permits bare execution. Changes are permanent - confirm "
            "before mutating."
        )
    elif not sandboxed:
        output_json["note"] = (
            "bwrap pack not installed: running unsandboxed on the host. "
            "See sandboxing_degraded.md to enable the sandbox."
        )
    elif degraded_reasons:
        # Pack registered but host prerequisites missing: surface loudly on
        # every result, never silently. bwrap_run re-probes per call and
        # reports hard failures explicitly; this is the persistent heads-up.
        output_json["degraded"] = True
        output_json["sandbox_missing"] = degraded_reasons
        output_json["note"] = (
            "Sandbox prerequisites missing on this host: "
            + "; ".join(degraded_reasons)
            + ". See bwrap_missing.md to fix it."
        )

    return ToolResult(
        tool_id=TOOL_ID,
        output_text=result["stdout"],
        output_json=output_json,
    )
