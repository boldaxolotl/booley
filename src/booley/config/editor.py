"""Resolution of supported VS Code-family editor commands."""

from __future__ import annotations

import os
import shutil
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path


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
_MACOS_CLI_NAMES = {
    "Visual Studio Code.app": "code",
    "Visual Studio Code - Insiders.app": "code-insiders",
    "VSCodium.app": "codium",
    "Cursor.app": "cursor",
    "Windsurf.app": "windsurf",
}


def _editor_install_candidates() -> tuple[tuple[Path, Path], ...]:
    """Return platform-native applications paired with their launch executables."""
    home = Path.home()
    if sys.platform == "darwin":
        applications = (
            ("Visual Studio Code.app", "Electron"),
            ("Visual Studio Code - Insiders.app", "Electron"),
            ("VSCodium.app", "Electron"),
            ("Cursor.app", "Cursor"),
            ("Windsurf.app", "Windsurf"),
        )
        roots = (Path("/Applications"), home / "Applications")
        return tuple(
            (application, application / "Contents" / "MacOS" / executable)
            for root in roots
            for name, executable in applications
            for application in (root / name,)
        )
    if sys.platform == "win32":
        local = Path(os.environ.get("LOCALAPPDATA", home / "AppData" / "Local"))
        relative = (
            Path("Programs/Microsoft VS Code/Code.exe"),
            Path("Programs/Microsoft VS Code Insiders/Code - Insiders.exe"),
            Path("Programs/VSCodium/VSCodium.exe"),
            Path("Programs/cursor/Cursor.exe"),
            Path("Programs/Windsurf/Windsurf.exe"),
        )
        return tuple((local / path, local / path) for path in relative)
    return ()


def resolve_editor_install() -> Path | None:
    """Return an installed GUI application when it can be proven on disk."""
    return next(
        (
            application
            for application, executable in _editor_install_candidates()
            if executable.is_file()
        ),
        None,
    )


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


def resolve_editor_management_command(
    which: Callable[[str], str | None] | None = None,
) -> str | None:
    """Return an editor CLI capable of managing desktop extensions."""
    if command := resolve_editor_command(which):
        return command
    for application, executable in _editor_install_candidates():
        if not executable.is_file():
            continue
        if sys.platform == "win32":
            return str(executable)
        if sys.platform == "darwin" and (name := _MACOS_CLI_NAMES.get(application.name)):
            command = application / "Contents" / "Resources" / "app" / "bin" / name
            if command.is_file():
                return str(command)
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
