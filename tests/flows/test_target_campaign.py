"""Direct tests for the shared Target campaign domain."""

import ast
from pathlib import Path
from types import SimpleNamespace

import pytest

from booley.flows.target_campaign import (
    CampaignResults,
    CampaignScopeError,
    NoRunnableTestsError,
    TargetCampaign,
    build_campaign_freshness,
    resolve_target_campaign,
)


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
    assert results.all_match(lambda value: len(value) > 3)
    assert results.any_match(lambda value: value == "CORNER")


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


def test_empty_results_cannot_pass_vacuously() -> None:
    results: CampaignResults[str] = CampaignResults(())

    assert results.all_match(lambda _value: True) is False
    assert results.any_match(lambda _value: True) is False


def test_builds_compatible_freshness_detail(tmp_path) -> None:
    (tmp_path / "rtl").mkdir()
    (tmp_path / "rtl" / "dut.sv").write_text("module dut; endmodule\n")

    freshness = build_campaign_freshness(
        tmp_path,
        target="sim",
        categories=("tb", "rtl", "rtl"),
    ).to_detail()

    assert freshness["target"] == "sim"
    assert freshness["categories"] == ["rtl", "tb"]
    assert freshness["fingerprint"]["algorithm"] == "sha256"


def test_campaign_domain_does_not_import_presentation_layers() -> None:
    source = Path(__file__).parents[2] / "src" / "booley" / "flows" / "target_campaign.py"
    tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
    imports = {
        node.module or ""
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.level == 0
    }

    forbidden = ("booley.harness", "booley.mcp", "booley.ticket_board")
    assert not {
        module
        for module in imports
        if any(module == prefix or module.startswith(f"{prefix}.") for prefix in forbidden)
    }
