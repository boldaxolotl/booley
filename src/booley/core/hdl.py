"""Small lexical HDL declaration checks; no preprocessing or parsing."""

from __future__ import annotations

import re

_STRING = r'"(?:\\(?:\r\n|.)|[^"\\\r\n])*"'
_TOKENS = re.compile(rf"{_STRING}|/\*.*?\*/|//[^\n]*", re.DOTALL)
_MODULES = re.compile(
    rf"{_STRING}|^\s*(?:macro)?module\s+(?:automatic\s+|static\s+)?([A-Za-z_]\w*)",
    re.MULTILINE | re.DOTALL,
)


def strip_comments(text: str) -> str:
    """Remove comments, retaining strings and the standalone scan's spacing."""
    return _TOKENS.sub(lambda m: m[0] if m[0].startswith('"') else "", text)


def declared_module_names(text: str) -> list[str]:
    """Return line-start module names in order, ignoring comments and strings.

    Keep standalone's identifier/lifetime grammar and comment deletion behavior.
    Attributes and multiple declarations on one line are not interpreted.
    """
    return [match[1] for match in _MODULES.finditer(strip_comments(text)) if match[1] is not None]


def declares_module(text: str) -> bool:
    """Whether text declares at least one module or macromodule."""
    return bool(declared_module_names(text))
