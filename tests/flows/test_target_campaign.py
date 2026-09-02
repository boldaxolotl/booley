"""Direct tests for the shared Target campaign domain."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from booley.flows.target_campaign import (
    TargetCampaign,
    all_campaign_results_match,
    resolve_target_campaign,
)
from booley.flows.target_criteria import CampaignScopeError
from booley.flows.target_test_suite import NoRunnableTestsError
from tests.architecture.production import assert_no_dependencies


def _entry(**params: object) -> SimpleNamespace:
    return SimpleNamespace(params=params)


def test_resolves_typed_criteria_scope_and_test_suite() -> None:
    campaign = resolve_target_campaign(
        "sim",
        ("coverage_toggle", "coverage_fsm"),
        {
            "coverage_toggle_sim": _entry(scope=["rtl/dut.sv"], min_pct=90),
            "coverage_fsm_sim": _entry(scope=["rtl/dut.sv"], min_pct=80),
        },
        test_names={"sim": ["smoke", "corner"]},
        test_skips={"sim": ["corner"]},
    )

    assert isinstance(campaign, TargetCampaign)
    assert campaign.scope == ("rtl/dut.sv",)
    assert campaign.params_for("coverage_toggle")["min_pct"] == 90
    assert campaign.suite.tests == ("smoke",)
    assert campaign.suite.skipped == ("corner",)


def test_rejects_missing_and_conflicting_criterion_scope() -> None:
    with pytest.raises(CampaignScopeError, match="missing"):
        resolve_target_campaign("sim", ("mutation_score",), {}, test_names={}, test_skips={})

    criteria = {
        "coverage_toggle_sim": _entry(scope=["rtl/a.sv"]),
        "coverage_fsm_sim": _entry(scope=["rtl/b.sv"]),
    }
    with pytest.raises(CampaignScopeError, match="conflicting"):
        resolve_target_campaign(
            "sim",
            ("coverage_toggle", "coverage_fsm"),
            criteria,
            test_names={},
            test_skips={},
        )


def test_explicit_scope_overrides_criterion_scope() -> None:
    campaign = resolve_target_campaign(
        "sim",
        ("coverage_toggle", "coverage_fsm"),
        {
            "coverage_toggle_sim": _entry(scope=["rtl/a.sv"]),
            "coverage_fsm_sim": _entry(scope=["rtl/b.sv"]),
        },
        explicit_scope="rtl/selected.sv",
        test_names={},
        test_skips={},
    )

    assert campaign.scope_arg == "rtl/selected.sv"


def test_executes_individual_units_through_injected_behavior() -> None:
    campaign = resolve_target_campaign(
        "sim",
        ("mutation_score",),
        {"mutation_score_sim": _entry(scope=["rtl/dut.sv"])},
        test_names={"sim": ["reset", "corner"]},
        test_skips={},
    )

    results = campaign.execute(lambda unit: unit.display_name.upper())

    assert results.values == ("RESET", "CORNER")


def test_batched_unit_describes_all_selected_tests() -> None:
    campaign = resolve_target_campaign(
        "sim",
        ("mutation_score",),
        {"mutation_score_sim": _entry(scope=["rtl/dut.sv"])},
        test_names={"sim": ["reset", "corner"]},
        test_skips={},
    )

    units = campaign.execution_units(batched=True)

    assert len(units) == 1
    assert units[0].display_name == "<cocotb-suite>"
    assert units[0].selected_tests == ("reset", "corner")


def test_all_skipped_campaign_is_rejected() -> None:
    with pytest.raises(NoRunnableTestsError, match="every declared test is skipped"):
        resolve_target_campaign(
            "sim",
            ("mutation_score",),
            {"mutation_score_sim": _entry(scope=["rtl/dut.sv"])},
            test_names={"sim": ["reset", "corner"]},
            test_skips={"sim": ["reset", "corner"]},
        )


def test_campaign_aggregation_cannot_pass_vacuously() -> None:
    assert all_campaign_results_match([], lambda _value: True) is False
    assert all_campaign_results_match([1, 2], lambda value: value > 0) is True


def test_campaign_domain_does_not_import_presentation_layers() -> None:
    flows_dir = Path(__file__).parents[2] / "src" / "booley" / "flows"
    paths = tuple(
        flows_dir / filename
        for filename in ("target_campaign.py", "target_criteria.py", "target_test_suite.py")
    )
    assert_no_dependencies(
        paths=paths,
        target_prefixes=("booley.harness", "booley.mcp", "booley.ticket_board"),
    )
