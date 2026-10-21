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
import contextlib
import re
import secrets
import shlex
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from kernel.models import ToolExecutionContext
from kernel.tools.output_stream import TOOL_OUTPUT_TOPIC, publish_tool_output

from lisa_packs.bash._decode import StreamDecoder, decode_bytes
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
_STATE_FUNC = re.compile(
    r"^(?P<sentinel>.+?)func (?P<name>[A-Za-z_][A-Za-z0-9_]*) (?P<b64>[A-Za-z0-9+/=]+)$"
)
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

    def __iter__(self) -> Iterator[str | int | None]:
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

    Deliberately NOT a full sentinel filter: the sentinel prompt echo is
    rare mid-command and harmless on a display surface. The ONE machinery
    class that must never reach the operator is the post-command state
    block the wrapper prints after the model's command (``rc=`` marker,
    ``state=cwd=``, one ``var NAME=`` line per exported env var — easily
    150+ lines per call on a host with a rich environment). That block is
    harness bookkeeping, not command output, so the tap filters those
    marker lines out of the operator stream here (the parse step still
    reads the full raw output for the model).
    """

    __slots__ = (
        "_buffer",
        "_ctx",
        "_decoder",
        "_max_line",
        "_sentinel",
        "_seq",
        "_stream",
    )

    def __init__(
        self,
        ctx: ToolExecutionContext,
        *,
        stream: str,
        sentinel: str = "",
    ) -> None:
        self._ctx = ctx
        self._stream = stream
        self._buffer = ""
        self._decoder = StreamDecoder()
        self._seq = 0
        # A pathological single line (e.g. base64 dump) is truncated for the
        # operator view; the full text still reaches the model via ToolResult.
        self._max_line = 2000
        self._sentinel = (sentinel or "").strip()

    def _is_machinery(self, line: str) -> bool:
        """True when ``line`` is one of the wrapper's post-command markers.

        Mirrors the parse contract in :func:`_parse_output` (rc / state=cwd /
        var NAME= / func NAME <b64> / funcs …) plus the bare sentinel prompt
        echo. Display-only: the markers stay in the raw stream the parser
        consumes for the model-visible result.
        """
        if not self._sentinel:
            return False
        stripped = line.strip()
        if stripped == self._sentinel:
            return True
        if not stripped.startswith(self._sentinel):
            return False
        return stripped[len(self._sentinel) :].startswith(
            ("rc=", "state=", "var ", "func ", "funcs ")
        )

    def feed(self, chunk: bytes) -> None:
        """Split ``chunk`` into lines and publish each complete one."""
        if not chunk:
            return
        self._buffer += self._decoder.decode(chunk)
        while "\n" in self._buffer:
            line, _, rest = self._buffer.partition("\n")
            self._buffer = rest
            self._publish(line)

    def close(self) -> None:
        """Flush the trailing partial line (if any) on stream end."""
        self._buffer += self._decoder.flush()
        if self._buffer:
            self._publish(self._buffer)
            self._buffer = ""

    def _publish(self, line: str) -> None:
        if self._is_machinery(line):
            return
        if len(line) > self._max_line:
            line = line[: self._max_line - 1] + "…"
        publish_tool_output(self._ctx, text=line, stream=self._stream, seq=self._seq)
        self._seq += 1


def _maybe_tap(
    ctx: ToolExecutionContext | None,
    *,
    stream: str,
    sentinel: str = "",
) -> StreamTap | None:
    """Build a :class:`StreamTap` only when streaming is declared and live."""
    if ctx is None or not getattr(ctx, "stream_output", False):
        return None
    bus = getattr(ctx, "event_bus", None)
    if bus is None or not hasattr(bus, "publish"):
        return None
    return StreamTap(ctx, stream=stream, sentinel=sentinel)


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
        "inline": decode_bytes(bytes(inline)),
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
        "inline": decode_bytes(bytes(inline)),
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
    paths_overlay = any(
        isinstance(overrides.get("paths"), dict) and access == "capture"
        for access in (overrides.get("paths") or {}).values()
    )
    overlay = bool(persist) or bool(layer_id) or persistence == "overlay" or paths_overlay
    if requested not in ("auto", "parallel", "serial"):
        requested = "auto"
    if overlay and requested != "serial":
        return "serial", ("overlay capture is per-call; parallel batch downgraded to serial")
    if requested == "auto":
        if not sandboxed or persistence in (None, "ephemeral", "readonly"):
            return "parallel", None
        return "serial", None
    return requested, None


@dataclass(slots=True)
class _MarkerScan:
    """Accumulated marker values harvested from one raw output stream."""

    return_code: int | None = None
    pipe_statuses: list[int] = field(default_factory=list)
    cwd: str = ""
    exported: dict[str, str] = field(default_factory=dict)
    functions: dict[str, str] = field(default_factory=dict)
    body_lines: list[str] = field(default_factory=list)


def _scan_marker_line(line: str, sentinel: str, scan: _MarkerScan) -> bool:
    """Fold one raw line into ``scan``; True when the line was a marker.

    Marker forms (all prefixed with this session's sentinel):
    - ``<sentinel>rc=N pipe=a b c`` — exit status + per-stage pipe statuses.
    - ``<sentinel>state=cwd=<q>`` — the post-command working directory.
    - ``<sentinel>var NAME=<q>`` — one exported variable added this command.
    - ``<sentinel>func NAME <b64>`` — one base64-encoded function definition.
    Leftover ``<sentinel>funcs ...`` lines (from older wrappers) and
    malformed ``<sentinel>func ...`` lines are dropped so they cannot leak
    into model stdout.
    """
    stripped_sentinel = sentinel.strip()
    if _scan_rc_marker(line, stripped_sentinel, scan):
        return True
    if _scan_state_marker(line, stripped_sentinel, scan):
        return True
    if _scan_var_marker(line, stripped_sentinel, scan):
        return True
    if _scan_func_marker(line, stripped_sentinel, scan):
        return True
    if _scan_dropped_marker(line, stripped_sentinel):
        return True
    # Drop prompt echo lines that are just the sentinel.
    return line.strip() == stripped_sentinel


def _scan_rc_marker(line: str, sentinel: str, scan: _MarkerScan) -> bool:
    """Fold an ``rc=N pipe=...`` marker into ``scan``."""
    match = _RC_MARKER.match(line)
    if match is None or sentinel not in match.group("sentinel"):
        return False
    scan.return_code = int(match.group("rc"))
    if match.group("pipe"):
        scan.pipe_statuses = [int(p) for p in match.group("pipe").split()]
    return True


def _scan_state_marker(line: str, sentinel: str, scan: _MarkerScan) -> bool:
    """Fold a ``state=cwd=...`` marker into ``scan``."""
    match = _STATE_CWD.match(line)
    if match is None or sentinel not in match.group("sentinel"):
        return False
    scan.cwd = match.group("cwd").strip("'\"")
    return True


def _scan_var_marker(line: str, sentinel: str, scan: _MarkerScan) -> bool:
    """Fold a ``var NAME=value`` marker into ``scan``."""
    match = _STATE_VAR.match(line)
    if match is None or sentinel not in match.group("sentinel"):
        return False
    value = match.group("value").strip()
    # printf %q emits a bare token unless quoting is needed.
    scan.exported[match.group("name")] = value.strip("'\"")
    return True


def _scan_func_marker(line: str, sentinel: str, scan: _MarkerScan) -> bool:
    """Fold a ``func NAME <base64>`` marker into ``scan``."""
    match = _STATE_FUNC.match(line)
    if match is None or sentinel not in match.group("sentinel"):
        return False
    try:
        body = base64.b64decode(match.group("b64")).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        # Corrupt capture must never poison the session state.
        return True
    scan.functions[match.group("name")] = body
    return True


def _scan_dropped_marker(line: str, sentinel: str) -> bool:
    """True for marker lines that carry no state and are simply dropped.

    Covers leftover ``<sentinel>funcs ...`` lines (from older wrappers) and
    malformed ``<sentinel>func ...`` lines (unparseable name/payload) — both
    are dropped rather than leak sentinel noise into model stdout.
    """
    if line.startswith(f"{sentinel}func "):
        return True
    match = _STATE_FUNCS.match(line)
    return match is not None and sentinel in match.group("sentinel")


def _scan_output(raw: str, sentinel: str) -> _MarkerScan:
    """Split ``raw`` into marker values and the user-visible body lines."""
    scan = _MarkerScan()
    for line in raw.splitlines():
        if not _scan_marker_line(line, sentinel, scan):
            scan.body_lines.append(line)
    return scan


def _strip_sentinel_noise(body: str, sentinel: str) -> str:
    """Remove the echoed command's trailing prompt noise from ``body``."""
    # A trailing lone rc marker without newline (prompt after last command).
    body = re.sub(re.escape(sentinel.strip()) + r"rc=\d+(?:\spipe=[0-9 ]+)?\s*$", "", body)
    return body.replace(sentinel, "")


def _parse_output(raw: str, sentinel: str) -> ParsedOutput:
    """Strip the sentinel prompt + parse rc/pipe/state markers from raw output.

    Marker forms are documented on :func:`_scan_marker_line`; anything that is
    not a marker is user output and lands in the returned body.
    """
    scan = _scan_output(raw, sentinel)
    body = _strip_sentinel_noise("\n".join(scan.body_lines), sentinel)
    state: dict[str, Any] = {}
    if scan.cwd:
        state["cwd"] = scan.cwd
    if scan.exported:
        state["exported"] = scan.exported
    if scan.functions:
        state["functions"] = scan.functions
    return ParsedOutput(
        body=body.strip("\n"),
        return_code=scan.return_code,
        pipe_statuses=scan.pipe_statuses,
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
    sentinel: str = "",
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
    stdout_task, stderr_task = _start_drain_tasks(ctx, process, stdout_path, stderr_path, sentinel)
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
        shaped = shape_empty_output("", result.get("stderr", ""), rc)
        if shaped != result["stdout"]:
            result["note"] = shaped
    benign = benign_rc1_note(command, rc)
    if benign is not None:
        result["note"] = f"{result['note']} {benign}" if result.get("note") else benign
    pipe_note = pipe_failure_note(result.get("pipe_statuses") or [], rc)
    if pipe_note is not None:
        result["note"] = f"{result['note']} {pipe_note}" if result.get("note") else pipe_note


class _MachineryFilterBus:
    """Event-bus wrapper dropping the session's sentinel machinery lines.

    Wraps the turn's event bus for the duration of a composed sandboxed call
    (bash_run → bwrap_run): the child tool publishes its incremental output
    on this bus, but the post-command state block the bash wrapper prints
    (``rc=`` marker, ``state=cwd=``, one ``var NAME=`` line per exported env
    var — easily 150+ lines per call) is harness bookkeeping, not command
    output. Machinery payloads on the ``lisa.tool_output`` topic are dropped
    here; every other topic, subscription, and real output line forwards to
    the wrapped bus unchanged. Display-only: the raw markers stay in the
    stream the parser consumes for the model-visible result.
    """

    __slots__ = ("_bus", "_sentinel")

    def __init__(self, bus: Any, *, sentinel: str) -> None:
        self._bus = bus
        self._sentinel = (sentinel or "").strip()

    def _is_machinery(self, text: str) -> bool:
        """True when ``text`` is one of the wrapper's post-command markers."""
        if not self._sentinel:
            return False
        stripped = text.strip()
        if stripped == self._sentinel:
            return True
        if not stripped.startswith(self._sentinel):
            return False
        return stripped[len(self._sentinel) :].startswith(
            ("rc=", "state=", "var ", "func ", "funcs ")
        )

    async def publish(self, topic: str, payload: dict[str, Any]) -> None:
        if topic == TOOL_OUTPUT_TOPIC and self._is_machinery(str(payload.get("text") or "")):
            return
        await self._bus.publish(topic, payload)

    def __getattr__(self, name: str) -> Any:
        # ``_bus`` is set first in __init__; guard the one slot that must
        # never recurse through the delegation below.
        if name == "_bus":
            raise AttributeError(name)
        return getattr(self._bus, name)


async def _run_via_bwrap(
    ctx: ToolExecutionContext,
    inner_argv: list[str],
    *,
    timeout_seconds: int,
    overrides: dict[str, Any],
    persist: bool,
    layer_id: str,
    sentinel: str = "",
) -> dict[str, Any]:
    """Compose through bwrap_run bound to its own pack configuration.

    The composed child streams its incremental output on this context's
    event bus (the kernel's ``dispatch_child`` copies it into the child
    context). The child's tap knows nothing of the bash sentinel, so the
    wrapper's state block would flood the operator surface on the
    sandboxed path. For the duration of the composed call the parent
    swaps in a :class:`_MachineryFilterBus` carrying this session's
    sentinel: machinery lines are dropped in the middle, everything else
    passes through. The child keeps its copied reference until it
    finishes, so an auto-backgrounded adoption still filters after this
    call returns.
    """
    dispatch = ctx.delegated_tool_dispatch
    if dispatch is None:
        return {"error": "bwrap sandbox dispatch is unavailable", "sandboxed": False}
    original_bus = ctx.event_bus
    # Idempotent: an already-filtering bus (left in place by a still-running
    # auto-backgrounded adoption of an earlier call) keeps filtering — never
    # stack wrappers race-ily over overlapping composed calls.
    if (
        original_bus is not None
        and hasattr(original_bus, "publish")
        and not isinstance(original_bus, _MachineryFilterBus)
    ):
        ctx.event_bus = _MachineryFilterBus(original_bus, sentinel=sentinel)
    try:
        tool_result = await dispatch(
            "bwrap_run",
            {
                "argv": inner_argv,
                "overrides": overrides,
                "timeout_seconds": timeout_seconds,
                "persist": persist,
                "layer_id": layer_id,
            },
        )
    finally:
        ctx.event_bus = original_bus
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
    if (
        payload.get("sandboxed") is not True
        or payload.get("degraded")
        or type(payload.get("return_code")) is not int
    ):
        return {"error": "bwrap sandbox returned an unverified result", "sandboxed": False}
    payload["stdout_raw"] = tool_result.output_text or ""
    return payload


#: Job-completion event topic published on the session event bus.
_JOB_DONE_TOPIC = "lisa.bash.job_done"

#: In-flight fire-and-forget publishes. Keeping a strong reference until the
#: task finishes stops the event loop from garbage-collecting a pending
#: publish mid-flight (RUF006).
_PENDING_PUBLISHES: set[asyncio.Task[None]] = set()


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
        task = asyncio.create_task(bus.publish(_JOB_DONE_TOPIC, payload))
    except Exception:  # pylint: disable=broad-except  # best-effort notification
        return
    _PENDING_PUBLISHES.add(task)
    task.add_done_callback(_PENDING_PUBLISHES.discard)


@dataclass(slots=True)
class _JobOutcome:
    """Result of one backgrounded command run."""

    stdout_raw: str = ""
    stderr: str = ""
    timed_out: bool = False
    rc: int = -1


def _as_exit_code(value: object, fallback: int) -> int:
    """Narrow an untrusted exit-code field to an int."""
    if isinstance(value, bool) or not isinstance(value, int):
        return fallback
    return value


def _optional_exit_code(value: object) -> int | None:
    """Narrow an untrusted exit-code field to an int, or None when absent."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _resolve_exit_code(raw: object, parsed: ParsedOutput, fallback: int = -1) -> int:
    """Resolve an exit code: the payload's value, else the parsed marker."""
    rc = _optional_exit_code(raw)
    if rc is not None:
        return rc
    if parsed.return_code is not None:
        return parsed.return_code
    return fallback


async def _run_sandboxed_background(
    ctx: ToolExecutionContext,
    session: BashSession,
    *,
    inner_argv: list[str],
    timeout_seconds: int,
    overrides: dict[str, Any],
    persist: bool,
    layer_id: str,
) -> _JobOutcome:
    """Run one backgrounded command through the bwrap composition."""
    payload = await _run_via_bwrap(
        ctx,
        inner_argv,
        timeout_seconds=max(timeout_seconds, 1) + 30,
        overrides=overrides,
        persist=persist,
        layer_id=layer_id,
        sentinel=session.sentinel,
    )
    stdout_raw = str(payload.get("stdout_raw", ""))
    stderr = str(payload.get("stderr", ""))
    timed_out = bool(payload.get("timed_out"))
    parsed = _parse_output(stdout_raw, session.sentinel)
    rc = _resolve_exit_code(payload.get("return_code"), parsed)
    return _JobOutcome(stdout_raw=stdout_raw, stderr=stderr, timed_out=timed_out, rc=rc)


async def _run_host_background(
    ctx: ToolExecutionContext,
    session: BashSession,
    *,
    inner_argv: list[str],
    log_path: Path,
) -> _JobOutcome:
    """Run one backgrounded command directly on the host."""
    # Explicit background jobs run to completion; the call-level timeout only
    # applies to foreground / auto-background.
    proc = await _run_process(
        inner_argv,
        timeout_seconds=None,
        spill_dir=log_path.parent,
        ctx=ctx,
        sentinel=session.sentinel,
    )
    return _JobOutcome(
        stdout_raw=str(proc["stdout_raw"]),
        stderr=str(proc["stderr"]),
        timed_out=bool(proc["timed_out"]),
        rc=_as_exit_code(proc.get("process_rc"), -1),
    )


def _write_background_log(
    log_path: Path,
    command: str,
    outcome: _JobOutcome,
    job_id: str,
) -> None:
    """Tee the command + its output into the job log (best effort)."""
    with contextlib.suppress(OSError):
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(f"$ {command}\n")
            if outcome.stdout_raw:
                handle.write(outcome.stdout_raw + "\n")
            if outcome.stderr:
                handle.write("[stderr] " + outcome.stderr.replace("\n", "\n[stderr] ") + "\n")
            handle.write(f"[rc={outcome.rc} timed_out={outcome.timed_out} job={job_id}]\n")


def _write_background_error(log_path: Path, exc: BaseException) -> None:
    """Record a background-job failure in the job log (best effort)."""
    with contextlib.suppress(OSError):
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(f"[bash-job error] {exc}\n")


def _write_rc_file(rc_path: Path, rc: int) -> None:
    """Write the job's exit code to its rc file (best effort)."""
    with contextlib.suppress(OSError):
        rc_path.write_text(str(rc), encoding="utf-8")


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
    outcome = _JobOutcome()
    try:
        if session.sandboxed:
            outcome = await _run_sandboxed_background(
                ctx,
                session,
                inner_argv=inner_argv,
                timeout_seconds=timeout_seconds,
                overrides=overrides,
                persist=persist,
                layer_id=layer_id,
            )
        else:
            outcome = await _run_host_background(
                ctx, session, inner_argv=inner_argv, log_path=log_path
            )
        _write_background_log(log_path, command, outcome, job_id)
        _write_rc_file(rc_path, outcome.rc)
    except Exception as exc:  # pylint: disable=broad-except  # jobs must not crash the session
        _write_background_error(log_path, exc)
        _write_rc_file(rc_path, outcome.rc)
    finally:
        session.complete_job(job_id, rc=outcome.rc, killed=outcome.timed_out)
        _publish_job_done(
            ctx,
            job_id=job_id,
            rc=outcome.rc,
            killed=outcome.timed_out,
            log_path=log_path,
            command=command,
        )


def _start_drain_tasks(
    ctx: ToolExecutionContext | None,
    process: asyncio.subprocess.Process,
    stdout_path: Path,
    stderr_path: Path,
    sentinel: str,
) -> tuple[asyncio.Task[dict[str, Any]], asyncio.Task[dict[str, Any]]]:
    """Start the stdout/stderr drain tasks for ``process``."""
    assert process.stdout is not None
    assert process.stderr is not None
    stdout_tap = _maybe_tap(ctx, stream="stdout", sentinel=sentinel)
    stderr_tap = _maybe_tap(ctx, stream="stderr", sentinel=sentinel)
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
    return stdout_task, stderr_task


def _write_auto_background_log(
    log_path: Path,
    command: str,
    *,
    stdout: dict[str, Any],
    stderr: dict[str, Any],
    rc: int,
    job_id: str,
) -> None:
    """Tee an auto-backgrounded command's output into the job log."""
    with contextlib.suppress(OSError), log_path.open("a", encoding="utf-8") as handle:
        handle.write(f"$ {command}\n")
        if stdout["inline"]:
            handle.write(str(stdout["inline"]) + "\n")
        if stderr["inline"]:
            handle.write("[stderr] " + str(stderr["inline"]).replace("\n", "\n[stderr] ") + "\n")
        handle.write(f"[rc={rc} auto_backgrounded=1 job={job_id}]\n")


@dataclass(slots=True)
class _AutoBackgroundJob:
    """Everything the auto-background collector needs to record its result."""

    process: asyncio.subprocess.Process
    stdout_task: asyncio.Task[dict[str, Any]]
    stderr_task: asyncio.Task[dict[str, Any]]
    command: str
    job_id: str
    log_path: Path
    rc_path: Path


async def _collect_auto_backgrounded(
    ctx: ToolExecutionContext,
    session: BashSession,
    job: _AutoBackgroundJob,
) -> None:
    """Drain an auto-backgrounded process to completion and record its result."""
    await job.process.wait()
    stdout = await job.stdout_task
    stderr = await job.stderr_task
    rc = job.process.returncode if job.process.returncode is not None else -1
    _write_auto_background_log(
        job.log_path, job.command, stdout=stdout, stderr=stderr, rc=rc, job_id=job.job_id
    )
    _write_rc_file(job.rc_path, rc)
    session.complete_job(job.job_id, rc=rc, killed=False)
    _publish_job_done(
        ctx,
        job_id=job.job_id,
        rc=rc,
        killed=False,
        log_path=job.log_path,
        command=job.command,
        extra={"auto_backgrounded": True},
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
    stdout_task, stderr_task = _start_drain_tasks(
        ctx, process, stdout_path, stderr_path, session.sentinel
    )
    try:
        await asyncio.wait_for(process.wait(), timeout=timeout_seconds)
    except TimeoutError:
        # Auto-background: keep the process running, track it as a job so its
        # artifacts are retrievable by the model.
        job_id = f"j{secrets.token_hex(6)}"
        log_path, rc_path, status_path = session.job_artifacts(job_id)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        collector = asyncio.create_task(
            _collect_auto_backgrounded(
                ctx,
                session,
                _AutoBackgroundJob(
                    process=process,
                    stdout_task=stdout_task,
                    stderr_task=stderr_task,
                    command=command,
                    job_id=job_id,
                    log_path=log_path,
                    rc_path=rc_path,
                ),
            ),
            name=f"lisa-bash-job-{job_id}",
        )
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


@dataclass(slots=True)
class _CommandOutcome:
    """A command's result plus the wrapper markers parsed from its output.

    ``parsed`` is ``None`` for the results that short-circuit the
    state-carryover tail: backgrounded job descriptors (the command has not
    finished) and hard sandbox errors.
    """

    result: dict[str, Any]
    parsed: ParsedOutput | None = None


@dataclass(slots=True)
class _SandboxOutcome:
    """Either a bwrap payload or a backgrounded job descriptor."""

    payload: dict[str, Any] | None = None
    backgrounded: dict[str, Any] | None = None


@dataclass(slots=True)
class _SandboxedAdoption:
    """Everything the sandboxed-job collector needs to record its result."""

    bwrap_task: asyncio.Task[dict[str, Any]]
    command: str
    job_id: str
    log_path: Path
    rc_path: Path
    sentinel: str


def _write_sandboxed_job_error(log_path: Path, command: str, exc: BaseException) -> None:
    """Record a failed sandboxed job in the job log (best effort)."""
    with contextlib.suppress(OSError), log_path.open("a", encoding="utf-8") as handle:
        handle.write(f"$ {command}\n[bash-job error] {exc}\n")


def _write_sandboxed_job_log(
    log_path: Path,
    command: str,
    *,
    stdout_raw: str,
    stderr: str,
    rc: int,
    timed_out: bool,
    job_id: str,
    sandbox_error: str,
) -> None:
    """Tee an adopted sandboxed job's output into the job log."""
    with contextlib.suppress(OSError):
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(f"$ {command}\n")
            if sandbox_error:
                handle.write(f"[bash-job error] {sandbox_error}\n")
            if stdout_raw:
                handle.write(stdout_raw + "\n")
            if stderr:
                handle.write("[stderr] " + stderr.replace("\n", "\n[stderr] ") + "\n")
            handle.write(f"[rc={rc} timed_out={timed_out} auto_backgrounded=1 job={job_id}]\n")


async def _collect_sandboxed_job(
    ctx: ToolExecutionContext,
    session: BashSession,
    adoption: _SandboxedAdoption,
) -> None:
    """Adopt the in-flight sandboxed call as a job and record its result."""
    rc = -1
    killed = False
    stdout_raw = ""
    stderr = ""
    timed_out = False
    sandbox_error = ""
    try:
        bg_payload = await adoption.bwrap_task
        stdout_raw = str(bg_payload.get("stdout_raw", ""))
        stderr = str(bg_payload.get("stderr", ""))
        timed_out = bool(bg_payload.get("timed_out"))
        bg_parsed = _parse_output(stdout_raw, adoption.sentinel)
        rc = _resolve_exit_code(bg_payload.get("return_code"), bg_parsed)
        killed = timed_out
        sandbox_error = str(bg_payload.get("error") or "")
    except Exception as exc:  # pylint: disable=broad-except  # jobs must not crash the session
        sandbox_error = str(exc)
        _write_sandboxed_job_error(adoption.log_path, adoption.command, exc)
    else:
        _write_sandboxed_job_log(
            adoption.log_path,
            adoption.command,
            stdout_raw=stdout_raw,
            stderr=stderr,
            rc=rc,
            timed_out=timed_out,
            job_id=adoption.job_id,
            sandbox_error=sandbox_error,
        )
    _write_rc_file(adoption.rc_path, rc)
    session.complete_job(adoption.job_id, rc=rc, killed=killed)
    _publish_job_done(
        ctx,
        job_id=adoption.job_id,
        rc=rc,
        killed=killed,
        log_path=adoption.log_path,
        command=adoption.command,
        extra={"auto_backgrounded": True},
    )


def _adopt_sandboxed_job(
    ctx: ToolExecutionContext,
    session: BashSession,
    bwrap_task: asyncio.Task[dict[str, Any]],
    *,
    command: str,
    timeout_seconds: int,
) -> dict[str, Any]:
    """Adopt an in-flight sandboxed call as a background job."""
    job_id = f"j{secrets.token_hex(6)}"
    log_path, rc_path, status_path = session.job_artifacts(job_id)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    collector = asyncio.create_task(
        _collect_sandboxed_job(
            ctx,
            session,
            _SandboxedAdoption(
                bwrap_task=bwrap_task,
                command=command,
                job_id=job_id,
                log_path=log_path,
                rc_path=rc_path,
                sentinel=session.sentinel,
            ),
        ),
        name=f"lisa-bash-job-{job_id}",
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


async def _race_sandboxed_auto_background(
    ctx: ToolExecutionContext,
    session: BashSession,
    *,
    command: str,
    inner_argv: list[str],
    timeout_seconds: int,
    overrides: dict[str, Any],
    persist: bool,
    layer_id: str,
) -> _SandboxOutcome:
    """Race the sandboxed call against the model timeout; adopt it on timeout.

    Sandboxed auto-background: grant the child a generous grace window past
    the model's timeout and race it. On timeout the still-running bwrap task
    is adopted as a job instead of killed - --die-with-parent only ties the
    child to THIS host process, so keeping the task alive keeps the work
    running (and lets overlay capture complete).
    """
    bwrap_task = asyncio.create_task(
        _run_via_bwrap(
            ctx,
            inner_argv,
            timeout_seconds=_auto_bg_bwrap_timeout(timeout_seconds),
            overrides=overrides,
            persist=persist,
            layer_id=layer_id,
            sentinel=session.sentinel,
        ),
        name="lisa-bash-sandboxed-fg",
    )
    try:
        await asyncio.wait_for(asyncio.shield(bwrap_task), timeout=timeout_seconds)
    except TimeoutError:
        backgrounded = _adopt_sandboxed_job(
            ctx, session, bwrap_task, command=command, timeout_seconds=timeout_seconds
        )
        return _SandboxOutcome(backgrounded=backgrounded)
    return _SandboxOutcome(payload=bwrap_task.result())


def _sandbox_error_result(payload: dict[str, Any]) -> dict[str, Any] | None:
    """The sandbox's own failure result, or None when the payload is fine."""
    if not payload.get("error"):
        return None
    # The sandbox itself reported a failure (e.g. bwrap missing on this
    # host): surface it with the sandbox stamp and any reason the child
    # provided so the model can relay the fix.
    error_result: dict[str, Any] = {
        "error": f"sandbox execution failed: {payload['error']}",
        "sandboxed": True,
    }
    if payload.get("missing"):
        error_result["missing"] = payload["missing"]
        error_result["error"] += ". Missing host prerequisites: " + "; ".join(payload["missing"])
    return error_result


def _sandbox_result(payload: dict[str, Any], parsed: ParsedOutput) -> dict[str, Any]:
    """Build the model-visible result dict from a bwrap payload."""
    result: dict[str, Any] = {
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
    if payload.get("layer_id") and not payload.get("degraded"):
        # Staged capture: point the model at the review contract doc.
        result["note"] = (
            "Writes are STAGED into capture layer "
            f"'{payload['layer_id']}', not applied to the live "
            "filesystem. See layer_review.md."
        )
    return result


async def _run_sandboxed_command(
    ctx: ToolExecutionContext,
    session: BashSession,
    *,
    command: str,
    inner_argv: list[str],
    timeout_seconds: int,
    overrides: dict[str, Any],
    persist: bool,
    layer_id: str,
) -> _CommandOutcome:
    """Run one command through the bwrap composition."""
    if is_auto_background_guarded(command):
        # Guarded sandboxed commands (sleep / git) keep the plain T+30s
        # bwrap timeout: bounded work is killed, never backgrounded.
        outcome = _SandboxOutcome(
            payload=await _run_via_bwrap(
                ctx,
                inner_argv,
                timeout_seconds=max(timeout_seconds, 1) + 30,
                overrides=overrides,
                persist=persist,
                layer_id=layer_id,
                sentinel=session.sentinel,
            )
        )
    else:
        outcome = await _race_sandboxed_auto_background(
            ctx,
            session,
            command=command,
            inner_argv=inner_argv,
            timeout_seconds=timeout_seconds,
            overrides=overrides,
            persist=persist,
            layer_id=layer_id,
        )
    if outcome.backgrounded is not None:
        return _CommandOutcome(result=outcome.backgrounded)
    payload = outcome.payload or {}
    error_result = _sandbox_error_result(payload)
    if error_result is not None:
        return _CommandOutcome(result=error_result)
    parsed = _parse_output(str(payload.get("stdout_raw", "")), session.sentinel)
    return _CommandOutcome(result=_sandbox_result(payload, parsed), parsed=parsed)


async def _run_bare_command(
    ctx: ToolExecutionContext,
    session: BashSession,
    *,
    command: str,
    inner_argv: list[str],
    timeout_seconds: int,
    overrides: dict[str, Any],
    persist: bool,
    layer_id: str,
) -> _CommandOutcome:
    """Run one command directly on the host.

    Bare path: auto-background on timeout unless the command is guarded
    (e.g. `sleep` deliberately waits; `git` mutates shared state and would be
    unsafe to leave running).
    """
    spill_dir = session.jobs_dir
    spill_dir.mkdir(parents=True, exist_ok=True)
    if is_auto_background_guarded(command):
        proc = await _run_process(
            inner_argv,
            timeout_seconds=timeout_seconds,
            spill_dir=spill_dir,
            ctx=ctx,
            sentinel=session.sentinel,
        )
    else:
        proc = await _run_process_with_auto_background(
            ctx,
            session,
            command=command,
            inner_argv=inner_argv,
            timeout_seconds=timeout_seconds,
            spill_dir=spill_dir,
            overrides=overrides,
            persist=persist,
            layer_id=layer_id,
        )
        if proc.get("backgrounded"):
            return _CommandOutcome(result=proc)
    parsed = _parse_output(str(proc["stdout_raw"]), session.sentinel)
    rc = parsed.return_code
    if rc is None:
        # No rc marker parsed (e.g. truncated output or timeout): fall
        # back to the process exit code rather than assuming success.
        rc = _as_exit_code(proc.get("process_rc"), 0)
    result: dict[str, Any] = {
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
    return _CommandOutcome(result=result, parsed=parsed)


def _start_background_job(
    ctx: ToolExecutionContext,
    session: BashSession,
    *,
    command: str,
    inner_argv: list[str],
    timeout_seconds: int,
    overrides: dict[str, Any],
    persist: bool,
    layer_id: str,
) -> dict[str, Any]:
    """Start an explicit background job and return its descriptor."""
    job_id = f"j{secrets.token_hex(6)}"
    log_path, rc_path, status_path = session.job_artifacts(job_id)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    task = asyncio.create_task(
        _run_background_command(
            ctx,
            session,
            command=command,
            inner_argv=inner_argv,
            timeout_seconds=timeout_seconds,
            overrides=overrides,
            persist=persist,
            layer_id=layer_id,
            log_path=log_path,
            rc_path=rc_path,
            job_id=job_id,
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


def _apply_state_carryover(
    session: BashSession,
    result: dict[str, Any],
    parsed: ParsedOutput,
    *,
    record_state: bool,
) -> None:
    """Persist the parsed state carryover and stamp the cwd on the result.

    The parsed state dict (either path) is persisted here by the host,
    replayed by the next call's rc source. Batch children pass
    record_state=False so they share a snapshot and cannot race the file.
    """
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
    if run_in_background:
        return _start_background_job(
            ctx,
            session,
            command=command,
            inner_argv=inner_argv,
            timeout_seconds=timeout_seconds,
            overrides=overrides or {},
            persist=persist,
            layer_id=layer_id,
        )
    try:
        if session.sandboxed:
            outcome = await _run_sandboxed_command(
                ctx,
                session,
                command=command,
                inner_argv=inner_argv,
                timeout_seconds=timeout_seconds,
                overrides=overrides or {},
                persist=persist,
                layer_id=layer_id,
            )
        else:
            outcome = await _run_bare_command(
                ctx,
                session,
                command=command,
                inner_argv=inner_argv,
                timeout_seconds=timeout_seconds,
                overrides=overrides or {},
                persist=persist,
                layer_id=layer_id,
            )
    except Exception as exc:
        # Covers both the sandbox path (bwrap_run raised through the
        # passthrough, e.g. bwrap missing on the host) and direct host
        # execution. Never fail silently: the stamp tells the model whether
        # the failure happened inside or outside the sandbox.
        return {
            "error": f"bash execution failed: {exc}",
            "sandboxed": session.sandboxed,
        }

    result = outcome.result
    parsed = outcome.parsed
    if parsed is None:
        # Backgrounded descriptor or hard sandbox error: no state carryover.
        return result
    _apply_state_carryover(session, result, parsed, record_state=record_state)
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
        return {"error": (f"commands is capped at {MAX_BATCH_COMMANDS} (got {len(commands)})")}
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
            await asyncio.gather(*[_one(i, command) for i, command in enumerate(commands)])
        )
    else:
        results = [await _one(i, command) for i, command in enumerate(commands)]

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
            "env/function deltas discarded" + ("" if not cwds else "; child cwds disagreed") + ")"
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
        "return_code": max((item.get("return_code") or 0) for item in results) if results else 0,
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
