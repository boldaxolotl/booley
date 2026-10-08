"""Real campaign manifests and selected Acceptance Journal proofs classify generated data."""

import hashlib
import json
from pathlib import Path
from uuid import uuid4

import pytest

from booley.criteria.state import DevelopmentState
from booley.flows.sim.campaign.codec import encode_simulation_campaign_manifest
from booley.flows.sim.campaign.planning import finalize_manifest, manifest_digest
from booley.goals.entry import EntryEnvironment
from booley.goals.finish import finish_goal
from booley.goals.lifecycle import LifecycleError
from booley.goals.paths import record_paths
from booley.goals.recorder import GoalEvidenceRecorder
from booley.mcp.goal_completion import complete_goal, completion_environment
from tests.flows.sim.test_campaign_manifest_codec import _manifest_components, _manifest_work
from tests.goals.conftest import LINT_KEY, bind, campaign_facts, enter_goals, git
from tests.goals.test_finish import Crash, request

# These multi-step Git/Reviewer cases retain the established Goal lifecycle budget.
# The measured Windows Reviewer baseline is 27.768s (windows-test-timings.json);
# its 3x/30s-rounded minimum is 90s, above CI's unannotated 60s default.
pytestmark = pytest.mark.timeout(120)


def classified(
    layout,
    *,
    suffix=".vmem",
    kind="generated_input",
    wrong_hash=False,
    foreign_target=False,
    foreign_origin=False,
    hdl_type=False,
    planner_program=False,
):
    layout.record = enter_goals(layout, [{"family": "lint", "target": "top"}])
    relative = "prepared" + suffix
    source = layout.worktree / relative
    source.write_bytes(b"00112233\n")
    (layout.main / ".git/info/exclude").write_bytes(f"/.booley_project\n{relative}\n".encode())
    core = layout.worktree / "top.core"
    core.write_bytes(
        core.read_bytes().replace(
            b"files: [rtl.v]",
            f"files: [rtl.v, {{{relative}: {{file_type: {'verilogSource' if hdl_type else 'user'}}}}}]".encode(),
        )
    )
    if planner_program:
        core.write_bytes(
            core.read_bytes() + f"scripts:\n  generate: {{cmd: [./{relative}]}}\n".encode()
        )
    git(layout.worktree, "add", "top.core")
    git(layout.worktree, "commit", "-qm", "declare generated build input")
    target, recipe, build, workload, suite = _manifest_components()
    target.update(
        vlnv="::foreign:0" if foreign_target else "::top:0",
        name="top",
        selector="top",
        revision="abc123",
    )
    variants, items = _manifest_work(target, recipe, build, workload)
    disclosure = {
        "planner": "fusesoc_setup",
        "scratch_inputs": [],
        "generated_files": [
            {
                "path": relative,
                "kind": kind,
                "bytes": len(source.read_bytes()),
                "sha256": "sha256:"
                + ("0" * 64 if wrong_hash else hashlib.sha256(source.read_bytes()).hexdigest()),
            }
        ],
        "tool_provenance": {"kind": "fusesoc", "version": "2", "contract_version": "1"},
        "cleanup": {"removed": True},
    }
    manifest = finalize_manifest(
        {
            "$schema": "booley.simulation-campaign-manifest/v1",
            "campaign_id": str(uuid4()),
            "created_at": "2026-10-07T10:00:00Z",
            "origin": {"execution_id": ("c" if foreign_origin else "b") * 32, "invocation_id": 7},
            "target": target,
            "workload": workload,
            "required_suite": suite,
            "build_variants": variants,
            "planning_disclosures": [disclosure],
            "prerequisites": [],
            "work_items": items,
        }
    )
    paths = record_paths(layout.control, layout.record.id)
    path = paths.runtime_dir / "flow-reports/sim/7/targets/top/campaign/manifest.json"
    path.parent.mkdir(parents=True)
    path.write_bytes(encode_simulation_campaign_manifest(manifest))
    facts = campaign_facts()
    facts.update(
        campaign_id=manifest.document["campaign_id"], manifest_sha256=manifest_digest(manifest)
    )
    recorder = GoalEvidenceRecorder(bind(layout))
    state = DevelopmentState.load(paths.state_file, recorder.state_persistence())
    shadow = DevelopmentState.from_json_object(state.to_dict())
    changes = shadow.set_criterion(LINT_KEY, True, detail={"warnings": 0})
    transaction = recorder.record_or_verify_transaction(
        state, changes, acceptance_facts=facts, ticket_identity={}
    )
    return source, path, transaction


def test_selected_campaign_generated_build_data_is_materialized_and_frozen(layout):
    source, manifest, transaction = classified(layout)
    call = request(layout)
    result = complete_goal(
        {
            "work_dir": str(layout.worktree),
            "record_id": layout.record.id,
            "operation_id": call.operation_id,
            "summary": call.summary,
        },
        EntryEnvironment(layout.control),
    )
    facts = json.loads(Path(result["package"]).read_bytes())
    row = next(
        row
        for row in facts["input_proof"]["nonversioned_observations"]
        if row["classification"] == "GeneratedBuildInput"
    )
    assert "bytes" not in row
    assert row["materialization"]["size"] == len(source.read_bytes())
    assert row["materialization"]["sha256"] == "sha256:" + row["sha256"]
    assert row["provenance"]["transaction_id"] == transaction.transaction_id
    assert "producer_manifest_bytes" not in row["provenance"]
    assert row["provenance"]["producer_manifest_capture"]["size"] == len(manifest.read_bytes())
    assert (
        facts["goals"][0]["selected_transaction"]["transaction_id"] == transaction.transaction_id
    )
    assert facts["evidence_transactions"][0]["manifest_sha256"]


@pytest.mark.parametrize(
    "refusal", ["missing-proof", "missing-bytes", "different-kind", "substituted-bytes", "hdl"]
)
def test_generated_authorization_refuses_unavailable_unclassified_or_design_inputs(
    layout, refusal
):
    source, manifest, _ = classified(
        layout,
        suffix=".sv" if refusal == "hdl" else ".vmem",
        kind="user" if refusal == "different-kind" else "generated_input",
        wrong_hash=refusal == "substituted-bytes",
    )
    if refusal == "missing-proof":
        manifest.unlink()
    if refusal == "missing-bytes":
        source.unlink()
    with pytest.raises(
        LifecycleError,
        match=r"(committed representation|unavailable|selected producer proof|met and fresh)",
    ):
        finish_goal(request(layout), completion_environment(EntryEnvironment(layout.control)))


@pytest.mark.parametrize("drift", ["generated", "manifest"])
def test_generated_materialization_and_selected_manifest_rechecked_on_recovery(layout, drift):
    source, manifest, _ = classified(layout)
    call = request(layout)
    base = completion_environment(EntryEnvironment(layout.control))
    from dataclasses import replace

    def stop(name):
        if name == "finishing":
            raise Crash()

    with pytest.raises(Crash):
        finish_goal(call, replace(base, on_boundary=stop))
    (source if drift == "generated" else manifest).write_bytes(b"changed after freezing")
    assert finish_goal(call, base)["status"] == "revalidation_required"


@pytest.mark.parametrize("foreign", ["target", "origin", "unselected", "duplicate"])
def test_generated_authorization_is_bound_to_exact_selected_producer(layout, foreign):
    _, manifest, _ = classified(
        layout, foreign_target=foreign == "target", foreign_origin=foreign == "origin"
    )
    if foreign == "unselected":
        from tests.goals.test_status import publish

        publish(layout)
    if foreign == "duplicate":
        duplicate = manifest.parent.with_name("duplicate") / "manifest.json"
        duplicate.parent.mkdir()
        duplicate.write_bytes(manifest.read_bytes())
    with pytest.raises(
        LifecycleError,
        match=r"(another Target|foreign producer|committed representation|ambiguous)",
    ):
        finish_goal(request(layout), completion_environment(EntryEnvironment(layout.control)))


@pytest.mark.parametrize("kind", ["hdl-type", "core-program", "project-program"])
def test_nonstandard_extension_design_and_planner_inputs_still_require_commit(layout, kind):
    if kind == "project-program":
        (layout.worktree / ".booley_project/booley.toml").write_bytes(
            b'[flows.sim]\npre_run_commands=["./prepared.workload"]\n'
        )
    classified(
        layout,
        suffix=".workload",
        hdl_type=kind == "hdl-type",
        planner_program=kind == "core-program",
    )
    with pytest.raises(LifecycleError, match="committed representation"):
        finish_goal(request(layout), completion_environment(EntryEnvironment(layout.control)))
