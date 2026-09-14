"""Atomic Ticket Criteria bind to existing Flow result keys."""

from __future__ import annotations

from booley.criteria.state import DevelopmentState
from booley.criteria.ticket_projection import project_ticket_criteria
from booley.flows.sim import coverage_acceptance, coverage_flow_context
from booley.ticket_board.ticket_document import (
    TicketAuthoringView,
    TicketConversionContext,
    convert_ticket_document,
)


def _spec():
    text = (
        "---\nsummary: Check core\ntype: feature\nbranch: main\nscope: [rtl/core.sv]\n"
        "on_success: [merge]\nCRITERIA_MANDATORY:\n"
        "  SYNTH: {synth_core: {area_um2_max: 10000, fmax_mhz_min: 400}}\n"
        "  SIM: {sim_core: {smoke: fail -> pass}}\n"
        "---\n\n## Description\n\nCheck core.\n"
    )
    view = TicketAuthoringView(lambda selector, _flow: selector, lambda _target: ("smoke",))
    converted = convert_ticket_document(
        text, TicketConversionContext("draft", lambda _generated: view)
    )
    assert converted.document is not None, converted.diagnostics
    return converted.document.spec


def test_one_synthesis_run_updates_independent_atomic_thresholds() -> None:
    projection = project_ticket_criteria(_spec())
    state = DevelopmentState()
    state.init_criteria(
        projection.required,
        flow_key_aliases=projection.aliases,
        criterion_params=projection.params,
        strict=True,
    )
    changes = state.set_criterion(
        "synthesis_ok_synth_core",
        True,
        detail={"area_um2": 9000, "fmax_mhz": 350},
    )
    assert len(changes) == 2
    assert {change.met for change in changes} == {True, False}
    assert not state.all_mandatory_met()


def test_named_sim_transition_is_bound_to_its_test() -> None:
    projection = project_ticket_criteria(_spec())
    key = next(key for key in projection.required if key.startswith("sim_pass_"))
    assert projection.params[key]["required_tests"] == ["smoke"]
    assert projection.params[key]["from_state"] == "fail"
    state = DevelopmentState()
    state.init_criteria(
        projection.required,
        flow_key_aliases=projection.aliases,
        criterion_params=projection.params,
        strict=True,
    )
    assert (
        state.set_criterion(
            "sim_pass_sim_core",
            True,
            detail={"test_selector": "other", "selected_tests": ["other"]},
        )
        == []
    )


def test_coverage_metrics_share_one_campaign_but_keep_separate_verdicts(
    monkeypatch,
    tmp_path,
) -> None:
    from types import SimpleNamespace

    text = (
        "---\nsummary: Check coverage\ntype: verification\nbranch: main\nscope: [rtl/core.sv]\n"
        "on_success: []\nCRITERIA_MANDATORY:\n"
        "  COVERAGE:\n    sim_core:\n      tests: all\n"
        "      metrics: {line: {min_pct: 90}, branch: {min_pct: 80}}\n"
        "---\n\n## Description\n\nCheck coverage.\n"
    )
    view = TicketAuthoringView(lambda selector, _flow: selector, lambda _target: ("smoke",))
    converted = convert_ticket_document(
        text, TicketConversionContext("draft", lambda _generated: view)
    )
    assert converted.document is not None, converted.diagnostics
    projection = project_ticket_criteria(converted.document.spec)
    state = DevelopmentState()
    state.init_criteria(
        projection.required,
        flow_key_aliases=projection.aliases,
        criterion_params=projection.params,
        strict=True,
    )

    class Catalog:
        def select(self, _selector, *, for_flow):
            assert for_flow == "sim"
            return SimpleNamespace(identity="sim_core", name="sim_core", selector="sim_core")

    monkeypatch.setattr(coverage_flow_context.TargetCatalog, "build", lambda _root: Catalog())
    policies = coverage_flow_context._coverage_policies(tmp_path, state)
    assert list(policies) == ["coverage_sim_core"]
    assert {item.metric for item in policies["coverage_sim_core"].thresholds} == {"line", "branch"}

    monkeypatch.setattr(coverage_acceptance, "_freshness", lambda *_args: {})
    from booley.flows.sim import coverage_campaign

    monkeypatch.setattr(
        coverage_campaign,
        "encode_coverage_campaign",
        lambda _campaign: {
            "evaluation": {
                "status": "fail",
                "suite": {"status": "match"},
                "diagnostics": [],
                "metrics": [
                    {"metric": "line", "verdict": "pass"},
                    {"metric": "branch", "verdict": "fail"},
                ],
            }
        },
    )
    plan = SimpleNamespace(
        handle=SimpleNamespace(identity="sim_core", name="sim_core"),
        selected_tests=("smoke",),
        declared_tests=("smoke",),
        criterion=policies["coverage_sim_core"],
        criterion_key="coverage_sim_core",
    )
    campaign = SimpleNamespace(runs=(), evaluation={"status": "fail"})
    changes = coverage_acceptance._apply_campaign(
        state, plan, campaign, tmp_path / "coverage.json"
    )
    assert len(changes) == 2
    assert {change.met for change in changes} == {True, False}
