"""Re-derive every recovered approval output from reviewed inputs and decisions."""

from __future__ import annotations

import json
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

from booley.core.boundary import require_dict, require_list, require_str
from booley.criteria.state import DevelopmentState
from booley.flows.sim.coverage_waiver_application import WaiverPromotionPlan
from booley.runtime.timefmt import parse_timestamp, rfc3339_from_datetime

from . import review_preparation as prep
from . import waiver_candidates as store
from .board_layout import waiver_candidates_path
from .provisional_coverage import TicketCoverageContext, describe_waiver_candidates
from .review_records import ReviewEntryError
from .waiver_approval import (
    WaiverDecisions,
    _ApprovalInputs,
    _publish,
    _strict_changes,
    _unmet_mandatory,
    _validate_decisions,
    _validate_resumed_plan,
    approver_identity,
    promotion_plan_path,
)

if TYPE_CHECKING:
    from .waiver_approval_transaction import _Patch


def _decisions(record: Mapping[str, Any]) -> WaiverDecisions:
    answers = require_dict(record.get("answers"), field="waiver answers")
    accepted = require_list(answers.get("accepted"), field="accepted candidates")
    rejected = require_list(answers.get("rejected"), field="rejected candidates")
    if not (accepted or rejected) or any(
        not isinstance(item, str) for item in accepted + rejected
    ):
        raise ReviewEntryError("waiver approval outputs require explicit decisions")
    reference = answers.get("approval_ref")
    if reference is not None:
        reference = require_str(answers, "approval_ref")
    return WaiverDecisions(frozenset(accepted), frozenset(rejected), reference)


def _outputs(staging: Path, slug: str) -> dict[str, bytes]:
    outputs = {
        path.relative_to(staging / "log").as_posix(): path.read_bytes()
        for path in sorted((staging / "log").rglob("*"))
        if path.is_file()
    }
    candidate = waiver_candidates_path(staging / "tickets", slug)
    if candidate.is_file():
        outputs["waiver-candidates"] = candidate.read_bytes()
    return outputs


def _restore_inputs(ctx: prep.ReviewPrepContext, staging: Path, patches: list[_Patch]) -> None:
    from .waiver_approval_transaction import _copy_inputs

    _copy_inputs(ctx, staging)
    for patch in patches:
        path = (
            waiver_candidates_path(staging / "tickets", ctx.slug)
            if patch.label == "waiver-candidates"
            else staging / "log" / patch.label
        )
        if patch.before is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(patch.before)
        elif path.exists():
            path.unlink()
            parent = path.parent
            while parent != staging and parent.is_dir() and not any(parent.iterdir()):
                parent.rmdir()
                parent = parent.parent


def _reconstruct_rejections(
    inputs: _ApprovalInputs, views, decisions, patches: list[_Patch]
) -> None:
    candidate = next((p for p in patches if p.label == "waiver-candidates"), None)
    if not decisions.rejected:
        return
    if candidate is None:
        raise ReviewEntryError("waiver approval outputs omit explicit rejections")
    observed_root = inputs.log_dir.parent / "observed"
    path = waiver_candidates_path(observed_root, inputs.slug)
    path.parent.mkdir(parents=True)
    path.write_bytes(candidate.after)
    observed = {item.key: item for item in store.load(observed_root, inputs.slug).rejections}
    approver = approver_identity(inputs.project_root)
    expected = []
    for view in views:
        item = view.candidate
        if item.candidate_id not in decisions.rejected:
            continue
        key = (item.binding.target_identity, item.proposal.point_id, item.proposal.source_sha256)
        if key not in observed or observed[key].rejected_by != approver:
            raise ReviewEntryError("waiver approval outputs disagree with explicit rejections")
        expected.append(store.Rejection(*key, observed[key].rejected_at, approver))
    store.record_rejections(inputs.context.tickets_dir, inputs.slug, expected)


def _derive_outputs(inputs: _ApprovalInputs, record, patches: list[_Patch]) -> dict[str, int]:
    decisions = _decisions(record)
    state = DevelopmentState.load(inputs.state_path)
    prior_evidence = {key: len(entry.transition_evidence) for key, entry in state.criteria.items()}
    views, _ = describe_waiver_candidates(state.criteria, inputs.context)
    _validate_decisions(decisions, views)
    plan_patch = next((p for p in patches if p.label == "acceptance/waiver-promotion.json"), None)
    if plan_patch is None:
        if not record.get("error") or any(p.label != "waiver-candidates" for p in patches):
            raise ReviewEntryError(
                "waiver approval outputs without a plan must be rejections only"
            )
        _reconstruct_rejections(inputs, views, decisions, patches)
        return {}
    if record.get("error"):
        raise ReviewEntryError("waiver approval outputs mix promotion with a prediction error")
    plan = WaiverPromotionPlan.from_json(json.loads(plan_patch.after))
    _validate_resumed_plan(inputs, plan, decisions)
    _reconstruct_rejections(inputs, views, decisions, patches)
    changes = _strict_changes(inputs, state, plan)
    if _unmet_mandatory(state):
        raise ReviewEntryError("waiver approval outputs do not meet strict mandatory Criteria")
    path = promotion_plan_path(inputs.log_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(plan.to_bytes())
    _publish(inputs, state, changes, plan.sha256())
    return {change.key: prior_evidence[change.key] for change in changes}


def _discard_audit_timestamp(document: dict[str, Any], field: str) -> None:
    value = require_str(document, field)
    if value != rfc3339_from_datetime(parse_timestamp(value)):
        raise ReviewEntryError("waiver approval outputs contain an invalid audit timestamp")
    document.pop(field)


def _semantic(content: bytes, label: str, changed: dict[str, int]) -> Any:
    """Ignore only publication audit times; retain all verdicts and unrelated state."""
    if label == ".runtime/booley_state.json":
        state = json.loads(content)
        _discard_audit_timestamp(state, "last_updated")
        for key, prior_count in changed.items():
            entry = state["criteria"][key]
            _discard_audit_timestamp(entry, "updated_at")
            for evidence in entry.get("transition_evidence", [])[prior_count:]:
                _discard_audit_timestamp(evidence, "recorded_at")
        return state
    if label.endswith("/record.json"):
        record = json.loads(content)
        _discard_audit_timestamp(record, "recorded_at")
        return record
    return content


def validate_transaction_outputs(
    ctx: prep.ReviewPrepContext, record: Mapping[str, Any], patches: list[_Patch]
) -> None:
    """Check recovered state, evidence, plan, and rejections before accepting drift."""
    assert ctx.inspection is not None
    with tempfile.TemporaryDirectory(prefix="booley-waiver-validation-") as directory:
        staging = Path(directory)
        _restore_inputs(ctx, staging, patches)
        before = _outputs(staging, ctx.slug)
        inputs = _ApprovalInputs(
            ctx.slug,
            staging / "log",
            staging / "log" / ".runtime" / "booley_state.json",
            ctx.worktree,
            ctx.project_root,
            TicketCoverageContext(ctx.slug, staging / "tickets", ctx.log_dir, ctx.worktree),
            ctx.inspection,
        )
        changed = _derive_outputs(inputs, record, patches)
        after = _outputs(staging, ctx.slug)
    expected = {label: content for label, content in after.items() if before.get(label) != content}
    actual = {patch.label: patch.after for patch in patches}
    if expected.keys() != actual.keys() or any(
        _semantic(expected[label], label, changed) != _semantic(actual[label], label, changed)
        for label in expected
    ):
        raise ReviewEntryError("waiver approval outputs disagree with the derived projection")
