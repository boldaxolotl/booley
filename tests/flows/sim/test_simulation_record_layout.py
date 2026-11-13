"""Simulation runs write the Record layout, and the layout Booley 0.4.0 used is refused.

``tests/flows/fixtures/booley_0_4_0`` holds three invocation directories (``complete``,
``interrupted`` and ``coverage``) that Booley 0.4.0's own ``SimulateFlow`` wrote over the
tag's fake simulator. They are captured data, not test cases: each text file embeds its
capture's temporary root, replaced by ``/booley-0.4.0-capture``; the lock files, run logs
and simulator executables were dropped. Its record schemas are the current ones, so the
old ``campaign/`` and ``coverage-campaign/`` directory names are the only thing that
refuses it. Content changed after 0.4.0 without a schema bump (for example the
``simulation.json`` ``tests[]`` rows gained ``log`` and ``trace``, and ``CHANGELOG.md``
Unreleased), which the schema-retiring parts of ADR 0090 (#3686-#3688) must allow for:
this part refuses on the layout before it reads any content.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from booley.flows.sim.campaign import collect_pre_sim_firings, read_durable_results
from booley.flows.sim.campaign.codec import (
    SimulationCampaignIntegrityError,
    linked_manifest_path,
)
from booley.flows.sim.campaign.dependency import read_campaign_dependencies
from booley.flows.sim.campaign.resume import validate_resume_manifest
from booley.flows.sim.campaign_reports import write_campaign_json
from booley.flows.sim.campaign_retention import CampaignRetentionError, prune_invocation
from booley.flows.sim.coverage_json import content_digest
from booley.flows.sim.coverage_reference import (
    CoverageCampaignReferenceError,
    resolve_persisted_coverage_campaign_reference,
)
from booley.flows.sim.flow import SimulateFlow
from booley.flows.sim.request import SimRequest
from booley.goals.entry import EntryEnvironment
from booley.goals.finish import finish_goal
from booley.goals.lifecycle import LifecycleError
from booley.mcp.goal_completion import completion_environment
from tests.flows.sim.campaign_test_support import coverage_invocation
from tests.flows.sim.test_campaign_resume_diagnostics import _interrupted_campaign
from tests.flows.sim.test_campaign_retention import _run_retention_cli
from tests.flows.sim.test_campaign_unsupported_formats import (
    _assert_refused,
    _assert_unsupported_type,
    copy_older_invocation,
)
from tests.flows.sim.test_campaign_unsupported_formats import (
    reports as _reports_fixture,
)
from tests.goals.conftest import layout as _layout_fixture
from tests.goals.test_finish import request
from tests.goals.test_generated_inputs import classified

# The report-root fixture of the Part 1 tests, collected here under the same name.
reports = _reports_fixture
# The Goal worktree fixture of tests/goals, which the broad suite does not collect.
layout = _layout_fixture

# The Target of each captured run: ``complete`` ran ``sim``, the others ``sim_0``.
_TARGET = {"complete": "sim", "interrupted": "sim_0", "coverage": "sim_0"}


def _marker(kind: str) -> str:
    """The directory a refusal names: the old Record directory of the run's one Target."""
    return f"targets/{_TARGET[kind]}/campaign"


def _manifest_of(invocation: Path, kind: str) -> Path:
    return invocation / "targets" / _TARGET[kind] / "campaign" / "manifest.json"


def _tree(root: Path) -> dict[str, bytes]:
    """Every file under *root* by relative path, to show a refusal changed nothing."""
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


# --- AC1: a new run writes the Record layout -------------------------------------------


def test_a_coverage_run_writes_the_record_layout(tmp_path: Path, monkeypatch) -> None:
    run = coverage_invocation(tmp_path, monkeypatch)
    target = run.target

    assert (target / "record" / "manifest.json").is_file()
    assert not (target / "campaign").exists()
    coverage = list(target.glob("record/work-items/*/attempts/*/coverage-record/coverage.json"))
    assert len(coverage) == 1
    assert not list(target.rglob("coverage-campaign"))
    reference = json.loads((target / "coverage.json").read_bytes())
    nested = reference["coverage_campaign"]["path"]
    assert nested.startswith("record/work-items/")
    assert nested.endswith("/coverage-record/coverage.json")
    projection = json.loads((target / "simulation.json").read_bytes())
    assert projection["campaign_manifest"]["path"] == "record/manifest.json"


def test_resume_from_accepts_a_record_manifest(tmp_path: Path, monkeypatch) -> None:
    reports_root, manifest, _executions = _interrupted_campaign(tmp_path, monkeypatch)
    assert manifest.parts[-2:] == ("record", "manifest.json")

    def resumed(_executor, _request):
        raise KeyboardInterrupt("resume reached execution")

    monkeypatch.setattr("booley.flows.sim.flow_resume.OrdinaryHdlSerialExecutor.execute", resumed)

    with pytest.raises(KeyboardInterrupt, match="resume reached execution"):
        SimulateFlow().execute(
            SimRequest(resume_from=manifest, work_dir=tmp_path, report_dir=reports_root)
        )


# --- AC2: --resume-from an older layout is refused before any work ----------------------


_RESUMED = (("0.4.0", "interrupted"), ("0.4.0", "coverage"), ("0.3.0", "interrupted"))


def test_resume_from_an_older_layout_manifest_is_refused_before_any_work(
    tmp_path: Path, reports: Path
) -> None:
    for version, kind in _RESUMED:
        shutil.rmtree(reports / "sim", ignore_errors=True)
        invocation = copy_older_invocation(version, kind, reports)
        before = _tree(invocation)

        result = SimulateFlow().execute(
            SimRequest(
                resume_from=_manifest_of(invocation, kind), work_dir=tmp_path, report_dir=reports
            )
        )

        assert result.exit_code == 2, (version, kind)
        _assert_refused(result.outcome.report_text, _marker(kind), invocation)
        assert not (reports / "sim" / "2").exists()
        assert _tree(invocation) == before


def test_a_campaign_path_naming_no_directory_is_not_called_an_older_record(
    tmp_path: Path, reports: Path
) -> None:
    """A new run's invocation is never described as an older record, whatever path is typed."""
    invocation = reports / "sim" / "1"
    (invocation / "targets" / "sim" / "record").mkdir(parents=True)
    typed = invocation / "targets" / "sim" / "campaign" / "manifest.json"

    with pytest.raises(SimulationCampaignIntegrityError) as excinfo:
        validate_resume_manifest(typed, project_root=tmp_path)

    assert "older Booley" not in str(excinfo.value)


# --- AC3: retention and pruning ---------------------------------------------------------


def test_full_prune_cli_refuses_an_older_layout(reports: Path) -> None:
    for kind in ("complete", "interrupted", "coverage"):
        shutil.rmtree(reports / "sim", ignore_errors=True)
        invocation = copy_older_invocation("0.4.0", kind, reports)

        result = _run_retention_cli("--reports-root", str(reports), "--invocation", "1", "--full")

        assert result.returncode == 2, kind
        _assert_refused(result.stderr, _marker(kind), invocation)
        assert invocation.is_dir()


def test_native_prune_cli_refuses_an_older_layout(reports: Path) -> None:
    invocation = copy_older_invocation("0.4.0", "coverage", reports)

    result = _run_retention_cli(
        "--reports-root", str(reports), "--invocation", "1", "--native-target", "sim_0"
    )

    assert result.returncode == 2
    _assert_refused(result.stderr, _marker("coverage"), invocation)
    assert invocation.is_dir()


def test_dependents_prune_refuses_an_older_layout(reports: Path) -> None:
    """An interrupted dependents batch is retried from its journal: that refuses too."""
    for batch in ("fresh", "batch-retry"):
        shutil.rmtree(reports / "sim", ignore_errors=True)
        invocation = copy_older_invocation("0.4.0", "complete", reports)
        if batch == "batch-retry":
            write_campaign_json(
                invocation / ".prune-batch.json",
                {
                    "$schema": "booley.simulation-prune-batch/v1",
                    "operation": "full-with-dependents",
                    "origin": str(invocation.absolute()),
                    "members": [str(invocation.absolute())],
                },
            )
        before = _tree(invocation)

        with pytest.raises(CampaignRetentionError) as excinfo:
            prune_invocation(reports, 1, include_dependents=True)

        _assert_refused(str(excinfo.value), _marker("complete"), invocation)
        assert _tree(invocation) == before, batch


# --- AC3: the Pre-Sim scan, Coverage analysis and Goal waiver binding --------------------


def test_pre_sim_scan_refuses_an_older_layout_manifest(reports: Path) -> None:
    invocation = copy_older_invocation("0.4.0", "interrupted", reports)

    with pytest.raises(ValueError) as excinfo:
        collect_pre_sim_firings(_manifest_of(invocation, "interrupted"))

    _assert_unsupported_type(excinfo.value)
    _assert_refused(str(excinfo.value), _marker("interrupted"), invocation)


def test_coverage_analysis_refuses_an_older_layout(reports: Path) -> None:
    from booley.flows.sim.coverage_analysis_input import (
        CoverageAnalysisError,
        read_coverage_campaign,
    )

    invocation = copy_older_invocation("0.4.0", "coverage", reports)

    with pytest.raises(CoverageAnalysisError) as excinfo:
        read_coverage_campaign(invocation / "targets" / "sim_0" / "coverage.json")

    # The reference names the nested path; the refusal still tells the user what to delete.
    _assert_refused(str(excinfo.value), "Coverage Campaign path campaign/", invocation)
    assert "fully pruned" not in str(excinfo.value)


def test_a_persisted_coverage_reference_into_an_older_layout_is_refused(reports: Path) -> None:
    """Goal waiver binding reads a persisted pointer to the public reference."""
    invocation = copy_older_invocation("0.4.0", "coverage", reports)
    raw = (invocation / "targets" / "sim_0" / "coverage.json").read_bytes()
    persisted = {
        "path_base": "reports_root",
        "path": "sim/1/targets/sim_0/coverage.json",
        "bytes": len(raw),
        "sha256": content_digest(raw),
        "nested_campaign_sha256": "sha256:" + "0" * 64,
    }

    with pytest.raises(ValueError) as excinfo:
        resolve_persisted_coverage_campaign_reference(reports, persisted)

    assert not isinstance(excinfo.value, CoverageCampaignReferenceError)
    _assert_unsupported_type(excinfo.value)
    _assert_refused(str(excinfo.value), "Coverage Campaign path campaign/", invocation)


# --- AC4: the old names are not aliases --------------------------------------------------


def test_the_old_directory_names_are_not_aliases(reports: Path, tmp_path: Path) -> None:
    older = copy_older_invocation("0.4.0", "complete", reports)
    older_manifest = _manifest_of(older, "complete")

    # A prerequisite link names the Record directory exactly.
    with pytest.raises(SimulationCampaignIntegrityError) as linked:
        linked_manifest_path(older, {"path": "targets/t/campaign/manifest.json"})
    assert "is not targets/<Target>/record/manifest.json" in str(linked.value)

    # A dependency origin is an exact Record manifest.
    with pytest.raises(ValueError, match="exact Simulation Campaign manifest"):
        read_campaign_dependencies(older_manifest)

    # Readers of a manifest path refuse the old name as an older Booley's record.
    for read in (
        lambda: read_durable_results(older_manifest, "sha256:" + "0" * 64),
        lambda: collect_pre_sim_firings(older_manifest),
    ):
        with pytest.raises(ValueError) as refused:
            read()
        _assert_unsupported_type(refused.value)
        _assert_refused(str(refused.value), _marker("complete"), older)

    # A Mutation manifest keeps its own ``campaign/`` name and is not an older Simulation one.
    mutation = tmp_path / "flow-reports" / "mutation" / "1" / "campaign" / "manifest.json"
    with pytest.raises(SimulationCampaignIntegrityError) as foreign:
        validate_resume_manifest(mutation, project_root=tmp_path)
    assert "must name one exact record/manifest.json" in str(foreign.value)
    assert "older Booley" not in str(foreign.value)


@pytest.mark.timeout(120)
def test_finish_refuses_an_older_layout_producer_manifest(layout) -> None:
    """A Goal's 0.4.0 producer decodes as current: only its ``campaign/`` directory marks it old."""
    fixture = Path(__file__).parents[1] / "fixtures" / "booley_0_4_0" / "complete" / "sim" / "1"
    classified(layout, copyto="dhrystone/dhry.hex", older_layout_invocation=fixture)
    with pytest.raises(LifecycleError) as excinfo:
        finish_goal(request(layout), completion_environment(EntryEnvironment(layout.control)))
    message = str(excinfo.value).replace("\\", "/")
    assert "was written by an older Booley and is no longer supported" in message
    assert "targets/sim/campaign" in message
    assert "/flow-reports/sim/7 and start a fresh run" in message
