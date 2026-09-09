"""Criterion bindings, evidence recording and source invalidation."""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Any

from booley.criteria.categories import verification_fingerprint_categories
from booley.criteria.state import (
    SOURCE_FINGERPRINT_DETAIL_KEY,
    CriterionChange,
)
from booley.flows.criterion_freshness import build_criterion_freshness
from booley.flows.endpoint_diff import (
    _classify_files,
)
from booley.flows.endpoint_events import (
    _emit_criteria_update,
)
from booley.flows.endpoint_session import PreparedExecution
from booley.fusesoc.fusesoc_registry import FuseSocError
from booley.runtime.endpoint_execution import (
    EXIT_ERROR,
    EXIT_SUCCESS,
    EndpointOutcome,
)

if TYPE_CHECKING:
    from booley.flows.endpoint_state import EndpointState


logger = logging.getLogger(__name__)


def set_criterion(
    endpoint: EndpointState,
    key: str,
    met: bool,
    *,
    detail: dict[str, Any] | None = None,
    source_target: str | None = None,
) -> None:
    """Set a criterion and persist state. No-op when state has no file."""
    if getattr(endpoint.args, "diagnostic", False):
        logger.info("Diagnostic run: not recording criterion %s", key)
        return
    key = endpoint._criterion_key_for_source(key, source_target)
    stamped_detail = endpoint._stamp_source_fingerprint(
        key,
        met,
        detail,
        source_target=source_target,
    )
    changes = endpoint.state.set_criterion(key, met, detail=stamped_detail)
    if endpoint.state._file_path is not None:
        endpoint._record_acceptance_changes(changes)
        endpoint.state.save()
        _emit_criteria_update(endpoint.state)


def _record_acceptance_changes(endpoint: EndpointState, changes: list[CriterionChange]) -> None:
    """Append normalized strict-Ticket outcomes before mutable state is saved."""
    from booley.review.execution_context import validate_recording

    validate_recording(getattr(endpoint.args, "work_dir", None))
    if not changes or not endpoint.state.strict_criteria:
        return
    endpoint._acceptance_recorder.record_changes(
        endpoint.state,
        changes,
        invocation_id=endpoint._invocation_id,
        producer=endpoint.name,
    )


def _criterion_key_for_source(endpoint: EndpointState, key: str, source_target: str | None) -> str:
    """Render a criterion key with the Target name, never a qualified selector."""
    if not source_target or not key.endswith(source_target):
        return key
    try:
        from booley.targets.catalog import TargetCatalog

        name = TargetCatalog.build(Path(endpoint.args.work_dir)).select(source_target).name
    except FuseSocError:
        return key
    return key[: -len(source_target)] + name


def _stamp_source_fingerprint(
    endpoint: EndpointState,
    key: str,
    met: bool,
    detail: dict[str, Any] | None,
    *,
    source_target: str | None,
) -> dict[str, Any] | None:
    """Attach source freshness metadata to verification criteria.

    Failed criteria retain actionable evidence, so every verification
    outcome receives the same atomic source/contract receipt.
    """
    categories = verification_fingerprint_categories(key)
    is_review = key.startswith(("review_rtl_", "review_tb_"))
    if not categories:
        return detail
    stamped = dict(detail or {})
    try:
        freshness = build_criterion_freshness(
            Path(endpoint.args.work_dir),
            target=source_target,
            categories=categories,
        )
    except (OSError, FuseSocError) as exc:
        logger.warning(
            "Could not stamp source fingerprint for criterion %s, target %r: %s",
            key,
            source_target,
            exc,
        )
        return stamped
    source_detail = freshness.to_detail()
    if is_review and stamped.get("review_detail_version") == 4:
        from booley.review.receipt import finalize_review_detail

        return finalize_review_detail(stamped, source_detail)
    stamped[SOURCE_FINGERPRINT_DETAIL_KEY] = source_detail
    return stamped


def classify_git_diff(endpoint: EndpointState) -> set[str]:
    """Classify changes since pre-run HEAD as RTL and/or TB categories.

    Uses ``_pre_run_head`` (captured before ``_run()``) so that commits
    made during the endpoint run are included in the diff.

    Returns set of category strings (may be empty if no changes).
    """
    ref = getattr(endpoint, "_pre_run_head", None) or "HEAD"
    work_dir = endpoint.args.work_dir
    try:
        result = subprocess.run(
            ["git", "diff", "--name-only", ref],
            cwd=work_dir,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if result.returncode != 0:
            return set()
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return set()
    return _classify_files(result.stdout.splitlines(), work_dir)


def invalidate_dependent_criteria(endpoint: EndpointState) -> list[str]:
    """Invalidate criteria based on what files changed.

    Only runs for code-modifying endpoints. Detects RTL vs TB changes
    from git diff and resets the appropriate criteria categories.
    """
    if not endpoint.code_modifying:
        return []
    if endpoint.modifies_category:
        return endpoint.state.reset_category(endpoint.modifies_category)
    categories = endpoint.classify_git_diff()
    reset_keys: list[str] = []
    for cat in categories:
        reset_keys.extend(endpoint.state.reset_category(cat))
    return reset_keys


def _default_target_args(endpoint: EndpointState) -> None:
    """Default well-known CLI arguments from the selected Target."""
    if endpoint._state is None:
        return
    if hasattr(endpoint.args, "tb_top") and not getattr(endpoint.args, "tb_top", None):
        target = getattr(endpoint.args, "target", "")
        if target:
            from booley.flows.flow_config import tb_top_for_target

            tb_top = tb_top_for_target(
                target,
                getattr(endpoint.args, "work_dir", None),
                resolved=None,
            )
            if tb_top:
                endpoint.args.tb_top = tb_top


def _requested_targets(endpoint: EndpointState) -> list[str]:
    """Return normalized Target tokens without resolving or invoking EDA."""
    raw = getattr(endpoint.args, "target", "")
    values = raw if isinstance(raw, list) else [raw]
    targets: list[str] = []
    for value in values:
        targets.extend(part.strip() for part in str(value or "").split(",") if part.strip())
    return targets


def _bound_criterion_keys(endpoint: EndpointState, target: str) -> list[str]:
    """Return basis-bound criteria this endpoint/Target invocation can update."""
    criterion_target = target
    target_identity: str | None = None
    try:
        from booley.targets.catalog import TargetCatalog

        selected = TargetCatalog.build(Path(endpoint.args.work_dir)).select(target)
        criterion_target = selected.name
        target_identity = selected.identity
    except FuseSocError:
        pass
    selector = getattr(endpoint.args, "test", None)
    detail = {
        "test_selector": selector or "all",
        "selected_tests": [selector] if selector else [],
    }
    bound: list[str] = []
    for family in endpoint.satisfies:
        generic_key = f"{family}_{criterion_target}"
        if generic_key in endpoint.state.criteria:
            bound.append(generic_key)
            continue
        for alias in endpoint.state.flow_key_aliases.get(generic_key, []):
            if alias in endpoint.state.criteria and endpoint.state._alias_matches_run(
                alias, detail
            ):
                bound.append(alias)
        if family in endpoint.state.criteria:
            bound.append(family)
        bound.extend(
            key
            for key, entry in endpoint.state.criteria.items()
            if key.startswith(f"{family}_")
            and isinstance(entry.params, dict)
            and endpoint._criterion_target_matches(
                entry.params,
                target,
                target_identity,
            )
            and key not in bound
        )
    return bound


def _criterion_target_matches(
    endpoint: EndpointState,
    params: dict[str, Any],
    invoked: str,
    invoked_identity: str | None,
) -> bool:
    """Compare criterion and invocation Targets by identity when resolvable."""
    from booley.targets.domain import (
        TARGET_IDENTITY_PARAM,
        TARGET_SELECTOR_PARAM,
        criterion_matches_target,
    )

    authored = params.get(TARGET_IDENTITY_PARAM)
    if not isinstance(authored, str):
        return False
    if TARGET_SELECTOR_PARAM in params:
        return invoked_identity is not None and criterion_matches_target(
            params,
            identity=invoked_identity,
            selector=invoked,
        )
    if authored == invoked:
        return True
    if invoked_identity is None:
        return False
    try:
        from booley.targets.catalog import TargetCatalog

        return (
            TargetCatalog.build(Path(endpoint.args.work_dir)).select(authored).identity
            == invoked_identity
        )
    except FuseSocError:
        return False


def _criterion_binding_gate(endpoint: EndpointState) -> EndpointOutcome | None:
    """Reject an unbound Ticket-mode Target before job admission/EDA."""
    # Explicit native collection also supports ungated Targets (#213). Coverage
    # preflight has already validated the complete selection before admission.
    if endpoint.name == "sim" and getattr(endpoint.args, "coverage", False):
        return None
    if (
        not endpoint.state.strict_criteria
        or not endpoint.satisfies
        or getattr(endpoint.args, "diagnostic", False)
    ):
        return None
    targets = endpoint._requested_targets()
    if not targets:
        return None
    missing = [target for target in targets if not endpoint._bound_criterion_keys(target)]
    if not missing:
        return None

    from booley.criteria.actions import planned_invocation

    pending: list[str] = []
    for key, entry in endpoint.state.criteria.items():
        if key.startswith("_") or not any(
            key == family or key.startswith(f"{family}_") for family in endpoint.satisfies
        ):
            continue
        invocation = planned_invocation(key, entry)
        pending.append(f"  {key} -> {invocation or endpoint.name}")
    pending_text = "\n".join(pending) if pending else "  (no compatible criterion declared)"
    return EndpointOutcome(
        exit_code=EXIT_ERROR,
        detail={
            "acceptance_effect": "rejected_unbound",
            "unbound_targets": missing,
        },
        report_text=(
            f"{endpoint.name}: Target(s) {', '.join(missing)} do not bind an Acceptance "
            f"Basis criterion.\nPending compatible criteria:\n{pending_text}\n"
            "Use --diagnostic only when this is intentionally a non-acceptance run."
        ),
    )


def record_acceptance(
    endpoint: EndpointState,
    prepared: PreparedExecution,
    outcome: EndpointOutcome,
) -> None:
    """Record immutable Ticket evidence before state/report persistence."""
    if prepared.non_persisting_dry_run:
        endpoint._pending_criteria_set = ()
        return
    result = endpoint._adapt_outcome(outcome)
    if endpoint._state is not None and endpoint._state._file_path is not None:
        endpoint.state.work_dir = str(Path(endpoint.args.work_dir).resolve())
        endpoint._pre_save_hook(result)
    reset_keys: list[str] = []
    if result.exit_code == EXIT_SUCCESS and endpoint.code_modifying:
        reset_keys = endpoint.invalidate_dependent_criteria()
        endpoint._record_acceptance_changes(
            [
                CriterionChange(
                    key,
                    endpoint.state.criteria[key].met,
                    "source-invalidated",
                    dict(endpoint.state.criteria[key].detail),
                    endpoint.state.criteria[key].mandatory,
                    dict(endpoint.state.criteria[key].params),
                )
                for key in reset_keys
            ]
        )
    criteria_set = [result.criterion_key] if result.criterion_key else []
    criteria_set.extend(f"~{key}" for key in reset_keys)
    endpoint._pending_criteria_set = tuple(criteria_set)
