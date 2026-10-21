"""Tests for resolving a bash the pack can actually drive.

On Windows, ``C:\\Windows\\System32\\bash.exe`` is the WSL launcher. It
accepts no ``-c`` argument, so the pack's composed command never runs
and the output is a launcher error rather than the command's own. The
resolver prefers a real POSIX bash over such a launcher.
"""

from __future__ import annotations

import shutil
from pathlib import Path, PurePosixPath, PureWindowsPath

import pytest

from lisa_packs.bash import session

#: The Windows WSL launcher path, as it appears on a real runner.
_WSL_LAUNCHER = r"C:\Windows\System32\bash.exe"

#: A real POSIX bash, as Git for Windows ships it.
_GIT_BASH = r"C:\Program Files\Git\bin\bash.exe"


def test_launcher_path_is_recognized() -> None:
    """A binary in the Windows system directory is treated as a launcher."""
    assert session._is_launcher(_WSL_LAUNCHER)
    assert session._is_launcher("/usr/bin/bash") is False
    assert session._is_launcher(_GIT_BASH) is False


def test_resolve_bash_prefers_a_real_shell_over_a_launcher(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A launcher on PATH is passed over for a real POSIX bash."""
    monkeypatch.setattr(shutil, "which", lambda _name: _WSL_LAUNCHER)
    monkeypatch.setattr(session, "_POSIX_BASH_GLOBS", (_GIT_BASH,))
    monkeypatch.setattr(Path, "is_file", lambda self: str(self) == _GIT_BASH)
    assert session.resolve_bash() == _GIT_BASH


def test_resolve_bash_uses_a_real_shell_on_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """A real bash on PATH is used as-is."""
    monkeypatch.setattr(shutil, "which", lambda _name: _GIT_BASH)
    assert session.resolve_bash() == _GIT_BASH


def test_resolve_bash_falls_back_to_path_when_nothing_else(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no real shell anywhere, the PATH entry is still reported.

    The caller then fails with a clear "bash is not installed" message
    rather than a confusing launcher error.
    """
    monkeypatch.setattr(shutil, "which", lambda _name: _WSL_LAUNCHER)
    monkeypatch.setattr(session, "_POSIX_BASH_GLOBS", ())
    assert session.resolve_bash() == _WSL_LAUNCHER


def test_bash_available_is_false_without_any_bash(monkeypatch: pytest.MonkeyPatch) -> None:
    """No bash anywhere means the pack reports itself unavailable."""
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    monkeypatch.setattr(session, "_POSIX_BASH_GLOBS", ())
    assert session.bash_available() is False


def test_path_glob_candidates_exist_on_this_host() -> None:
    """Each declared bash location is absolute for the platform it targets.

    A relative entry would resolve against the process working directory,
    which is not where a shell lives. The POSIX candidates are absolute on
    POSIX and the Windows candidates are absolute on Windows, so each is
    checked against its own flavour.
    """
    for candidate in session._POSIX_BASH_GLOBS:
        if candidate.startswith("C:"):
            assert PureWindowsPath(candidate).is_absolute()
        else:
            assert PurePosixPath(candidate).is_absolute()
