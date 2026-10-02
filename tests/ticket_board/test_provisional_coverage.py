"""A strictly failing Coverage Criterion can be met provisionally (ADR 0066).

End to end through a real Simulation Campaign: the mandatory 70% line Criterion
observes 1/2 points, and one recorded Waiver Candidate for the uncovered point
makes it 1/1 provisionally. The candidate record is checked, not trusted.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from booley.criteria.state import CriterionEntry, DevelopmentState
from booley.flows.sim.coverage_reference import resolve_persisted_coverage_campaign_reference
from booley.flows.sim.flow import SimulateFlow
from booley.flows.sim.request import SimRequest
from booley.ticket_board import waiver_candidates as store
from booley.ticket_board.criteria_acceptance import check_criteria_acceptance
from booley.ticket_board.provisional_coverage import (
    TicketCoverageContext,
    evaluate_ticket_provisional_coverage,
)
from tests.flows.sim.test_coverage_flow import (
    _AcceptanceAdapter,
    _prepare_atomic_coverage_ticket,
    _SplitMetricsExecution,
)

_NOW = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)


@pytest.mark.parametrize("case", ["current", "missing_context", "stale", "other_unmet"])
def test_report_gate_allows_only_verified_provisional_coverage(ticket, monkeypatch, case):
    from booley.mcp.submit_run_report import SubmitRunReportMcpTool
    from booley.ticket_board.helpers import tickets_dir_from_project_root

    state_path, context = ticket
    monkeypatch.setenv("BOOLEY_CONTROL_PROJECT_ROOT", str(context.worktree))
    monkeypatch.setenv("BOOLEY_RUNTIME_DIR", str(context.log_dir / ".runtime"))
    monkeypatch.setenv("BOOLEY_SLUG", context.slug)
    context = replace(context, tickets_dir=tickets_dir_from_project_root(context.worktree))
    _record_uncovered_line(context, state_path)
    state = DevelopmentState.load(state_path)
    state.set_criterion("_report_submitted", False)
    if case == "missing_context":
        monkeypatch.delenv("BOOLEY_CONTROL_PROJECT_ROOT")
    elif case == "stale":
        (context.worktree / "rtl" / "counter.sv").write_bytes(b"module changed; endmodule\n")
    elif case == "other_unmet":
        state.criteria["implementation_done"] = CriterionEntry(met=False, mandatory=True)
    endpoint = SubmitRunReportMcpTool()
    endpoint.parse_args(
        [
            "--work-dir",
            str(context.worktree),
            "--summary",
            "Coverage needs human review",
            "--uncertainties",
            "Candidate justification needs approval",
            "--design-decisions",
            "Kept unreachable logic",
            "--file-justifications",
            "{}",
        ]
    )
    endpoint._state = state

    result = endpoint._criteria_freshness_gate()

    assert (result is None) == (case == "current")
    assert not state.criteria[_metric_key(state, "line")].met


class _LineGapExecution(_SplitMetricsExecution):
    """Line 1/2 (fails the mandatory 70%), expression 1/1, branch 0/1 (optional)."""

    def payload(self, _hits):
        records = (
            ("line", 1, "block", 1),
            ("line", 4, "block", 0),
            ("expr", 2, "true", 1),
            ("branch", 3, "true", 0),
        )
        body = "".join(
            "C '\x01f\x02rtl/counter.sv"
            f"\x01l\x02{line}\x01n\x021\x01h\x02TOP.counter"
            f"\x01t\x02{metric}\x01o\x02{outcome}' {hits}\n"
            for metric, line, outcome, hits in records
        )
        return "# SystemC::Coverage-3\n" + body


def _metric_key(state: DevelopmentState, metric: str) -> str:
    return next(
        key
        for key, entry in state.criteria.items()
        if key.startswith("coverage_") and metric in entry.params.get("metrics", {})
    )


@pytest.fixture
def ticket(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    state_path, _initial = _prepare_atomic_coverage_ticket(tmp_path, monkeypatch)
    log_dir = tmp_path / "logs"
    adapter = _AcceptanceAdapter(
        log_dir=log_dir, ticket_identity={"generation": "d" * 32, "authored_sha256": "e" * 64}
    )
    flow = SimulateFlow(
        coverage_execution=lambda _handle, _options, _commands, _access: _LineGapExecution()
    )
    result = flow.execute(
        SimRequest(
            target="sim_custom",
            work_dir=tmp_path,
            coverage=True,
            report_dir=log_dir / ".runtime" / "flow-reports",
        ),
        adapter=adapter,
    )
    assert result.exit_code == 0  # a threshold miss is recorded, not an execution failure
    state = DevelopmentState.load(state_path)
    state.set_criterion(
        "_report_submitted",
        True,
        detail={"unmet_optional_criteria": [_metric_key(state, "branch")]},
    )
    state.save()
    context = TicketCoverageContext("ticket", tmp_path / "tickets", log_dir, tmp_path)
    return state_path, context


def _line_reference(state_path: Path) -> dict:
    state = DevelopmentState.load(state_path)
    entry = state.criteria[_metric_key(state, "line")]
    assert entry.met is False and entry.mandatory is True
    return entry.detail["coverage_campaign_reference"]


def _record_uncovered_line(
    context: TicketCoverageContext, state_path: Path, *, metric: str = "line", **overrides
) -> None:
    reference = _line_reference(state_path)
    resolved = resolve_persisted_coverage_campaign_reference(context.flow_reports_root, reference)
    campaign = resolved.loaded.campaign
    summary = resolved.loaded.summary
    point = next(
        item
        for item in campaign.points
        if item.identity.metric == metric and not sum(item.hits_by_run.values())
    )
    source = str(point.identity.location["source"])
    sha = {row["path"]: row["sha256"] for row in campaign.source_closure["rtl"]}[source]
    binding = {
        "campaign_id": campaign.campaign_id,
        "manifest_sha256": summary.manifest_sha256,
        "point_store_sha256": summary.point_store.sha256,
        "campaign_path": reference["path"],
        "target_identity": campaign.target.identity,
        "target_selector": campaign.target.selector,
    }
    binding.update(overrides)
    store.record_proposals(
        context.tickets_dir,
        context.slug,
        store.CampaignBinding(**binding),
        [store.WaiverProposal(point.id, source, sha, "unreachable", "Tied-off reset branch")],
        invocation_id="1",
        now=_NOW,
    )


def _verdict(state_path: Path, context: TicketCoverageContext):
    def judge(state, unmet):
        return evaluate_ticket_provisional_coverage(state, unmet, context).met_keys

    return check_criteria_acceptance(
        state_path,
        work_dir=context.worktree,
        provisional=judge,
        log_dir=context.log_dir,
        ticket_identity={"generation": "d" * 32, "authored_sha256": "e" * 64},
    )


def test_strict_failure_without_candidates_stays_failed(ticket) -> None:
    state_path, context = ticket

    verdict = _verdict(state_path, context)

    assert verdict.disposition == "failed"
    assert verdict.provisional == ()


def test_recorded_candidate_makes_the_ticket_provisionally_reviewable(ticket) -> None:
    state_path, context = ticket
    _record_uncovered_line(context, state_path)

    verdict = _verdict(state_path, context)

    state = DevelopmentState.load(state_path)
    assert verdict.disposition == "review"
    assert verdict.provisional == (_metric_key(state, "line"),)
    # Strict acceptance is unchanged: the Criterion itself is still unmet.
    assert check_criteria_acceptance(state_path, work_dir=context.worktree).disposition == "failed"
    judged = evaluate_ticket_provisional_coverage(state, [_metric_key(state, "line")], context)
    (criterion,) = judged.criteria
    assert criterion.verdict.strict_status == "fail"
    assert [s.needed for s in criterion.verdict.screens] == [True]
    assert judged.record_sha256.startswith("sha256:")


def test_changed_rtl_source_in_the_worktree_makes_the_candidate_stale(ticket) -> None:
    state_path, context = ticket
    _record_uncovered_line(context, state_path)
    (context.worktree / "rtl/counter.sv").write_text("module counter; wire x; endmodule\n")

    assert _verdict(state_path, context).disposition == "failed"


@pytest.mark.parametrize(
    "override",
    [
        {"campaign_path": "sim/99/targets/sim_custom/coverage.json"},
        {"manifest_sha256": "sha256:" + "0" * 64},
        {"campaign_id": "campaign:forged"},
    ],
)
def test_candidates_bound_to_another_campaign_do_not_count(ticket, override) -> None:
    state_path, context = ticket
    _record_uncovered_line(context, state_path, **override)

    assert _verdict(state_path, context).disposition == "failed"


def test_corrupt_candidate_record_fails_closed(ticket) -> None:
    state_path, context = ticket
    path = context.tickets_dir / "waiver-candidates" / "ticket.json"
    path.parent.mkdir(parents=True)
    path.write_text("{", encoding="utf-8")

    assert _verdict(state_path, context).disposition == "failed"


def test_tampered_campaign_reference_fails_closed(ticket) -> None:
    state_path, context = ticket
    _record_uncovered_line(context, state_path)
    public = context.flow_reports_root / _line_reference(state_path)["path"]
    document = json.loads(public.read_text())
    document["tampered"] = True
    public.chmod(0o600)
    public.write_text(json.dumps(document))

    assert _verdict(state_path, context).disposition == "failed"


def test_declared_block_wins_over_a_provisional_verdict(ticket) -> None:
    state_path, context = ticket
    _record_uncovered_line(context, state_path)
    state = DevelopmentState.load(state_path)
    state.criteria["_blocked_reason"] = CriterionEntry(
        met=True, mandatory=False, detail={"reason": "needs a spec answer"}
    )
    state.save()

    verdict = _verdict(state_path, context)

    assert verdict.disposition == "blocked"


def test_review_views_list_offered_and_stale_candidates(ticket) -> None:
    from booley.ticket_board.provisional_coverage import describe_waiver_candidates

    state_path, context = ticket
    _record_uncovered_line(context, state_path)
    _record_uncovered_line(
        context,
        state_path,
        metric="branch",
        campaign_path="sim/99/targets/sim_custom/coverage.json",
    )
    state = DevelopmentState.load(state_path)

    views, digest = describe_waiver_candidates(state.criteria, context)

    by_status = {view.status: view for view in views}
    assert set(by_status) == {"offered", "stale"}, [(v.status, v.reason) for v in views]
    offered = by_status["offered"]
    assert offered.criteria == (_metric_key(state, "line"),)  # not the expression Criterion
    assert offered.needed is True
    (delta,) = offered.metrics
    assert delta["metric"] == "line"
    assert (delta["strict_percent"], delta["provisional_percent"]) == (50.0, 100.0)
    assert "no longer a Criterion's evidence" in by_status["stale"].reason
    assert digest.startswith("sha256:")


def test_review_views_of_an_empty_record_are_empty(ticket) -> None:
    from booley.ticket_board.provisional_coverage import describe_waiver_candidates

    state_path, context = ticket
    state = DevelopmentState.load(state_path)

    assert describe_waiver_candidates(state.criteria, context)[0] == ()
