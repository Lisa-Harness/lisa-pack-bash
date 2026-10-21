"""Persistent, agent-optimized bash session tool for Lisa."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Mapping
from dataclasses import dataclass
from typing import Any

from kernel.models import ToolExecutionContext, ToolResult
from pydantic import BaseModel, Field, field_validator, model_validator

from lisa_packs.bash import read_authored_text
from lisa_packs.bash.executor import (
    MAX_BATCH_COMMANDS,
    run_batch_in_session,
    run_in_session,
)
from lisa_packs.bash.session import (
    BashSession,
    SandboxRequiredButUnavailable,
    _floor_requires_sandbox,
    _sandbox_available,
    _sandbox_degraded,
    bash_available,
    resolve_or_create_session,
)

TOOL_ID = "bash_run"

#: Override axes that count as a tightening request (opting into the sandbox
#: under a bare floor). A request outside these keys is not an integrity raise.
_TIGHTENING_OVERRIDE_KEYS = ("persistence", "network", "device", "ipc", "paths")


def _wants_sandbox(params: Parameters) -> bool:
    """True when the model asked for containment above the bare floor.

    A bare floor is a PERMISSION, not a mandate: without an explicit
    tightening request the shell runs on the host; ``persist``/``layer_id``
    or any sandbox-axis ``overrides`` entry composes through the sandbox
    instead. Model parameters are untrusted, so this never WEAKENS the floor
    - it only chooses between two postures the floor already permits.
    """
    if params.persist or params.layer_id:
        return True
    overrides = params.overrides
    return isinstance(overrides, Mapping) and any(
        key in overrides for key in _TIGHTENING_OVERRIDE_KEYS
    )


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

    @field_validator("commands", mode="before")
    @classmethod
    def _accept_json_list_string(cls, value: object) -> object:
        """Accept a JSON-encoded list for ``commands`` (lenient model fix).

        Under a JSON-mode (strict=False) tool schema weaker models
        sometimes emit a list-valued parameter as a STRING containing the
        JSON array instead of a real array. Observed in production on
        2026-10-18 (four consecutive identical validation errors in one
        turn: ``Input should be a valid list``, with the raw JSON payload
        as ``input_value``); the model's retries reproduced the same
        shape each time because nothing in the error told it why the
        shape was wrong. Coercing a string that parses to a list of
        strings into a real list is model-forgiving and cannot mask a
        genuinely wrong request: a non-JSON string or a JSON
        non-list falls through to the standard list_type error.
        """
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
            except json.JSONDecodeError:
                return value
            return parsed
        return value

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
    overrides: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Tightening overrides for the sandbox, keyed by axis: persistence "
            "(direct-write|overlay|ephemeral|readonly), network (allow|deny), "
            "device (dev|minimal|none), ipc (shared|unshared), paths "
            "({path: rw|capture|ro|hidden}). `capture` stages writes for that "
            "path into the overlay layer; it requires the overlay posture. "
            "Overrides may only raise integrity above the harness floor, "
            "never loosen it. Under a bare floor, ANY override here (or "
            "persist/layer_id) opts this call into the sandbox; with no "
            "overrides the call runs on the host."
        ),
    )

    @model_validator(mode="after")
    def _exclusive_command_or_commands(self) -> Parameters:
        has_command = bool(self.command)
        has_commands = bool(self.commands)
        if has_command == has_commands:
            raise ValueError("provide exactly one of `command` or `commands`")
        if self.commands is not None:
            if self.run_in_background:
                raise ValueError("`run_in_background` is not valid with `commands`")
            if len(self.commands) > MAX_BATCH_COMMANDS:
                raise ValueError(
                    f"commands is capped at {MAX_BATCH_COMMANDS} (got {len(self.commands)})"
                )
            if any(not isinstance(item, str) or not item.strip() for item in self.commands):
                raise ValueError("every item in `commands` must be a non-empty string")
        if self.when not in ("auto", "parallel", "serial"):
            raise ValueError("`when` must be auto, parallel, or serial")
        return self


@dataclass(slots=True)
class _Posture:
    """The sandbox posture decided for one call.

    ``sandboxed`` is whether the call composes through bwrap;
    ``deliberate_bare`` distinguishes "the floor chose bare" from "the
    sandbox is broken" (a degraded posture).
    """

    sandboxed: bool
    deliberate_bare: bool
    degraded_reasons: list[str]


def _missing_sandbox_result(reasons: list[str]) -> ToolResult:
    """The hard error returned when the floor mandates a sandbox that cannot run."""
    exc = SandboxRequiredButUnavailable(reasons)
    return ToolResult(
        tool_id=TOOL_ID,
        error=str(exc),
        output_json={
            "sandboxed": False,
            "missing": exc.reasons,
            "doc": read_authored_text("prompts/bwrap_missing.md"),
        },
    )


def _decide_posture(ctx: ToolExecutionContext, params: Parameters) -> _Posture | ToolResult:
    """Decide the sandbox posture for this call.

    Returns a :class:`_Posture` when the call may proceed, or the hard-error
    :class:`ToolResult` the floor demands when the sandbox is required but
    unavailable.
    """
    available = _sandbox_available()
    degraded_reasons = _sandbox_degraded() if available else []
    requires = _floor_requires_sandbox(ctx)
    if requires is True:
        if not available:
            return _missing_sandbox_result(["sandbox pack not installed"])
        if degraded_reasons:
            return _missing_sandbox_result(degraded_reasons)
        return _Posture(sandboxed=True, deliberate_bare=False, degraded_reasons=degraded_reasons)
    if not available:
        # The floor deliberately permits bare execution (e.g. on a host without
        # bwrap): run bare by choice, stamped distinctly from a broken sandbox.
        return _Posture(sandboxed=False, deliberate_bare=True, degraded_reasons=degraded_reasons)
    if not _wants_sandbox(params):
        # Floor bare + healthy sandbox + no tightening request: bare is the
        # harness's chosen posture, not a fallback. The model can still opt
        # into containment per call via persist/layer_id/overrides.
        return _Posture(sandboxed=False, deliberate_bare=True, degraded_reasons=degraded_reasons)
    return _Posture(sandboxed=True, deliberate_bare=False, degraded_reasons=degraded_reasons)


def _degraded_doc(posture: _Posture) -> str:
    """The authored doc that explains this call's degraded posture."""
    if not posture.sandboxed and not posture.deliberate_bare:
        return read_authored_text("prompts/bash_no_sandbox.md")
    if posture.degraded_reasons:
        return read_authored_text("prompts/bwrap_missing.md")
    return ""


def _run_params(
    ctx: ToolExecutionContext,
    session: BashSession,
    params: Parameters,
) -> Awaitable[dict[str, Any]]:
    """Dispatch the validated parameters to the single-command or batch path."""
    if params.commands is not None:
        return run_batch_in_session(
            ctx,
            session,
            params.commands,
            timeout_seconds=params.timeout_seconds,
            when=params.when,
            overrides=params.overrides,
            persist=params.persist,
            layer_id=params.layer_id,
        )
    return run_in_session(
        ctx,
        session,
        params.command or "",
        timeout_seconds=params.timeout_seconds,
        run_in_background=params.run_in_background,
        overrides=params.overrides,
        persist=params.persist,
        layer_id=params.layer_id,
    )


def _backgrounded_output_json(
    result: dict[str, Any],
    *,
    sandboxed: bool,
) -> dict[str, Any]:
    """The output_json for a backgrounded job descriptor."""
    output_json: dict[str, Any] = {
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
    return output_json


def _batch_output_json(
    result: dict[str, Any],
    *,
    sandboxed: bool,
    deliberate_bare: bool,
    log_path: str,
) -> dict[str, Any]:
    """The output_json for a batch result envelope."""
    output_json: dict[str, Any] = {
        "batch": True,
        "when": result.get("when"),
        "requested_when": result.get("requested_when"),
        "results": result.get("results") or [],
        "return_code": result.get("return_code", 0),
        "sandboxed": result.get("sandboxed", sandboxed),
        "timed_out": result.get("timed_out", False),
        "truncated": result.get("truncated", False),
        "log_path": log_path,
    }
    if result.get("reason"):
        output_json["reason"] = result["reason"]
    if result.get("note"):
        output_json["shaping_note"] = result["note"]
    if result.get("background_jobs"):
        output_json["background_jobs"] = result["background_jobs"]
    output_json.update(_posture_output_json(sandboxed=sandboxed, deliberate_bare=deliberate_bare))
    return output_json


def _posture_output_json(*, sandboxed: bool, deliberate_bare: bool) -> dict[str, Any]:
    """The posture fields appended to every result.

    ``note`` is reserved for sandbox/posture messages; the shaping note
    lives on its own ``shaping_note`` field.
    """
    if deliberate_bare:
        return {
            "mode": "bare",
            "note": (
                "Running unsandboxed on the host because the sandbox floor for this "
                "session permits bare execution. Changes are permanent - confirm "
                "before mutating."
            ),
        }
    if not sandboxed:
        return {
            "note": (
                "bwrap pack not installed: running unsandboxed on the host. "
                "See sandboxing_degraded.md to enable the sandbox."
            )
        }
    return {}


def _sandbox_detail_output_json(result: dict[str, Any]) -> dict[str, Any]:
    """The sandbox-specific detail fields of a single-command result."""
    detail: dict[str, Any] = {}
    if result.get("backend"):
        detail["backend"] = result["backend"]
        detail["layer_id"] = result.get("layer_id")
        detail["persistence"] = result.get("persistence")
        if result.get("capture_root"):
            detail["capture_root"] = result["capture_root"]
        if result.get("scratch_dir"):
            detail["scratch_dir"] = result["scratch_dir"]
        if result.get("identity"):
            detail["identity"] = result["identity"]
    if result.get("degraded"):
        detail["degraded"] = True
        detail["note"] = result.get("note")
    return detail


def _command_output_json(
    result: dict[str, Any],
    *,
    sandboxed: bool,
    deliberate_bare: bool,
    degraded_reasons: list[str],
    log_path: str,
) -> dict[str, Any]:
    """The output_json for a single-command result."""
    output_json: dict[str, Any] = {
        "return_code": result["return_code"],
        "stderr": result["stderr"],
        "sandboxed": result["sandboxed"],
        "timed_out": result["timed_out"],
        "truncated": result["truncated"],
        "log_path": log_path,
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
    output_json.update(_sandbox_detail_output_json(result))
    if result.get("note"):
        # Shaping note (benign rc=1, pipe-stage failure, empty output) goes on
        # its own field; `note` is reserved for sandbox/posture messages.
        output_json["shaping_note"] = result["note"]
    output_json.update(_posture_output_json(sandboxed=sandboxed, deliberate_bare=deliberate_bare))
    if degraded_reasons and not deliberate_bare and sandboxed:
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
    return output_json


def _result_output_json(
    result: dict[str, Any],
    *,
    sandboxed: bool,
    deliberate_bare: bool,
    degraded_reasons: list[str],
    log_path: str,
) -> dict[str, Any]:
    """The output_json for a successful (non-error) command or batch result."""
    if result.get("backgrounded"):
        return _backgrounded_output_json(result, sandboxed=sandboxed)
    if result.get("batch"):
        return _batch_output_json(
            result,
            sandboxed=sandboxed,
            deliberate_bare=deliberate_bare,
            log_path=log_path,
        )
    return _command_output_json(
        result,
        sandboxed=sandboxed,
        deliberate_bare=deliberate_bare,
        degraded_reasons=degraded_reasons,
        log_path=log_path,
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

    decided = _decide_posture(ctx, params)
    if isinstance(decided, ToolResult):
        return decided
    posture = decided
    sandboxed = posture.sandboxed
    deliberate_bare = posture.deliberate_bare
    degraded_reasons = posture.degraded_reasons

    session = resolve_or_create_session(ctx, sandboxed=sandboxed)
    degraded_doc = _degraded_doc(posture)

    try:
        result = await _run_params(ctx, session, params)
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

    return ToolResult(
        tool_id=TOOL_ID,
        output_text=result.get("stdout", ""),
        output_json=_result_output_json(
            result,
            sandboxed=sandboxed,
            deliberate_bare=deliberate_bare,
            degraded_reasons=degraded_reasons,
            log_path=str(session.log_path),
        ),
    )
