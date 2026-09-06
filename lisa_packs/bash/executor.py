"""Bounded command execution in a primed bash session.

Runs the session's primed bash argv with drained/truncated output, parses the
sentinel + return-code marker, and tee's everything to the per-session audit
log. When the bwrap sandbox is available, the same argv is composed through
``bwrap_run`` via a child ToolExecutionContext (bash stays the approval
boundary; the composed path reports its backend transparently).
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Any

from kernel.models import ToolExecutionContext

from lisa_packs.bash.session import BashSession

__all__ = ["run_in_session", "MAX_OUTPUT_BYTES"]

MAX_OUTPUT_BYTES = 512_000

_RC_MARKER = re.compile(r"(?P<sentinel>.+?)rc=(?P<rc>\d+)")


async def _drain_limited(
    stream: asyncio.StreamReader,
    *,
    limit: int,
) -> tuple[bytes, bool]:
    retained = bytearray()
    overflowed = False
    while chunk := await stream.read(8192):
        remaining = limit - len(retained)
        if remaining > 0:
            retained.extend(chunk[:remaining])
        if len(chunk) > remaining:
            overflowed = True
    return bytes(retained), overflowed


def _parse_output(raw: str, sentinel: str) -> tuple[str, int | None]:
    """Strip the sentinel prompt + parse the rc marker from raw output."""
    return_code: int | None = None
    lines: list[str] = []
    for line in raw.splitlines():
        match = _RC_MARKER.match(line)
        if match and sentinel.strip() in match.group("sentinel"):
            return_code = int(match.group("rc"))
            continue
        # Drop prompt echo lines that are just the sentinel.
        if line.strip() == sentinel.strip():
            continue
        lines.append(line)
    # Remove the echoed command's trailing prompt noise.
    body = "\n".join(lines)
    # A trailing lone rc marker without newline (prompt after last command).
    body = re.sub(re.escape(sentinel.strip()) + r"rc=\d+\s*$", "", body)
    body = body.replace(sentinel, "")
    return body.strip("\n"), return_code


def _append_log(log_path: Path, command: str, result: dict[str, Any]) -> None:
    """Append command + output to the user-visible audit log."""
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(f"$ {command}\n")
            out = result.get("stdout", "")
            if out:
                handle.write(out + ("" if out.endswith("\n") else "\n"))
            err = result.get("stderr", "")
            if err:
                handle.write("[stderr] " + err.replace("\n", "\n[stderr] ") + "\n")
            handle.write(
                f"[rc={result.get('return_code')} timed_out={result.get('timed_out')} "
                f"sandboxed={result.get('sandboxed')}]\n"
            )
    except OSError:
        # Logging must never fail the tool call.
        return


async def _run_process(argv: list[str], *, timeout_seconds: int) -> dict[str, Any]:
    process = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    assert process.stdout is not None
    assert process.stderr is not None
    stdout_task = asyncio.create_task(_drain_limited(process.stdout, limit=MAX_OUTPUT_BYTES))
    stderr_task = asyncio.create_task(_drain_limited(process.stderr, limit=MAX_OUTPUT_BYTES))
    timed_out = False
    try:
        await asyncio.wait_for(process.wait(), timeout=timeout_seconds)
    except TimeoutError:
        timed_out = True
        process.kill()
        await process.wait()
    stdout, stdout_over = await stdout_task
    stderr, stderr_over = await stderr_task
    return {
        "stdout_raw": stdout.decode("utf-8", errors="replace"),
        "stderr": stderr.decode("utf-8", errors="replace"),
        "timed_out": timed_out,
        "truncated": stdout_over or stderr_over,
        "process_rc": process.returncode,
    }


async def _run_via_bwrap(
    ctx: ToolExecutionContext,
    inner_argv: list[str],
    *,
    timeout_seconds: int,
    overrides: dict[str, Any],
    persist: bool,
    layer_id: str,
) -> dict[str, Any]:
    """Compose the command through the bwrap_run tool via a child ctx.

    Network/device/path containment is decided by the harness floor inside
    bwrap_run (read from ``pack_config``); bash forwards the model's tightening
    ``overrides`` and the floor does the rest - the model can only raise
    integrity, never loosen it.
    """
    from kernel.tools.registry import execute_tool

    child = ToolExecutionContext(
        tool_id="bwrap_run",
        workspace_id=ctx.workspace_id,
        user_id=ctx.user_id,
        user_slug=ctx.user_slug,
        surface_type=ctx.surface_type,
        surface_id=ctx.surface_id,
        memory_scope=ctx.memory_scope,
        mode_id=ctx.mode_id,
        query=ctx.query,
        parameters={
            "argv": inner_argv,
            "overrides": overrides,
            "timeout_seconds": timeout_seconds,
            "persist": persist,
            "layer_id": layer_id,
        },
        data_dir=ctx.data_dir,
        pack_config=ctx.pack_config,
        approval_service=ctx.approval_service,
        approval_request_id=ctx.approval_request_id,
        session_plan_digest=ctx.session_plan_digest,
        event_bus=ctx.event_bus,
        state_provider=ctx.state_provider,
    )
    tool_result = await execute_tool("bwrap_run", child)
    payload = dict(tool_result.output_json or {})
    if tool_result.error:
        return {
            "error": tool_result.error,
            "stdout_raw": "",
            "stderr": payload.get("stderr", ""),
            "timed_out": payload.get("timed_out", False),
            "truncated": payload.get("truncated", False),
            "backend": payload.get("backend"),
            "persistence": payload.get("persistence"),
            "degraded": payload.get("degraded", False),
        }
    payload["stdout_raw"] = tool_result.output_text or ""
    return payload


async def run_in_session(
    ctx: ToolExecutionContext,
    session: BashSession,
    command: str,
    *,
    timeout_seconds: int,
    overrides: dict[str, Any] | None = None,
    persist: bool = False,
    layer_id: str = "",
) -> dict[str, Any]:
    """Run a command in the session, composing through bwrap when available."""
    inner_argv = session.build_inner_argv(command)

    try:
        if session.sandboxed:
            payload = await _run_via_bwrap(
                ctx,
                inner_argv,
                timeout_seconds=max(timeout_seconds, 1) + 30,
                overrides=overrides or {},
                persist=persist,
                layer_id=layer_id,
            )
            if "error" in payload and payload["error"]:
                # The sandbox itself reported a failure (e.g. bwrap missing on
                # this host): surface it with the sandbox stamp and any reason
                # the child provided so the model can relay the fix.
                error_result: dict[str, Any] = {
                    "error": f"sandbox execution failed: {payload['error']}",
                    "sandboxed": True,
                }
                if payload.get("missing"):
                    error_result["missing"] = payload["missing"]
                    error_result["error"] += (
                        ". Missing host prerequisites: " + "; ".join(payload["missing"])
                    )
                return error_result
            stdout_body, rc = _parse_output(payload.get("stdout_raw", ""), session.sentinel)
            result = {
                "stdout": stdout_body,
                "stderr": payload.get("stderr", ""),
                "return_code": payload.get("return_code", rc if rc is not None else 0),
                "timed_out": payload.get("timed_out", False),
                "truncated": payload.get("truncated", False),
                "sandboxed": True,
                "backend": payload.get("backend"),
                "layer_id": payload.get("layer_id"),
                "persistence": payload.get("persistence"),
                "capture_root": payload.get("capture_root"),
                "scratch_dir": payload.get("scratch_dir"),
            }
            if payload.get("degraded"):
                result["degraded"] = True
                result["note"] = payload.get("note")
        else:
            proc = await _run_process(inner_argv, timeout_seconds=timeout_seconds)
            stdout_body, rc = _parse_output(proc["stdout_raw"], session.sentinel)
            if rc is None:
                # No rc marker parsed (e.g. truncated output or timeout): fall
                # back to the process exit code rather than assuming success.
                rc = proc.get("process_rc")
            result = {
                "stdout": stdout_body,
                "stderr": proc["stderr"],
                "return_code": rc if rc is not None else 0,
                "timed_out": proc["timed_out"],
                "truncated": proc["truncated"],
                "sandboxed": False,
            }
    except Exception as exc:
        # Covers both the sandbox path (bwrap_run raised through the
        # passthrough, e.g. bwrap missing on the host) and direct host
        # execution. Never fail silently: the stamp tells the model whether
        # the failure happened inside or outside the sandbox.
        return {
            "error": f"bash execution failed: {exc}",
            "sandboxed": session.sandboxed,
        }

    _append_log(session.log_path, command, result)
    return result


def _reset_for_tests() -> None:
    from lisa_packs.bash import session as _session_module

    _session_module._reset_sessions()
