"""Catalog discovery test for the bash pack."""

from __future__ import annotations

import json

from sdk.content._catalog import (
    build_mode_catalog_json,
    build_tool_catalog_json,
    resolve_mode_roots,
    resolve_tool_roots,
)

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
    assert "background" in tool["description"].lower() or "batch" in tool["description"].lower()


def test_shell_mode_still_routable() -> None:
    """Frontmatter is load-bearing: command_pattern + routable must survive rewrites."""
    roots = resolve_mode_roots([get_content_root()])
    assert roots, "No modes/ directory found in bash pack"
    catalog = json.loads(build_mode_catalog_json(roots))
    assert "shell" in catalog
    mode = catalog["shell"]
    assert mode["routable"] is True
    assert mode["command_pattern"].startswith("^(?:/)?shell")
    assert "bash_run" in mode["allowed_tool_ids"]
    # layers_list must NOT be in allowed_tool_ids: it is a cross-pack
    # read-effect OPERATION (bwrap pack), not an MCP tool. Listing it here
    # made mode_completion_effects classify it as a required write (the
    # unresolvable-tool-id default) and the write-contract backstop blocked
    # every summarize-only shell turn as a hallucinated success
    # (2026-09-07 turns 114/116/118). It stays reachable via the operation
    # catalog; the mode body still tells the model to use it in Regime C.
    assert "layers_list" not in mode["allowed_tool_ids"]
    assert "sandbox_facts" in mode["pre_turn_operation_ids"]
