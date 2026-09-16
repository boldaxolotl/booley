"""Plan and apply Interactive Mode Project Initialization reconciliation."""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from booley.eda.provisioning.licensing.flexnet_docker import (
    remove_relay,
    resources_for_session,
)
from booley.runtime import interactive_docker, session_issuance, session_runtime
from booley.runtime.git import add_git_excludes, git_excludes_pending

_EXCLUDE_NAMES = (".devcontainer", ".booley_project", ".claude")


@dataclass(frozen=True, slots=True)
class InteractiveInitRequest:
    """Inputs needed to prepare one immutable Interactive Mode plan."""

    project_root: Path
    build_spec: session_issuance.SpecBuilder
    expected_image_id: str | None = None
    mask_source: Path | None = None


@dataclass(frozen=True, slots=True)
class InteractiveInitPlan:
    """One shared desired-versus-actual Interactive Mode decision."""

    request: InteractiveInitRequest
    prepared: session_issuance.PreparedSessionSpec
    issuance: session_issuance.Issuance | None
    issuance_problem: str = ""
    exclusions_pending: bool = False
    relay_cleanup_pending: bool = False
    runtime_cleanup_pending: bool = False

    @property
    def pending_details(self) -> tuple[str, ...]:
        """Human-readable actions ordinary initialization would apply."""
        details: list[str] = []
        if self.issuance is None:
            details.append(f"Sandbox specification/issuance ({self.issuance_problem})")
        if self.exclusions_pending:
            details.append("Git exclusions")
        if self.relay_cleanup_pending:
            details.append("orphaned license relay")
        if self.runtime_cleanup_pending:
            details.append("stopped Sandbox resources")
        return tuple(details)


@dataclass(frozen=True, slots=True)
class InteractiveInitChanges:
    """Effects produced while applying an :class:`InteractiveInitPlan`."""

    issuance: session_issuance.Issuance
    issued: bool
    relay_removed: bool
    runtime_reconciled: bool
    exclusions_changed: bool

    @property
    def changed(self) -> bool:
        return any(
            (
                self.issued,
                self.relay_removed,
                self.runtime_reconciled,
                self.exclusions_changed,
            )
        )


def inspect(request: InteractiveInitRequest) -> InteractiveInitPlan:
    """Prepare desired state and compare every managed Interactive Mode output."""
    prepared = session_issuance.preview(
        request.project_root,
        request.build_spec,
        expected_image_id=request.expected_image_id,
    )
    issuance, issuance_problem = _inspect_issuance(request.project_root, prepared)
    licensed = prepared.inputs.license_profile_name is not None
    relay_pending = bool(
        not licensed and shutil.which("docker") and _unlicensed_relay_pending(request.project_root)
    )
    desired_issuance = issuance or prepared.prospective_issuance
    if desired_issuance is None:
        raise session_issuance.RuntimeSpecError(
            "prepared Sandbox specification lacks a prospective issuance"
        )
    runtime_pending = session_runtime.plan_stopped_headless_runtime_reconciliation(
        request.project_root, desired_issuance
    ).pending
    return InteractiveInitPlan(
        request=request,
        prepared=prepared,
        issuance=issuance,
        issuance_problem=issuance_problem,
        exclusions_pending=git_excludes_pending(request.project_root, _EXCLUDE_NAMES),
        relay_cleanup_pending=relay_pending,
        runtime_cleanup_pending=runtime_pending,
    )


def apply(plan: InteractiveInitPlan, *, force: bool = False) -> InteractiveInitChanges:
    """Apply one prepared plan, revalidating mutable external state before effects."""
    issued = plan.issuance is None or force
    issuance = plan.issuance
    if issued:
        if plan.request.mask_source is not None:
            plan.request.mask_source.mkdir(parents=True, exist_ok=True)
        issuance = session_issuance.issue_prepared(
            plan.request.project_root,
            plan.prepared,
            force_dependencies=force,
        )
    assert issuance is not None
    relay_removed = bool(
        issuance.license_profile is None
        and shutil.which("docker")
        and _remove_unlicensed_relay(plan.request.project_root)
    )
    runtime_reconciled = session_runtime.reconcile_stopped_headless_runtime(
        plan.request.project_root, issuance
    )
    exclusions_changed = add_git_excludes(plan.request.project_root, _EXCLUDE_NAMES)
    return InteractiveInitChanges(
        issuance=issuance,
        issued=issued,
        relay_removed=relay_removed,
        runtime_reconciled=runtime_reconciled,
        exclusions_changed=exclusions_changed,
    )


def _inspect_issuance(
    project_root: Path,
    prepared: session_issuance.PreparedSessionSpec,
) -> tuple[session_issuance.Issuance | None, str]:
    try:
        return session_issuance.inspect_prepared(project_root, prepared), ""
    except session_issuance.RuntimeSpecError as exc:
        return None, str(exc)


def _unlicensed_relay_pending(project_root: Path) -> bool:
    resources = resources_for_session(str(project_root.resolve()))
    return (
        interactive_docker.container_exists(resources.relay_container)
        or interactive_docker.network_exists(resources.private_network)
        or interactive_docker.network_exists(resources.outbound_network)
    )


def _remove_unlicensed_relay(project_root: Path) -> bool:
    if not _unlicensed_relay_pending(project_root):
        return False
    remove_relay(resources_for_session(str(project_root.resolve())))
    return True
