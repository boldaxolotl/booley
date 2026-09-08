"""Ticket evidence publication for already durable Coverage Campaigns."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from booley.criteria.categories import verification_fingerprint_categories
from booley.criteria.state import SOURCE_FINGERPRINT_DETAIL_KEY, DevelopmentState
from booley.flows.criterion_freshness import build_criterion_freshness
from booley.ticket_board.acceptance_ledger import record_changes

if TYPE_CHECKING:
    from .coverage_campaign import CoverageCampaign
    from .coverage_invocation import CoverageTargetPlan


@dataclass(frozen=True)
class CoverageAcceptance:
    """Explicit Ticket publication destination; absent for Interactive Mode."""

    state: DevelopmentState
    logs_dir: Path | None = None
    execution_id: str = ""
    acceptance_basis: dict[str, Any] = field(default_factory=dict)

    def publish(self, plan: CoverageTargetPlan, campaign: CoverageCampaign, path: Path) -> None:
        """Append normalized evidence before committing the mutable state projection."""
        shadow = deepcopy(self.state)
        shadow.work_dir = str(plan.handle.project_root)
        changes = []
        common = _simulation_detail(plan, campaign, path)
        if plan.criterion is not None:
            detail = {
                **common,
                "evaluation": dict(campaign.evaluation),
                "criterion_fingerprint": campaign.evaluation.get("criterion_fingerprint"),
            }
            # Encode nested immutable mappings before the state/ledger JSON boundary.
            from .coverage_campaign import encode_coverage_campaign

            detail["evaluation"] = encode_coverage_campaign(campaign)["evaluation"]
            detail[SOURCE_FINGERPRINT_DETAIL_KEY] = _freshness(plan, plan.criterion_key)
            changes.extend(
                shadow.set_criterion(
                    plan.criterion_key, campaign.evaluation["status"] == "pass", detail=detail
                )
            )
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
        if not changes:
            return
        if self.logs_dir is not None and shadow.strict_criteria:
            record_changes(
                self.logs_dir,
                shadow,
                changes,
                invocation_id=campaign.campaign_id,
                producer="sim",
                execution_id=self.execution_id,
                acceptance_basis=self.acceptance_basis,
            )
        shadow.save()
        self.state.work_dir = shadow.work_dir
        self.state.criteria = shadow.criteria
        self.state.last_updated = shadow.last_updated


def _simulation_detail(
    plan: CoverageTargetPlan, campaign: CoverageCampaign, path: Path
) -> dict[str, Any]:
    selected = list(plan.selected_tests)
    return {
        "coverage_campaign": str(path),
        "simulation_report": str(path.with_name("simulation.json")),
        "target": plan.handle.identity,
        "selected_tests": selected,
        "registry_tests": list(plan.declared_tests),
        "test_selector": "all" if plan.selected_tests == plan.declared_tests else "partial",
        "passed_tests": [run.test for run in campaign.runs if run.simulation_verdict == "pass"],
        "failed_tests": [run.test for run in campaign.runs if run.simulation_verdict != "pass"],
        "tests_passed": sum(run.simulation_verdict == "pass" for run in campaign.runs),
        "tests_total": len(campaign.runs),
    }


def _freshness(plan: CoverageTargetPlan, key: str) -> dict[str, Any]:
    return build_criterion_freshness(
        plan.handle.project_root,
        target=plan.handle.selector,
        categories=tuple(verification_fingerprint_categories(key)),
    ).to_detail()
