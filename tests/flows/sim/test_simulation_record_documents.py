"""The Simulation summary, cocotb transport and projection name the Simulation Record.

Part 4 of ADR 0090. The 0.4.0 documents are read from committed bytes that Booley
0.4.0's own flow wrote: ``tests/flows/fixtures/booley_0_4_0/{complete,coverage}`` hold a
summary and the Simulation projections, ``resumed/sim/2`` the report-only invocation a
``--resume-from`` leaves behind, and ``cocotb/cocotb-transport.json`` the cocotb transport.
The last two were captured from the ``v0.4.0`` tag with its own tests: the tag's
``tests/flows/sim/test_endpoint_record_lifecycle.py::test_ordinary_cli_manifest_and_card_paths``
ran a ``cocotb_batch`` Simulation Record and its ``*/evidence/*cocotb_results.json`` was copied byte
for byte; the tag's ``tests/flows/sim/test_simulation_record_resume_diagnostics.py::_interrupted_campaign``
interrupted a run and ``SimulateFlow().execute(SimRequest(resume_from=<manifest>))`` wrote
``sim/2``, whose ``report.json`` and ``progress.json`` are committed with the capture's
temporary root replaced by ``/booley-0.4.0-capture``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from booley.flows.sim.coverage_analysis_input import CoverageAnalysisError, read_coverage_record
from booley.flows.sim.execution.contract import SimulationTestOutcome
from booley.flows.sim.record.codec import canonical_json_bytes, raise_if_retired
from booley.flows.sim.record.projection import decode_simulation_projection
from tests.flows.sim.simulation_record_test_support import coverage_invocation
from tests.flows.sim.test_simulation_record_cocotb_coverage_items import _run_cocotb
from tests.flows.sim.test_simulation_record_unsupported_formats import (
    _assert_refused,
    _assert_unsupported_type,
)
from tests.flows.sim.test_simulation_record_unsupported_formats import reports as _reports_fixture

# The report-root fixture of the Part 1 tests, collected here under the same name.
reports = _reports_fixture

_FIXTURES = Path(__file__).parents[1] / "fixtures" / "booley_0_4_0"
_OLD_PROJECTION = "booley.simulation-projection/v3"
_OLD_TRANSPORT = "booley.cocotb-campaign-transport/v1"
_OLD_KINDS = ("simulation_campaign_manifest", "coverage_campaign_reference")


def _json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_bytes())


def _write(path: Path, document: dict[str, Any]) -> None:
    if path.exists():
        path.chmod(0o600)
    path.write_bytes(canonical_json_bytes(document))


def _kinds(value: object) -> set[str]:
    """Every ``kind`` string found anywhere in a JSON document."""
    found: set[str] = set()
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "kind" and isinstance(item, str):
                found.add(item)
            found |= _kinds(item)
    elif isinstance(value, list):
        for item in value:
            found |= _kinds(item)
    return found


def _0_4_0_projection(kind: str) -> dict[str, Any]:
    (path,) = _FIXTURES.glob(f"{kind}/sim/1/targets/*/simulation.json")
    return _json(path)


# --- AC1: the names a run writes -----------------------------------------------------------


def test_a_coverage_run_writes_record_names_in_its_projection_and_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = coverage_invocation(tmp_path, monkeypatch)
    projection = _json(run.target / "simulation.json")
    record_id = _json(run.target / "record" / "manifest.json")["simulation_record_id"]

    assert projection["$schema"] == "booley.simulation-projection/v4"
    assert projection["simulation_record_id"] == record_id
    for key in ("campaign_id", "campaign_manifest", "coverage_campaign"):
        assert key not in projection
    assert projection["simulation_record_manifest"]["kind"] == "simulation_record_manifest"
    assert projection["coverage_record"]["kind"] == "coverage_record_reference"

    # The layout: ``record/`` and ``coverage-record/`` directories, never the 0.4.0 names.
    target = run.target
    assert (target / "record" / "manifest.json").is_file()
    assert not (target / "campaign").exists()
    coverage = list(target.glob("record/work-items/*/attempts/*/coverage-record/coverage.json"))
    assert len(coverage) == 1
    assert not list(target.rglob("coverage-campaign"))
    nested = _json(target / "coverage.json")["coverage_record"]["path"]
    assert nested.startswith("record/work-items/")
    assert nested.endswith("/coverage-record/coverage.json")
    assert projection["simulation_record_manifest"]["path"] == "record/manifest.json"

    # Every evidence kind of the invocation's report artifact references is a new one.
    invocation = run.reports / "sim" / "1"
    kinds: set[str] = set()
    for name in ("report.json", "progress.json"):
        kinds |= _kinds(_json(invocation / name))
    assert {"simulation_record_manifest", "coverage_record_reference"} <= kinds
    assert not kinds & set(_OLD_KINDS)


def test_a_cocotb_attempt_writes_the_record_transport(tmp_path: Path) -> None:
    reported = (
        SimulationTestOutcome(name="count", verdict="pass", passed=True),
        SimulationTestOutcome(name="reset", verdict="pass", passed=True),
    )
    store = _run_cocotb(tmp_path, ("count", "reset"), reported)

    schemas = {
        _json(path).get("$schema")
        for path in store.root.glob("work-items/*/attempts/*/evidence/*")
        if path.suffix == ".json"
    }
    assert "booley.cocotb-record-transport/v1" in schemas
    assert _OLD_TRANSPORT not in schemas


# --- AC2: every 0.4.0 document is refused as older ------------------------------------------


def test_a_0_4_0_projection_is_refused_naming_its_invocation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Coverage analysis reads the projection beside a Coverage reference.
    (tmp_path / "coverage").mkdir()
    run = coverage_invocation(tmp_path / "coverage", monkeypatch)
    _write(run.target / "simulation.json", _0_4_0_projection("coverage"))
    with pytest.raises(CoverageAnalysisError) as analysed:
        read_coverage_record(run.target / "coverage.json")
    _assert_refused(str(analysed.value), _OLD_PROJECTION, run.reports / "sim" / "1")


def test_a_0_4_0_cocotb_transport_is_refused_as_older() -> None:
    """Nothing decodes the transport after it is written, so the retired-schema registry is the contract."""
    document = _json(_FIXTURES / "cocotb" / "cocotb-transport.json")
    assert document["$schema"] == _OLD_TRANSPORT

    with pytest.raises(ValueError) as refused:
        raise_if_retired(document)

    _assert_unsupported_type(refused.value)
    _assert_refused(str(refused.value), _OLD_TRANSPORT)


# --- AC3: no old key or kind is an alias -------------------------------------------------------


def test_old_projection_keys_and_kinds_are_not_aliases(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = coverage_invocation(tmp_path, monkeypatch)
    path = run.target / "simulation.json"
    current = _json(path)
    refusals: list[str] = []

    # (a) The old identity key in place of the new one is not an exact projection.
    aliased = dict(current)
    aliased["campaign_id"] = aliased.pop("simulation_record_id")
    with pytest.raises(ValueError, match="fields are not exact") as exact:
        decode_simulation_projection(aliased, coverage_expected=True)
    refusals.append(str(exact.value))

    # (b), (c) An old evidence kind in a current reference is a mismatch, not accepted.
    for key, old_kind in (
        ("simulation_record_manifest", _OLD_KINDS[0]),
        ("coverage_record", _OLD_KINDS[1]),
    ):
        altered = json.loads(json.dumps(current))
        altered[key]["kind"] = old_kind
        _write(path, altered)
        with pytest.raises(CoverageAnalysisError) as analysed:
            read_coverage_record(run.target / "coverage.json")
        refusals.append(str(analysed.value))

    assert refusals
    assert all("older Booley" not in text for text in refusals)
