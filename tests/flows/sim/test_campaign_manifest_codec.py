from __future__ import annotations

import hashlib
import json
import subprocess
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from hypothesis import given
from hypothesis import strategies as st

from booley.flows.endpoint_admission import AdmissionContext
from booley.flows.sim.campaign import serial_execution
from booley.flows.sim.campaign.codec import (
    SimulationCampaignIntegrityError,
    canonical_json_bytes,
    decode_simulation_campaign_manifest,
    encode_simulation_campaign_manifest,
)
from booley.flows.sim.campaign.coordinator import (
    CampaignPolicy,
    WorkExecutionRequest,
    _new_store,
)
from booley.flows.sim.campaign.model import (
    SimulationCampaignManifest,
    SimulationCampaignPlan,
    create_simulation_campaign_plan,
)
from booley.flows.sim.campaign.planning import (
    compare_manifests,
    finalize_manifest,
    manifest_digest,
)
from booley.flows.sim.campaign.resume import validate_resume_manifest
from booley.flows.sim.campaign.serial_execution import OrdinaryHdlSerialExecutor
from booley.flows.sim.campaign.store import CampaignStore
from booley.flows.sim.execution.contract import SimulationTargetOutcome, SimulationTestOutcome
from booley.flows.sim.flow import SimulateFlow
from booley.targets.catalog import TargetCatalog


def _sha(value: object) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _manifest() -> dict[str, object]:
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
        "backend": "icarus",
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
    manifest = {
        "$schema": "booley.simulation-campaign-manifest/v1",
        "campaign_id": "f47ac10b-58cc-4372-a567-0e02b2c3d479",
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
    manifest["fingerprints"] = {
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
    return manifest


def _baseline_manifest():
    document = _manifest()
    document.pop("fingerprints")
    document["campaign_id"] = "550e8400-e29b-41d4-a716-446655440000"
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
    identity = {
        key: value
        for key, value in item.items()  # type: ignore[union-attr]
        if key not in {"fingerprint_sha256", "work_item_id"}
    }
    fingerprint = _sha(identity)
    item["fingerprint_sha256"] = fingerprint  # type: ignore[index]
    item["work_item_id"] = "item:0000:" + fingerprint[7:23]  # type: ignore[index]
    return finalize_manifest(document)


def _linked_campaign_stores(tmp_path: Path):
    baseline = _baseline_manifest()
    invocation = tmp_path / "reports" / "000001"
    baseline_store = CampaignStore(invocation / "targets" / "base-revision" / "campaign")
    baseline_store.publish_manifest(baseline)
    raw = encode_simulation_campaign_manifest(baseline)
    baseline_json = json.loads(raw)
    candidate_document = _manifest()
    candidate_document.pop("fingerprints")
    candidate_document["prerequisites"] = [
        {
            "role": "cycle_count_baseline",
            "manifest": {
                "path_base": "origin_invocation",
                "path": baseline_store.manifest_path.relative_to(invocation).as_posix(),
                "bytes": len(raw),
                "sha256": manifest_digest(baseline),
                "kind": "simulation_campaign_manifest",
                "owner": baseline_json["campaign_id"],
            },
            "campaign_id": baseline_json["campaign_id"],
            "target": baseline_json["target"],
            "required_observation": "cycle_count",
            "work_item_id": baseline_json["work_items"][0]["work_item_id"],
        }
    ]
    candidate = finalize_manifest(candidate_document)
    candidate_store = CampaignStore(invocation / "targets" / "sim" / "campaign")
    candidate_store.publish_manifest(candidate)
    return candidate, candidate_store, baseline, baseline_store


def test_manifest_exact_codec_recomputes_all_component_digests() -> None:
    raw = canonical_json_bytes(_manifest())
    value = decode_simulation_campaign_manifest(raw)
    assert value.canonical_bytes() == raw
    assert encode_simulation_campaign_manifest(value) == raw


def test_campaign_plan_is_derived_only_from_a_validated_manifest() -> None:
    manifest = decode_simulation_campaign_manifest(canonical_json_bytes(_manifest()))
    plan = create_simulation_campaign_plan(manifest)
    assert plan.manifest == manifest
    assert plan.work_item_ids == (manifest.document["work_items"][0]["work_item_id"],)  # type: ignore[index]
    with pytest.raises(TypeError, match="create_simulation_campaign_plan"):
        SimulationCampaignPlan()
    with pytest.raises(TypeError):
        SimulationCampaignPlan(manifest, ("item:0000:ffffffffffffffff",))  # type: ignore[call-arg]


def test_campaign_plan_rejects_an_unvalidated_manifest_value() -> None:
    with pytest.raises(SimulationCampaignIntegrityError, match="exact fields"):
        create_simulation_campaign_plan(SimulationCampaignManifest({}))


def test_resume_preview_reports_all_workload_mismatches() -> None:
    expected_document = _manifest()
    expected_document.pop("fingerprints")
    expected = finalize_manifest(expected_document)
    current_document = _manifest()
    current_document.pop("fingerprints")
    current_document["target"]["revision"] = "changed"  # type: ignore[index]
    item = current_document["work_items"][0]  # type: ignore[index]
    item["revision"] = "changed"
    item["target"]["revision"] = "changed"
    identity = {
        key: value
        for key, value in item.items()
        if key not in {"fingerprint_sha256", "work_item_id"}
    }
    fingerprint = _sha(identity)
    item["fingerprint_sha256"] = fingerprint
    item["work_item_id"] = "item:0000:" + fingerprint.removeprefix("sha256:")[:16]
    current = finalize_manifest(current_document)

    mismatches = compare_manifests(expected, current)

    assert len(mismatches) >= 3
    assert {item.pointer for item in mismatches} >= {
        "/target/revision",
        "/work_items/0/revision",
        "/work_items/0/target/revision",
    }


def test_cycle_baseline_campaign_has_a_noncolliding_store_path(tmp_path: Path) -> None:
    candidate = decode_simulation_campaign_manifest(canonical_json_bytes(_manifest()))
    baseline_document = _manifest()
    baseline_document.pop("fingerprints")
    baseline_document["target"]["role"] = "cycle_count_baseline"  # type: ignore[index]
    item = baseline_document["work_items"][0]  # type: ignore[index]
    item["role"] = "cycle_count_baseline"
    item["target"]["role"] = "cycle_count_baseline"
    identity = {
        key: value
        for key, value in item.items()
        if key not in {"fingerprint_sha256", "work_item_id"}
    }
    fingerprint = _sha(identity)
    item["fingerprint_sha256"] = fingerprint
    item["work_item_id"] = "item:0000:" + fingerprint.removeprefix("sha256:")[:16]
    baseline = finalize_manifest(baseline_document)
    invocation = tmp_path / "000001"

    candidate_store = _new_store(
        SimpleNamespace(
            plan=create_simulation_campaign_plan(candidate),
            invocation_directory=invocation,
        )
    )
    baseline_store = _new_store(
        SimpleNamespace(
            plan=create_simulation_campaign_plan(baseline),
            invocation_directory=invocation,
        )
    )

    assert candidate_store.root != baseline_store.root
    assert baseline_store.root.name == "campaign"
    assert baseline_store.root.parent.name == "sim%40baseline-abc123"


def test_resume_binds_distinct_candidate_and_baseline_revision_roots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    candidate, candidate_store, baseline, baseline_store = _linked_campaign_stores(tmp_path)
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
        "booley.flows.sim.campaign.resume.git_full_sha",
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


def _source_equal_revision_drift(tmp_path: Path) -> tuple[Path, str]:
    root = tmp_path / "source"
    root.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True)
    (root / "source.sv").write_text("module source; endmodule\n", encoding="utf-8")
    subprocess.run(["git", "add", "source.sv"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "source"], cwd=root, check=True)
    original = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, check=True, capture_output=True, text=True
    ).stdout.strip()
    subprocess.run(["git", "commit", "--allow-empty", "-qm", "drift"], cwd=root, check=True)
    return root, original


def _publish_stale_candidate_manifest(tmp_path: Path, revision: str) -> CampaignStore:
    document = _manifest()
    target = dict(document["target"])  # type: ignore[arg-type]
    target["revision"] = revision
    document["target"] = target
    items = [dict(item) for item in document["work_items"]]  # type: ignore[arg-type]
    items[0]["target"] = target
    items[0]["revision"] = revision
    identity = {
        key: value
        for key, value in items[0].items()
        if key not in {"fingerprint_sha256", "work_item_id"}
    }
    item_digest = _sha(identity)
    items[0]["fingerprint_sha256"] = item_digest
    items[0]["work_item_id"] = "item:0000:" + item_digest.removeprefix("sha256:")[:16]
    document["work_items"] = items
    document.pop("fingerprints")
    manifest = finalize_manifest(document)
    store = CampaignStore(tmp_path / "000001" / "targets" / "sim" / "campaign")
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
    flow._args = SimpleNamespace(resume_from=store.manifest_path, work_dir=root)

    with pytest.raises(SimulationCampaignIntegrityError, match="revision"):
        flow._prepare_campaign_targets()


def test_recovery_retries_a_crash_after_attempt_directory_allocation(
    tmp_path: Path,
) -> None:
    manifest = decode_simulation_campaign_manifest(canonical_json_bytes(_manifest()))
    store = CampaignStore(tmp_path / "campaign")
    store.publish_manifest(manifest)
    work_item_id = manifest.document["work_items"][0]["work_item_id"]  # type: ignore[index]

    ordinal, _directory = store.allocate_attempt_directory(
        work_item_id, "550e8400-e29b-41d4-a716-446655440001"
    )
    assert ordinal == 1
    assert store.scan().interrupted == (work_item_id,)

    retry_ordinal, _retry = store.allocate_attempt_directory(
        work_item_id, "550e8400-e29b-41d4-a716-446655440002"
    )
    assert retry_ordinal == 2
    recovery = store.scan()
    assert recovery.interrupted == (work_item_id,)
    assert recovery.items[0].attempt_count == 2


@pytest.mark.parametrize("nondeterministic", [False, True])
def test_serial_executor_authenticates_planned_generator_closure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, nondeterministic: bool
) -> None:
    document = _manifest()
    document.pop("fingerprints")
    disclosure = {
        "planner": "fusesoc_setup",
        "scratch_inputs": [],
        "generated_files": [
            {
                "path": "generated.sv",
                "bytes": 1,
                "sha256": "sha256:" + hashlib.sha256(b"a").hexdigest(),
                "kind": "generated_input",
            }
        ],
        "tool_provenance": {
            "kind": "fusesoc",
            "version": "1",
            "contract_version": "1",
        },
        "cleanup": {"removed": True},
    }
    document["planning_disclosures"] = [disclosure]
    manifest = finalize_manifest(document)
    store = CampaignStore(tmp_path / "campaign")
    store.publish_manifest(manifest)
    item = manifest.document["work_items"][0]  # type: ignore[index]
    attempt_id = "550e8400-e29b-41d4-a716-446655440001"
    ordinal, attempt_directory = store.allocate_attempt_directory(
        item["work_item_id"],
        attempt_id,  # type: ignore[index]
    )
    build_root = tmp_path / "engine-build"
    build_root.mkdir()
    (tmp_path / "run").mkdir()
    (build_root / "simv").write_bytes(b"image")
    (build_root / ".booley-build-manifest.json").write_text(
        json.dumps({"artifacts": {"simv": "digest"}}), encoding="utf-8"
    )
    run_log = build_root / "run.log"
    run_log.write_text("PASS\n", encoding="utf-8")

    class FakeCatalog:
        def select(self, token: str, *, for_flow: str):
            assert (token, for_flow) == ("sim", "sim")
            return SimpleNamespace(
                identity="acme:lib:dut:1#sim",
                project_root=tmp_path,
                selector="sim",
                eda_tool="icarus",
            )

    class FakeGroup:
        def __init__(self):
            self.artifact_paths = (build_root / "simv",)

        @property
        def build_root(self):
            return build_root

        def compile(self):
            return SimpleNamespace(passed=True)

        def planning_disclosure(self):
            if not nondeterministic:
                return disclosure
            changed = json.loads(json.dumps(disclosure))
            changed["generated_files"][0]["sha256"] = (  # type: ignore[index]
                "sha256:" + hashlib.sha256(b"b").hexdigest()
            )
            return changed

        def launch_snapshot(self, snapshot_root, run_cwd):
            assert snapshot_root.is_dir()
            assert run_cwd == tmp_path / "run"
            test = SimulationTestOutcome(
                name="sim", verdict="pass", passed=True, run_log_path=str(run_log)
            )
            return SimulationTargetOutcome(
                target="sim",
                target_identity="acme:lib:dut:1#sim",
                toplevel="tb",
                eda_tool="icarus",
                passed=True,
                verdict="pass",
                elapsed_s=0.1,
                tests=(test,),
            )

    class FakeExecution:
        @contextmanager
        def ordinary_group(self, handle, names):
            del handle
            assert names == ()
            yield FakeGroup()

    monkeypatch.setattr(
        "booley.flows.sim.campaign.serial_execution.TargetCatalog.build",
        lambda _root: FakeCatalog(),
    )
    executor = OrdinaryHdlSerialExecutor(
        invoke=lambda *_args, **_kwargs: None,  # type: ignore[arg-type]
        execution_factory=lambda _options: FakeExecution(),  # type: ignore[arg-type,return-value]
    )
    request = WorkExecutionRequest(
        store=store,
        manifest=manifest,
        work_item=item,  # type: ignore[arg-type]
        attempt_id=attempt_id,
        attempt_ordinal=ordinal,
        attempt_directory=attempt_directory,
        producer_invocation_id=1,
        policy=CampaignPolicy(),
        admission=AdmissionContext(
            "unmanaged", None, None, 1, "interactive", "", None, lambda: False
        ),
        project_root=tmp_path,
    )
    if nondeterministic:
        with pytest.raises(
            SimulationCampaignIntegrityError,
            match="generator source closure disagrees",
        ):
            executor.execute(request)
        assert store.scan().interrupted == (item["work_item_id"],)  # type: ignore[index]
        return
    result = executor.execute(request)
    store.verify_result_evidence(attempt_directory, result)
    store.publish_result(item["work_item_id"], result)  # type: ignore[index]
    recovery = store.scan()
    assert recovery.complete == (item["work_item_id"],)  # type: ignore[index]
    assert result.document["grade"] == "pass"
    assert result.document["build_result"]["sharing"] == "shared_variant"  # type: ignore[index]


@given(st.sampled_from(["workload_sha256", "target_recipe_sha256", "work_items_sha256"]))
def test_manifest_digest_mutations_are_rejected(field: str) -> None:
    manifest = _manifest()
    manifest["fingerprints"][field] = "sha256:" + "f" * 64  # type: ignore[index]
    with pytest.raises(SimulationCampaignIntegrityError, match="disagrees"):
        decode_simulation_campaign_manifest(canonical_json_bytes(manifest))


@given(st.sampled_from(["missing", "unknown", "type", "path", "digest", "conditional"]))
def test_manifest_structural_mutations_are_rejected(mutation: str) -> None:
    manifest = _manifest()
    target = manifest["target"]
    if mutation == "missing":
        del target["revision"]
    elif mutation == "unknown":
        target["unexpected"] = True
    elif mutation == "type":
        target["name"] = 7
    elif mutation == "path":
        manifest["required_suite"]["source_path"] = "../escape"  # type: ignore[index]
    elif mutation == "digest":
        manifest["fingerprints"]["workload_sha256"] = "sha256:" + "f" * 64  # type: ignore[index]
    else:
        manifest["work_items"][0]["role"] = "cycle_count_baseline"  # type: ignore[index]
    with pytest.raises(SimulationCampaignIntegrityError):
        decode_simulation_campaign_manifest(canonical_json_bytes(manifest))
    with pytest.raises(SimulationCampaignIntegrityError):
        encode_simulation_campaign_manifest(SimulationCampaignManifest(manifest))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("defines", ["x" * 4097]),
        ("pre_sim_commands", ["x" * (16 * 1024 + 1)]),
    ],
)
def test_manifest_rejects_recipe_string_resource_overflow(field: str, value: object) -> None:
    manifest = _manifest()
    manifest["workload"]["source_recipe"][field] = value  # type: ignore[index]
    with pytest.raises(SimulationCampaignIntegrityError, match="ceiling"):
        decode_simulation_campaign_manifest(canonical_json_bytes(manifest))


@pytest.mark.parametrize(
    ("field", "value"),
    [("role", "cycle_count_baseline"), ("revision", "different")],
)
def test_work_item_role_and_revision_must_match_target(field: str, value: str) -> None:
    manifest = _manifest()
    manifest["work_items"][0][field] = value  # type: ignore[index]
    with pytest.raises(SimulationCampaignIntegrityError, match="role/revision"):
        decode_simulation_campaign_manifest(canonical_json_bytes(manifest))


@pytest.mark.parametrize("names", [[], [1], ["smoke", "smoke"]])
def test_named_selection_requires_unique_nonempty_bounded_strings(names: list[object]) -> None:
    manifest = _manifest()
    manifest["work_items"][0]["selection"] = {"kind": "named", "names": names}  # type: ignore[index]
    with pytest.raises(SimulationCampaignIntegrityError):
        decode_simulation_campaign_manifest(canonical_json_bytes(manifest))


@pytest.mark.parametrize("mutation", ["target_role", "work_item_id"])
def test_prerequisite_binds_a_baseline_target_and_valid_work_item(mutation: str) -> None:
    manifest = _manifest()
    target = dict(manifest["target"])  # type: ignore[arg-type]
    target["role"] = "candidate" if mutation == "target_role" else "cycle_count_baseline"
    prerequisite = {
        "role": "cycle_count_baseline",
        "manifest": {
            "path_base": "origin_invocation",
            "path": "targets/baseline/manifest.json",
            "bytes": 1,
            "sha256": "sha256:" + "a" * 64,
            "kind": "simulation_campaign_manifest",
            "owner": "550e8400-e29b-41d4-a716-446655440000",
        },
        "campaign_id": "550e8400-e29b-41d4-a716-446655440000",
        "target": target,
        "required_observation": "cycle_count",
        "work_item_id": (
            "not-an-item" if mutation == "work_item_id" else "item:0000:0123456789abcdef"
        ),
    }
    manifest["prerequisites"] = [prerequisite]
    with pytest.raises(SimulationCampaignIntegrityError):
        decode_simulation_campaign_manifest(canonical_json_bytes(manifest))


def test_run_cwd_rejects_non_string_placeholder_items_as_campaign_errors() -> None:
    manifest = _manifest()
    manifest["workload"]["run_cwd"] = {  # type: ignore[index]
        "configured": "run",
        "kind": "literal",
        "placeholders": [[]],
    }
    with pytest.raises(SimulationCampaignIntegrityError):
        decode_simulation_campaign_manifest(canonical_json_bytes(manifest))


def test_serial_result_classifies_simulator_crash_as_design_failure() -> None:
    test = SimulationTestOutcome(
        name="smoke",
        verdict="crash",
        passed=False,
        crashed=True,
        reason="terminated by signal 11",
    )
    observation = serial_execution._observation(test)
    assert serial_execution._result_state((test,)) == "crash"
    assert observation["execution"] == "crash"
    assert observation["failure_class"] == "design"
    assert observation["functional"] == "not_observed"


@pytest.mark.parametrize(
    "configured",
    ["{bogus}", "{test!r}", "{test:>4}", "{test", "{test/foo}"],
)
def test_run_cwd_rejects_noncanonical_format_syntax(configured: str) -> None:
    manifest = _manifest()
    manifest["workload"]["run_cwd"] = {  # type: ignore[index]
        "configured": configured,
        "kind": "templated",
        "placeholders": ["test"],
    }
    with pytest.raises(SimulationCampaignIntegrityError):
        decode_simulation_campaign_manifest(canonical_json_bytes(manifest))
