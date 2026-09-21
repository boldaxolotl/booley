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
import shutil
import stat
import uuid
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal

from booley.feedback import materialize

MANIFEST_VERSION = 1
SETUP_ROOT = Path(".booley_project")
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
        if current.kind != self.kind or current.device != self.device or current.inode != self.inode:
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

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> ManifestEntry:
        identity = FileIdentity(**raw["identity"])
        dependencies = tuple(str(item) for item in raw.get("dependencies", ()))
        return cls(
            path=str(raw["path"]),
            producer=str(raw.get("producer", "unknown")),
            artifact_class=str(raw.get("artifact_class", "unknown")),
            disposition=str(raw.get("disposition", "remove")),
            identity=identity,
            dependencies=dependencies,
            active=bool(raw.get("active", False)),
        )

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


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
    project_dir = resolved / SETUP_ROOT
    if not project_dir.is_dir():
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
    project_dir = (root / SETUP_ROOT).resolve()
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
    if not run_id or Path(run_id).name != run_id or not run_id.isascii():
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
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
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
    entries = []
    for raw in payload.get("entries", []):
        entry = ManifestEntry.from_dict(raw)
        relative = _safe_relative(root, entry.path)
        _reject_shared_boundary(root, _absolute(root, relative), path)
        entries.append(entry)
    payload["entries"] = [entry.as_dict() for entry in entries]
    payload.setdefault("journal", {})
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


def _descendants(root: Path, path: Path) -> Iterable[Path]:
    """Yield a bounded tree without following symlinks."""
    if not path.is_dir():
        return ()
    found: list[Path] = []
    for child in path.rglob("*"):
        if child.is_symlink() or child.is_file() or child.is_dir():
            found.append(child)
    return tuple(found)


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
    identity = FileIdentity.from_path(absolute)
    if identity.kind == "symlink":
        raise CleanupError(f"symlinks cannot be setup-owned artifacts: {relative}")
    dep_paths = tuple(_safe_relative(root, item) for item in dependencies)
    entry = ManifestEntry(
        relative,
        producer,
        artifact_class,
        disposition,
        identity,
        dep_paths,
        active,
    )
    entries = [ManifestEntry.from_dict(raw) for raw in payload["entries"]]
    entries = [old for old in entries if old.path != relative]
    entries.append(entry)
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
            )
        )
    unique = {item.path: item for item in entries}
    payload["entries"] = [item.as_dict() for item in sorted(unique.values(), key=lambda item: item.path)]
    _atomic_write(manifest_path, payload)
    return entry


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
    if entry.active:
        return CleanupItem(entry.path, "unresolved", recorded_bytes, "active-use claim", entry.identity)
    if entry.disposition == "preserve" or (
        retention_mode == "diagnostic" and entry.artifact_class == "raw-evidence"
    ):
        category: Category = "preserve"
        reason = "manifest retention decision"
    elif entry.artifact_class == "flow-cache":
        category = "evict-cache" if cache_disposition == "evict-setup-touched" else "preserve"
        reason = "explicit Flow-cache eviction" if category == "evict-cache" else "reusable Flow cache"
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
        digest = hashlib.sha256(json.dumps([item.as_dict() for item in items], sort_keys=True).encode()).hexdigest()
        return CleanupPlan(str(root), None, None, True, retention_mode, cache_disposition, items, digest)
    payload = _load_manifest(manifest_path, root)
    entries = [ManifestEntry.from_dict(raw) for raw in payload["entries"]]
    items = [
        _item_for_entry(root, entry, retention_mode, cache_disposition) for entry in entries
    ]
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
        raise CleanupBlockedError("legacy inventory has no ownership evidence; nothing may be deleted")
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


def _delete_quarantine(path: Path) -> None:
    """Delete only an already-quarantined, identity-checked path."""
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    elif path.exists() or path.is_symlink():
        path.unlink()


def _apply_entry(  # noqa: PLR0911 -- each fail-closed boundary has a distinct outcome
    root: Path,
    entry: ManifestEntry,
    journal: dict[str, Any],
    checkpoint: Callable[[], None],
) -> tuple[str, int, str | None]:
    """Move one candidate through sibling quarantine with identity checks."""
    state = journal.get(entry.path, {})
    if state.get("status") == "done":
        return "removed", 0, None
    path = _entry_path(root, entry)
    if not path.exists():
        journal[entry.path] = {"status": "absent"}
        return "absent", 0, None
    if not entry.identity.matches(path) or entry.identity.kind in {"symlink", "special"}:
        return "unresolved", 0, "identity changed or unsupported file type"
    if entry.identity.kind == "directory" and any(path.iterdir()):
        return "unresolved", 0, "directory is not empty after bounded children were processed"
    if path.parent.stat().st_dev != path.stat().st_dev:
        return "unresolved", 0, "candidate is not on the same filesystem as its parent"
    quarantine = Path(state["quarantine"]) if state.get("quarantine") else _quarantine_path(path)
    journal[entry.path] = {"status": "quarantining", "quarantine": str(quarantine)}
    checkpoint()
    if quarantine.exists() or quarantine.is_symlink():
        if not entry.identity.matches(quarantine):
            return "unresolved", 0, "quarantine identity changed"
    else:
        if not entry.identity.matches(path):
            return "unresolved", 0, "candidate changed before quarantine"
        path.rename(quarantine)
    if not entry.identity.matches(quarantine):
        return "unresolved", 0, "quarantine identity changed after rename"
    size = entry.identity.size
    _delete_quarantine(quarantine)
    journal[entry.path] = {"status": "done"}
    return "removed", size, None


def _entry_map(payload: dict[str, Any]) -> dict[str, ManifestEntry]:
    """Build a validated path index for the apply transaction."""
    entries = [ManifestEntry.from_dict(raw) for raw in payload["entries"]]
    return {entry.path: entry for entry in entries}


def _materialize_dependencies(root: Path, entries: Iterable[ManifestEntry]) -> set[Path]:
    """Snapshot structured Feedback attachments before their source is removed."""
    sources = {_absolute(root, entry.path) for entry in entries}
    return set(materialize.materialize_attachments(root / SETUP_ROOT, sources))


def _materialize_for_candidates(
    root: Path,
    manifest_path: Path,
    payload: dict[str, Any],
    entries: dict[str, ManifestEntry],
    candidates: Iterable[CleanupItem],
) -> list[str]:
    """Materialize Feedback evidence and isolate corrupt-log blockers."""
    dependency_entries = [entries[item.path] for item in candidates if item.path in entries]
    try:
        _materialize_dependencies(root, dependency_entries)
        unresolved: list[str] = []
    except (
        CleanupError,
        OSError,
        ValueError,
        materialize.CorruptFindingsLogError,
        materialize.MaterializationError,
    ) as exc:
        referenced = materialize.referenced_sources(
            root / SETUP_ROOT,
            {_absolute(root, entry.path) for entry in dependency_entries},
        )
        unresolved = [_safe_relative(root, path) for path in referenced]
        payload["journal"]["attachments"] = {"status": "unresolved", "reason": str(exc)}
    _atomic_write(manifest_path, payload)
    return unresolved


def _apply_scratch_root(
    root: Path,
    item: CleanupItem,
    journal: dict[str, Any],
    checkpoint: Callable[[], None],
) -> tuple[str, int, str | None]:
    """Remove a run root only when its exact contents are known and empty."""
    path = _absolute(root, item.path)
    state = journal.get(item.path, {})
    if not path.exists():
        quarantine = Path(state["quarantine"]) if state.get("quarantine") else None
        if quarantine and quarantine.exists() and item.identity and item.identity.matches(quarantine):
            _delete_quarantine(quarantine)
            journal[item.path] = {"status": "done"}
            return "removed", 0, None
        return "absent", 0, None
    if not path.is_dir() or not item.identity or not item.identity.matches(path):
        return "unresolved", 0, "scratch root identity changed"
    contents = tuple(path.iterdir())
    if len(contents) != 1 or contents[0].name != "manifest.json":
        return "unresolved", 0, "scratch root still contains retained or unmanifested data"
    quarantine = _quarantine_path(path)
    journal[item.path] = {"status": "quarantining", "quarantine": str(quarantine)}
    checkpoint()
    path.rename(quarantine)
    if not item.identity.matches(quarantine):
        return "unresolved", 0, "scratch quarantine identity changed"
    _delete_quarantine(quarantine)
    journal[item.path] = {"status": "done"}
    return "removed", 0, None


def _apply_candidate(
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
        if cache_evictor is None or not cache_evictor(item.path):
            return "unresolved", 0, "Flow cache owner is unavailable"
        return "evicted", 0, None
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
    lines = [f"cleanup preview {plan.digest}", f"inventory-only: {'yes' if plan.inventory_only else 'no'}"]
    for category in ("preserve", "remove", "evict-cache", "unresolved"):
        group = groups[category]
        lines.append(f"{category}: {group['count']} path(s), {group['bytes']} byte(s)")
    if result is not None:
        lines.append(f"removed: {result.bytes_removed} byte(s) across {len(result.removed)} path(s)")
        lines.append(f"unresolved after apply: {len(result.unresolved)}")
    return "\n".join(lines)
