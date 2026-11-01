"""Make a bare ``python`` command run the interpreter that runs the tests.

Production launches Flow and endpoint children as ``["python", ...]`` because
the Sandbox image provides ``python``. A host interpreter such as
``/usr/bin/python3`` has no ``python`` beside it, so tests that exercise a real
child need a PATH entry on which ``python`` is exactly ``sys.executable``.
"""

from __future__ import annotations

import os
import shlex
import sys
from pathlib import Path


def python_path_entry(directory: Path) -> str:
    """Return a PATH entry on which a bare ``python`` runs exactly ``sys.executable``.

    On POSIX a ``python`` shell shim is written into ``directory`` that ``exec``s
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
    shim = directory / "python"
    shim.write_text(f'#!/bin/sh\nexec {shlex.quote(sys.executable)} "$@"\n')
    shim.chmod(0o755)
    return str(directory)
