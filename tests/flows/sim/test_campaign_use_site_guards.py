"""Guards that replaced decode-time validation of manifest paths and baselines (#2136)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from booley.flows.sim.campaign.bundle import authenticate_bundle_artifacts
from booley.flows.sim.campaign.codec import (
    SimulationCampaignIntegrityError,
    canonical_json_bytes,
    decode_simulator_bundle,
    linked_manifest_path,
)
from booley.flows.sim.campaign.flow_planning import plan_ordinary_hdl_campaign
from booley.flows.sim.campaign.inspection import (
    inspect_retained_campaign,
    recover_retained_campaign_resources,
)
from booley.flows.sim.config import parse_run_cwd_template
from tests.flows.sim.test_campaign_flow_planning import _planning_fixture
from tests.flows.sim.test_campaign_phase3_adversarial import (
    _completed,
    _executor,
    _read,
    _write,
)
from tests.flows.sim.test_campaign_phase3_integrity import _request


@pytest.mark.parametrize(
    "path",
    [
        "targets/x/campaign/manifest.json",
        "targets/x%40baseline-abc/campaign/manifest.json",
    ],
)
def test_a_planned_prerequisite_path_stays_inside_its_invocation(
    tmp_path: Path, path: str
) -> None:
    assert linked_manifest_path(tmp_path, {"path": path}) == tmp_path / path


@pytest.mark.parametrize(
    "path",
    [
        "../outside/targets/x/campaign/manifest.json",
        "targets/../campaign/manifest.json",
        "targets/x/campaign/manifest.json/extra",
        "/abs/targets/x/campaign/manifest.json",
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
    fixture = Path(__file__).parents[1] / "fixtures/simulation_campaign/bundle.json"
    document = json.loads(fixture.read_bytes())
    artifact = document["artifacts"][0]
    artifact.update(path="../escape", bytes=1, sha256="sha256:" + hashlib.sha256(b"x").hexdigest())
    bundle = decode_simulator_bundle(canonical_json_bytes(document))

    with pytest.raises(SimulationCampaignIntegrityError, match="contained"):
        authenticate_bundle_artifacts(bundle, bundle_root)


@pytest.mark.parametrize("case", ["shared_failure", "ordinal", "run_directory", "ready_bundle"])
def test_a_malformed_published_record_is_corrupt_storage(tmp_path: Path, case: str) -> None:
    """Nested data the decoders no longer check fails as the integrity error where it is read."""
    completed = _completed(tmp_path)
    store = completed.store
    build_path = completed.build_dir / "build-result.json"
    build_result = _read(build_path)
    variant = build_result["build_variant_id"]
    if case == "shared_failure":
        # A second item of the same variant reuses the tampered shared result at launch.
        build_result.update(state="design_failure", bundle=None, observation=None)
        _write(build_path, build_result)
        manifest = store.load_manifest()
        item = manifest.document["work_items"][0]
        request = _request(store, manifest, item, tmp_path / "project", invocation=1)
        with pytest.raises(SimulationCampaignIntegrityError):
            _executor(tmp_path / "engine-build").execute(request)
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
        build_result["bundle"] = None
        _write(build_path, build_result)
        with pytest.raises(SimulationCampaignIntegrityError):
            store.recover_shared_build(variant, 1)
