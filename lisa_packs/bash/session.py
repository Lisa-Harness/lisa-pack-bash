"""Persistent bash session management for Lisa.

Bash is a hard dependency: there is no shell without it, so a missing bash is
a hard error (see ``content/prompts/bash_missing.md``). When the bwrap pack is
available, each command is composed through the sandbox via a child
ToolExecutionContext (tool-to-tool passthrough); otherwise commands run on the
host and every result is stamped ``sandboxed: false`` (visible, not silent).
"""

from __future__ import annotations

import asyncio
import fnmatch
import hashlib
import json
import logging
import shlex
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from kernel.models import ToolExecutionContext

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

# Commands composed through bwrap get these forwarded flags by default.
_DEFAULT_BWRAP_TIMEOUT = 120

#: Subdirectory of the host data dir where the pack publishes its per-session
#: sandbox *containment fact* for the host approval engine. The pack reports
#: the fact; the host owns the approve/ask/deny decision (pack never decides).
SANDBOX_FACTS_DIRNAME = "sandbox-facts"


def bash_available() -> bool:
    """Return True when a bash binary is on PATH."""
    return shutil.which(_BASH_BIN) is not None


#: Default state-carryover policy: cwd and env always carry; functions are
#: opt-in via `pack_config["session_state"]["functions"]` (the harness is the
#: policy owner; the model cannot forge it).
_DEFAULT_SESSION_STATE = {
    "cwd": True,
    "env": True,
    "functions": "off",
    "function_allow": [],
    "function_block": [],
}


def _session_state_policy(ctx: ToolExecutionContext) -> dict[str, Any]:
    """Resolve the session-state carryover policy for this ctx.

    Floor default: cwd+env always carry. Functions are off unless the
    harness opts in via ``session_state.functions`` (``"allowlist"`` /
    ``"blocklist"`` plus ``function_allow``/``function_block`` globs).
    Unknown keys are ignored so the policy is a stable extension point.
    """
    policy = dict(_DEFAULT_SESSION_STATE)
    pack_config = getattr(ctx, "pack_config", None)
    if not isinstance(pack_config, dict):
        return policy
    override = pack_config.get("session_state")
    if not isinstance(override, dict):
        return policy
    if override.get("cwd") is False:
        policy["cwd"] = False
    if override.get("env") is False:
        policy["env"] = False
    functions = override.get("functions")
    if functions in ("allowlist", "blocklist", "off"):
        policy["functions"] = functions
    for key in ("function_allow", "function_block"):
        value = override.get(key)
        if isinstance(value, list):
            policy[key] = [p for p in value if isinstance(p, str)]
    return policy


class SessionResult(dict):
    """Structured per-command result (a dict to keep ToolResult json simple)."""


def _session_key(ctx: ToolExecutionContext) -> str:
    raw = (
        "/".join(
            part
            for part in (
                ctx.workspace_id,
                ctx.user_slug,
                ctx.session_plan_digest,
                ctx.surface_id,
            )
            if part
        )
        or "default"
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _sandbox_available() -> bool:
    """True when bwrap_run is registered (pack installed + importable)."""
    try:
        from kernel.tools.registry import get_tool_catalog
    except Exception:  # pragma: no cover - kernel always present at runtime
        return False
    # The catalog drops tools whose implementation module fails to import, so
    # presence here means bwrap is actually runnable in this process.
    return "bwrap_run" in get_tool_catalog()


def _floor_requires_sandbox(ctx: ToolExecutionContext) -> bool | None:
    """Read the harness floor's execution posture for this pack.

    Returns True when the harness floor mandates the sandbox (the model or fate
    may never run bare), False when the floor deliberately permits bare, and
    None when the floor is unset/unreadable (fall back to availability). The
    floor is trusted session configuration - the lisa kernel populates
    ``pack_config`` from ``resources.packs.configuration`` and model
    ``parameters`` cannot forge it. bwrap is intentionally not imported (pack
    isolation forbids cross-pack imports), so the policy is parsed locally with
    no dependency on the bwrap pack.
    """
    pack_config = getattr(ctx, "pack_config", None)
    if not isinstance(pack_config, dict):
        return None
    sandbox = pack_config.get("sandbox")
    if not isinstance(sandbox, dict):
        return None
    floor = sandbox.get("floor")
    if not isinstance(floor, dict):
        return None
    execution = floor.get("execution")
    if execution == "sandbox":
        return True
    if execution == "bare":
        return False
    return None


class SandboxRequiredButUnavailable(RuntimeError):  # noqa: N818 - not a tool error
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
        from lisa_packs.bwrap.probe import detect_capabilities
    except ImportError:  # pragma: no cover - bwrap pack always present when registered
        return ["bubblewrap capability probe is unavailable"]
    return list(detect_capabilities().missing)


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
        self.jobs: dict[str, asyncio.Task] = {}
        # Baseline: what the rc already exports/defines before the model ever
        # runs anything. A carryover delta is computed against this so the rc's
        # own exports (LANG, TERM, NO_COLOR…) aren't treated as state to replay.
        self._base_env: frozenset[str] = initial_env or frozenset()
        self._base_funcs: frozenset[str] = initial_funcs or frozenset()
        self._state_policy: dict[str, Any] = dict(
            _DEFAULT_SESSION_STATE, **(state_policy or {})
        )

    @property
    def jobs_dir(self) -> Path:
        """Directory for job artifacts and spill files (host-visible)."""
        return self.data_dir / "jobs" / self.session_key

    def record_post_run_state(
        self,
        *,
        cwd: str,
        exported: dict[str, str],
        func_names: list[str],
    ) -> None:
        """Persist the per-call state capture to the replay file.

        The host (not the shell in-sandbox) writes this file, then the next
        rc source replays it. Only the delta against the baseline is replayed:
        rc-managed exports (LANG, TERM…) are excluded so a second source of
        the state file cannot clobber the rc's own exports.
        """
        lines: list[str] = ["# Auto-managed by lisa bash pack. Do not edit."]
        if self._state_policy["cwd"] and cwd and cwd != ".":
            lines.append(f"cd {shlex.quote(cwd)}")
        if self._state_policy["env"]:
            for name, value in sorted(exported.items()):
                if name and name not in self._base_env:
                    lines.append(f"export {name}={shlex.quote(value)}")
        if self._state_policy["functions"] != "off":
            allow: list[str] = self._state_policy["function_allow"]
            block: list[str] = self._state_policy["function_block"]
            for name in func_names:
                if (
                    name
                    and name not in self._base_funcs
                    and self._fn_included(name, allow, block)
                ):
                    # Functions are marked for re-definition via a comment; the
                    # actual bodies are re-sourced from the delta below.
                    lines.append(f"# carry:func:{name}")
        path = self.priming.state_path
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".sh.tmp")
            tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
            tmp.replace(path)
        except OSError as exc:
            logger.debug("bash: state file write failed (%s)", exc)

    @staticmethod
    def _fn_included(name: str, allow: list[str], block: list[str]) -> bool:
        for pattern in block:
            if fnmatch.fnmatch(name, pattern):
                return False
        if not allow:
            return True
        return any(fnmatch.fnmatch(name, pattern) for pattern in allow)

    def job_artifacts(self, job_id: str) -> tuple[Path, Path, Path]:
        """(log, rc, status) paths for one job under jobs_dir."""
        base = self.jobs_dir / job_id
        return (
            base.with_suffix(".log"),
            base.with_suffix(".rc"),
            base.with_suffix(".status.json"),
        )

    def register_job(self, job_id: str, task: "asyncio.Task[Any]") -> None:
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
        """Replace only the ``cd`` line in the state file, keeping env/functions.

        Used after a batch: env/function deltas from children are discarded,
        but a cwd every child agreed on is adopted.
        """
        if not self._state_policy["cwd"] or not cwd or cwd == ".":
            return
        path = self.priming.state_path
        try:
            existing = (
                path.read_text(encoding="utf-8")
                if path.is_file()
                else "# Auto-managed by lisa bash pack. Do not edit.\n"
            )
            lines = [ln for ln in existing.splitlines() if not ln.startswith("cd ")]
            if not lines:
                lines = ["# Auto-managed by lisa bash pack. Do not edit."]
            lines.append(f"cd {shlex.quote(cwd)}")
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".sh.tmp")
            tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
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
        state file (cwd, exported vars, functions from the prior call), then
        the model's command runs, then trailing printf state-emitters capture
        rc + PIPESTATUS + new exports (delta-filtered) for the next call.
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
            '__lisa_rc=${__lisa_rcpipe%%,*}; '
            '__lisa_pipe=${__lisa_rcpipe#*,}; '
            f"printf '\\n{marker}%s pipe=%s\\n' \"$__lisa_rc\" \"$__lisa_pipe\"; "
            # A separate state line powers session-wide carryover: cwd plus
            # each new export on its own indented continuation line (so values
            # containing spaces cannot break the parser), then function names.
            # The host delta-filters against the rc baseline, so rc-managed
            # exports (LANG, TERM…) are never replayed.
            f'printf \'%s\\n\' "{state_marker}cwd=$(printf \'%q\' "$PWD")"; '
            'compgen -A export 2>/dev/null | while IFS= read -r n; '
            f'do v="${{!n}}"; printf \'%svar %s=%q\\n\' '
            f'"{self.sentinel.strip()}" "$n" "$v"; done; '
            'funcs=$(compgen -A function 2>/dev/null | tr \'\\n\' \' \'); '
            f'printf \'%sfuncs %s\\n\' "{self.sentinel.strip()}" "$funcs"; '
            'exit "$__lisa_rc"'
        )
        return [
            _BASH_BIN,
            "--noprofile",
            "-c",
            wrapped,
        ]


def _containment_current() -> tuple[str, list[str]]:
    """Compute the live containment state from current environment probes.

    - ``contained``        - registered and runnable (commands compose via bwrap).
    - ``degraded``         - registered but cannot run (binary missing, userns
      disabled). Reasons explain the gap to the operator.
    - ``deliberate_bare``  - the harness floor permits bare and the sandbox is
      unavailable; a CHOSEN posture, distinct from an unexpected gap.
    - ``none``             - no sandbox registered at all; commands run on the host.
    """
    if _sandbox_available():
        reasons = _sandbox_degraded()
        if reasons:
            return "degraded", reasons
        return "contained", []
    return "none", ["sandbox pack not installed"]


def _write_containment_fact(session: BashSession) -> None:
    """Publish this session's containment fact for the host approval engine.

    Writes ``<data_dir>/sandbox-facts/<session_key>.json`` atomically so the
    policy engine can read it at approval time WITHOUT importing pack code —
    the pack reports the fact, the host owns the decision. The host recomputes
    the session key from the approval identity (same four inputs), so the file
    is the only coupling between pack and host on this axis. Never raises: a
    failure to publish must not block a shell session (the engine fails closed
    to a prompt when the fact is absent or unreadable).
    """
    state, reasons = _containment_current()
    payload = {
        "tool_id": "bash_run",
        "containment": state,
        "reasons": reasons,
        "probed_at": datetime.now(UTC).isoformat(),
    }
    path = session.data_dir / SANDBOX_FACTS_DIRNAME / f"{session.session_key}.json"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        tmp.replace(path)
    except OSError as exc:  # pragma: no cover - logging only, non-fatal
        logger.debug("bash: containment fact write failed (%s)", exc)


def _baseline_snapshot(rc_content: str) -> tuple[frozenset[str], frozenset[str]]:
    """Run the rendered rc host-side once and capture its export/function set.

    One extra subprocess per session is a cheap price for a delta that
    excludes the rc's own exports from replay. Falls back to empty sets on
    failure — the session still works, it just replays more than strictly
    needed.
    """
    probe = (
        rc_content
        + '\nprintf "__ENV__\\n"; compgen -A export 2>/dev/null'
        + '\nprintf "__FUNCS__\\n"; compgen -A function 2>/dev/null\n'
    )
    try:
        import subprocess

        result = subprocess.run(
            [_BASH_BIN, "--noprofile", "-c", probe],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return frozenset(), frozenset()
    env: set[str] = set()
    funcs: set[str] = set()
    section: str | None = None
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
    tool catalog.
    """
    key = _session_key(ctx)
    session = _SESSIONS.get(key)
    if session is not None:
        _write_containment_fact(session)
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
    _write_containment_fact(session)
    return session


def _reset_sessions() -> None:
    """Test hook: drop all cached sessions."""
    _SESSIONS.clear()
