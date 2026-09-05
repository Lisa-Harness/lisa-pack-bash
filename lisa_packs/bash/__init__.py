"""Shell execution capability pack for Lisa."""

from __future__ import annotations

from pathlib import Path

_READ_AUTHORED_TEXT = None


def get_content_root() -> Path:
    """Return the content root directory for this pack."""
    return Path(__file__).parent


def read_authored_text(relative_path: str) -> str:
    """Read an authored content file from this pack's own content root.

    Tool handlers have no ``ctx.host`` seam, so they read their own authored
    markdown directly from the content root rather than through the shared
    registry. Fails loudly when the file is missing (pack-content rule).
    """
    path = get_content_root() / "content" / relative_path
    if not path.is_file():
        raise RuntimeError(f"bash pack missing authored content file: {path}")
    return path.read_text(encoding="utf-8")
