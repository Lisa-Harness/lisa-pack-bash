"""Tests for the encoding-aware subprocess output decoder.

On Windows the ``bash`` on PATH is the system's ``wsl.exe`` launcher,
which writes its output as UTF-16LE with a byte-order mark. Decoding
that as UTF-8 produces the "NUL between every character" output that the
Windows CI run surfaced.
"""

from __future__ import annotations

import pytest

from lisa_packs.bash._decode import StreamDecoder, decode_bytes

# The UTF-16LE text a Windows launcher produces.
_WSL_MESSAGE = (
    "Windows Subsystem for Linux has no installed distributions.\n"
    "You can resolve this by running: wsl.exe --list --online\n"
)


def test_decode_bytes_reads_utf16le_bom() -> None:
    """A UTF-16LE byte-order mark selects UTF-16LE decoding."""
    data = b"\xff\xfe" + _WSL_MESSAGE.encode("utf-16-le")
    assert decode_bytes(data) == _WSL_MESSAGE


def test_decode_bytes_reads_utf16be_bom() -> None:
    """A UTF-16BE byte-order mark selects UTF-16BE decoding."""
    data = b"\xfe\xff" + _WSL_MESSAGE.encode("utf-16-be")
    assert decode_bytes(data) == _WSL_MESSAGE


def test_decode_bytes_without_bom_stays_utf8() -> None:
    """A stream with no byte-order mark is decoded as UTF-8."""
    assert decode_bytes(b"one\ntwo\n") == "one\ntwo\n"


def test_decode_bytes_empty() -> None:
    """Empty input decodes to an empty string."""
    assert decode_bytes(b"") == ""


def test_stream_decoder_reads_utf16le() -> None:
    """The streaming decoder picks UTF-16LE from the leading BOM."""
    data = b"\xff\xfe" + _WSL_MESSAGE.encode("utf-16-le")
    decoder = StreamDecoder()
    assert decoder.decode(data) + decoder.flush() == _WSL_MESSAGE
    assert decoder.encoding == "utf-16-le"


@pytest.mark.parametrize("chunk_size", [1, 3, 7, 13])
def test_stream_decoder_survives_a_split_character(chunk_size: int) -> None:
    """A chunk boundary inside a UTF-16 character does not corrupt output."""
    data = b"\xff\xfe" + _WSL_MESSAGE.encode("utf-16-le")
    decoder = StreamDecoder()
    parts = [decoder.decode(data[i : i + chunk_size]) for i in range(0, len(data), chunk_size)]
    parts.append(decoder.flush())
    assert "".join(parts) == _WSL_MESSAGE


@pytest.mark.parametrize("chunk_size", [1, 2, 5])
def test_stream_decoder_survives_a_split_utf8_character(chunk_size: int) -> None:
    """A chunk boundary inside a UTF-8 character does not corrupt output."""
    text = "café ☕ 日本語\n"
    data = text.encode("utf-8")
    decoder = StreamDecoder()
    parts = [decoder.decode(data[i : i + chunk_size]) for i in range(0, len(data), chunk_size)]
    parts.append(decoder.flush())
    assert "".join(parts) == text
