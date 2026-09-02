"""Recoverable Session Image and Runtime refresh orchestration."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
from dataclasses import asdict, dataclass, fields, replace
from enum import StrEnum
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

from booley.core.boundary import (
    BoundaryError,
    as_str,
    require_bool,
    require_dict,
    require_int,
    require_opt_str,
    require_str,
)
from booley.eda.provisioning import runtime_spec
from booley.harness import devcontainer as dc
from booley.harness import session_runtime as sr
from booley.harness.image_lifecycle import LifecycleResult
from booley.harness.init_cmd import (
    SessionSpecSnapshot,
    capture_session_spec,
    inspect_refreshable_session_image,
    refresh_session_image,
    reissue_session_spec,
    restore_session_spec,
)
from booley.harness.lifecycle_lock import host_lifecycle_lock
from booley.runtime.auth_token import config_dir
from booley.runtime.private_store import PrivateStore

_JOURNAL_VERSION = 1
_JOURNAL_DIR = Path("eda") / "session-refresh"
_MAX_SNAPSHOT_BYTES = 1024 * 1024


class _RefreshPhase(StrEnum):
    PREPARED = "prepared"
    PARKED = "parked"
    IMAGE_SELECTED = "image_selected"
    ISSUED = "issued"
    VERIFIED = "verified"


class _RecoveryDirection(StrEnum):
    RESTORE_ELIGIBLE = "restore_eligible"
    COMMITTED_FORWARD = "committed_forward"


class RecoveryOutcome(StrEnum):
    """Observable result of interrupted Session-refresh recovery."""

    NONE = "none"
    RESTORED = "restored"
    RESUMED = "resumed"


@dataclass(frozen=True, slots=True)
class RecoveryResult:
    """One Project's durable Session-refresh recovery result."""

    project_root: Path
    outcome: RecoveryOutcome


@dataclass(frozen=True, slots=True)
class _RefreshJournal:
    project_root: Path
    transaction_id: str
    phase: _RefreshPhase
    direction: _RecoveryDirection
    snapshot: SessionSpecSnapshot
    prior_issuance: runtime_spec.Issuance
    prior_runtime: sr.ParkedSession | None
    target_image_id: str | None
    target_payload_fingerprint: str | None
    replacement_issuance: runtime_spec.Issuance | None


def _journal_store() -> PrivateStore:
    root = config_dir() / _JOURNAL_DIR
    return PrivateStore(root, config_dir(), "Session refresh journal", sr.SessionError)


def _journal_name(project_root: Path) -> str:
    identity = hashlib.sha256(str(project_root).encode()).hexdigest()
    return f"{identity}.json"


def _require_mapping(raw: object, label: str) -> dict[str, Any]:
    try:
        values = require_dict(raw, field=f"Session refresh journal {label}")
    except BoundaryError as exc:
        raise sr.SessionError(str(exc)) from exc
    return cast(dict[str, Any], values)


def _require_exact_fields(values: dict[str, Any], expected: frozenset[str], label: str) -> None:
    if set(values) != expected:
        raise sr.SessionError(f"Session refresh journal {label} has unexpected or missing fields")


def _decode_snapshot_content(values: dict[str, Any], name: str, present_name: str) -> bytes | None:
    try:
        present = require_bool(values, present_name)
    except BoundaryError as exc:
        raise sr.SessionError(f"Session refresh journal snapshot is invalid: {exc}") from exc
    encoded = as_str(values.get(name))
    if encoded is None:
        raise sr.SessionError("Session refresh journal snapshot content must be a string")
    try:
        decoded = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise sr.SessionError("Session refresh journal snapshot encoding is invalid") from exc
    if len(decoded) > _MAX_SNAPSHOT_BYTES:
        raise sr.SessionError("Session refresh journal snapshot is too large")
    return decoded if present else None


def _decode_snapshot_mode(values: dict[str, Any], name: str) -> int:
    try:
        mode = require_int(values.get(name), field=f"snapshot {name}")
    except BoundaryError as exc:
        raise sr.SessionError(f"Session refresh journal snapshot is invalid: {exc}") from exc
    if not 0 <= mode <= 0o777:
        raise sr.SessionError(f"Session refresh journal snapshot {name} is outside mode bits")
    return mode


def _decode_snapshot(raw: object, project_root: Path) -> SessionSpecSnapshot:
    values = _require_mapping(raw, "snapshot")
    expected = frozenset(
        {
            "spec_present",
            "spec_content",
            "spec_mode",
            "stamp_present",
            "stamp_content",
            "stamp_mode",
            "image_id",
        }
    )
    _require_exact_fields(values, expected, "snapshot")
    try:
        image_id = require_opt_str(values, "image_id", field="snapshot image_id")
    except BoundaryError as exc:
        raise sr.SessionError(f"Session refresh journal snapshot is invalid: {exc}") from exc
    return SessionSpecSnapshot(
        dc.devcontainer_path(project_root),
        _decode_snapshot_content(values, "spec_content", "spec_present"),
        _decode_snapshot_mode(values, "spec_mode"),
        runtime_spec.stamp_path(project_root),
        _decode_snapshot_content(values, "stamp_content", "stamp_present"),
        _decode_snapshot_mode(values, "stamp_mode"),
        image_id,
    )


def _decode_issuance(raw: object, label: str) -> runtime_spec.Issuance:
    try:
        return runtime_spec.issuance_from_document(raw)
    except runtime_spec.RuntimeSpecError as exc:
        raise sr.SessionError(f"Session refresh journal {label} is invalid: {exc}") from exc


def _decode_runtime(raw: object, project_root: Path) -> sr.ParkedSession | None:
    if raw is None:
        return None
    values = _require_mapping(raw, "prior runtime")
    expected = frozenset(field.name for field in fields(sr.ParkedSession))
    _require_exact_fields(values, expected, "prior runtime")
    try:
        parked = sr.ParkedSession(
            name=require_str(values, "name"),
            backup=require_str(values, "backup"),
            was_running=require_bool(values, "was_running"),
            project_id=require_str(values, "project_id"),
            reconnect_egress=require_bool(values, "reconnect_egress"),
            container_id=require_str(values, "container_id"),
            image_id=require_str(values, "image_id"),
            egress_network_id=require_opt_str(values, "egress_network_id"),
        )
    except BoundaryError as exc:
        raise sr.SessionError(f"Session refresh journal prior runtime is invalid: {exc}") from exc
    expected_name = sr.session_container_name(project_root)
    if parked.name != expected_name or parked.backup != f"{expected_name}-pre-refresh":
        raise sr.SessionError("Session refresh journal contains invalid Session Runtime names")
    if parked.reconnect_egress and parked.egress_network_id is None:
        raise sr.SessionError(
            "Session refresh journal prior runtime egress identity is incomplete"
        )
    return parked


def _decode_replay_metadata(
    values: dict[str, Any], expected_root: Path
) -> tuple[str, _RefreshPhase, _RecoveryDirection, str | None, str | None]:
    try:
        version = require_int(values.get("version"), field="journal version")
        project_root = require_str(values, "project_root")
        transaction_id = require_str(values, "transaction_id")
        phase = _RefreshPhase(require_str(values, "phase"))
        direction = _RecoveryDirection(require_str(values, "direction"))
        target = require_opt_str(values, "target_image_id")
        payload = require_opt_str(values, "target_payload_fingerprint")
    except (BoundaryError, ValueError) as exc:
        raise sr.SessionError(
            f"Session refresh journal replay metadata is invalid: {exc}"
        ) from exc
    if version != _JOURNAL_VERSION or project_root != str(expected_root):
        raise sr.SessionError("Session refresh journal identity or version is invalid")
    return transaction_id, phase, direction, target, payload


def _validate_journal_identities(journal: _RefreshJournal) -> None:
    if journal.prior_issuance.project_root != str(journal.project_root):
        raise sr.SessionError("Session refresh journal prior issuance belongs to another Project")
    if journal.snapshot.image_id != journal.prior_issuance.image_id:
        raise sr.SessionError("Session refresh journal predecessor identities disagree")
    if journal.prior_runtime is not None and (
        journal.prior_runtime.image_id != journal.prior_issuance.image_id
    ):
        raise sr.SessionError("Session refresh journal predecessor image identities disagree")
    replacement = journal.replacement_issuance
    if replacement is not None and (
        replacement.project_root != str(journal.project_root)
        or replacement.image_id != journal.target_image_id
    ):
        raise sr.SessionError("Session refresh journal replacement identities disagree")
    if journal.direction is _RecoveryDirection.COMMITTED_FORWARD and (
        journal.phase is not _RefreshPhase.VERIFIED
        or replacement is None
        or journal.target_image_id is None
    ):
        raise sr.SessionError("committed Session refresh journal is incomplete")


def _decode_journal(raw: object, expected_root: Path) -> _RefreshJournal:
    values = _require_mapping(raw, "document")
    expected = frozenset(
        {
            "version",
            "transaction_id",
            "project_root",
            "phase",
            "direction",
            "snapshot",
            "prior_issuance",
            "prior_runtime",
            "target_image_id",
            "target_payload_fingerprint",
            "replacement_issuance",
        }
    )
    _require_exact_fields(values, expected, "document")
    transaction_id, phase, direction, target, payload = _decode_replay_metadata(
        values, expected_root
    )
    replacement = values.get("replacement_issuance")
    journal = _RefreshJournal(
        expected_root,
        transaction_id,
        phase,
        direction,
        _decode_snapshot(values.get("snapshot"), expected_root),
        _decode_issuance(values.get("prior_issuance"), "prior issuance"),
        _decode_runtime(values.get("prior_runtime"), expected_root),
        target,
        payload,
        _decode_issuance(replacement, "replacement issuance") if replacement is not None else None,
    )
    _validate_journal_identities(journal)
    return journal


def _load_journal(project_root: Path) -> _RefreshJournal | None:
    store = _journal_store()
    if not store.validate_existing_directory():
        return None
    name = _journal_name(project_root)
    path = store.root / name
    if not path.exists() and not path.is_symlink():
        return None
    try:
        raw = store.read_json(name)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise sr.SessionError(f"cannot read Session refresh journal {path}: {exc}") from exc
    return _decode_journal(raw, project_root)


def _delete_journal(project_root: Path) -> None:
    store = _journal_store()
    path = store.root / _journal_name(project_root)
    path.unlink(missing_ok=True)
    if os.name != "nt":
        descriptor = os.open(store.root, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _encode_content(content: bytes | None) -> str:
    return base64.b64encode(content or b"").decode("ascii")


def _journal_document(journal: _RefreshJournal) -> dict[str, object]:
    snapshot = journal.snapshot
    return {
        "version": _JOURNAL_VERSION,
        "transaction_id": journal.transaction_id,
        "project_root": str(journal.project_root),
        "phase": journal.phase,
        "direction": journal.direction,
        "snapshot": {
            "spec_present": snapshot.spec_content is not None,
            "spec_content": _encode_content(snapshot.spec_content),
            "spec_mode": snapshot.spec_mode,
            "stamp_present": snapshot.stamp_content is not None,
            "stamp_content": _encode_content(snapshot.stamp_content),
            "stamp_mode": snapshot.stamp_mode,
            "image_id": snapshot.image_id,
        },
        "prior_issuance": asdict(journal.prior_issuance),
        "prior_runtime": asdict(journal.prior_runtime) if journal.prior_runtime else None,
        "target_image_id": journal.target_image_id,
        "target_payload_fingerprint": journal.target_payload_fingerprint,
        "replacement_issuance": (
            asdict(journal.replacement_issuance) if journal.replacement_issuance else None
        ),
    }


def _write_journal(journal: _RefreshJournal) -> None:
    store = _journal_store()
    store.ensure_directory()
    content = json.dumps(_journal_document(journal), indent=2, sort_keys=True) + "\n"
    store.atomic_write_text(_journal_name(journal.project_root), content)


def _new_journal(
    project_root: Path,
    snapshot: SessionSpecSnapshot,
    issuance: runtime_spec.Issuance,
    parked: sr.ParkedSession | None,
) -> _RefreshJournal:
    if parked is not None and (
        not parked.container_id
        or not parked.image_id
        or (parked.reconnect_egress and not parked.egress_network_id)
    ):
        raise sr.SessionError("cannot durably identify the prior Session Runtime")
    return _RefreshJournal(
        project_root,
        uuid4().hex,
        _RefreshPhase.PREPARED,
        _RecoveryDirection.RESTORE_ELIGIBLE,
        snapshot,
        issuance,
        parked,
        None,
        None,
        None,
    )


def _restore_journal(journal: _RefreshJournal) -> RecoveryResult:
    project = journal.project_root
    errors = []
    try:
        restore_session_spec(project, journal.snapshot)
    except BaseException as exc:  # noqa: BLE001 -- attempt every durable recovery action
        errors.append(f"host issuance: {exc}")
    if journal.prior_runtime is not None:
        try:
            sr.restore_refresh_session(
                journal.prior_runtime,
                candidate_issuance=journal.replacement_issuance,
            )
        except BaseException as exc:  # noqa: BLE001 -- attempt every durable recovery action
            errors.append(f"Session Runtime {journal.prior_runtime.backup!r}: {exc}")
    elif journal.replacement_issuance is not None:
        try:
            sr.discard_refresh_candidate(project, journal.replacement_issuance)
        except BaseException as exc:  # noqa: BLE001 -- attempt every durable recovery action
            errors.append(f"replacement Session Runtime: {exc}")
    if errors:
        raise sr.SessionError("; ".join(errors))
    _verify_restored_journal(journal)
    _delete_journal(project)
    return RecoveryResult(project, RecoveryOutcome.RESTORED)


def _verify_restored_journal(journal: _RefreshJournal) -> None:
    path = journal.snapshot.spec_path
    try:
        spec = json.loads(path.read_bytes())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise sr.SessionError(f"cannot verify restored Session Runtime spec: {exc}") from exc
    if not isinstance(spec, dict):
        raise sr.SessionError("restored Session Runtime spec is not an object")
    try:
        issuance = runtime_spec.load_recovery_snapshot(journal.project_root, spec, path)
    except runtime_spec.RuntimeSpecError as exc:
        raise sr.SessionError(f"restored host issuance did not verify: {exc}") from exc
    if issuance != journal.prior_issuance:
        raise sr.SessionError("restored host issuance differs from the recorded predecessor")
    if journal.prior_runtime is not None:
        sr.verify_restored_refresh_session(journal.prior_runtime)


def _replacement_is_coherent(journal: _RefreshJournal) -> bool:
    replacement = journal.replacement_issuance
    target = journal.target_image_id
    if replacement is None or target is None or replacement.image_id != target:
        return False
    path = dc.devcontainer_path(journal.project_root)
    try:
        spec = json.loads(path.read_bytes())
        if not isinstance(spec, dict):
            return False
        observed = runtime_spec.validate(journal.project_root, spec, path)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, runtime_spec.RuntimeSpecError):
        return False
    return observed == replacement


def _resume_journal(journal: _RefreshJournal) -> RecoveryResult:
    target = journal.target_image_id
    replacement = journal.replacement_issuance
    if (
        target is None
        or replacement is None
        or replacement.project_root != str(journal.project_root)
        or replacement.image_id != target
    ):
        raise sr.SessionError("committed Session refresh journal is incomplete")
    sr._up_unlocked(
        journal.project_root,
        expected_image_id=target,
        expected_payload_fingerprint=journal.target_payload_fingerprint,
    )
    if journal.direction is not _RecoveryDirection.COMMITTED_FORWARD:
        journal = replace(
            journal,
            phase=_RefreshPhase.VERIFIED,
            direction=_RecoveryDirection.COMMITTED_FORWARD,
        )
        _write_journal(journal)
    if journal.prior_runtime is not None:
        sr.discard_refresh_session(journal.prior_runtime)
    _delete_journal(journal.project_root)
    return RecoveryResult(journal.project_root, RecoveryOutcome.RESUMED)


def recover_project_locked(project_root: Path) -> RecoveryResult:
    """Recover one interrupted refresh while the caller holds the host lock."""
    project = project_root.resolve(strict=True)
    journal = _load_journal(project)
    if journal is None:
        return RecoveryResult(project, RecoveryOutcome.NONE)
    if journal.direction is _RecoveryDirection.COMMITTED_FORWARD or _replacement_is_coherent(
        journal
    ):
        return _resume_journal(journal)
    if journal.direction is _RecoveryDirection.RESTORE_ELIGIBLE:
        return _restore_journal(journal)
    raise sr.SessionError("Session refresh journal direction is invalid")


def _scanned_project_root(filename: str, raw: object) -> Path:
    values = _require_mapping(raw, "document")
    persisted = values.get("project_root")
    if not isinstance(persisted, str) or not persisted:
        raise sr.SessionError("Session refresh journal Project identity is invalid")
    candidate = Path(persisted)
    if not candidate.is_absolute() or str(candidate) != persisted:
        raise sr.SessionError("Session refresh journal Project identity is not canonical")
    try:
        project = candidate.resolve(strict=True)
    except OSError as exc:
        raise sr.SessionError(
            f"Session refresh journal Project is unavailable: {persisted}: {exc}"
        ) from exc
    if str(project) != persisted or _journal_name(project) != filename:
        raise sr.SessionError(
            "Session refresh journal filename does not match its canonical Project identity"
        )
    return project


def pending_refresh_projects() -> tuple[Path, ...]:
    """Return validated Projects with unfinished refresh journals without mutation."""
    store = _journal_store()
    if not store.validate_existing_directory():
        return ()
    projects = []
    for path in sorted(store.root.iterdir()):
        if path.name.startswith("."):
            continue
        if path.is_symlink() or not path.is_file() or not path.name.endswith(".json"):
            raise sr.SessionError(f"unexpected Session refresh journal entry: {path}")
        try:
            raw = store.read_json(path.name)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise sr.SessionError(f"cannot read Session refresh journal {path}: {exc}") from exc
        projects.append(_scanned_project_root(path.name, raw))
    return tuple(projects)


def has_pending_refresh(project_root: Path) -> bool:
    """Report whether one Project has a valid unfinished refresh journal."""
    project = project_root.resolve(strict=True)
    return _load_journal(project) is not None


def recover_all_locked() -> tuple[RecoveryResult, ...]:
    """Recover every interrupted refresh before shared host mutation."""
    return tuple(recover_project_locked(project) for project in pending_refresh_projects())


def shared_recovery_blocks_command(*, read_only: bool) -> bool:
    """Check or recover host-wide journals; report whether the command must stop."""
    if read_only:
        return bool(pending_refresh_projects())
    return bool(recover_all_locked())


def _reject_existing_vscode(project_root: Path) -> None:
    vscode = sr.strict_conflicting_vscode_session(project_root)
    if vscode:
        raise sr.SessionError(
            f"VS Code owns the active Session Runtime {vscode!r}; use "
            "'Dev Containers: Rebuild Container' so the editor can replace it safely"
        )


def _reject_vscode_started(project_root: Path, consequence: str) -> None:
    vscode = sr.strict_conflicting_vscode_session(project_root)
    if vscode:
        raise sr.SessionError(
            f"VS Code started Session Runtime {vscode!r} during refresh; {consequence}"
        )


def _load_recovery_issuance(
    project_root: Path, snapshot: SessionSpecSnapshot | None = None
) -> runtime_spec.Issuance:
    try:
        if snapshot is not None:
            if snapshot.spec_content is None:
                raise runtime_spec.RuntimeSpecError("prior Session Runtime spec is missing")
            try:
                spec = json.loads(snapshot.spec_content)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise runtime_spec.RuntimeSpecError(
                    "prior Session Runtime spec is not valid JSON"
                ) from exc
            if not isinstance(spec, dict):
                raise runtime_spec.RuntimeSpecError("prior Session Runtime spec is not an object")
            return runtime_spec.load_recovery_snapshot(project_root, spec, snapshot.spec_path)
        return runtime_spec.load_issued_snapshot(project_root)
    except runtime_spec.RuntimeSpecError as exc:
        raise sr.SessionError(f"cannot preserve the prior Session Runtime: {exc}") from exc


def _reconcile_refresh_image(
    journal: _RefreshJournal, inspection: LifecycleResult, *, verbose: bool
) -> tuple[LifecycleResult, _RefreshJournal]:
    if journal.prior_runtime is not None:
        sr.park_planned_session(journal.prior_runtime)
    journal = replace(journal, phase=_RefreshPhase.PARKED)
    _write_journal(journal)
    result = refresh_session_image(
        journal.project_root,
        verbose=verbose,
        inspection=inspection,
    )
    if result.selected_id is None:
        raise sr.SessionError("image refresh did not return an immutable Session Image ID")
    journal = replace(
        journal,
        phase=_RefreshPhase.IMAGE_SELECTED,
        target_image_id=result.selected_id,
        target_payload_fingerprint=result.payload_fingerprint,
    )
    _write_journal(journal)
    return result, journal


def _issue_and_verify_replacement(
    journal: _RefreshJournal, result: LifecycleResult, *, verbose: bool
) -> _RefreshJournal:
    assert result.selected_id is not None
    project = journal.project_root
    reissue_session_spec(project, result.selected_id, verbose=verbose)
    issuance = runtime_spec.load_issued_snapshot(project)
    journal = replace(
        journal,
        phase=_RefreshPhase.ISSUED,
        replacement_issuance=issuance,
    )
    _write_journal(journal)
    _reject_vscode_started(project, "the preserved headless Session was not replaced")
    sr._up_unlocked(  # refresh already owns the host lifecycle lock
        project,
        expected_image_id=result.selected_id,
        expected_payload_fingerprint=result.payload_fingerprint,
    )
    _reject_vscode_started(project, "the new headless Session is being rolled back")
    journal = replace(
        journal,
        phase=_RefreshPhase.VERIFIED,
        direction=_RecoveryDirection.COMMITTED_FORWARD,
    )
    _write_journal(journal)
    return journal


def _recover_failed_refresh(project_root: Path, original: BaseException) -> None:
    try:
        current = _load_journal(project_root)
        if current is None:
            raise sr.SessionError("Session refresh journal disappeared")
        if current.direction is _RecoveryDirection.COMMITTED_FORWARD:
            raise sr.SessionError("Session refresh already committed forward")
        _restore_journal(current)
    except BaseException as recovery_error:  # noqa: BLE001 -- preserve original failure
        raise sr.SessionError(
            f"Session refresh failed ({original}); recovery was incomplete: {recovery_error}"
        ) from original


def _refresh_unlocked(project_root: Path, *, verbose: bool) -> LifecycleResult:
    project = project_root.resolve(strict=True)
    _reject_existing_vscode(project)
    inspection = inspect_refreshable_session_image(project, verbose=verbose)
    snapshot = capture_session_spec(project)
    issuance = _load_recovery_issuance(project, snapshot)
    journal = _new_journal(project, snapshot, issuance, sr.plan_session_refresh(project, issuance))
    _write_journal(journal)
    try:
        result, journal = _reconcile_refresh_image(journal, inspection, verbose=verbose)
        journal = _issue_and_verify_replacement(journal, result, verbose=verbose)
    except BaseException as exc:
        _recover_failed_refresh(project, exc)
        raise
    if journal.prior_runtime is not None:
        sr.discard_refresh_session(journal.prior_runtime)
    _delete_journal(project)
    return result


def refresh(project_root: Path, *, verbose: bool = False) -> LifecycleResult:
    """Refresh the selected image and replace its headless Session atomically."""
    with host_lifecycle_lock("session refresh"):
        recovered = recover_all_locked()
        if recovered:
            raise sr.SessionError(
                "recovered an interrupted Session refresh; run `booley session refresh` again"
            )
        return _refresh_unlocked(project_root, verbose=verbose)
