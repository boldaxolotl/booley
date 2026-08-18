"""Editor configuration for Console clickable links.

The Console always launches VS Code (``code``) for clickable file/ticket
links — no user configuration. VS Code is already a hard dependency of
Interactive Mode (ADR 0018), so assuming it here keeps the click path
trivial. If ``code`` is not on PATH the subprocess layer degrades to a
status-bar hint on the first click (see ``console/links.invoke``).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ResolvedEditor:
    """Concrete editor command templates.

    Each template is an argv tuple. Tokens may contain ``{file}``,
    ``{line}``, ``{left}``, ``{right}`` placeholders; the click invoker
    substitutes them per action.

    ``diff`` is ``None`` when the editor exposes no diff command; the
    click resolver degrades a would-be diff action to ``open`` in that
    case.
    """

    open: tuple[str, ...]
    open_at_line: tuple[str, ...]
    diff: tuple[str, ...] | None


# The one and only editor. Tokens stay in argv form (no shell quoting) so
# the click subprocess layer never goes through a shell.
VSCODE_EDITOR = ResolvedEditor(
    open=("code", "--goto", "{file}"),
    open_at_line=("code", "--goto", "{file}:{line}"),
    diff=("code", "--diff", "{left}", "{right}"),
)
