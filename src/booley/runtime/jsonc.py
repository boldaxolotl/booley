"""Small bounded JSONC reader with source spans for narrow, comment-preserving edits."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from itertools import pairwise
from typing import Any

_TOKEN = re.compile(
    r'\s+|//[^\r\n]*|/\*[\s\S]*?\*/|"(?:[^"\\\x00-\x1f]|\\.)*"|[-0-9.eE+]+|true|false|null|[{}\[\]:,]'
)


@dataclass(frozen=True)
class Node:
    """One parsed value and its exact source span."""

    value: Any
    start: int
    end: int
    children: tuple[Node, ...] = ()
    members: dict[str, Node] = field(default_factory=dict)


class Document:
    """Reject unsupported, ambiguous or malformed documents before any edit."""

    def __init__(self, source: str) -> None:
        if len(source.encode()) > 1024 * 1024:
            raise ValueError("JSONC document exceeds 1 MiB")
        self.source = source
        self.newline: str = "\r\n" if "\r\n" in source else "\n"
        self.tokens = self._tokens(source)
        self.index = 0
        self.root = self._value(0)
        if self.index != len(self.tokens) or not isinstance(self.root.value, dict):
            raise ValueError("JSONC requires exactly one object")

    @staticmethod
    def _tokens(source: str) -> list[tuple[str, int, int]]:
        tokens, end = [], 0
        for match in _TOKEN.finditer(source):
            if match.start() != end:
                raise ValueError("unsupported JSONC syntax")
            token = match.group()
            if not token.isspace() and not token.startswith(("//", "/*")):
                tokens.append((token, match.start(), match.end()))
            end = match.end()
        if end != len(source):
            raise ValueError("unsupported JSONC syntax")
        return tokens

    def _take(self, expected: str | None = None) -> tuple[str, int, int]:
        if self.index >= len(self.tokens):
            raise ValueError("incomplete JSONC document")
        token = self.tokens[self.index]
        self.index += 1
        if expected is not None and token[0] != expected:
            raise ValueError(f"expected {expected}")
        return token

    def _value(self, depth: int) -> Node:
        if depth > 32:
            raise ValueError("JSONC nesting exceeds 32")
        token, start, end = self._take()
        if token in {"{", "["}:
            return self._collection(token, start, depth + 1)
        return Node(json.loads(token), start, end)

    def _collection(self, opening: str, start: int, depth: int) -> Node:
        closing = "}" if opening == "{" else "]"
        children, members = [], {}
        for _ in range(len(self.tokens)):
            if self.index >= len(self.tokens):
                raise ValueError("incomplete JSONC collection")
            if self.tokens[self.index][0] == closing:
                _, _, end = self._take(closing)
                value = (
                    {key: node.value for key, node in members.items()}
                    if opening == "{"
                    else [node.value for node in children]
                )
                return Node(value, start, end, tuple(children), members)
            key = None
            if opening == "{":
                key = json.loads(self._take()[0])
                if not isinstance(key, str) or key in members:
                    raise ValueError("invalid or duplicate JSONC key")
                self._take(":")
            node = self._value(depth)
            children.append(node)
            if key is not None:
                members[key] = node
            if self.index < len(self.tokens) and self.tokens[self.index][0] != closing:
                self._take(",")
        raise ValueError("incomplete JSONC collection")

    def task_text(self, task: dict[str, Any], array: Node | None) -> str:
        """Use the document's indentation and line endings for the inserted task."""
        indents = re.findall(r'(?m)^([ \t]+)"', self.source)
        indent = ""
        if array and array.children:
            first = array.children[0]
            prefix = self.source[self.source.rfind("\n", 0, first.start) + 1 : first.start]
            if prefix and prefix.isspace():
                indent = prefix
        levels = sorted({"", *indents, indent}, key=len)
        increments = [
            deeper[len(shallow) :]
            for shallow, deeper in pairwise(levels)
            if deeper.startswith(shallow)
        ]
        unit = min(increments, key=len) if increments else "  "
        indent = indent or unit * 2
        return indent + json.dumps(task, indent=unit).replace("\n", self.newline + indent)

    def append(self, array: Node, raw: str) -> str:
        """Insert one reversible span, preserving delimiters and closing indentation."""
        prior = [token for token in self.tokens if array.start < token[1] < array.end - 1]
        comma = "," if array.children and prior[-1][0] != "," else ""
        at = array.end - 1
        line = self.source.rfind("\n", array.start, at) + 1
        if line > array.start and self.source[line:at].isspace():
            at = line
        return self.source[:at] + comma + self.newline + raw + self.newline + self.source[at:]

    def remove(self, array: Node, node: Node) -> str:
        """Compatibility removal for an unchanged task owned by an older revision."""
        before = next(
            (
                token
                for token in reversed(self.tokens)
                if array.start < token[1] < node.start and token[0] == ","
            ),
            None,
        )
        after = next(
            (
                token
                for token in self.tokens
                if node.end <= token[1] < array.end and token[0] == ","
            ),
            None,
        )
        comma = after or before
        spans = [(node.start, node.end)] + ([] if comma is None else [(comma[1], comma[2])])
        source = self.source
        for start, end in sorted(spans, reverse=True):
            source = source[:start] + source[end:]
        return source

    def add_tasks(self, raw: str) -> str:
        """Append the missing tasks member in one reversible span."""
        at = self.root.end - 1
        last = self.tokens[-2][0]
        comma = "," if self.root.members and last != "," else ""
        return (
            self.source[:at]
            + comma
            + self.newline
            + '"tasks": ['
            + self.newline
            + raw
            + self.newline
            + "]"
            + self.newline
            + self.source[at:]
        )
