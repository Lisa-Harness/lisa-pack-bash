"""Encoding-aware decoding of subprocess output.

The bash pack runs a real subprocess and reads its stdout and stderr as
bytes. Those bytes are not always UTF-8.

On Windows, the ``bash`` on PATH is the system's ``wsl.exe`` launcher
(until Git Bash is installed ahead of it on PATH), and ``wsl.exe`` writes
its output as UTF-16LE with a byte-order mark. Decoding that stream as
UTF-8 produces the classic "NUL between every character" output, which
the caller then reports as garbage.

The decoder therefore reads the encoding from the bytes themselves:

- A UTF-16LE BOM (``b"\\xff\\xfe"``) selects UTF-16LE.
- A UTF-16BE BOM (``b"\\xfe\\xff"``) selects UTF-16BE.
- No BOM means UTF-8, which is what every POSIX host produces.

Because the decoder is *streaming* — chunks arrive at arbitrary
boundaries — a multi-byte character can straddle two chunks, and the BOM
arrives only in the first. The incremental decoders from :mod:`codecs`
hold the incomplete tail of a split character until the next chunk, so a
boundary in the middle of a character does not corrupt the output.
"""

from __future__ import annotations

import codecs
from typing import Final

__all__ = ["StreamDecoder", "decode_bytes"]

#: Byte-order marks for the UTF-16 encodings, longest first so the
#: UTF-32LE mark is not mistaken for a UTF-16LE one.
_UTF16_BOMS: Final[tuple[tuple[bytes, str], ...]] = (
    (codecs.BOM_UTF16_LE, "utf-16-le"),
    (codecs.BOM_UTF16_BE, "utf-16-be"),
)

#: The encoding used when the stream carries no byte-order mark.
_DEFAULT_ENCODING: Final[str] = "utf-8"

#: Bytes needed to recognize a byte-order mark. A shorter first chunk
#: waits for more.
_BOM_LENGTH: Final[int] = 2


class StreamDecoder:
    """Decode a byte stream whose encoding is not known up front.

    The encoding is taken from the first chunk's byte-order mark, if it
    has one, and from UTF-8 otherwise. Once decided it does not change:
    a stream does not switch encoding halfway through.
    """

    __slots__ = ("_decided", "_decoder", "_encoding", "_pending")

    def __init__(self) -> None:
        self._decided = False
        self._encoding = _DEFAULT_ENCODING
        self._decoder: codecs.IncrementalDecoder | None = None
        # Bytes seen before the encoding was decided. The BOM is two
        # bytes, so a one-byte first chunk cannot be classified yet.
        self._pending = b""

    @property
    def encoding(self) -> str:
        """The encoding the stream is being decoded as."""
        return self._encoding

    def _decide(self) -> str:
        """Pick the encoding from the leading bytes and start the decoder.

        Returns the text decoded from the bytes seen so far.
        """
        self._decided = True
        data = self._pending
        self._pending = b""
        for bom, encoding in _UTF16_BOMS:
            if data.startswith(bom):
                self._encoding = encoding
                # "utf-16-le" keeps the BOM as a character, so strip it
                # explicitly and decode the remainder.
                self._decoder = codecs.getincrementaldecoder(encoding)(errors="replace")
                return self._decoder.decode(data[len(bom) :], False)
        self._encoding = _DEFAULT_ENCODING
        self._decoder = codecs.getincrementaldecoder(_DEFAULT_ENCODING)(errors="replace")
        return self._decoder.decode(data, False)

    def decode(self, chunk: bytes) -> str:
        """Decode the next chunk, holding back an incomplete character.

        Returns the text that is complete. A multi-byte character split
        across two chunks is decoded when the rest of it arrives.
        """
        if not chunk:
            return ""
        if not self._decided:
            self._pending += chunk
            # Two bytes is enough to recognize a BOM; a shorter first
            # chunk waits for more.
            if len(self._pending) >= _BOM_LENGTH:
                return self._decide()
            return ""
        assert self._decoder is not None
        return self._decoder.decode(chunk, False)

    def flush(self) -> str:
        """Decode whatever is left at the end of the stream."""
        if not self._decided:
            self._decide()
        assert self._decoder is not None
        return self._decoder.decode(b"", True)


def decode_bytes(data: bytes) -> str:
    """Decode a complete byte buffer, picking the encoding from its BOM.

    This is the one-shot counterpart to :class:`StreamDecoder`, for output
    that has already been collected in full.
    """
    if not data:
        return ""
    for bom, encoding in _UTF16_BOMS:
        if data.startswith(bom):
            return data[len(bom) :].decode(encoding, errors="replace")
    return data.decode(_DEFAULT_ENCODING, errors="replace")
