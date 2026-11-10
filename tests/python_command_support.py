"""Make a bare ``python`` or ``python3`` command run the interpreter that runs the tests.

Production launches Flow and endpoint children as ``["python", ...]`` or
``["python3", ...]`` because the Sandbox image provides both. A host
interpreter such as ``/usr/bin/python3`` has no ``python`` beside it, and a bare
``python3`` may be a different interpreter than the one running pytest, so
tests that exercise a real child need a PATH entry on which both names are
exactly ``sys.executable``.
"""

from __future__ import annotations

import os
import shlex
import sys
import sysconfig
from pathlib import Path


def python_path_entry(directory: Path) -> str:
    """Return a PATH entry on which bare ``python`` and ``python3`` run ``sys.executable``.

    On POSIX ``python`` and ``python3`` shell shims are written into ``directory`` that ``exec``
    the unresolved ``sys.executable``; a symlink would drop a virtual
    environment's packages, because CPython finds ``pyvenv.cfg`` from the
    invoked path. On Windows ``CreateProcess`` cannot run a script shim and
    searches the parent application's directory before PATH, where CPython
    always keeps ``python.exe`` beside the running interpreter, so the returned
    entry is nominal there: the interpreter directory.
    """
    if os.name == "nt":
        return str(Path(sys.executable).parent)
    directory.mkdir(parents=True, exist_ok=True)
    for name in ("python", "python3"):
        shim = directory / name
        shim.write_text(f'#!/bin/sh\nexec {shlex.quote(sys.executable)} "$@"\n')
        shim.chmod(0o755)
    return str(directory)


def test_interpreter_path(shim_directory: Path, base_path: str) -> str:
    """Return ``base_path`` with the test interpreter's commands ahead of it.

    The interpreter shims come first, then the interpreter's scripts directory
    (so a bare ``fusesoc`` is the test environment's console script), then
    ``base_path``. Empty parts are dropped.
    """
    parts = (python_path_entry(shim_directory), sysconfig.get_path("scripts"), base_path)
    return os.pathsep.join(filter(None, parts))
