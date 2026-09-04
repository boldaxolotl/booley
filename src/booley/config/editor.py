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


@dataclass(frozen=True, slots=True)
class _EditorSpec:
    command: str
    macos_application: str
    macos_executable: str
    windows_executable: Path


@dataclass(frozen=True, slots=True)
class _EditorInstallCandidate:
    command: str
    application: Path
    executable: Path
    management_command: Path | None


_EDITOR_SPECS = (
    _EditorSpec(
        "code",
        "Visual Studio Code.app",
        "Electron",
        Path("Programs/Microsoft VS Code/Code.exe"),
    ),
    _EditorSpec(
        "code-insiders",
        "Visual Studio Code - Insiders.app",
        "Electron",
        Path("Programs/Microsoft VS Code Insiders/Code - Insiders.exe"),
    ),
    _EditorSpec("codium", "VSCodium.app", "Electron", Path("Programs/VSCodium/VSCodium.exe")),
    _EditorSpec("cursor", "Cursor.app", "Cursor", Path("Programs/cursor/Cursor.exe")),
    _EditorSpec("windsurf", "Windsurf.app", "Windsurf", Path("Programs/Windsurf/Windsurf.exe")),
)
EDITOR_COMMANDS = tuple(spec.command for spec in _EDITOR_SPECS)


def _editor_install_candidates() -> tuple[_EditorInstallCandidate, ...]:
    """Return platform-native editor installations in selection order."""
    home = Path.home()
    if sys.platform == "darwin":
        roots = (Path("/Applications"), home / "Applications")
        return tuple(
            _EditorInstallCandidate(
                spec.command,
                application,
                application / "Contents" / "MacOS" / spec.macos_executable,
                None,
            )
            for root in roots
            for spec in _EDITOR_SPECS
            for application in (root / spec.macos_application,)
        )
    if sys.platform == "win32":
        local = Path(os.environ.get("LOCALAPPDATA", home / "AppData" / "Local"))
        return tuple(
            _EditorInstallCandidate(
                spec.command,
                application,
                application,
                application.parent / "bin" / f"{spec.command}.cmd",
            )
            for spec in _EDITOR_SPECS
            for application in (local / spec.windows_executable,)
        )
    return ()


def resolve_editor_install() -> Path | None:
    """Return an installed GUI application when it can be proven on disk."""
    return next(
        (
            candidate.application
            for candidate in _editor_install_candidates()
            if candidate.executable.is_file()
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
    """Return the management CLI for the highest-priority installed editor."""
    resolver = which or shutil.which
    native = _editor_install_candidates()
    for spec in _EDITOR_SPECS:
        if command := resolver(spec.command):
            return command
        candidate = next(
            (
                item
                for item in native
                if item.command == spec.command and item.executable.is_file()
            ),
            None,
        )
        if candidate is None:
            continue
        if candidate.management_command and candidate.management_command.is_file():
            return str(candidate.management_command)
        return None
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
