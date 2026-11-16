from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path
from types import MappingProxyType, SimpleNamespace

import pytest

from booley.flows.sim.execution.contract import SimulationTestOutcome
from booley.flows.sim.flow import PreparedSimulationEndpoint, SimulateFlow
from booley.flows.sim.record import serial_execution
from booley.flows.sim.record.codec import (
    MANIFEST_SCHEMA,
    SimulationRecordIntegrityError,
    canonical_json_bytes,
    decode_simulation_record_manifest,
    encode_simulation_record_manifest,
)
from booley.flows.sim.record.coordinator import (
    SimulationRecord,
    SimulationRecordRecoveryStatus,
)
from booley.flows.sim.record.model import (
    SimulationRecordPlan,
    create_simulation_record_plan,
)
from booley.flows.sim.record.planning import (
    compare_manifests,
    finalize_manifest,
    manifest_digest,
)
from booley.flows.sim.record.resume import (
    ValidatedManifestNode,
    ValidatedResumeManifest,
    validate_resume_manifest,
)
from booley.flows.sim.record.store import SimulationRecordStore
from booley.flows.sim.record_files import document_digest
from booley.flows.test_runs import build_test_run, test_run_document
from booley.runtime.endpoint_execution import EXIT_ERROR, EndpointOutcome
from booley.targets.catalog import TargetCatalog
from tests.fusesoc_test_support import symlinked_booley_dir
from tests.runtime.test_sandbox_layout import project_alias
from tests.subprocess_budget import SUBPROCESS_TIMEOUT_S, run_bounded_child

__all__ = ["project_alias"]


def _sha(value: object) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _seal_item(item: dict[str, object]) -> None:
    """Recompute a work item's fingerprint and id after its identity fields changed."""
    identity = {
        key: value
        for key, value in item.items()
        if key not in {"fingerprint_sha256", "work_item_id"}
    }
    fingerprint = _sha(identity)
    item["fingerprint_sha256"] = fingerprint
    item["work_item_id"] = "item:0000:" + fingerprint.removeprefix("sha256:")[:16]


def _selection_test_runs(names):
    return [test_run_document(build_test_run(name)) for name in names or (None,)]


def _manifest() -> dict[str, object]:
    target, source_recipe, build_recipe, workload, suite = _manifest_components()
    variants, items = _manifest_work(target, source_recipe, build_recipe, workload)
    manifest = {
        "$schema": MANIFEST_SCHEMA,
        "simulation_record_id": "f47ac10b-58cc-4372-a567-0e02b2c3d479",
        "created_at": "2026-09-21T10:00:00Z",
        "origin": {"execution_id": "", "invocation_id": 1},
        "target": target,
        "workload": workload,
        "required_suite": suite,
        "build_variants": variants,
        "planning_disclosures": [],
        "prerequisites": [],
        "work_items": items,
        "fingerprints": {},
    }
    manifest["fingerprints"] = _manifest_fingerprints(
        manifest, target, source_recipe, build_recipe, variants, items, suite
    )
    return manifest


def _manifest_components():
    target = {
        "vlnv": "acme:lib:dut:1",
        "name": "sim",
        "selector": "sim",
        "project_identity": "project",
        "revision": "abc123",
        "role": "candidate",
        "display_name": "sim",
    }
    source_recipe = {
        "sources": [],
        "parameters": [],
        "defines": [],
        "pre_sim_commands": [],
    }
    build_recipe = {
        "eda_tool": "icarus",
        "toplevel": "tb",
        "arguments": [],
        "command_model_sha256": "sha256:" + "a" * 64,
    }
    workload = {
        "mode": "simulate",
        "trace": False,
        "coverage": False,
        "eda": {"kind": "icarus", "version": "12"},
        "planner_contract_version": "1",
        "adapter_contract_version": "1",
        "pre_sim_build_access": "immutable",
        "run_cwd": {"configured": "run", "kind": "literal", "placeholders": []},
        "runtime_inputs": [],
        "source_recipe": source_recipe,
        "build_recipe": build_recipe,
    }
    suite = {
        "names": [],
        "default_invocation": True,
        "source_path": "",
        "source_bytes": 0,
        "source_sha256": "sha256:" + hashlib.sha256(b"").hexdigest(),
    }
    return target, source_recipe, build_recipe, workload, suite


def _manifest_work(target, source_recipe, build_recipe, workload):
    variant_recipe = {
        "kind": "candidate",
        "source_closure": [],
        "source_recipe": source_recipe,
        "build_recipe": build_recipe,
        "eda": workload["eda"],
        "trace": False,
        "coverage": False,
    }
    variant_sha = _sha(variant_recipe)
    variants = [
        {
            "build_variant_id": "variant:" + variant_sha.removeprefix("sha256:"),
            "kind": "candidate",
            "sharing_eligible": True,
            "source_closure": [],
            "recipe_sha256": variant_sha,
        }
    ]
    item_identity = {
        "ordinal": 0,
        "kind": "ordinary_hdl",
        "role": "candidate",
        "revision": "abc123",
        "target": target,
        "selection": {"kind": "default", "names": []},
        "test_runs": _selection_test_runs(()),
        "arguments": [],
        "build_variant_id": variants[0]["build_variant_id"],
        "run_directory": {"configured": "run", "kind": "literal", "collision_template": "run"},
    }
    item_sha = _sha(item_identity)
    items = [
        {
            "work_item_id": "item:0000:" + item_sha.removeprefix("sha256:")[:16],
            **item_identity,
            "fingerprint_sha256": item_sha,
        }
    ]
    return variants, items


def _manifest_fingerprints(manifest, target, source_recipe, build_recipe, variants, items, suite):
    return {
        "target_recipe_sha256": _sha(
            {"target": target, "source_recipe": source_recipe, "build_recipe": build_recipe}
        ),
        "source_closures_sha256": _sha(
            [{"build_variant_id": variants[0]["build_variant_id"], "source_closure": []}]
        ),
        "required_suite_sha256": _sha(suite),
        "planning_disclosures_sha256": _sha([]),
        "prerequisites_sha256": _sha([]),
        "work_items_sha256": _sha(items),
        "workload_sha256": _sha(
            {
                key: manifest[key]
                for key in (
                    "target",
                    "workload",
                    "required_suite",
                    "build_variants",
                    "planning_disclosures",
                    "prerequisites",
                    "work_items",
                )
            }
        ),
    }


def _baseline_manifest():
    document = _manifest()
    document.pop("fingerprints")
    document["simulation_record_id"] = "550e8400-e29b-41d4-a716-446655440000"
    target = document["target"]
    target.update(  # type: ignore[union-attr]
        {
            "name": "base",
            "selector": "base",
            "revision": "base-rev",
            "role": "cycle_count_baseline",
        }
    )
    item = document["work_items"][0]  # type: ignore[index]
    item.update(  # type: ignore[union-attr]
        {"role": "cycle_count_baseline", "revision": "base-rev", "target": target}
    )
    _seal_item(item)  # type: ignore[arg-type]
    return finalize_manifest(document)


def _prerequisite_entry() -> dict[str, object]:
    """A cycle-count baseline prerequisite entry with literal, self-consistent fields."""
    return {
        "role": "cycle_count_baseline",
        "manifest": {
            "path_base": "origin_invocation",
            "path": "targets/base/record/manifest.json",
            "bytes": 1,
            "sha256": "sha256:" + "a" * 64,
            "kind": "simulation_record_manifest",
            "owner": "550e8400-e29b-41d4-a716-446655440000",
        },
        "simulation_record_id": "550e8400-e29b-41d4-a716-446655440000",
        "target": {**_manifest()["target"], "role": "cycle_count_baseline"},  # type: ignore[dict-item]
        "required_observation": "cycle_count",
        "work_item_id": "item:0000:0123456789abcdef",
    }


def test_finalize_manifest_accepts_immutable_nested_planning_input() -> None:
    document = _manifest()
    document.pop("fingerprints")
    document["planning_disclosures"] = [
        MappingProxyType(
            {
                "planner": "fusesoc_setup",
                "scratch_inputs": (),
                "generated_files": (),
                "tool_provenance": MappingProxyType(
                    {"kind": "fusesoc", "version": "1", "contract_version": "1"}
                ),
                "cleanup": MappingProxyType({"removed": True}),
            }
        )
    ]

    manifest = finalize_manifest(document)

    assert manifest.document["planning_disclosures"][0]["planner"] == "fusesoc_setup"  # type: ignore[index]


def _linked_simulation_record_stores(tmp_path: Path, *, topology_revisions=None):
    baseline = _baseline_manifest()
    if topology_revisions is not None:
        document = json.loads(encode_simulation_record_manifest(baseline))
        document.pop("fingerprints")
        _retarget_topology_manifest(document, topology_revisions[1])
        baseline = finalize_manifest(document)
    invocation = tmp_path / "reports" / "000001"
    baseline_store = SimulationRecordStore(invocation / "targets" / "base-revision" / "record")
    baseline_store.publish_manifest(baseline)
    raw = encode_simulation_record_manifest(baseline)
    baseline_json = json.loads(raw)
    candidate_document = _manifest()
    candidate_document.pop("fingerprints")
    if topology_revisions is not None:
        _retarget_topology_manifest(candidate_document, topology_revisions[0])
    candidate_document["prerequisites"] = [
        {
            "role": "cycle_count_baseline",
            "manifest": {
                "path_base": "origin_invocation",
                "path": baseline_store.manifest_path.relative_to(invocation).as_posix(),
                "bytes": len(raw),
                "sha256": manifest_digest(baseline),
                "kind": "simulation_record_manifest",
                "owner": baseline_json["simulation_record_id"],
            },
            "simulation_record_id": baseline_json["simulation_record_id"],
            "target": baseline_json["target"],
            "required_observation": "cycle_count",
            "work_item_id": baseline_json["work_items"][0]["work_item_id"],
        }
    ]
    candidate = finalize_manifest(candidate_document)
    candidate_store = SimulationRecordStore(invocation / "targets" / "sim" / "record")
    candidate_store.publish_manifest(candidate)
    return candidate, candidate_store, baseline, baseline_store


def test_simulation_record_plan_is_derived_only_from_a_validated_manifest() -> None:
    manifest = decode_simulation_record_manifest(canonical_json_bytes(_manifest()))
    plan = create_simulation_record_plan(manifest)
    assert plan.work_item_ids == (manifest.document["work_items"][0]["work_item_id"],)  # type: ignore[index]
    with pytest.raises(TypeError, match="create_simulation_record_plan"):
        SimulationRecordPlan()
    with pytest.raises(TypeError):
        SimulationRecordPlan(manifest, ("item:0000:ffffffffffffffff",))  # type: ignore[call-arg]


def test_resume_binds_distinct_candidate_and_baseline_revision_roots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    candidate, candidate_store, baseline, baseline_store = _linked_simulation_record_stores(
        tmp_path
    )
    candidate_root = tmp_path / "candidate-source"
    baseline_root = tmp_path / "baseline-source"
    candidate_root.mkdir()
    baseline_root.mkdir()

    class Catalog:
        def __init__(self, root: Path) -> None:
            self.root = root

        def select(self, selector: str, *, for_flow: str):
            assert for_flow == "sim"
            target = (
                candidate.document["target"] if selector == "sim" else baseline.document["target"]
            )
            return SimpleNamespace(
                identity=f"{target['vlnv']}#{target['name']}",
                selector=selector,
                project_root=self.root,
            )

    monkeypatch.setattr(TargetCatalog, "build", lambda root: Catalog(Path(root)))
    roots = {"candidate": candidate_root, "cycle_count_baseline": baseline_root}
    revisions = {candidate_root.resolve(): "abc123", baseline_root.resolve(): "base-rev"}
    monkeypatch.setattr(
        "booley.flows.sim.record.resume.git_full_sha",
        lambda _ref, root: revisions[Path(root).resolve()],
    )
    validated = validate_resume_manifest(
        candidate_store.manifest_path,
        project_root=candidate_root,
        revision_root=lambda target: roots[target["role"]],
    )

    assert [binding.project_root for binding in validated.bindings] == [
        candidate_root,
        baseline_root,
    ]
    reference = candidate.document["prerequisites"][0]["manifest"]  # type: ignore[index]
    assert validated.prerequisite_for(reference).path == baseline_store.manifest_path


def _stub_resume_target(monkeypatch: pytest.MonkeyPatch, manifest) -> None:
    """Make resume see *manifest*'s one Target at its recorded revision in the checkout it is given."""
    target = manifest.document["target"]
    monkeypatch.setattr(
        "booley.flows.sim.record.resume.git_full_sha", lambda *_args: target["revision"]
    )
    monkeypatch.setattr(
        TargetCatalog,
        "build",
        lambda root: SimpleNamespace(
            select=lambda *_args, **_kwargs: SimpleNamespace(
                identity=f"{target['vlnv']}#{target['name']}", project_root=Path(root)
            )
        ),
    )


@pytest.mark.parametrize("defect", ["ancestor_symlink", "final_symlink", "hardlink"])
def test_resume_rejects_linked_manifest_paths_without_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, defect: str
) -> None:
    manifest = decode_simulation_record_manifest(canonical_json_bytes(_manifest()))
    real = SimulationRecordStore(tmp_path / "real/000001/targets/sim/record")
    real.publish_manifest(manifest)
    view = tmp_path / "view/000001/targets/sim/record"
    if defect == "ancestor_symlink":
        (tmp_path / "view").mkdir()
        (tmp_path / "view/000001").symlink_to(real.root.parents[2], target_is_directory=True)
    else:
        view.mkdir(parents=True)
        if defect == "final_symlink":
            (view / "manifest.json").symlink_to(real.manifest_path)
        else:
            (view / "manifest.json").hardlink_to(real.manifest_path)
    _stub_resume_target(monkeypatch, manifest)
    before = sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*"))

    with pytest.raises(SimulationRecordIntegrityError, match=r"regular|link"):
        validate_resume_manifest(view / "manifest.json", project_root=tmp_path / "source")

    assert sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*")) == before


@pytest.mark.skipif(os.name == "nt", reason="Windows publishes by rename, never by link")
def test_resume_validation_discards_a_staged_link_of_the_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A kill between link and unlink leaves the manifest whole with a second link."""
    manifest = decode_simulation_record_manifest(canonical_json_bytes(_manifest()))
    published = SimulationRecordStore(tmp_path / "reports/000001/targets/sim/record")
    published.publish_manifest(manifest)
    staged = published.manifest_path.with_name(".manifest.json.x.booley-staged")
    os.link(published.manifest_path, staged)
    _stub_resume_target(monkeypatch, manifest)

    validated = validate_resume_manifest(published.manifest_path, project_root=tmp_path)

    assert validated.path == published.manifest_path
    assert not staged.exists()


@pytest.mark.parametrize("spelling", ["lexical", "physical", "below"])
def test_resume_reads_through_the_booley_dir_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, spelling: str
) -> None:
    """#2191: resume follows a symlinked ``.booley`` entry, in either spelling, and no deeper."""
    root = tmp_path.resolve()
    project = root / "project"
    project.mkdir()
    external = symlinked_booley_dir(project, root / "external-booley")
    entry = project / ".booley"
    manifest = decode_simulation_record_manifest(canonical_json_bytes(_manifest()))
    # Publish through the physical spelling: only resume itself crosses the entry.
    reports = external / "flow-reports"
    if spelling == "below":
        # ``flow-reports`` below the entry is a link: refused exactly as today.
        (root / "real-reports").mkdir()
        reports.symlink_to(root / "real-reports", target_is_directory=True)
        reports = root / "real-reports"
    published = SimulationRecordStore(reports / "000001/targets/sim/record")
    published.publish_manifest(manifest)
    lexical = entry / "flow-reports" / published.manifest_path.relative_to(reports)
    physical = external / "flow-reports" / published.manifest_path.relative_to(reports)
    _stub_resume_target(monkeypatch, manifest)

    if spelling == "below":
        with pytest.raises(SimulationRecordIntegrityError, match="not a regular file"):
            validate_resume_manifest(lexical, project_root=project)
        return
    validated = validate_resume_manifest(
        lexical if spelling == "lexical" else physical, project_root=project
    )

    # A copied physical path yields the same lexical origin, and recovery reads through it.
    assert validated.path == lexical
    assert validated.booley_dir == entry
    status = SimulationRecord().inspect_resume(validated)
    assert isinstance(status, SimulationRecordRecoveryStatus)


def test_simulation_record_store_rejects_linked_root_before_lock_write(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    linked = tmp_path / "linked"
    linked.symlink_to(outside, target_is_directory=True)
    store = SimulationRecordStore(linked / "record")

    with (
        pytest.raises(SimulationRecordIntegrityError, match="contains a link"),
        store.mutation_lock(),
    ):
        pass

    assert not (outside / "record").exists()


def _source_equal_revision_drift(tmp_path: Path) -> tuple[Path, str]:
    root = tmp_path / "source"
    root.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=root, check=True, timeout=SUBPROCESS_TIMEOUT_S)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=root,
        check=True,
        timeout=SUBPROCESS_TIMEOUT_S,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"], cwd=root, check=True, timeout=SUBPROCESS_TIMEOUT_S
    )
    (root / "source.sv").write_text("module source; endmodule\n", encoding="utf-8")
    subprocess.run(["git", "add", "source.sv"], cwd=root, check=True, timeout=SUBPROCESS_TIMEOUT_S)
    subprocess.run(
        ["git", "commit", "-qm", "source"], cwd=root, check=True, timeout=SUBPROCESS_TIMEOUT_S
    )
    original = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
        timeout=SUBPROCESS_TIMEOUT_S,
    ).stdout.strip()
    subprocess.run(
        ["git", "commit", "--allow-empty", "-qm", "drift"],
        cwd=root,
        check=True,
        timeout=SUBPROCESS_TIMEOUT_S,
    )
    return root, original


def _publish_stale_candidate_manifest(tmp_path: Path, revision: str) -> SimulationRecordStore:
    document = _manifest()
    target = dict(document["target"])  # type: ignore[arg-type]
    target["revision"] = revision
    document["target"] = target
    items = [dict(item) for item in document["work_items"]]  # type: ignore[arg-type]
    items[0]["target"] = target
    items[0]["revision"] = revision
    _seal_item(items[0])
    document["work_items"] = items
    document.pop("fingerprints")
    manifest = finalize_manifest(document)
    store = SimulationRecordStore(tmp_path / "000001" / "targets" / "sim" / "record")
    store.publish_manifest(manifest)
    return store


def test_resume_rejects_stale_candidate_revision_with_source_equal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, original = _source_equal_revision_drift(tmp_path)
    store = _publish_stale_candidate_manifest(tmp_path, original)

    class Catalog:
        def select(self, selector: str, *, for_flow: str):
            assert (selector, for_flow) == ("sim", "sim")
            return SimpleNamespace(
                identity="acme:lib:dut:1#sim",
                selector=selector,
                project_root=root,
            )

    monkeypatch.setattr(TargetCatalog, "build", lambda _root: Catalog())

    flow = SimulateFlow()
    flow._args = SimpleNamespace(resume_from=store.manifest_path, work_dir=root, coverage=False)

    outcome = flow.prepare_simulation_endpoint()

    assert isinstance(outcome, EndpointOutcome)
    assert outcome.exit_code == EXIT_ERROR
    assert "revision" in outcome.report_text


@pytest.mark.parametrize("kind", ["ordinary_hdl", "cocotb_batch"])
def test_resume_of_a_non_coverage_record_with_an_interrupted_item_is_not_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    """#962: only coverage aggregates are refused for unfinished work; HDL resumes are not inspected."""
    document = _manifest()
    document.pop("fingerprints")
    item = document["work_items"][0]  # type: ignore[index]
    item["kind"] = kind
    _seal_item(item)
    manifest = finalize_manifest(document)
    store = SimulationRecordStore(tmp_path / "record")
    store.publish_manifest(manifest)
    work_item_id = manifest.document["work_items"][0]["work_item_id"]  # type: ignore[index]
    store.allocate_attempt_directory(work_item_id, "550e8400-e29b-41d4-a716-446655440001")
    assert store.scan().interrupted == (work_item_id,)
    validated = ValidatedResumeManifest(
        ValidatedManifestNode(store.manifest_path, manifest, "0" * 64), (), ()
    )

    def forbidden(*_args: object) -> None:
        raise AssertionError("non-coverage resume must not be inspected by the guard")

    monkeypatch.setattr(
        "booley.flows.sim.flow.validate_resume_manifest", lambda *_a, **_k: validated
    )
    monkeypatch.setattr("booley.flows.sim.flow_resume.SimulationRecord.inspect_resume", forbidden)
    flow = SimulateFlow()
    flow._args = SimpleNamespace(
        resume_from=store.manifest_path, work_dir=tmp_path, coverage=False
    )

    prepared = flow.prepare_simulation_endpoint()

    assert isinstance(prepared, PreparedSimulationEndpoint)
    assert prepared.resume is validated


def test_serial_result_classifies_simulator_crash_as_design_failure() -> None:
    test = SimulationTestOutcome(
        name="smoke",
        verdict="crash",
        passed=False,
        crashed=True,
        reason="terminated by signal 11",
    )
    observation = serial_execution.observation_document(test)
    assert serial_execution.settled_state([observation]) == "crash"
    assert observation["execution"] == "crash"
    assert observation["failure_class"] == "design"
    assert observation["functional"] == "not_observed"


def _retarget_topology_manifest(document, revision):
    target = document["target"]
    target.update(vlnv="acme:lib:top:1", name="sim_core", selector="sim_core", revision=revision)
    item = document["work_items"][0]
    item.update(target=target, revision=revision)
    _seal_item(item)


@pytest.mark.parametrize("topology", ["standalone", "linked"])
def test_resume_baseline_materializes_project_topologies(tmp_path, monkeypatch, topology):
    from booley.flows.sim.mode import SimulationMode
    from tests.flows.test_baseline_worktree import (
        _assert_topology_baseline,
        _project_topology_checkout,
    )

    root, revision = _project_topology_checkout(tmp_path, monkeypatch, topology)
    current = run_bounded_child(["git", "rev-parse", "HEAD"], cwd=root, check=True).stdout.strip()
    _candidate, store, _baseline, baseline_store = _linked_simulation_record_stores(
        tmp_path, topology_revisions=(current, revision)
    )
    flow = SimulateFlow()
    flow._args = SimpleNamespace(
        work_dir=root,
        resume_from=store.manifest_path,
        mode=SimulationMode.SIMULATE,
        coverage=False,
    )
    roots = []

    def validate(path, *, project_root, revision_root):
        def inspect(target):
            historical = revision_root(target)
            if target["role"] != "candidate":
                _assert_topology_baseline(historical, root, "sim_core")
                assert revision_root(target) == historical
                roots.append(historical)
            return historical

        return validate_resume_manifest(path, project_root=project_root, revision_root=inspect)

    monkeypatch.setattr("booley.flows.sim.flow.validate_resume_manifest", validate)
    try:
        prepared = flow.prepare_simulation_endpoint()
        assert isinstance(prepared, PreparedSimulationEndpoint)
        validated = prepared.resume
        assert validated is not None
        handles = prepared.targets
        assert handles == validated.target_handles
        assert handles[0].project_root == root
        assert handles[1].identity == "acme:lib:top:1#sim_core"
        assert handles[1].project_root == roots[0]
        assert validated.prerequisites[0].path == baseline_store.manifest_path
        assert prepared.selected_targets == ()
        assert prepared.test_names_map == {}
        assert roots[0].exists()
    finally:
        flow.publication_resources.close()
    assert not roots[0].exists()


def _diagnostic_document(*, sources=(), parameters=(), names=()):
    document = _manifest()
    document.pop("fingerprints")
    document["workload"]["source_recipe"]["sources"] = list(sources)
    document["workload"]["source_recipe"]["parameters"] = list(parameters)
    document["build_variants"][0]["source_closure"] = list(sources)
    document["required_suite"]["names"] = list(names)
    document["required_suite"]["default_invocation"] = not names
    if names:
        document["required_suite"]["source_path"] = ".booley/tests.toml"
        document["work_items"][0]["kind"] = "cocotb_batch"
        document["work_items"][0]["selection"] = {"kind": "unfiltered", "names": []}
    return document


def _diagnostic_source(path, *, digest="a", size=1):
    return {"path": path, "bytes": size, "sha256": "sha256:" + digest * 64, "kind": "testbench"}


def _finalize_diagnostic_document(document):
    workload = document["workload"]
    variant = document["build_variants"][0]
    recipe = {
        "kind": variant["kind"],
        "source_closure": variant["source_closure"],
        "source_recipe": workload["source_recipe"],
        "build_recipe": workload["build_recipe"],
        "eda": workload["eda"],
        "trace": workload["trace"],
        "coverage": workload["coverage"],
    }
    variant["recipe_sha256"] = _sha(recipe)
    variant["build_variant_id"] = "variant:" + _sha(recipe)[7:]
    item = document["work_items"][0]
    item["target"] = document["target"]
    item["revision"] = document["target"]["revision"]
    item["build_variant_id"] = variant["build_variant_id"]
    _seal_item(item)
    return finalize_manifest(document)


def _project_diagnostics(old, new):
    from booley.flows.sim.record.planning import (
        SimulationRecordWorkloadMismatchError,
        verify_workload,
    )

    left, right = _finalize_diagnostic_document(old), _finalize_diagnostic_document(new)
    raw = compare_manifests(left, right)
    with pytest.raises(SimulationRecordWorkloadMismatchError) as exc:
        verify_workload(left, right)
    assert exc.value.diagnostic.mismatches == raw
    assert exc.value.diagnostic.detail["mismatches"] == [item.message for item in raw]
    return exc.value.diagnostic


def test_manifest_with_recorded_flow_option_defines_still_decodes_and_compares():
    """A Simulation Record stored with `source_recipe.defines` stays readable; resume names the cause."""
    import copy

    legacy_doc = _diagnostic_document()
    legacy_doc["workload"]["source_recipe"]["defines"] = ["LEGACY=1"]
    current_doc = _diagnostic_document()

    stored = _finalize_diagnostic_document(copy.deepcopy(legacy_doc))
    encoded = encode_simulation_record_manifest(stored)
    assert encode_simulation_record_manifest(decode_simulation_record_manifest(encoded)) == (
        encoded
    )
    assert compare_manifests(stored, stored) == ()

    diagnostic = _project_diagnostics(legacy_doc, current_doc)
    assert diagnostic.mismatch_summary == (
        "recorded flow_options.defines is no longer recorded (no Flow applies it); "
        "re-plan the Simulation Record",
    )


def _sources(*paths: str, size: int = 1) -> list[dict[str, object]]:
    return [_diagnostic_source(path, size=size) for path in paths]


@pytest.mark.parametrize(
    ("old_sources", "new_sources", "lines"),
    [
        (_sources("a.sv"), _sources("head.sv", "a.sv"), ("source added: head.sv",)),
        (_sources("a.sv", "b.sv"), _sources("b.sv"), ("source removed: a.sv",)),
        (
            _sources("a.sv"),
            _sources("b.sv"),
            ("source removed: a.sv", "source added: b.sv"),
        ),
        (_sources("a.sv", "b.sv"), _sources("b.sv", "a.sv"), ("source order changed",)),
        # Control characters in a path are escaped in the label.
        (_sources("a\n.sv"), _sources("a\n.sv", size=2), ('source changed: "a\\n.sv"',)),
    ],
)
def test_source_diagnostics_align_paths_across_positional_diff(old_sources, new_sources, lines):
    old = _diagnostic_document(sources=old_sources)
    new = _diagnostic_document(sources=new_sources)
    diagnostic = _project_diagnostics(old, new)
    assert set(diagnostic.mismatch_summary) == set(lines)
    assert diagnostic.derived_fingerprint_count == 5


@pytest.mark.parametrize("duplicate_digest", ["a", "b"])
def test_duplicate_source_paths_preserve_second_entry_evidence(duplicate_digest):
    old = _diagnostic_document(
        sources=[_diagnostic_source("a.sv"), _diagnostic_source("a.sv", digest=duplicate_digest)]
    )
    new = json.loads(json.dumps(old))
    new["workload"]["source_recipe"]["sources"][1]["bytes"] = 2
    new["build_variants"][0]["source_closure"][1]["bytes"] = 2
    diagnostic = _project_diagnostics(old, new)
    assert "source paths ambiguous: duplicate source path" in diagnostic.mismatch_summary
    assert any("/sources/1/bytes" in line for line in diagnostic.mismatch_summary)
    assert any("/source_closure/1/bytes" in line for line in diagnostic.mismatch_summary)
    assert diagnostic.derived_fingerprint_count == 5


@pytest.mark.parametrize(
    ("old_parameters", "new_parameters"),
    [
        ([{"name": "N", "value": 1}, {"name": "N", "value": 2}], [{"name": "N", "value": 1}]),
        ([{"name": {}, "value": 1}], [{"name": "N", "value": 1}]),
    ],
    ids=["duplicate-name", "non-string-name"],
)
def test_ambiguous_parameter_names_report_ambiguity_instead_of_crashing(
    old_parameters, new_parameters
):
    old = _diagnostic_document(parameters=old_parameters)
    new = _diagnostic_document(parameters=new_parameters)
    diagnostic = _project_diagnostics(old, new)
    assert (
        "parameter names ambiguous: duplicate or non-string parameter name"
        in diagnostic.mismatch_summary
    )


def test_conflicting_source_replica_remains_visible():
    old = _diagnostic_document(sources=[_diagnostic_source("a.sv")])
    new = json.loads(json.dumps(old))
    new["workload"]["source_recipe"]["sources"][0]["bytes"] = 2
    new["build_variants"][0]["source_closure"][0]["bytes"] = 3
    diagnostic = _project_diagnostics(old, new)
    assert "source changed: a.sv" in diagnostic.mismatch_summary
    assert any("/source_closure/0/bytes" in line for line in diagnostic.mismatch_summary)


@pytest.mark.parametrize(
    ("old_names", "new_names", "line"),
    [
        (("a", "b"), ("b", "c"), "suite changed: +c -a"),
        (("a", "b"), ("b", "a"), "suite order changed"),
    ],
)
def test_suite_names_are_manifest_projection_cases(old_names, new_names, line):
    diagnostic = _project_diagnostics(
        _diagnostic_document(names=old_names), _diagnostic_document(names=new_names)
    )
    assert diagnostic.mismatch_summary == (line,)


@pytest.mark.parametrize(
    ("old_parameters", "new_parameters", "line"),
    [
        (
            ({"name": "a/~b", "value": False},),
            ({"name": "a/~b", "value": "false"},),
            'parameter a/~b: false → "false"',
        ),
        ((), ({"name": "N", "value": "<absent>"},), 'parameter N: <absent> → "<absent>"'),
        ((), ({"name": "N", "value": 2},), "parameter N: <absent> → 2"),
        (({"name": "N", "value": 2},), (), "parameter N: 2 → <absent>"),
        (
            ({"name": "shape", "value": {"x": [1, False]}},),
            ({"name": "shape", "value": {"x": [2, True]}},),
            'parameter shape: {"x":[1,false]} → {"x":[2,true]}',
        ),
        (
            ({"name": "A", "value": 1}, {"name": "B", "value": 2}),
            ({"name": "B", "value": 2}, {"name": "A", "value": 1}),
            "parameter order changed",
        ),
    ],
)
def test_parameter_diagnostics_preserve_values_and_absence(old_parameters, new_parameters, line):
    diagnostic = _project_diagnostics(
        _diagnostic_document(parameters=old_parameters),
        _diagnostic_document(parameters=new_parameters),
    )
    assert diagnostic.mismatch_summary == (line,)


def test_opaque_command_model_digest_is_a_named_root_cause():
    old, new = _diagnostic_document(), _diagnostic_document()
    new["workload"]["build_recipe"]["command_model_sha256"] = "sha256:" + "b" * 64
    diagnostic = _project_diagnostics(old, new)
    assert diagnostic.mismatch_summary == ("build command model changed",)
    assert diagnostic.derived_fingerprint_count == 5


def test_fingerprint_only_report_retains_details():
    from booley.flows.sim.record.planning import WorkloadDiagnostic, WorkloadMismatch

    finding = WorkloadMismatch("/build_variants/0/recipe_sha256", "sha256:old", "sha256:new")
    diagnostic = WorkloadDiagnostic((finding,), (), 1)
    assert "no root cause identified" in diagnostic.report()
    assert "1 derived fingerprints differ" in diagnostic.report()
    assert finding.message in diagnostic.report(verbose=True)


def test_target_revision_fallback_collapses_verified_replicas():
    old, new = _diagnostic_document(), _diagnostic_document()
    new["target"]["revision"] = "changed"
    diagnostic = _project_diagnostics(old, new)
    assert len(diagnostic.mismatch_summary) == 1
    assert "/target/revision" in diagnostic.mismatch_summary[0]
    assert diagnostic.derived_fingerprint_count == 2


def test_zero_mismatch_verification_remains_successful():
    from booley.flows.sim.record.planning import project_workload_mismatches, verify_workload

    manifest = finalize_manifest(_diagnostic_document())
    verify_workload(manifest, manifest)
    diagnostic = project_workload_mismatches(manifest, manifest)
    assert diagnostic.detail == {
        "mismatches": [],
        "mismatch_summary": [],
        "derived_fingerprint_count": 0,
    }


@pytest.mark.parametrize("region", ["planning_disclosures", "prerequisites"])
def test_independent_container_additions_preserve_raw_fallback(region):
    old, new = _diagnostic_document(), _diagnostic_document()
    if region == "planning_disclosures":
        new[region] = [
            {
                "planner": "setup",
                "scratch_inputs": [],
                "generated_files": [],
                "tool_provenance": {"kind": "fusesoc", "version": "1", "contract_version": "1"},
                "cleanup": {"removed": True},
            }
        ]
    else:
        new[region] = [_prerequisite_entry()]
    diagnostic = _project_diagnostics(old, new)
    assert len(diagnostic.mismatch_summary) == 1
    assert (
        diagnostic.mismatch_summary[0] == "workload changed: " + diagnostic.mismatches[0].message
    )
    assert f"/{region}/0" in diagnostic.mismatch_summary[0]
    assert diagnostic.derived_fingerprint_count == 0


@pytest.mark.parametrize("field", ["sharing_eligible", "eda_version", "run_cwd"])
def test_unrecognized_identity_changes_remain_visible(field):
    old, new = _diagnostic_document(), _diagnostic_document()
    if field == "sharing_eligible":
        new["build_variants"][0][field] = False
        pointer = "/build_variants/0/sharing_eligible"
    elif field == "eda_version":
        new["workload"]["eda"]["version"] = "13"
        pointer = "/workload/eda/version"
    else:
        new["workload"]["run_cwd"]["configured"] = "other"
        pointer = "/workload/run_cwd/configured"
    diagnostic = _project_diagnostics(old, new)
    assert any(pointer in line for line in diagnostic.mismatch_summary)


def _runtime_inputs(*pairs: tuple[str, str]) -> list[dict[str, str]]:
    """Runtime input declarations for ``(destination, source_artifact_path)`` pairs."""
    declarations = []
    for destination, source in pairs:
        declaration = {"destination": destination, "source_artifact_path": source}
        declarations.append({**declaration, "declaration_id": document_digest(declaration)})
    return declarations


@pytest.mark.parametrize(
    ("old_inputs", "new_inputs", "line", "derived_count"),
    [
        (
            _runtime_inputs(("input/data.bin", "old/data.bin")),
            _runtime_inputs(("input/data.bin", "new/data.bin")),
            "runtime input changed: input/data.bin",
            1,
        ),
        (
            _runtime_inputs(("a", "a")),
            _runtime_inputs(("head", "head"), ("a", "a")),
            "runtime input added: head",
            0,
        ),
        (
            _runtime_inputs(("a", "a"), ("b", "b")),
            _runtime_inputs(("b", "b")),
            "runtime input removed: a",
            0,
        ),
        (
            _runtime_inputs(("a", "a"), ("b", "b")),
            _runtime_inputs(("b", "b"), ("a", "a")),
            "runtime input order changed",
            0,
        ),
    ],
)
def test_runtime_declarations_are_aligned_by_destination(
    old_inputs, new_inputs, line, derived_count
):
    old, new = _diagnostic_document(), _diagnostic_document()
    old["workload"]["runtime_inputs"] = old_inputs
    new["workload"]["runtime_inputs"] = new_inputs
    diagnostic = _project_diagnostics(old, new)
    assert diagnostic.mismatch_summary == (line,)
    assert diagnostic.derived_fingerprint_count == derived_count


def _change_source_size(document: dict[str, object]) -> None:
    document["workload"]["source_recipe"]["sources"][0]["bytes"] = 2  # type: ignore[index]


def _change_target_revision(document: dict[str, object]) -> None:
    document["target"]["revision"] = "changed"  # type: ignore[index]


@pytest.mark.parametrize(
    ("mutate", "pointers", "detail"),
    [
        (
            _change_source_size,
            [
                "/build_variants/0/build_variant_id",
                "/build_variants/0/recipe_sha256",
                "/build_variants/0/source_closure/0/bytes",
                "/work_items/0/build_variant_id",
                "/work_items/0/fingerprint_sha256",
                "/work_items/0/work_item_id",
                "/workload/source_recipe/sources/0/bytes",
            ],
            (
                "/workload/source_recipe/sources/0/bytes",
                1,
                2,
                "/workload/source_recipe/sources/0/bytes: manifest has 1, current workload has 2",
            ),
        ),
        # A resume preview reports every place a changed revision is recorded.
        (
            _change_target_revision,
            [
                "/target/revision",
                "/work_items/0/fingerprint_sha256",
                "/work_items/0/revision",
                "/work_items/0/target/revision",
                "/work_items/0/work_item_id",
            ],
            (
                "/work_items/0/target/revision",
                "abc123",
                "changed",
                '/work_items/0/target/revision: manifest has "abc123", current workload has "changed"',
            ),
        ),
    ],
    ids=["source-bytes", "target-revision"],
)
def test_raw_comparator_pointer_value_contract_is_unchanged(mutate, pointers, detail):
    old = _diagnostic_document(sources=[_diagnostic_source("a.sv")])
    new = _diagnostic_document(sources=[_diagnostic_source("a.sv")])
    mutate(new)
    left, right = _finalize_diagnostic_document(old), _finalize_diagnostic_document(new)
    raw = compare_manifests(left, right)
    assert [item.pointer for item in raw] == pointers
    pointer, expected, actual, message = detail
    (finding,) = [item for item in raw if item.pointer == pointer]
    assert (finding.expected, finding.actual) == (expected, actual)
    assert finding.message == message


def test_independent_work_item_arguments_remain_visible():
    old, new = _diagnostic_document(), _diagnostic_document()
    new["work_items"][0]["arguments"] = ["+extra"]
    diagnostic = _project_diagnostics(old, new)
    assert any("/work_items/0/arguments/0" in line for line in diagnostic.mismatch_summary)
    assert diagnostic.derived_fingerprint_count == 2


def test_diagnostic_projection_needs_no_path_read_bytes_or_text(monkeypatch):
    from booley.flows.sim.record.planning import project_workload_mismatches

    old = _diagnostic_document(sources=[_diagnostic_source("relative/a.sv")])
    new = _diagnostic_document(sources=[_diagnostic_source("relative/a.sv", size=2)])
    left, right = _finalize_diagnostic_document(old), _finalize_diagnostic_document(new)

    def forbidden_read(*_args, **_kwargs):
        pytest.fail("diagnostic projection must not read files")

    monkeypatch.setattr(Path, "read_bytes", forbidden_read)
    monkeypatch.setattr(Path, "read_text", forbidden_read)
    diagnostic = project_workload_mismatches(left, right)
    assert diagnostic.mismatch_summary == ("source changed: relative/a.sv",)


@pytest.mark.parametrize(
    ("old_value", "new_value", "line"),
    [
        (False, 0, "parameter N: false → 0"),
        (True, 1, "parameter N: true → 1"),
        (1, 1.0, "parameter N: 1 → 1.0"),
        ([1], [True], "parameter N: [1] → [true]"),
        ({"x": 0}, {"x": False}, 'parameter N: {"x":0} → {"x":false}'),
    ],
)
def test_typed_json_parameter_causes_preserve_static_digest_raw_comparison(
    old_value, new_value, line
):
    old = _diagnostic_document(parameters=[{"name": "N", "value": old_value}])
    new = _diagnostic_document(parameters=[{"name": "N", "value": new_value}])
    diagnostic = _project_diagnostics(old, new)
    assert diagnostic.mismatch_summary == (line,)
    assert diagnostic.derived_fingerprint_count == 5
    assert not any(
        item.pointer.startswith("/workload/source_recipe/parameters")
        for item in diagnostic.mismatches
    )


def _prepared_disclosure(sources):
    return {
        "planner": "fusesoc_setup",
        "scratch_inputs": [],
        "generated_files": [{**source, "kind": "generated_input"} for source in sources],
        "tool_provenance": {"kind": "fusesoc", "version": "1", "contract_version": "1"},
        "cleanup": {"removed": True},
    }


def _prepared_diagnostic_pair():
    old = _diagnostic_document(sources=[_diagnostic_source("a.sv")])
    new = _diagnostic_document(sources=[_diagnostic_source("a.sv", size=2, digest="b")])
    for document in (old, new):
        document["planning_disclosures"] = [
            _prepared_disclosure(document["workload"]["source_recipe"]["sources"])
        ]
    return old, new


@pytest.mark.parametrize(
    "case",
    [
        json.loads(line)
        for line in (Path(__file__).parent / "fixtures/v4_manifest_pairs.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ],
    ids=lambda case: case["id"],
)
def test_prepared_manifest_pair_corpus(case):
    from booley.flows.sim.record.planning import (
        SimulationRecordWorkloadMismatchError,
        project_workload_mismatches,
        verify_workload,
    )

    left, right = [
        decode_simulation_record_manifest(canonical_json_bytes(case[key]))
        for key in ("expected", "current")
    ]
    raw = compare_manifests(left, right)
    assert canonical_json_bytes(
        [[item.pointer, item.expected, item.actual, item.message] for item in raw]
    ) == canonical_json_bytes(case["raw"])
    assert [manifest_digest(left), manifest_digest(right)] == case["digests"]
    diagnostic = project_workload_mismatches(left, right, raw)
    assert project_workload_mismatches(left, right) == diagnostic
    assert diagnostic.detail == case["detail"]
    assert diagnostic.report() == case["report"]
    assert diagnostic.report(verbose=True) == case["verbose_report"]
    assert diagnostic.report(verbose=True) == diagnostic.report() + case["raw_verbose_suffix"]
    if case["outcome"] == "accept":
        verify_workload(left, right)
    else:
        with pytest.raises(SimulationRecordWorkloadMismatchError) as exc:
            verify_workload(left, right)
        assert exc.value.diagnostic == diagnostic


def test_explicit_zero_findings_produces_no_typed_parameter_roots():
    from booley.flows.sim.record.planning import project_workload_mismatches

    old = _finalize_diagnostic_document(
        _diagnostic_document(parameters=[{"name": "N", "value": False}])
    )
    new = _finalize_diagnostic_document(
        _diagnostic_document(parameters=[{"name": "N", "value": 0}])
    )
    diagnostic = project_workload_mismatches(old, new, ())
    assert diagnostic.mismatch_summary == ()
    assert diagnostic.derived_fingerprint_count == 0


def test_unrecognized_pointers_keep_every_raw_fallback_in_order():
    from booley.flows.sim.record.planning import WorkloadMismatch, project_workload_mismatches

    manifest = _finalize_diagnostic_document(_diagnostic_document())
    derived = ("/build_variants/0/recipe_sha256",) * 2
    unknown = ("/", "//child", "/required_suites", "/build_variants/01/recipe_sha256", "/x", "/x")
    findings = tuple(WorkloadMismatch(pointer, "old", "new") for pointer in derived + unknown)
    diagnostic = project_workload_mismatches(manifest, manifest, findings)
    assert diagnostic.mismatch_summary == tuple(
        f"workload changed: {item.message}" for item in findings[2:]
    )
    assert diagnostic.derived_fingerprint_count == 2


def test_parameter_display_escapes_nested_controls_without_changing_raw_values():
    old = _diagnostic_document(parameters=[{"name": "N", "value": {"x": ["safe"]}}])
    controls = "\u0085\u202e\u2066\u2028\u2029"
    new = _diagnostic_document(parameters=[{"name": "N", "value": {"x": [controls]}}])
    diagnostic = _project_diagnostics(old, new)
    assert diagnostic.mismatch_summary == (
        'parameter N: {"x":["safe"]} → {"x":["\\u0085\\u202e\\u2066\\u2028\\u2029"]}',
    )
    assert not any(character in diagnostic.report() for character in controls)
    assert any(item.actual == controls for item in diagnostic.mismatches)
    assert diagnostic.detail["mismatches"] == [item.message for item in diagnostic.mismatches]


def test_copyto_prepared_digest_diagnostic_uses_declared_source():
    old, new = _prepared_diagnostic_pair()
    for document in (old, new):
        entry = document["planning_disclosures"][0]["generated_files"][0]
        entry["source_path"] = entry["path"]
        entry["path"] = "renamed.sv"
    diagnostic = _project_diagnostics(old, new)
    assert diagnostic.mismatch_summary == ("source changed: a.sv",)
    new["planning_disclosures"][0]["generated_files"][0]["source_path"] = "other.sv"
    assert any("source_path" in line for line in _project_diagnostics(old, new).mismatch_summary)


def test_image_alias_resume_returns_canonical_manifest(project_alias, monkeypatch):
    alias, root = project_alias
    manifest = decode_simulation_record_manifest(canonical_json_bytes(_manifest()))
    store = SimulationRecordStore(root / "sim/1/targets/sim/record")
    store.publish_manifest(manifest)
    _stub_resume_target(monkeypatch, manifest)
    validated = validate_resume_manifest(
        alias / store.manifest_path.relative_to(root), project_root=root
    )
    assert validated.candidate.path == store.manifest_path
    (root / "linked").symlink_to(root, target_is_directory=True)
    with pytest.raises(SimulationRecordIntegrityError, match=r"link|regular"):
        validate_resume_manifest(
            alias / "linked" / store.manifest_path.relative_to(root), project_root=root
        )


def test_finalize_manifest_writes_the_planned_fingerprints() -> None:
    """Planning computes the same seven fingerprints an independent helper does."""
    document = _manifest()
    document.pop("fingerprints")
    assert finalize_manifest(document).canonical_bytes() == canonical_json_bytes(_manifest())
