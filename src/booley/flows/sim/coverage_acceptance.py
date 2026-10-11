"""Ticket evidence publication for already durable Coverage Campaigns."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Sequence
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from booley.criteria.categories import verification_fingerprint_categories
from booley.criteria.state import CriterionChange, CriterionEntry, DevelopmentState
from booley.evidence.fields import SOURCE_FINGERPRINT_DETAIL_KEY
from booley.flows.criterion_freshness import build_criterion_freshness
from booley.flows.execution_persistence import AcceptanceRecorder, EvidenceDiscarded
from booley.flows.test_runs import TestRun, build_test_run, test_run_document

from .coverage_projection import project_coverage_criterion

if TYPE_CHECKING:
    from .coverage_campaign import CoverageCampaign, CoverageRun
    from .coverage_invocation import CoverageTargetPlan


#: The fields a published shadow hands back to the live state.
_COPIED_BACK = ("work_dir", "criteria", "acceptance_transactions", "last_updated")


@dataclass(frozen=True)
class CoverageAcceptance:
    """Explicit Ticket publication destination; absent for Interactive Mode."""

    state: DevelopmentState
    recorder: AcceptanceRecorder = field(compare=False)
    observer: Callable[[list[CriterionChange]], None] | None = field(default=None, compare=False)
    # Told when the recorder discards the evidence (B1); without it the error propagates.
    on_discard: Callable[[EvidenceDiscarded], None] | None = field(default=None, compare=False)

    def publish(self, plan: CoverageTargetPlan, campaign: CoverageCampaign, path: Path) -> None:
        """Append normalized evidence before committing the mutable state projection."""
        shadow = deepcopy(self.state)
        shadow.work_dir = str(plan.handle.project_root)
        changes = _apply_campaign(shadow, plan, campaign, path)
        if self.observer is not None:
            self.observer(changes)
        if not changes:
            return
        try:
            self._record(shadow, campaign, changes)
            shadow.save()
        except EvidenceDiscarded as exc:
            if self.on_discard is None:
                raise
            self.on_discard(exc)
            return
        self.state.take_saved(shadow, _COPIED_BACK)

    def _record(
        self, shadow: DevelopmentState, campaign: CoverageCampaign, changes: list[CriterionChange]
    ) -> None:
        if not shadow.strict_criteria:
            return
        transaction = hashlib.sha256(campaign.campaign_id.encode()).hexdigest()
        self.recorder.record_changes(
            shadow,
            changes,
            invocation_id=campaign.campaign_id,
            producer="sim",
            transaction_id=transaction,
        )
        if transaction not in shadow.acceptance_transactions:
            shadow.acceptance_transactions.append(transaction)


def _apply_campaign(
    shadow: DevelopmentState, plan: CoverageTargetPlan, campaign: CoverageCampaign, path: Path
) -> list[CriterionChange]:
    common = _simulation_detail(plan, campaign, path)
    changes = _apply_coverage_criterion(shadow, plan, campaign, common)
    if all(run.simulation_verdict != "inconclusive" for run in campaign.runs):
        key = f"sim_pass_{plan.handle.name}"
        if key in shadow.criteria or key in shadow.flow_key_aliases:
            changes.extend(
                shadow.set_criterion(
                    key,
                    all(run.simulation_verdict == "pass" for run in campaign.runs),
                    detail={**common, SOURCE_FINGERPRINT_DETAIL_KEY: _freshness(plan, key)},
                )
            )
    return changes


def _apply_coverage_criterion(
    shadow: DevelopmentState,
    plan: CoverageTargetPlan,
    campaign: CoverageCampaign,
    common: dict[str, Any],
) -> list[CriterionChange]:
    if plan.criterion is None:
        return []
    from .coverage_campaign import encode_coverage_campaign

    detail = {
        **common,
        "evaluation": encode_coverage_campaign(campaign)["evaluation"],
        "criterion_fingerprint": campaign.evaluation.get("criterion_fingerprint"),
        SOURCE_FINGERPRINT_DETAIL_KEY: _freshness(plan, plan.criterion_key),
    }
    aliases = shadow.flow_key_aliases.get(plan.criterion_key, [])
    if not aliases:
        met, projected_detail = project_coverage_criterion(
            shadow.criteria.get(plan.criterion_key, CriterionEntry()),
            detail["evaluation"],
            detail,
            atomic=False,
        )
        return shadow.set_criterion(
            plan.criterion_key,
            met,
            detail=projected_detail,
        )
    return _apply_coverage_aliases(shadow, aliases, detail)


def _apply_coverage_aliases(
    shadow: DevelopmentState, aliases: list[str], detail: dict[str, Any]
) -> list[CriterionChange]:
    evaluation = detail["evaluation"]
    if not isinstance(evaluation, dict):
        raise TypeError("Coverage Campaign evaluation is not a mapping")
    changes: list[CriterionChange] = []
    for key in aliases:
        entry = shadow.criteria.get(key)
        if entry is None:
            continue
        met, projected_detail = project_coverage_criterion(entry, evaluation, detail, atomic=True)
        changes.extend(
            shadow.set_criterion(
                key,
                met,
                detail=projected_detail,
            )
        )
    return changes


def coverage_test_run_rows(
    test_runs: Sequence[TestRun], runs: Sequence[CoverageRun]
) -> list[dict[str, object]]:
    """One row per collected run: the Test Run it is, and how it ended.

    *test_runs* are the Test Runs the invocation froze before collection; a
    row takes its Test Variant, seed, arguments and environment from there,
    never from the current ``tests.toml``. A run collected without frozen
    Test Runs is the bare Test with no declared inputs. A run whose recorded
    fingerprint is not the frozen Test Run's has no row.
    """
    frozen = {run.name: run for run in test_runs}
    rows: list[dict[str, object]] = []
    for run in runs:
        test_run = frozen.get(run.test_run) or _bare_test_run(run)
        if test_run is None or test_run.fingerprint != run.fingerprint:
            continue
        rows.append({**test_run_document(test_run), "grade": _run_grade(run)})
    return rows


def _bare_test_run(run: CoverageRun) -> TestRun | None:
    """The Test Run of a run that names only its Test, when that is a Test name."""
    if run.test_run != run.test:
        return None
    try:
        return build_test_run(run.test)
    except ValueError:
        return None


def _run_grade(run: CoverageRun) -> str:
    """One word for how a run ended, as a Simulation Campaign grades it."""
    if run.simulation_verdict in {"pass", "inconclusive"}:
        return run.simulation_verdict
    return "error" if run.attributes.get("failure_kind") == "infrastructure" else "fail"


def _simulation_detail(
    plan: CoverageTargetPlan, campaign: CoverageCampaign, path: Path
) -> dict[str, Any]:
    del path
    selected = list(plan.selected_tests)
    # Evidence names Tests. A Test passed only when every one of its Test Runs
    # passed, so one passing run never stands in for a failing run of the Test.
    failed = list(
        dict.fromkeys(run.test for run in campaign.runs if run.simulation_verdict != "pass")
    )
    passed = [
        test for test in dict.fromkeys(run.test for run in campaign.runs) if test not in failed
    ]
    return {
        "campaign_id": getattr(campaign, "campaign_id", "unavailable"),
        "target": plan.handle.identity,
        "selected_tests": selected,
        "registry_tests": list(plan.declared_tests),
        "test_selector": "all" if plan.selected_tests == plan.declared_tests else "partial",
        "passed_tests": passed,
        "failed_tests": failed,
        "tests_passed": len(passed),
        "tests_total": len(passed) + len(failed),
        "test_runs": coverage_test_run_rows(plan.test_runs, campaign.runs),
    }


def _freshness(plan: CoverageTargetPlan, key: str) -> dict[str, Any]:
    return build_criterion_freshness(
        plan.handle.project_root,
        target=plan.handle.selector,
        categories=tuple(verification_fingerprint_categories(key)),
    ).to_detail()
