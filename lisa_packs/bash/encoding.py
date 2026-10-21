"""Encoding-aware decoding of subprocess output.

The bash pack runs a real subprocess and reads its stdout and stderr as
bytes. Those bytes are not always UTF-8.

On Windows the ``bash`` on PATH is frequently the system's WSL launcher
(``C:\\Windows\\System32\\bash.exe``), which writes its output as UTF-16.
Decoding such a stream as UTF-8 turns every other byte into a NUL, so
the pack sees ``"W\\x00i\\x00n\\x00d\\x00o\\x00w\\x00s\\x00"`` where it
should see ``"Windows"``.

The encoding is therefore detected from the bytes themselves — no
platform check, no hard-coded Windows branch:

- A UTF-16LE byte-order mark (``b"\\xff\\xfe"``) selects UTF-16LE.
- A UTF-16BE byte-order mark (``b"\\xfe\\xff"``) selects UTF-16BE.
- A UTF-8 byte-order mark (``b"\\xef\\xbb\\xbf"``) selects UTF-8, and the
  mark is dropped.
- Otherwise the head of the stream is sniffed. UTF-16 ASCII text has
  roughly every other byte NUL; UTF-8 text has no NULs at all. A stream
  with a high NUL density is UTF-16, and the parity of the first non-NUL
  byte settles the endianness.

The BOM-less sniff is deliberately conservative: it only fires on the
high-NUL-density shape that ASCII-in-UTF-16 produces, so ordinary UTF-8
output keeps its current decoding.

Because the decoder is *streaming* — chunks arrive at arbitrary
boundaries — a multi-byte character can straddle two chunks, and the
encoding is only decided once enough bytes have arrived. The
incremental decoders from :mod:`codecs` hold the incomplete tail of a
split character until the next chunk, so a boundary in the middle of a
character does not corrupt the output.
"""

from __future__ import annotations

import codecs
from typing import Final

__all__ = ["StreamDecoder", "decode_bytes", "detect_encoding"]

#: Byte-order marks that identify an encoding outright.
_BOM_UTF8: Final[bytes] = codecs.BOM_UTF8
_BOM_UTF16_LE: Final[bytes] = codecs.BOM_UTF16_LE
_BOM_UTF16_BE: Final[bytes] = codecs.BOM_UTF16_BE

#: The encoding used when nothing says otherwise.
_UTF8: Final[str] = "utf-8"

#: How many bytes the sniffer inspects before committing to an encoding.
_SNIFF_WINDOW: Final[int] = 512

#: How many NUL-free bytes are enough to rule out UTF-16 and decide UTF-8
#: without waiting for the full sniff window. UTF-16 ASCII puts a NUL on
#: every second byte, so any prefix of four bytes of it contains one.
_MIN_SNIFF_LENGTH: Final[int] = 4

#: Minimum share of NUL bytes in the sniff window for the UTF-16 shape test.
#: UTF-16 ASCII text is ~50% NULs; real UTF-8 text is ~0%. The threshold
#: sits well below 0.5 so that short, partly-binary UTF-16 still sniffs,
#: and well above 0 so ordinary UTF-8 never does.
_NUL_SHARE_THRESHOLD: Final[float] = 0.25


def _looks_like_utf16(data: bytes) -> bool:
    """True when ``data`` has the high-NUL-density shape of UTF-16 ASCII text.

    UTF-16LE encodes ASCII ``"a"`` as ``b"a\\x00"`` and UTF-16BE as
    ``b"\\x00a"``; either way roughly every other byte is NUL. UTF-8 text
    (the normal case) has no NULs at all, so the density test separates the
    two cleanly.
    """
    if not data:
        return False
    nul_count = data.count(0)
    return nul_count >= len(data) * _NUL_SHARE_THRESHOLD


def _utf16_parity(data: bytes) -> str:
    """Return ``"utf-16-le"`` or ``"utf-16-be"`` for a BOM-less UTF-16 stream.

    The first non-NUL byte settles the endianness: in UTF-16LE the ASCII
    range lands on even offsets and in UTF-16BE on odd offsets. A stream
    that is all NULs has no signal, so LE is assumed.
    """
    for index, byte in enumerate(data):
        if byte:
            return "utf-16-le" if index % 2 == 0 else "utf-16-be"
    return "utf-16-le"


def detect_encoding(data: bytes) -> tuple[str, int]:
    """Return ``(encoding, bom_length)`` for the head of a byte stream.

    ``bom_length`` is the number of leading bytes to drop (the mark), or 0
    when the stream carries no mark.

    ``data`` may be short. A mark settles the encoding immediately; a
    BOM-less stream needs enough bytes for the NUL-density test to mean
    something, so the caller should keep feeding until the result is
    conclusive rather than deciding on the first two bytes.
    """
    if data.startswith(_BOM_UTF8):
        return _UTF8, len(_BOM_UTF8)
    if data.startswith(_BOM_UTF16_LE):
        return "utf-16-le", len(_BOM_UTF16_LE)
    if data.startswith(_BOM_UTF16_BE):
        return "utf-16-be", len(_BOM_UTF16_BE)
    if _looks_like_utf16(data):
        return _utf16_parity(data), 0
    return _UTF8, 0


def _is_conclusive(data: bytes) -> bool:
    """Report whether ``data`` settles the encoding on its own.

    A byte-order mark settles it immediately. Without a mark the
    NUL-density test needs enough bytes to be meaningful: on a short
    prefix of ordinary UTF-8 text the density is near zero, but so is the
    density of a two-character UTF-16 prefix, and guessing early would
    decode a UTF-16 stream as UTF-8.
    """
    if data.startswith((_BOM_UTF8, _BOM_UTF16_LE, _BOM_UTF16_BE)):
        return True
    if len(data) >= _SNIFF_WINDOW:
        return True
    # A NUL-free prefix of at least a few bytes cannot be UTF-16 ASCII.
    # Deciding UTF-8 there is safe, and it keeps ordinary short output
    # from being held back.
    return len(data) >= _MIN_SNIFF_LENGTH and 0 not in data


class StreamDecoder:
    """Incremental decoder that picks an encoding from the stream's head.

    Feed bytes with :meth:`decode` and read text out. The encoding is
    detected from the first bytes seen and then held fixed for the life of
    the stream, so a chunk boundary inside a multi-byte character is
    buffered by the underlying incremental decoder rather than corrupting
    the output.
    """

    __slots__ = ("_decoder", "_encoding", "_prefix")

    def __init__(self) -> None:
        self._decoder: codecs.IncrementalDecoder | None = None
        self._encoding = _UTF8
        self._prefix = b""

    @property
    def encoding(self) -> str:
        """The encoding the stream is being decoded as."""
        return self._encoding

    def _decide(self) -> str:
        """Pick the encoding from the bytes seen so far and start decoding.

        Returns the text decoded from those bytes.
        """
        encoding, bom_length = detect_encoding(self._prefix)
        self._encoding = encoding
        self._decoder = codecs.getincrementaldecoder(encoding)(errors="replace")
        data = self._prefix
        self._prefix = b""
        return self._decoder.decode(data[bom_length:], False)

    def decode(self, chunk: bytes) -> str:
        """Decode the next chunk, holding back an incomplete character.

        Returns the text that is complete. A multi-byte character split
        across two chunks is decoded when the rest of it arrives.
        """
        if not chunk:
            return ""
        if self._decoder is None:
            self._prefix += chunk
            # The head has to be long enough to judge: a UTF-16 mark is
            # two bytes, and the NUL-density sniff needs a few characters
            # before it can tell UTF-16 from UTF-8. Short ordinary output
            # is released as soon as it is clearly not UTF-16.
            if not _is_conclusive(self._prefix):
                return ""
            return self._decide()
        assert self._decoder is not None
        return self._decoder.decode(chunk, False)

    def flush(self) -> str:
        """Decode whatever is left at the end of the stream."""
        if self._decoder is None:
            return self._decide()
        assert self._decoder is not None
        return self._decoder.decode(b"", True)


def decode_bytes(data: bytes) -> str:
    """Decode a complete byte buffer, picking the encoding from its head.

    This is the one-shot counterpart to :class:`StreamDecoder`, for output
    that has already been collected in full.
    """
    if not data:
        return ""
    encoding, bom_length = detect_encoding(data)
    return data[bom_length:].decode(encoding, errors="replace")
