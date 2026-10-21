"""Priming render tests - rc substitution, sentinel, log path."""

from __future__ import annotations

from pathlib import Path

from lisa_packs.bash.priming import SessionPriming, new_sentinel, render_rc


def test_render_rc_substitutes_all_placeholders(tmp_path: Path) -> None:
    content = render_rc(
        sentinel="LISA_TEST>> ",
        log_path=tmp_path / "log.txt",
        state_path=tmp_path / "state.sh",
        lang="C.UTF-8",
    )
    assert "%%SENTINEL%%" not in content
    assert "%%LOG_PATH%%" not in content
    assert "%%STATE_FILE%%" not in content
    assert "%%LANG%%" not in content
    assert "LISA_TEST>> " in content
    assert str(tmp_path / "log.txt") in content
    assert str(tmp_path / "state.sh") in content
    assert "C.UTF-8" in content


def test_render_rc_disables_history_and_sets_dumb_term(tmp_path: Path) -> None:
    content = render_rc(
        sentinel="S>> ",
        log_path=Path("/x/log"),
        state_path=tmp_path / "state.sh",
        lang="C.UTF-8",
    )
    assert "set +H" in content
    assert "HISTSIZE=0" in content
    assert "export TERM=dumb" in content
    assert "PROMPT_COMMAND" in content
    assert "NO_COLOR" in content
    assert "STATE_FILE" not in content  # placeholder already substituted
    assert '[ -f "' in content  # state file source present


def test_new_sentinel_unique_and_prefixed() -> None:
    a = new_sentinel()
    b = new_sentinel()
    assert a != b
    assert a.startswith("LISA_")


def test_session_priming_writes_rc_and_log_dirs(tmp_path: Path) -> None:
    priming = SessionPriming(data_dir=tmp_path, session_key="abc123")
    rendered = priming.render()
    assert rendered.path.is_file()
    assert priming.log_path.parent.is_dir()
    body = rendered.path.read_text(encoding="utf-8")
    assert priming.sentinel in body
    assert str(priming.log_path) in body
