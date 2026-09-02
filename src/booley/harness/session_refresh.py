"""Recoverable Session Image and Runtime refresh orchestration."""

from __future__ import annotations

from pathlib import Path

from booley.eda.provisioning import runtime_spec
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


def _rollback(
    project_root: Path,
    snapshot: SessionSpecSnapshot,
    parked: sr.ParkedSession | None,
    candidate_issuance: runtime_spec.Issuance | None,
) -> tuple[str, ...]:
    errors: list[str] = []
    try:
        restore_session_spec(project_root, snapshot)
    except BaseException as exc:  # noqa: BLE001 -- compensation must survive interrupts
        errors.append(f"host issuance: {exc}")
    if parked is not None:
        try:
            sr.restore_refresh_session(parked)
        except BaseException as exc:  # noqa: BLE001 -- attempt every compensation
            errors.append(f"Session Runtime {parked.backup!r}: {exc}")
    elif candidate_issuance is not None:
        try:
            sr.discard_refresh_candidate(project_root, candidate_issuance)
        except BaseException as exc:  # noqa: BLE001 -- attempt every compensation
            errors.append(f"new Session Runtime: {exc}")
    return tuple(errors)


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


def _load_recovery_issuance(project_root: Path) -> runtime_spec.Issuance:
    try:
        return runtime_spec.load_issued_snapshot(project_root)
    except runtime_spec.RuntimeSpecError as exc:
        raise sr.SessionError(f"cannot preserve the prior Session Runtime: {exc}") from exc


def _refresh_unlocked(project_root: Path, *, verbose: bool) -> LifecycleResult:
    _reject_existing_vscode(project_root)
    inspection = inspect_refreshable_session_image(project_root, verbose=verbose)
    snapshot = capture_session_spec(project_root)
    issuance = _load_recovery_issuance(project_root)
    parked = sr.park_session_for_refresh(project_root, issuance)
    candidate_issuance = None
    candidate_ready = False
    try:
        result = refresh_session_image(
            project_root,
            verbose=verbose,
            inspection=inspection,
        )
        if result.selected_id is None:
            raise sr.SessionError("image refresh did not return an immutable Session Image ID")
        reissue_session_spec(project_root, result.selected_id, verbose=verbose)
        candidate_issuance = runtime_spec.load_issued_snapshot(project_root)
        _reject_vscode_started(
            project_root,
            "the preserved headless Session was not replaced",
        )
        sr._up_unlocked(  # refresh already owns the host lifecycle lock
            project_root,
            expected_image_id=result.selected_id,
            expected_payload_fingerprint=result.payload_fingerprint,
        )
        candidate_ready = True
        _reject_vscode_started(
            project_root,
            "the new headless Session is being rolled back",
        )
    except BaseException as exc:
        rollback_errors = _rollback(
            project_root,
            snapshot,
            parked,
            candidate_issuance if candidate_ready else None,
        )
        if rollback_errors:
            detail = "; ".join(rollback_errors)
            recovery = f"; recovery container: {parked.backup}" if parked is not None else ""
            raise sr.SessionError(
                f"Session refresh failed ({exc}); rollback was incomplete: {detail}{recovery}"
            ) from exc
        raise
    if parked is not None:
        sr.discard_refresh_session(parked)
    return result


def refresh(project_root: Path, *, verbose: bool = False) -> LifecycleResult:
    """Refresh the selected image and replace its headless Session atomically."""
    with host_lifecycle_lock("session refresh"):
        return _refresh_unlocked(project_root, verbose=verbose)
