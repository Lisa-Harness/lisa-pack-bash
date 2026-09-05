"""Persistent bash session management for Lisa.

Bash is a hard dependency: there is no shell without it, so a missing bash is
a hard error (see ``content/prompts/bash_missing.md``). When the bwrap pack is
available, each command is composed through the sandbox via a child
ToolExecutionContext (tool-to-tool passthrough); otherwise commands run on the
host and every result is stamped ``sandboxed: false`` (visible, not silent).
"""

from __future__ import annotations

import hashlib
import shlex
import shutil
from pathlib import Path

from kernel.models import ToolExecutionContext

from lisa_packs.bash.priming import SessionPriming

__all__ = ["BashSession", "SessionResult", "resolve_or_create_session", "bash_available"]

_BASH_BIN = "bash"
_SESSIONS: dict[str, "BashSession"] = {}

# Commands composed through bwrap get these forwarded flags by default.
_DEFAULT_BWRAP_TIMEOUT = 120


def bash_available() -> bool:
    """Return True when a bash binary is on PATH."""
    return shutil.which(_BASH_BIN) is not None


class SessionResult(dict):
    """Structured per-command result (a dict to keep ToolResult json simple)."""


def _session_key(ctx: ToolExecutionContext) -> str:
    raw = "/".join(
        part
        for part in (ctx.workspace_id, ctx.user_slug, ctx.session_plan_digest, ctx.surface_id)
        if part
    ) or "default"
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


def _sandbox_degraded() -> list[str]:
    """Report sandbox gaps when the pack is registered but the host cannot run it.

    Returns a list of reasons. Empty means fully functional; an empty list when
    ``_sandbox_available()`` is False means the bwrap pack itself is missing.
    Probing a real namespaced run here would be redundant - ``bwrap_run``
    re-probes on every call and reports failures explicitly (never silently).
    """
    if not _sandbox_available():
        return []
    try:
        from lisa_packs.bwrap.probe import bwrap_binary
    except Exception:  # pragma: no cover - bwrap pack always present when registered
        return []
    if bwrap_binary() is None:
        return ["bubblewrap (bwrap) binary not found on PATH"]
    return []


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
    return session


def _reset_sessions() -> None:
    """Test hook: drop all cached sessions."""
    _SESSIONS.clear()
