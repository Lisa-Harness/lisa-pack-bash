"""Tests for incremental output streaming in the bash executor.

Covers the ``StreamTap`` line-buffering contract, ``tapped_drain_to_file``
(spill/inline parity with the plain drain), and the ``_maybe_tap`` gating.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from kernel.models import ToolExecutionContext

from lisa_packs.bash.executor import StreamTap, tapped_drain_to_file


class _FakeBus:
    """Minimal bus double recording publishes."""

    def __init__(self) -> None:
        self.published: list[tuple[str, dict]] = []

    async def publish(self, topic: str, payload: dict) -> None:
        self.published.append((topic, payload))

    def subscribe(self, topic: str, handler) -> None:
        pass


def _ctx(**overrides) -> ToolExecutionContext:
    fields = {"tool_id": "bash_run"}
    fields.update(overrides)
    return ToolExecutionContext(**fields)


def _published_texts(bus: _FakeBus) -> list[str]:
    return [payload["text"] for _, payload in bus.published]


def _flush_publishes() -> None:
    """Yield the loop once so pending create_task publishes complete."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return


class TestStreamTap:
    """Line-buffered feed semantics (publish runs on a real loop)."""

    def _drain(self, tap: StreamTap, chunks: list[bytes]) -> list[str]:
        """Feed ``chunks`` + close inside an event loop, return published texts."""
        bus = tap._ctx.event_bus

        async def drive() -> None:
            for chunk in chunks:
                tap.feed(chunk)
            tap.close()
            await asyncio.sleep(0)

        asyncio.run(drive())
        return _published_texts(bus)

    def test_complete_lines_publish_immediately(self) -> None:
        bus = _FakeBus()
        ctx = _ctx(event_bus=bus, stream_output=True)
        tap = StreamTap(ctx, stream="stdout")
        assert self._drain(tap, [b"line one\nline two\n"]) == ["line one", "line two"]

    def test_partial_line_held_until_close(self) -> None:
        bus = _FakeBus()
        ctx = _ctx(event_bus=bus, stream_output=True)
        tap = StreamTap(ctx, stream="stdout")
        assert self._drain(tap, [b"no trailing newline"]) == ["no trailing newline"]

    def test_chunked_arrivals_reassemble(self) -> None:
        bus = _FakeBus()
        ctx = _ctx(event_bus=bus, stream_output=True)
        tap = StreamTap(ctx, stream="stdout")
        chunks = [b"hel", b"lo wo", b"rld\nnext\n"]
        assert self._drain(tap, chunks) == ["hello world", "next"]

    def test_seq_increments_per_line(self) -> None:
        bus = _FakeBus()
        ctx = _ctx(event_bus=bus, stream_output=True)
        tap = StreamTap(ctx, stream="stdout")
        self._drain(tap, [b"a\nb\n"])
        seqs = [payload["seq"] for _, payload in bus.published]
        assert seqs == [0, 1]

    def test_huge_line_truncated_for_operator(self) -> None:
        bus = _FakeBus()
        ctx = _ctx(event_bus=bus, stream_output=True)
        tap = StreamTap(ctx, stream="stdout")
        texts = self._drain(tap, [b"x" * 5000 + b"\n"])
        (text,) = texts
        assert len(text) < 2100
        assert text.endswith("…")

    def test_multibyte_split_across_chunks(self) -> None:
        bus = _FakeBus()
        ctx = _ctx(event_bus=bus, stream_output=True)
        tap = StreamTap(ctx, stream="stdout")
        # é is 2 bytes in UTF-8; split it across feeds — errors="replace"
        # degrades the partial byte rather than raising.
        chunks = ["é".encode()[:1], "é".encode()[1:] + b"\n"]
        assert len(self._drain(tap, chunks)) == 1


class TestMaybeTapGating:
    """``_maybe_tap`` builds a tap only when declared AND a bus exists."""

    def test_no_stream_flag_no_tap(self) -> None:
        from lisa_packs.bash.executor import _maybe_tap

        ctx = _ctx(event_bus=_FakeBus(), stream_output=False)
        assert _maybe_tap(ctx, stream="stdout") is None

    def test_no_bus_no_tap(self) -> None:
        from lisa_packs.bash.executor import _maybe_tap

        ctx = _ctx(event_bus=None, stream_output=True)
        assert _maybe_tap(ctx, stream="stdout") is None

    def test_gated_on_with_bus_builds_tap(self) -> None:
        from lisa_packs.bash.executor import _maybe_tap

        ctx = _ctx(event_bus=_FakeBus(), stream_output=True)
        assert isinstance(_maybe_tap(ctx, stream="stdout"), StreamTap)


class TestTappedDrain:
    """``tapped_drain_to_file`` preserves the drain-to-file contract."""

    def test_tap_lines_and_spill_content_match(self, tmp_path: Path) -> None:
        bus = _FakeBus()
        ctx = _ctx(event_bus=bus, stream_output=True)
        tap = StreamTap(ctx, stream="stdout")
        spill = tmp_path / "out.log"

        async def drive() -> dict:
            class _Reader:
                def __init__(self, chunks: list[bytes]) -> None:
                    self._chunks = list(chunks)

                async def read(self, n: int) -> bytes:
                    return self._chunks.pop(0) if self._chunks else b""

            reader = _Reader([b"alpha\nbeta\n"])
            return await tapped_drain_to_file(reader, spill, tap)  # type: ignore[arg-type]

        result = asyncio.run(drive())
        assert result["inline"] == "alpha\nbeta\n"
        assert spill.read_text(encoding="utf-8") == "alpha\nbeta\n"
        assert _published_texts(bus) == ["alpha", "beta"]

    def test_plain_drain_untouched_when_untapped(self, tmp_path: Path) -> None:
        """A non-streaming call sees identical behavior (no tap constructed)."""
        from lisa_packs.bash.executor import drain_to_file

        spill = tmp_path / "out.log"

        async def drive() -> dict:
            class _Reader:
                def __init__(self, chunks: list[bytes]) -> None:
                    self._chunks = list(chunks)

                async def read(self, n: int) -> bytes:
                    return self._chunks.pop(0) if self._chunks else b""

            reader = _Reader([b"gamma\ndelta\n"])
            return await drain_to_file(reader, spill)  # type: ignore[arg-type]

        result = asyncio.run(drive())
        assert result["inline"] == "gamma\ndelta\n"
        assert "output_path" in result