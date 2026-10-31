"""Terminal color codes are produced only by the shared color helpers (#2431)."""

from __future__ import annotations

import ast
import re
from pathlib import Path

_SOURCE_ROOT = Path(__file__).parents[2] / "src" / "booley"
_COLOR_HELPERS = "harness/colors.py"
# SGR (color/style) sequences only; cursor and window controls end in other letters.
_SGR = re.compile(r"\x1b\[[0-9;]*m")


def _literal_text(node: ast.AST) -> str | None:
    """Return the text of a string/bytes literal or f-string (placeholders become ``0``)."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Constant) and isinstance(node.value, bytes):
        return node.value.decode("latin-1")
    if isinstance(node, ast.JoinedStr):
        return "".join(
            part.value if isinstance(part, ast.Constant) else "0" for part in node.values
        )
    return None


def _sgr_literals(source: str) -> list[int]:
    """Line numbers of literals in *source* that contain an SGR escape sequence."""
    lines = {
        node.lineno
        for node in ast.walk(ast.parse(source))
        if (text := _literal_text(node)) is not None and _SGR.search(text)
    }
    return sorted(lines)


def test_sgr_scan_detects_literals_and_ignores_cursor_controls() -> None:
    assert _sgr_literals('x = "\\033[2m"\n') == [1]
    assert _sgr_literals('x = f"\\033[38;5;{code}m{text}"\n') == [1]
    assert _sgr_literals('x = b"\\x1b[0m"\n') == [1]
    assert _sgr_literals('x = f"\\x1b[{rows}A\\x1b[J"\n') == []


def test_no_raw_sgr_codes_outside_color_helpers() -> None:
    offenders = [
        f"{path.relative_to(_SOURCE_ROOT).as_posix()}:{line}"
        for path in sorted(_SOURCE_ROOT.rglob("*.py"))
        if path.relative_to(_SOURCE_ROOT).as_posix() != _COLOR_HELPERS
        for line in _sgr_literals(path.read_text(encoding="utf-8"))
    ]
    assert not offenders, f"raw SGR color codes outside {_COLOR_HELPERS}: {offenders}"
