"""Guards that replaced decode-time validation of manifest paths and baselines (#2136)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from booley.flows.sim.config import parse_run_cwd_template
from booley.flows.sim.record.bundle import authenticate_bundle_artifacts
from booley.flows.sim.record.codec import (
    SimulationCampaignIntegrityError,
    canonical_json_bytes,
    decode_simulation_result,
    decode_simulator_bundle,
    linked_manifest_path,
)
from booley.flows.sim.record.flow_planning import plan_ordinary_hdl_campaign
from booley.flows.sim.record.inspection import (
    inspect_retained_campaign,
    read_durable_results,
    recover_retained_campaign_resources,
)
from booley.flows.sim.record.planning import manifest_digest
from tests.flows.sim.simulation_record_test_support import (
    _completed,
    _read,
    _recovery_executor,
    _request,
    _write,
)
from tests.flows.sim.test_simulation_record_codec_golden import _simulation_result
from tests.flows.sim.test_simulation_record_flow_planning import _planning_fixture


@pytest.mark.parametrize(
    "path",
    [
        "targets/x/record/manifest.json",
        "targets/x%40baseline-abc/record/manifest.json",
    ],
)
def test_a_planned_prerequisite_path_stays_inside_its_invocation(
    tmp_path: Path, path: str
) -> None:
    assert linked_manifest_path(tmp_path, {"path": path}) == tmp_path / path


@pytest.mark.parametrize(
    "path",
    [
        "../outside/targets/x/record/manifest.json",
        "targets/../record/manifest.json",
        "targets/x/record/manifest.json/extra",
        "/abs/targets/x/record/manifest.json",
        None,
    ],
)
def test_a_prerequisite_path_that_is_not_a_contained_manifest_is_refused(
    tmp_path: Path, path: object
) -> None:
    with pytest.raises(SimulationCampaignIntegrityError):
        linked_manifest_path(tmp_path, {"path": path})


def test_planning_refuses_a_prerequisite_not_planned_as_a_baseline(tmp_path: Path) -> None:
    """A Cocotb baseline is planned as a candidate, so its link would point nowhere."""
    _project, handle, inspection, preview = _planning_fixture(tmp_path)
    prerequisite = {"target": {"selector": "base", "role": "candidate"}}

    with pytest.raises(
        SimulationCampaignIntegrityError, match=r"'base' was not planned as a Cycle Count baseline"
    ):
        plan_ordinary_hdl_campaign(
            handle=handle,
            inspection=inspection,
            preview=preview,
            groups=preview.groups,
            required_suite=("reset", "count"),
            revision="abc123",
            invocation_id=1,
            execution_id="",
            trace=False,
            prerequisite_documents=(prerequisite,),
        )


@pytest.mark.parametrize("template", ["../shared", "a/../b", "..\\shared", "a\\..\\b"])
def test_run_cwd_refuses_a_parent_component_with_either_separator(template: str) -> None:
    with pytest.raises(ValueError, match=r"must not contain a '\.\.' component"):
        parse_run_cwd_template(template)


def test_a_bundle_artifact_path_outside_its_root_is_refused_before_use(tmp_path: Path) -> None:
    """The decoder no longer checks artifact paths, so the file operation must (#1743)."""
    bundle_root = tmp_path / "bundle"
    bundle_root.mkdir()
    escaped = tmp_path / "escape"
    escaped.write_bytes(b"x")
    fixture = Path(__file__).parents[1] / "fixtures/simulation_record/bundle.json"
    document = json.loads(fixture.read_bytes())
    artifact = document["artifacts"][0]
    artifact.update(path="../escape", bytes=1, sha256="sha256:" + hashlib.sha256(b"x").hexdigest())
    bundle = decode_simulator_bundle(canonical_json_bytes(document))

    with pytest.raises(SimulationCampaignIntegrityError, match="contained"):
        authenticate_bundle_artifacts(bundle, bundle_root)


@pytest.mark.parametrize(
    "case",
    [
        "shared_failure",
        "shared_failure_text",
        "shared_failure_detail",
        "ordinal",
        "run_directory",
        "ready_bundle",
        "bundle_path",
        "build_state",
    ],
)
def test_a_malformed_published_record_is_corrupt_storage(tmp_path: Path, case: str) -> None:
    """Nested data the decoders no longer check fails as the integrity error where it is read."""
    completed = _completed(tmp_path)
    store = completed.store
    build_path = completed.build_dir / "build-result.json"
    build_result = _read(build_path)
    variant = build_result["build_variant_id"]
    if case.startswith("shared_failure"):
        # A second item of the same variant reuses the tampered shared result at launch.
        observation = {
            "shared_failure": None,
            "shared_failure_text": {"class": "design", "code": "c", "message": 42, "detail": {}},
            "shared_failure_detail": {
                "class": "design",
                "code": "c",
                "message": "m",
                "detail": "x",
            },
        }[case]
        build_result.update(state="design_failure", bundle=None, observation=observation)
        _write(build_path, build_result)
        manifest = store.load_manifest()
        item = manifest.document["work_items"][0]
        request = _request(store, manifest, item, tmp_path / "project", invocation=1)
        with pytest.raises(SimulationCampaignIntegrityError):
            _recovery_executor(tmp_path / "engine-build").execute(request)
    elif case == "ordinal":
        attempt_path = completed.attempt_dir / "attempt.json"
        _write(attempt_path, _read(attempt_path) | {"attempt_ordinal": "1"})
        with pytest.raises(SimulationCampaignIntegrityError):
            store.scan()
    elif case == "run_directory":
        attempt_path = completed.attempt_dir / "attempt.json"
        _write(attempt_path, _read(attempt_path) | {"run_directory": "x"})
        result_path = store.work_item_directory(completed.item_id) / "result.json"
        result_path.chmod(0o600)
        result_path.unlink()
        status = inspect_retained_campaign(store.manifest_path)
        assert status.interrupted == (completed.item_id,)
        for project_data in (None, tmp_path / "project"):
            with pytest.raises(SimulationCampaignIntegrityError):
                recover_retained_campaign_resources(status, project_data)
    else:
        if case == "ready_bundle":
            build_result["bundle"] = None
        elif case == "bundle_path":
            build_result["bundle"]["manifest_path"] = "../escape"
        else:
            build_result["state"] = "bogus"
        _write(build_path, build_result)
        with pytest.raises(SimulationCampaignIntegrityError):
            store.recover_shared_build(variant, 1)


def _bundle_with_artifacts(artifacts: list[dict[str, object]]):
    fixture = Path(__file__).parents[1] / "fixtures/simulation_record/bundle.json"
    document = json.loads(fixture.read_bytes())
    document["artifacts"] = artifacts
    return decode_simulator_bundle(canonical_json_bytes(document))


@pytest.mark.parametrize("case", ["unknown_kind", "duplicate_path", "no_executable"])
def test_a_bundle_inventory_the_decoder_no_longer_checks_is_refused(case: str) -> None:
    from booley.flows.sim.record.bundle import require_bundle_inventory

    executable = {"bytes": 1, "kind": "simulator_executable", "path": "simv", "sha256": "sha256:b"}
    artifacts = {
        "unknown_kind": [executable | {"kind": "bogus"}],
        "duplicate_path": [executable, executable],
        "no_executable": [executable | {"kind": "runtime_data"}],
    }[case]
    with pytest.raises(SimulationCampaignIntegrityError):
        require_bundle_inventory(_bundle_with_artifacts(artifacts))


def test_a_deeply_nested_record_is_refused_at_decode() -> None:
    """Skipping the nested-field checks must not skip the nesting ceiling (#1743)."""
    fixture = Path(__file__).parents[1] / "fixtures/simulation_record/bundle.json"
    document = json.loads(fixture.read_bytes())
    document["created_at"] = "X"
    raw = canonical_json_bytes(document).replace(b'"X"', b"[" * 600 + b"]" * 600)
    with pytest.raises(SimulationCampaignIntegrityError, match="nesting"):
        decode_simulator_bundle(raw)


def _refuse_malformed_result(root: Path, case: str) -> None:
    root.mkdir()
    completed = _completed(root)
    store = completed.store
    result_path = store.work_item_directory(completed.item_id) / "result.json"
    result = _read(result_path)
    if case == "grade":
        result["grade"] = "PASS"
    elif case == "observation_missing":
        del result["observations"][0]["execution"]
    elif case == "observation_enum":
        result["observations"][0]["assertions"] = "unknown"
    elif case == "evidence_path":
        escape = {"path": "/abs/escape", "bytes": 1, "sha256": "sha256:" + "0" * 64}
        result["evidence"] = [escape | {"kind": "log", "owner": result["attempt_id"]}]
    else:
        result["build_result"]["sharing"] = "shared_variant"
        result["build_result"]["path"] = "../../escape"
    _write(result_path, result)
    manifest = store.load_manifest()
    digest = manifest_digest(manifest)
    with pytest.raises(SimulationCampaignIntegrityError):
        if case == "grade":
            store.regenerate_summary(manifest, digest)
        elif case.startswith("observation"):
            read_durable_results(store.manifest_path, digest)
        else:
            inspect_retained_campaign(store.manifest_path)


def test_a_malformed_published_result_is_corrupt_storage(tmp_path: Path) -> None:
    """Nested result data the decoder no longer checks fails as the integrity error at its use (#1744)."""
    for case in (
        "grade",
        "observation_missing",
        "observation_enum",
        "evidence_path",
        "build_path",
    ):
        _refuse_malformed_result(tmp_path / case, case)


def test_result_observations_refuses_a_malformed_baseline_cycle_count() -> None:
    """The prerequisite baseline path reads the observation's cycle count (#1744)."""
    from booley.flows.sim.record.coordinator import result_observations

    for cycle_count in ("7", -1, True):
        document = json.loads(_simulation_result("completed"))
        document["observations"][0]["cycle_count"] = cycle_count
        result = decode_simulation_result(canonical_json_bytes(document))
        with pytest.raises(SimulationCampaignIntegrityError):
            result_observations(result)


def test_result_observations_refuses_an_observation_that_could_override_identity() -> None:
    """An extra member would override the work item identity in acceptance facts (#1744)."""
    from booley.flows.sim.record.coordinator import result_observations

    document = json.loads(_simulation_result("completed"))
    document["observations"][0]["role"] = "candidate"
    result = decode_simulation_result(canonical_json_bytes(document))
    with pytest.raises(SimulationCampaignIntegrityError):
        result_observations(result)


def test_result_observations_refuses_observations_that_are_not_a_list() -> None:
    """A mapping in place of the observation list is refused, not iterated by key (#1744)."""
    from booley.flows.sim.record.coordinator import result_observations

    document = json.loads(_simulation_result("completed"))
    document["observations"] = {}
    result = decode_simulation_result(canonical_json_bytes(document))
    with pytest.raises(SimulationCampaignIntegrityError):
        result_observations(result)


def test_coverage_owner_refuses_malformed_result_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A result whose evidence lacks a member the owner check reads is a reference error (#1744)."""
    from booley.flows.sim import coverage_reference, record
    from booley.flows.sim.coverage_reference import CoverageCampaignReferenceError

    evidence = {"kind": "coverage_record_manifest"}
    authenticated = SimpleNamespace(
        manifest=SimpleNamespace(document={"target": {}}),
        result=SimpleNamespace(document={"evidence": (evidence,)}),
    )
    monkeypatch.setattr(record, "authenticate_work_item", lambda *_args, **_kwargs: authenticated)
    monkeypatch.setattr(coverage_reference, "crossed_booley_dir_entry", lambda _path: None)
    resolved = SimpleNamespace(
        reference=SimpleNamespace(
            document={"coverage_record": {}, "simulation_work_item_id": "item"}
        ),
        reference_path=tmp_path / "reference.json",
    )
    with pytest.raises(CoverageCampaignReferenceError, match="evidence is malformed"):
        coverage_reference.authenticate_coverage_campaign_owner(resolved)  # type: ignore[arg-type]
