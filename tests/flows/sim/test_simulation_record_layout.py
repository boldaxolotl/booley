"""Simulation runs write the Record layout, and the layout Booley 0.4.0 used is refused.

``tests/flows/fixtures/booley_0_4_0`` holds three invocation directories (``complete``,
``interrupted`` and ``coverage``) that Booley 0.4.0's own ``SimulateFlow`` wrote over the
tag's fake simulator. They are captured data, not test cases: each text file embeds its
capture's temporary root, replaced by ``/booley-0.4.0-capture``; the lock files, run logs
and simulator executables were dropped. The old ``campaign/`` and ``coverage-campaign/``
directory names refuse it before any record is read; the record schemas it holds are
retired too (``test_simulation_record_names.py``). Content changed after 0.4.0 without a
schema bump (for example the ``simulation.json`` ``tests[]`` rows gained ``log`` and
``trace``, and ``CHANGELOG.md`` Unreleased), which the schema-retiring parts of ADR 0090
(#3687-#3688) must allow for: the layout check refuses before it reads any content.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from booley.flows.sim.coverage_analysis_input import read_coverage_record
from booley.flows.sim.coverage_json import content_digest
from booley.flows.sim.coverage_reference import (
    CoverageRecordReferenceError,
    resolve_persisted_coverage_record_reference,
)
from booley.flows.sim.flow import SimulateFlow
from booley.flows.sim.record import collect_pre_sim_firings, read_durable_results
from booley.flows.sim.record.codec import (
    SimulationRecordIntegrityError,
)
from booley.flows.sim.record.resume import validate_resume_manifest
from booley.flows.sim.request import SimRequest
from tests.flows.sim.test_simulation_record_names import _OLD_MANIFEST, _at_record_layout
from tests.flows.sim.test_simulation_record_resume_diagnostics import (
    _interrupted_simulation_record,
)
from tests.flows.sim.test_simulation_record_unsupported_formats import (
    _assert_refused,
    _assert_unsupported_type,
    copy_older_invocation,
)
from tests.flows.sim.test_simulation_record_unsupported_formats import (
    reports as _reports_fixture,
)

# The report-root fixture of the Part 1 tests, collected here under the same name.
reports = _reports_fixture

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


# A new run writes the Record layout: see ``test_simulation_record_documents.py``.


def test_resume_from_a_record_with_an_executable_snapshot_is_refused_before_any_work(
    tmp_path: Path, monkeypatch
) -> None:
    reports_root, manifest, _executions = _interrupted_simulation_record(tmp_path, monkeypatch)
    (attempt, *_rest) = manifest.parent.glob("work-items/*/attempts/*")
    snapshot = attempt / "snapshot" / "snapshot.json"
    snapshot.parent.mkdir()
    snapshot.write_bytes(
        (Path(__file__).parents[1] / "fixtures/simulation_record/snapshot.json").read_bytes()
    )
    original = reports_root / "sim" / "1"
    before = _tree(original)

    def reached(_executor, _request):
        raise AssertionError("resume reached execution")

    monkeypatch.setattr(
        "booley.flows.sim.record.serial_execution.OrdinaryHdlSerialExecutor.execute", reached
    )

    result = SimulateFlow().execute(
        SimRequest(resume_from=manifest, work_dir=tmp_path, report_dir=reports_root)
    )

    assert result.exit_code == 2
    assert (
        "Executable snapshot booley.executable-snapshot/v2 was written by an older Booley"
        in result.outcome.report_text
    )
    # The refusal reads the record only: it neither changes it nor starts a new one.
    assert _tree(original) == before
    assert not list(reports_root.glob("sim/2/**/manifest.json"))


# --- AC2: --resume-from an older layout is refused before any work ----------------------


@pytest.mark.parametrize(
    ("version", "kind", "relayout"),
    [
        ("0.4.0", "interrupted", False),
        ("0.4.0", "coverage", False),
        ("0.3.0", "interrupted", False),
        # Renamed to the Record layout, so only the manifest schema refuses it.
        ("0.4.0", "interrupted", True),
        ("0.4.0", "coverage", True),
    ],
)
def test_resume_from_an_older_layout_manifest_is_refused_before_any_work(
    tmp_path: Path, reports: Path, version: str, kind: str, relayout: bool
) -> None:
    invocation = copy_older_invocation(version, kind, reports)
    refused_name = _marker(kind)
    manifest = _manifest_of(invocation, kind)
    if relayout:
        _at_record_layout(invocation)
        refused_name = _OLD_MANIFEST
        manifest = invocation / "targets" / _TARGET[kind] / "record" / "manifest.json"
    before = _tree(invocation)

    result = SimulateFlow().execute(
        SimRequest(resume_from=manifest, work_dir=tmp_path, report_dir=reports)
    )

    assert result.exit_code == 2
    _assert_refused(result.outcome.report_text, refused_name, invocation)
    assert not (reports / "sim" / "2").exists()
    assert _tree(invocation) == before


# --- AC3: Coverage analysis and waiver binding ---------------------------------------------


@pytest.mark.parametrize("entry", ["analysis", "persisted_reference"])
def test_coverage_entry_points_refuse_an_older_layout(reports: Path, entry: str) -> None:
    invocation = copy_older_invocation("0.4.0", "coverage", reports)
    coverage_json = invocation / "targets" / "sim_0" / "coverage.json"

    with pytest.raises(ValueError) as excinfo:
        if entry == "analysis":
            read_coverage_record(coverage_json)
        else:
            # Waiver binding reads a persisted pointer to the public reference.
            raw = coverage_json.read_bytes()
            resolve_persisted_coverage_record_reference(
                reports,
                {
                    "path_base": "reports_root",
                    "path": "sim/1/targets/sim_0/coverage.json",
                    "bytes": len(raw),
                    "sha256": content_digest(raw),
                    "nested_campaign_sha256": "sha256:" + "0" * 64,
                },
            )

    # The 0.4.0 reference is refused by its schema before its nested path is read.
    _assert_refused(
        str(excinfo.value),
        "Coverage Campaign reference booley.coverage-campaign-reference/v1",
        invocation,
    )
    assert "fully pruned" not in str(excinfo.value)
    # Neither entry point calls the refusal a corrupt reference.
    assert not isinstance(excinfo.value, CoverageRecordReferenceError)
    if entry == "persisted_reference":
        _assert_unsupported_type(excinfo.value)


# --- AC4: the old names are not aliases --------------------------------------------------


def test_the_old_directory_names_are_not_aliases(reports: Path) -> None:
    older = copy_older_invocation("0.4.0", "complete", reports)
    older_manifest = _manifest_of(older, "complete")

    # Readers of a manifest path refuse the old name as an older Booley's record.
    for read in (
        lambda: read_durable_results(older_manifest, "sha256:" + "0" * 64),
        lambda: collect_pre_sim_firings(older_manifest),
    ):
        with pytest.raises(ValueError) as refused:
            read()
        _assert_unsupported_type(refused.value)
        _assert_refused(str(refused.value), _marker("complete"), older)


@pytest.mark.parametrize(
    "typed",
    [
        # A new run's invocation is never called an older record, whatever path is typed.
        "sim/1/targets/sim/campaign/manifest.json",
        # A Mutation manifest keeps its own ``campaign/`` name.
        "mutation/1/campaign/manifest.json",
    ],
)
def test_a_manifest_path_naming_no_older_record_is_not_called_one(
    tmp_path: Path, reports: Path, typed: str
) -> None:
    path = reports / typed
    if typed.startswith("sim/"):
        (path.parent.parent / "record").mkdir(parents=True)

    with pytest.raises(SimulationRecordIntegrityError) as excinfo:
        validate_resume_manifest(path, project_root=tmp_path)

    assert "must name one exact record/manifest.json" in str(excinfo.value)
    assert "older Booley" not in str(excinfo.value)
