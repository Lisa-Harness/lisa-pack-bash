"""Persistent bash session management for Lisa.

Bash is a hard dependency: there is no shell without it, so a missing bash is
a hard error (see ``content/prompts/bash_missing.md``). When the bwrap pack is
available, each command is composed through the sandbox via a child
ToolExecutionContext (tool-to-tool passthrough); otherwise commands run on the
host and every result is stamped ``sandboxed: false`` (visible, not silent).
"""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
from datetime import UTC, datetime
from pathlib import Path

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
    """One Lisa session's shell: priming, logging, and command execution."""

    def __init__(
        self,
        *,
        session_key: str,
        data_dir: Path,
        priming: SessionPriming,
        sandboxed: bool,
    ) -> None:
        self.session_key = session_key
        self.data_dir = data_dir
        self.priming = priming
        self.sandboxed = sandboxed
        self._rc: object | None = None

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
        """Build the bash argv that sources the rc and runs the command.

        Uses ``--rcfile`` so the priming profile (env hygiene, locale, log
        path) is applied deterministically. Under ``bash -c`` prompts never
        render, so the rc's PS1/PROMPT_COMMAND sentinel cannot fire on its
        own; the command is therefore wrapped to emit the rc marker
        explicitly and exit with the command's status so the executor can
        recover a truthful return code.
        """
        self.ensure_primed()
        rc_path = self.priming.rc_path
        marker = f"{self.sentinel.strip()}rc="
        wrapped = (
            f"{command}\n"
            # Leading \n keeps the marker on its own line even when the
            # command's output has no trailing newline, so the parser never
            # concatenates (and drops) the final output line.
            f'__lisa_rc=$?; printf \'\\n%s\\n\' "{marker}$__lisa_rc"; exit "$__lisa_rc"'
        )
        return [
            _BASH_BIN,
            "--noprofile",
            "--rcfile",
            str(rc_path),
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
        # Refresh the fact on every reuse so a mid-session sandbox flip (or a
        # stale cache) is reflected the next time the approval engine reads it.
        _write_containment_fact(session)
        return session

    data_dir = Path(ctx.data_dir) if ctx.data_dir else Path.home() / ".lisa"
    priming = SessionPriming(data_dir=data_dir, session_key=key)
    session = BashSession(
        session_key=key,
        data_dir=data_dir,
        priming=priming,
        sandboxed=_sandbox_available() if sandboxed is None else sandboxed,
    )
    _SESSIONS[key] = session
    _write_containment_fact(session)
    return session


def _reset_sessions() -> None:
    """Test hook: drop all cached sessions."""
    _SESSIONS.clear()
