"""Resolve, claim, and clean Simulation Campaign run directories."""

from __future__ import annotations

import hashlib
import os
import stat
import sys
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import IO

from booley.flows.sim.campaign_reports import is_report_link
from booley.flows.sim.config import literal_run_cwd_error
from booley.runtime.booley_dir import checkout_runtime_dir
from booley.runtime.file_lock import LockTimeoutError, release_file_lock, wait_for_file_lock

from .codec import SimulationCampaignIntegrityError, canonical_json_bytes

_MARKER = ".booley-simulation-attempt.json"
_PARENT_MARKER = ".booley-simulation-parent.json"
_PARENT_LOCK = "parents.lock"
_FIELDS = frozenset({"campaign_id", "work_item_id", "attempt_id"})
_LOCK_TIMEOUT_S = 60.0
_CREATE_ATTEMPTS = 3


@dataclass(frozen=True, slots=True)
class RunDirectory:
    """One canonical resolved cwd and its cleanup ownership."""

    path: Path
    collision_key: str
    owned: bool
    lock_path: Path
    # Deepest directory above the attempt that no placeholder touches (kept, never
    # removed); None for literal directories.
    template_root: Path | None


def _template_root(configured: str, resolved: Path) -> Path | None:
    """Return the path before the first placeholder component of ``configured``.

    Every placeholder expands to exactly one component, so the rendered path has
    the same component count as the template and the root is a prefix of it.
    """
    parts = Path(configured).parts
    first = next((index for index, part in enumerate(parts) if "{" in part), None)
    if first is None:
        return None
    keep = len(resolved.parts) - len(parts) + first
    return Path(*resolved.parts[:keep]) if keep >= 1 else None


def _absolute(rendered: str, project_root: Path) -> Path:
    """Resolve one rendered run_cwd against the Project Root without following links."""
    path = Path(rendered)
    return (path if path.is_absolute() else project_root / path).absolute()


def literal_run_path(configured: str, *, project_root: Path) -> Path:
    """Return the absolute path a literal run_cwd names."""
    return _absolute(configured, project_root)


def require_literal_run_directory(path: Path) -> None:
    """Raise with the actionable remedy unless ``path`` is an existing real directory."""
    message = literal_run_cwd_error(path)
    if message is not None:
        raise SimulationCampaignIntegrityError(message)


def expand_run_directory(
    configured: str,
    *,
    project_root: Path,
    campaign_id: str,
    target_key: str,
    work_item_key: str,
    attempt_key: str,
) -> RunDirectory:
    """Expand the four supported placeholders and canonicalize collisions."""
    values = {
        "campaign": campaign_id,
        "target": target_key,
        "test": work_item_key,
        "attempt": attempt_key,
    }
    rendered = configured
    for name, value in values.items():
        if not value or any(character in value for character in ("/", "\\", "\0")):
            raise SimulationCampaignIntegrityError(
                f"run-directory placeholder {name!r} is not one component"
            )
        rendered = rendered.replace("{" + name + "}", value)
    if "{" in rendered or "}" in rendered or not rendered or "\0" in rendered:
        raise SimulationCampaignIntegrityError("run-directory template is invalid")
    resolved = _absolute(rendered, project_root)
    collision_key = os.path.normcase(str(resolved.resolve(strict=False)))
    lock_name = hashlib.sha256(collision_key.encode("utf-8")).hexdigest() + ".lock"
    return RunDirectory(
        path=resolved,
        collision_key=collision_key,
        owned=rendered != configured,
        lock_path=checkout_runtime_dir(project_root) / "simulation-run-locks" / lock_name,
        template_root=_template_root(configured, resolved) if rendered != configured else None,
    )


@contextmanager
def claimed_run_directory(
    run: RunDirectory,
    *,
    identity: Mapping[str, str],
) -> Iterator[Path]:
    """Claim a templated cwd, cleaning only the matching owned directory."""
    with (
        _held_path_lock(run.lock_path, discard=run.owned),
        _claimed_run_directory(run, identity=identity) as path,
    ):
        yield path


@contextmanager
def _claimed_run_directory(run: RunDirectory, *, identity: Mapping[str, str]) -> Iterator[Path]:
    """Apply ownership semantics while the canonical collision lock is held."""
    if set(identity) != _FIELDS or any(not value for value in identity.values()):
        raise SimulationCampaignIntegrityError("run-directory identity is incomplete")
    if not run.owned:
        require_literal_run_directory(run.path)
        yield run.path
        return
    _require_no_links(run.path)
    marker = run.path / _MARKER
    expected = canonical_json_bytes(dict(identity))
    created = False
    if run.path.exists():
        if not marker.is_file() or marker.read_bytes() != expected:
            raise SimulationCampaignIntegrityError(
                f"run directory is not owned by this attempt: {run.path}"
            )
    else:
        _create_run_directory(run, expected)
        created = True
    try:
        yield run.path
    finally:
        if marker.is_file() and marker.read_bytes() == expected:
            _remove_owned_tree(run.path)
            _remove_booley_parents(run)
        elif created:
            raise SimulationCampaignIntegrityError(
                f"run-directory ownership changed before cleanup: {run.path}"
            )


def cleanup_interrupted_run_directory(run: RunDirectory, *, identity: Mapping[str, str]) -> bool:
    """Clean one surviving owned cwd only when its marker matches exactly."""
    with _held_path_lock(run.lock_path, discard=run.owned):
        return _cleanup_interrupted_run_directory(run, identity=identity)


def _cleanup_interrupted_run_directory(run: RunDirectory, *, identity: Mapping[str, str]) -> bool:
    """Remove the attempt tree if it survives, then any empty parents Booley created."""
    if not run.owned:
        return False
    removed = False
    if run.path.exists():
        marker = run.path / _MARKER
        expected = canonical_json_bytes(dict(identity))
        if not marker.is_file() or marker.read_bytes() != expected:
            raise SimulationCampaignIntegrityError(
                f"refusing to clean unowned run directory: {run.path}"
            )
        _remove_owned_tree(run.path)
        removed = True
    _remove_booley_parents(run)
    return removed


def restore_run_directory(document: Mapping[str, object], *, project_root: Path) -> RunDirectory:
    """Reconstruct and authenticate one persisted attempt run directory."""
    return _restore_run_directory(document, checkout_runtime_dir(project_root))


def restore_run_directory_from_project_data(
    document: Mapping[str, object], *, project_data: Path
) -> RunDirectory:
    """Reconstruct a persisted run directory from resolved Booley Directory."""
    return _restore_run_directory(document, project_data / ".runtime")


def _restore_run_directory(document: Mapping[str, object], runtime: Path) -> RunDirectory:
    path = Path(str(document["resolved"]))
    collision_key = os.path.normcase(str(path.resolve(strict=False)))
    if collision_key != document["collision_key"]:
        raise SimulationCampaignIntegrityError(
            "persisted run-directory collision key disagrees with resolved path"
        )
    lock_name = hashlib.sha256(collision_key.encode("utf-8")).hexdigest() + ".lock"
    return RunDirectory(
        path=path,
        collision_key=collision_key,
        owned=bool(document["owned"]),
        lock_path=runtime / "simulation-run-locks" / lock_name,
        template_root=(
            _template_root(str(document["configured"]), path) if bool(document["owned"]) else None
        ),
    )


def _lock_is_current(lock: IO[str], lock_path: Path) -> bool:
    """Return whether ``lock`` is still the file ``lock_path`` names (not unlinked)."""
    try:
        current = lock_path.stat()
    except FileNotFoundError:
        return False
    held = os.fstat(lock.fileno())
    return (held.st_dev, held.st_ino) == (current.st_dev, current.st_ino)


def _acquire_current_lock(lock_path: Path) -> IO[str]:
    """Lock ``lock_path`` and return the handle, retrying when the file was discarded.

    A waiter that opened the file before its holder unlinked it acquires a dead
    inode; it must queue again on the current file or two runs could overlap.
    """
    deadline = time.monotonic() + _LOCK_TIMEOUT_S
    while time.monotonic() <= deadline:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock = lock_path.open("a+", encoding="utf-8")
        try:
            wait_for_file_lock(lock, timeout_s=max(0.0, deadline - time.monotonic()))
            if _lock_is_current(lock, lock_path):
                return lock
            release_file_lock(lock)
        except BaseException:
            lock.close()
            raise
        lock.close()
    raise LockTimeoutError(f"timed out waiting for run-directory lock: {lock_path}")


@contextmanager
def _held_path_lock(lock_path: Path, *, discard: bool) -> Iterator[None]:
    """Hold the lock at ``lock_path``; with ``discard`` unlink it again on exit.

    Discarding keeps one lock file per live attempt path instead of one per path
    ever used. The unlink follows a successful acquisition only.
    """
    lock = _acquire_current_lock(lock_path)
    try:
        yield
    finally:
        try:
            if discard and sys.platform != "win32":
                # A failed unlink only leaves a lock file; it must not mask the body's
                # error or keep the lock held.
                with suppress(OSError):
                    lock_path.unlink()
        finally:
            release_file_lock(lock)
            lock.close()
        if discard and sys.platform == "win32":
            # A waiter may still hold the file open; it then needs it, so keep it.
            with suppress(PermissionError, FileNotFoundError):
                lock_path.unlink()


def _write_marker(path: Path, content: bytes) -> None:
    """Write one ownership marker and make it durable."""
    path.write_bytes(content)
    _fsync_file(path)


def _create_run_directory(run: RunDirectory, expected: bytes) -> None:
    """Create the attempt directory, marking each parent Booley itself creates.

    Runs under the checkout-wide parent lock so a sibling never sees (and removes)
    a marked parent that is still being filled. Safety does not depend on the lock:
    removal only ever succeeds on an empty directory.
    """
    root = run.template_root
    if root is None or not run.path.is_relative_to(root):
        run.path.mkdir(parents=True)
        _write_marker(run.path / _MARKER, expected)
        return
    parents = run.path.relative_to(root).parts[:-1]
    with _held_path_lock(run.lock_path.parent / _PARENT_LOCK, discard=False):
        for _ in range(_CREATE_ATTEMPTS):
            try:
                _create_marked_chain(root, parents, run.path, expected)
            except FileNotFoundError:
                continue  # an ancestor vanished under us (unlocked sibling); start over
            return
    raise SimulationCampaignIntegrityError(f"run directory could not be created: {run.path}")


def _create_marked_chain(
    root: Path, parents: tuple[str, ...], attempt: Path, expected: bytes
) -> None:
    """Create ``root``, the missing marked ``parents`` below it, then the attempt dir."""
    root.mkdir(parents=True, exist_ok=True)
    current = root
    for name in parents:
        current = current / name
        try:
            current.mkdir()
        except FileExistsError:
            continue  # pre-existing or a sibling's: never ours to remove unmarked
        # No fsync: a lost parent marker only leaves the directory unmarked (safe).
        (current / _PARENT_MARKER).write_bytes(expected)
    attempt.mkdir()
    _write_marker(attempt / _MARKER, expected)


def _remove_booley_parents(run: RunDirectory) -> None:
    """Remove the empty, marked parents of ``run`` strictly below its template root.

    Walks upward and stops at the first link, unmarked directory, or directory
    holding anything besides its marker. A missing directory is skipped, so a
    resume after a partly finished walk completes it.
    """
    root = run.template_root
    if root is None or not run.path.is_relative_to(root):
        return
    with _held_path_lock(run.lock_path.parent / _PARENT_LOCK, discard=False):
        current = run.path.parent
        for _ in range(len(run.path.parts)):
            if current == root or not current.is_relative_to(root):
                return
            if not (current.is_symlink() or current.exists()):
                current = current.parent
                continue
            if not _remove_marked_empty_directory(current):
                return
            current = current.parent


def _remove_marked_empty_directory(directory: Path) -> bool:
    """Remove ``directory`` if it holds only Booley's parent marker; report success."""
    if is_report_link(directory):
        return False
    marker = directory / _PARENT_MARKER
    try:
        if [entry.name for entry in directory.iterdir()] != [_PARENT_MARKER]:
            return False
        if marker.is_symlink() or not marker.is_file():
            return False
        marker.unlink()
        directory.rmdir()
    except OSError:
        return False
    return True


def _remove_owned_tree(root: Path) -> None:
    for path in sorted(root.rglob("*"), key=lambda item: len(item.parts), reverse=True):
        if is_report_link(path):
            path.unlink()
        elif path.is_dir():
            path.rmdir()
        elif path.is_file():
            path.unlink()
        else:
            raise SimulationCampaignIntegrityError(f"unsafe run-directory entry: {path}")
    root.rmdir()


def _require_no_links(path: Path) -> None:
    existing = path
    while not existing.exists() and existing != existing.parent:
        existing = existing.parent
    for parent in (existing, *existing.parents):
        if is_report_link(parent):
            raise SimulationCampaignIntegrityError(f"run-directory path contains a link: {parent}")


def _fsync_file(path: Path) -> None:
    descriptor = os.open(path, os.O_RDWR | getattr(os, "O_NOFOLLOW", 0))
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise SimulationCampaignIntegrityError("ownership marker is not regular")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


__all__ = [
    "RunDirectory",
    "claimed_run_directory",
    "cleanup_interrupted_run_directory",
    "expand_run_directory",
    "literal_run_path",
    "require_literal_run_directory",
    "restore_run_directory",
    "restore_run_directory_from_project_data",
]
