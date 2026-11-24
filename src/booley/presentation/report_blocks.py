"""Text, table and code blocks shared by the Measurement Report and the Review Report.

A report is a list of sections (a title and blocks) built from stored JSON alone. The
same sections render as Markdown and as terminal text, so both hold the same facts in the
same order. In Markdown, specials in user text are backslash-escaped; on a terminal, a
block's tone is a colour.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Literal

from booley.presentation.colors import amber, bold, green, red
from booley.presentation.terminal_table import render_table

Tone = Literal["met", "fail", "warn"] | None


@dataclass(frozen=True)
class Text:
    text: str
    tone: Tone = None


@dataclass(frozen=True)
class Rows:
    headers: tuple[str, ...]
    rows: tuple[tuple[str, ...], ...]
    tones: tuple[Tone, ...]  # per row; colours its last cell on a terminal


@dataclass(frozen=True)
class Code:
    text: str


Block = Text | Rows | Code
Section = tuple[str, list[Block]]  # title, blocks


def _fence(text: str) -> str:
    """A backtick fence longer than any backtick run in *text*."""
    longest = max((len(run) for run in re.findall("`+", text)), default=0)
    return "`" * max(3, longest + 1)


_CODE_SPAN = re.compile(r"`[^`\n]*`")
_SPECIAL = re.compile(r"[\\*<>\[\]`]|(?<![A-Za-z0-9])_|_(?![A-Za-z0-9])")


def _md(text: str) -> str:
    """Backslash-escape what Markdown would parse, leaving backtick spans as written."""
    pieces = re.split(f"({_CODE_SPAN.pattern})", text)
    return "".join(
        piece if index % 2 else _SPECIAL.sub(lambda hit: "\\" + hit.group(), piece)
        for index, piece in enumerate(pieces)
    )


def _cell(text: str) -> str:
    return _md(text).replace("|", "\\|").replace("\n", " ")


def markdown_chunks(blocks: Sequence[Block]) -> list[str]:
    """One chunk per table or fence; consecutive text lines form one list."""
    chunks: list[str] = []
    for block in blocks:
        if isinstance(block, Text):
            item = "- " + _md(block.text).replace("\n", " ")
            if chunks and chunks[-1].startswith("- "):
                chunks[-1] += "\n" + item
            else:
                chunks.append(item)
        elif isinstance(block, Rows):
            lines = [block.headers, ("---",) * len(block.headers), *block.rows]
            chunks.append("\n".join("| " + " | ".join(map(_cell, row)) + " |" for row in lines))
        else:
            fence = _fence(block.text)
            chunks.append(f"{fence}\n{block.text}\n{fence}")
    return chunks


def paint(tone: Tone) -> Callable[[str], str]:
    return {"met": green, "fail": red, "warn": amber}.get(tone or "", str)


def terminal_lines(block: Block, width: int | None) -> list[str]:
    if isinstance(block, Text):
        return ["  " + paint(block.tone)(block.text.replace("\n", " "))]
    if isinstance(block, Code):
        return ["    " + line for line in block.text.splitlines()]
    rows = [
        (*row[:-1], paint(tone)(row[-1]))
        for row, tone in zip(block.rows, block.tones, strict=True)
    ]
    return render_table(block.headers, rows, width=width)


def markdown_sections(sections: Sequence[Section]) -> list[str]:
    """``## <title>`` then the chunks, for each section."""
    return [
        part for title, blocks in sections for part in (f"## {title}", *markdown_chunks(blocks))
    ]


def terminal_sections(sections: Sequence[Section], width: int | None) -> list[str]:
    """A blank line, the bold title and the block lines, for each section."""
    lines: list[str] = []
    for title, blocks in sections:
        lines.extend(["", bold(title)])
        lines.extend(line for block in blocks for line in terminal_lines(block, width))
    return lines
