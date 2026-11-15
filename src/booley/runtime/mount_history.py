"""Former Sandbox mount points of the Booley Directory, and path translation.

Records store absolute Sandbox paths. When Session Issuance moves the mount
point, ``record_former_mount`` keeps the previous one in the mounted Booley
Directory, and ``translate_stored_path`` rebases stored paths onto the current
mount when a record is parsed. Paths outside every former mount are unchanged.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path, PurePosixPath
from typing import Any

from booley.runtime import sandbox_layout
from booley.runtime.atomic_files import atomic_replace_bytes

MOUNT_HISTORY = Path(".runtime") / "sandbox-mounts.json"
_SCHEMA = 1


class MountHistoryError(RuntimeError):
    """The mount history cannot be read or extended.

    A ``RuntimeError`` on purpose: ``ValueError`` from a record parse reads as an
    absent record, which would hide an existing Job.
    """


def _history_path() -> Path:
    return Path(sandbox_layout.BOOLEY_DIR_TARGET) / MOUNT_HISTORY


def _read(path: Path) -> list[str]:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    except (OSError, UnicodeDecodeError) as exc:
        raise MountHistoryError(f"cannot read {path}: {exc}") from exc
    try:
        data: Any = json.loads(text)
        mounts = data["mounts"]
        if data["schema"] != _SCHEMA or not isinstance(mounts, list):
            raise ValueError("unsupported mount history")
        if not all(isinstance(item, str) for item in mounts):
            raise ValueError("mount points must be strings")
    except (ValueError, KeyError, TypeError) as exc:
        raise MountHistoryError(f"malformed mount history {path}: {exc}") from exc
    return [str(item) for item in mounts]


def former_mounts() -> tuple[str, ...]:
    """The former mount points recorded in the mounted Booley Directory.

    Raises ``MountHistoryError`` when the history cannot be read; no history is empty.
    """
    return tuple(_read(_history_path()))


def rebase(path: Path, former: Iterable[str]) -> Path | None:
    """Rebase *path* from the first of the *former* mounts holding it; ``None`` outside all."""
    for mount in former:
        if path.is_relative_to(mount):
            return Path(sandbox_layout.BOOLEY_DIR_TARGET) / path.relative_to(mount)
    return None


def _former(record: str) -> tuple[str, ...]:
    try:
        return former_mounts()
    except MountHistoryError as exc:
        raise MountHistoryError(
            f"{record} cannot be read: {exc}; restore {_history_path()}, which lists the former "
            "mount points and is written by `booley init` when the mount point changes"
        ) from exc


def translate_stored_path(value: str, *, record: str) -> str:
    """Rebase *value* from a former mount point onto the current one."""
    current = Path(sandbox_layout.BOOLEY_DIR_TARGET)
    path = Path(value)
    if not path.is_absolute() or path.is_relative_to(current):
        return value
    rebased = rebase(path, _former(record))
    return value if rebased is None else str(rebased)


def former_spellings(value: str, *, record: str) -> tuple[str, ...]:
    """*value*, a path under the current mount, as each former mount spelled it; () otherwise.

    The current mount counts by its lexical spelling or its resolved directory, because the
    image makes the mount a symlink and attribution resolves paths before it derives keys.
    """
    path = Path(value)
    if not path.is_absolute():
        return ()
    current = Path(sandbox_layout.BOOLEY_DIR_TARGET)
    for mount in dict.fromkeys((current, current.resolve())):
        if path.is_relative_to(mount):
            relative = path.relative_to(mount)
            return tuple(str(Path(former) / relative) for former in _former(record))
    return ()


def _former_mount_of(spec: bytes) -> str | None:
    try:
        data: Any = json.loads(spec)
        value = data["remoteEnv"]["BOOLEY_DIR"]
    except (ValueError, KeyError, TypeError):
        return None
    return value if isinstance(value, str) else None


def _require_mount(value: str, spec_file: Path) -> None:
    # Sandbox paths are POSIX whatever the host: a native Windows host issues the spec too.
    path = PurePosixPath(value)
    work = PurePosixPath(sandbox_layout.WORK_DIR)
    if (
        not (path.is_absolute() or Path(value).is_absolute())
        or path == PurePosixPath("/")
        or path.is_relative_to(work)
        or work.is_relative_to(path)
    ):
        raise MountHistoryError(
            f"{spec_file} names an unusable Booley Directory mount {value!r}; "
            "remove it and run `booley init` again"
        )


def record_former_mount(
    source: Path, previous: bytes | None, current: str, spec_file: Path
) -> None:
    """Remember the mount named by the *previous* spec when it differs from *current*."""
    former = None if previous is None else _former_mount_of(previous)
    if former is None or former == current:
        return
    _require_mount(former, spec_file)
    path = source / MOUNT_HISTORY
    mounts = _read(path)
    if former in mounts:
        return
    payload = {"schema": _SCHEMA, "mounts": [*mounts, former]}
    try:
        atomic_replace_bytes(path, json.dumps(payload, indent=2).encode("utf-8") + b"\n")
    except OSError as exc:
        raise MountHistoryError(f"cannot record the former mount in {path}: {exc}") from exc
