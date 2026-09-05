"""Catalog discovery test for the bash pack."""

from __future__ import annotations

import json

from sdk.content._catalog import build_tool_catalog_json, resolve_tool_roots

from lisa_packs.bash import get_content_root


def test_bash_pack_bash_run_tool_discovered() -> None:
    roots = resolve_tool_roots([get_content_root()])
    assert roots, "No tools/ directory found in bash pack"

    catalog = json.loads(build_tool_catalog_json(roots))

    assert "bash_run" in catalog
    tool = catalog["bash_run"]
    assert tool["tool_id"] == "bash_run"
    assert tool["pack_id"] == "lisa-pack-bash"
    assert tool["effect"] == "execute"
    assert tool["approval_required"] is True
    assert tool["exportable"] is True
    assert tool["module_path"] == "lisa_packs.bash.tools.bash_run.bash_run"
