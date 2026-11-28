from __future__ import annotations

import copy
import hashlib
import json
import os
import subprocess
from pathlib import Path
from types import MappingProxyType, SimpleNamespace

import pytest

from booley.flows.sim.execution.contract import SimulationTestOutcome
from booley.flows.sim.flow import PreparedSimulationEndpoint, SimulateFlow
from booley.flows.sim.record import planning, serial_execution
from booley.flows.sim.record.codec import (
    MANIFEST_SCHEMA,
    SimulationRecordIntegrityError,
    canonical_json_bytes,
    decode_simulation_record_manifest,
    encode_simulation_record_manifest,
)
from booley.flows.sim.record.coordinator import SimulationRecord, SimulationRecordRecoveryStatus
from booley.flows.sim.record.model import SimulationRecordPlan, create_simulation_record_plan
from booley.flows.sim.record.planning import finalize_manifest, manifest_digest
from booley.flows.sim.record.resume import (
    ValidatedManifestNode,
    ValidatedResumeManifest,
    validate_resume_manifest,
)
from booley.flows.sim.record.store import SimulationRecordStore
from booley.flows.sim.request import SimRequest
from booley.flows.test_runs import build_test_run, test_run_document
from booley.runtime.endpoint_execution import EXIT_ERROR, EndpointOutcome
from booley.targets.catalog import TargetCatalog
from tests.fusesoc_test_support import symlinked_booley_dir
from tests.runtime.test_sandbox_layout import project_alias
from tests.subprocess_budget import SUBPROCESS_TIMEOUT_S

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
    source_recipe = {"sources": [], "parameters": [], "defines": [], "pre_sim_commands": []}
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


def test_simulation_record_plan_is_derived_only_from_a_validated_manifest() -> None:
    manifest = decode_simulation_record_manifest(canonical_json_bytes(_manifest()))
    plan = create_simulation_record_plan(manifest)
    assert plan.work_item_ids == (manifest.document["work_items"][0]["work_item_id"],)  # type: ignore[index]
    with pytest.raises(TypeError, match="create_simulation_record_plan"):
        SimulationRecordPlan()
    with pytest.raises(TypeError):
        SimulationRecordPlan(manifest, ("item:0000:ffffffffffffffff",))  # type: ignore[call-arg]


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


@pytest.mark.parametrize("defect", ["ancestor_symlink", "final_symlink"])
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
        (view / "manifest.json").symlink_to(real.manifest_path)
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
                identity="acme:lib:dut:1#sim", selector=selector, project_root=root
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
        ValidatedManifestNode(store.manifest_path, manifest, "0" * 64), (object(),)
    )

    def forbidden(*_args: object) -> None:
        raise AssertionError("non-coverage resume must not be inspected by the guard")

    monkeypatch.setattr(
        "booley.flows.sim.flow.validate_resume_manifest", lambda *_a, **_k: validated
    )
    monkeypatch.setattr("booley.flows.sim.flow_resume.SimulationRecord.inspect_resume", forbidden)
    flow = SimulateFlow()
    flow._args = SimRequest(target="", work_dir=tmp_path, resume_from=store.manifest_path)

    prepared = flow.prepare_simulation_endpoint()

    assert isinstance(prepared, PreparedSimulationEndpoint)
    assert prepared.resume is validated


def test_serial_result_classifies_simulator_crash_as_design_failure() -> None:
    test = SimulationTestOutcome(
        name="smoke", verdict="crash", passed=False, crashed=True, reason="terminated by signal 11"
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


def _grow_source(document):
    document["workload"]["source_recipe"]["sources"][0]["bytes"] = 2
    document["build_variants"][0]["source_closure"][0]["bytes"] = 2


_SOURCE_AREAS = ("Target recipe", "source closures", "work items")


@pytest.mark.parametrize(
    ("kwargs", "mutate", "areas"),
    [
        pytest.param(
            {"sources": [_diagnostic_source("a.sv")]}, _grow_source, _SOURCE_AREAS, id="source"
        ),
        pytest.param(
            {"names": ["reset"]},
            lambda d: d["required_suite"].update(names=["reset", "wrap"]),
            ("test suite",),
            id="suite",
        ),
        pytest.param(
            {},
            lambda d: d["workload"]["run_cwd"].update(configured="other"),
            ("other workload settings",),
            id="run-cwd",
        ),
        pytest.param(
            {},
            lambda d: d.update(
                planning_disclosures=[
                    {
                        "planner": "setup",
                        "scratch_inputs": [],
                        "generated_files": [],
                        "tool_provenance": {
                            "kind": "fusesoc",
                            "version": "1",
                            "contract_version": "1",
                        },
                        "cleanup": {"removed": True},
                    }
                ]
            ),
            ("planning disclosures",),
            id="disclosure",
        ),
        pytest.param(
            {},
            lambda d: d.update(prerequisites=[_prerequisite_entry()]),
            ("prerequisites",),
            id="prerequisite",
        ),
        pytest.param(
            {},
            lambda d: d["work_items"][0].update(arguments=["+extra"]),
            ("work items",),
            id="arguments",
        ),
        pytest.param(
            {},
            lambda d: d["target"].update(revision="changed"),
            ("Target recipe", "work items"),
            id="revision",
        ),
        # False == 0, so only a digest comparison sees the changed parameter type.
        pytest.param(
            {"parameters": [{"name": "N", "value": False}]},
            lambda d: d["workload"]["source_recipe"]["parameters"][0].update(value=0),
            _SOURCE_AREAS,
            id="typed-parameter",
        ),
        pytest.param({}, lambda d: None, (), id="unchanged"),
        # No named area's members differ, so the catch-all must still name something.
        pytest.param(
            {},
            lambda d: d["build_variants"][0].update(sharing_eligible=False),
            ("other workload settings",),
            id="sharing",
        ),
    ],
)
def test_workload_mismatch_names_each_changed_area(kwargs, mutate, areas):
    old = _diagnostic_document(**kwargs)
    new = copy.deepcopy(old)
    mutate(new)
    left, right = _finalize_diagnostic_document(old), _finalize_diagnostic_document(new)
    if areas:
        with pytest.raises(planning.SimulationRecordWorkloadMismatchError) as exc:
            planning.verify_workload(left, right)
        assert exc.value.diagnostic.areas == areas
    else:
        planning.verify_workload(left, right)
    assert planning.workload_diagnostic(left, right).areas == areas


def test_manifest_with_recorded_flow_option_defines_still_decodes_and_compares():
    """A Simulation Record stored with `source_recipe.defines` stays readable; resume refuses."""
    legacy_doc = _diagnostic_document()
    legacy_doc["workload"]["source_recipe"]["defines"] = ["LEGACY=1"]

    stored = _finalize_diagnostic_document(legacy_doc)
    encoded = encode_simulation_record_manifest(stored)
    assert encode_simulation_record_manifest(decode_simulation_record_manifest(encoded)) == (
        encoded
    )
    planning.verify_workload(stored, stored)

    current = _finalize_diagnostic_document(_diagnostic_document())
    with pytest.raises(planning.SimulationRecordWorkloadMismatchError) as exc:
        planning.verify_workload(stored, current)
    assert exc.value.diagnostic.areas == _SOURCE_AREAS


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
    left, right = [
        decode_simulation_record_manifest(canonical_json_bytes(case[key]))
        for key in ("expected", "current")
    ]
    assert [manifest_digest(left), manifest_digest(right)] == case["digests"]
    diagnostic = planning.workload_diagnostic(left, right)
    assert diagnostic.areas == tuple(case["areas"])
    if case["outcome"] == "accept":
        planning.verify_workload(left, right)
    else:
        with pytest.raises(planning.SimulationRecordWorkloadMismatchError) as exc:
            planning.verify_workload(left, right)
        assert exc.value.diagnostic == diagnostic


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
