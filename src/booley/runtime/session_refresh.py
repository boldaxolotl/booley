"""Recoverable Sandbox Image reconciliation and Sandbox replacement."""

from __future__ import annotations

import base64
import hashlib
import json
import os
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, replace
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

from booley.core.differences import format_differences
from booley.core.private_store import PrivateStore
from booley.core.user_paths import config_dir
from booley.runtime import devcontainer as dc
from booley.runtime import session_issuance as runtime_spec
from booley.runtime import session_runtime as sr
from booley.runtime.lifecycle_lock import host_lifecycle_lock
from booley.runtime.session_spec import (
    SessionSpecSnapshot,
    capture_session_spec,
    restore_session_spec,
)

_JOURNAL_VERSION = 2
_JOURNAL_DIR = Path("eda") / "session-refresh"


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
class RefreshPreparedImage:
    """Durable image-tag state needed to recover candidate adoption."""

    reference: str
    candidate_reference: str
    candidate_id: str
    prior_id: str | None


@dataclass(frozen=True, slots=True)
class RefreshImage:
    """Immutable Sandbox Image facts needed by runtime replacement."""

    selected_reference: str
    selected_id: str
    payload_fingerprint: str | None = None
    wheel_sha256: str | None = None
    prepared_images: tuple[RefreshPreparedImage, ...] = ()
    image_graph_changed: bool = True
    wheel_source_fingerprint: str | None = None


@dataclass(frozen=True, slots=True)
class RefreshResult:
    """Verified outcome, identified by immutable container IDs."""

    image: RefreshImage
    outcome: str
    previous_container_id: str | None
    container_id: str

    @property
    def message(self) -> str:
        if self.outcome == "kept":
            return "kept: already current"
        if self.outcome == "created":
            return f"created {self.container_id}"
        return f"replaced {self.previous_container_id} → {self.container_id}"


class RuntimeImageOperations(Protocol):
    """Image operations composed by the Project Initialization caller."""

    def inspect(self, project_root: Path, *, verbose: bool) -> None: ...

    def prepare(self, project_root: Path, *, verbose: bool) -> RefreshImage: ...

    def validate(self, prepared: RefreshImage) -> None: ...

    def commit(self) -> RefreshImage: ...

    def abort(self) -> None: ...

    def prepare_spec(self, project_root: Path, image: RefreshImage) -> None: ...

    def spec_is_current(self, project_root: Path) -> bool: ...

    def validate_spec(self, project_root: Path) -> None: ...

    def requires_license(self) -> bool: ...

    def finalize(self, project_root: Path) -> None: ...

    def reissue(
        self,
        project_root: Path,
        image_id: str,
        *,
        verbose: bool,
    ) -> None: ...


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
    target_wheel_sha256: str | None
    prepared_images: tuple[RefreshPreparedImage, ...]
    replacement_issuance: runtime_spec.Issuance | None


def _journal_store() -> PrivateStore:
    root = config_dir() / _JOURNAL_DIR
    return PrivateStore(root, config_dir(), "Session refresh journal", sr.SessionError)


def _journal_name(project_root: Path) -> str:
    identity = hashlib.sha256(str(project_root).encode()).hexdigest()
    return f"{identity}.json"


def _decode_issuance(raw: object, label: str) -> runtime_spec.Issuance:
    try:
        return runtime_spec.issuance_from_document(raw)
    except runtime_spec.RuntimeSpecError as exc:
        raise ValueError(f"{label} is invalid: {exc}") from exc


def _decode_snapshot(
    project_root: Path,
    *,
    spec_present: bool,
    spec_content: str,
    spec_mode: int,
    stamp_present: bool,
    stamp_content: str,
    stamp_mode: int,
    image_id: str | None,
) -> SessionSpecSnapshot:
    """Build the snapshot from its stored fields; unknown or missing keys raise TypeError."""
    return SessionSpecSnapshot(
        dc.devcontainer_path(project_root),
        base64.b64decode(spec_content, validate=True) if spec_present else None,
        spec_mode,
        runtime_spec.stamp_path(project_root),
        base64.b64decode(stamp_content, validate=True) if stamp_present else None,
        stamp_mode,
        image_id,
    )


def _journal_from(
    expected_root: Path,
    *,
    transaction_id: str,
    project_root: str,
    phase: str,
    direction: str,
    snapshot: dict[str, Any],
    prior_issuance: object,
    prior_runtime: dict[str, Any] | None,
    target_image_id: str | None,
    target_payload_fingerprint: str | None,
    replacement_issuance: object,
    target_wheel_sha256: str | None = None,
    prepared_images: Sequence[dict[str, Any]] = (),
) -> _RefreshJournal:
    """Build a journal directly from its stored fields (version 1 omits the last two)."""
    if project_root != str(expected_root):
        raise ValueError("journal belongs to another Project")
    return _RefreshJournal(
        expected_root,
        transaction_id,
        _RefreshPhase(phase),
        _RecoveryDirection(direction),
        _decode_snapshot(expected_root, **snapshot),
        _decode_issuance(prior_issuance, "prior issuance"),
        sr.ParkedSession(**prior_runtime) if prior_runtime is not None else None,
        target_image_id,
        target_payload_fingerprint,
        target_wheel_sha256,
        tuple(RefreshPreparedImage(**item) for item in prepared_images),
        (
            _decode_issuance(replacement_issuance, "replacement issuance")
            if replacement_issuance is not None
            else None
        ),
    )


def _decode_journal(raw: object, expected_root: Path, path: Path) -> _RefreshJournal:
    """Decode a journal; any structural fault refuses recovery and leaves the file on disk."""
    try:
        if not isinstance(raw, dict):
            raise TypeError("document must be an object")
        version = raw.pop("version", None)
        if version not in {1, _JOURNAL_VERSION}:
            raise ValueError(f"unsupported version {version!r}")
        return _journal_from(expected_root, **raw)
    except (TypeError, ValueError) as exc:
        raise sr.SessionError(f"cannot decode Session refresh journal {path}: {exc}") from exc


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
    return _decode_journal(raw, project_root, path)


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
        "target_wheel_sha256": journal.target_wheel_sha256,
        "prepared_images": [asdict(image) for image in journal.prepared_images],
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
        raise sr.SessionError("cannot durably identify the prior Sandbox")
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
        (),
        None,
    )


def _restore_journal(journal: _RefreshJournal) -> RecoveryResult:
    project = journal.project_root
    errors = []
    try:
        _recover_prepared_image_tags(journal.prepared_images, restore=True)
    except BaseException as exc:  # noqa: BLE001 -- attempt every durable recovery action
        errors.append(f"Sandbox Image tags: {exc}")
    try:
        restore_session_spec(project, journal.snapshot)
    except BaseException as exc:  # noqa: BLE001 -- attempt every durable recovery action
        errors.append(f"host issuance: {exc}")
    if journal.prior_runtime is not None:
        try:
            sr.restore_refresh_session(
                journal.prior_runtime,
                candidate_issuance=journal.replacement_issuance,
                recovery=True,
            )
        except BaseException as exc:  # noqa: BLE001 -- attempt every durable recovery action
            errors.append(f"Sandbox {journal.prior_runtime.backup!r}: {exc}")
    elif journal.replacement_issuance is not None:
        try:
            sr.discard_refresh_candidate(project, journal.replacement_issuance)
        except BaseException as exc:  # noqa: BLE001 -- attempt every durable recovery action
            errors.append(f"replacement Sandbox: {exc}")
    if errors:
        raise sr.SessionError("; ".join(errors))
    _verify_restored_journal(journal)
    _delete_journal(project)
    _cleanup_owned_images(project)
    return RecoveryResult(project, RecoveryOutcome.RESTORED)


def _verify_restored_journal(journal: _RefreshJournal) -> None:
    path = journal.snapshot.spec_path
    try:
        spec = json.loads(path.read_bytes())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise sr.SessionError(f"cannot verify restored Sandbox spec: {exc}") from exc
    if not isinstance(spec, dict):
        raise sr.SessionError("restored Sandbox spec is not an object")
    try:
        issuance = runtime_spec.load_recovery_snapshot(journal.project_root, spec, path)
    except runtime_spec.RuntimeSpecError as exc:
        raise sr.SessionError(f"restored host issuance did not verify: {exc}") from exc
    if issuance != journal.prior_issuance:
        raise sr.SessionError(
            "restored host issuance differs from the recorded predecessor: "
            + format_differences(asdict(journal.prior_issuance), asdict(issuance))
        )
    if journal.prior_runtime is not None:
        sr.verify_restored_refresh_session(journal.prior_runtime, recovery=True)


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
        expected_wheel_source_fingerprint=journal.target_payload_fingerprint,
        expected_wheel_sha256=journal.target_wheel_sha256,
        recovery=True,
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
    _recover_prepared_image_tags(journal.prepared_images, restore=False)
    finalize_project(journal.project_root)
    _delete_journal(journal.project_root)
    _cleanup_owned_images(journal.project_root)
    return RecoveryResult(journal.project_root, RecoveryOutcome.RESUMED)


def _recover_prepared_image_tags(
    images: tuple[RefreshPreparedImage, ...],
    *,
    restore: bool,
) -> None:
    """Restore prior managed tags or clean committed transaction references."""
    from booley.runtime import image_lifecycle

    docker = image_lifecycle._docker_adapter()
    for image in images:
        candidate = docker.image_id(image.candidate_reference)
        if candidate is not None and candidate != image.candidate_id:
            raise sr.SessionError("candidate tag changed before recovery")
        managed = docker.image_id(image.reference)
        if restore and managed not in {image.candidate_id, image.prior_id, None}:
            raise sr.SessionError(
                "managed tag has an unexpected immutable identity before recovery"
            )
    for image in reversed(images):
        if image.candidate_reference == image.reference:
            continue
        managed_id = docker.image_id(image.reference)
        if restore and managed_id == image.candidate_id:
            if image.prior_id is None:
                docker.remove_tag(image.reference)
            else:
                docker.tag(image.prior_id, image.reference)
        if docker.image_id(image.candidate_reference) is not None:
            docker.remove_tag(image.candidate_reference)


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
    return _restore_journal(journal)


def _scanned_project_root(filename: str, raw: object) -> Path:
    persisted = raw.get("project_root") if isinstance(raw, dict) else None
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
    """Report whether one Project has a decodable unfinished refresh journal."""
    project = project_root.resolve(strict=True)
    return _load_journal(project) is not None


def recover_all_locked() -> tuple[RecoveryResult, ...]:
    """Recover every interrupted refresh before shared host mutation."""
    return tuple(recover_project_locked(project) for project in pending_refresh_projects())


def shared_recovery_blocks_command(
    *,
    read_only: bool,
    cleanup_resources: Callable[[str], tuple[str, ...]] | None = None,
) -> bool:
    """Check or recover host-wide journals; report whether the command must stop."""
    from booley.runtime import issuance_invalidation

    if read_only:
        refreshes = pending_refresh_projects()
        invalidations = issuance_invalidation.pending_invalidations()
        return bool(refreshes or invalidations)
    if cleanup_resources is None:
        from booley.eda.provisioning.licensing.flexnet_docker import (
            cleanup_project_resources_for_identity,
        )

        cleanup_resources = cleanup_project_resources_for_identity
    invalidated = issuance_invalidation.recover_all_locked(
        cleanup_resources=cleanup_resources,
    )
    refreshed = recover_all_locked()
    return bool(invalidated or refreshed)


def _reject_existing_vscode(project_root: Path) -> None:
    vscode = sr.strict_conflicting_vscode_session(project_root)
    if vscode:
        raise sr.SessionError(
            f"VS Code owns the active Sandbox {vscode!r}; use "
            "'Dev Containers: Rebuild Container' so the editor can replace it safely"
        )


def _reject_vscode_started(project_root: Path, consequence: str) -> None:
    try:
        vscode = sr.strict_conflicting_vscode_session(project_root)
    except sr.SessionError as exc:
        raise sr.SessionError(f"{exc}; {consequence}") from exc
    if vscode:
        raise sr.SessionError(f"VS Code started Sandbox {vscode!r} during refresh; {consequence}")


def _load_recovery_issuance(
    project_root: Path, snapshot: SessionSpecSnapshot | None = None
) -> runtime_spec.Issuance:
    try:
        if snapshot is not None:
            if snapshot.spec_content is None:
                raise runtime_spec.RuntimeSpecError("prior Sandbox spec is missing")
            try:
                spec = json.loads(snapshot.spec_content)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise runtime_spec.RuntimeSpecError(
                    "prior Sandbox spec is not valid JSON"
                ) from exc
            if not isinstance(spec, dict):
                raise runtime_spec.RuntimeSpecError("prior Sandbox spec is not an object")
            return runtime_spec.load_recovery_snapshot(project_root, spec, snapshot.spec_path)
        return runtime_spec.load_issued_snapshot(project_root)
    except runtime_spec.RuntimeSpecError as exc:
        raise sr.SessionError(
            f"cannot preserve the prior Sandbox: {exc}; {sr.sandbox_remedy(project_root, recovery_unavailable=True)}"
        ) from exc


def _reconcile_refresh_image(
    journal: _RefreshJournal,
    prepared_result: RefreshImage,
    images: RuntimeImageOperations,
) -> tuple[RefreshImage, _RefreshJournal]:
    if journal.prior_runtime is not None:
        sr.park_planned_session(journal.prior_runtime)
    journal = replace(journal, phase=_RefreshPhase.PARKED)
    _write_journal(journal)
    result = images.commit()
    if not result.selected_id:
        raise sr.SessionError("refresh did not produce an immutable Sandbox Image ID")
    if result != prepared_result:
        raise sr.SessionError(
            "committed Sandbox Image differs from the verified candidate: "
            + format_differences(asdict(prepared_result), asdict(result))
        )
    journal = replace(
        journal,
        phase=_RefreshPhase.IMAGE_SELECTED,
        target_image_id=result.selected_id,
        target_payload_fingerprint=result.payload_fingerprint,
        target_wheel_sha256=result.wheel_sha256,
    )
    _write_journal(journal)
    return result, journal


def _issue_and_verify_replacement(
    journal: _RefreshJournal,
    result: RefreshImage,
    images: RuntimeImageOperations,
    *,
    verbose: bool,
) -> _RefreshJournal:
    project = journal.project_root
    images.reissue(project, result.selected_id, verbose=verbose)
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
        expected_wheel_source_fingerprint=(
            result.wheel_source_fingerprint or result.payload_fingerprint
        ),
        expected_wheel_sha256=result.wheel_sha256,
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

    if current.prior_runtime is None:
        message = f"candidate failed: {original}; restored prior issuance; no prior Sandbox"
    else:
        message = (
            f"rolled back to {current.prior_runtime.container_id}; candidate failed: {original}"
        )
    raise sr.SessionError(message) from original


@dataclass(frozen=True, slots=True)
class _PreparedRefresh:
    image: RefreshImage
    snapshot: SessionSpecSnapshot
    issuance: runtime_spec.Issuance
    parked: sr.ParkedSession | None


def _prepare_refresh(
    project: Path, prior: runtime_spec.Issuance, images: RuntimeImageOperations, *, verbose: bool
) -> _PreparedRefresh:
    image = images.prepare(project, verbose=verbose)
    if not image.selected_id:
        raise sr.SessionError("refresh did not produce an immutable Sandbox Image ID")
    snapshot = capture_session_spec(project)
    issuance = _load_recovery_issuance(project, snapshot)
    if issuance != prior:
        raise sr.SessionError("prior Sandbox issuance changed during refresh preparation")
    parked = sr.plan_session_refresh(project, issuance)
    images.prepare_spec(project, image)
    images.validate(image)
    images.validate_spec(project)
    return _PreparedRefresh(image, snapshot, issuance, parked)


def _finish_refresh(
    journal: _RefreshJournal, result: RefreshImage, images: RuntimeImageOperations
) -> RefreshResult:
    project = journal.project_root
    try:
        container_id = sr.refresh_container_id(project)
        if journal.prior_runtime is not None:
            sr.discard_refresh_session(journal.prior_runtime)
        images.finalize(project)
    except Exception as exc:
        raise sr.SessionError(
            f"replacement committed; final reconciliation is incomplete: {exc}"
        ) from exc
    _delete_journal(project)
    _cleanup_owned_images(project)
    parked = journal.prior_runtime
    return RefreshResult(
        result,
        "replaced" if parked else "created",
        parked.container_id if parked else None,
        container_id,
    )


def _refresh_unlocked(
    project_root: Path, images: RuntimeImageOperations, *, verbose: bool
) -> RefreshResult:
    project = project_root.resolve(strict=True)
    _reject_existing_vscode(project)
    prior = _load_recovery_issuance(project, capture_session_spec(project))
    images.inspect(project, verbose=verbose)
    try:
        prepared = _prepare_refresh(project, prior, images, verbose=verbose)
        parked = prepared.parked
        committed = _try_complete_noop(project, prepared.image, prepared.issuance, parked, images)
        if committed is not None:
            images.finalize(project)
            assert parked is not None and parked.container_id is not None
            return RefreshResult(committed, "kept", parked.container_id, parked.container_id)
        if parked is not None and images.requires_license():
            raise sr.SessionError(
                "refresh cannot add a licensed relay topology to an existing Sandbox; "
                "run `booley session down` then `booley session refresh`"
            )
        action = "replace" if parked is not None else "create"
        print(f"  planned: {action} Sandbox after verification")
        journal = _prepared_journal(
            project, prepared.snapshot, prepared.issuance, parked, prepared.image
        )
        result, journal = _reconcile_refresh_image(journal, prepared.image, images)
        journal = _issue_and_verify_replacement(journal, result, images, verbose=verbose)
    except BaseException as exc:
        try:
            images.abort()
        finally:
            if _load_journal(project) is not None:
                _recover_failed_refresh(project, exc)
        raise
    return _finish_refresh(journal, result, images)


def _try_complete_noop(
    project: Path,
    prepared: RefreshImage,
    issuance: runtime_spec.Issuance,
    parked: sr.ParkedSession | None,
    images: RuntimeImageOperations,
) -> RefreshImage | None:
    if not _is_current_running_session(prepared, issuance, parked) or not images.spec_is_current(
        project
    ):
        return None
    try:
        sr.verify_refreshed_session(
            project,
            prepared.selected_id,
            None,
            expected_wheel_source_fingerprint=(
                prepared.wheel_source_fingerprint or prepared.payload_fingerprint
            ),
            expected_wheel_sha256=prepared.wheel_sha256,
        )
    except sr.SessionError:
        return None
    committed = images.commit()
    if committed != prepared:
        raise sr.SessionError(
            "committed Sandbox Image differs from the verified candidate: "
            + format_differences(asdict(prepared), asdict(committed))
        )
    return committed


def _is_current_running_session(
    prepared: RefreshImage,
    issuance: runtime_spec.Issuance,
    parked: sr.ParkedSession | None,
) -> bool:
    return (
        not prepared.image_graph_changed
        and issuance.image_id == prepared.selected_id
        and parked is not None
        and parked.image_id == prepared.selected_id
        and parked.was_running
    )


def _prepared_journal(
    project: Path,
    snapshot: SessionSpecSnapshot,
    issuance: runtime_spec.Issuance,
    parked: sr.ParkedSession | None,
    prepared: RefreshImage,
) -> _RefreshJournal:
    journal = _new_journal(project, snapshot, issuance, parked)
    journal = replace(
        journal,
        target_image_id=prepared.selected_id,
        target_payload_fingerprint=(
            prepared.wheel_source_fingerprint or prepared.payload_fingerprint
        ),
        target_wheel_sha256=prepared.wheel_sha256,
        prepared_images=prepared.prepared_images,
    )
    _write_journal(journal)
    return journal


def refresh(
    project_root: Path,
    images: RuntimeImageOperations,
    *,
    verbose: bool = False,
) -> RefreshResult:
    """Refresh the selected image and replace its headless Session atomically."""
    with host_lifecycle_lock("session refresh"):
        recovered = shared_recovery_blocks_command(read_only=False)
        if recovered:
            raise sr.SessionError(
                "recovered interrupted Sandbox host state; run `booley session refresh` again"
            )
        return _refresh_unlocked(project_root, images, verbose=verbose)


def finalize_project(project: Path) -> None:
    """Reconcile ancillary files only after the runtime has committed forward."""
    from booley.commit_policy import stealth_enabled
    from booley.runtime import dashboard_tasks
    from booley.runtime.git import add_git_excludes

    source = runtime_spec.authorized_project_data_source(project)
    dashboard_tasks.reconcile(project, source)
    names = (".devcontainer", ".claude")
    add_git_excludes(project, (*names, ".booley") if stealth_enabled(project) else names)


def _cleanup_owned_images(project: Path) -> None:
    from booley.runtime import image_lifecycle

    try:
        retained_images = image_lifecycle.cleanup_owned_images(project)
    except image_lifecycle.ImageLifecycleError as exc:
        sr.logger.warning("refresh image cleanup deferred: %s", exc)
        return
    for retained in retained_images:
        sr.logger.warning("refresh image cleanup: %s", retained)
