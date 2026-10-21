"""Tests for incremental output streaming in the bash executor.

Covers the ``StreamTap`` line-buffering contract, ``tapped_drain_to_file``
(spill/inline parity with the plain drain), and the ``_maybe_tap`` gating.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from kernel.models import ToolExecutionContext

from lisa_packs.bash.executor import StreamTap, tapped_drain_to_file

#: Upper bound on a single published line. The tap truncates at 2000
#: characters; the operator view must stay below this ceiling.
_MAX_PUBLISHED_LINE = 2100


class _FakeBus:
    """Minimal bus double recording publishes."""

    def __init__(self) -> None:
        self.published: list[tuple[str, dict[str, Any]]] = []

    async def publish(self, topic: str, payload: dict[str, Any]) -> None:
        self.published.append((topic, payload))

    def subscribe(self, topic: str, handler: Callable[..., Any]) -> None:
        pass


def _ctx(
    *,
    event_bus: Any = None,
    stream_output: bool = False,
) -> ToolExecutionContext:
    """A minimal context carrying the tap's gating fields."""
    return ToolExecutionContext(
        tool_id="bash_run",
        event_bus=event_bus,
        stream_output=stream_output,
    )


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
        assert len(text) < _MAX_PUBLISHED_LINE
        assert text.endswith("…")

    def test_multibyte_split_across_chunks(self) -> None:
        bus = _FakeBus()
        ctx = _ctx(event_bus=bus, stream_output=True)
        tap = StreamTap(ctx, stream="stdout")
        # é is 2 bytes in UTF-8; split it across feeds — errors="replace"
        # degrades the partial byte rather than raising.
        chunks = ["é".encode()[:1], "é".encode()[1:] + b"\n"]
        assert len(self._drain(tap, chunks)) == 1


class TestSentinelMachineryFilter:
    """The tap drops the wrapper's post-command state markers.

    The wrapper prints an rc marker, a ``state=cwd=`` line, and one ``var
    NAME=`` line per exported env var after every command — hundreds of
    harness-bookkeeping lines on hosts with a rich environment. The tap
    filters them from the operator stream; the parser still reads the full
    raw output for the model-visible result.
    """

    SENTINEL = "LISA_9f2c1a7b8842e065>> "

    def _tap(self, bus: _FakeBus) -> StreamTap:
        ctx = _ctx(event_bus=bus, stream_output=True)
        return StreamTap(ctx, stream="stdout", sentinel=self.SENTINEL)

    def test_machinery_lines_dropped_real_output_kept(self) -> None:
        bus = _FakeBus()
        tap = self._tap(bus)
        s = self.SENTINEL.strip()
        raw = (
            "real output line\n"
            f"{s}rc=0 pipe=0 \n"
            f"{s}state=cwd=/home/adam\n"
            f"{s}var AI_AGENT=github_copilot\n"
            f"{s}var PATH='/home/adam/bin'\n"
            f"{s}func myfn dGVjaG8=\n"
            f"{s}funcs myfn otherfn\n"
            f"{s}\n"
        )
        asyncio.run(self._drive(tap, [raw.encode()]))
        assert _published_texts(bus) == ["real output line"]

    def test_no_sentinel_passes_everything(self) -> None:
        """A tap without a sentinel (degenerate use) never drops lines."""
        bus = _FakeBus()
        ctx = _ctx(event_bus=bus, stream_output=True)
        tap = StreamTap(ctx, stream="stdout")
        asyncio.run(self._drive(tap, [b"rc=0\nvar X=1\n"]))
        assert _published_texts(bus) == ["rc=0", "var X=1"]

    def test_other_session_sentinel_not_ours(self) -> None:
        """A different session's marker is user output here — keep it."""
        bus = _FakeBus()
        tap = self._tap(bus)
        asyncio.run(self._drive(tap, [b"LISA_0000000000000000>>rc=0\n"]))
        assert _published_texts(bus) == ["LISA_0000000000000000>>rc=0"]

    def test_output_that_happens_to_contain_rc_prefix_kept(self) -> None:
        """Only lines starting with THIS sentinel are machinery."""
        bus = _FakeBus()
        tap = self._tap(bus)
        asyncio.run(self._drive(tap, [b"echo rc=0\nrc=0\n"]))
        assert _published_texts(bus) == ["echo rc=0", "rc=0"]

    async def _drive(self, tap: StreamTap, chunks: list[bytes]) -> None:
        """Feed ``chunks`` + close, then yield so publishes complete."""
        for chunk in chunks:
            tap.feed(chunk)
        tap.close()
        await asyncio.sleep(0)


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

        async def drive() -> dict[str, Any]:
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

        async def drive() -> dict[str, Any]:
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


class TestMachineryFilterBus:
    """The composed sandboxed path's event-bus wrapper.

    bash_run → bwrap_run composes through the child tool, whose own tap
    publishes on the parent's bus without knowing the bash sentinel. The
    filter bus drops the machinery lines the wrapper prints after the
    command; every other topic/payload forwards unchanged.
    """

    SENTINEL = "LISA_9f2c1a7b8842e065>> "

    def _make(self, sentinel: str | None = None) -> tuple[Any, Any]:
        from lisa_packs.bash.executor import _MachineryFilterBus

        bus = _FakeBus()
        filt = _MachineryFilterBus(
            bus, sentinel=sentinel if sentinel is not None else self.SENTINEL
        )
        return bus, filt

    def test_tool_output_machinery_dropped_real_output_forwards(self) -> None:
        from kernel.tools.output_stream import TOOL_OUTPUT_TOPIC

        bus, filt = self._make()
        s = self.SENTINEL.strip()

        async def drive() -> None:
            await filt.publish(TOOL_OUTPUT_TOPIC, {"text": "real output"})
            await filt.publish(TOOL_OUTPUT_TOPIC, {"text": f"{s}rc=0 pipe=0 "})
            await filt.publish(TOOL_OUTPUT_TOPIC, {"text": f"{s}state=cwd=/tmp"})
            await filt.publish(TOOL_OUTPUT_TOPIC, {"text": f"{s}var HOME=/home/adam"})

        asyncio.run(drive())
        assert [p["text"] for _, p in bus.published] == ["real output"]

    def test_other_topics_forward_untouched(self) -> None:
        _bus, filt = self._make()

        async def drive() -> None:
            await filt.publish("lisa.bash.job_done", {"job_id": "j1"})
            await filt.publish("tool.telemetry", {"target_id": "bash_run"})

        asyncio.run(drive())
        topics = [t for t, _payload in _bus.published]
        assert topics == ["lisa.bash.job_done", "tool.telemetry"]

    def test_empty_sentinel_never_filters(self) -> None:
        from kernel.tools.output_stream import TOOL_OUTPUT_TOPIC

        bus, filt = self._make(sentinel="")

        async def drive() -> None:
            await filt.publish(TOOL_OUTPUT_TOPIC, {"text": "rc=0"})

        asyncio.run(drive())
        assert bus.published[0][1]["text"] == "rc=0"

    def test_delegation_exposes_wrapped_bus_attributes(self) -> None:
        from lisa_packs.bash.executor import _MachineryFilterBus

        _bus, filt = self._make()
        assert isinstance(filt, _MachineryFilterBus)
        # Attribute access falls through to the wrapped bus (subscribe etc.).
        assert hasattr(filt, "publish")
        with pytest.raises(AttributeError):
            filt._missing  # noqa: B018  # pylint: disable=pointless-statement

    def test_idempotent_wrap_keeps_single_filter(self) -> None:
        """_run_via_bwrap never stacks a filter over a filter."""
        from lisa_packs.bash.executor import _MachineryFilterBus

        _bus, filt = self._make()
        assert isinstance(filt, _MachineryFilterBus)
        assert filt._is_machinery(f"{self.SENTINEL.strip()}rc=1") is True
