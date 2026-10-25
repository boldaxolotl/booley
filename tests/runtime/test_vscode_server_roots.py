"""Guard: the default VS Code server root has one definition in ``src/``."""

from __future__ import annotations

import re
from pathlib import Path

import booley

# A trailing ``-`` would be the separate Insiders root name, not the default root.
_DEFAULT_ROOT = re.compile(r"\.vscode-server(?!-)")


def test_default_server_root_is_spelled_once_in_src():
    package = Path(booley.__file__).parent
    hits = sorted(
        path.relative_to(package).as_posix()
        for path in package.rglob("*.py")
        if _DEFAULT_ROOT.search(path.read_text(encoding="utf-8"))
    )

    assert hits == ["runtime/vscode_server.py"]
