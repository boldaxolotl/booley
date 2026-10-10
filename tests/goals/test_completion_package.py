"""Actual Reviewer production receipts, exact derivations and modern completion transport."""

import asyncio
import json
from pathlib import Path
from uuid import uuid4

import pytest
from mcp import Client

from booley.goals.finish import finish_goal
from booley.goals.lifecycle import LifecycleRequest
from booley.review.goal_package import GoalCompletionPackage
from tests.goals.conftest import enter_goals
from tests.goals.test_finish import environment
from tests.goals.test_mcp_routing import _goal_reviewer_endpoint
from tests.goals.test_proposal_wire import sdk
from tests.goals.test_proposals import observations
from tests.goals.test_reviewer_proposals import approve_review_relaxation, review_issues

# These multi-step Git/Reviewer cases retain the established Goal lifecycle budget.
# The measured Windows Reviewer baseline is 27.768s (windows-test-timings.json);
# its 3x/30s-rounded minimum is 90s, above CI's unannotated 60s default.
pytestmark = pytest.mark.timeout(120)


@pytest.mark.parametrize("derived", [False, True])
def test_open_done_findings_finish_without_ticket_assessment_or_second_approval(
    layout, monkeypatch, capsys, derived
):
    monkeypatch.setenv("BOOLEY_MCP_MODE", "interactive")
    layout.record = enter_goals(
        layout,
        [{"family": "review", "review": "rtl_bugs", "verdict": "clean" if derived else "done"}],
    )
    endpoint, provider = _goal_reviewer_endpoint(layout, monkeypatch)
    provider.return_value.output = json.dumps({"issues": review_issues(True)})
    assert endpoint._run().exit_code == int(derived)
    producer = observations(layout)[0]
    if derived:
        asyncio.run(approve_review_relaxation(layout, monkeypatch))
    selected = observations(layout)[-1]
    replay, replay_provider = _goal_reviewer_endpoint(layout, monkeypatch)
    replay_result = replay._run()
    assert replay_result.exit_code == 0 and replay_provider.call_count == 0
    display = capsys.readouterr().out + str(replay_result)
    assert "explicit human approval before acceptance" not in display
    call = LifecycleRequest(
        layout.worktree,
        layout.record.id,
        str(uuid4()),
        summary="Completed review; the finding remains visible.",
        explain_html=True,
    )
    result = finish_goal(call, environment(layout))
    assert result["status"] == "finished"
    facts = json.loads(Path(result["package"]).read_bytes())
    assert facts["goals"][0]["selected_observation"] == selected
    assert "fixture finding" in result["message"]
    assert "fixture finding" in Path(result["html"]).read_text()
    assert "SemanticAssessment" not in Path(result["package"]).read_text()
    assert "approval before acceptance" not in result["message"]
    assert GoalCompletionPackage.from_json(facts).to_json() == facts
    if derived:
        assert facts["goals"][0]["original_observations"] == [producer]
        assert facts["proposal_decisions"][0]["decision"]["quote"]
        assert facts["proposal_decisions"][0]["transaction_digest"]


def test_modern_mcp_finish_requires_stable_binding_and_preserves_response_retry(
    layout, monkeypatch
):
    from tests.goals.test_status import publish

    layout.record = enter_goals(layout, [{"family": "lint", "target": "top"}])
    publish(layout)
    operation = str(uuid4())
    arguments = {
        "work_dir": str(layout.worktree),
        "record_id": layout.record.id,
        "operation_id": operation,
        "summary": "Modern transport completed the Goal.",
    }

    async def run():
        async with Client(sdk(layout, monkeypatch), mode="2026-07-28") as client:
            first = await client.call_tool("goal_finish", arguments)
            assert not first.is_error, first.content
            retry = await client.call_tool("goal_finish", arguments)
            assert not retry.is_error
            assert retry.content == first.content
            changed = await client.call_tool("goal_finish", {**arguments, "summary": "Changed"})
            assert changed.is_error and "immutable payload" in changed.content[0].text

    asyncio.run(run())


def test_derived_sibling_and_rejected_agent_decision_are_frozen_separately(layout):
    from tests.goals.test_proposals import CYCLE_KEY, approve, proposal, publish, setup_cycle

    env = setup_cycle(layout)
    publish(layout)
    approved = proposal(layout, env)
    approve(layout, env, approved.proposal.id)
    rejected = proposal(layout, env, maximum=300)
    approve(layout, env, rejected.proposal.id, answer="reject")
    selected = observations(layout)
    result = finish_goal(
        LifecycleRequest(
            layout.worktree,
            layout.record.id,
            str(uuid4()),
            summary="Measured cycle bound accepted; rejected further relaxation.",
            explain_html=True,
        ),
        environment(layout),
    )
    facts = json.loads(Path(result["package"]).read_bytes())
    cycle = next(row for row in facts["goals"] if row["key"] == CYCLE_KEY)
    assert cycle["selected_observation"] == next(
        row for row in reversed(selected) if row["criterion"] == CYCLE_KEY
    )
    assert cycle["original_observations"][0] == selected[0]
    assert (
        len(facts["change_log"])
        == len(facts["applied_proposal_decisions"])
        == len(facts["rejected_proposal_decisions"])
        == 1
    )
    assert len(facts["agent_recorded_decisions"]) == 2
    assert rejected.proposal.payload_digest in result["message"]
    assert rejected.proposal.payload_digest in Path(result["html"]).read_text()


def test_modern_abandon_quote_binding_and_saved_retry_after_replacement(layout, monkeypatch):
    from booley.goals.paths import record_paths

    layout.record = enter_goals(layout, [{"family": "lint", "target": "top"}])
    arguments = {
        "work_dir": str(layout.worktree),
        "record_id": layout.record.id,
        "operation_id": str(uuid4()),
        "abandon": True,
        "instruction_quote": "Stop with work retained",
    }

    async def run():
        async with Client(sdk(layout, monkeypatch), mode="2026-07-28") as client:
            first = await client.call_tool("goal_finish", arguments)
            assert not first.is_error
            changed = await client.call_tool(
                "goal_finish", {**arguments, "instruction_quote": "Different"}
            )
            assert changed.is_error
            from booley.goals.entry import EntryEnvironment, EntryRequest, enter_goal_mode
            from booley.goals.model import parse_goal_args

            record = enter_goal_mode(
                EntryRequest(
                    layout.worktree,
                    "replacement",
                    parse_goal_args([{"family": "lint", "target": "top"}]),
                ),
                EntryEnvironment(layout.control),
            ).record
            path = record_paths(layout.control, record.id).record_file
            before = path.read_bytes()
            retry = await client.call_tool("goal_finish", arguments)
            assert retry.content == first.content and not retry.is_error
            assert path.read_bytes() == before

    asyncio.run(run())


# Native Windows passed call: 40.944s; 3x rounded to 30s requires 150s.
@pytest.mark.timeout(150)
def test_attempt_frozen_survives_real_status_and_reviewer_activity_without_new_evidence(
    layout, monkeypatch
):
    from booley.goals.paths import record_paths
    from tests.goals.test_finish import Crash

    monkeypatch.setenv("BOOLEY_MCP_MODE", "interactive")
    layout.record = enter_goals(
        layout, [{"family": "review", "review": "rtl_bugs", "verdict": "done"}]
    )
    endpoint, provider = _goal_reviewer_endpoint(layout, monkeypatch)
    provider.return_value.output = json.dumps({"issues": review_issues(True)})
    assert endpoint._run().exit_code == 0
    call = LifecycleRequest(
        layout.worktree, layout.record.id, str(uuid4()), summary="Finish the recorded review."
    )

    def stop(name):
        if name == "attempt-frozen":
            raise Crash()

    with pytest.raises(Crash):
        finish_goal(call, environment(layout, stop))
    path = record_paths(layout.control, layout.record.id).state_file
    before = json.loads(path.read_bytes())
    exact = observations(layout)
    monkeypatch.setattr("booley.goals.state_store.utc_now_rfc3339", lambda: "2030-01-02T03:04:05Z")

    async def status():
        async with Client(sdk(layout, monkeypatch), mode="2026-07-28") as client:
            result = await client.call_tool("goal_status", {"work_dir": str(layout.worktree)})
            assert not result.is_error

    asyncio.run(status())
    monkeypatch.setenv("BOOLEY_CONTAINER", "1")
    replay, replay_provider = _goal_reviewer_endpoint(layout, monkeypatch)
    activity = replay.execute_prepared()
    assert activity.exit_code == 0, activity.outcome.report_text
    assert replay_provider.call_count == 0
    after = json.loads(path.read_bytes())
    assert before["criteria"] == after["criteria"] and observations(layout) == exact
    assert (
        before["timeline"] != after["timeline"] or before["last_updated"] != after["last_updated"]
    )
    result = finish_goal(call, environment(layout))
    assert result["status"] == "finished"
    assert (
        json.loads(Path(result["package"]).read_bytes())["goals"][0]["selected_observation"]
        == exact[0]
    )


@pytest.mark.parametrize(
    ("family", "detail", "expected"),
    [
        ("elab", {"mode": "elab", "target": "top", "attempts": 1}, "elaborated"),
        ("lint", {}, "evidence recorded"),
    ],
)
def test_elaboration_and_custom_producer_metric_finish(layout, family, detail, expected):
    from types import SimpleNamespace

    from booley.criteria.state import DevelopmentState
    from booley.flows.endpoint_state import EndpointState
    from booley.goals.flow_execution import GoalFlowExecution
    from booley.goals.status import build_status
    from booley.goals.store import GoalStore
    from tests.goals.conftest import bind

    layout.record = enter_goals(layout, [{"family": family, "target": "top"}])
    adapter = GoalFlowExecution(bind(layout))
    key = layout.record.goals[0].spec.key
    endpoint = SimpleNamespace(
        args=SimpleNamespace(work_dir=layout.worktree), _acceptance_recorder=adapter
    )
    stamped = EndpointState._stamp_source_fingerprint(
        endpoint, key, True, detail, source_target=None
    )
    state = DevelopmentState.load(adapter.state_file, adapter.state_persistence())
    changes = state.set_criterion(key, True, detail=stamped)
    adapter.record_changes(state, changes, invocation_id="ignored", producer="custom")
    state.save()
    goal = build_status(GoalStore(layout.control), layout.record).goals[0]
    assert goal.status == "met"
    result = finish_goal(
        LifecycleRequest(layout.worktree, layout.record.id, str(uuid4()), summary="Completed."),
        environment(layout),
    )
    assert result["status"] == "finished"
    facts = json.loads(Path(result["package"]).read_bytes())
    assert facts["goals"][0]["metric"] == goal.evidence_summary == expected
    assert GoalCompletionPackage.from_json(facts).to_json() == facts


METRIC_CASES = [
    ("lint", "lint_clean_top", {"warnings": 0}, "clean"),
    ("sim", "sim_pass_top", {"tests_passed": 2, "tests_total": 2}, "2/2 tests"),
    ("elab", "elab_pass_top", {"mode": "elab"}, "elaborated"),
    ("synth", "synthesis_ok_top", {"cells": 12}, "12 cells"),
    ("fpga", "fpga_impl_ok_top", {"lut_count": 1}, "1 LUTs"),
    ("cycle_count", "cycle_count_top_test", {"cycles": 1234}, "1,234 cycles"),
    ("coverage", "coverage_top", {"status": "pass"}, "pass"),
    ("mutation", "mutation_score_top", {"detected": 0, "total_valid": 10}, "0/10 (0%)"),
    ("lint", "custom_gate", {"opaque": True}, "evidence recorded"),
]


def metric_cases():
    from booley.goals.model import REVIEW_KINDS, REVIEW_VERDICTS

    reviews = [
        (
            "review",
            f"review_{kind}_{verdict}",
            {"issues": 0},
            "clean" if verdict == "clean" else "reviewed, 0 findings",
        )
        for kind in REVIEW_KINDS
        for verdict in REVIEW_VERDICTS
    ]
    normal = METRIC_CASES + reviews
    absent = [
        (family, key, {}, "elaborated" if family == "elab" else "evidence recorded")
        for family, key, _, _ in normal
    ]
    return (
        normal
        + absent
        + [
            (
                "mutation",
                "mutation_score_top",
                {"detected": 0, "total_valid": 0},
                "evidence recorded",
            ),
            ("sim", "sim_pass_top", {"tests_passed": 0, "tests_total": 0}, "evidence recorded"),
        ]
    )


def metric_package(layout, monkeypatch, family, key, detail):
    from dataclasses import replace

    from booley.criteria.state import DevelopmentState
    from booley.goals import review_package
    from booley.goals.model import GoalFamily
    from booley.review.goal_package import GoalReviewContext

    # Opaque keys exercise package compatibility, not Goal entry policy.
    layout.record = enter_goals(layout, [{"family": "lint", "target": "top"}])
    goal = layout.record.goals[0]
    spec = replace(goal.spec, family=GoalFamily(family), key=key)
    record = replace(layout.record, goals=(replace(goal, spec=spec),))
    state = DevelopmentState()
    state.set_criterion(key, True, detail=detail)
    observation = {
        "sequence": 1,
        "producer": "custom",
        "recorded_at": "2026-10-10T00:00:00Z",
        "invocation_id": "metric-test",
        "detail": detail,
    }
    monkeypatch.setattr(review_package, "selected_observations", lambda *_: {key: observation})
    monkeypatch.setattr(review_package, "validated_evidence_records", lambda *_: [observation])
    context = GoalReviewContext(
        record.to_json(),
        record.base_sha,
        record.base_sha,
        record.branch,
        str(layout.control / "logs"),
        "Completed.",
        {},
        [],
        [],
        {},
    )
    return review_package.build_goal_package(context, record, state, layout.control), state


@pytest.mark.parametrize(("family", "key", "detail", "expected"), metric_cases())
def test_all_goal_family_package_metrics(layout, monkeypatch, family, key, detail, expected):
    from booley.goals.format import format_met_goal_metric

    package, state = metric_package(layout, monkeypatch, family, key, detail)
    facts = package.to_json()
    assert (
        facts["goals"][0]["metric"] == format_met_goal_metric(key, state.criteria[key]) == expected
    )
    assert GoalCompletionPackage.from_json(facts).to_json() == facts


def test_historical_inventory_body_omission_preserves_original_digest_and_references(tmp_path):
    import hashlib
    from types import SimpleNamespace

    from booley.goals.review_package import _transactions

    logs = tmp_path
    directory = logs / "acceptance/transactions"
    directory.mkdir(parents=True)
    old = {
        "changes": [
            {
                "detail": {
                    "_source_fingerprint": {
                        "fingerprint": {"target_surface": {"files": ["other.vmem"]}}
                    }
                }
            }
        ]
    }
    selected = {
        "changes": [
            {"detail": {"_source_fingerprint": {"fingerprint": {"rtl": {"files": ["rtl.v"]}}}}}
        ]
    }
    for identity, manifest in (("old", old), ("selected", selected)):
        (directory / (identity + ".json")).write_text(json.dumps(manifest))
    indexed = {
        1: {"sequence": 1, "transaction_id": "old"},
        2: {"sequence": 2, "transaction_id": "selected"},
    }
    before = {path: path.read_bytes() for path in directory.iterdir()}
    facts = _transactions(
        logs,
        SimpleNamespace(acceptance_transactions=["old", "selected"]),
        indexed,
        omitted_inputs=frozenset({"other.vmem"}),
    )
    original_digest = hashlib.sha256(
        json.dumps(old, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()
    assert facts[0]["manifest"] is None
    assert facts[0]["manifest_sha256"] == original_digest
    assert facts[0]["observations"][0]["sequence"] == 1
    assert facts[1]["manifest"] == selected and facts[1]["manifest_sha256"]
    assert before == {path: path.read_bytes() for path in directory.iterdir()}


def _publish_current_base_simulation(layout):
    from booley.criteria.state import DevelopmentState
    from booley.flows.sim.campaign.codec import encode_simulation_campaign_manifest
    from booley.flows.sim.campaign.planning import finalize_manifest, manifest_digest
    from booley.goals.paths import record_paths
    from booley.goals.recorder import GoalEvidenceRecorder
    from tests.flows.sim.test_campaign_manifest_codec import _manifest_components, _manifest_work
    from tests.goals.conftest import bind, campaign_facts

    target, recipe, build, workload, suite = _manifest_components()
    target.update(vlnv="::base:0", name="base", selector="base", revision="abc123")
    variants, items = _manifest_work(target, recipe, build, workload)
    manifest = finalize_manifest(
        {
            "$schema": "booley.simulation-campaign-manifest/v1",
            "campaign_id": str(uuid4()),
            "created_at": "2026-10-07T10:00:00Z",
            "origin": {"execution_id": "b" * 32, "invocation_id": 8},
            "target": target,
            "workload": workload,
            "required_suite": suite,
            "build_variants": variants,
            "planning_disclosures": [],
            "prerequisites": [],
            "work_items": items,
        }
    )
    paths = record_paths(layout.control, layout.record.id)
    path = paths.runtime_dir / "flow-reports/sim/8/targets/base/campaign/manifest.json"
    path.parent.mkdir(parents=True)
    path.write_bytes(encode_simulation_campaign_manifest(manifest))
    facts = campaign_facts()
    facts.update(
        campaign_id=manifest.document["campaign_id"],
        manifest_sha256=manifest_digest(manifest),
        origin={"execution_id": "b" * 32, "invocation_id": 8},
        target={"identity": "::base:0#base", "selector": "base"},
        required_suite={"names": [], "default_invocation": True},
    )
    recorder = GoalEvidenceRecorder(bind(layout))
    state = DevelopmentState.load(paths.state_file, recorder.state_persistence())
    shadow = DevelopmentState.from_json_object(state.to_dict())
    changes = shadow.set_criterion(
        "sim_pass_base", True, detail={"required_tests": ["default"], "passed_tests": ["default"]}
    )
    transaction = recorder.record_or_verify_transaction(
        state, changes, acceptance_facts=facts, ticket_identity={}
    )
    return transaction


def test_obsolete_simulation_after_approved_retarget_omits_inventory_without_rewriting_ledger(
    layout, monkeypatch
):
    from booley.goals.apply import ChangeEnvironment
    from booley.goals.change_service import create_proposal
    from booley.goals.entry import EntryEnvironment
    from tests.goals.conftest import SIM_KEY, git
    from tests.goals.test_generated_inputs import classified
    from tests.goals.test_proposals import approve

    _source, _old_manifest, old_transaction = classified(
        layout,
        goals=[
            {"family": "sim", "target": "top"},
            {"family": "review", "review": "rtl_bugs", "verdict": "clean"},
        ],
        criterion=SIM_KEY,
        criterion_detail={"required_tests": ["smoke"], "passed_tests": ["smoke"]},
    )
    (layout.worktree / "base.core").write_bytes(
        b"CAPI=2:\nname: ::base:0\nfilesets:\n  rtl: {files: [rtl.v], file_type: verilogSource}\n"
        b"targets:\n  base: {filesets: [rtl], toplevel: top}\n"
    )
    git(layout.worktree, "add", "base.core")
    git(layout.worktree, "commit", "-qm", "add replacement target")
    env = ChangeEnvironment(EntryEnvironment(layout.control))
    proposal = create_proposal(
        env,
        layout.record.id,
        {
            "kind": "retarget",
            "goal_key": SIM_KEY,
            "after": {"family": "sim", "target": "base"},
            "rationale": "validate replacement target",
        },
        session_key="test-session",
    )
    approve(layout, env, proposal.proposal.id)
    current = _publish_current_base_simulation(layout)
    monkeypatch.setenv("BOOLEY_MCP_MODE", "interactive")
    endpoint, _provider = _goal_reviewer_endpoint(layout, monkeypatch)
    assert endpoint._run().exit_code == 0
    _assert_retargeted_simulation_package(layout, old_transaction, current)


def _assert_retargeted_simulation_package(layout, old_transaction, current):
    from booley.goals.paths import record_paths
    from tests.goals.test_finish import request

    logs = record_paths(layout.control, layout.record.id).logs_dir
    ledger = {path: path.read_bytes() for path in logs.rglob("*") if path.is_file()}
    result = finish_goal(request(layout, explain_html=True), environment(layout))
    facts = json.loads(Path(result["package"]).read_bytes())
    transactions = {row["transaction_id"]: row for row in facts["evidence_transactions"]}
    assert transactions[old_transaction.transaction_id]["manifest"] is None
    import hashlib

    from booley.goals.review_package import canonical_json
    from booley.review.goal_package import render_goal_briefing
    from booley.review.goal_presentation_v1 import render_goal_html

    old_path = logs / "acceptance/transactions" / (old_transaction.transaction_id + ".json")
    assert (
        transactions[old_transaction.transaction_id]["manifest_sha256"]
        == hashlib.sha256(canonical_json(json.loads(ledger[old_path]))).hexdigest()
    )
    assert transactions[current.transaction_id]["manifest"] is not None
    assert "prepared.vmem" not in Path(result["package"]).read_text()
    package = GoalCompletionPackage.from_json(facts)
    assert package.to_json() == facts
    for rendered in (
        Path(result["html"]).read_text(),
        render_goal_briefing(package),
        render_goal_html(package),
        Path(result["summary"]).read_text(),
    ):
        assert "prepared.vmem" not in rendered
    assert all(path.read_bytes() == content for path, content in ledger.items())
