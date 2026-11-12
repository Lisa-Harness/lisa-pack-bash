"""``bash_run`` history semantics: which shell results a later result makes stale.

The kernel's history ledger (``kernel.history``) folds superseded results to a
ref stub; this module makes the shell's claims. A conservative classifier
recognizes *observation* commands, ones that only read state (``ls``,
``cat``, ``git status``, a pipe of such stages, and so on). A rerun of the same
observation in the same overlay layer supersedes the earlier output. Any
other command may have changed what an earlier observation saw, so it
invalidates them all and folds once its turn ends. Background jobs make no
claims: their result is a job descriptor the model still needs.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from kernel.models import ToolExecutionContext, ToolResult

from lisa_packs.bash.session import _SESSIONS, _session_key

OBSERVATION_PREFIX = "bash.obs:"
_ANCHOR_FILE = "bash_run:file:"
_MAX_ANCHOR_BYTES = 256_000

#: Commands that only read state whatever their (non-excluded) arguments.
_OBSERVERS = frozenset(
    {
        "basename", "cat", "df", "dirname", "du", "file", "grep", "head", "id",
        "ls", "nl", "printenv", "ps", "pwd", "readlink", "realpath", "rg", "sort",
        "stat", "tail", "tree", "type", "uname", "wc", "which", "whoami",
    }
)  # fmt: skip
_GIT_OBSERVERS = frozenset(
    {"blame", "diff", "log", "ls-files", "rev-parse", "show", "status"}
)
#: Predicates that make ``find`` write files or run programs (``-fprint`` covers
#: ``-fprint0``/``-fprintf``).
_FIND_WRITERS = ("-delete", "-exec", "-execdir", "-ok", "-okdir", "-fprint", "-fls")


@dataclass(frozen=True)
class _Writers:
    """Options that make an otherwise read-only command write or execute.

    ``short`` letters match anywhere in a bundled cluster (``sort -uo out``);
    ``long`` names also match unambiguous abbreviations (``sort --out=x``),
    which GNU getopt and git's parse-options both accept.
    """

    short: str = ""
    long: tuple[str, ...] = ()

    def hit(self, arg: str) -> bool:
        if arg.startswith("--"):
            name = arg.split("=", 1)[0]
            return len(name) > 2 and any(w.startswith(name) for w in self.long)
        return arg.startswith("-") and any(c in self.short for c in arg[1:])


_WRITERS: dict[str, _Writers] = {
    "file": _Writers(short="C", long=("--compile",)),
    "git": _Writers(long=("--output",)),
    "rg": _Writers(long=("--pre",)),
    "sort": _Writers(short="o", long=("--output", "--compress-program")),
    # ``-R`` writes an 00Tree.html into every directory.
    "tree": _Writers(short="oR"),
}
#: Anything that chains, substitutes, redirects, or backgrounds is not an observation.
_UNSAFE = re.compile(r"[;&<>`\n]|\$\(|\|\|")


def _observes(argv: list[str]) -> bool:
    if not argv:
        return False
    head, args = argv[0], argv[1:]
    writers = _WRITERS.get(head)
    if writers is not None and any(writers.hit(a) for a in args):
        return False
    if head == "find":
        return not any(a.startswith(_FIND_WRITERS) for a in args)
    if head == "git":
        return bool(args) and args[0] in _GIT_OBSERVERS
    return head in _OBSERVERS or (head == "env" and not args)


def observation_key(command: str) -> str:
    """Return the command's normalized form when it only reads state, else ``""``."""
    if _UNSAFE.search(command):
        return ""
    stages = command.split("|")
    normalized: list[str] = []
    for stage in stages:
        try:
            argv = shlex.split(stage)
        except ValueError:
            return ""
        if not _observes(argv):
            return ""
        normalized.append(shlex.join(argv))
    return " | ".join(normalized)


def history_keys(params: dict[str, Any], result: ToolResult) -> dict[str, Any] | None:
    """Claim keys for one ``bash_run`` result (the kernel's ``history.hook``)."""
    output = result.output_json if isinstance(result.output_json, dict) else {}
    if output.get("backgrounded") or params.get("run_in_background"):
        # The job descriptor stays resident; a job that may write still stales
        # what the observations saw.
        command = str(params.get("command") or "")
        return (
            None if observation_key(command) else {"invalidates": [OBSERVATION_PREFIX]}
        )
    commands = params.get("commands")
    if commands is not None:
        observing = all(observation_key(str(c)) for c in commands)
        return {
            "scope": "turn",
            "invalidates": [] if observing else [OBSERVATION_PREFIX],
        }
    command = str(params.get("command") or "")
    observed = observation_key(command)
    if not observed or output.get("timed_out"):
        return {
            "scope": "turn",
            "invalidates": [] if observed else [OBSERVATION_PREFIX],
        }
    layer = str(params.get("layer_id") or "-")
    claims: dict[str, Any] = {"keys": [f"{OBSERVATION_PREFIX}{layer}:{observed}"]}
    if output.get("stdout_path"):
        claims["anchor"] = f"{_ANCHOR_FILE}{output['stdout_path']}"
    return claims


async def history_snapshot(ctx: ToolExecutionContext, keys: list[str]) -> str:
    """Keep the shell's live state resident: cwd, posture, and running jobs."""
    del keys
    session = _SESSIONS.get(_session_key(ctx))
    if session is None:
        return ""
    cwd = ""
    state = session.priming.state_path
    if state.is_file():
        for line in state.read_text(encoding="utf-8").splitlines():
            if line.startswith("cd "):
                cwd = " ".join(shlex.split(line[3:]))
                break
    running = [job_id for job_id, task in session.jobs.items() if not task.done()]
    parts = [
        f"cwd={cwd or '(session start)'}",
        f"sandboxed={'true' if session.sandboxed else 'false'}",
        f"running jobs={', '.join(running) if running else 'none'}",
    ]
    return "bash session: " + "; ".join(parts)


async def resolve_history_anchor(ctx: ToolExecutionContext, anchor: str) -> str | None:
    """Serve a folded result's full spilled stdout from this session's jobs dir."""
    if not anchor.startswith(_ANCHOR_FILE):
        return None
    session = _SESSIONS.get(_session_key(ctx))
    if session is None:
        return None
    path = Path(anchor.removeprefix(_ANCHOR_FILE)).resolve()
    if not path.is_relative_to(session.jobs_dir.resolve()) or not path.is_file():
        return None
    with path.open("rb") as handle:
        data = handle.read(_MAX_ANCHOR_BYTES + 1)
    text = data[:_MAX_ANCHOR_BYTES].decode("utf-8", errors="replace")
    if len(data) > _MAX_ANCHOR_BYTES:
        text += f"\n[... truncated at {_MAX_ANCHOR_BYTES} bytes; read {path} directly]"
    return text
