"""Render the agent-optimized bash rc for one Lisa session.

The rc is an authored template under ``content/profile/lisa.rc.tmpl``
(pack-content rule: no embedded markdown/shell prose in .py). We substitute a
unique sentinel, the per-session log path, and the state-file path, then write
it under the host data dir for the session launcher to source. The state file
is rendered lazily by the session layer once the harness post-command state
is known.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from pathlib import Path

from lisa_packs.bash import get_content_root

__all__ = ["RenderedRc", "render_rc", "SessionPriming"]

_TEMPLATE_RELATIVE = Path("content") / "profile" / "lisa.rc.tmpl"


@dataclass(frozen=True, slots=True)
class RenderedRc:
    """A fully-substituted rc file ready to source."""

    path: Path
    sentinel: str


def new_sentinel() -> str:
    """Return a unique per-session sentinel value."""
    return f"LISA_{secrets.token_hex(8)}>> "


def render_rc(
    *,
    sentinel: str,
    log_path: Path,
    state_path: Path,
    lang: str = "C.UTF-8",
    template_dir: Path | None = None,
) -> str:
    """Render the rc content with per-session substitutions."""
    template_path = (template_dir or get_content_root()) / _TEMPLATE_RELATIVE
    if not template_path.is_file():
        raise RuntimeError(f"bash pack missing rc template: {template_path}")
    body = template_path.read_text(encoding="utf-8")
    return (
        body.replace("%%SENTINEL%%", sentinel)
        .replace("%%LOG_PATH%%", str(log_path))
        .replace("%%STATE_FILE%%", str(state_path))
        .replace("%%LANG%%", lang)
    )


class SessionPriming:
    """Owns rc rendering + on-disk placement for one session."""

    def __init__(self, *, data_dir: Path, session_key: str) -> None:
        self.data_dir = data_dir
        self.session_key = session_key
        self.sentinel = new_sentinel()
        run_dir = data_dir / "run" / session_key
        self.log_path = run_dir / f"bash-{session_key}.log"
        self.rc_path = run_dir / "profile" / f"rc-{session_key}"
        self.state_path = run_dir / "profile" / f"state-{session_key}.sh"

    def render(self) -> RenderedRc:
        """Write the rendered rc to disk and return its handle."""
        self.rc_path.parent.mkdir(parents=True, exist_ok=True)
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        content = render_rc(
            sentinel=self.sentinel,
            log_path=self.log_path,
            state_path=self.state_path,
        )
        self.rc_path.write_text(content, encoding="utf-8")
        return RenderedRc(path=self.rc_path, sentinel=self.sentinel)
