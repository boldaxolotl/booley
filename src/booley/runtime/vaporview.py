"""Authoritative VaporView installation-state probes and recovery guidance."""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Callable, Iterable
from enum import Enum
from pathlib import Path

from booley.runtime import vscode_server

EXTENSION_ID = "lramseyer.vaporview"
VERIFIED_VERSION = "1.5.4"
# The Sandbox spec installs this exact build. VS Code marks a versioned install
# as pinned, so auto-update cannot move it to a release Booley has not verified.
PINNED_EXTENSION = f"{EXTENSION_ID}@{VERIFIED_VERSION}"
RELEASES_URL = "https://github.com/Lramseyer/vaporview/releases"
EDITOR_PROBE_TIMEOUT_SECONDS = 30

# Stable human-facing colors backed by VaporView custom palette slots. The
# extension's named WCP colors omit red and its theme-derived indices are not
# deterministic, so every adapter consumes these settings and indices from one
# source of truth.
PRESENTATION_COLORS = {
    "red": ("vaporview.customColor5", 4, "#e5484d"),
    "blue": ("vaporview.customColor6", 5, "#3b82f6"),
    "green": ("vaporview.customColor7", 6, "#2da44e"),
}
PRESENTATION_COLOR_INDICES = {
    name: color_index for name, (_setting, color_index, _value) in PRESENTATION_COLORS.items()
}
PRESENTATION_COLOR_SETTINGS = {
    setting: value for setting, _index, value in PRESENTATION_COLORS.values()
}


class ExtensionState(Enum):
    """What an authoritative extension-registry observation established."""

    INSTALLED = "installed"
    MISSING = "missing"
    UNKNOWN = "unknown"


def session_home() -> Path:
    """Resolve the Sandbox user's home consistently on every host OS."""
    return Path(os.environ.get("HOME", "/home/agent"))


def find_manifests(home: Path) -> list[Path]:
    """Return installed VaporView manifests from every VS Code server root."""
    return [
        manifest
        for root in vscode_server.extension_dirs(home)
        for manifest in sorted(root.glob(f"{EXTENSION_ID}-*/package.json"))
    ]


def installed_versions(home: Path) -> list[str] | None:
    """Return the sorted versions of every live VaporView install, or None if unknown.

    Each extension root's ``extensions.json`` is VS Code's own registry and is
    authoritative: after a downgrade the replaced build's directory stays on
    disk, unmarked, until the next VS Code start. Roots without that file fall
    back to the directories VS Code has not marked obsolete. Any unreadable
    registry or manifest makes the answer unknown rather than partial, as does
    a home with no VS Code server root at all.
    """
    roots = vscode_server.extension_dirs(home)
    if not roots:
        return None
    versions: set[str] = set()
    for root in roots:
        root_versions = _registry_versions(root)
        if root_versions is None:
            return None
        versions |= root_versions
    return sorted(versions)


def _registry_versions(root: Path) -> set[str] | None:
    """Collect live VaporView versions from one extension root."""
    try:
        source = (root / "extensions.json").read_text(encoding="utf-8")
    except FileNotFoundError:
        return _directory_versions(root)
    except OSError:
        return None
    try:
        entries = json.loads(source)
    except ValueError:
        return None
    if not isinstance(entries, list):
        return None
    versions: set[str] = set()
    for entry in entries:
        identifier = entry.get("identifier") if isinstance(entry, dict) else None
        ext_id = identifier.get("id") if isinstance(identifier, dict) else None
        if not isinstance(ext_id, str) or ext_id.lower() != EXTENSION_ID:
            continue
        version = entry.get("version")
        if not isinstance(version, str):
            return None
        versions.add(version)
    return versions


def _directory_versions(root: Path) -> set[str] | None:
    """Collect VaporView manifest versions from non-obsolete extension directories."""
    try:
        entries = list(root.iterdir())
    except OSError:
        return None
    obsolete = _obsolete_extensions(root)
    if obsolete is None:
        return None
    versions: set[str] = set()
    for entry in entries:
        name = entry.name.lower()
        if not name.startswith(f"{EXTENSION_ID}-") or name in obsolete:
            continue
        try:
            payload = json.loads((entry / "package.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        version = payload.get("version") if isinstance(payload, dict) else None
        if not isinstance(version, str):
            return None
        versions.add(version)
    return versions


def is_pinned_entry(entry: object) -> bool:
    """Whether a devcontainer ``extensions`` entry requests exactly the verified build."""
    return isinstance(entry, str) and entry.lower() == PINNED_EXTENSION.lower()


def is_vaporview_entry(entry: object) -> bool:
    """Whether a devcontainer ``extensions`` entry requests VaporView at any version."""
    return isinstance(entry, str) and entry.lower().partition("@")[0] == EXTENSION_ID


def probe_home(home: Path) -> ExtensionState:
    """Inspect every VS Code server root's registry; disagreeing roots are unknown."""
    return aggregate_states(_probe_registry(root) for root in vscode_server.extension_dirs(home))


def _probe_registry(root: Path) -> ExtensionState:
    """Inspect one extension registry without guessing on I/O failure."""
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
        f"  {editor} --install-extension {PINNED_EXTENSION}\n"
        "If the container cannot reach the Marketplace, download "
        f"vaporview-{VERIFIED_VERSION}.vsix from the official releases page on a "
        f'networked host ({RELEASES_URL}), then run "Extensions: Install from VSIX..." in '
        "the attached remote window. Booley's attach watcher patches a late install "
        'automatically; afterward run "Developer: Reload Window". Verify in the remote '
        "terminal with:\n"
        f"  {editor} --list-extensions --show-versions\n"
        f"which must list {PINNED_EXTENSION}. A host CLI without the remote "
        "window selected may install locally instead of into the container."
    )
