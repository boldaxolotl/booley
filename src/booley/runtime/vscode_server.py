"""Where the VS Code server keeps its per-user data in the Sandbox home.

The server stores extensions and Machine settings under a root directory in the
user's home. The root's name depends on the product: Microsoft's stable build,
Insiders, and GitHub Codespaces (which uses the server's bare default) each use
a different one, and more than one can exist side by side. This module is the
single place that knows those names.
"""

from __future__ import annotations

from pathlib import Path

# The root used when no server root exists yet. This is the only place in
# ``src/`` that spells it out.
DEFAULT_ROOT_NAME = ".vscode-server"
ROOT_NAMES = (DEFAULT_ROOT_NAME, ".vscode-remote", ".vscode-server-insiders")


def _roots_holding(home: Path, relative: Path) -> list[Path]:
    """Server roots under which *relative* exists, else the default root.

    Existence is checked on every call, so a root that appears after the caller
    started (a devcontainer attach in progress) is seen by the next poll.
    """
    found = [home / name for name in ROOT_NAMES if (home / name / relative).exists()]
    return found or [home / DEFAULT_ROOT_NAME]


def extension_dirs(home: Path) -> list[Path]:
    """Every existing extension registry, else the default one."""
    return [root / "extensions" for root in _roots_holding(home, Path("extensions"))]


def machine_settings_paths(home: Path) -> list[Path]:
    """Machine settings of every existing server root, else the default one."""
    return [root / "data" / "Machine" / "settings.json" for root in _roots_holding(home, Path())]
