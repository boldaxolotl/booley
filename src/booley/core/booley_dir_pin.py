"""Host-side pin of the target behind a symlinked ``<root>/.booley`` entry.

A Project may link its Booley Directory entry to a directory outside the
checkout. The Sandbox binds that entry, so a link retargeted after setup would
silently hand the Sandbox (and every host command) a different directory. The
pin records the resolved target plus its device and inode in host-private state
at ``booley init``; every later host use of the linked entry must match it.

Real entries, host ``BOOLEY_DIR`` selections and the Sandbox itself need no pin.
Stdlib plus ``core`` only, like :mod:`booley.core.booley_layout`.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path

from booley.core.booley_layout import BOOLEY_DIR_NAME, BooleyLayoutError
from booley.core.private_store import PrivateStore
from booley.core.user_paths import config_dir

_SCHEMA_VERSION = 1
_REPIN_REMEDY = "run `booley init --repin-booley-dir` from the Project Root to replace the pin"
_INIT_REMEDY = "run `booley init` from the Project Root to pin the linked entry"


class BooleyDirPinError(BooleyLayoutError):
    """A linked Booley Directory entry is unpinned or no longer matches its pin."""

    def __init__(self, message: str, remedy: str = _REPIN_REMEDY) -> None:
        # ``PrivateStore`` raises this type with a message alone, so the remedy defaults.
        super().__init__(f"{message}; {remedy}")
        self.detail = message
        self.remedy = remedy


@dataclass(frozen=True)
class EntryPin:
    """The resolved target of a linked entry and its filesystem identity."""

    target: str
    device: int
    inode: int


def _store() -> PrivateStore:
    return PrivateStore(
        config_dir() / "booley-dir-pins",
        config_dir().parent,
        "Booley Directory pin",
        BooleyDirPinError,
    )


def _pin_filename(project_root: Path) -> str:
    spelled = os.path.normcase(str(project_root.resolve()))
    return hashlib.sha256(spelled.encode()).hexdigest() + ".json"


def current_target(entry: Path) -> EntryPin:
    """Resolve *entry* strictly and stat what it points at."""
    resolved = entry.resolve(strict=True)
    info = resolved.stat()
    return EntryPin(str(resolved), info.st_dev, info.st_ino)


def read_pin(project_root: Path) -> EntryPin | None:
    """Return the stored pin, or ``None`` only when none was ever written."""
    store = _store()
    if not store.validate_existing_directory():
        return None
    filename = _pin_filename(project_root)
    if not (store.root / filename).is_symlink() and not (store.root / filename).exists():
        return None
    try:
        document = store.read_json(filename)
        if not isinstance(document, dict) or document["schema_version"] != _SCHEMA_VERSION:
            raise TypeError("unsupported schema")
        target, device, inode = document["target"], document["device"], document["inode"]
        if not isinstance(target, str) or type(device) is not int or type(inode) is not int:
            raise TypeError("invalid field types")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        # ValueError covers truncated JSON; OSError an unreadable file.
        raise BooleyDirPinError(
            f"Booley Directory pin for {project_root} is unreadable ({exc})", _REPIN_REMEDY
        ) from exc
    return EntryPin(target, device, inode)


def write_pin(project_root: Path, pin: EntryPin) -> None:
    """Persist *pin* for *project_root* atomically in host-private state."""
    store = _store()
    store.ensure_directory()
    document = {
        "schema_version": _SCHEMA_VERSION,
        # Informational only: names the Project when someone inspects the file.
        "project_root": str(project_root.resolve()),
        "target": pin.target,
        "device": pin.device,
        "inode": pin.inode,
    }
    store.atomic_write_text(_pin_filename(project_root), json.dumps(document, indent=2) + "\n")


def pin_difference(pinned: EntryPin, current: EntryPin) -> str | None:
    """Describe how *current* departs from *pinned*, or ``None`` when it matches."""
    if current.target != pinned.target:
        return f"pinned to {pinned.target} but now resolves to {current.target}"
    if (current.device, current.inode) != (pinned.device, pinned.inode):
        return f"pinned target {pinned.target} was replaced (device/inode changed)"
    return None


def entry_needs_pin(project_root: Path) -> bool:
    """Whether host use of ``<root>/.booley`` is subject to the pin."""
    if os.environ.get("BOOLEY_CONTAINER") == "1":
        return False
    return (project_root / BOOLEY_DIR_NAME).is_symlink()


def require_pinned_entry(project_root: Path) -> None:
    """Refuse a linked entry that is unpinned, retargeted, or replaced.

    A dangling or looping link is left to the callers' own "unavailable" errors.
    """
    if not entry_needs_pin(project_root):
        return
    entry = project_root / BOOLEY_DIR_NAME
    try:
        current = current_target(entry)
    except (OSError, RuntimeError):
        return
    pinned = read_pin(project_root)
    if pinned is None:
        raise BooleyDirPinError(
            f"{entry} is a symlink to {current.target} that Booley has not pinned", _INIT_REMEDY
        )
    difference = pin_difference(pinned, current)
    if difference is not None:
        raise BooleyDirPinError(
            f"{entry} is {difference}",
            "if you moved the link on purpose, run `booley init --repin-booley-dir` from the "
            f"Project Root; otherwise point {BOOLEY_DIR_NAME} back at {pinned.target}",
        )
