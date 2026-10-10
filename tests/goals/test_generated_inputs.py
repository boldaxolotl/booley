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
    wrong_size=False,
    foreign_target=False,
    foreign_origin=False,
    hdl_type=False,
    planner_program=False,
    copyto=None,
    source_metadata=False,
    real_producer=False,
    unmapped=False,
    source_override=None,
    disclosure_override=None,
):
    layout.record = enter_goals(layout, [{"family": "lint", "target": "top"}])
    relative = (
        ("dhrystone/dhry.hex" if suffix == ".vmem" else "dhrystone/dhry" + suffix)
        if copyto is not None
        else "prepared" + suffix
    )
    source = layout.worktree / relative
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"00112233\n")
    (layout.main / ".git/info/exclude").write_bytes(f"/.booley_project\n{relative}\n".encode())
    core = layout.worktree / "top.core"
    core.write_bytes(
        core.read_bytes().replace(
            b"files: [rtl.v]",
            f"files: [rtl.v, {{{relative}: {{file_type: {'verilogSource' if hdl_type else 'user'}{', copyto: ' + copyto if copyto is not None else ''}}}}}]".encode(),
        )
    )
    if planner_program:
        core.write_bytes(
            core.read_bytes() + f"scripts:\n  generate: {{cmd: [./{relative}]}}\n".encode()
        )
    if real_producer:
        core.write_text(
            core.read_text().replace(
                "toplevel: top", "toplevel: top, flow: sim, flow_options: {tool: icarus}"
            )
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
                "path": copyto if copyto is not None else relative,
                **(
                    {"source_path": source_override or relative}
                    if source_metadata and not unmapped
                    else {}
                ),
                "kind": kind,
                "bytes": len(source.read_bytes()) + int(wrong_size),
                "sha256": "sha256:"
                + ("0" * 64 if wrong_hash else hashlib.sha256(source.read_bytes()).hexdigest()),
            }
        ],
        "tool_provenance": {"kind": "fusesoc", "version": "2", "contract_version": "1"},
        "cleanup": {"removed": True},
    }
    if real_producer:
        disclosure = _real_disclosure(layout)
    if disclosure_override is not None:
        disclosure = disclosure_override
    source_metadata = source_metadata or disclosure["tool_provenance"]["contract_version"] == "2"
    manifest = finalize_manifest(
        {
            "$schema": "booley.simulation-campaign-manifest/v3"
            if source_metadata
            else "booley.simulation-campaign-manifest/v1",
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


def _real_disclosure(layout):
    from booley.flows.sim.execution import SimulationExecution, SimulationOptions
    from booley.targets.catalog import TargetCatalog
    from tests.flows.sim.test_execution_engine import _subprocess_invoker

    handle = TargetCatalog.build(layout.worktree).select("top", for_flow="sim")
    execution = SimulationExecution(
        invoke=_subprocess_invoker(layout.worktree), options=SimulationOptions(timeout_ms=30_000)
    )
    with execution.ordinary_group(handle, ()) as group:
        return group.planning_disclosure()


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


@pytest.mark.parametrize("copyto", ["dhry.hex", "dhrystone/dhry.hex"])
def test_copyto_generated_source_finish(layout, copyto):
    source, _, _ = classified(layout, copyto=copyto, source_metadata=True)
    result = complete_goal(
        {
            "work_dir": str(layout.worktree),
            "record_id": layout.record.id,
            "operation_id": request(layout).operation_id,
            "summary": "generated proof",
        },
        EntryEnvironment(layout.control),
    )
    facts = json.loads(Path(result["package"]).read_bytes())
    row = next(
        row
        for row in facts["input_proof"]["nonversioned_observations"]
        if row["classification"] == "GeneratedBuildInput"
    )
    assert (
        row["provenance"]["generated_entry"]["source_path"]
        == source.relative_to(layout.worktree).as_posix()
    )


@pytest.mark.parametrize("copyto", ["dhry.hex", "dhrystone/dhry.hex", None])
def test_copyto_real_producer_codec_finish(layout, copyto):
    source, _, _ = classified(layout, copyto=copyto, source_metadata=True, real_producer=True)
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
    assert (
        row["provenance"]["generated_entry"]["source_path"]
        == source.relative_to(layout.worktree).as_posix()
    )


@pytest.mark.parametrize("destination", ["dhry.py", "dhry.vh", "dhry.svh", "dhry.tcl", "dhry.sh"])
def test_copyto_protected_destination_refuses_finish(layout, destination):
    classified(layout, suffix=".gen", copyto=destination, source_metadata=True)
    with pytest.raises(LifecycleError, match="no committed representation"):
        finish_goal(request(layout), completion_environment(EntryEnvironment(layout.control)))


@pytest.mark.parametrize("case", ["legacy-renamed", "v3-unmapped", "traversal"])
def test_copyto_unproven_source_refuses_finish(layout, case):
    if case == "traversal":
        from booley.flows.sim.campaign.codec import SimulationCampaignIntegrityError

        with pytest.raises(SimulationCampaignIntegrityError):
            classified(
                layout, copyto="dhry.hex", source_metadata=True, source_override="../escape.hex"
            )
        return
    classified(
        layout,
        copyto="dhrystone/dhry.hex" if case == "v3-unmapped" else "dhry.hex",
        source_metadata=case != "legacy-renamed",
        unmapped=case == "v3-unmapped",
    )
    with pytest.raises(LifecycleError, match="no committed representation"):
        finish_goal(request(layout), completion_environment(EntryEnvironment(layout.control)))


@pytest.mark.parametrize(
    "suffix",
    [
        ".v",
        ".sv",
        ".vh",
        ".svh",
        ".vhd",
        ".vhdl",
        ".core",
        ".toml",
        ".py",
        ".sh",
        ".tcl",
        ".sdc",
        ".xdc",
    ],
)
def test_v3_protected_source_requires_commit(layout, suffix):
    classified(layout, suffix=suffix, source_metadata=True)
    with pytest.raises(LifecycleError, match=r"(no committed representation|met and fresh)"):
        finish_goal(request(layout), completion_environment(EntryEnvironment(layout.control)))


@pytest.mark.parametrize("drift", ["size", "symlink", "missing-producer", "digest"])
def test_copyto_generated_proof_refuses_live_drift(layout, drift, tmp_path):
    source, producer, _ = classified(
        layout,
        copyto="dhry.hex",
        source_metadata=True,
        wrong_hash=drift == "digest",
        wrong_size=drift == "size",
    )
    if drift == "symlink":
        outside = tmp_path / "outside.hex"
        outside.write_bytes(source.read_bytes())
        source.unlink()
        source.symlink_to(outside)
    elif drift == "missing-producer":
        producer.unlink()
    with pytest.raises(
        LifecycleError,
        match=r"(selected producer proof|escapes|committed representation|met and fresh)",
    ):
        finish_goal(request(layout), completion_environment(EntryEnvironment(layout.control)))


@pytest.mark.parametrize("restriction", ["hdl_type", "planner_program"])
def test_copyto_nonstandard_design_or_program_source_requires_commit(layout, restriction):
    classified(layout, copyto="dhry.hex", source_metadata=True, **{restriction: True})
    with pytest.raises(LifecycleError, match="no committed representation"):
        finish_goal(request(layout), completion_environment(EntryEnvironment(layout.control)))


def test_copyto_source_symlink_escape_rejected_at_proof_boundary(layout, tmp_path):
    from booley.mcp.goal_generated_inputs import _disclosed_inputs

    source, manifest, _ = classified(layout, copyto="dhry.hex", source_metadata=True)
    outside = tmp_path / "outside-data.hex"
    outside.write_bytes(source.read_bytes())
    source.unlink()
    source.symlink_to(outside)
    producer = json.loads(manifest.read_bytes())
    row = {"detail": {"_source_fingerprint": {"target": "top"}}}
    with pytest.raises(LifecycleError, match="escapes the selected worktree"):
        _disclosed_inputs(layout.worktree, producer, row, {})


def test_entirely_ambiguous_producer_cannot_authorize_destination_decoy(layout, tmp_path):
    from dataclasses import replace

    from booley.flows.sim.execution.engine import _SourceDeclaration
    from booley.fusesoc.fusesoc_registry import ResolvedFile
    from tests.flows.sim.test_execution_engine import _handle, _prepared_group_with_sources

    group = _prepared_group_with_sources(_handle(tmp_path / "producer"))
    destination = "dhrystone/dhry.hex"
    staged = group.build_root / destination
    staged.parent.mkdir(parents=True)
    staged.write_bytes(b"00112233\n")
    group._attempt.prepared = replace(
        group._attempt.prepared,
        resolved=replace(
            group._attempt.prepared.resolved, files=(ResolvedFile(destination, "user"),)
        ),
    )
    group._source_declarations = tuple(
        _SourceDeclaration(source, "::top:0", "user", (destination,))
        for source in ("source-a/data.hex", "source-b/data.hex")
    )
    disclosure = group.planning_disclosure()
    assert all("source_path" not in entry for entry in disclosure["generated_files"])
    assert disclosure["tool_provenance"]["contract_version"] == "2"
    # The live Target declares the staged name as an ignored destination decoy;
    # even matching bytes cannot establish the absent source association.
    source, manifest, _ = classified(layout, copyto=destination, disclosure_override=disclosure)
    assert source.read_bytes() == staged.read_bytes()
    assert json.loads(manifest.read_bytes())["$schema"] == "booley.simulation-campaign-manifest/v3"
    with pytest.raises(LifecycleError, match="no committed representation"):
        finish_goal(request(layout), completion_environment(EntryEnvironment(layout.control)))
