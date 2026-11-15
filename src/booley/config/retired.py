"""The one refusal for ``booley.toml`` configuration that no longer exists.

A retired ``[jobs]`` key or a ``[goals]`` section (Goal Mode was removed) makes
every Project command refuse with a single message that names each problem, its
line and the fix. Only the Main Booley Directory's ``booley.toml`` is read, the
file admission reads: a linked worktree's or a measured commit's copy never is,
so exported checkouts stay measurable.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

from booley.core.booley_dir import resolve_common_booley_dir
from booley.core.config_paths import resolve_toml

TABLE_HEADER = re.compile(r"^\s*\[\[?\s*([^\[\]]*?)\s*\]\]?\s*(?:#.*)?$")
_STRING = re.compile(r"\"(?:\\.|[^\"\\])*\"|'[^']*'")


class RetiredConfigError(ValueError):
    """``booley.toml`` still sets a ``[jobs]`` key that was retired or has a ``[goals]`` section."""


def normalized_name(raw: str) -> str:
    """A TOML table or key name without quotes or spaces around its dots."""
    return re.sub(r"\s*\.\s*", ".", raw.replace('"', "").replace("'", "")).strip()


def opens_multiline_string(line: str) -> str | None:
    """The delimiter of a multi-line string this line opens and leaves open, if any."""
    _, _, value = line.partition("=")
    for delimiter in ('"""', "'''"):
        if delimiter in value and value.count(delimiter) % 2 == 1:
            return delimiter
    return None


def _open_brackets(line: str) -> int:
    """How many array or inline-table brackets *line* leaves open (strings and comments ignored)."""
    bare = _STRING.sub("", line.partition("=")[2]).partition("#")[0]
    return bare.count("[") + bare.count("{") - bare.count("]") - bare.count("}")


def _is_goals_name(name: str) -> bool:
    return name == "goals" or name.startswith("goals.")


class _GoalsScanner:
    """Line-by-line scan that collects the spans of Goal-owned blocks."""

    def __init__(self) -> None:
        self.spans: list[list[int]] = []
        self._table = ""
        self._owned = False  # the statement being read belongs to a Goal-owned block
        self._delimiter: str | None = None
        self._depth = 0

    def feed(self, number: int, line: str) -> None:
        if self._delimiter is not None or self._depth > 0:
            self._continue_statement(number, line)
            return
        header = TABLE_HEADER.match(line)
        if header is not None:
            self._table = normalized_name(header.group(1))
            self._owned = _is_goals_name(self._table)
            if self._owned:
                self.spans.append([number, number])
            return
        content = line.strip()
        if not content or content.startswith("#"):
            return
        self._owned = _is_goals_name(self._table) or (
            self._table == "" and _is_goals_name(normalized_name(line.partition("=")[0]))
        )
        if self._owned and self._table == "":
            self.spans.append([number, number])
        elif self._owned:
            self.spans[-1][1] = number
        self._delimiter = opens_multiline_string(line)
        self._depth = 0 if self._delimiter else max(_open_brackets(line), 0)

    def _continue_statement(self, number: int, line: str) -> None:
        if self._delimiter is not None:
            self._delimiter = None if self._delimiter in line else self._delimiter
        else:
            self._depth += _open_brackets(f"={line}")
        if self._owned:
            self.spans[-1][1] = number


def goals_section_spans(path: Path) -> tuple[tuple[int, int], ...]:
    """The ``(first, last)`` 1-based lines of each Goal-owned block of the ``booley.toml`` at *path*.

    A block is the ``[goals]`` table or a ``[goals.*]`` subtable (up to the line
    before the next table header, without trailing blank lines, so a kept table
    between two Goal tables is never inside a span) or a top-level ``goals = ...``
    or ``goals.<key> = ...`` assignment. Empty when the file is missing,
    unparseable or has no ``goals`` key.
    """
    try:
        text = path.read_text(encoding="utf-8")
        data = tomllib.loads(text)
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        return ()
    if "goals" not in data:
        return ()
    scanner = _GoalsScanner()
    for number, line in enumerate(text.splitlines(), start=1):
        scanner.feed(number, line)
    merged: list[list[int]] = []
    for first, last in scanner.spans:
        if merged and merged[-1][1] + 1 == first:
            merged[-1][1] = last
        else:
            merged.append([first, last])
    return tuple((first, last) for first, last in merged)


def _lines(spans: tuple[tuple[int, int], ...]) -> str:
    named = ", ".join(str(first) if first == last else f"{first}-{last}" for first, last in spans)
    return f"line {named}" if len(spans) == 1 and spans[0][0] == spans[0][1] else f"lines {named}"


def goals_section_fix(path: Path, spans: tuple[tuple[int, int], ...]) -> str:
    """The repair for a ``[goals]`` section: delete exactly the reported spans, nothing else."""
    return f"delete the [goals] tables ({_lines(spans)}) from {path}; Goal Mode was removed"


def refuse_retired_config(project_root: Path | None) -> None:
    """Raise :class:`RetiredConfigError` while the live Project configuration is retired.

    Names every retired ``[jobs]`` key and a ``[goals]`` section with its line and
    fix. A Project that cannot be resolved has nothing to refuse.
    """
    from booley.config.jobs import retired_jobs_keys, retired_jobs_keys_message

    try:
        path = resolve_toml(resolve_common_booley_dir(project_root))
    except (FileNotFoundError, ValueError, RuntimeError):
        return
    found = retired_jobs_keys(path)
    spans = goals_section_spans(path)
    if found and not spans:
        raise RetiredConfigError(retired_jobs_keys_message(path, found))
    if spans:
        message = (
            f"booley.toml has a [goals] section (line {spans[0][0]}); Goal Mode was removed. "
            f"Fix: {goals_section_fix(path, spans).partition('; ')[0]} "
            "(or run booley-heal to remove it)."
        )
        if found:
            message = f"{retired_jobs_keys_message(path, found)} Also: {message}"
        raise RetiredConfigError(message)
