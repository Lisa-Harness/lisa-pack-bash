"""Persistent bash session management for Lisa.

Bash is a hard dependency: there is no shell without it, so a missing bash is
a hard error (see ``content/prompts/bash_missing.md``). The bwrap owner's
execution floor decides the posture; a missing floor never authorizes host execution.
"""

from __future__ import annotations

import asyncio
import fnmatch
import importlib
import json
import logging
import shlex
import shutil
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, cast

from kernel.capability_binding import ExecutionFloorView
from kernel.models import ToolExecutionContext
from sdk.identity import execution_scope_key

from lisa_packs.bash.priming import SessionPriming

__all__ = [
    "SANDBOX_FACTS_DIRNAME",
    "BashSession",
    "SessionResult",
    "bash_available",
    "resolve_or_create_session",
]

logger = logging.getLogger(__name__)

_BASH_BIN = "bash"
_SESSIONS: dict[str, BashSession] = {}

#: Directories that hold a real POSIX bash, searched when the one on PATH
#: turns out to be a launcher rather than a shell.
#:
#: On Windows, ``C:\Windows\System32\bash.exe`` is the WSL launcher: it
#: accepts no ``-c`` argument, so the pack's command composition cannot
#: drive it, and it reports a distribution error instead of running
#: anything. Git for Windows ships a real bash at ``<git>/bin/bash.exe``
#: and ``<git>/usr/bin/bash.exe``, which is the one the pack needs.
_POSIX_BASH_GLOBS: tuple[str, ...] = (
    "/usr/bin/bash",
    "/bin/bash",
    "/usr/local/bin/bash",
    r"C:\Program Files\Git\bin\bash.exe",
    r"C:\Program Files\Git\usr\bin\bash.exe",
    r"C:\Program Files (x86)\Git\bin\bash.exe",
)

#: Markers of a bash that is a launcher rather than a shell. The WSL
#: launcher lives in the system directory on Windows and prints a
#: distribution error rather than running the command it is given.
_LAUNCHER_MARKERS: tuple[str, ...] = (
    "system32",
    "syswow64",
)

# Commands composed through bwrap get these forwarded flags by default.
_DEFAULT_BWRAP_TIMEOUT = 120

#: Subdirectory of the host data dir where the pack publishes its per-session
#: sandbox *containment fact* for the host approval engine. The pack reports
#: the fact; the host owns the approve/ask/deny decision (pack never decides).
SANDBOX_FACTS_DIRNAME = "sandbox-facts.json"

#: Per-session execution state lives under ``<data_dir>/run/<session_key>/``
#: (the sdk.layout RUN category) — one gc-able tree per session holding job
#: artifacts, scratch, profile files, session logs, and the containment fact.
_RUN_DIR_NAME = "run"


def _is_launcher(path: str) -> bool:
    """True when ``path`` looks like a launcher rather than a real shell.

    The Windows WSL launcher lives in the system directory. It accepts no
    ``-c`` argument, so the pack cannot drive it, and it answers with a
    distribution error instead of running anything.
    """
    lowered = path.lower().replace("/", "\\")
    return any(marker in lowered for marker in _LAUNCHER_MARKERS)


def resolve_bash() -> str | None:
    """Return the path of a bash the pack can actually drive, or None.

    A launcher on PATH is not usable: it takes no ``-c`` argument, so the
    pack's composed command never runs and the output is a launcher error
    message rather than the command's own. When PATH resolves to such a
    launcher, the well-known locations of a real POSIX bash are searched
    instead — Git for Windows ships one.
    """
    on_path = shutil.which(_BASH_BIN)
    if on_path is not None and not _is_launcher(on_path):
        return on_path
    for candidate in _POSIX_BASH_GLOBS:
        if Path(candidate).is_file():
            return candidate
    return on_path


def bash_available() -> bool:
    """Return True when a usable bash binary is available.

    A bash that is only a launcher does not count: the pack composes
    ``bash -c <command>``, and a launcher does not accept that.
    """
    return resolve_bash() is not None


#: Default state-carryover policy: cwd and env always carry. Function-body
#: replay is opt-in via ``pack_config["session_state"]``:
#: ``{"functions": "off" | "allowlist" | "blocklist", "patterns": [...]}``.
#: ``allowlist`` replays only functions whose name matches a ``patterns``
#: fnmatch glob; ``blocklist`` replays everything except matches. Unknown
#: keys on the pack_config override are ignored so this remains a stable
#: extension point.
_DEFAULT_SESSION_STATE = {
    "cwd": True,
    "env": True,
    "functions": "off",
    "patterns": [],
}

_FUNCTIONS_MODES = ("off", "allowlist", "blocklist")

#: Header line of every host-written state file (also used to detect it).
_STATE_HEADER = "# Auto-managed by lisa bash pack. Do not edit."
#: Section marker introducing the function-replay part of the state file.
#: ``apply_cwd`` never edits lines at-or-below this marker, so a function
#: body that itself contains a ``cd`` statement survives cwd adoption.
_FUNCTIONS_SECTION = "# === lisa function replay ==="


def _session_state_policy(ctx: ToolExecutionContext) -> dict[str, Any]:
    """Resolve the session-state carryover policy for this ctx.

    Floor default: cwd+env always carry, functions do not replay. The pack
    config may enable function replay (``functions`` mode + ``patterns``)
    and may tighten cwd/env to off. Unknown keys are ignored so the policy
    is a stable extension point.
    """
    policy = dict(_DEFAULT_SESSION_STATE)
    pack_config = getattr(ctx, "pack_config", None)
    if not isinstance(pack_config, Mapping):
        return policy
    override = pack_config.get("session_state")
    if not isinstance(override, Mapping):
        return policy
    if override.get("cwd") is False:
        policy["cwd"] = False
    if override.get("env") is False:
        policy["env"] = False
    functions_mode = override.get("functions")
    if functions_mode in _FUNCTIONS_MODES:
        policy["functions"] = functions_mode
    patterns = override.get("patterns")
    if isinstance(patterns, str):
        policy["patterns"] = [patterns]
    elif isinstance(patterns, list) and all(isinstance(p, str) for p in patterns):
        policy["patterns"] = list(patterns)
    return policy


class SessionResult(dict[str, Any]):
    """Structured per-command result (a dict to keep ToolResult json simple)."""


def _session_key(ctx: ToolExecutionContext) -> str:
    return execution_scope_key(ctx)


def _sandbox_available() -> bool:
    """True when bwrap_run is registered (pack installed + importable)."""
    try:
        from kernel.tools.registry import get_tool_catalog
    except Exception:  # pragma: no cover - kernel always present at runtime
        return False
    # The catalog drops tools whose implementation module fails to import, so
    # presence here means bwrap is actually runnable in this process.
    return "bwrap_run" in get_tool_catalog()


def _bwrap_floor(ctx: ToolExecutionContext) -> ExecutionFloorView | None:
    floor_for = ctx.execution_floor_for
    return floor_for("bwrap_run") if floor_for is not None else None


def _floor_requires_sandbox(ctx: ToolExecutionContext) -> bool:
    """Read the harness floor's execution posture for the shell.

    Only an explicit bare floor from the bwrap owner permits host execution.
    A missing floor is not evidence of consent to run bare.
    """
    floor = _bwrap_floor(ctx)
    return floor is None or floor.execution != "bare"


class SandboxRequiredButUnavailable(RuntimeError):
    """The floor mandates sandboxed execution but the bwrap pack cannot run."""

    def __init__(self, reasons: list[str]) -> None:
        self.reasons = list(reasons)
        detail = "; ".join(reasons) if reasons else "the bwrap pack is not installed"
        super().__init__(
            "The sandbox floor for this session requires contained execution "
            f"(execution=sandbox), but the sandbox cannot run: {detail}. "
            "Running unsandboxed would silently violate the harness policy, so "
            "this shell is unavailable until the sandbox is fixed or the floor "
            "is deliberately lowered. See bwrap_missing.md."
        )


class _BwrapCapabilities(Protocol):
    """Structural view of the bwrap pack's capability report."""

    missing: Sequence[str]


class _BwrapProbeModule(Protocol):
    """Structural view of ``lisa_packs.bwrap.probe``'s public surface."""

    detect_capabilities: Callable[..., _BwrapCapabilities]


def _sandbox_degraded() -> list[str]:
    """Report sandbox gaps when the pack is registered but the host cannot run it.

    Returns a list of reasons. Empty means fully functional; an empty list when
    ``_sandbox_available()`` is False means the bwrap pack itself is missing.
    The bwrap probe performs a real namespaced run, so a present binary with
    disabled user namespaces never gets reported as contained.
    """
    if not _sandbox_available():
        return []
    try:
        module = importlib.import_module("lisa_packs.bwrap.probe")
    except ImportError:  # pragma: no cover - bwrap pack always present when registered
        return ["bubblewrap capability probe is unavailable"]
    probe = cast(_BwrapProbeModule, module)
    return list(probe.detect_capabilities().missing)


class BashSession:
    """One Lisa session's shell: priming, logging, state, and execution."""

    def __init__(
        self,
        *,
        session_key: str,
        data_dir: Path,
        priming: SessionPriming,
        sandboxed: bool,
        initial_env: frozenset[str] | None = None,
        initial_funcs: frozenset[str] | None = None,
        state_policy: dict[str, Any] | None = None,
    ) -> None:
        self.session_key = session_key
        self.data_dir = data_dir
        self.priming = priming
        self.sandboxed = sandboxed
        self._rc: object | None = None
        # Job registry: backgrounded commands tracked by id. Values are
        # host-side asyncio.Tasks; artifacts live under jobs_dir.
        self.jobs: dict[str, asyncio.Task[Any]] = {}
        # Baseline: what the rc already exports/defines before the model ever
        # runs anything. Carryover deltas are computed against these so the
        # rc's own exports (LANG, TERM, NO_COLOR…) and functions aren't
        # treated as state to replay.
        self._base_env: frozenset[str] = initial_env or frozenset()
        self._base_funcs: frozenset[str] = initial_funcs or frozenset()
        self._state_policy: dict[str, Any] = dict(_DEFAULT_SESSION_STATE, **(state_policy or {}))

    @property
    def jobs_dir(self) -> Path:
        """Directory for job artifacts and spill files (host-visible)."""
        return self.data_dir / _RUN_DIR_NAME / self.session_key / "jobs"

    def _select_functions(self, functions: dict[str, str]) -> dict[str, str]:
        """Apply the functions policy (off / allowlist / blocklist + patterns)."""
        mode = self._state_policy.get("functions", "off")
        if mode == "off" or not functions:
            return {}
        patterns = self._state_policy.get("patterns") or []

        def _matches(name: str) -> bool:
            return any(fnmatch.fnmatch(name, pattern) for pattern in patterns)

        if mode == "allowlist":
            return {n: b for n, b in functions.items() if _matches(n)}
        return {n: b for n, b in functions.items() if not _matches(n)}

    def record_post_run_state(
        self,
        *,
        cwd: str,
        exported: dict[str, str],
        functions: dict[str, str] | None = None,
    ) -> None:
        """Persist the per-call state capture to the replay file.

        The host (not the shell in-sandbox) writes this file, then the next
        rc source replays it. Only the delta against the baseline is replayed:
        rc-managed exports (LANG, TERM…) and rc-defined functions are excluded
        so a second source of the state file cannot clobber the rc's own
        definitions. Function bodies replay per the session policy (off by
        default; allowlist/blocklist + fnmatch patterns via pack_config).
        """
        lines: list[str] = [_STATE_HEADER]
        if self._state_policy["cwd"] and cwd and cwd != ".":
            lines.append(f"cd {shlex.quote(cwd)}")
        if self._state_policy["env"]:
            for name, value in sorted(exported.items()):
                if name and name not in self._base_env:
                    lines.append(f"export {name}={shlex.quote(value)}")
        selected = self._select_functions(
            {
                name: body
                for name, body in (functions or {}).items()
                if name and name not in self._base_funcs
            }
        )
        if selected:
            lines.append(_FUNCTIONS_SECTION)
            for name in sorted(selected):
                body = (selected[name] or "").rstrip()
                if body:
                    lines.append(body)
        path = self.priming.state_path
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".sh.tmp")
            tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
            tmp.replace(path)
        except OSError as exc:
            logger.debug("bash: state file write failed (%s)", exc)

    def job_artifacts(self, job_id: str) -> tuple[Path, Path, Path]:
        """(log, rc, status) paths for one job under jobs_dir."""
        base = self.jobs_dir / job_id
        return (
            base.with_suffix(".log"),
            base.with_suffix(".rc"),
            base.with_suffix(".status.json"),
        )

    def register_job(self, job_id: str, task: asyncio.Task[Any]) -> None:
        """Add a running background job and persist a ``running`` status file."""
        self.jobs[job_id] = task
        log_path, rc_path, status_path = self.job_artifacts(job_id)
        self._write_job_status(
            status_path,
            {
                "job_id": job_id,
                "state": "running",
                "started_at": datetime.now(UTC).isoformat(),
                "log_path": str(log_path),
                "rc_path": str(rc_path),
            },
        )

    def complete_job(self, job_id: str, *, rc: int | None, killed: bool) -> None:
        """Persist a finished job's status and drop it from the running set."""
        log_path, rc_path, status_path = self.job_artifacts(job_id)
        self._write_job_status(
            status_path,
            {
                "job_id": job_id,
                "state": "killed" if killed else "done",
                "rc": rc,
                "ended_at": datetime.now(UTC).isoformat(),
                "log_path": str(log_path),
                "rc_path": str(rc_path),
            },
        )
        self.jobs.pop(job_id, None)

    def _write_job_status(self, status_path: Path, payload: dict[str, Any]) -> None:
        try:
            status_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = status_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload), encoding="utf-8")
            tmp.replace(status_path)
        except OSError as exc:
            logger.debug("bash: job status write failed (%s)", exc)

    def apply_cwd(self, cwd: str) -> None:
        """Replace only the ``cd`` line in the state file, keeping the rest.

        Used after a batch: env/function deltas from children are discarded,
        but a cwd every child agreed on is adopted. Lines in the function
        section are never touched — a body may legitimately contain a ``cd``.
        """
        if not self._state_policy["cwd"] or not cwd or cwd == ".":
            return
        path = self.priming.state_path
        try:
            existing = path.read_text(encoding="utf-8") if path.is_file() else _STATE_HEADER + "\n"
            lines = existing.splitlines()
            split_at = (
                lines.index(_FUNCTIONS_SECTION) if _FUNCTIONS_SECTION in lines else len(lines)
            )
            head = [ln for ln in lines[:split_at] if not ln.startswith("cd ")]
            if not head:
                head = [_STATE_HEADER]
            head.insert(1, f"cd {shlex.quote(cwd)}")
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".sh.tmp")
            tmp.write_text("\n".join(head + lines[split_at:]) + "\n", encoding="utf-8")
            tmp.replace(path)
        except OSError as exc:
            logger.debug("bash: cwd apply failed (%s)", exc)

    def jobs_summary(self) -> list[dict[str, Any]]:
        """Snapshot of running jobs for embedding in a result's background_jobs."""
        out: list[dict[str, Any]] = []
        for job_id, task in self.jobs.items():
            out.append(
                {
                    "job_id": job_id,
                    "done": task.done(),
                    "log_path": str(self.job_artifacts(job_id)[0]),
                }
            )
        return out

    def ensure_primed(self) -> None:
        """Render the rc once per session."""
        if self._rc is None:
            self._rc = self.priming.render()

    @property
    def log_path(self) -> Path:
        return self.priming.log_path

    @property
    def sentinel(self) -> str:
        return self.priming.sentinel

    def build_inner_argv(self, command: str) -> list[str]:
        """Build the bash argv that replays the rc + state and runs the command.

        ``--rcfile`` is not honored by ``bash -c``, so the rc is sourced
        explicitly at the top of the wrapper. The rc in turn sources the
        state file (cwd, exported vars, and policy-permitted function bodies
        from the prior call), then the model's command runs, then trailing
        printf state-emitters capture rc + PIPESTATUS + new exports
        (delta-filtered) and function bodies (base64, one line each) for the
        next call; the host applies the carryover policy.
        """
        self.ensure_primed()
        rc_path = self.priming.rc_path
        marker = f"{self.sentinel.strip()}rc="
        state_marker = f"{self.sentinel.strip()}state="
        wrapped = (
            # rc is sourced explicitly because `bash -c` ignores --rcfile.
            f". {shlex.quote(str(rc_path))}; "
            f"{command}\n"
            # `$?` and `${PIPESTATUS[*]}` must be captured in ONE assignment -
            # any intermediate command resets one of them. We bundle them into
            # a single var via `rc,pipe1 pipe2...` and split on the first comma.
            # Leading \\n keeps the marker parseable when output lacks a final newline.
            "__lisa_rcpipe=$?,$(printf '%s ' \"${PIPESTATUS[@]}\") ; "
            "__lisa_rc=${__lisa_rcpipe%%,*}; "
            "__lisa_pipe=${__lisa_rcpipe#*,}; "
            f'printf \'\\n{marker}%s pipe=%s\\n\' "$__lisa_rc" "$__lisa_pipe"; '
            # A separate state line powers session-wide carryover: cwd plus
            # each new export on its own line (so values containing spaces
            # cannot break the parser). Function bodies are emitted as base64
            # on ONE line each so multi-line bodies cannot break the
            # line-oriented parser; the host decodes, policy-filters, and
            # replays them via the state file. The host delta-filters against
            # the rc baseline, so rc-managed exports (LANG, TERM…) are never
            # replayed.
            f"printf '%s\\n' \"{state_marker}cwd=$(printf '%q' \"$PWD\")\"; "
            "if command -v base64 >/dev/null 2>&1; then "
            "compgen -A function 2>/dev/null | while IFS= read -r __lisa_fn; do "
            '[ -n "$__lisa_fn" ] || continue; '
            f'printf \'%sfunc %s %s\\n\' "{self.sentinel.strip()}" "$__lisa_fn" '
            '"$(declare -f "$__lisa_fn" | base64 | tr -d \'\\n\')"; '
            "done; fi; "
            "compgen -A export 2>/dev/null | while IFS= read -r n; "
            f"do v=\"${{!n}}\"; printf '%svar %s=%q\\n' "
            f'"{self.sentinel.strip()}" "$n" "$v"; done; '
            'exit "$__lisa_rc"'
        )
        return [
            resolve_bash() or _BASH_BIN,
            "--noprofile",
            "-c",
            wrapped,
        ]


def _containment_current(
    ctx: ToolExecutionContext | None = None,
) -> tuple[str, list[str]]:
    """Compute the live containment state from current environment probes.

    - ``contained``        - registered and runnable (commands compose via bwrap).
    - ``degraded``         - registered but cannot run (binary missing, userns
      disabled). Reasons explain the gap to the operator.
    - ``deliberate_bare``  - the harness floor permits bare and no tightening
      request is in force: commands run on the host BY CHOICE, distinct from
      an unexpected gap. The approval engine treats this as not-contained
      (fail closed) so bare execution still prompts.
    - ``none``             - no sandbox registered at all; commands run on the host.
    """
    if ctx is not None and _floor_requires_sandbox(ctx) is False:
        return "deliberate_bare", []
    if _sandbox_available():
        reasons = _sandbox_degraded()
        if reasons:
            return "degraded", reasons
        return "contained", []
    return "none", ["sandbox pack not installed"]


def _floor_posture_payload(floor: ExecutionFloorView | None) -> dict[str, Any]:
    """Extract the floor's execution/persistence axes for the containment fact.

    Only owner-projected execution axes are surfaced to the approval engine.
    """
    if floor is None:
        return {}
    out: dict[str, Any] = {}
    if floor.execution in ("bare", "sandbox"):
        out["execution"] = floor.execution
    persistence = floor.persistence
    if isinstance(persistence, str) and persistence in (
        "direct-write",
        "overlay",
        "ephemeral",
        "readonly",
    ):
        out["persistence"] = persistence
    captured = sorted(str(path) for path, access in floor.paths.items() if access == "capture")
    if captured:
        out["capture_paths"] = captured
    return out


def _write_containment_fact(session: BashSession, ctx: ToolExecutionContext | None = None) -> None:
    """Publish this session's containment fact for the host approval engine.

    Writes ``<data_dir>/sandbox-facts/<session_key>.json`` atomically so the
    policy engine can read it at approval time WITHOUT importing pack code —
    the pack reports the fact; the host owns the approve/ask/deny decision. The
    host recomputes the session key from the approval identity (same four
    inputs), so the file is the only coupling between pack and host on this
    axis. Never raises: a failure to publish must not block a shell session
    (the engine fails closed to a prompt when the fact is absent or unreadable).

    ``ctx`` carries the harness floor: a bare floor flips the reported state to
    ``deliberate_bare`` so the engine never auto-approves host execution off a
    stale ``contained`` fact.
    """
    state, reasons = _containment_current(ctx)
    payload: dict[str, Any] = {
        "tool_id": "bash_run",
        "containment": state,
        "reasons": reasons,
        "probed_at": datetime.now(UTC).isoformat(),
    }
    # Additive posture fields (hosts ignore unknown keys): the harness floor's
    # authored execution/persistence axes, so the approval engine can tailor
    # prompts - e.g. reserve prompting for the merge step when the floor pins
    # overlay persistence (nothing lands on the live filesystem). Absent
    # axes stay absent, never guessed.
    try:
        posture = _floor_posture_payload(_bwrap_floor(ctx)) if ctx is not None else {}
        if posture:
            payload["floor_posture"] = posture
    except Exception:  # pylint: disable=broad-except  # the fact must never fail to publish
        pass
    path = session.data_dir / _RUN_DIR_NAME / session.session_key / SANDBOX_FACTS_DIRNAME
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        tmp.replace(path)
    except OSError as exc:  # pragma: no cover - logging only, non-fatal
        logger.debug("bash: containment fact write failed (%s)", exc)


def _baseline_snapshot(rc_content: str) -> tuple[frozenset[str], frozenset[str]]:
    """Run the rendered rc host-side once; capture its export + function sets.

    One extra subprocess per session is a cheap price for a delta that
    excludes the rc's own exports and functions from replay. Falls back to
    empty sets on failure — the session still works, it just replays more
    than strictly needed.
    """
    probe = (
        rc_content
        + '\nprintf "__ENV__\\n"; compgen -A export 2>/dev/null\n'
        + 'printf "__FUNCS__\\n"; compgen -A function 2>/dev/null\n'
    )
    try:
        import subprocess

        result = subprocess.run(
            [resolve_bash() or _BASH_BIN, "--noprofile", "-c", probe],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return frozenset(), frozenset()
    env: set[str] = set()
    funcs: set[str] = set()
    section = ""
    for line in result.stdout.splitlines():
        if line == "__ENV__":
            section = "env"
            continue
        if line == "__FUNCS__":
            section = "funcs"
            continue
        if section == "env":
            env.add(line.strip())
        elif section == "funcs":
            funcs.add(line.strip())
    env.discard("")
    funcs.discard("")
    return frozenset(env), frozenset(funcs)


def resolve_or_create_session(
    ctx: ToolExecutionContext,
    *,
    sandboxed: bool | None = None,
) -> BashSession:
    """Return (creating if needed) the session for this ctx identity.

    ``sandboxed`` may be forced by tests; when None it is detected from the
    tool catalog. When explicitly passed on a cached session, the posture is
    updated in place: the per-call decision (bare floor without tightening vs.
    a tightened sandboxed run) must not be pinned to whichever posture happened
    to create the session.
    """
    key = _session_key(ctx)
    session = _SESSIONS.get(key)
    if session is not None:
        if sandboxed is not None:
            session.sandboxed = sandboxed
        _write_containment_fact(session, ctx)
        return session

    data_dir = Path(ctx.data_dir) if ctx.data_dir else Path.home() / ".lisa"
    priming = SessionPriming(data_dir=data_dir, session_key=key)
    rc_content = priming.render()
    env, funcs = _baseline_snapshot(rc_content.path.read_text(encoding="utf-8"))
    session = BashSession(
        session_key=key,
        data_dir=data_dir,
        priming=priming,
        sandboxed=_sandbox_available() if sandboxed is None else sandboxed,
        initial_env=env,
        initial_funcs=funcs,
        state_policy=_session_state_policy(ctx),
    )
    _SESSIONS[key] = session
    _write_containment_fact(session, ctx)
    return session


def _reset_sessions() -> None:
    """Test hook: drop all cached sessions."""
    _SESSIONS.clear()
