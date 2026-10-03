from __future__ import annotations

import hashlib
import json
import subprocess
from contextlib import contextmanager
from pathlib import Path
from types import MappingProxyType, SimpleNamespace

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
    CampaignRecoveryStatus,
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
from booley.flows.sim.campaign.resume import (
    ValidatedManifestNode,
    ValidatedResumeManifest,
    validate_resume_manifest,
)
from booley.flows.sim.campaign.serial_execution import OrdinaryHdlSerialExecutor
from booley.flows.sim.campaign.store import CampaignStore
from booley.flows.sim.execution.contract import SimulationTargetOutcome, SimulationTestOutcome
from booley.flows.sim.flow import SimulateFlow, _unfinished_coverage_work
from booley.targets.catalog import TargetCatalog


def _sha(value: object) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _build_execution() -> dict[str, object]:
    return {
        "$schema": "booley.simulation-build-execution/v1",
        "process": {
            "returncode": 0,
            "stdout": "build\n",
            "stderr": "",
            "timed_out": False,
            "duration_s": 0.1,
            "dispatched_unix": 1.0,
            "peak_rss_mb": None,
            "oom_kill_delta": 0,
        },
        "build": {
            "ran": True,
            "verdict": "pass",
            "failure_kind": None,
            "elapsed_s": 0.1,
            "output": "build\n",
            "returncode": 0,
            "timed_out": False,
            "peak_rss_mb": None,
            "oom_kill_delta": 0,
            "terminal_record": True,
            "reason": "",
            "cache_decision": "",
        },
    }


class _GeneratorCatalog:
    def __init__(self, root: Path) -> None:
        self.root = root

    def select(self, token: str, *, for_flow: str):
        assert (token, for_flow) == ("sim", "sim")
        return SimpleNamespace(
            identity="acme:lib:dut:1#sim",
            project_root=self.root,
            selector="sim",
            eda_tool="icarus",
        )


class _GeneratorGroup:
    def __init__(self, root: Path, build_root: Path, run_log: Path, disclosure, changed):
        self.root = root
        self.build_root = build_root
        self.run_log = run_log
        self.disclosure = disclosure
        self.changed = changed
        self.artifact_paths = (build_root / "simv",)
        self.compile_surface = SimpleNamespace(
            project_root=root.resolve(),
            authored_paths=(),
            operational_paths=(),
            optional_paths=(),
        )

    def compile(self):
        return SimpleNamespace(passed=True)

    def planning_disclosure(self):
        return self.changed if self.changed is not None else self.disclosure

    def build_recovery_document(self):
        return _build_execution()

    def bind_authenticated_bundle(self, evidence):
        assert evidence == _build_execution()

    def launch_snapshot(self, snapshot_root, run_cwd):
        assert snapshot_root.is_dir()
        assert run_cwd == self.root / "run"
        test = SimulationTestOutcome("sim", "pass", True, str(self.run_log))
        return SimulationTargetOutcome(
            "sim", "acme:lib:dut:1#sim", "tb", "icarus", True, "pass", 0.1, (test,)
        )


class _GeneratorExecution:
    def __init__(self, group: _GeneratorGroup) -> None:
        self.group = group

    @contextmanager
    def ordinary_group(self, handle, names):
        del handle
        assert names == ()
        yield self.group


def _manifest() -> dict[str, object]:
    target, source_recipe, build_recipe, workload, suite = _manifest_components()
    variants, items = _manifest_work(target, source_recipe, build_recipe, workload)
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


_V1 = "booley.simulation-campaign-manifest/v1"
_V2 = "booley.simulation-campaign-manifest/v2"


def _refinalized(schema: str, **workload_members: object):
    """Re-finalize the fixture so fingerprints stay valid and only the workload rule varies."""
    document = _manifest()
    document.pop("fingerprints")
    document["$schema"] = schema
    workload = document["workload"]
    workload.update(workload_members)  # type: ignore[union-attr]
    # The build variant recipe binds ``workload.coverage``; keep it consistent.
    variant = document["build_variants"][0]  # type: ignore[index]
    variant["source_closure"] = []
    recipe_sha = _sha(
        {
            "kind": variant["kind"],
            "source_closure": variant["source_closure"],
            "source_recipe": workload["source_recipe"],  # type: ignore[index]
            "build_recipe": workload["build_recipe"],  # type: ignore[index]
            "eda": workload["eda"],  # type: ignore[index]
            "trace": workload["trace"],  # type: ignore[index]
            "coverage": workload["coverage"],  # type: ignore[index]
        }
    )
    variant["recipe_sha256"] = recipe_sha
    variant["build_variant_id"] = "variant:" + recipe_sha.removeprefix("sha256:")
    item = document["work_items"][0]  # type: ignore[index]
    item["build_variant_id"] = variant["build_variant_id"]
    identity = {
        key: value
        for key, value in item.items()
        if key not in {"fingerprint_sha256", "work_item_id"}
    }
    item["fingerprint_sha256"] = _sha(identity)
    item["work_item_id"] = "item:0000:" + item["fingerprint_sha256"][7:23]
    return finalize_manifest(document)


def test_no_waivers_v2_manifest_decodes_and_keeps_its_schema() -> None:
    manifest = _refinalized(_V2, coverage=True, no_waivers=True)
    assert manifest.document["$schema"] == _V2
    assert manifest.document["workload"]["no_waivers"] is True  # type: ignore[index]
    decoded = decode_simulation_campaign_manifest(encode_simulation_campaign_manifest(manifest))
    assert decoded.document == manifest.document


@pytest.mark.parametrize(
    ("schema", "members"),
    [
        (_V2, {"coverage": True}),
        (_V2, {"coverage": True, "no_waivers": False}),
        (_V2, {"coverage": True, "no_waivers": "yes"}),
        (_V2, {"coverage": False, "no_waivers": True}),
        (_V1, {"coverage": True, "no_waivers": True}),
    ],
)
def test_no_waivers_manifest_grammar_is_exact(schema: str, members: dict[str, object]) -> None:
    with pytest.raises(SimulationCampaignIntegrityError):
        _refinalized(schema, **members)


def test_v1_manifest_grammar_is_unchanged_by_no_waivers_support() -> None:
    manifest = _refinalized(_V1, coverage=True)
    assert manifest.document["$schema"] == _V1
    assert "no_waivers" not in manifest.document["workload"]  # type: ignore[operator]


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


def _linked_campaign_stores(tmp_path: Path, *, topology_revisions=None):
    baseline = _baseline_manifest()
    if topology_revisions is not None:
        document = json.loads(encode_simulation_campaign_manifest(baseline))
        document.pop("fingerprints")
        _retarget_topology_manifest(document, topology_revisions[1])
        baseline = finalize_manifest(document)
    invocation = tmp_path / "reports" / "000001"
    baseline_store = CampaignStore(invocation / "targets" / "base-revision" / "campaign")
    baseline_store.publish_manifest(baseline)
    raw = encode_simulation_campaign_manifest(baseline)
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


def test_finalize_manifest_accepts_immutable_mapping_inputs() -> None:
    document = _manifest()
    document.pop("fingerprints")
    workload = document["workload"]
    assert isinstance(workload, dict)
    document["workload"] = MappingProxyType(
        {
            **workload,
            "eda": MappingProxyType(workload["eda"]),
        }
    )

    manifest = finalize_manifest(document)

    assert manifest.document["workload"]["eda"]["kind"] == "icarus"


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


@pytest.mark.parametrize("defect", ["ancestor_symlink", "final_symlink", "hardlink"])
def test_resume_rejects_linked_manifest_paths_without_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, defect: str
) -> None:
    manifest = decode_simulation_campaign_manifest(canonical_json_bytes(_manifest()))
    real = CampaignStore(tmp_path / "real/000001/targets/sim/campaign")
    real.publish_manifest(manifest)
    view = tmp_path / "view/000001/targets/sim/campaign"
    if defect == "ancestor_symlink":
        (tmp_path / "view").mkdir()
        (tmp_path / "view/000001").symlink_to(real.root.parents[2], target_is_directory=True)
    else:
        view.mkdir(parents=True)
        if defect == "final_symlink":
            (view / "manifest.json").symlink_to(real.manifest_path)
        else:
            (view / "manifest.json").hardlink_to(real.manifest_path)
    target = manifest.document["target"]
    monkeypatch.setattr(
        "booley.flows.sim.campaign.resume.git_full_sha",
        lambda _ref, _root: target["revision"],
    )
    monkeypatch.setattr(
        TargetCatalog,
        "build",
        lambda root: SimpleNamespace(
            select=lambda _selector, **_kwargs: SimpleNamespace(
                identity=f"{target['vlnv']}#{target['name']}",
                project_root=Path(root),
            )
        ),
    )
    before = sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*"))

    with pytest.raises(SimulationCampaignIntegrityError, match=r"regular|link"):
        validate_resume_manifest(view / "manifest.json", project_root=tmp_path / "source")

    assert sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*")) == before


def test_campaign_store_rejects_linked_root_before_lock_write(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    linked = tmp_path / "linked"
    linked.symlink_to(outside, target_is_directory=True)
    store = CampaignStore(linked / "campaign")

    with (
        pytest.raises(SimulationCampaignIntegrityError, match="contains a link"),
        store.mutation_lock(),
    ):
        pass

    assert not (outside / "campaign").exists()


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


@pytest.mark.parametrize("state", ["pending", "interrupted", "complete"])
@pytest.mark.parametrize("kind", ["ordinary_hdl", "cocotb_batch", "coverage_aggregate"])
def test_unfinished_coverage_work_only_applies_to_coverage_aggregates(
    tmp_path: Path, kind: str, state: str
) -> None:
    manifest = SimpleNamespace(document={"work_items": [{"kind": kind, "work_item_id": "w1"}]})
    status = CampaignRecoveryStatus(
        manifest_path=tmp_path / "manifest.json",
        manifest_sha256="0" * 64,
        completed=("w1",) if state == "complete" else (),
        interrupted=("w1",) if state == "interrupted" else (),
        pending=("w1",) if state == "pending" else (),
    )

    unfinished = _unfinished_coverage_work(manifest, status)  # type: ignore[arg-type]

    assert unfinished == (("w1",) if kind == "coverage_aggregate" and state != "complete" else ())


def test_hdl_resume_with_interrupted_item_is_not_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = decode_simulation_campaign_manifest(canonical_json_bytes(_manifest()))
    store = CampaignStore(tmp_path / "campaign")
    store.publish_manifest(manifest)
    work_item_id = manifest.document["work_items"][0]["work_item_id"]  # type: ignore[index]
    store.allocate_attempt_directory(work_item_id, "550e8400-e29b-41d4-a716-446655440001")
    assert store.scan().interrupted == (work_item_id,)
    validated = ValidatedResumeManifest(
        ValidatedManifestNode(store.manifest_path, manifest, "0" * 64), (), ()
    )

    def forbidden(*_args: object) -> None:
        raise AssertionError("non-coverage resume must not be inspected by the guard")

    monkeypatch.setattr("booley.flows.sim.flow.SimulationCampaign.inspect_resume", forbidden)

    assert SimulateFlow()._refuse_unfinished_coverage_resume(validated) is None


@pytest.mark.parametrize("nondeterministic", [False, True])
def test_serial_executor_authenticates_planned_generator_closure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, nondeterministic: bool
) -> None:
    disclosure = _generator_disclosure()
    manifest, store, item, ordinal, attempt_directory = _generator_campaign(tmp_path, disclosure)
    executor = _generator_executor(tmp_path, disclosure, nondeterministic, monkeypatch)
    request = _generator_request(tmp_path, store, manifest, item, ordinal, attempt_directory)
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
    assert store.scan().complete == (item["work_item_id"],)  # type: ignore[index]
    assert result.document["grade"] == "pass"
    assert result.document["build_result"]["sharing"] == "shared_variant"  # type: ignore[index]


def _generator_disclosure() -> dict[str, object]:
    return {
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
        "tool_provenance": {"kind": "fusesoc", "version": "1", "contract_version": "1"},
        "cleanup": {"removed": True},
    }


def _generator_campaign(tmp_path: Path, disclosure):
    document = _manifest()
    document.pop("fingerprints")
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
    return manifest, store, item, ordinal, attempt_directory


def _generator_executor(tmp_path, disclosure, nondeterministic, monkeypatch):
    build_root = tmp_path / "engine-build"
    build_root.mkdir()
    (tmp_path / "run").mkdir()
    (build_root / "simv").write_bytes(b"image")
    (build_root / ".booley-build-manifest.json").write_text(
        json.dumps({"artifacts": {"simv": "digest"}}), encoding="utf-8"
    )
    run_log = build_root / "run.log"
    run_log.write_text("PASS\n", encoding="utf-8")
    changed = None
    if nondeterministic:
        changed = json.loads(json.dumps(disclosure))
        changed["generated_files"][0]["sha256"] = (  # type: ignore[index]
            "sha256:" + hashlib.sha256(b"b").hexdigest()
        )
    group = _GeneratorGroup(tmp_path, build_root, run_log, disclosure, changed)
    monkeypatch.setattr(
        "booley.flows.sim.campaign.serial_execution.TargetCatalog.build",
        lambda _root: _GeneratorCatalog(tmp_path),
    )
    return OrdinaryHdlSerialExecutor(
        invoke=lambda *_args, **_kwargs: None,  # type: ignore[arg-type]
        execution_factory=lambda _options: _GeneratorExecution(group),  # type: ignore[arg-type,return-value]
    )


def _generator_request(tmp_path, store, manifest, item, ordinal, attempt_directory):
    attempt_id = "550e8400-e29b-41d4-a716-446655440001"
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
    return request


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


def _retarget_topology_manifest(document, revision):
    target = document["target"]
    target.update(vlnv="acme:lib:top:1", name="sim_core", selector="sim_core", revision=revision)
    item = document["work_items"][0]
    item.update(target=target, revision=revision)
    identity = {
        key: value
        for key, value in item.items()
        if key not in {"fingerprint_sha256", "work_item_id"}
    }
    fingerprint = _sha(identity)
    item.update(fingerprint_sha256=fingerprint, work_item_id="item:0000:" + fingerprint[7:23])


@pytest.mark.parametrize("topology", ["standalone", "linked"])
def test_resume_baseline_materializes_project_topologies(tmp_path, monkeypatch, topology):
    from tests.flows.sim.test_cycle_observation import _criterion_flow
    from tests.flows.test_baseline_worktree import (
        _assert_topology_baseline,
        _project_topology_checkout,
    )

    root, revision = _project_topology_checkout(tmp_path, monkeypatch, topology)
    current = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    ).stdout.strip()
    _candidate, store, _baseline, baseline_store = _linked_campaign_stores(
        tmp_path, topology_revisions=(current, revision)
    )
    flow, _key = _criterion_flow(relative=True)
    flow._args.work_dir = root
    flow._args.resume_from = store.manifest_path
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
        handles, validated, selected, tests = flow._prepare_campaign_targets()
        assert handles == validated.target_handles
        assert handles[0].project_root == root
        assert handles[1].identity == "acme:lib:top:1#sim_core"
        assert handles[1].project_root == roots[0]
        assert validated.prerequisites[0].path == baseline_store.manifest_path
        assert selected == ()
        assert tests == {}
        assert roots[0].exists()
    finally:
        flow.context.publication_resources.close()
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
        document["required_suite"]["source_path"] = ".booley_project/tests.toml"
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
    identity = {
        key: value
        for key, value in item.items()
        if key not in {"work_item_id", "fingerprint_sha256"}
    }
    item["fingerprint_sha256"] = _sha(identity)
    item["work_item_id"] = "item:0000:" + _sha(identity)[7:23]
    return finalize_manifest(document)


def _project_diagnostics(old, new):
    from booley.flows.sim.campaign.planning import (
        SimulationCampaignWorkloadMismatchError,
        verify_workload,
    )

    left, right = _finalize_diagnostic_document(old), _finalize_diagnostic_document(new)
    raw = compare_manifests(left, right)
    with pytest.raises(SimulationCampaignWorkloadMismatchError) as exc:
        verify_workload(left, right)
    assert exc.value.diagnostic.mismatches == raw
    assert exc.value.diagnostic.detail["mismatches"] == [item.message for item in raw]
    return exc.value.diagnostic


@pytest.mark.parametrize(
    ("old_paths", "new_paths", "lines"),
    [
        (("a.sv",), ("head.sv", "a.sv"), ("source added: head.sv",)),
        (("a.sv", "b.sv"), ("b.sv",), ("source removed: a.sv",)),
        (("a.sv",), ("b.sv",), ("source removed: a.sv", "source added: b.sv")),
        (("a.sv", "b.sv"), ("b.sv", "a.sv"), ("source order changed",)),
    ],
)
def test_source_diagnostics_align_paths_across_positional_diff(old_paths, new_paths, lines):
    old = _diagnostic_document(sources=[_diagnostic_source(path) for path in old_paths])
    new = _diagnostic_document(sources=[_diagnostic_source(path) for path in new_paths])
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
    ],
)
def test_parameter_diagnostics_preserve_values_and_absence(old_parameters, new_parameters, line):
    diagnostic = _project_diagnostics(
        _diagnostic_document(parameters=old_parameters),
        _diagnostic_document(parameters=new_parameters),
    )
    assert diagnostic.mismatch_summary == (line,)


def test_runtime_declarations_are_aligned_by_destination():
    from booley.flows.sim.campaign.planning import canonical_sha256

    old, new = _diagnostic_document(), _diagnostic_document()
    for document, source in ((old, "old/data.bin"), (new, "new/data.bin")):
        declaration = {"destination": "input/data.bin", "source_artifact_path": source}
        document["workload"]["runtime_inputs"] = [
            {**declaration, "declaration_id": canonical_sha256(declaration)}
        ]
    diagnostic = _project_diagnostics(old, new)
    assert diagnostic.mismatch_summary == ("runtime input changed: input/data.bin",)
    assert diagnostic.derived_fingerprint_count == 1


def test_opaque_command_model_digest_is_a_named_root_cause():
    old, new = _diagnostic_document(), _diagnostic_document()
    new["workload"]["build_recipe"]["command_model_sha256"] = "sha256:" + "b" * 64
    diagnostic = _project_diagnostics(old, new)
    assert diagnostic.mismatch_summary == ("build command model changed",)
    assert diagnostic.derived_fingerprint_count == 5


def test_fingerprint_only_report_retains_details():
    from booley.flows.sim.campaign.planning import WorkloadDiagnostic, WorkloadMismatch

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


def test_controls_in_source_labels_are_escaped():
    old = _diagnostic_document(sources=[_diagnostic_source("a\n.sv")])
    new = _diagnostic_document(sources=[_diagnostic_source("a\n.sv", size=2)])
    diagnostic = _project_diagnostics(old, new)
    assert diagnostic.mismatch_summary == ('source changed: "a\\n.sv"',)


def test_zero_mismatch_verification_remains_successful():
    from booley.flows.sim.campaign.planning import project_workload_mismatches, verify_workload

    manifest = finalize_manifest(_diagnostic_document())
    verify_workload(manifest, manifest)
    diagnostic = project_workload_mismatches(manifest, manifest)
    assert diagnostic.detail == {
        "mismatches": [],
        "mismatch_summary": [],
        "derived_fingerprint_count": 0,
    }


def test_parameter_nested_json_values_are_rendered_as_json():
    old = _diagnostic_document(parameters=[{"name": "shape", "value": {"x": [1, False]}}])
    new = _diagnostic_document(parameters=[{"name": "shape", "value": {"x": [2, True]}}])
    diagnostic = _project_diagnostics(old, new)
    assert diagnostic.mismatch_summary == ('parameter shape: {"x":[1,false]} → {"x":[2,true]}',)


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
        target = dict(old["target"])
        target.update(role="cycle_count_baseline", revision="baseline")
        new[region] = [
            {
                "role": "cycle_count_baseline",
                "target": target,
                "campaign_id": "550e8400-e29b-41d4-a716-446655440000",
                "work_item_id": "item:0000:0123456789abcdef",
                "required_observation": "cycle_count",
                "manifest": {
                    "path_base": "origin_invocation",
                    "path": "targets/baseline/campaign/manifest.json",
                    "bytes": 1,
                    "sha256": "sha256:" + "a" * 64,
                    "kind": "simulation_campaign_manifest",
                    "owner": "550e8400-e29b-41d4-a716-446655440000",
                },
            }
        ]
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


@pytest.mark.parametrize(
    ("old_destinations", "new_destinations", "line"),
    [
        (("a",), ("head", "a"), "runtime input added: head"),
        (("a", "b"), ("b",), "runtime input removed: a"),
        (("a", "b"), ("b", "a"), "runtime input order changed"),
    ],
)
def test_runtime_declaration_list_changes_use_destination_identity(
    old_destinations, new_destinations, line
):
    from booley.flows.sim.campaign.planning import canonical_sha256

    old, new = _diagnostic_document(), _diagnostic_document()
    for document, destinations in ((old, old_destinations), (new, new_destinations)):
        declarations = []
        for destination in destinations:
            declaration = {"destination": destination, "source_artifact_path": destination}
            declarations.append({**declaration, "declaration_id": canonical_sha256(declaration)})
        document["workload"]["runtime_inputs"] = declarations
    diagnostic = _project_diagnostics(old, new)
    assert diagnostic.mismatch_summary == (line,)


def test_parameter_order_refusal_remains_visible():
    parameters = [{"name": "A", "value": 1}, {"name": "B", "value": 2}]
    diagnostic = _project_diagnostics(
        _diagnostic_document(parameters=parameters),
        _diagnostic_document(parameters=list(reversed(parameters))),
    )
    assert diagnostic.mismatch_summary == ("parameter order changed",)


def test_raw_comparator_pointer_value_contract_is_unchanged():
    old = _diagnostic_document(sources=[_diagnostic_source("a.sv")])
    new = _diagnostic_document(sources=[_diagnostic_source("a.sv", size=2)])
    left, right = _finalize_diagnostic_document(old), _finalize_diagnostic_document(new)
    raw = compare_manifests(left, right)
    assert [item.pointer for item in raw] == [
        "/build_variants/0/build_variant_id",
        "/build_variants/0/recipe_sha256",
        "/build_variants/0/source_closure/0/bytes",
        "/work_items/0/build_variant_id",
        "/work_items/0/fingerprint_sha256",
        "/work_items/0/work_item_id",
        "/workload/source_recipe/sources/0/bytes",
    ]
    assert (raw[-1].expected, raw[-1].actual) == (1, 2)
    assert (
        raw[-1].message
        == "/workload/source_recipe/sources/0/bytes: manifest has 1, current workload has 2"
    )


def test_independent_work_item_arguments_remain_visible():
    old, new = _diagnostic_document(), _diagnostic_document()
    new["work_items"][0]["arguments"] = ["+extra"]
    diagnostic = _project_diagnostics(old, new)
    assert any("/work_items/0/arguments/0" in line for line in diagnostic.mismatch_summary)
    assert diagnostic.derived_fingerprint_count == 2


def test_diagnostic_projection_needs_no_path_read_bytes_or_text(monkeypatch):
    from booley.flows.sim.campaign.planning import project_workload_mismatches

    old = _diagnostic_document(sources=[_diagnostic_source("relative/a.sv")])
    new = _diagnostic_document(sources=[_diagnostic_source("relative/a.sv", size=2)])
    left, right = _finalize_diagnostic_document(old), _finalize_diagnostic_document(new)

    def forbidden_read(*_args, **_kwargs):
        pytest.fail("diagnostic projection must not read files")

    monkeypatch.setattr(Path, "read_bytes", forbidden_read)
    monkeypatch.setattr(Path, "read_text", forbidden_read)
    diagnostic = project_workload_mismatches(left, right)
    assert diagnostic.mismatch_summary == ("source changed: relative/a.sv",)


def test_absolute_source_paths_remain_rejected_before_projection():
    with pytest.raises(SimulationCampaignIntegrityError, match="relative"):
        _finalize_diagnostic_document(
            _diagnostic_document(sources=[_diagnostic_source("/absolute/a.sv")])
        )


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


def test_prepared_path_and_content_equality_collapses_across_multiple_disclosures():
    old, new = _prepared_diagnostic_pair()
    for document in (old, new):
        document["planning_disclosures"] *= 2
    diagnostic = _project_diagnostics(old, new)
    assert diagnostic.mismatch_summary == ("source changed: a.sv",)
    assert diagnostic.derived_fingerprint_count == 5
    assert (
        len(
            [
                item
                for item in diagnostic.mismatches
                if item.pointer.startswith("/planning_disclosures/")
            ]
        )
        == 4
    )


@pytest.mark.parametrize(
    "changed",
    [
        "disclosure_duplicate",
        "planner",
        "kind",
        "contract",
        "version",
        "unmatched",
    ],
)
def test_ambiguous_or_unmatched_prepared_disclosure_changes_preserve_fallback(changed):
    old, new = _prepared_diagnostic_pair()
    disclosure = new["planning_disclosures"][0]
    if changed == "disclosure_duplicate":
        for document in (old, new):
            document["planning_disclosures"][0]["generated_files"] *= 2
    elif changed == "unmatched":
        disclosure["generated_files"][0]["path"] = "other.sv"
    elif changed == "planner":
        disclosure["planner"] = "other"
    else:
        field = {"kind": "kind", "contract": "contract_version", "version": "version"}[changed]
        disclosure["tool_provenance"][field] = "other"
    diagnostic = _project_diagnostics(old, new)
    assert any("/planning_disclosures/" in line for line in diagnostic.mismatch_summary)
    assert diagnostic.detail["mismatches"] == [item.message for item in diagnostic.mismatches]


def test_unremoved_planning_scratch_is_rejected_before_diagnostic_projection():
    _old, new = _prepared_diagnostic_pair()
    new["planning_disclosures"][0]["cleanup"]["removed"] = False
    with pytest.raises(SimulationCampaignIntegrityError, match="planning scratch must be removed"):
        _finalize_diagnostic_document(new)


@pytest.mark.parametrize("change", ["add", "remove", "reorder"])
def test_disclosure_source_list_shifts_preserve_whole_entry_and_positional_fallback(change):
    paths = {
        "add": (("a.sv",), ("head.sv", "a.sv")),
        "remove": (("a.sv", "b.sv"), ("b.sv",)),
        "reorder": (("a.sv", "b.sv"), ("b.sv", "a.sv")),
    }[change]
    old, new = [
        _diagnostic_document(sources=[_diagnostic_source(path) for path in side]) for side in paths
    ]
    for document in (old, new):
        document["planning_disclosures"] = [
            _prepared_disclosure(document["workload"]["source_recipe"]["sources"])
        ]
    diagnostic = _project_diagnostics(old, new)
    assert any(
        "/planning_disclosures/0/generated_files/" in line for line in diagnostic.mismatch_summary
    )
    assert diagnostic.derived_fingerprint_count == 5


def test_standalone_prepared_source_edit_is_not_misattributed_to_unchanged_recipe():
    old, _new = _prepared_diagnostic_pair()
    new = json.loads(json.dumps(old))
    new["planning_disclosures"][0]["generated_files"][0]["bytes"] = 2
    diagnostic = _project_diagnostics(old, new)
    assert len(diagnostic.mismatch_summary) == 1
    assert diagnostic.mismatch_summary == ("prepared source changed: a.sv",)
    assert "/planning_disclosures/0/generated_files/0/bytes" in diagnostic.mismatches[0].message
    assert diagnostic.derived_fingerprint_count == 0


def test_explicit_zero_findings_produces_no_typed_parameter_roots():
    from booley.flows.sim.campaign.planning import project_workload_mismatches

    old = _finalize_diagnostic_document(
        _diagnostic_document(parameters=[{"name": "N", "value": False}])
    )
    new = _finalize_diagnostic_document(
        _diagnostic_document(parameters=[{"name": "N", "value": 0}])
    )
    diagnostic = project_workload_mismatches(old, new, ())
    assert diagnostic.mismatch_summary == ()
    assert diagnostic.derived_fingerprint_count == 0


def _own_prepared_pair(changes):
    old, new = _diagnostic_document(), _diagnostic_document()
    for document, endpoint in ((old, 1), (new, 2)):
        document["planning_disclosures"] = [
            _prepared_disclosure([_diagnostic_source(path, size=values[endpoint - 1])])
            for path, *values in changes
        ]
    return old, new


@pytest.mark.parametrize(
    "changes,lines",
    [
        (
            [("staged/a.sv", 1, 2), ("staged/a.sv", 1, 2)],
            ("prepared source changed: staged/a.sv",),
        ),
        (
            [("a.sv", 1, 2), ("a.sv", 1, 3)],
            (
                "prepared source changed: a.sv (planning disclosure 0)",
                "prepared source changed: a.sv (planning disclosure 1)",
            ),
        ),
        (
            [("a.sv", 1, 2), ("a.sv", 3, 2)],
            (
                "prepared source changed: a.sv (planning disclosure 0)",
                "prepared source changed: a.sv (planning disclosure 1)",
            ),
        ),
        (
            [("a.sv", 1, 2), ("a.sv", 1, 3), ("a.sv", 1, 2)],
            (
                "prepared source changed: a.sv (planning disclosures 0, 2)",
                "prepared source changed: a.sv (planning disclosure 1)",
            ),
        ),
    ],
)
def test_own_prepared_changes_group_only_identical_observed_facts(changes, lines):
    diagnostic = _project_diagnostics(*_own_prepared_pair(changes))
    assert diagnostic.mismatch_summary == lines
    assert len(diagnostic.mismatches) == len(changes)
    assert diagnostic.derived_fingerprint_count == 0


def test_prepared_render_collision_retains_all_colliding_original_leaves():
    changes = [("x.sv", 1, 2), ("x.sv", 1, 3), ("x.sv (planning disclosure 1)", 1, 2)]
    diagnostic = _project_diagnostics(*_own_prepared_pair(changes))
    assert (
        diagnostic.mismatch_summary[0] == "prepared source changed: x.sv (planning disclosure 0)"
    )
    assert set(diagnostic.mismatch_summary[1:]) == {
        "workload changed: " + item.message for item in diagnostic.mismatches[1:]
    }
    assert diagnostic.derived_fingerprint_count == 0


@pytest.mark.parametrize("changed", ["recipe_duplicate", "bytes", "hash", "staged"])
def test_unmatched_prepared_facts_are_named_without_recipe_correspondence(changed):
    old, new = _prepared_diagnostic_pair()
    if changed == "recipe_duplicate":
        for document in (old, new):
            document["workload"]["source_recipe"]["sources"] *= 2
            document["build_variants"][0]["source_closure"] *= 2
    elif changed == "staged":
        for document in (old, new):
            document["planning_disclosures"][0]["generated_files"][0]["path"] = "staged/a.sv"
    else:
        field, value = ("bytes", 3) if changed == "bytes" else ("sha256", "sha256:" + "c" * 64)
        new["planning_disclosures"][0]["generated_files"][0][field] = value
    diagnostic = _project_diagnostics(old, new)
    expected_path = "staged/a.sv" if changed == "staged" else "a.sv"
    assert f"prepared source changed: {expected_path}" in diagnostic.mismatch_summary
    assert not any("/planning_disclosures/" in line for line in diagnostic.mismatch_summary)
    assert diagnostic.derived_fingerprint_count == 5


@pytest.mark.parametrize("own_count", [1, 2])
def test_equality_collapsed_occurrences_do_not_affect_own_key_suffixes(own_count):
    old, new = _prepared_diagnostic_pair()
    for document, endpoint in ((old, 0), (new, 1)):
        document["planning_disclosures"] += [
            _prepared_disclosure(
                [_diagnostic_source("a.sv", size=(3 + index, 5 + index)[endpoint])]
            )
            for index in range(own_count)
        ]
    diagnostic = _project_diagnostics(old, new)
    prepared = [line for line in diagnostic.mismatch_summary if line.startswith("prepared source")]
    assert prepared == (
        ["prepared source changed: a.sv"]
        if own_count == 1
        else [
            "prepared source changed: a.sv (planning disclosure 1)",
            "prepared source changed: a.sv (planning disclosure 2)",
        ]
    )
    assert diagnostic.mismatch_summary[0] == "source changed: a.sv"


@pytest.mark.parametrize("changed", ["scratch", "path_layout", "entry_kind"])
def test_prepared_pair_and_entry_guards_retain_otherwise_collapsible_leaves(changed):
    old, new = _prepared_diagnostic_pair()
    if changed == "scratch":
        new["planning_disclosures"][0]["scratch_inputs"] = [_diagnostic_source("scratch.sv")]
    elif changed == "entry_kind":
        new["planning_disclosures"][0]["generated_files"][0]["kind"] = "rtl"
    else:
        new["planning_disclosures"][0]["generated_files"].append(
            {**_diagnostic_source("head.sv"), "kind": "generated_input"}
        )
    diagnostic = _project_diagnostics(old, new)
    original = [
        item.message
        for item in diagnostic.mismatches
        if item.pointer.startswith("/planning_disclosures/")
    ]
    assert {"workload changed: " + item for item in original} <= set(diagnostic.mismatch_summary)
    assert not any(
        line.startswith("prepared source changed:") for line in diagnostic.mismatch_summary
    )


@pytest.mark.parametrize("side", [0, 1])
@pytest.mark.parametrize("field,value", [("bytes", 7), ("sha256", "sha256:" + "c" * 64)])
def test_either_side_unequal_prepared_content_has_its_own_named_cause(side, field, value):
    old, new = _prepared_diagnostic_pair()
    (old, new)[side]["planning_disclosures"][0]["generated_files"][0][field] = value
    diagnostic = _project_diagnostics(old, new)
    assert diagnostic.mismatch_summary == ("source changed: a.sv", "prepared source changed: a.sv")
    assert diagnostic.derived_fingerprint_count == 5


def test_distinct_prepared_hash_only_changes_preserve_each_observed_cause():
    old, new = _own_prepared_pair([("staged.sv", 1, 1), ("staged.sv", 1, 1)])
    for position, digest in enumerate(("b", "c")):
        new["planning_disclosures"][position]["generated_files"][0]["sha256"] = (
            "sha256:" + digest * 64
        )
    diagnostic = _project_diagnostics(old, new)
    assert diagnostic.mismatch_summary == (
        "prepared source changed: staged.sv (planning disclosure 0)",
        "prepared source changed: staged.sv (planning disclosure 1)",
    )
    assert [item.pointer for item in diagnostic.mismatches] == [
        "/planning_disclosures/0/generated_files/0/sha256",
        "/planning_disclosures/1/generated_files/0/sha256",
    ]
    assert diagnostic.derived_fingerprint_count == 0


@pytest.mark.parametrize("side", [0, 1])
def test_generated_path_duplicates_on_either_side_block_prepared_naming(side):
    old, new = _prepared_diagnostic_pair()
    (old, new)[side]["planning_disclosures"][0]["generated_files"] *= 2
    diagnostic = _project_diagnostics(old, new)
    original = [
        item.message
        for item in diagnostic.mismatches
        if item.pointer.startswith("/planning_disclosures/")
    ]
    assert {"workload changed: " + item for item in original} <= set(diagnostic.mismatch_summary)


@pytest.mark.parametrize("changed", ["planner", "kind", "contract", "version"])
def test_unknown_preparation_metadata_falls_back_but_equal_versions_allow_collapse(changed):
    old, new = _prepared_diagnostic_pair()
    for document in (old, new):
        disclosure = document["planning_disclosures"][0]
        if changed == "planner":
            disclosure["planner"] = "unknown"
        else:
            field = {"kind": "kind", "contract": "contract_version", "version": "version"}[changed]
            disclosure["tool_provenance"][field] = (
                "unavailable" if changed == "version" else "unknown"
            )
    diagnostic = _project_diagnostics(old, new)
    if changed == "version":
        assert diagnostic.mismatch_summary == ("source changed: a.sv",)
    else:
        assert any("/planning_disclosures/" in line for line in diagnostic.mismatch_summary)


def test_repeated_diagnostic_region_lookups_do_not_rescan_original_findings():
    from booley.flows.sim.campaign.planning import WorkloadMismatch, _DiagnosticProjection

    class CountingFindings(tuple):
        walks = 0
        indexed_reads = 0

        def __getitem__(self, index):
            self.indexed_reads += 1
            return super().__getitem__(index)

        def __iter__(self):
            self.walks += 1
            return super().__iter__()

    findings = CountingFindings(
        [WorkloadMismatch(f"/build_variants/{index}/sha256", "old", "new") for index in range(200)]
        + [WorkloadMismatch("/build_variants/0/sha256", "other", "new")]
    )
    projection = _DiagnosticProjection(findings)
    original_walks = findings.walks
    original_indexed_reads = findings.indexed_reads
    for _ in range(20):
        assert projection.indices("/build_variants/0/sha256") == {0, 200}
        assert projection.indices("/build_variants/0") == {0, 200}
        assert projection.indices("/build_variants") == set(range(201))
        assert projection.indices("/build_variants/20") == {20}
        assert projection.indices("/build_variants/2") == {2}
        assert projection.indices("/absent") == set()
    assert findings.walks == original_walks
    assert findings.indexed_reads == original_indexed_reads
    projection.explain({0, 200}, ("same region changed",))
    projection.derived.add(1)
    diagnostic = projection.finish()
    assert diagnostic.mismatches is findings
    assert diagnostic.derived_fingerprint_count == 1
    assert len(diagnostic.mismatch_summary) == 199


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


def test_diagnostic_prefix_lookup_preserves_root_and_escaped_component_boundaries():
    from booley.flows.sim.campaign.planning import WorkloadMismatch, _DiagnosticProjection

    pointers = ("", "/", "//child", "/scope/~1x", "/scope/~1x/child", "/scope/~1xy")
    projection = _DiagnosticProjection(tuple(WorkloadMismatch(p, "old", "new") for p in pointers))
    assert projection.indices("") == set(range(6))
    assert projection.indices("/") == {1, 2}
    assert projection.indices("/scope/~1x") == {3, 4}
    assert projection.indices("/scope/~1") == set()
    selected = projection.indices("/scope/~1x")
    selected.clear()
    assert projection.indices("/scope/~1x") == {3, 4}


def test_projected_root_membership_does_not_scan_ordered_output():
    from booley.flows.sim.campaign.planning import WorkloadMismatch, _DiagnosticProjection

    class CountingRoots(list):
        membership_checks = 0

        def __contains__(self, line):
            self.membership_checks += 1
            return super().__contains__(line)

    finding = WorkloadMismatch("/unknown", "old", "new")
    projection = _DiagnosticProjection((finding, finding))
    projection.roots = CountingRoots()
    expected = [f"source changed: source_{index}.sv" for index in range(200)]
    projection.explain(set(), expected)
    projection.explain(set(), list(reversed(expected)))
    assert projection.roots == expected
    assert projection.roots.membership_checks == 0
    diagnostic = projection.finish()
    fallback = f"workload changed: {finding.message}"
    assert diagnostic.mismatch_summary == (*expected, fallback, fallback)
    assert projection.root_lines == set(diagnostic.mismatch_summary)
    assert diagnostic.mismatches == (finding, finding)
