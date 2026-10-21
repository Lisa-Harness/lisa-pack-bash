"""Tests for the encoding-aware subprocess output decoder.

On Windows the ``bash`` on PATH is frequently the system's WSL launcher,
which writes its output as UTF-16. The stream captured from the Windows
CI run starts ``57 00 69 00`` — UTF-16LE with *no* byte-order mark — so
detection cannot rely on a mark alone.
"""

from __future__ import annotations

import pytest

from lisa_packs.bash.encoding import StreamDecoder, decode_bytes, detect_encoding

# The UTF-16 text a Windows launcher produces.
_WSL_MESSAGE = (
    "Windows Subsystem for Linux has no installed distributions.\n"
    "You can resolve this by installing a distribution.\n"
)


def test_detect_encoding_reads_utf16le_bom() -> None:
    """A UTF-16LE byte-order mark selects UTF-16LE decoding."""
    assert detect_encoding(b"\xff\xfe" + b"a\x00") == ("utf-16-le", 2)


def test_detect_encoding_reads_utf16be_bom() -> None:
    """A UTF-16BE byte-order mark selects UTF-16BE decoding."""
    assert detect_encoding(b"\xfe\xff" + b"\x00a") == ("utf-16-be", 2)


def test_detect_encoding_sniffs_bomless_utf16() -> None:
    """A BOM-less stream with the UTF-16 NUL density is detected as UTF-16.

    This is the shape the Windows CI run produced: the captured stdout
    begins ``57 00 69 00`` with no mark at all.
    """
    encoding, bom_length = detect_encoding(_WSL_MESSAGE.encode("utf-16-le"))
    assert encoding == "utf-16-le"
    assert bom_length == 0


def test_detect_encoding_leaves_plain_utf8_alone() -> None:
    """UTF-8 text with no NUL density stays UTF-8."""
    encoding, bom_length = detect_encoding(b"one\ntwo\n")
    assert encoding == "utf-8"
    assert bom_length == 0


def test_decode_bytes_reads_bomless_utf16() -> None:
    """A BOM-less UTF-16LE stream decodes to readable text."""
    assert decode_bytes(_WSL_MESSAGE.encode("utf-16-le")) == _WSL_MESSAGE


def test_decode_bytes_reads_utf16le_bom() -> None:
    """A UTF-16LE byte-order mark selects UTF-16LE decoding."""
    data = b"\xff\xfe" + _WSL_MESSAGE.encode("utf-16-le")
    assert decode_bytes(data) == _WSL_MESSAGE


def test_decode_bytes_reads_utf16be_bom() -> None:
    """A UTF-16BE byte-order mark selects UTF-16BE decoding."""
    data = b"\xfe\xff" + _WSL_MESSAGE.encode("utf-16-be")
    assert decode_bytes(data) == _WSL_MESSAGE


def test_decode_bytes_reads_utf8_bom() -> None:
    """A UTF-8 byte-order mark is dropped, not rendered."""
    assert decode_bytes(b"\xef\xbb\xbf" + b"one\n") == "one\n"


def test_decode_bytes_without_bom_stays_utf8() -> None:
    """A stream with no byte-order mark is decoded as UTF-8."""
    assert decode_bytes(b"one\ntwo\n") == "one\ntwo\n"


def test_decode_bytes_empty() -> None:
    """Empty input decodes to an empty string."""
    assert decode_bytes(b"") == ""


def test_stream_decoder_reads_bomless_utf16() -> None:
    """The streaming decoder sniffs a BOM-less UTF-16LE stream."""
    data = _WSL_MESSAGE.encode("utf-16-le")
    decoder = StreamDecoder()
    assert decoder.decode(data) + decoder.flush() == _WSL_MESSAGE
    assert decoder.encoding == "utf-16-le"


@pytest.mark.parametrize("chunk_size", [1, 3, 7, 13])
def test_stream_decoder_survives_a_split_character(chunk_size: int) -> None:
    """A chunk boundary inside a UTF-16 character does not corrupt output."""
    data = _WSL_MESSAGE.encode("utf-16-le")
    decoder = StreamDecoder()
    parts = [decoder.decode(data[i : i + chunk_size]) for i in range(0, len(data), chunk_size)]
    parts.append(decoder.flush())
    assert "".join(parts) == _WSL_MESSAGE


def test_stream_decoder_buffers_a_short_utf16_prefix() -> None:
    """A prefix too short to classify is held, not misread as UTF-8.

    Four bytes of UTF-16 ASCII are not enough to rule UTF-16 out, so the
    decoder waits for more rather than decoding them as UTF-8 and
    emitting half a character.
    """
    data = _WSL_MESSAGE.encode("utf-16-le")
    decoder = StreamDecoder()
    prefix = decoder.decode(data[:4])
    assert prefix == ""
    # The held prefix is decoded together with the rest, so the stream
    # reassembles in full.
    assert decoder.decode(data[4:]) + decoder.flush() == _WSL_MESSAGE


def test_stream_decoder_releases_short_utf8_immediately() -> None:
    """A short UTF-8 chunk is published at once, not held for more bytes.

    Streaming output has to reach the operator surface as it arrives, so
    a chunk that is clearly not UTF-16 must not wait for the sniff
    window to fill.
    """
    decoder = StreamDecoder()
    assert decoder.decode(b"a\nb\n") == "a\nb\n"


@pytest.mark.parametrize("chunk_size", [1, 2, 5])
def test_stream_decoder_survives_a_split_utf8_character(chunk_size: int) -> None:
    """A chunk boundary inside a UTF-8 character does not corrupt output."""
    text = "café ☕ 日本語\n"
    data = text.encode("utf-8")
    decoder = StreamDecoder()
    parts = [decoder.decode(data[i : i + chunk_size]) for i in range(0, len(data), chunk_size)]
    parts.append(decoder.flush())
    assert "".join(parts) == text
