"""Resolve, claim, and clean Simulation Campaign run directories."""

from __future__ import annotations

import hashlib
import os
import stat
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from booley.flows.sim.campaign_reports import is_report_link
from booley.runtime.file_lock import release_file_lock, wait_for_file_lock
from booley.runtime.project_dir import checkout_runtime_dir

from .codec import SimulationCampaignIntegrityError, canonical_json_bytes

_MARKER = ".booley-simulation-attempt.json"
_FIELDS = frozenset({"campaign_id", "work_item_id", "attempt_id"})


@dataclass(frozen=True, slots=True)
class RunDirectory:
    """One canonical resolved cwd and its cleanup ownership."""

    path: Path
    collision_key: str
    owned: bool
    lock_path: Path


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
    path = Path(rendered)
    resolved = (path if path.is_absolute() else project_root / path).absolute()
    collision_key = os.path.normcase(str(resolved.resolve(strict=False)))
    lock_name = hashlib.sha256(collision_key.encode("utf-8")).hexdigest() + ".lock"
    return RunDirectory(
        path=resolved,
        collision_key=collision_key,
        owned=rendered != configured,
        lock_path=checkout_runtime_dir(project_root) / "simulation-run-locks" / lock_name,
    )


@contextmanager
def claimed_run_directory(
    run: RunDirectory,
    *,
    identity: Mapping[str, str],
) -> Iterator[Path]:
    """Claim a templated cwd, cleaning only the matching owned directory."""
    run.lock_path.parent.mkdir(parents=True, exist_ok=True)
    with run.lock_path.open("a+", encoding="utf-8") as lock:
        wait_for_file_lock(lock, timeout_s=60)
        try:
            with _claimed_run_directory(run, identity=identity) as path:
                yield path
        finally:
            release_file_lock(lock)


@contextmanager
def _claimed_run_directory(run: RunDirectory, *, identity: Mapping[str, str]) -> Iterator[Path]:
    """Apply ownership semantics while the canonical collision lock is held."""
    if set(identity) != _FIELDS or any(not value for value in identity.values()):
        raise SimulationCampaignIntegrityError("run-directory identity is incomplete")
    if not run.owned:
        if is_report_link(run.path) or not run.path.is_dir():
            raise SimulationCampaignIntegrityError(
                f"literal run directory must already exist: {run.path}"
            )
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
        run.path.mkdir(parents=True)
        marker.write_bytes(expected)
        _fsync_file(marker)
        created = True
    try:
        yield run.path
    finally:
        if marker.is_file() and marker.read_bytes() == expected:
            _remove_owned_tree(run.path)
        elif created:
            raise SimulationCampaignIntegrityError(
                f"run-directory ownership changed before cleanup: {run.path}"
            )


def cleanup_interrupted_run_directory(run: RunDirectory, *, identity: Mapping[str, str]) -> bool:
    """Clean one surviving owned cwd only when its marker matches exactly."""
    run.lock_path.parent.mkdir(parents=True, exist_ok=True)
    with run.lock_path.open("a+", encoding="utf-8") as lock:
        wait_for_file_lock(lock, timeout_s=60)
        try:
            return _cleanup_interrupted_run_directory(run, identity=identity)
        finally:
            release_file_lock(lock)


def _cleanup_interrupted_run_directory(run: RunDirectory, *, identity: Mapping[str, str]) -> bool:
    if not run.owned or not run.path.exists():
        return False
    marker = run.path / _MARKER
    expected = canonical_json_bytes(dict(identity))
    if not marker.is_file() or marker.read_bytes() != expected:
        raise SimulationCampaignIntegrityError(
            f"refusing to clean unowned run directory: {run.path}"
        )
    _remove_owned_tree(run.path)
    return True


def restore_run_directory(document: Mapping[str, object], *, project_root: Path) -> RunDirectory:
    """Reconstruct and authenticate one persisted attempt run directory."""
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
        lock_path=checkout_runtime_dir(project_root) / "simulation-run-locks" / lock_name,
    )


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
    "restore_run_directory",
]
