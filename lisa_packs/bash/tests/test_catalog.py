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


def test_probe_mode_delegate_only_contract() -> None:
    """Probe is a bounded read-only diagnostician, reachable only via delegate.

    The 2026-10-25 runaway: a stuck coding_execute turn read-looped to the
    nudge, delegated the gathering to a full shell child, and the child burned
    19 bash calls over 14 minutes to the wall-budget kill — the parent then
    died of StreamIdleTimeout. Probe replaces that escape hatch: never
    routable (an operator's "fix my machine" query must land in shell, not a
    read-only mode), bash_run only, posture pre-warmed, small completions.
    """
    roots = resolve_mode_roots([get_content_root()])
    assert roots, "No modes/ directory found in bash pack"
    catalog = json.loads(build_mode_catalog_json(roots))
    assert "probe" in catalog
    mode = catalog["probe"]
    assert mode["routable"] is False
    # No command pattern: a delegate-only mode has no user-facing entry.
    assert not mode["command_pattern"]
    assert mode["allowed_tool_ids"] == ["bash_run"]
    assert mode["pre_turn_operation_ids"] == ["sandbox_facts"]
    # The contract prose is the enforcement surface for read-only behavior:
    # it must name the read-only rule and the call budget. Whitespace is
    # normalized because the authored Markdown hard-wraps lines.
    body = " ".join(mode["instruction_body"].split())
    assert "Read-only, always" in body
    assert "at most 8 bash calls" in body


def _declared_timeout_ceiling(tool_id: str) -> int:
    """Read the kernel wrapper timeout declared for ``tool_id``."""
    catalog = json.loads(build_tool_catalog_json(resolve_tool_roots([get_content_root()])))
    return int(catalog[tool_id]["timeout_seconds"])


def test_bash_run_declares_itself_self_bounding() -> None:
    """``bash_run`` must disable the kernel wrapper (``timeout_seconds: 0``).

    Regression (2026-10-05 shell turn): with no declared value the wrapper
    inherited the 30s default and cancelled the call before the pack could
    adopt a slow sandboxed command as a job — the command was stranded and
    the turn aborted. bash_run self-bounds on every path (the model's own
    ``timeout_seconds`` parameter, the sandbox grace window, job adoption),
    and its ceiling is published to the operator via ``stream_output``, so a
    wrapper lifetime bound can never be sized correctly; the tool must
    declare itself self-bounding instead.
    """
    assert _declared_timeout_ceiling("bash_run") == 0, (
        "bash_run must declare timeout_seconds: 0 (self-bounding); a "
        "positive wrapper bound will eventually cancel a slow sandboxed "
        "call before job adoption can run"
    )
    # The self-bounding declaration only holds while the tool publishes
    # observable progress while it runs: that is what replaces the wrapper.
    declared_streaming = json.loads(
        build_tool_catalog_json(resolve_tool_roots([get_content_root()]))
    )["bash_run"]["stream_output"]
    assert declared_streaming is True, (
        "bash_run must keep stream_output: true alongside "
        "timeout_seconds: 0 — the operator-visible progress is what makes "
        "the self-bounding declaration safe"
    )
