"""Atomic Ticket Criteria bind to existing Flow result keys."""

from __future__ import annotations

from booley.criteria.state import DevelopmentState
from booley.criteria.templates import BASELINE_TARGET_PARAM
from booley.flows.sim import coverage_acceptance, coverage_flow_context
from booley.harness.setup import intake
from booley.targets.domain import TARGET_IDENTITY_PARAM
from booley.ticket_board import amendment
from booley.ticket_board import criteria_projection as projection_module
from booley.ticket_board.criteria_projection import project_ticket_criteria
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
    synth_entries = {
        key: entry for key, entry in state.criteria.items() if key.startswith("synthesis_ok_")
    }
    for entry in synth_entries.values():
        parameter = next(
            param for param in entry.params if param in {"area_um2_max", "fmax_mhz_min"}
        )
        checks = {check["param"]: check for check in entry.detail["checks"]}
        assert checks[parameter]["pass"] is (parameter == "area_um2_max")
    assert len({id(entry.detail) for entry in synth_entries.values()}) == 2


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


def test_cycle_and_synthesis_baselines_reach_flow_state() -> None:
    text = (
        "---\nsummary: Compare measured targets\ntype: verification\nbranch: main\n"
        "scope: [rtl/core.sv]\non_success: []\nCRITERIA_MANDATORY:\n"
        "  CYCLE_COUNT: {sim_core: {smoke: {baseline: sim_base, cycle_count_increase_at_most: '10%'}}}\n"
        "  SYNTH: {synth_core: {baseline: synth_base, area_increase_at_most: '5%'}}\n"
        "---\n\n## Description\n\nCompare results.\n"
    )
    view = TicketAuthoringView(lambda selector, _flow: selector, lambda _target: ("smoke",))
    converted = convert_ticket_document(
        text, TicketConversionContext("draft", lambda _generated: view)
    )
    assert converted.document is not None, converted.diagnostics
    projection = project_ticket_criteria(converted.document.spec)
    cycle_key = next(key for key in projection.params if key.startswith("cycle_count_"))
    synth_key = next(key for key in projection.params if key.startswith("synthesis_ok_"))
    assert projection.params[cycle_key][BASELINE_TARGET_PARAM] == "sim_base"
    assert projection.params[cycle_key]["required_tests"] == ["smoke"]
    assert projection.params[synth_key][BASELINE_TARGET_PARAM] == "synth_base"


def test_standalone_and_project_scalar_have_no_per_target_result_alias() -> None:
    text = (
        "---\nsummary: Check project\ntype: verification\nbranch: main\n"
        "scope: [rtl/core.sv]\non_success: []\nCRITERIA_MANDATORY:\n"
        "  ELAB_STANDALONE: [core_a, core_b]\n"
        "  IMPLEMENTATION_DONE: true\n"
        "---\n\n## Description\n\nCheck project.\n"
    )
    view = TicketAuthoringView(
        lambda selector, _flow: selector,
        lambda _target: (),
        frozenset({"IMPLEMENTATION_DONE"}),
    )
    converted = convert_ticket_document(
        text, TicketConversionContext("draft", lambda _generated: view)
    )
    assert converted.document is not None, converted.diagnostics
    projection = project_ticket_criteria(converted.document.spec)
    assert projection.params["elaborate_standalone"]["targets"] == ["core_a", "core_b"]
    assert projection.params["implementation_done"] == {}
    assert projection.aliases == {}


def test_projection_contract_covers_all_ticket_capabilities() -> None:
    text = (
        "---\nsummary: Project every capability\ntype: verification\nbranch: main\n"
        "scope: [rtl/core.sv]\non_success: []\nCRITERIA_MANDATORY:\n"
        "  LINT: {lint_core: clean}\n"
        "  SIM: {sim_core: {all: pass, smoke: fail -> pass}}\n"
        "  CYCLE_COUNT:\n"
        "    sim_core: {smoke: {cycle_count_max: 100}}\n"
        "    sim_relative: {smoke: {baseline: sim_base, cycle_count_increase_at_most: '10%'}}\n"
        "  SYNTH:\n"
        "    synth_run: pass\n"
        "    synth_metrics: {area_um2_max: 10000, fmax_mhz_min: 400}\n"
        "    synth_relative: {baseline: synth_base, area_increase_at_most: '5%'}\n"
        "  FPGA:\n"
        "    fpga_run: pass\n"
        "    fpga_metrics: {lut_count_max: 1000, fmax_mhz_min: 200}\n"
        "  MUTATION: {sim_core: {scope: [rtl/core.sv], min_detected: 8, total: 10}}\n"
        "  COVERAGE:\n"
        "    sim_core: {tests: [smoke], metrics: {line: {min_pct: 90}, branch: {min_pct: 80}}}\n"
        "  ELAB_STANDALONE: [sim_core, sim_relative]\n"
        "  REVIEW: {rtl: {bugs: clean}, tb: {quality: clean}}\n"
        "  IMPLEMENTATION_DONE: true\n"
        "CRITERIA_OPTIONAL:\n  LINT: {lint_optional: clean}\n"
        "---\n\n## Description\n\nProject every capability.\n"
    )
    view = TicketAuthoringView(
        lambda selector, _flow: f"acme:ip:core:1.0#{selector}",
        lambda _target: ("smoke",),
        frozenset({"IMPLEMENTATION_DONE"}),
    )
    converted = convert_ticket_document(
        text, TicketConversionContext("draft", lambda _generated: view)
    )
    assert converted.document is not None, converted.diagnostics
    spec = converted.document.spec

    projection = project_ticket_criteria(spec)

    assert projection.required == {row.identity: row.mandatory for row in spec.criteria}
    assert any(not required for required in projection.required.values())
    rows = {(row.capability, row.target, row.parameter, row.test): row for row in spec.criteria}

    sim_all = rows[("SIM", "acme:ip:core:1.0#sim_core", None, "all")]
    assert projection.params[sim_all.identity] == {
        "target": sim_all.target,
        TARGET_IDENTITY_PARAM: sim_all.target,
        "test_selector": "all",
    }
    sim_named = rows[("SIM", "acme:ip:core:1.0#sim_core", None, "smoke")]
    assert projection.params[sim_named.identity] == {
        "target": sim_named.target,
        TARGET_IDENTITY_PARAM: sim_named.target,
        "test_selector": "smoke",
        "required_tests": ["smoke"],
        "minimum_total": 1,
        "from_state": "fail",
    }

    cycle = rows[("CYCLE_COUNT", "acme:ip:core:1.0#sim_core", "cycle_count_max", "smoke")]
    assert projection.params[cycle.identity] == {
        "target": cycle.target,
        TARGET_IDENTITY_PARAM: cycle.target,
        "test": "smoke",
        "test_selector": "smoke",
        "required_tests": ["smoke"],
        "minimum_total": 1,
        "cycle_count_max": 100,
    }
    relative_cycle = rows[
        (
            "CYCLE_COUNT",
            "acme:ip:core:1.0#sim_relative",
            "cycle_count_increase_at_most",
            "smoke",
        )
    ]
    assert projection.params[relative_cycle.identity][BASELINE_TARGET_PARAM] == (
        "acme:ip:core:1.0#sim_base"
    )

    for capability, target in (("SYNTH", "synth_run"), ("FPGA", "fpga_run")):
        row = rows[(capability, f"acme:ip:core:1.0#{target}", "run", None)]
        assert projection.params[row.identity] == {
            "target": row.target,
            TARGET_IDENTITY_PARAM: row.target,
        }
    for capability, target, parameter, threshold in (
        ("SYNTH", "synth_metrics", "area_um2_max", 10000),
        ("SYNTH", "synth_metrics", "fmax_mhz_min", 400),
        ("FPGA", "fpga_metrics", "lut_count_max", 1000),
        ("FPGA", "fpga_metrics", "fmax_mhz_min", 200),
    ):
        row = rows[(capability, f"acme:ip:core:1.0#{target}", parameter, None)]
        assert projection.params[row.identity][parameter] == threshold
    relative_synth = rows[
        (
            "SYNTH",
            "acme:ip:core:1.0#synth_relative",
            "area_increase_at_most",
            None,
        )
    ]
    assert projection.params[relative_synth.identity][BASELINE_TARGET_PARAM] == (
        "acme:ip:core:1.0#synth_base"
    )

    mutation = next(row for row in spec.criteria if row.capability == "MUTATION")
    assert projection.params[mutation.identity] == {
        "target": mutation.target,
        TARGET_IDENTITY_PARAM: mutation.target,
        "scope": ["rtl/core.sv"],
        "min_detected": 8,
        "total": 10,
    }
    coverage = [row for row in spec.criteria if row.capability == "COVERAGE"]
    assert [projection.params[row.identity]["metrics"] for row in coverage] == [
        {"line": {"min_pct": 90}},
        {"branch": {"min_pct": 80}},
    ]
    assert all(projection.params[row.identity]["tests"] == ["smoke"] for row in coverage)

    standalone = next(row for row in spec.criteria if row.capability == "ELAB_STANDALONE")
    assert projection.params[standalone.identity]["targets"] == [
        "acme:ip:core:1.0#sim_core",
        "acme:ip:core:1.0#sim_relative",
    ]
    assert standalone.identity not in {
        identity for aliases in projection.aliases.values() for identity in aliases
    }
    assert set(projection.categories.values()) == {"rtl", "tb"}
    assert projection.params["implementation_done"] == {}
    assert "implementation_done" not in {
        identity for aliases in projection.aliases.values() for identity in aliases
    }
    assert projection.aliases["sim_pass_sim_core"] == [sim_all.identity, sim_named.identity]
    assert projection.aliases["coverage_sim_core"] == [row.identity for row in coverage]


def test_callers_bind_the_ticket_board_projection() -> None:
    assert intake.project_ticket_criteria is projection_module.project_ticket_criteria
    assert amendment.project_ticket_criteria is projection_module.project_ticket_criteria
