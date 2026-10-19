"""Real presentation adapters agree without exporting machine keys as human names."""

import asyncio
import json
import re
from dataclasses import replace
from uuid import uuid4

import pytest

from booley.flows import endpoint_events
from booley.goals.changes import ChangeKind
from booley.goals.entry import EntryResult
from booley.goals.finish_presentation import render_goal_briefing, render_goal_html
from booley.goals.flow_execution import GoalFlowExecution
from booley.goals.format import render_status
from booley.goals.model import REVIEW_KINDS, GoalRecord
from booley.goals.paths import record_paths
from booley.goals.presentation import goal_presentation, present_spec
from booley.goals.proposals import ProposalView, new_proposal
from booley.goals.status import GoalStatus, GoalStatusView
from booley.goals.store import GoalStore
from booley.harness.dashboard.model import _terminal, project_goal
from booley.mcp.application import McpInputRequired, McpRequestContext
from booley.mcp.goal_changes import _pending_response
from booley.mcp.goal_tools import dispatch_goal_tool
from tests.goals.conftest import bind
from tests.goals.test_completion_package import metric_package

CASES = [
    ("lint", "lint_clean_expanded_sentinel", "LINT:top"),
    ("sim", "sim_pass_u16x73656e74696e656c", "SIM:top"),
    ("elab", "elab_pass_expanded_sentinel", "ELAB:top"),
    ("synth", "synthesis_ok_expanded_sentinel", "SYNTH:top"),
    ("fpga", "fpga_impl_ok_expanded_sentinel", "FPGA:top"),
    ("cycle_count", "cycle_count_expanded_sentinel", "CYCLE_COUNT:top"),
    ("coverage", "coverage_expanded_sentinel", "COVERAGE:top"),
    ("mutation", "mutation_score_expanded_sentinel", "MUTATION:top"),
    *[("review", f"review_{kind}_clean", f"REVIEW:{kind}") for kind in REVIEW_KINDS],
]


# Windows CI has measured over 40% of the 60 s default; keep 3x headroom (#1202).
@pytest.mark.timeout(120)
@pytest.mark.parametrize(("family", "key", "expected"), CASES)
def test_entry_status_dashboard_approval_completion_and_events_agree(
    layout, monkeypatch, capsys, family, key, expected
):
    package, state = metric_package(layout, monkeypatch, family, key, {})
    record = GoalRecord.from_json(package.facts["record"])
    spec = record.goals[0].spec
    presentation = goal_presentation(spec)
    assert presentation.name == expected
    view = GoalStatusView(
        record, (GoalStatus(key, "unmet", "no evidence", presentation=presentation),)
    )
    response = _approval_response(record, spec, key)
    terminal = _completed_dashboard(layout, record, package)
    active_outputs, adapter = _active_surfaces(layout, record, key, monkeypatch, capsys)
    outputs = [
        EntryResult(record, (), "rules").render(),
        render_status((view,)),
        render_status((view,), short=True),
        response.message,
        render_goal_briefing(package, present=present_spec),
        render_goal_html(package, present=present_spec),
        terminal.goals[0].presentation.label,
        *active_outputs,
    ]
    _assert_public_outputs(outputs, expected, key)
    assert adapter.present_criterion(key, {}).label == expected
    _assert_event_name(state, adapter, monkeypatch, key, expected)


def _assert_public_outputs(outputs, expected, key):
    for output in outputs:
        assert expected in output
        assert key not in output
        for internal in (
            "lint_clean",
            "sim_pass",
            "elab_pass",
            "synthesis_ok",
            "fpga_impl_ok",
            "mutation_score",
            "cycle_count",
        ):
            assert not re.search(r"\b" + re.escape(internal) + r"\b", output)


def _assert_event_name(state, adapter, monkeypatch, key, expected):
    emitted = []
    monkeypatch.setattr(endpoint_events, "_write_display_event", emitted.append)
    endpoint_events._emit_criteria_update(state, adapter)
    assert emitted[0]["criteria"][key]["presentation"]["label"] == expected
    assert key not in json.dumps(emitted[0]["criteria"][key]["presentation"])


def _active_surfaces(layout, record, key, monkeypatch, capsys):
    store = GoalStore(layout.control)
    with store.record_lock(record.id) as lock:
        layout.record = store.save(
            lock, replace(record, project_snapshot=layout.record.project_snapshot)
        )
    active = project_goal(store, layout.record)
    result = asyncio.run(
        dispatch_goal_tool(
            "goal_status", {"work_dir": str(layout.worktree)}, booley_dir=layout.control
        )
    )
    assert not result.is_error
    blocks, structured = result.value
    assert structured["records"][0]["goals"][0]["goal_key"] == key
    cli = _cli_text(layout, monkeypatch, capsys)
    refusal = _finish_refusal(layout)
    return [active.goals[0].presentation.label, blocks[0].text, cli, refusal], GoalFlowExecution(
        bind(layout)
    )


def _cli_text(layout, monkeypatch, capsys):
    from argparse import Namespace

    from booley.harness.booley import _cmd_goal

    monkeypatch.chdir(layout.worktree)
    assert _cmd_goal(Namespace(goal_command="status", short=False, long=True), layout.main) == 0
    return capsys.readouterr().out


def _finish_refusal(layout):
    from booley.goals.finish import finish_goal
    from booley.goals.lifecycle import LifecycleError, LifecycleRequest
    from tests.goals.test_finish import environment

    call = LifecycleRequest(
        layout.worktree, layout.record.id, str(uuid4()), summary="Attempt completion."
    )
    with pytest.raises(LifecycleError, match="met and fresh") as caught:
        finish_goal(call, environment(layout))
    return str(caught.value)


def _approval_response(record, spec, key):
    proposal = new_proposal(
        record,
        kind=ChangeKind.ADD,
        goal_key=key,
        before=None,
        after=spec,
        after_arg=None,
        runtime_params={},
        rationale="add this Goal",
        session_key=None,
    )
    response = _pending_response(
        ProposalView(proposal),
        record.id,
        McpRequestContext(protocol="2026-07-28", form_capability=True),
    )
    assert isinstance(response, McpInputRequired)
    return response


def _completed_dashboard(layout, record, package):
    operation = str(uuid4())
    paths = record_paths(layout.control, record.id)
    destination = paths.root / "operations" / operation / "review-package.json"
    destination.parent.mkdir(parents=True)
    destination.write_text(json.dumps(package.to_json()))
    return _terminal(GoalStore(layout.control), replace(record, finish_operation=operation))


def test_completion_audit_projects_before_after_without_changing_package(layout, monkeypatch):
    package, _state = metric_package(layout, monkeypatch, "synth", "opaque_key_sentinel", {})
    facts = package.facts
    spec = facts["goals"][0]["spec"]
    facts["change_log"] = [
        {
            "id": "change",
            "goal_key": "opaque_key_sentinel",
            "before": spec,
            "after": spec,
            "reason": "human reason",
        }
    ]
    facts["proposal_decisions"] = [
        {
            "proposal": {
                "id": "proposal",
                "kind": "relax",
                "goal_key": "opaque_key_sentinel",
                "before": spec,
                "after": spec,
                "rationale": "human rationale",
                "runtime_params": {"opaque_key_sentinel": True},
            },
            "state": "applied",
        }
    ]
    saved = json.dumps(facts, sort_keys=True)
    for output in (
        render_goal_briefing(package, present=present_spec),
        render_goal_html(package, present=present_spec),
    ):
        assert "SYNTH:top" in output
        assert "opaque_key_sentinel" not in output
        assert "human reason" in output and "human rationale" in output
    assert json.dumps(facts, sort_keys=True) == saved


def test_admitted_producer_name_does_not_follow_a_later_retarget(goal_mode):
    store = GoalStore(goal_mode.control)
    adapter = GoalFlowExecution(bind(goal_mode))
    assert adapter.present_criterion(goal_mode.record.goals[0].spec.key, {}).label == "LINT:top"
    recorded = goal_mode.record.goals[0]
    old = recorded.spec
    changed = replace(recorded, spec=replace(old, target="new_target"), spec_revision=2)
    with store.record_lock(goal_mode.record.id) as lock:
        store.save(lock, replace(goal_mode.record, goals=(changed, *goal_mode.record.goals[1:])))
    assert adapter.present_criterion(old.key, {}).label == "LINT:top"
    assert "new_target" not in adapter.present_criterion(old.key, {}).label


@pytest.mark.parametrize(
    "raw",
    [
        {
            "family": "synthesis_ok",
            "target": "secret_sentinel",
            "nested": {"key": "opaque_sentinel"},
        },
        {"synthesis_ok": {"target": "secret_sentinel", "key": "opaque_sentinel"}},
        {"target": "secret_sentinel", "goal_key": "sim_pass_u16x73656e74696e656c"},
        "synthesis_ok",
        ["sim_pass_u16x73656e74696e656c"],
        None,
    ],
)
def test_goal_schema_errors_never_echo_rejected_internal_names(raw):
    from jsonschema import Draft202012Validator

    from booley.goals.model import goal_arg_json_schema
    from booley.mcp.application import _format_validation_error

    error = next(Draft202012Validator(goal_arg_json_schema()).iter_errors(raw))
    message = _format_validation_error(error)
    assert "SYNTH" in message
    for value in (
        "synthesis_ok",
        "secret_sentinel",
        "opaque_sentinel",
        "sim_pass_u16x73656e74696e656c",
    ):
        assert value not in message


def test_corrupt_record_diagnostic_never_exports_goal_identity(goal_mode):
    paths = record_paths(goal_mode.control, goal_mode.record.id)
    raw = json.loads(paths.record_file.read_text())
    raw["goals"][0]["spec_revision"] = raw["revision"] + 1
    paths.record_file.write_text(json.dumps(raw))
    result = asyncio.run(
        dispatch_goal_tool(
            "goal_status", {"work_dir": str(goal_mode.worktree)}, booley_dir=goal_mode.control
        )
    )
    assert result.is_error
    assert (
        result.value[0].text
        == f"ERROR: Goal unavailable (unsupported record); inspect: {paths.record_file}"
    )
    assert raw["goals"][0]["key"] not in result.value[0].text


def test_actual_reviewer_replay_and_limit_reports_keep_the_goal_name(layout, monkeypatch):
    from tests.goals.conftest import enter_goals
    from tests.goals.test_mcp_routing import _goal_reviewer_endpoint

    layout.record = enter_goals(
        layout,
        [{"paths": ["rtl.v"], "family": "REVIEW", "review": "rtl_bugs", "verdict": "clean"}],
    )
    endpoint, _provider = _goal_reviewer_endpoint(layout, monkeypatch)
    key = layout.record.goals[0].spec.key
    for result in (
        endpoint._already_clean_result(key),
        endpoint._clean_verify_limit(key, {"verify_attempts": 2}),
        endpoint._clean_verify_limit(key, {"total_verify_cycles": 3}),
    ):
        assert "REVIEW:rtl_bugs" in result.report_text
        assert key not in result.report_text


def test_corrupt_evidence_state_has_bounded_dashboard_diagnostic(goal_mode):
    from booley.harness.dashboard.model import DashboardReader

    paths = record_paths(goal_mode.control, goal_mode.record.id)
    raw = json.loads(paths.state_file.read_text())
    key = goal_mode.record.goals[0].spec.key
    raw["criteria"][key] = None
    paths.state_file.write_text(json.dumps(raw))
    view = DashboardReader(goal_mode.worktree, goal_mode.control).read().goals[0]
    assert view.diagnostic == f"Goal unavailable (unsupported record); inspect: {paths.state_file}"
    assert key not in view.diagnostic


def test_corrupt_proposal_diagnostic_keeps_location_without_internal_family(goal_mode):
    from pathlib import Path

    from booley.harness.dashboard.model import DashboardReader

    fixture = Path(__file__).parent / "fixtures/non-waiver-proposal-v1/payload.json"
    raw = json.loads(fixture.read_bytes())
    raw["after"]["family"] = "synthesis_ok"
    directory = record_paths(goal_mode.control, goal_mode.record.id).root / "proposals" / raw["id"]
    directory.mkdir(parents=True)
    directory.joinpath("payload.json").write_text(json.dumps(raw))
    result = asyncio.run(
        dispatch_goal_tool(
            "goal_status", {"work_dir": str(goal_mode.worktree)}, booley_dir=goal_mode.control
        )
    )
    assert result.is_error
    assert "Goal unavailable" in result.value[0].text and str(directory) in result.value[0].text
    assert "synthesis_ok" not in result.value[0].text
    diagnostic = DashboardReader(goal_mode.worktree, goal_mode.control).read().goals[0].diagnostic
    assert str(directory) in diagnostic and "synthesis_ok" not in diagnostic


@pytest.mark.parametrize("selector", ["sim_0", "acme:demo:counter:1#sim_0"])
@pytest.mark.parametrize("alias", [False, True])
def test_coverage_no_waivers_rejection_uses_admitted_goal_name(
    layout, monkeypatch, selector, alias
):
    from booley.flows.sim.coverage_flow_context import coverage_context_for_execution
    from booley.flows.sim.coverage_invocation import (
        CoverageInvocationRequest,
        prepare_coverage_invocation,
    )
    from booley.goals.state_store import load_goal_state

    _admit_coverage_goal(layout, monkeypatch, selector)
    adapter = GoalFlowExecution(bind(layout))
    state = load_goal_state(GoalStore(layout.control), layout.record)
    key = layout.record.goals[0].spec.key
    if alias:
        state.flow_key_aliases["coverage_sim_0"] = [key]
    context = coverage_context_for_execution(layout.worktree, state, recorder=adapter)
    result = prepare_coverage_invocation(
        CoverageInvocationRequest(("sim_0",), no_waivers=True), context
    )
    assert result.plan is None
    assert len(result.findings) == 1 and result.findings[0].code == "COV_NO_WAIVERS_CRITERION"
    message = result.findings[0].message
    assert "COVERAGE:sim_0" in message
    assert layout.record.goals[0].spec.key not in message
    assert set(context.criteria) == {"coverage_sim_0" if alias else key}


def _admit_coverage_goal(layout, monkeypatch, selector):
    from booley.goals import entry
    from booley.goals.model import parse_goal_args
    from booley.targets.catalog import TargetCatalog
    from tests.flows.sim.test_coverage_invocation import project
    from tests.goals.conftest import git

    project(layout.worktree)
    (layout.worktree / ".booley/tests.toml").write_text('[sim_0]\ntests = ["reset", "wrap"]\n')
    git(layout.worktree, "add", "counter.core", "rtl")
    git(layout.worktree, "commit", "-qm", "coverage Target")
    monkeypatch.setattr(entry, "TargetCatalog", TargetCatalog)
    layout.record = entry.enter_goal_mode(
        entry.EntryRequest(
            work_dir=layout.worktree,
            slug="coverage",
            goals=parse_goal_args(
                [
                    {
                        "family": "COVERAGE",
                        "target": selector,
                        "tests": "all",
                        "metrics": {"line": 100},
                    }
                ]
            ),
        ),
        entry.EntryEnvironment(
            booley_dir=layout.control, routing_guidance="Explicit test routing guidance."
        ),
    ).record
