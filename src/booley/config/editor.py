"""Resolution of supported VS Code-family editor commands."""

from __future__ import annotations

import shutil
from collections.abc import Callable
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


EDITOR_COMMANDS = ("code", "code-insiders", "codium", "cursor", "windsurf")


def editor_for_command(command: str) -> ResolvedEditor:
    """Build argv templates for one VS Code-compatible command."""
    return ResolvedEditor(
        open=(command, "--goto", "{file}"),
        open_at_line=(command, "--goto", "{file}:{line}"),
        diff=(command, "--diff", "{left}", "{right}"),
    )


def resolve_editor_command(
    which: Callable[[str], str | None] | None = None,
) -> str | None:
    """Return the first supported editor executable present on ``PATH``."""
    resolver = which or shutil.which
    for command in EDITOR_COMMANDS:
        if found := resolver(command):
            return found
    return None


def resolve_editor(
    which: Callable[[str], str | None] | None = None,
) -> ResolvedEditor | None:
    """Resolve immutable command templates for the installed editor."""
    command = resolve_editor_command(which)
    return editor_for_command(command) if command is not None else None


# Backward-compatible default for callers that need a launch attempt even when
# discovery has not run yet.
VSCODE_EDITOR = editor_for_command("code")
