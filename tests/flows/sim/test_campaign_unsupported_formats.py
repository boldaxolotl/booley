"""Simulation records an older Booley wrote are refused with one actionable message.

``tests/flows/fixtures/booley_0_3_0`` holds three invocation directories (``complete``,
``interrupted`` and ``coverage``) that Booley 0.3.0's own ``SimulateFlow`` wrote, with only
the simulator replaced. They are captured data, not test cases: each text file embeds
its capture's temporary root, replaced by ``/booley-0.3.0-capture``; the lock files,
run logs and simulator executables were dropped.
"""

from __future__ import annotations

import json
import shutil
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
from booley.flows.sim.coverage_json import content_digest
from booley.flows.sim.flow import SimulateFlow
from booley.flows.sim.request import SimRequest
from booley.targets.catalog import TargetCatalog
from tests.flows.sim.campaign_test_support import _retained_invocation, coverage_invocation
from tests.flows.sim.test_campaign_manifest_codec import _linked_campaign_stores, _manifest
from tests.flows.sim.test_campaign_resume_diagnostics import _interrupted_campaign
from tests.flows.sim.test_campaign_retention import _run_retention_cli

_MANIFEST = "booley.simulation-campaign-manifest/"
_RESULT_V2 = "booley.simulation-result/v2"
_PROJECTION_V2 = "booley.simulation-projection/v2"
_UNSUPPORTED = "UnsupportedSimulationCampaignFormatError"
# Where a plain prune stops: a coverage run is refused at its Coverage Campaign reference,
# which the retention code reads before the Simulation manifest.
_REFUSED_AT_RECORD = pytest.mark.parametrize(
    ("kind", "schema"),
    [("complete", _MANIFEST + "v1"), ("coverage", "booley.coverage-campaign/v4")],
    ids=["complete", "coverage"],
)
_FIXTURES_0_3_0 = Path(__file__).parents[1] / "fixtures" / "booley_0_3_0"


@pytest.fixture
def reports(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The report root of a Project whose Booley Directory is empty, inside the Sandbox."""
    monkeypatch.setenv("BOOLEY_CONTAINER", "1")
    root = tmp_path / ".booley" / ".runtime" / "flow-reports"
    root.mkdir(parents=True)
    return root


def copy_0_3_0_invocation(kind: str, reports: Path) -> Path:
    """Put the captured 0.3.0 ``complete``, ``interrupted`` or ``coverage`` run at ``sim/1``."""
    return Path(shutil.copytree(_FIXTURES_0_3_0 / kind / "sim" / "1", reports / "sim" / "1"))


def _manifest_of(invocation: Path, kind: str) -> Path:
    """The Simulation Campaign manifest of the captured run's one Target."""
    target = "sim" if kind == "complete" else "sim_0"
    return invocation / "targets" / target / "campaign" / "manifest.json"


def _assert_refused(text: str, schema: str, invocation: Path | None = None) -> None:
    """The one shape of every refusal: an older Booley, the schema, the directory, nothing corrupt."""
    assert "was written by an older Booley and is no longer supported" in text
    assert schema in text
    if invocation is not None:
        assert (
            f"delete the Simulation invocation directory {invocation} and start a fresh run"
            in text
        )
    for corrupt in (
        "integrity failure",
        "is invalid",
        "corrupt",
        "cannot be read",
        "unreadable",
        "missing",
        "Traceback",
    ):
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


def test_inspection_refuses_a_0_3_0_manifest_naming_its_invocation(tmp_path) -> None:
    reports, _project_data, store = _retained_invocation(tmp_path)
    _as_030_invocation(store, "v1")

    with pytest.raises(ValueError) as excinfo:
        inspect_retained_campaign(store.manifest_path)

    _assert_unsupported_type(excinfo.value)
    _assert_refused(str(excinfo.value), _MANIFEST + "v1", reports / "sim/1")


def test_coverage_analysis_refuses_a_0_3_0_projection_naming_its_invocation(
    tmp_path, monkeypatch
) -> None:
    from booley.flows.sim.coverage_analysis_input import (
        CoverageAnalysisError,
        read_coverage_campaign,
    )

    run = coverage_invocation(tmp_path, monkeypatch)
    reports, target = run.reports, run.target
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


def test_full_prune_cli_refuses_an_unsupported_nested_coverage_campaign_naming_its_directory(
    tmp_path, monkeypatch
) -> None:
    """``--full`` reaches the nested Coverage Campaign before any Simulation Campaign check."""
    from tests.flows.sim.coverage_campaign_test_support import rewrite_manifest_as_v4

    run = coverage_invocation(tmp_path, monkeypatch)
    nested = json.loads(run.campaign_path.read_bytes())
    rewrite_manifest_as_v4(nested)
    raw = canonical_json_bytes(nested)
    _rewrite(run.campaign_path, raw)
    reference = json.loads(run.public.read_bytes())
    reference["coverage_campaign"].update(bytes=len(raw), sha256=content_digest(raw))
    _rewrite(run.public, canonical_json_bytes(reference))

    result = _run_retention_cli("--reports-root", str(run.reports), "--invocation", "1", "--full")

    assert result.returncode == 2
    assert "COV_SCHEMA_VERSION_UNSUPPORTED" in result.stderr
    assert str(run.storage) in result.stderr
    assert "Traceback" not in result.stderr
    assert run.campaign_path.is_file()


@pytest.mark.parametrize("kind", ["interrupted", "coverage"])
def test_resume_refuses_a_0_3_0_record_naming_its_invocation(
    kind: str, tmp_path: Path, reports: Path
) -> None:
    invocation = copy_0_3_0_invocation(kind, reports)

    result = SimulateFlow().execute(
        SimRequest(
            resume_from=_manifest_of(invocation, kind), work_dir=tmp_path, report_dir=reports
        )
    )

    assert result.exit_code == 2
    _assert_refused(result.outcome.report_text, _MANIFEST + "v1", invocation)


@_REFUSED_AT_RECORD
def test_full_prune_cli_refuses_a_0_3_0_record_naming_its_invocation(
    kind: str, schema: str, reports: Path
) -> None:
    invocation = copy_0_3_0_invocation(kind, reports)

    result = _run_retention_cli("--reports-root", str(reports), "--invocation", "1", "--full")

    assert result.returncode == 2
    _assert_refused(result.stderr, schema, invocation)
    assert invocation.is_dir()


def test_pruning_a_0_3_0_coverage_record_refuses_as_a_retention_error(reports: Path) -> None:
    """The Coverage Campaign reference is read first: it must not read as an invalid contract."""
    invocation = copy_0_3_0_invocation("coverage", reports)

    with pytest.raises(CampaignRetentionError) as excinfo:
        prune_invocation(reports, 1)

    _assert_refused(str(excinfo.value), "booley.coverage-campaign/v4", invocation)
    assert invocation.is_dir()


def test_native_prune_cli_refuses_a_0_3_0_coverage_record_at_its_manifest(reports: Path) -> None:
    """``--native-target`` inspects the Simulation Campaign first: its manifest is the marker."""
    invocation = copy_0_3_0_invocation("coverage", reports)

    result = _run_retention_cli(
        "--reports-root", str(reports), "--invocation", "1", "--native-target", "sim_0"
    )

    assert result.returncode == 2
    _assert_refused(result.stderr, _MANIFEST + "v1", invocation)
    assert invocation.is_dir()


@_REFUSED_AT_RECORD
def test_dependents_pruning_refuses_a_0_3_0_record_naming_its_invocation(
    kind: str, schema: str, reports: Path
) -> None:
    """``include_dependents`` runs the resource preflight over every Target first."""
    invocation = copy_0_3_0_invocation(kind, reports)

    with pytest.raises(CampaignRetentionError) as excinfo:
        prune_invocation(reports, 1, include_dependents=True)

    _assert_refused(str(excinfo.value), schema, invocation)
    assert invocation.is_dir()


def test_pruning_an_interrupted_0_3_0_record_refuses_before_recovery(reports: Path) -> None:
    """Its attempt has no result, so recovery would run: it must refuse and touch nothing."""
    invocation = copy_0_3_0_invocation("interrupted", reports)
    attempts = list(invocation.glob("targets/sim_0/campaign/work-items/*/attempts/*"))
    assert len(attempts) == 1

    with pytest.raises(CampaignRetentionError) as excinfo:
        prune_invocation(reports, 1)

    _assert_refused(str(excinfo.value), _MANIFEST + "v1", invocation)
    assert attempts[0].is_dir()


def test_coverage_analysis_refuses_a_0_3_0_coverage_record_naming_its_invocation(
    reports: Path,
) -> None:
    from booley.flows.sim.coverage_analysis_input import (
        CoverageAnalysisError,
        read_coverage_campaign,
    )

    invocation = copy_0_3_0_invocation("coverage", reports)

    with pytest.raises(CoverageAnalysisError) as excinfo:
        read_coverage_campaign(invocation / "targets" / "sim_0" / "coverage.json")

    _assert_refused(str(excinfo.value), "booley.coverage-campaign/v4", invocation)
    assert "fully pruned" not in str(excinfo.value)


def test_the_refusal_names_an_older_booley_and_what_to_do(reports: Path) -> None:
    invocation = copy_0_3_0_invocation("complete", reports)

    with pytest.raises(ValueError) as excinfo:
        CampaignStore(invocation / "targets" / "sim" / "campaign").load_manifest()

    _assert_unsupported_type(excinfo.value)
    assert str(excinfo.value) == (
        f"Simulation Campaign manifest {_MANIFEST}v1 was written by an older Booley and is "
        f"no longer supported; delete the Simulation invocation directory {invocation} "
        "and start a fresh run"
    )


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

    monkeypatch.setattr("booley.flows.sim.flow_resume.OrdinaryHdlSerialExecutor.execute", resumed)

    with pytest.raises(KeyboardInterrupt, match="resume reached execution"):
        SimulateFlow().execute(
            SimRequest(resume_from=manifest, work_dir=tmp_path, report_dir=reports)
        )
