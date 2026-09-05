"""Output-parsing unit tests for the executor sentinel protocol."""

from __future__ import annotations

from lisa_packs.bash import executor


def test_parse_output_strips_sentinel_and_rc() -> None:
    sentinel = "LISA_ABC>> "
    raw = (
        "hello world\n"
        "second line\n"
        f"{sentinel}rc=0\n"
    )
    body, rc = executor._parse_output(raw, sentinel)
    assert rc == 0
    assert "hello world" in body
    assert "rc=0" not in body
    assert sentinel not in body


def test_parse_output_nonzero_rc() -> None:
    sentinel = "LISA_X>> "
    raw = f"err output\n{sentinel}rc=127\n"
    body, rc = executor._parse_output(raw, sentinel)
    assert rc == 127
    assert "err output" in body


def test_parse_output_prompt_only_lines_dropped() -> None:
    sentinel = "LISA_P>> "
    raw = f"{sentinel}\nvalue\n{sentinel}rc=3\n{sentinel}"
    body, rc = executor._parse_output(raw, sentinel)
    assert rc == 3
    assert body.strip() == "value"
