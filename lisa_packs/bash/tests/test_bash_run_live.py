"""Real round-trip test for bash_run in unsandboxed mode.

Marked ``live``: launches a real primed bash process, parses the sentinel, and
writes the audit log. Skipped when bash is absent.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from kernel.models import ToolExecutionContext

from lisa_packs.bash import executor as _executor
from lisa_packs.bash.tools.bash_run import bash_run

pytestmark = pytest.mark.live


@pytest.fixture(autouse=True)
def _fresh_sessions():
    _executor._reset_for_tests()
    yield
    _executor._reset_for_tests()


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not installed")
@pytest.mark.asyncio
async def test_bash_run_roundtrip_unsandboxed(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(bash_run, "_sandbox_available", lambda: False)
    ctx = ToolExecutionContext(
        tool_id="bash_run",
        workspace_id="ws",
        parameters={"command": "echo roundtrip-$((40+2))"},
        data_dir=str(tmp_path),
    )
    result = await bash_run.execute(ctx)
    assert result.error == ""
    assert "roundtrip-42" in result.output_text
    assert result.output_json["return_code"] == 0
    assert result.output_json["sandboxed"] is False
    log_path = Path(result.output_json["log_path"])
    assert log_path.is_file()
    assert "roundtrip" in log_path.read_text(encoding="utf-8")
