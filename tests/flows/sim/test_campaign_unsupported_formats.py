"""Booley 0.3.0 Simulation Campaign records are refused with one actionable message."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from booley.flows.sim.campaign import inspect_retained_campaign
from booley.flows.sim.campaign.codec import (
    canonical_json_bytes,
    decode_simulation_campaign_manifest,
)
from booley.flows.sim.campaign.resume import validate_resume_manifest
from booley.flows.sim.campaign.store import CampaignStore
from booley.flows.sim.campaign_retention import CampaignRetentionError, prune_invocation
from booley.flows.sim.flow import SimulateFlow
from booley.flows.sim.request import SimRequest
from booley.targets.catalog import TargetCatalog
from tests.flows.sim.test_campaign_manifest_codec import _linked_campaign_stores, _manifest
from tests.flows.sim.test_campaign_phase6 import _retained_invocation
from tests.flows.sim.test_campaign_resume_diagnostics import _interrupted_campaign
from tests.flows.sim.test_campaign_retention import _run_retention_cli

_MANIFEST = "booley.simulation-campaign-manifest/"
_RESULT_V2 = "booley.simulation-result/v2"
_PROJECTION_V2 = "booley.simulation-projection/v2"
_UNSUPPORTED = "UnsupportedSimulationCampaignFormatError"


def _assert_refused(text: str, schema: str, invocation: Path | None = None) -> None:
    """The one shape of every refusal: 0.3.0, the schema, the directory, no corruption wording."""
    assert "written by Booley 0.3.0 and is no longer supported" in text
    assert schema in text
    if invocation is not None:
        assert f"delete the Simulation invocation directory {invocation} by hand" in text
    for corrupt in ("integrity failure", "is invalid", "corrupt", "Traceback"):
        assert corrupt not in text


def _assert_unsupported_type(error: BaseException) -> None:
    """Compare by name: the class must not be an integrity error, whatever it inherits."""
    assert type(error).__name__ == _UNSUPPORTED
    assert "SimulationCampaignIntegrityError" not in [t.__name__ for t in type(error).__mro__]


def _as_030_manifest(raw: bytes, version: str) -> bytes:
    """Re-encode a current manifest the way Booley 0.3.0 wrote it: no ``test_runs``."""
    document = json.loads(raw)
    document["$schema"] = _MANIFEST + version
    if version == "v2":
        document["workload"]["no_waivers"] = True
    for item in document["work_items"]:
        item.pop("test_runs", None)
    return canonical_json_bytes(document)


def _rewrite(path: Path, raw: bytes) -> None:
    path.chmod(0o600)
    path.write_bytes(raw)


def _as_030_invocation(store, version: str) -> None:
    """Turn a retained current Campaign into a 0.3.0 one, including its child mirror."""
    _rewrite(store.manifest_path, _as_030_manifest(store.manifest_path.read_bytes(), version))
    mirror = store.root / "child-executions" / "entries"
    mirror.mkdir(parents=True)
    (mirror / f"{'1' * 32}.json").write_text("{}", encoding="utf-8")


@pytest.mark.parametrize("version", ["v1", "v2"])
def test_manifest_decoder_refuses_0_3_0_manifests(version: str) -> None:
    raw = _as_030_manifest(canonical_json_bytes(_manifest()), version)
    with pytest.raises(ValueError) as excinfo:
        decode_simulation_campaign_manifest(raw)
    _assert_unsupported_type(excinfo.value)
    _assert_refused(str(excinfo.value), _MANIFEST + version)


def test_resume_refuses_a_0_3_0_manifest_naming_its_invocation(tmp_path, monkeypatch) -> None:
    reports, manifest, _executions = _interrupted_campaign(tmp_path, monkeypatch)
    _rewrite(manifest, _as_030_manifest(manifest.read_bytes(), "v1"))

    result = SimulateFlow().execute(
        SimRequest(resume_from=manifest, work_dir=tmp_path, report_dir=reports)
    )

    assert result.exit_code == 2
    _assert_refused(result.outcome.report_text, _MANIFEST + "v1", reports / "sim/1")


def test_full_prune_cli_refuses_a_0_3_0_manifest_naming_its_invocation(tmp_path) -> None:
    reports, _project_data, store = _retained_invocation(tmp_path)
    _as_030_invocation(store, "v2")

    result = _run_retention_cli("--reports-root", str(reports), "--invocation", "1", "--full")

    assert result.returncode == 2
    _assert_refused(result.stderr, _MANIFEST + "v2", reports / "sim/1")
    assert (reports / "sim/1").is_dir()


def test_inspection_refuses_a_0_3_0_manifest_naming_its_invocation(tmp_path) -> None:
    reports, _project_data, store = _retained_invocation(tmp_path)
    _as_030_invocation(store, "v1")

    with pytest.raises(ValueError) as excinfo:
        inspect_retained_campaign(store.manifest_path)

    _assert_unsupported_type(excinfo.value)
    _assert_refused(str(excinfo.value), _MANIFEST + "v1", reports / "sim/1")


def _coverage_target(tmp_path, monkeypatch) -> tuple[Path, Path]:
    """A completed Coverage run: the reports root and its Target report directory."""
    from tests.flows.sim.test_coverage_invocation import project
    from tests.flows.sim.test_coverage_transaction import NativeExecution

    monkeypatch.setenv("BOOLEY_CONTAINER", "1")
    project(tmp_path)
    data = tmp_path / ".booley"
    data.mkdir()
    (data / "tests.toml").write_text('[sim_0]\ntests = ["reset", "wrap"]\n')
    reports = tmp_path / "reports"
    flow = SimulateFlow(
        coverage_execution=lambda _handle, _options, _cmds, _access: NativeExecution()
    )
    request = SimRequest(target="sim_0", work_dir=tmp_path, coverage=True, report_dir=reports)
    assert flow.execute(request).exit_code == 0
    return reports, reports / "sim/1/targets/sim_0"


def test_coverage_analysis_refuses_a_0_3_0_manifest_naming_its_invocation(
    tmp_path, monkeypatch
) -> None:
    from booley.flows.sim.coverage_analysis_input import (
        CoverageAnalysisError,
        read_coverage_campaign,
    )

    reports, target = _coverage_target(tmp_path, monkeypatch)
    manifest = target / "campaign/manifest.json"
    _rewrite(manifest, _as_030_manifest(manifest.read_bytes(), "v2"))

    with pytest.raises(CoverageAnalysisError) as excinfo:
        read_coverage_campaign(target / "coverage.json")

    _assert_refused(str(excinfo.value), _MANIFEST + "v2", reports / "sim/1")
    assert "fully pruned" not in str(excinfo.value)


def test_coverage_analysis_refuses_a_0_3_0_projection_naming_its_invocation(
    tmp_path, monkeypatch
) -> None:
    from booley.flows.sim.coverage_analysis_input import (
        CoverageAnalysisError,
        read_coverage_campaign,
    )

    reports, target = _coverage_target(tmp_path, monkeypatch)
    projection = json.loads((target / "simulation.json").read_bytes())
    projection["$schema"] = _PROJECTION_V2
    del projection["test_runs"]
    (target / "simulation.json").write_bytes(canonical_json_bytes(projection))

    with pytest.raises(CoverageAnalysisError) as excinfo:
        read_coverage_campaign(target / "coverage.json")

    _assert_refused(str(excinfo.value), _PROJECTION_V2, reports / "sim/1")


def test_resume_refuses_a_0_3_0_cycle_count_baseline_naming_its_invocation(
    tmp_path, monkeypatch
) -> None:
    candidate, candidate_store, _baseline, baseline_store = _linked_campaign_stores(tmp_path)
    _rewrite(
        baseline_store.manifest_path,
        _as_030_manifest(baseline_store.manifest_path.read_bytes(), "v1"),
    )
    root = tmp_path / "source"
    root.mkdir()
    target = candidate.document["target"]
    monkeypatch.setattr(
        TargetCatalog,
        "build",
        lambda _root: SimpleNamespace(
            select=lambda selector, **_: SimpleNamespace(
                identity=f"{target['vlnv']}#{target['name']}", selector=selector
            )
        ),
    )
    monkeypatch.setattr(
        "booley.flows.sim.campaign.resume.git_full_sha", lambda *_args: target["revision"]
    )

    with pytest.raises(ValueError) as excinfo:
        validate_resume_manifest(
            candidate_store.manifest_path,
            project_root=root,
            revision_root=lambda _target: root,
        )

    _assert_unsupported_type(excinfo.value)
    _assert_refused(str(excinfo.value), _MANIFEST + "v1", tmp_path / "reports" / "000001")


def test_dependents_pruning_refuses_a_0_3_0_manifest_naming_its_invocation(tmp_path) -> None:
    reports, _project_data, store = _retained_invocation(tmp_path)
    _as_030_invocation(store, "v1")

    with pytest.raises(CampaignRetentionError) as excinfo:
        prune_invocation(reports, 1, include_dependents=True)

    _assert_refused(str(excinfo.value), _MANIFEST + "v1", reports / "sim/1")


def test_a_schema_less_projection_cannot_authenticate_a_coverage_campaign() -> None:
    from booley.flows.sim.campaign.projection import decode_simulation_projection

    schema_less = {
        "flow": "sim",
        "target": "sim",
        "target_identity": "::t:0#sim",
        "complete": True,
    }

    with pytest.raises(ValueError, match="cannot authenticate a Coverage Campaign"):
        decode_simulation_projection(schema_less, coverage_expected=True)


def test_a_store_outside_the_report_layout_cannot_name_an_invocation(tmp_path) -> None:
    store = CampaignStore(tmp_path / "campaign")
    store.root.mkdir()
    store.manifest_path.write_bytes(_as_030_manifest(canonical_json_bytes(_manifest()), "v1"))

    with pytest.raises(ValueError) as excinfo:
        store.load_manifest()

    _assert_unsupported_type(excinfo.value)
    _assert_refused(str(excinfo.value), _MANIFEST + "v1")


def test_a_0_3_0_result_is_refused_naming_its_invocation(tmp_path) -> None:
    reports, _project_data, store = _retained_invocation(tmp_path)
    result_path = next(store.root.glob("work-items/*/result.json"))
    result = json.loads(result_path.read_bytes())
    result["$schema"] = _RESULT_V2
    for observation in result["observations"]:
        observation.pop("test_run", None)
    _rewrite(result_path, canonical_json_bytes(result))

    with pytest.raises(ValueError) as excinfo:
        store.scan()

    _assert_unsupported_type(excinfo.value)
    _assert_refused(str(excinfo.value), _RESULT_V2, reports / "sim/1")


def test_a_0_3_0_projection_is_refused_naming_its_invocation(tmp_path) -> None:
    reports, _project_data, store = _retained_invocation(tmp_path)
    projection_path = store.root.parent / "simulation.json"
    projection = json.loads(projection_path.read_bytes())
    projection.update(
        {
            "$schema": _PROJECTION_V2,
            "campaign_id": store.load_manifest().document["campaign_id"],
            "campaign_manifest": {},
        }
    )
    projection_path.write_bytes(canonical_json_bytes(projection))

    with pytest.raises(CampaignRetentionError) as excinfo:
        prune_invocation(reports, 1)

    _assert_refused(str(excinfo.value), _PROJECTION_V2, reports / "sim/1")


def test_resume_ignores_a_leftover_project_child_registry(tmp_path, monkeypatch) -> None:
    """The child-execution registry is no longer read: a malformed entry cannot block resume."""
    reports, manifest, _executions = _interrupted_campaign(tmp_path, monkeypatch)
    entries = tmp_path / ".booley/.runtime/campaign-child-executions/entries"
    entries.mkdir(parents=True)
    (entries / f"{'1' * 32}.json").write_text("not json", encoding="utf-8")

    def resumed(_executor, _request):
        raise KeyboardInterrupt("resume reached execution")

    monkeypatch.setattr("booley.flows.sim.flow.OrdinaryHdlSerialExecutor.execute", resumed)

    with pytest.raises(KeyboardInterrupt, match="resume reached execution"):
        SimulateFlow().execute(
            SimRequest(resume_from=manifest, work_dir=tmp_path, report_dir=reports)
        )
