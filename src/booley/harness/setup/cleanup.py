"""Bounded, manifest-driven cleanup for Project Setup.

Project Setup creates useful reports beside disposable command output.  This
module owns the small transaction needed to separate those two classes.  It
never discovers deletion targets from ``.gitignore`` or from an age filter:
every removal candidate must come from the current setup run's manifest.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import stat
import uuid
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Literal

from booley.core.boundary import require_dict, require_list
from booley.feedback import materialize
from booley.feedback.render import Environment
from booley.harness.feedback_environment import resolve_feedback_environment
from booley.runtime.process_group import ProcessGroup, is_process_group_alive
from booley.runtime.project_dir import PROJECT_DIR_NAME, resolve_checkout_project_dir

MANIFEST_VERSION = 1
SETUP_ROOT = Path(PROJECT_DIR_NAME)
LEGACY_GROUPS = (Path(".runtime"), Path("tmp"), Path("logs"))
Category = Literal["preserve", "remove", "evict-cache", "unresolved"]


class CleanupError(RuntimeError):
    """Base class for cleanup validation and transaction failures."""


class CleanupBlockedError(CleanupError):
    """Raised when the caller cannot prove a safe mutation boundary."""


@dataclass(frozen=True)
class FileIdentity:
    """The no-follow identity recorded before a candidate is mutated."""

    kind: str
    device: int
    inode: int
    size: int
    mode: int

    @classmethod
    def from_path(cls, path: Path) -> FileIdentity:
        info = path.lstat()
        if stat.S_ISREG(info.st_mode):
            kind = "file"
        elif stat.S_ISDIR(info.st_mode):
            kind = "directory"
        elif stat.S_ISLNK(info.st_mode):
            kind = "symlink"
        else:
            kind = "special"
        size = info.st_size if kind == "file" else 0
        return cls(kind, info.st_dev, info.st_ino, size, stat.S_IMODE(info.st_mode))

    def matches(self, path: Path) -> bool:
        try:
            current = FileIdentity.from_path(path)
        except FileNotFoundError:
            return False
        if (
            current.kind != self.kind
            or current.device != self.device
            or current.inode != self.inode
        ):
            return False
        return current.size == self.size and current.mode == self.mode


@dataclass(frozen=True)
class ManifestEntry:
    """One setup-owned path and the evidence supporting its disposition."""

    path: str
    producer: str
    artifact_class: str
    disposition: str
    identity: FileIdentity
    dependencies: tuple[str, ...] = ()
    active: bool = False
    process_group_id: int | None = None
    job_record: str | None = None

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> ManifestEntry:
        try:
            identity = FileIdentity(**require_dict(raw.get("identity"), field="artifact identity"))
        except (TypeError, ValueError, KeyError) as exc:
            raise CleanupError(f"invalid artifact identity: {exc}") from exc
        raw_dependencies = require_list(raw.get("dependencies", []), field="artifact dependencies")
        if not all(isinstance(item, str) for item in raw_dependencies):
            raise CleanupError("artifact dependencies must be strings")
        dependencies = tuple(raw_dependencies)
        process_group_id = raw.get("process_group_id")
        if process_group_id is not None and (
            isinstance(process_group_id, bool)
            or not isinstance(process_group_id, int)
            or process_group_id <= 0
        ):
            raise CleanupError("process_group_id must be a positive integer")
        active = raw.get("active", False)
        if not isinstance(active, bool):
            raise CleanupError("active must be a boolean")
        return cls(
            path=str(raw["path"]),
            producer=str(raw.get("producer", "unknown")),
            artifact_class=str(raw.get("artifact_class", "unknown")),
            disposition=str(raw.get("disposition", "remove")),
            identity=identity,
            dependencies=dependencies,
            active=active,
            process_group_id=process_group_id,
            job_record=str(raw["job_record"]) if raw.get("job_record") else None,
        )

    def as_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["dependencies"] = list(self.dependencies)
        return value


@dataclass(frozen=True)
class CleanupItem:
    """A preview result for one bounded candidate."""

    path: str
    category: Category
    bytes: int
    reason: str
    identity: FileIdentity | None = None

    def as_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["identity"] = asdict(self.identity) if self.identity else None
        return value


@dataclass(frozen=True)
class CleanupPlan:
    """Immutable preview returned to callers and accepted by ``apply``."""

    project_root: str
    run_id: str | None
    manifest_path: str | None
    inventory_only: bool
    retention_mode: str
    cache_disposition: str
    items: tuple[CleanupItem, ...]
    digest: str

    def grouped(self) -> dict[str, dict[str, int]]:
        result: dict[str, dict[str, int]] = {}
        for category in ("preserve", "remove", "evict-cache", "unresolved"):
            selected = [item for item in self.items if item.category == category]
            result[category] = {"count": len(selected), "bytes": sum(i.bytes for i in selected)}
        return result

    def as_dict(self) -> dict[str, Any]:
        return {
            "project_root": self.project_root,
            "run_id": self.run_id,
            "manifest_path": self.manifest_path,
            "inventory_only": self.inventory_only,
            "retention_mode": self.retention_mode,
            "cache_disposition": self.cache_disposition,
            "items": [item.as_dict() for item in self.items],
            "digest": self.digest,
            "groups": self.grouped(),
        }


@dataclass(frozen=True)
class CleanupResult:
    """Structured outcome of an apply operation."""

    digest: str
    removed: tuple[str, ...] = ()
    already_absent: tuple[str, ...] = ()
    evicted: tuple[str, ...] = ()
    unresolved: tuple[str, ...] = ()
    bytes_removed: int = 0

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _project_root(root: Path | None) -> Path:
    """Resolve and validate the checkout containing the Project directory."""
    resolved = (root or Path.cwd()).resolve()
    try:
        project_dir = resolve_checkout_project_dir(resolved)
    except (OSError, ValueError) as exc:
        raise CleanupError(f"cannot resolve the Project directory: {exc}") from exc
    if (
        project_dir != resolved / SETUP_ROOT
        or project_dir.is_symlink()
        or not project_dir.is_dir()
    ):
        raise CleanupError("no .booley_project directory was found")
    return resolved


def _safe_relative(root: Path, raw: str | Path) -> str:
    """Return a safe checkout-relative path, rejecting all escape forms."""
    candidate = Path(raw).expanduser()
    if candidate.is_absolute():
        candidate = Path(os.path.relpath(candidate, root))
    normalized = Path(os.path.normpath(str(candidate)))
    parts = normalized.parts
    if not parts or parts[0] != SETUP_ROOT.name or ".." in parts:
        raise CleanupError(f"path is outside {SETUP_ROOT}: {raw}")
    if ".git" in parts:
        raise CleanupError(f"Git metadata is never a cleanup target: {raw}")
    absolute = root / normalized
    resolved = absolute.resolve(strict=False)
    project_dir = root / SETUP_ROOT
    if project_dir.is_symlink():
        raise CleanupError(f"the Project directory must not be a symlink: {project_dir}")
    project_dir = project_dir.resolve()
    try:
        resolved.relative_to(project_dir)
    except ValueError as exc:
        raise CleanupError(f"path traverses outside {SETUP_ROOT}: {raw}") from exc
    if resolved == project_dir:
        raise CleanupError("the .booley_project root is never a cleanup target")
    return normalized.as_posix()


def _absolute(root: Path, relative: str) -> Path:
    """Resolve a previously validated manifest path without following it."""
    return root / Path(_safe_relative(root, relative))


def _reject_shared_boundary(root: Path, absolute: Path, manifest_path: Path) -> None:
    """Reject roots and control files that cannot be bounded as leaves."""
    project_dir = root / SETUP_ROOT
    forbidden = {project_dir, project_dir / "tmp", manifest_path.parent, manifest_path}
    if absolute in forbidden:
        raise CleanupError(f"shared or cleanup-control path is not a candidate: {absolute}")


def _manifest_path(root: Path, run_id: str) -> Path:
    """Return the only supported manifest location for a setup run."""
    if not run_id or run_id in {".", ".."} or Path(run_id).name != run_id or not run_id.isascii():
        raise CleanupError("run_id must be one safe ASCII path component")
    return root / SETUP_ROOT / "tmp" / "setup" / run_id / "manifest.json"


def _atomic_write(path: Path, payload: dict[str, Any]) -> None:
    """Publish one complete JSON state file beside its previous generation."""
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(6)}.tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def _new_manifest(run_id: str, root: Path) -> dict[str, Any]:
    """Create an empty manifest with a recovery journal."""
    scratch = _manifest_path(root, run_id).parent
    scratch.mkdir(parents=True, exist_ok=True)
    payload = {"version": MANIFEST_VERSION, "run_id": run_id, "entries": [], "journal": {}}
    _atomic_write(scratch / "manifest.json", payload)
    return payload


def prepare_run(project_root: Path | None = None, run_id: str | None = None) -> Path:
    """Allocate a collision-resistant setup scratch root and its manifest."""
    root = _project_root(project_root)
    chosen = run_id or uuid.uuid4().hex
    path = _manifest_path(root, chosen)
    if path.exists():
        raise CleanupError(f"setup run {chosen!r} already has a manifest")
    _new_manifest(chosen, root)
    return path.parent


def _load_manifest(path: Path, root: Path) -> dict[str, Any]:
    """Load and validate one manifest before it can influence cleanup."""
    try:
        payload = require_dict(
            json.loads(path.read_text(encoding="utf-8")), field="cleanup manifest"
        )
    except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise CleanupError(f"cannot read cleanup manifest {path}: {exc}") from exc
    if payload.get("version") != MANIFEST_VERSION:
        raise CleanupError("unsupported cleanup manifest version")
    expected = _manifest_path(root, str(payload.get("run_id", "")))
    expected_parent = expected.parent.parent
    valid_quarantine = path.parent.parent == expected_parent and path.parent.name.startswith(
        ".booley-cleanup-"
    )
    if path.resolve() != expected.resolve() and not valid_quarantine:
        raise CleanupError("manifest is not beneath its recorded setup run root")
    try:
        raw_entries = require_list(payload.get("entries", []), field="manifest entries")
        journal = require_dict(payload.get("journal", {}), field="manifest journal")
    except (TypeError, ValueError) as exc:
        raise CleanupError(f"invalid cleanup manifest structure: {exc}") from exc
    entries = []
    for raw in raw_entries:
        try:
            entry = ManifestEntry.from_dict(require_dict(raw, field="manifest entry"))
        except (TypeError, ValueError, KeyError) as exc:
            raise CleanupError(f"invalid manifest entry: {exc}") from exc
        relative = _safe_relative(root, entry.path)
        _reject_shared_boundary(root, _absolute(root, relative), path)
        entries.append(entry)
    payload["entries"] = [entry.as_dict() for entry in entries]
    payload["journal"] = journal
    return payload


def _entry_path(root: Path, entry: ManifestEntry) -> Path:
    """Resolve an entry and reject unsafe final symlinks at mutation time."""
    path = _absolute(root, entry.path)
    project_dir = (root / SETUP_ROOT).resolve()
    for parent in path.parents:
        if parent == project_dir:
            break
        if parent.is_symlink():
            raise CleanupBlockedError(f"candidate has a symlinked parent: {entry.path}")
    if path.is_symlink():
        raise CleanupBlockedError(f"candidate became a symlink: {entry.path}")
    return path


def _active_use_reason(entry: ManifestEntry) -> str | None:
    """Return a live process or Job claim that blocks mutation."""
    if entry.active:
        return "active-use claim"
    if entry.process_group_id is not None and is_process_group_alive(
        ProcessGroup(entry.process_group_id)
    ):
        return f"process group {entry.process_group_id} is still alive"
    if entry.job_record:
        from booley.runtime import job_records
        from booley.runtime.pid import is_pid_alive

        record_path = Path(entry.job_record)
        if record_path.is_file():
            record = job_records.read_record(record_path.stem, record_path.parent)
            if record is None:
                return f"Job record is unreadable: {record_path}"
            if job_records.is_active(record, is_pid_alive):
                return f"Job record is still active: {record_path}"
    return None


def _descendants(root: Path, path: Path) -> Iterable[Path]:
    """Yield a bounded tree without following symlinks."""
    if not path.is_dir():
        return ()
    found: list[Path] = []
    for child in path.rglob("*"):
        if child.is_symlink() or child.is_file() or child.is_dir():
            found.append(child)
    return tuple(found)


def _manifest_entries(payload: dict[str, Any]) -> list[ManifestEntry]:
    """Decode the manifest entries after the document boundary was checked."""
    return [ManifestEntry.from_dict(raw) for raw in payload["entries"]]


def _bounded_entries(
    root: Path,
    absolute: Path,
    *,
    producer: str,
    artifact_class: str,
    disposition: str,
    active: bool,
    process_group_id: int | None,
    job_record: str | None,
) -> list[ManifestEntry]:
    """Expand a recorded directory into its exact, no-discovery children."""
    identity = FileIdentity.from_path(absolute)
    if identity.kind == "symlink":
        raise CleanupError(f"symlinks cannot be setup-owned artifacts: {absolute}")
    entries = [
        ManifestEntry(
            _safe_relative(root, absolute),
            producer,
            artifact_class,
            disposition,
            identity,
            (),
            active,
            process_group_id,
            job_record,
        )
    ]
    for child in _descendants(root, absolute):
        child_relative = _safe_relative(root, child)
        child_identity = FileIdentity.from_path(child)
        if child_identity.kind in {"symlink", "special"}:
            raise CleanupError(f"bounded artifact contains an unsupported child: {child_relative}")
        entries.append(
            ManifestEntry(
                child_relative,
                producer,
                artifact_class,
                disposition,
                child_identity,
                (),
                active,
                process_group_id,
                job_record,
            )
        )
    return entries


def record_artifact(
    project_root: Path | None,
    run_id: str,
    path: Path | str,
    *,
    producer: str,
    artifact_class: str,
    disposition: str = "remove",
    dependencies: Iterable[Path | str] = (),
    active: bool = False,
    process_group_id: int | None = None,
    job_record: Path | str | None = None,
) -> ManifestEntry:
    """Record an existing setup-created path before it is eligible for removal."""
    root = _project_root(project_root)
    manifest_path = _manifest_path(root, run_id)
    payload = _load_manifest(manifest_path, root)
    relative = _safe_relative(root, path)
    absolute = _absolute(root, relative)
    _reject_shared_boundary(root, absolute, manifest_path)
    if not absolute.exists() and not absolute.is_symlink():
        raise CleanupError(f"cannot record missing artifact: {relative}")
    dep_paths = tuple(_safe_relative(root, item) for item in dependencies)
    job_path = str(job_record) if job_record is not None else None
    new_entries = _bounded_entries(
        root,
        absolute,
        producer=producer,
        artifact_class=artifact_class,
        disposition=disposition,
        active=active,
        process_group_id=process_group_id,
        job_record=job_path,
    )
    new_entries[0] = replace(new_entries[0], dependencies=dep_paths)
    entries = _manifest_entries(payload)
    entries = [old for old in entries if old.path != relative]
    entries.extend(new_entries)
    unique = {item.path: item for item in entries}
    payload["entries"] = [
        item.as_dict() for item in sorted(unique.values(), key=lambda item: item.path)
    ]
    _atomic_write(manifest_path, payload)
    return new_entries[0]


def _read_plan_ledger(root: Path) -> dict[str, str]:
    """Read the small execution ledger embedded in SETUP-PLAN.md."""
    plan = root / SETUP_ROOT / "SETUP-PLAN.md"
    if not plan.is_file():
        return {}
    values: dict[str, str] = {}
    for line in plan.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.lstrip().startswith("-") or ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip(" -*").lower().replace(" ", "_")
        values[key] = value.strip().strip("`")
    return values


def _find_manifest(root: Path, run_id: str | None) -> Path | None:
    """Resolve only a manifest named by the execution ledger or its caller."""
    ledger = _read_plan_ledger(root)
    chosen = run_id or ledger.get("run_id")
    if not chosen:
        return None
    path = _manifest_path(root, chosen)
    if path.is_file():
        return path
    setup_root = path.parent.parent
    for quarantine in setup_root.glob(f".booley-cleanup-*-{chosen}"):
        candidate = quarantine / "manifest.json"
        if candidate.is_file():
            return candidate
    return None


def _size(path: Path) -> int:
    """Return bounded, no-follow size for inventory and accounting."""
    try:
        if path.is_symlink():
            return 0
        if path.is_file():
            return path.stat().st_size
        if path.is_dir():
            return sum(_size(child) for child in path.iterdir())
    except OSError:
        return 0
    return 0


def _legacy_inventory(root: Path) -> tuple[CleanupItem, ...]:
    """Inventory obvious residue without adopting any path as owned."""
    project_dir = root / SETUP_ROOT
    items: list[CleanupItem] = []
    for group in LEGACY_GROUPS:
        path = project_dir / group
        if path.exists():
            relative = (SETUP_ROOT / group).as_posix()
            items.append(CleanupItem(relative, "unresolved", _size(path), "ownership unavailable"))
    for path in project_dir.glob("*.log"):
        items.append(
            CleanupItem(
                (SETUP_ROOT / path.name).as_posix(),
                "unresolved",
                _size(path),
                "pre-existing or unmanifested log",
            )
        )
    return tuple(items)


def _manifest_digest(payload: dict[str, Any], items: Iterable[CleanupItem]) -> str:
    """Hash the immutable candidate set accepted by ``apply``."""
    stable_manifest = {key: value for key, value in payload.items() if key != "journal"}
    body = {"manifest": stable_manifest, "items": [item.as_dict() for item in items]}
    encoded = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _item_for_entry(
    root: Path,
    entry: ManifestEntry,
    retention_mode: str,
    cache_disposition: str,
) -> CleanupItem:
    """Classify one manifest entry without mutating it."""
    try:
        _entry_path(root, entry)
    except CleanupError as exc:
        return CleanupItem(entry.path, "unresolved", 0, str(exc), entry.identity)
    recorded_bytes = entry.identity.size
    active_reason = _active_use_reason(entry)
    if active_reason:
        return CleanupItem(entry.path, "unresolved", recorded_bytes, active_reason, entry.identity)
    if entry.disposition == "preserve" or (
        retention_mode == "diagnostic" and entry.artifact_class == "raw-evidence"
    ):
        category: Category = "preserve"
        reason = "manifest retention decision"
    elif entry.artifact_class == "flow-cache":
        category = "evict-cache" if cache_disposition == "evict-setup-touched" else "preserve"
        reason = (
            "explicit Flow-cache eviction" if category == "evict-cache" else "reusable Flow cache"
        )
    elif entry.disposition == "remove":
        category = "remove"
        reason = f"owned by {entry.producer}"
    else:
        category = "unresolved"
        reason = f"unsupported disposition {entry.disposition!r}"
    return CleanupItem(entry.path, category, recorded_bytes, reason, entry.identity)


def preview_cleanup(
    project_root: Path | None = None,
    *,
    run_id: str | None = None,
    retention_mode: str = "minimal",
    cache_disposition: str = "preserve",
) -> CleanupPlan:
    """Build an immutable cleanup plan; legacy projects are inventory-only."""
    if retention_mode not in {"minimal", "diagnostic"}:
        raise CleanupError("retention mode must be minimal or diagnostic")
    if cache_disposition not in {"preserve", "evict-setup-touched"}:
        raise CleanupError("cache disposition is invalid")
    root = _project_root(project_root)
    manifest_path = _find_manifest(root, run_id)
    if manifest_path is None:
        items = _legacy_inventory(root)
        digest = hashlib.sha256(
            json.dumps([item.as_dict() for item in items], sort_keys=True).encode()
        ).hexdigest()
        return CleanupPlan(
            str(root), None, None, True, retention_mode, cache_disposition, items, digest
        )
    payload = _load_manifest(manifest_path, root)
    entries = [ManifestEntry.from_dict(raw) for raw in payload["entries"]]
    items = [_item_for_entry(root, entry, retention_mode, cache_disposition) for entry in entries]
    scratch = manifest_path.parent
    if scratch.exists():
        identity = FileIdentity.from_path(scratch)
        items.append(
            CleanupItem(
                (SETUP_ROOT / "tmp" / "setup" / str(payload["run_id"])).as_posix(),
                "remove",
                0,
                "current run scratch root",
                identity,
            )
        )
    items = tuple(items)
    digest = _manifest_digest(payload, items)
    return CleanupPlan(
        str(root),
        str(payload["run_id"]),
        str(manifest_path),
        False,
        retention_mode,
        cache_disposition,
        items,
        digest,
    )


def _load_plan_manifest(plan: CleanupPlan) -> tuple[Path, dict[str, Any]]:
    """Reload the manifest named by a preview before applying it."""
    if plan.inventory_only or not plan.manifest_path:
        raise CleanupBlockedError(
            "legacy inventory has no ownership evidence; nothing may be deleted"
        )
    root = Path(plan.project_root)
    path = Path(plan.manifest_path)
    payload = _load_manifest(path, root)
    current = preview_cleanup(
        root,
        run_id=plan.run_id,
        retention_mode=plan.retention_mode,
        cache_disposition=plan.cache_disposition,
    )
    if current.digest != plan.digest:
        raise CleanupBlockedError("cleanup preview changed; request a new preview")
    return path, payload


def _quarantine_path(path: Path) -> Path:
    """Choose a unique sibling quarantine on the candidate's filesystem."""
    return path.with_name(f".booley-cleanup-{uuid.uuid4().hex}-{path.name}")


def _open_parent_nofollow(path: Path) -> int:
    """Open a candidate parent without following a boundary symlink."""
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    directory = getattr(os, "O_DIRECTORY", 0)
    if not nofollow or not directory:
        raise OSError("no-follow directory operations are unavailable")
    cloexec = getattr(os, "O_CLOEXEC", 0)
    return os.open(path, os.O_RDONLY | cloexec | directory | nofollow)


def _rename_nofollow(path: Path, quarantine: Path) -> None:
    """Rename within one opened parent directory without following parents."""
    if os.name == "nt":
        if path.is_symlink() or path.parent.is_symlink() or quarantine.parent.is_symlink():
            raise OSError("symlinked cleanup boundary")
        path.rename(quarantine)
        return
    fd = _open_parent_nofollow(path.parent)
    try:
        os.rename(path.name, quarantine.name, src_dir_fd=fd, dst_dir_fd=fd)
    finally:
        os.close(fd)


def _delete_quarantine(path: Path) -> None:
    """Delete only an already-quarantined, identity-checked path."""
    if os.name == "nt":
        if path.is_symlink():
            raise OSError("symlinked quarantine")
        if path.is_dir():
            path.rmdir()
        else:
            path.unlink()
        return
    fd = _open_parent_nofollow(path.parent)
    try:
        info = os.stat(path.name, dir_fd=fd, follow_symlinks=False)
        if stat.S_ISDIR(info.st_mode):
            os.rmdir(path.name, dir_fd=fd)
        else:
            os.unlink(path.name, dir_fd=fd)
    except FileNotFoundError:
        return
    finally:
        os.close(fd)


def _delete_scratch_quarantine(path: Path) -> None:
    """Delete a quarantined scratch root after removing its manifest."""
    if not path.is_dir() or path.is_symlink():
        raise OSError(f"scratch quarantine is not a directory: {path}")
    contents = tuple(path.iterdir())
    if len(contents) != 1 or contents[0].name != "manifest.json":
        raise OSError(f"scratch quarantine contains unexpected data: {path}")
    _delete_quarantine(contents[0])
    _delete_quarantine(path)


def _apply_entry(  # noqa: PLR0911, PLR0912 -- each fail-closed boundary is explicit
    root: Path,
    entry: ManifestEntry,
    journal: dict[str, Any],
    checkpoint: Callable[[], None],
) -> tuple[str, int, str | None]:
    """Move one candidate through sibling quarantine with identity checks."""
    state = journal.get(entry.path, {})
    if state.get("status") == "done":
        return "removed", 0, None
    quarantine = Path(state["quarantine"]) if state.get("quarantine") else None
    if quarantine and (quarantine.exists() or quarantine.is_symlink()):
        if not entry.identity.matches(quarantine):
            return "unresolved", 0, "quarantine identity changed"
        _delete_quarantine(quarantine)
        journal[entry.path] = {"status": "done"}
        return "removed", entry.identity.size, None
    path = _entry_path(root, entry)
    if not path.exists():
        journal[entry.path] = {"status": "absent"}
        return "absent", 0, None
    active_reason = _active_use_reason(entry)
    if active_reason:
        return "unresolved", 0, active_reason
    if not entry.identity.matches(path) or entry.identity.kind in {"symlink", "special"}:
        return "unresolved", 0, "identity changed or unsupported file type"
    if entry.identity.kind == "directory" and any(path.iterdir()):
        return "unresolved", 0, "directory is not empty after bounded children were processed"
    if path.parent.stat().st_dev != path.stat().st_dev:
        return "unresolved", 0, "candidate is not on the same filesystem as its parent"
    quarantine = quarantine or _quarantine_path(path)
    journal[entry.path] = {"status": "quarantining", "quarantine": str(quarantine)}
    checkpoint()
    if quarantine.exists() or quarantine.is_symlink():
        if not entry.identity.matches(quarantine):
            return "unresolved", 0, "quarantine identity changed"
    else:
        if not entry.identity.matches(path):
            return "unresolved", 0, "candidate changed before quarantine"
        _rename_nofollow(path, quarantine)
    if not entry.identity.matches(quarantine):
        return "unresolved", 0, "quarantine identity changed after rename"
    size = entry.identity.size
    _delete_quarantine(quarantine)
    journal[entry.path] = {"status": "done"}
    return "removed", size, None


def _entry_map(payload: dict[str, Any]) -> dict[str, ManifestEntry]:
    """Build a validated path index for the apply transaction."""
    entries = _manifest_entries(payload)
    return {entry.path: entry for entry in entries}


def _materialize_dependencies(
    root: Path,
    entries: Iterable[ManifestEntry],
    environment: Environment,
) -> set[Path]:
    """Snapshot structured Feedback attachments before their source is removed."""
    entries = tuple(entries)
    sources = {
        _absolute(root, dependency)
        for entry in entries
        for dependency in (*entry.dependencies, entry.path)
    }
    return set(materialize.materialize_attachments(root / SETUP_ROOT, sources, env=environment))


def _materialize_for_candidates(
    root: Path,
    manifest_path: Path,
    payload: dict[str, Any],
    entries: dict[str, ManifestEntry],
    candidates: Iterable[CleanupItem],
) -> list[str]:
    """Materialize Feedback evidence and isolate corrupt-log blockers."""
    dependency_entries = [entries[item.path] for item in candidates if item.path in entries]
    explicit = {
        _absolute(root, dependency)
        for entry in dependency_entries
        for dependency in entry.dependencies
    }
    selected = explicit | {
        _absolute(root, item.path) for item in candidates if item.path in entries
    }
    environment = resolve_feedback_environment(root / SETUP_ROOT)
    try:
        referenced_before = materialize.referenced_sources(root / SETUP_ROOT, selected)
        changed = _materialize_dependencies(root, dependency_entries, environment)
        candidate_paths = {
            _absolute(root, candidate.path)
            for candidate in candidates
            if candidate.path in entries
        }
        unresolved = [
            _safe_relative(root, path)
            for path in (explicit | referenced_before) & candidate_paths
            if path not in changed
        ]
    except (
        CleanupError,
        OSError,
        ValueError,
        materialize.CorruptFindingsLogError,
        materialize.MaterializationError,
    ) as exc:
        referenced = materialize.referenced_sources(
            root / SETUP_ROOT,
            selected,
        )
        unresolved = [_safe_relative(root, path) for path in referenced]
        payload["journal"]["attachments"] = {"status": "unresolved", "reason": str(exc)}
    _atomic_write(manifest_path, payload)
    return unresolved


def _apply_scratch_root(  # noqa: PLR0911 -- each recovery boundary is explicit
    root: Path,
    item: CleanupItem,
    journal: dict[str, Any],
    checkpoint: Callable[[], None],
) -> tuple[str, int, str | None]:
    """Remove a run root only when its exact contents are known and empty."""
    path = _absolute(root, item.path)
    state = journal.get(item.path, {})
    quarantine = Path(state["quarantine"]) if state.get("quarantine") else None
    if quarantine and (quarantine.exists() or quarantine.is_symlink()):
        if item.identity and item.identity.matches(quarantine):
            _delete_scratch_quarantine(quarantine)
            journal[item.path] = {"status": "done"}
            return "removed", 0, None
        return "unresolved", 0, "scratch quarantine identity changed"
    if not path.exists():
        return "absent", 0, None
    if not path.is_dir() or not item.identity or not item.identity.matches(path):
        return "unresolved", 0, "scratch root identity changed"
    contents = tuple(path.iterdir())
    if len(contents) != 1 or contents[0].name != "manifest.json":
        return "unresolved", 0, "scratch root still contains retained or unmanifested data"
    quarantine = _quarantine_path(path)
    journal[item.path] = {"status": "quarantining", "quarantine": str(quarantine)}
    checkpoint()
    _rename_nofollow(path, quarantine)
    if not item.identity.matches(quarantine):
        return "unresolved", 0, "scratch quarantine identity changed"
    _delete_scratch_quarantine(quarantine)
    journal[item.path] = {"status": "done"}
    return "removed", 0, None


def _apply_candidate(  # noqa: PLR0911 -- each disposition boundary fails closed
    root: Path,
    item: CleanupItem,
    entry: ManifestEntry | None,
    journal: dict[str, Any],
    cache_evictor: Callable[[str], bool] | None,
    checkpoint: Callable[[], None],
) -> tuple[str, int, str | None]:
    """Apply one regular, cache, or scratch-root candidate."""
    if entry is None:
        return _apply_scratch_root(root, item, journal, checkpoint)
    if item.category == "evict-cache":
        if cache_evictor is not None:
            if not cache_evictor(item.path):
                return "unresolved", 0, "Flow cache owner rejected eviction"
            return "evicted", 0, None
        from booley.flows.edam import WorkRootLeaseError, try_work_root_lease

        path = _entry_path(root, entry)
        try:
            with try_work_root_lease(path) as leased:
                if leased is None:
                    return "unresolved", 0, "Flow cache slot is actively leased"
                outcome, size, reason = _apply_entry(root, entry, journal, checkpoint)
        except WorkRootLeaseError as exc:
            return "unresolved", 0, str(exc)
        return ("evicted", size, reason) if outcome == "removed" else (outcome, size, reason)
    return _apply_entry(root, entry, journal, checkpoint)


def _apply_candidates(
    root: Path,
    manifest_path: Path,
    payload: dict[str, Any],
    entries: dict[str, ManifestEntry],
    candidates: Iterable[CleanupItem],
    blocked: set[str],
    cache_evictor: Callable[[str], bool] | None,
) -> CleanupResult:
    """Apply candidates deepest-first and persist the recovery journal."""
    journal = payload["journal"]

    def checkpoint() -> None:
        """Persist the quarantine intent before the filesystem rename."""
        _atomic_write(manifest_path, payload)

    removed: list[str] = []
    absent: list[str] = []
    evicted: list[str] = []
    unresolved: list[str] = list(blocked)
    bytes_removed = 0
    ordered = sorted(candidates, key=lambda item: item.path.count("/"), reverse=True)
    for item in ordered:
        if item.path in blocked:
            continue
        entry = entries.get(item.path)
        try:
            outcome, size, reason = _apply_candidate(
                root, item, entry, journal, cache_evictor, checkpoint
            )
        except (CleanupError, OSError) as exc:
            outcome, size, reason = "unresolved", 0, str(exc)
        payload["journal"] = journal
        if entry is not None:
            _atomic_write(manifest_path, payload)
        if outcome == "removed":
            removed.append(item.path)
            bytes_removed += size
        elif outcome == "absent":
            absent.append(item.path)
        elif outcome == "evicted":
            evicted.append(item.path)
        else:
            unresolved.append(f"{item.path}: {reason or 'unsafe'}")
    return CleanupResult(
        "",
        tuple(removed),
        tuple(absent),
        tuple(evicted),
        tuple(unresolved),
        bytes_removed,
    )


def apply_cleanup(
    plan: CleanupPlan,
    *,
    cache_evictor: Callable[[str], bool] | None = None,
) -> CleanupResult:
    """Apply one unchanged preview, or fail closed when ownership is unclear."""
    if plan.inventory_only:
        return CleanupResult(plan.digest, unresolved=tuple(item.path for item in plan.items))
    root = Path(plan.project_root)
    manifest_path, payload = _load_plan_manifest(plan)
    entries = _entry_map(payload)
    payload.setdefault("journal", {})
    candidates = [item for item in plan.items if item.category in {"remove", "evict-cache"}]
    blocked = set(_materialize_for_candidates(root, manifest_path, payload, entries, candidates))
    result = _apply_candidates(
        root,
        manifest_path,
        payload,
        entries,
        candidates,
        blocked,
        cache_evictor,
    )
    return CleanupResult(
        plan.digest,
        result.removed,
        result.already_absent,
        result.evicted,
        result.unresolved,
        result.bytes_removed,
    )


def format_summary(plan: CleanupPlan, result: CleanupResult | None = None) -> str:
    """Render the concise closeout shared by the CLI and Step 7."""
    groups = plan.grouped()
    lines = [
        f"cleanup preview {plan.digest}",
        f"inventory-only: {'yes' if plan.inventory_only else 'no'}",
    ]
    for category in ("preserve", "remove", "evict-cache", "unresolved"):
        group = groups[category]
        lines.append(f"{category}: {group['count']} path(s), {group['bytes']} byte(s)")
    if result is not None:
        lines.append(
            f"removed: {result.bytes_removed} byte(s) across {len(result.removed)} path(s)"
        )
        lines.append(f"unresolved after apply: {len(result.unresolved)}")
    return "\n".join(lines)
