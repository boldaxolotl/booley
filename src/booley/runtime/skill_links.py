"""Ownership-aware reconciliation of agent skill links.

The public interface hides source discovery, precedence, ownership metadata,
POSIX symlinks, and Windows junctions. Host setup and Session Runtime startup
only choose roots and render the returned report.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from uuid import uuid4

from booley.core.boundary import BoundaryError, require_dict, require_int, require_str
from booley.runtime.platform_paths import IS_WINDOWS

MANIFEST_FILENAME = ".booley-skill-links.json"
_MANIFEST_VERSION = 1
_TEMP_PREFIX = ".booley-skill-link-tmp-"
_BACKUP_PREFIX = ".booley-skill-link-backup-"

SourceKind = Literal["packaged"]
Outcome = Literal[
    "created",
    "adopted",
    "equivalent",
    "retargeted",
    "removed",
    "unchanged",
    "conflict",
    "error",
]


@dataclass(frozen=True)
class SkillLinkEvent:
    """One per-name reconciliation outcome."""

    name: str
    outcome: Outcome
    source_kind: SourceKind | None
    entry_path: Path
    previous_target: str | None = None
    desired_target: str | None = None
    detail: str = ""

    @property
    def failed(self) -> bool:
        """Whether this event needs user attention."""
        return self.outcome in {"conflict", "error"}

    @property
    def changed(self) -> bool:
        """Whether this event changed a link or its ownership record."""
        return self.outcome in {"created", "adopted", "retargeted", "removed"}

    @property
    def always_visible(self) -> bool:
        """Whether adapters should render this event outside verbose mode."""
        return self.failed or self.outcome not in {"created", "unchanged"}

    @property
    def replaces_target(self) -> bool:
        """Whether applying this event replaces one link target with another."""
        return self.outcome == "retargeted"

    @property
    def preview_label(self) -> str:
        """Human-readable action label for a dry-run event."""
        verbs = {
            "created": "create",
            "adopted": "adopt",
            "equivalent": "accept as equivalent",
            "retargeted": "retarget",
            "removed": "remove",
            "unchanged": "leave unchanged",
            "conflict": "report conflict for",
            "error": "report error for",
        }
        return f"would {verbs[self.outcome]}"


@dataclass(frozen=True)
class SkillLinkReport:
    """Complete reconciliation result; a fatal report made no changes."""

    events: tuple[SkillLinkEvent, ...] = ()
    diagnostics: tuple[str, ...] = ()
    fatal: str | None = None

    def count(self, outcome: Outcome) -> int:
        """Return the number of events with *outcome*."""
        return sum(event.outcome == outcome for event in self.events)

    @property
    def failed(self) -> bool:
        """Whether reconciliation needs user or operator attention."""
        return bool(self.fatal or self.diagnostics or any(event.failed for event in self.events))


@dataclass(frozen=True)
class _Desired:
    name: str
    source_kind: SourceKind
    path: Path
    target: str


@dataclass(frozen=True)
class _Record:
    name: str
    source_kind: SourceKind
    target: str


@dataclass(frozen=True)
class _Entry:
    kind: Literal["missing", "link", "foreign"]
    target: str | None = None


@dataclass(frozen=True)
class _Action:
    event: SkillLinkEvent
    operation: Literal["none", "create", "replace", "remove"]
    expected: _Entry
    record_after: _Record | None
    record_on_failure: _Record | None


def _name_key(name: str) -> str:
    return os.path.normcase(name)


def _strip_windows_namespace(value: str) -> str:
    if value.startswith("\\\\?\\") or value.startswith("\\??\\"):
        return value[4:]
    return value


def _normalize_target(value: str | Path, *, parent: Path | None = None) -> str:
    raw = _strip_windows_namespace(os.fspath(value))
    if parent is not None and not Path(raw).is_absolute():
        raw = os.path.join(str(parent), raw)  # noqa: PTH118 - lexical path handling
    return os.path.normcase(os.path.abspath(os.path.normpath(raw)))  # noqa: PTH100


def _skill_tree_identity(root: Path) -> bytes | None:
    """Return a content identity for a regular, self-contained skill tree."""
    try:
        if not _regular_directory(root):
            return None
        identity = hashlib.sha256()
        children = sorted(root.rglob("*"), key=lambda path: path.relative_to(root).as_posix())
        for child in children:
            relative = child.relative_to(root).as_posix().encode()
            metadata = child.lstat()
            if stat.S_ISDIR(metadata.st_mode):
                identity.update(b"D\0" + relative + b"\0")
                continue
            if not stat.S_ISREG(metadata.st_mode):
                return None
            content = hashlib.sha256()
            with child.open("rb") as source:
                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                    content.update(chunk)
            identity.update(b"F\0" + relative + b"\0" + content.digest())
        return identity.digest()
    except (OSError, ValueError):
        return None


def _equivalent_skill(existing_target: str, desired: Path) -> bool:
    existing_identity = _skill_tree_identity(Path(existing_target))
    return existing_identity is not None and existing_identity == _skill_tree_identity(desired)


def _is_junction(path: Path) -> bool:
    if not IS_WINDOWS or path.is_symlink():
        return False
    try:
        metadata = path.lstat()
    except OSError:
        return False
    mount_point = getattr(stat, "IO_REPARSE_TAG_MOUNT_POINT", 0xA0000003)
    return getattr(metadata, "st_reparse_tag", None) == mount_point


def _occupied(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    return True


def _read_entry(path: Path) -> _Entry:
    try:
        path.lstat()
    except FileNotFoundError:
        return _Entry("missing")
    if not (path.is_symlink() or _is_junction(path)):
        return _Entry("foreign")
    raw = os.readlink(path)  # noqa: PTH115 - target may be dangling
    return _Entry("link", _normalize_target(raw, parent=path.parent))


def _regular_directory(path: Path) -> bool:
    try:
        return stat.S_ISDIR(path.lstat().st_mode)
    except FileNotFoundError:
        return False


def _regular_file(path: Path) -> bool:
    try:
        return stat.S_ISREG(path.lstat().st_mode)
    except FileNotFoundError:
        return False


def _discover_source(root: Path) -> tuple[dict[str, _Desired], str | None]:
    try:
        metadata = root.lstat()
        if not stat.S_ISDIR(metadata.st_mode):
            return {}, f"packaged skills path is not a directory: {root}"
        children = sorted(root.iterdir(), key=lambda path: _name_key(path.name))
        return _discover_children(children)
    except FileNotFoundError:
        return {}, f"packaged skills directory is missing: {root}"
    except OSError as exc:
        return {}, f"cannot inspect packaged skills directory {root}: {exc}"


def _discover_children(children: list[Path]) -> tuple[dict[str, _Desired], str | None]:
    desired: dict[str, _Desired] = {}
    reserved = _name_key(MANIFEST_FILENAME)
    for child in children:
        if not _regular_directory(child) or not _regular_file(child / "SKILL.md"):
            continue
        if _name_key(child.name) == reserved:
            return {}, f"reserved skill name {child.name!r} in packaged source"
        key = _name_key(child.name)
        if key in desired:
            return {}, f"duplicate skill name {child.name!r} in packaged source"
        desired[key] = _Desired(
            child.name,
            "packaged",
            child,
            _normalize_target(child),
        )
    return desired, None


def _load_manifest(target_dir: Path) -> tuple[dict[str, _Record], str | None]:
    manifest = target_dir / MANIFEST_FILENAME
    if not _occupied(manifest):
        return {}, None
    if not _regular_file(manifest):
        return {}, f"skill-link manifest is not a regular file: {manifest}"
    try:
        raw = json.loads(manifest.read_text(encoding="utf-8"))
        return _parse_manifest(raw), None
    except (BoundaryError, json.JSONDecodeError, OSError, UnicodeError) as exc:
        return {}, f"cannot read skill-link manifest {manifest}: {exc}"


def _parse_manifest(raw: object) -> dict[str, _Record]:
    document = require_dict(raw, field="skill-link manifest")
    version = require_int(document.get("version"), field="skill-link manifest version")
    if version != _MANIFEST_VERSION:
        raise BoundaryError(f"unsupported skill-link manifest version {version}")
    links = require_dict(document.get("links"), field="skill-link manifest links")
    records: dict[str, _Record] = {}
    for name, value in links.items():
        _validate_manifest_name(name)
        record = require_dict(value, field=f"skill-link manifest entry {name!r}")
        source_kind = require_str(record, "source_kind")
        if source_kind != "packaged":
            raise BoundaryError(f"invalid source_kind for skill-link manifest entry {name!r}")
        target = require_str(record, "target")
        _validate_manifest_target(name, target)
        key = _name_key(name)
        if key in records:
            raise BoundaryError(f"duplicate skill-link manifest name {name!r}")
        records[key] = _Record(name, source_kind, _normalize_target(target))
    return records


def _validate_manifest_name(name: object) -> None:
    if not isinstance(name, str) or not name:
        raise BoundaryError("skill-link manifest names must be non-empty strings")
    if name in {".", ".."} or "/" in name or "\\" in name:
        raise BoundaryError(f"invalid skill-link manifest name {name!r}")
    if _name_key(name) == _name_key(MANIFEST_FILENAME):
        raise BoundaryError(f"reserved skill-link manifest name {name!r}")


def _validate_manifest_target(name: str, target: str) -> None:
    if not target or not Path(target).is_absolute():
        raise BoundaryError(f"target for skill-link manifest entry {name!r} must be absolute")
    if target != _normalize_target(target):
        raise BoundaryError(f"target for skill-link manifest entry {name!r} must be normalized")


def _target_entries(target_dir: Path) -> tuple[dict[str, tuple[str, _Entry]], str | None]:
    if not _occupied(target_dir):
        return {}, None
    if not _regular_directory(target_dir):
        return {}, f"skills target is not a directory: {target_dir}"
    try:
        children = list(target_dir.iterdir())
        entries = {
            _name_key(child.name): (child.name, _read_entry(child))
            for child in children
            if child.name != MANIFEST_FILENAME
        }
    except OSError as exc:
        return {}, f"cannot inspect skills target {target_dir}: {exc}"
    return entries, None


def _event(
    name: str,
    outcome: Outcome,
    target_dir: Path,
    desired: _Desired | None,
    entry: _Entry,
    detail: str = "",
) -> SkillLinkEvent:
    return SkillLinkEvent(
        name=name,
        outcome=outcome,
        source_kind=None if desired is None else desired.source_kind,
        entry_path=target_dir / name,
        previous_target=entry.target,
        desired_target=None if desired is None else desired.target,
        detail=detail,
    )


def _record_for(desired: _Desired | None) -> _Record | None:
    if desired is None:
        return None
    return _Record(desired.name, desired.source_kind, desired.target)


def _plan_recorded(
    target_dir: Path,
    record: _Record,
    desired: _Desired | None,
    entry: _Entry,
    *,
    allow_retarget: bool,
) -> _Action | None:
    if entry.kind == "missing":
        return _plan_missing_recorded(target_dir, record, desired, entry)
    if entry.kind != "link" or entry.target != record.target:
        event = _event(
            record.name, "conflict", target_dir, desired, entry, "recorded link was replaced"
        )
        return _Action(event, "none", entry, None, None)
    if desired is None:
        event = _event(record.name, "removed", target_dir, None, entry)
        return _Action(event, "remove", entry, None, record)
    if entry.target == desired.target:
        event = _event(desired.name, "unchanged", target_dir, desired, entry)
        return _Action(event, "none", entry, _record_for(desired), record)
    if not allow_retarget:
        event = _event(
            desired.name,
            "conflict",
            target_dir,
            desired,
            entry,
            "managed link retarget requires explicit authority; rerun booley init --force",
        )
        return _Action(event, "none", entry, record, record)
    event = _event(desired.name, "retargeted", target_dir, desired, entry)
    return _Action(event, "replace", entry, _record_for(desired), record)


def _plan_missing_recorded(
    target_dir: Path,
    record: _Record,
    desired: _Desired | None,
    entry: _Entry,
) -> _Action:
    if desired is None:
        event = _event(
            record.name,
            "removed",
            target_dir,
            None,
            entry,
            "recorded link was already absent",
        )
        return _Action(event, "none", entry, None, None)
    event = _event(desired.name, "created", target_dir, desired, entry)
    return _Action(event, "create", entry, _record_for(desired), None)


def _plan_unrecorded(
    target_dir: Path,
    desired: _Desired | None,
    entry_name: str,
    entry: _Entry,
    *,
    allow_retarget: bool,
) -> _Action | None:
    if desired is None:
        return None
    if entry.kind == "missing":
        event = _event(desired.name, "created", target_dir, desired, entry)
        return _Action(event, "create", entry, _record_for(desired), None)
    if entry.kind == "link" and entry.target == desired.target:
        event = _event(desired.name, "adopted", target_dir, desired, entry)
        return _Action(event, "none", entry, _record_for(desired), None)
    if entry.kind == "link" and entry.target and _equivalent_skill(entry.target, desired.path):
        if allow_retarget:
            event = _event(desired.name, "retargeted", target_dir, desired, entry)
            return _Action(event, "replace", entry, _record_for(desired), None)
        event = _event(
            desired.name,
            "equivalent",
            target_dir,
            desired,
            entry,
            "equivalent packaged skill accepted without mutation; use --force to relink",
        )
        return _Action(event, "none", entry, None, None)
    event = _event(
        entry_name, "conflict", target_dir, desired, entry, "desired skill name is occupied"
    )
    return _Action(event, "none", entry, None, None)


def _plan_actions(
    target_dir: Path,
    desired: dict[str, _Desired],
    records: dict[str, _Record],
    entries: dict[str, tuple[str, _Entry]],
    *,
    allow_retarget: bool,
) -> list[_Action]:
    actions: list[_Action] = []
    for key in sorted(desired.keys() | records.keys() | entries.keys()):
        wanted = desired.get(key)
        record = records.get(key)
        entry_name, entry = entries.get(
            key,
            ((record or wanted).name if record or wanted else key, _Entry("missing")),
        )
        action = (
            _plan_recorded(
                target_dir,
                record,
                wanted,
                entry,
                allow_retarget=allow_retarget,
            )
            if record
            else _plan_unrecorded(
                target_dir,
                wanted,
                entry_name,
                entry,
                allow_retarget=allow_retarget,
            )
        )
        if action is not None:
            actions.append(action)
    return actions


def _make_link(link: Path, target: Path) -> None:
    if IS_WINDOWS:
        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target.absolute())],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode:
            raise OSError(result.stderr.strip() or result.stdout.strip() or "mklink failed")
        return
    link.symlink_to(os.path.relpath(target, link.parent))


def _remove_link(link: Path) -> None:
    if _is_junction(link):
        link.rmdir()
    else:
        link.unlink()


def _unique_path(parent: Path, prefix: str) -> Path:
    for _ in range(16):
        candidate = parent / f"{prefix}{uuid4().hex}"
        if not _occupied(candidate):
            return candidate
    raise OSError(f"could not allocate temporary skill-link path under {parent}")


def _replace_link_windows(
    link: Path,
    desired: Path,
    diagnostics: list[str],
) -> None:
    temporary = _unique_path(link.parent, _TEMP_PREFIX)
    backup = _unique_path(link.parent, _BACKUP_PREFIX)
    _make_link(temporary, desired)
    try:
        link.rename(backup)
        temporary.rename(link)
    except OSError as exc:
        _restore_windows_link(link, temporary, backup, diagnostics)
        raise OSError(f"could not install replacement junction {link}: {exc}") from exc
    try:
        _remove_link(backup)
    except OSError as exc:
        diagnostics.append(f"replacement succeeded but backup remains at {backup}: {exc}")


def _restore_windows_link(
    link: Path,
    temporary: Path,
    backup: Path,
    diagnostics: list[str],
) -> None:
    try:
        if _occupied(link):
            _remove_link(link)
        if _occupied(backup):
            backup.rename(link)
        if _occupied(temporary):
            _remove_link(temporary)
    except OSError as exc:
        diagnostics.append(
            f"junction rollback needs manual recovery: link={link}, temporary={temporary}, "
            f"backup={backup}: {exc}"
        )


def _replace_link(link: Path, desired: Path, diagnostics: list[str]) -> None:
    if IS_WINDOWS:
        _replace_link_windows(link, desired, diagnostics)
        return
    temporary = _unique_path(link.parent, _TEMP_PREFIX)
    try:
        _make_link(temporary, desired)
        temporary.replace(link)
    finally:
        if _occupied(temporary):
            try:
                _remove_link(temporary)
            except OSError as exc:
                diagnostics.append(f"temporary skill link remains at {temporary}: {exc}")


def _apply_action(
    action: _Action, diagnostics: list[str]
) -> tuple[SkillLinkEvent, _Record | None]:
    if action.operation == "none":
        return action.event, action.record_after
    try:
        current = _read_entry(action.event.entry_path)
    except OSError as exc:
        return _error_event(action, f"cannot recheck entry: {exc}"), action.record_on_failure
    if current != action.expected:
        detail = "entry changed during reconciliation"
        event = _event(
            action.event.name,
            "conflict",
            action.event.entry_path.parent,
            None,
            current,
            detail,
        )
        return event, None
    try:
        _perform_action(action, diagnostics)
    except OSError as exc:
        return _error_event(action, str(exc)), action.record_on_failure
    return action.event, action.record_after


def _perform_action(action: _Action, diagnostics: list[str]) -> None:
    link = action.event.entry_path
    desired = action.event.desired_target
    if action.operation == "create":
        assert desired is not None
        _make_link(link, Path(desired))
    elif action.operation == "replace":
        assert desired is not None
        _replace_link(link, Path(desired), diagnostics)
    else:
        _remove_link(link)


def _error_event(action: _Action, detail: str) -> SkillLinkEvent:
    event = action.event
    return SkillLinkEvent(
        event.name,
        "error",
        event.source_kind,
        event.entry_path,
        event.previous_target,
        event.desired_target,
        detail,
    )


def _manifest_payload(records: dict[str, _Record]) -> str:
    links = {
        record.name: {"source_kind": record.source_kind, "target": record.target}
        for _, record in sorted(records.items())
    }
    return json.dumps({"version": _MANIFEST_VERSION, "links": links}, indent=2) + "\n"


def _write_manifest(target_dir: Path, records: dict[str, _Record]) -> tuple[str, ...]:
    manifest = target_dir / MANIFEST_FILENAME
    temporary = _unique_path(target_dir, f".{MANIFEST_FILENAME}.tmp-")
    diagnostics: list[str] = []
    try:
        temporary.write_text(_manifest_payload(records), encoding="utf-8")
        temporary.replace(manifest)
    except OSError as exc:
        diagnostics.append(f"could not write skill-link manifest {manifest}: {exc}")
    finally:
        if _occupied(temporary):
            try:
                temporary.unlink()
            except OSError as exc:
                diagnostics.append(f"temporary manifest remains at {temporary}: {exc}")
    return tuple(diagnostics)


def _preflight(
    target_dir: Path,
    packaged_dir: Path,
) -> tuple[
    dict[str, _Desired],
    dict[str, _Record],
    dict[str, tuple[str, _Entry]],
    str | None,
]:
    packaged, error = _discover_source(packaged_dir)
    if error:
        return {}, {}, {}, error
    records, error = _load_manifest(target_dir)
    if error:
        return {}, {}, {}, error
    entries, error = _target_entries(target_dir)
    return packaged, records, entries, error


def reconcile_skill_links(
    target_dir: Path,
    packaged_dir: Path,
    *,
    dry_run: bool = False,
    allow_retarget: bool = False,
) -> SkillLinkReport:
    """Converge one agent skills directory while preserving foreign entries.

    ``allow_retarget`` is explicit authority to replace a manifest-owned link or
    a content-equivalent packaged skill link. A fatal report guarantees that no
    filesystem mutation occurred.
    """
    desired, records, entries, fatal = _preflight(target_dir, packaged_dir)
    if fatal:
        return SkillLinkReport(fatal=fatal)
    actions = _plan_actions(
        target_dir,
        desired,
        records,
        entries,
        allow_retarget=allow_retarget,
    )
    if dry_run:
        return SkillLinkReport(events=tuple(action.event for action in actions))
    return _apply_actions(target_dir, records, actions)


def _apply_actions(
    target_dir: Path,
    records: dict[str, _Record],
    actions: list[_Action],
) -> SkillLinkReport:
    if not actions:
        return SkillLinkReport()
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return SkillLinkReport(fatal=f"cannot create skills target {target_dir}: {exc}")
    updated = dict(records)
    diagnostics: list[str] = []
    events: list[SkillLinkEvent] = []
    for action in actions:
        event, record_after = _apply_action(action, diagnostics)
        events.append(event)
        key = _name_key(event.name)
        if record_after is None:
            updated.pop(key, None)
        else:
            updated[key] = record_after
    if updated != records:
        diagnostics.extend(_write_manifest(target_dir, updated))
    return SkillLinkReport(tuple(events), tuple(diagnostics))
