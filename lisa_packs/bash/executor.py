"""Bounded command execution in a primed bash session.

Runs the session's primed bash argv with drained/truncated output, parses the
sentinel + return-code marker, and tee's everything to the per-session audit
log. When the bwrap sandbox is available, the same argv is composed through
``bwrap_run`` via a child ToolExecutionContext (bash stays the approval
boundary; the composed path reports its backend transparently).
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import re
import secrets
import shlex
from pathlib import Path
from typing import Any

from kernel.models import ToolExecutionContext
from kernel.tools.output_stream import publish_tool_output

from lisa_packs.bash.session import BashSession

__all__ = [
    "MAX_BATCH_COMMANDS",
    "MAX_OUTPUT_BYTES",
    "ParsedOutput",
    "StreamTap",
    "drain_to_file",
    "is_auto_background_guarded",
    "pipe_failure_note",
    "resolve_batch_schedule",
    "run_batch_in_session",
    "run_in_session",
    "shape_empty_output",
    "tapped_drain_to_file",
]

MAX_OUTPUT_BYTES = 512_000
#: Hard cap for the on-disk spill file; beyond this the stream is dropped.
MAX_SPILL_BYTES = 64 * 1024 * 1024
#: Hard cap for ``commands`` in one bash_run call.
MAX_BATCH_COMMANDS = 8

#: Extra seconds granted to the sandboxed child (bwrap_run) over the model's
#: timeout, so auto-backgrounded sandboxed work keeps running well past the
#: point the model stopped waiting. Capped at bwrap_run's 3600s ceiling.
_AUTO_BG_GRACE_SECONDS = 600
_AUTO_BG_MAX_TIMEOUT_SECONDS = 3600

_RC_MARKER = re.compile(r"^(?P<sentinel>.+?)rc=(?P<rc>\d+)(?:\spipe=(?P<pipe>[0-9 ]+))?$")
_STATE_CWD = re.compile(r"^(?P<sentinel>.+?)state=cwd=(?P<cwd>.*)$")
_STATE_VAR = re.compile(r"^(?P<sentinel>.+?)var (?P<name>[A-Za-z_][A-Za-z0-9_]*)=(?P<value>.*)$")
_STATE_FUNC = re.compile(r"^(?P<sentinel>.+?)func (?P<name>[A-Za-z_][A-Za-z0-9_]*) (?P<b64>[A-Za-z0-9+/=]+)$")
_STATE_FUNCS = re.compile(r"^(?P<sentinel>.+?)funcs (?P<funcs>.*)$")

#: Commands whose exit code 1 is an informational answer, not a failure
#: (SWE-agent/Claude Code finding: a grep with no matches is not an error).
BENIGN_RC1_COMMANDS = frozenset(
    {"grep", "rg", "egrep", "fgrep", "find", "diff", "test", "[", "cmp"}
)
_BENIGN_RC1_PREFIXES = ("git diff", "git grep")


class ParsedOutput:
    """Structured parse of one command's raw stdout."""

    __slots__ = ("body", "pipe_statuses", "return_code", "state")

    def __init__(
        self,
        *,
        body: str,
        return_code: int | None,
        pipe_statuses: list[int] | None = None,
        state: dict[str, Any] | None = None,
    ) -> None:
        self.body = body
        self.return_code = return_code
        self.pipe_statuses = pipe_statuses or []
        self.state = state or {}

    def __iter__(self):
        # Backward-compat: tuple unpack (body, rc) keeps old call sites working.
        yield self.body
        yield self.return_code


class StreamTap:
    """Line-buffered incremental-output forwarder for one subprocess stream.

    Wraps the executor's drain loops: as chunks arrive, complete lines are
    forwarded to the operator surface via ``publish_tool_output`` (the
    ``lisa.tool_output`` bus topic) while the trailing partial line is held
    until the next chunk or stream close. Gated on ``ctx.stream_output`` —
    tools that don't declare the flag (or run headless with no bus) pay only
    the line-splitting cost and publish nothing.

    Deliberately NOT a sentinel filter: the sandboxed path composes through
    ``bwrap_run``, whose output carries the primed prompt + rc marker only at
    the very end; mid-command sentinel echo is rare and harmless on a display
    surface (the parse step still strips it from the model-visible result).
    """

    __slots__ = ("_buffer", "_ctx", "_max_line", "_seq", "_stream")

    def __init__(self, ctx: ToolExecutionContext, *, stream: str) -> None:
        self._ctx = ctx
        self._stream = stream
        self._buffer = ""
        self._seq = 0
        # A pathological single line (e.g. base64 dump) is truncated for the
        # operator view; the full text still reaches the model via ToolResult.
        self._max_line = 2000

    def feed(self, chunk: bytes) -> None:
        """Split ``chunk`` into lines and publish each complete one."""
        if not chunk:
            return
        text = chunk.decode("utf-8", errors="replace")
        self._buffer += text
        while "\n" in self._buffer:
            line, _, rest = self._buffer.partition("\n")
            self._buffer = rest
            self._publish(line)

    def close(self) -> None:
        """Flush the trailing partial line (if any) on stream end."""
        if self._buffer:
            self._publish(self._buffer)
            self._buffer = ""

    def _publish(self, line: str) -> None:
        if len(line) > self._max_line:
            line = line[: self._max_line - 1] + "…"
        publish_tool_output(self._ctx, text=line, stream=self._stream, seq=self._seq)
        self._seq += 1


def _maybe_tap(ctx: ToolExecutionContext, *, stream: str) -> StreamTap | None:
    """Build a :class:`StreamTap` only when streaming is declared and live."""
    if not getattr(ctx, "stream_output", False):
        return None
    bus = getattr(ctx, "event_bus", None)
    if bus is None or not hasattr(bus, "publish"):
        return None
    return StreamTap(ctx, stream=stream)


async def drain_to_file(
    stream: asyncio.StreamReader,
    spill_path: Path,
    *,
    inline_limit: int = MAX_OUTPUT_BYTES,
    spill_limit: int = MAX_SPILL_BYTES,
) -> dict[str, Any]:
    """Drain a stream fully to ``spill_path`` while retaining an inline head.

    Returns a bounded inline prefix plus metadata; the full output lands on
    disk so truncation never loses information (the model inspects the file).
    """
    inline = bytearray()
    overflowed = False
    total = 0
    spill_path.parent.mkdir(parents=True, exist_ok=True)
    with spill_path.open("wb") as handle:
        while chunk := await stream.read(8192):
            total += len(chunk)
            if total <= spill_limit:
                handle.write(chunk)
            else:
                overflowed = True
            inline_remaining = inline_limit - len(inline)
            if inline_remaining > 0:
                inline.extend(chunk[:inline_remaining])
    if total > inline_limit:
        overflowed = True
    return {
        "inline": inline.decode("utf-8", errors="replace"),
        "output_path": str(spill_path),
        "output_bytes": total,
        "truncated": overflowed or len(inline) >= inline_limit,
    }


async def tapped_drain_to_file(
    stream: asyncio.StreamReader,
    spill_path: Path,
    tap: StreamTap,
    *,
    inline_limit: int = MAX_OUTPUT_BYTES,
    spill_limit: int = MAX_SPILL_BYTES,
) -> dict[str, Any]:
    """:func:`drain_to_file` with an incremental :class:`StreamTap` feed.

    Same spill/inline contract as the plain drain (the tap sees every chunk
    but never alters what lands on disk or in the inline head); the tap just
    observes the stream so the operator surface can render output while the
    command runs.
    """
    inline = bytearray()
    overflowed = False
    total = 0
    spill_path.parent.mkdir(parents=True, exist_ok=True)
    with spill_path.open("wb") as handle:
        while chunk := await stream.read(8192):
            tap.feed(chunk)
            total += len(chunk)
            if total <= spill_limit:
                handle.write(chunk)
            else:
                overflowed = True
            inline_remaining = inline_limit - len(inline)
            if inline_remaining > 0:
                inline.extend(chunk[:inline_remaining])
    tap.close()
    if total > inline_limit:
        overflowed = True
    return {
        "inline": inline.decode("utf-8", errors="replace"),
        "output_path": str(spill_path),
        "output_bytes": total,
        "truncated": overflowed or len(inline) >= inline_limit,
    }


def shape_empty_output(stdout: str, stderr: str, return_code: int) -> str:
    """Return the explicit message for a silent success (SWE-agent finding)."""
    if return_code == 0 and not stdout.strip() and not stderr.strip():
        return "(your command ran successfully and produced no output)"
    return stdout


def benign_rc1_note(command: str, return_code: int) -> str | None:
    """Note text when rc=1 is a benign informational answer for this command."""
    if return_code != 1:
        return None
    stripped = command.strip()
    first = stripped.split(maxsplit=1)[0] if stripped else ""
    if first in BENIGN_RC1_COMMANDS or stripped.startswith(_BENIGN_RC1_PREFIXES):
        return (
            f"exit 1 from {first!r} is informational here (e.g. no matches), "
            "not a failure; read the output for the answer."
        )
    return None


def pipe_failure_note(pipe_statuses: list[int], return_code: int | None) -> str | None:
    """Note text when an early pipeline stage failed but the last didn't."""
    if not pipe_statuses or return_code is None:
        return None
    failing = [str(rc) for rc in pipe_statuses if rc != 0]
    if failing and pipe_statuses[-1] == 0:
        return (
            f"pipeline stage(s) exited nonzero ({', '.join(sorted(set(failing)))}) "
            "even though the final stage succeeded (rc=0); intermediate output "
            "may be incomplete."
        )
    return None


def is_auto_background_guarded(command: str) -> bool:
    """True when a command must never be auto-backgrounded on timeout."""
    stripped = command.strip()
    if stripped.startswith("sleep"):
        return True
    try:
        tokens = shlex.split(stripped)
    except ValueError:
        tokens = stripped.split()
    return "git" in tokens


def _auto_bg_bwrap_timeout(model_timeout_seconds: int) -> int:
    """bwrap_run timeout granted to auto-background candidates.

    The model's timeout stays the user-visible wait; the child gets a grace
    window past it so backgrounded work keeps making progress. Capped at
    bwrap_run's 3600s parameter ceiling.
    """
    return min(
        max(model_timeout_seconds, 1) + _AUTO_BG_GRACE_SECONDS,
        _AUTO_BG_MAX_TIMEOUT_SECONDS,
    )


def resolve_batch_schedule(
    requested: str,
    *,
    sandboxed: bool,
    persist: bool = False,
    layer_id: str = "",
    overrides: dict[str, Any] | None = None,
) -> tuple[str, str | None]:
    """Decide serial vs parallel for a ``commands`` batch.

    Overlay capture is per-call, so parallel is always downgraded. ``auto``
    picks parallel in bare / ephemeral / readonly regimes and serial otherwise.
    """
    overrides = overrides or {}
    persistence = overrides.get("persistence")
    overlay = bool(persist) or bool(layer_id) or persistence == "overlay"
    if requested not in ("auto", "parallel", "serial"):
        requested = "auto"
    if overlay and requested != "serial":
        return "serial", (
            "overlay capture is per-call; parallel batch downgraded to serial"
        )
    if requested == "auto":
        if not sandboxed or persistence in (None, "ephemeral", "readonly"):
            return "parallel", None
        return "serial", None
    return requested, None


def _parse_output(raw: str, sentinel: str) -> ParsedOutput:
    """Strip the sentinel prompt + parse rc/pipe/state markers from raw output.

    Markers live on their own lines after the command body:
    - ``<sentinel>rc=N pipe=a b c`` — exit status + per-stage pipe statuses.
    - ``<sentinel>state=cwd=<q>`` — the post-command working directory.
    - ``<sentinel>var NAME=<q>`` — one exported variable added this command.
    - ``<sentinel>func NAME <b64>`` — one function definition, base64-encoded
      so multi-line bodies stay on a single parseable line. Decoded bodies
      land in ``state['functions']``; the host applies the carryover policy.
    Leftover ``<sentinel>funcs ...`` lines (from older wrappers) are dropped
    so they cannot leak into model stdout.
    Anything else is user output.
    """
    return_code: int | None = None
    pipe_statuses: list[int] = []
    cwd = ""
    exported: dict[str, str] = {}
    functions: dict[str, str] = {}
    lines: list[str] = []
    for line in raw.splitlines():
        match = _RC_MARKER.match(line)
        if match and sentinel.strip() in match.group("sentinel"):
            return_code = int(match.group("rc"))
            if match.group("pipe"):
                pipe_statuses = [int(p) for p in match.group("pipe").split()]
            continue
        cwd_match = _STATE_CWD.match(line)
        if cwd_match and sentinel.strip() in cwd_match.group("sentinel"):
            cwd = cwd_match.group("cwd").strip("'\"")
            continue
        var_match = _STATE_VAR.match(line)
        if var_match and sentinel.strip() in var_match.group("sentinel"):
            value = var_match.group("value").strip()
            # printf %q emits a bare token unless quoting is needed.
            exported[var_match.group("name")] = value.strip("'\"")
            continue
        func_match = _STATE_FUNC.match(line)
        if func_match and sentinel.strip() in func_match.group("sentinel"):
            try:
                body = base64.b64decode(func_match.group("b64")).decode("utf-8")
            except (binascii.Error, UnicodeDecodeError, ValueError):
                # Corrupt capture must never poison the session state.
                continue
            functions[func_match.group("name")] = body
            continue
        if line.startswith(f"{sentinel.strip()}func "):
            # Malformed function marker (unparseable name/payload): drop it
            # rather than leak sentinel noise into model stdout.
            continue
        funcs_match = _STATE_FUNCS.match(line)
        if funcs_match and sentinel.strip() in funcs_match.group("sentinel"):
            # Drop leftover marker lines; do not persist function names.
            continue
        # Drop prompt echo lines that are just the sentinel.
        if line.strip() == sentinel.strip():
            continue
        lines.append(line)
    # Remove the echoed command's trailing prompt noise.
    body = "\n".join(lines)
    # A trailing lone rc marker without newline (prompt after last command).
    body = re.sub(re.escape(sentinel.strip()) + r"rc=\d+(?:\spipe=[0-9 ]+)?\s*$", "", body)
    body = body.replace(sentinel, "")
    state: dict[str, Any] = {}
    if cwd:
        state["cwd"] = cwd
    if exported:
        state["exported"] = exported
    if functions:
        state["functions"] = functions
    return ParsedOutput(
        body=body.strip("\n"),
        return_code=return_code,
        pipe_statuses=pipe_statuses,
        state=state,
    )


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


async def _run_process(
    argv: list[str],
    *,
    timeout_seconds: int | None,
    spill_dir: Path | None = None,
    ctx: ToolExecutionContext | None = None,
) -> dict[str, Any]:
    process = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    assert process.stdout is not None
    assert process.stderr is not None
    if spill_dir is None:
        # No data dir (degenerate ctx): fall back to a temp dir so the
        # drain-to-file contract holds without surfacing paths to the user.
        import tempfile

        spill_dir = Path(tempfile.gettempdir()) / "lisa-bash-spill"
    spill_dir.mkdir(parents=True, exist_ok=True)

    stamp = secrets.token_hex(4)
    stdout_path = spill_dir / f"stdout-{stamp}.log"
    stderr_path = spill_dir / f"stderr-{stamp}.log"
    stdout_tap = _maybe_tap(ctx, stream="stdout")
    stderr_tap = _maybe_tap(ctx, stream="stderr")
    if stdout_tap is not None:
        stdout_task = asyncio.create_task(
            tapped_drain_to_file(process.stdout, stdout_path, stdout_tap)
        )
    else:
        stdout_task = asyncio.create_task(drain_to_file(process.stdout, stdout_path))
    if stderr_tap is not None:
        stderr_task = asyncio.create_task(
            tapped_drain_to_file(process.stderr, stderr_path, stderr_tap)
        )
    else:
        stderr_task = asyncio.create_task(drain_to_file(process.stderr, stderr_path))
    timed_out = False
    try:
        if timeout_seconds is None:
            await process.wait()
        else:
            await asyncio.wait_for(process.wait(), timeout=timeout_seconds)
    except TimeoutError:
        timed_out = True
        process.kill()
        await process.wait()
    stdout = await stdout_task
    stderr = await stderr_task
    return {
        "stdout_raw": stdout["inline"],
        "stderr": stderr["inline"],
        "stdout_path": stdout["output_path"],
        "stderr_path": stderr["output_path"],
        "stdout_bytes": stdout["output_bytes"],
        "stderr_bytes": stderr["output_bytes"],
        "timed_out": timed_out,
        "truncated": stdout["truncated"] or stderr["truncated"],
        "process_rc": process.returncode,
    }


def _apply_shaping(result: dict[str, Any], command: str) -> None:
    """Apply the three output-shaping notes to a finished result dict."""
    rc = result.get("return_code")
    if rc is None:
        return
    if result["stdout"] == "":
        shaped = shape_empty_output(
            "", result.get("stderr", ""), rc
        )
        if shaped != result["stdout"]:
            result["note"] = shaped
    benign = benign_rc1_note(command, rc)
    if benign is not None:
        result["note"] = f"{result['note']} {benign}" if result.get("note") else benign
    pipe_note = pipe_failure_note(result.get("pipe_statuses") or [], rc)
    if pipe_note is not None:
        result["note"] = f"{result['note']} {pipe_note}" if result.get("note") else pipe_note


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
        pack_configuration_resolver=getattr(
            ctx, "pack_configuration_resolver", None
        ),
        approval_service=ctx.approval_service,
        approval_request_id=ctx.approval_request_id,
        session_plan_digest=ctx.session_plan_digest,
        event_bus=ctx.event_bus,
        state_provider=ctx.state_provider,
        stream_output=ctx.stream_output,
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


def _publish_job_done(
    ctx: ToolExecutionContext,
    *,
    job_id: str,
    rc: int | None,
    killed: bool,
    log_path: Path,
    command: str,
    extra: dict[str, Any] | None = None,
) -> None:
    """Best-effort job completion event on the session event bus."""
    bus = getattr(ctx, "event_bus", None)
    if bus is None or not hasattr(bus, "publish"):
        return
    payload: dict[str, Any] = {
        "job_id": job_id,
        "rc": rc,
        "killed": killed,
        "log_path": str(log_path),
        "command": command[:200],
    }
    if extra:
        payload.update(extra)
    try:
        asyncio.create_task(bus.publish("lisa.bash.job_done", payload))
    except Exception:
        pass


async def _run_background_command(
    ctx: ToolExecutionContext,
    session: BashSession,
    *,
    command: str,
    inner_argv: list[str],
    timeout_seconds: int,
    overrides: dict[str, Any],
    persist: bool,
    layer_id: str,
    log_path: Path,
    rc_path: Path,
    job_id: str,
) -> None:
    """Run one backgrounded command to completion, teeing output to log_path.

    On finish: write rc to rc_path (one int, no prose), flip the job status,
    and (if an event bus is available) publish a completion event so hosts
    that subscribe get a turn-level notification. Never raises; failures land
    as rc=-1 and a ``[bash-job error] …`` line in the log.
    """
    rc = -1
    killed = False
    try:
        if session.sandboxed:
            payload = await _run_via_bwrap(
                ctx,
                inner_argv,
                timeout_seconds=max(timeout_seconds, 1) + 30,
                overrides=overrides,
                persist=persist,
                layer_id=layer_id,
            )
            stdout_raw = payload.get("stdout_raw", "")
            stderr = payload.get("stderr", "")
            timed_out = bool(payload.get("timed_out"))
            parsed = _parse_output(stdout_raw, session.sentinel)
            rc = payload.get("return_code")
            if rc is None:
                rc = parsed.return_code if parsed.return_code is not None else -1
            killed = timed_out
        else:
            # Explicit background jobs run to completion; the call-level
            # timeout only applies to foreground / auto-background.
            proc = await _run_process(
                inner_argv,
                timeout_seconds=None,
                spill_dir=log_path.parent,
                ctx=ctx,
            )
            stdout_raw = proc["stdout_raw"]
            stderr = proc["stderr"]
            timed_out = proc["timed_out"]
            rc = proc.get("process_rc") if proc.get("process_rc") is not None else -1
            killed = timed_out
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with log_path.open("a", encoding="utf-8") as handle:
                handle.write(f"$ {command}\n")
                if stdout_raw:
                    handle.write(stdout_raw + "\n")
                if stderr:
                    handle.write("[stderr] " + stderr.replace("\n", "\n[stderr] ") + "\n")
                handle.write(f"[rc={rc} timed_out={timed_out} job={job_id}]\n")
        except OSError:
            pass
        try:
            rc_path.write_text(str(rc), encoding="utf-8")
        except OSError:
            pass
    except Exception as exc:  # noqa: BLE001 - background tasks must not crash the session
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with log_path.open("a", encoding="utf-8") as handle:
                handle.write(f"[bash-job error] {exc}\n")
        except OSError:
            pass
        try:
            rc_path.write_text(str(rc), encoding="utf-8")
        except OSError:
            pass
    finally:
        session.complete_job(job_id, rc=rc, killed=killed)
        _publish_job_done(
            ctx,
            job_id=job_id,
            rc=rc,
            killed=killed,
            log_path=log_path,
            command=command,
        )


async def _run_process_with_auto_background(
    ctx: ToolExecutionContext,
    session: BashSession,
    *,
    command: str,
    inner_argv: list[str],
    timeout_seconds: int,
    spill_dir: Path,
    overrides: dict[str, Any],
    persist: bool,
    layer_id: str,
) -> dict[str, Any]:
    """Run a bare command; on timeout, background it instead of killing.

    Returns the standard proc result dict when the command finishes, or a
    backgrounded descriptor when the timeout trips. The caller routes the
    latter to the tool result unchanged.
    """
    process = await asyncio.create_subprocess_exec(
        *inner_argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    assert process.stdout is not None
    assert process.stderr is not None
    stamp = secrets.token_hex(4)
    stdout_path = spill_dir / f"stdout-{stamp}.log"
    stderr_path = spill_dir / f"stderr-{stamp}.log"
    stdout_tap = _maybe_tap(ctx, stream="stdout")
    stderr_tap = _maybe_tap(ctx, stream="stderr")
    if stdout_tap is not None:
        stdout_task = asyncio.create_task(
            tapped_drain_to_file(process.stdout, stdout_path, stdout_tap)
        )
    else:
        stdout_task = asyncio.create_task(drain_to_file(process.stdout, stdout_path))
    if stderr_tap is not None:
        stderr_task = asyncio.create_task(
            tapped_drain_to_file(process.stderr, stderr_path, stderr_tap)
        )
    else:
        stderr_task = asyncio.create_task(drain_to_file(process.stderr, stderr_path))
    try:
        await asyncio.wait_for(process.wait(), timeout=timeout_seconds)
    except TimeoutError:
        # Auto-background: keep the process running, track it as a job so its
        # artifacts are retrievable by the model.
        job_id = f"j{secrets.token_hex(6)}"
        log_path, rc_path, status_path = session.job_artifacts(job_id)
        log_path.parent.mkdir(parents=True, exist_ok=True)

        async def _collect() -> None:
            await process.wait()
            stdout = await stdout_task
            stderr = await stderr_task
            rc = process.returncode if process.returncode is not None else -1
            try:
                with log_path.open("a", encoding="utf-8") as handle:
                    handle.write(f"$ {command}\n")
                    if stdout["inline"]:
                        handle.write(stdout["inline"] + "\n")
                    if stderr["inline"]:
                        handle.write("[stderr] " + stderr["inline"].replace("\n", "\n[stderr] ") + "\n")
                    handle.write(f"[rc={rc} auto_backgrounded=1 job={job_id}]\n")
            except OSError:
                pass
            try:
                rc_path.write_text(str(rc), encoding="utf-8")
            except OSError:
                pass
            session.complete_job(job_id, rc=rc, killed=False)
            _publish_job_done(
                ctx,
                job_id=job_id,
                rc=rc,
                killed=False,
                log_path=log_path,
                command=command,
                extra={"auto_backgrounded": True},
            )

        collector = asyncio.create_task(_collect(), name=f"lisa-bash-job-{job_id}")
        session.register_job(job_id, collector)
        return {
            "backgrounded": True,
            "auto_backgrounded_on_timeout": True,
            "job_id": job_id,
            "log_path": str(log_path),
            "rc_path": str(rc_path),
            "status_path": str(status_path),
            "timeout_seconds": timeout_seconds,
            "command": command,
            "sandboxed": session.sandboxed,
            "background_jobs": session.jobs_summary(),
        }
    stdout = await stdout_task
    stderr = await stderr_task
    return {
        "stdout_raw": stdout["inline"],
        "stderr": stderr["inline"],
        "stdout_path": stdout["output_path"],
        "stderr_path": stderr["output_path"],
        "stdout_bytes": stdout["output_bytes"],
        "stderr_bytes": stderr["output_bytes"],
        "timed_out": False,
        "truncated": stdout["truncated"] or stderr["truncated"],
        "process_rc": process.returncode,
    }


async def run_in_session(
    ctx: ToolExecutionContext,
    session: BashSession,
    command: str,
    *,
    timeout_seconds: int,
    run_in_background: bool = False,
    record_state: bool = True,
    overrides: dict[str, Any] | None = None,
    persist: bool = False,
    layer_id: str = "",
) -> dict[str, Any]:
    """Run a command in the session, composing through bwrap when available.

    When ``run_in_background`` is true the command runs on a host-side
    asyncio.Task instead of blocking the tool turn; the result is a small
    job descriptor (``job_id``, ``log_path``, ``rc_path``, ``status_path``)
    the model can poll. A command that hits its timeout is NOT killed - it is
    auto-backgrounded (unless guarded: `sleep`, `git`), so the work is never
    lost mid-run.
    """
    inner_argv = session.build_inner_argv(command)
    job_id: str | None = None
    if run_in_background:
        job_id = f"j{secrets.token_hex(6)}"
        log_path, rc_path, status_path = session.job_artifacts(job_id)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        task = asyncio.create_task(
            _run_background_command(
                ctx, session, command=command, inner_argv=inner_argv,
                timeout_seconds=timeout_seconds, overrides=overrides or {},
                persist=persist, layer_id=layer_id,
                log_path=log_path, rc_path=rc_path, job_id=job_id,
            ),
            name=f"lisa-bash-job-{job_id}",
        )
        session.register_job(job_id, task)
        return {
            "backgrounded": True,
            "job_id": job_id,
            "log_path": str(log_path),
            "rc_path": str(rc_path),
            "status_path": str(status_path),
            "status": "running",
            "command": command,
            "sandboxed": session.sandboxed,
            "background_jobs": session.jobs_summary(),
        }
    try:
        if session.sandboxed:
            if not is_auto_background_guarded(command):
                # Sandboxed auto-background: grant the child a generous grace
                # window past the model's timeout and race it. On timeout the
                # still-running bwrap task is adopted as a job instead of
                # killed - --die-with-parent only ties the child to THIS host
                # process, so keeping the task alive keeps the work running
                # (and lets overlay capture complete).
                bwrap_task = asyncio.create_task(
                    _run_via_bwrap(
                        ctx,
                        inner_argv,
                        timeout_seconds=_auto_bg_bwrap_timeout(timeout_seconds),
                        overrides=overrides or {},
                        persist=persist,
                        layer_id=layer_id,
                    ),
                    name="lisa-bash-sandboxed-fg",
                )
                try:
                    await asyncio.wait_for(
                        asyncio.shield(bwrap_task), timeout=timeout_seconds
                    )
                except TimeoutError:
                    job_id = f"j{secrets.token_hex(6)}"
                    log_path, rc_path, status_path = session.job_artifacts(job_id)
                    log_path.parent.mkdir(parents=True, exist_ok=True)

                    async def _collect_sandboxed() -> None:
                        """Adopt the in-flight sandboxed call as a job."""
                        rc = -1
                        killed = False
                        try:
                            bg_payload = await bwrap_task
                            stdout_raw = bg_payload.get("stdout_raw", "")
                            stderr = bg_payload.get("stderr", "")
                            timed_out = bool(bg_payload.get("timed_out"))
                            bg_parsed = _parse_output(stdout_raw, session.sentinel)
                            rc = bg_payload.get("return_code")
                            if rc is None:
                                rc = (
                                    bg_parsed.return_code
                                    if bg_parsed.return_code is not None
                                    else -1
                                )
                            killed = timed_out
                            sandbox_error = bg_payload.get("error") or ""
                        except Exception as exc:  # noqa: BLE001 - jobs must not crash the session
                            sandbox_error = str(exc)
                            try:
                                with log_path.open("a", encoding="utf-8") as handle:
                                    handle.write(f"$ {command}\n[bash-job error] {exc}\n")
                            except OSError:
                                pass
                        else:
                            try:
                                log_path.parent.mkdir(parents=True, exist_ok=True)
                                with log_path.open("a", encoding="utf-8") as handle:
                                    handle.write(f"$ {command}\n")
                                    if sandbox_error:
                                        handle.write(f"[bash-job error] {sandbox_error}\n")
                                    if stdout_raw:
                                        handle.write(stdout_raw + "\n")
                                    if stderr:
                                        handle.write(
                                            "[stderr] "
                                            + stderr.replace("\n", "\n[stderr] ")
                                            + "\n"
                                        )
                                    handle.write(
                                        f"[rc={rc} timed_out={timed_out} "
                                        f"auto_backgrounded=1 job={job_id}]\n"
                                    )
                            except OSError:
                                pass
                        try:
                            rc_path.write_text(str(rc), encoding="utf-8")
                        except OSError:
                            pass
                        session.complete_job(job_id, rc=rc, killed=killed)
                        _publish_job_done(
                            ctx,
                            job_id=job_id,
                            rc=rc,
                            killed=killed,
                            log_path=log_path,
                            command=command,
                            extra={"auto_backgrounded": True},
                        )

                    collector = asyncio.create_task(
                        _collect_sandboxed(), name=f"lisa-bash-job-{job_id}"
                    )
                    session.register_job(job_id, collector)
                    return {
                        "backgrounded": True,
                        "auto_backgrounded_on_timeout": True,
                        "job_id": job_id,
                        "log_path": str(log_path),
                        "rc_path": str(rc_path),
                        "status_path": str(status_path),
                        "status": "running",
                        "timeout_seconds": timeout_seconds,
                        "command": command,
                        "sandboxed": True,
                        "background_jobs": session.jobs_summary(),
                    }
                payload = bwrap_task.result()
            else:
                payload = await _run_via_bwrap(
                    ctx,
                    inner_argv,
                    timeout_seconds=max(timeout_seconds, 1) + 30,
                    overrides=overrides or {},
                    persist=persist,
                    layer_id=layer_id,
                )
            if payload.get("error"):
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
            parsed = _parse_output(payload.get("stdout_raw", ""), session.sentinel)
            result = {
                "stdout": parsed.body,
                "stderr": payload.get("stderr", ""),
                "return_code": payload.get(
                    "return_code", parsed.return_code if parsed.return_code is not None else 0
                ),
                "timed_out": payload.get("timed_out", False),
                "truncated": payload.get("truncated", False),
                "sandboxed": True,
                "backend": payload.get("backend"),
                "layer_id": payload.get("layer_id"),
                "persistence": payload.get("persistence"),
                "capture_root": payload.get("capture_root"),
                "scratch_dir": payload.get("scratch_dir"),
                "identity": payload.get("identity"),
            }
            if parsed.pipe_statuses:
                result["pipe_statuses"] = parsed.pipe_statuses
            for spill_key in ("stdout_path", "stderr_path", "stdout_bytes", "stderr_bytes"):
                if payload.get(spill_key):
                    result[spill_key] = payload[spill_key]
            if payload.get("degraded"):
                result["degraded"] = True
                result["note"] = payload.get("note")
            # Guarded sandboxed commands (sleep / git) keep the plain T+30s
            # bwrap timeout: bounded work is killed, never backgrounded.
        else:
            # Bare path: auto-background on timeout unless the command is
            # guarded (e.g. `sleep` deliberately waits; `git` mutates shared
            # state and would be unsafe to leave running).
            spill_dir = session.jobs_dir
            spill_dir.mkdir(parents=True, exist_ok=True)
            if is_auto_background_guarded(command):
                proc = await _run_process(
                    inner_argv,
                    timeout_seconds=timeout_seconds,
                    spill_dir=spill_dir,
                    ctx=ctx,
                )
            else:
                proc = await _run_process_with_auto_background(
                    ctx,
                    session,
                    command=command,
                    inner_argv=inner_argv,
                    timeout_seconds=timeout_seconds,
                    spill_dir=spill_dir,
                    overrides=overrides or {},
                    persist=persist,
                    layer_id=layer_id,
                )
                if proc.get("backgrounded"):
                    return proc
            parsed = _parse_output(proc["stdout_raw"], session.sentinel)
            rc = parsed.return_code
            if rc is None:
                # No rc marker parsed (e.g. truncated output or timeout): fall
                # back to the process exit code rather than assuming success.
                rc = proc.get("process_rc")
            result = {
                "stdout": parsed.body,
                "stderr": proc["stderr"],
                "return_code": rc if rc is not None else 0,
                "timed_out": proc["timed_out"],
                "truncated": proc["truncated"],
                "sandboxed": False,
            }
            if parsed.pipe_statuses:
                result["pipe_statuses"] = parsed.pipe_statuses
            if proc.get("truncated"):
                result["stdout_path"] = proc["stdout_path"]
                result["stderr_path"] = proc["stderr_path"]
                result["stdout_bytes"] = proc["stdout_bytes"]
                result["stderr_bytes"] = proc["stderr_bytes"]
    except Exception as exc:
        # Covers both the sandbox path (bwrap_run raised through the
        # passthrough, e.g. bwrap missing on the host) and direct host
        # execution. Never fail silently: the stamp tells the model whether
        # the failure happened inside or outside the sandbox.
        return {
            "error": f"bash execution failed: {exc}",
            "sandboxed": session.sandboxed,
        }

    # State carryover: the parsed state dict (either path) is persisted here
    # by the host, replayed by the next call's rc source. Batch children pass
    # record_state=False so they share a snapshot and cannot race the file.
    cwd = ""
    if parsed.state:
        cwd = parsed.state.get("cwd", "") or ""
        if record_state:
            session.record_post_run_state(
                cwd=cwd,
                exported=parsed.state.get("exported") or {},
                functions=parsed.state.get("functions") or {},
            )
    if cwd:
        result["cwd"] = cwd
    _apply_shaping(result, command)
    # Surface running background jobs on every result so the model never needs
    # a separate listing round-trip.
    jobs = session.jobs_summary()
    if jobs:
        result["background_jobs"] = jobs
    _append_log(session.log_path, command, result)
    return result


async def run_batch_in_session(
    ctx: ToolExecutionContext,
    session: BashSession,
    commands: list[str],
    *,
    timeout_seconds: int,
    when: str = "auto",
    overrides: dict[str, Any] | None = None,
    persist: bool = False,
    layer_id: str = "",
) -> dict[str, Any]:
    """Run ``commands`` against a snapshot of session state.

    Parallel is the default in bare/ephemeral/readonly regimes; overlay
    capture always serializes. Env and function deltas from children are
    discarded; cwd is adopted only when every child agrees.
    """
    if not commands:
        return {"error": "commands must be a non-empty list"}
    if len(commands) > MAX_BATCH_COMMANDS:
        return {
            "error": (
                f"commands is capped at {MAX_BATCH_COMMANDS} "
                f"(got {len(commands)})"
            )
        }
    overrides = overrides or {}
    schedule, reason = resolve_batch_schedule(
        when,
        sandboxed=session.sandboxed,
        persist=persist,
        layer_id=layer_id,
        overrides=overrides,
    )

    async def _one(index: int, command: str) -> dict[str, Any]:
        item = await run_in_session(
            ctx,
            session,
            command,
            timeout_seconds=timeout_seconds,
            record_state=False,
            overrides=overrides,
            persist=persist,
            layer_id=layer_id,
        )
        item["index"] = index
        item["command"] = command
        return item

    if schedule == "parallel":
        results = list(
            await asyncio.gather(
                *[_one(i, command) for i, command in enumerate(commands)]
            )
        )
    else:
        results = [
            await _one(i, command) for i, command in enumerate(commands)
        ]

    cwds = [item["cwd"] for item in results if item.get("cwd")]
    notes: list[str] = []
    if reason:
        notes.append(reason)
    if cwds and len(set(cwds)) == 1:
        session.apply_cwd(cwds[0])
        notes.append(
            f"cwd adopted ({cwds[0]}) because every command agreed; "
            "env and function deltas from the batch were discarded"
        )
    else:
        notes.append(
            "session cwd/env unchanged (batch ran against a snapshot; "
            "env/function deltas discarded"
            + ("" if not cwds else "; child cwds disagreed")
            + ")"
        )
    envelope: dict[str, Any] = {
        "batch": True,
        "when": schedule,
        "requested_when": when,
        "results": results,
        "sandboxed": session.sandboxed,
        "note": " ".join(notes),
        "stdout": "",
        "stderr": "",
        "return_code": max(
            (item.get("return_code") or 0) for item in results
        ) if results else 0,
        "timed_out": any(item.get("timed_out") for item in results),
        "truncated": any(item.get("truncated") for item in results),
    }
    if reason:
        envelope["reason"] = reason
    jobs = session.jobs_summary()
    if jobs:
        envelope["background_jobs"] = jobs
    return envelope


def _reset_for_tests() -> None:
    from lisa_packs.bash import session as _session_module

    _session_module._reset_sessions()
