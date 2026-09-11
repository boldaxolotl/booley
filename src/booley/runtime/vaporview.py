"""Authoritative VaporView installation-state probes and recovery guidance."""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Callable, Iterable
from enum import Enum
from pathlib import Path

EXTENSION_ID = "lramseyer.vaporview"
VERIFIED_VERSION = "1.5.4"
RELEASES_URL = "https://github.com/Lramseyer/vaporview/releases"
EDITOR_PROBE_TIMEOUT_SECONDS = 30


class ExtensionState(Enum):
    """What an authoritative extension-registry observation established."""

    INSTALLED = "installed"
    MISSING = "missing"
    UNKNOWN = "unknown"


def session_home() -> Path:
    """Resolve the Session Runtime user's home consistently on every host OS."""
    return Path(os.environ.get("HOME", "/home/agent"))


def find_manifests(home: Path) -> list[Path]:
    """Return installed VaporView manifests from the VS Code remote registry."""
    root = home / ".vscode-server" / "extensions"
    return sorted(root.glob(f"{EXTENSION_ID}-*/package.json"))


def probe_home(home: Path) -> ExtensionState:
    """Inspect a VS Code remote extension registry without guessing on I/O failure."""
    root = home / ".vscode-server" / "extensions"
    try:
        entries = list(root.iterdir())
    except OSError:
        return ExtensionState.UNKNOWN
    obsolete = _obsolete_extensions(root)
    if obsolete is None:
        return ExtensionState.UNKNOWN
    candidates = [
        entry
        for entry in entries
        if entry.name.lower().startswith(f"{EXTENSION_ID}-") and entry.name.lower() not in obsolete
    ]
    manifest_states = [_manifest_matches(candidate / "package.json") for candidate in candidates]
    if any(state is True for state in manifest_states):
        return ExtensionState.INSTALLED
    if candidates:
        return ExtensionState.UNKNOWN
    return ExtensionState.MISSING


def _obsolete_extensions(root: Path) -> set[str] | None:
    """Return VS Code's obsolete extension-directory names, or unknown on corruption."""
    try:
        source = (root / ".obsolete").read_text(encoding="utf-8")
    except FileNotFoundError:
        return set()
    except OSError:
        return None
    try:
        payload = json.loads(source)
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    return {str(name).lower() for name, removed in payload.items() if removed is True}


def _manifest_matches(path: Path) -> bool | None:
    """Validate that a readable package manifest identifies VaporView."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    publisher = payload.get("publisher")
    name = payload.get("name")
    if not isinstance(publisher, str) or not isinstance(name, str):
        return None
    return f"{publisher}.{name}".lower() == EXTENSION_ID


def probe_editor(
    editor: str,
    *,
    run: Callable[..., subprocess.CompletedProcess[str]] | None = None,
) -> ExtensionState:
    """Ask one editor CLI for its extensions; failed queries remain unknown."""
    runner = run or subprocess.run
    try:
        result = runner(
            [editor, "--list-extensions"],
            capture_output=True,
            text=True,
            timeout=EDITOR_PROBE_TIMEOUT_SECONDS,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError):
        return ExtensionState.UNKNOWN
    if result.returncode != 0:
        return ExtensionState.UNKNOWN
    installed = {line.strip().lower() for line in result.stdout.splitlines()}
    if EXTENSION_ID in installed:
        return ExtensionState.INSTALLED
    return ExtensionState.MISSING


def aggregate_states(states: Iterable[ExtensionState]) -> ExtensionState:
    """Combine editor observations without turning uncertainty into absence."""
    observed = tuple(states)
    if observed and all(state is observed[0] for state in observed):
        return observed[0]
    return ExtensionState.UNKNOWN


def install_guidance(*, editor: str = "code") -> str:
    """Explain online and offline installation into the attached remote window."""
    return (
        "Install it in the attached remote window:\n"
        f"  {editor} --install-extension {EXTENSION_ID}\n"
        "If the container cannot reach the Marketplace, download "
        f"vaporview-{VERIFIED_VERSION}.vsix from the official releases page on a "
        f'networked host ({RELEASES_URL}), then run "Extensions: Install from VSIX..." in '
        "the attached remote window. Booley's attach watcher patches a late install "
        'automatically; afterward run "Developer: Reload Window". Verify in the remote '
        "terminal with:\n"
        f"  {editor} --list-extensions --show-versions\n"
        f"which must list {EXTENSION_ID}@{VERIFIED_VERSION}. A host CLI without the remote "
        "window selected may install locally instead of into the container."
    )
