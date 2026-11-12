"""Scripted model for history_fold.scenario.yml (see e2etest.scripted).

``cd`` into the workspace named by the session context, ``ls -a`` twice
(the rerun supersedes the first), ``cat notes.txt``, ``touch marker.txt``
(invalidates every resident observation and folds at turn end), ``ls -a``
once more, then a text answer.
"""

from __future__ import annotations

import re

from e2etest.scripted import Reply, ScriptError, ScriptTurn

_WORKSPACE = re.compile(r"The workspace is the fixture repository at `([^`]+)`")
_COMMANDS = ("ls -a", "ls -a", "cat notes.txt", "touch marker.txt", "ls -a")


def _workspace(turn: ScriptTurn) -> str:
    for message in turn.request.messages:
        content = message.get("content")
        found = _WORKSPACE.search(content if isinstance(content, str) else str(content))
        if found:
            return found.group(1)
    raise ScriptError("session context does not name the workspace")


def auxiliary(turn: ScriptTurn) -> Reply:
    if turn.request.response_format is not None:
        return Reply.text("{}")
    return Reply.text("Listed the workspace, read notes.txt, and added marker.txt.")


def respond(turn: ScriptTurn) -> Reply:
    if "bash_run" not in turn.tool_names:
        raise ScriptError(f"bash_run not offered; tools={sorted(turn.tool_names)}")
    done = turn.count("bash_run")
    if done == 0:
        return Reply.call("bash_run", command=f"cd {_workspace(turn)}")
    if done <= len(_COMMANDS):
        return Reply.call("bash_run", command=_COMMANDS[done - 1])
    return Reply.text("Listed the workspace, read notes.txt, and added marker.txt.")
