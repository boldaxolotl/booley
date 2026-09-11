"""Boundary-safe Markdown document parsing for Evidence records."""

from __future__ import annotations

import re
from typing import Any

import yaml

from booley.core.boundary import BoundaryError, require_dict

_FRONTMATTER = re.compile(r"\A---\r?\n(?P<yaml>.*?)\r?\n---(?:\r?\n|\Z)", re.DOTALL)


class MarkdownDocumentError(ValueError):
    """A Markdown document has malformed structured metadata."""


def parse_yaml_frontmatter(text: str) -> dict[str, Any]:
    """Return validated YAML frontmatter, or an empty mapping when absent."""
    match = _FRONTMATTER.match(text)
    if match is None:
        if text.startswith("---\n") or text.startswith("---\r\n"):
            raise MarkdownDocumentError("frontmatter is not terminated")
        return {}
    try:
        return require_dict(
            yaml.safe_load(match.group("yaml")) or {},
            field="Markdown frontmatter",
        )
    except (yaml.YAMLError, BoundaryError) as exc:
        raise MarkdownDocumentError(str(exc)) from exc
