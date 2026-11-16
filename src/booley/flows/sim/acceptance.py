"""Publish each Simulation Record outcome's ``simulation.json`` projection."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from booley.flows.execution_persistence import AcceptanceRecordingError
from booley.flows.sim.coverage_json import content_digest
from booley.flows.sim.coverage_reference import (
    MAX_REFERENCE_BYTES,
    ResolvedCoverageRecord,
    authenticate_coverage_record_owner,
    encode_coverage_record_reference,
    resolve_coverage_record_reference,
)
from booley.flows.sim.record import (
    SIMULATION_PROJECTION_SCHEMA,
    SimulationRecordOutcome,
    build_artifact_reference,
    encode_artifact_reference,
)
from booley.flows.sim.record.codec import MANIFEST_MAX_BYTES, SimulationRecordIntegrityError
from booley.flows.sim.record.test_run_files import simulation_record_test_run_files
from booley.flows.sim.record.test_run_report import evidence_test_run_rows
from booley.flows.sim.record_reports import (
    simulation_booley_dir,
    write_compatibility_projection,
)


@dataclass(frozen=True, slots=True)
class _PublicCoverage:
    """One outcome's public ``coverage.json``: authenticated, with its canonical bytes' digest."""

    path: Path
    resolved: ResolvedCoverageRecord
    size: int
    sha256: str


def publish_simulation_record_projections(
    work_dir: Path | None, outcomes: tuple[object, ...]
) -> None:
    """Write each outcome's ``simulation.json`` projection next to its Target's record."""
    # A symlinked Booley Directory entry is followed when reading published artifacts back.
    booley_dir = None if work_dir is None else simulation_booley_dir(Path(work_dir))
    for item in outcomes:
        if not isinstance(item, SimulationRecordOutcome):
            raise TypeError("invalid Simulation Record outcome")
        projection = _simulation_record_projection(item, booley_dir=booley_dir)
        origin = item.manifest_path.parents[1] / "simulation.json"
        write_compatibility_projection(origin, projection, acceptance_committed=True)


def _observation_passed(observation: Mapping[str, object]) -> bool:
    return (
        observation["execution"] == "completed"
        and observation["functional"] == "pass"
        and observation["assertions"] != "dirty"
    )


def _simulation_record_projection(
    outcome: SimulationRecordOutcome, *, booley_dir: Path | None = None
) -> dict[str, object]:
    facts = outcome.acceptance_facts.document
    simulation_record_id = str(facts["campaign_id"])
    target_directory = outcome.manifest_path.parents[1]
    test_runs = [
        {
            key: value
            for key, value in row.items()
            if key not in {"functional_failure", "failure_execution"}
        }
        for row in evidence_test_run_rows(outcome)
    ]
    projection = {
        "$schema": SIMULATION_PROJECTION_SCHEMA,
        "flow": "sim",
        "mode": "simulate",
        "target": outcome.target["selector"],
        "target_identity": f"{outcome.target['vlnv']}#{outcome.target['name']}",
        "passed": outcome.aggregate_grade == "pass",
        "inconclusive": outcome.aggregate_grade == "inconclusive",
        "tests": _simulation_record_test_projections(outcome, test_runs, target_directory),
        "test_runs": test_runs,
        "simulation_record_id": simulation_record_id,
        "simulation_record_manifest": _artifact_document(
            outcome.manifest_path,
            base=target_directory,
            kind="simulation_record_manifest",
            owner=simulation_record_id,
            maximum=MANIFEST_MAX_BYTES,
            booley_dir=booley_dir,
        ),
    }
    if outcome.coverage_reference is not None:
        projection.update(
            _coverage_projection_fields(
                outcome, target_directory, simulation_record_id, booley_dir=booley_dir
            )
        )
    return projection


def _simulation_record_test_projections(
    outcome: SimulationRecordOutcome, test_runs: list[dict[str, object]], target_directory: Path
) -> list[dict[str, object]]:
    """One row per observation, with the Test Run it observed.

    *test_runs* is aligned with the observations. Each row names the Test
    Run's ``log`` and ``trace`` files relative to *target_directory* (the
    directory of ``simulation.json``), ``None`` where a file does not exist.
    They stay out of the ``test_runs`` rows, which rerun matching reads.
    """
    files = simulation_record_test_run_files(outcome)
    return [
        {
            "name": observation["test"] or "default",
            **_test_run_fields(run),
            "passed": _observation_passed(observation),
            "cycles": observation["cycle_count"],
            "sva_errors": observation["assertion_count"],
            "timed_out": observation["execution"] == "timeout",
            "execution": observation["execution"],
            "failure_kind": observation["failure_class"] or "",
            "error_tail": _observation_error_tail(observation),
            **found.relative_to(target_directory),
        }
        for observation, run, found in zip(outcome.observations, test_runs, files, strict=True)
    ]


def _test_run_fields(run: Mapping[str, object]) -> dict[str, object]:
    """What a per-Test row says about its Test Run (the coverage rows' keys)."""
    return {
        "test_run": run["name"],
        "variant": run["variant"],
        "seed": run["seed"],
        "fingerprint": run["fingerprint"],
    }


def _observation_error_tail(observation: Mapping[str, object]) -> str:
    detail = observation["detail"]
    if not isinstance(detail, Mapping) or not isinstance(detail.get("reason"), str):
        raise SimulationRecordIntegrityError(
            "Simulation Record observation detail must contain a string reason"
        )
    if _observation_passed(observation):
        return ""
    return detail["reason"]


def _coverage_projection_fields(
    outcome: SimulationRecordOutcome,
    target_directory: Path,
    simulation_record_id: str,
    *,
    booley_dir: Path | None = None,
) -> dict[str, object]:
    public = _resolve_facts_coverage(outcome)
    coverage = public.resolved.loaded.coverage_record
    return {
        "coverage_record": _artifact_document(
            public.path,
            base=target_directory,
            kind="coverage_record_reference",
            owner=simulation_record_id,
            maximum=MAX_REFERENCE_BYTES,
            booley_dir=booley_dir,
        ),
        "collection": coverage.collection["status"],
        "evaluation": coverage.evaluation["status"],
    }


def _artifact_document(
    path: Path,
    *,
    base: Path,
    kind: str,
    owner: str,
    maximum: int,
    booley_dir: Path | None,
) -> dict[str, object]:
    reference = build_artifact_reference(
        path,
        base_name="origin_target",
        base=base,
        kind=kind,
        owner=owner,
        maximum=maximum,
        booley_dir=booley_dir,
    )
    return encode_artifact_reference(reference)


def _resolve_facts_coverage(outcome: SimulationRecordOutcome) -> _PublicCoverage:
    """Resolve the record's public ``coverage.json`` against its Acceptance Facts."""
    public_path = outcome.manifest_path.parents[1] / "coverage.json"
    facts = outcome.acceptance_facts.document
    bound = facts["coverage_reference"]
    if not isinstance(bound, Mapping) or not isinstance(outcome.coverage_reference, Mapping):
        raise AcceptanceRecordingError("coverage acceptance facts omit their public reference")
    document = bound["document"]
    evidence = bound["reference"]
    if not isinstance(document, Mapping) or not isinstance(evidence, Mapping):
        raise AcceptanceRecordingError("coverage acceptance reference is malformed")
    resolved = resolve_coverage_record_reference(public_path)
    raw = encode_coverage_record_reference(resolved.reference)
    digest = content_digest(raw)
    invocation = outcome.manifest_path.parents[3]
    expected_path = public_path.relative_to(invocation).as_posix()
    if (
        resolved.reference.document != document
        or outcome.coverage_reference != document
        or evidence["path"] != expected_path
        or evidence["bytes"] != len(raw)
        or evidence["sha256"] != digest
        or evidence["owner"] != facts["campaign_id"]
    ):
        raise AcceptanceRecordingError(
            "public Coverage Record reference disagrees with Acceptance Facts"
        )
    authenticate_coverage_record_owner(resolved)
    return _PublicCoverage(public_path, resolved, len(raw), digest)


__all__ = ["publish_simulation_record_projections"]
