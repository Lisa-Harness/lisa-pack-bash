"""Pack-validation eval: the bash pack stays structurally healthy.

Runs under the framework's ``eval`` marker (deterministic, no model calls).
Validates the pack from source via ``packstest.catalog.validate_pack``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from packstest.catalog import validate_pack

PACK_PATH = Path(__file__).resolve().parents[4]  # .../lisa-packs/bash

EXPECTED_TOOLS = {"bash_run"}


@pytest.mark.eval
def test_bash_pack_validates_from_source() -> None:
    """Entry points load, every tool YAML parses, modules import in-process."""

    report = validate_pack(PACK_PATH)
    assert report.ok, report.summary()


@pytest.mark.eval
def test_bash_pack_tool_catalog_complete() -> None:
    """Every expected tool is discovered with the bash provider id."""

    report = validate_pack(PACK_PATH)
    missing = EXPECTED_TOOLS - set(report.tools)
    unexpected = set(report.tools) - EXPECTED_TOOLS
    assert not missing, f"tools missing from catalog: {sorted(missing)}"
    assert not unexpected, f"unexpected catalog tools: {sorted(unexpected)}"
    for tool_id, tool_dict in sorted(report.tools.items()):
        assert tool_dict["tool_provider_id"] == "lisa-pack-bash", tool_id
        assert tool_dict["pack_id"] == "lisa-pack-bash", tool_id


@pytest.mark.eval
def test_bash_pack_effect_contract() -> None:
    """bash_run is an executing tool: effect=execute."""

    report = validate_pack(PACK_PATH)
    effect = report.tools["bash_run"]["effect"]
    assert effect == "execute", f"bash_run must be effect=execute, got {effect!r}"


@pytest.mark.eval
def test_bash_pack_entry_points_load() -> None:
    """The content-root entry point resolves from source."""

    report = validate_pack(PACK_PATH)
    assert "lisa.extensions.content_roots" in report.entry_points
    for group, entries in report.entry_points.items():
        for name, value in entries.items():
            assert value is not None, f"{group}:{name} failed to load"