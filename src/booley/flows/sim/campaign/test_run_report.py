"""Per-Test-Run rows of one Simulation Campaign, for reports (ADR 0072).

A campaign's acceptance facts name the Test Run of every observation, and its
manifest records what each Test Run is: Test Variant, seed, declared
arguments and environment. This module joins the two into one row per Test
Run and renders the report lines, including the command that reruns a failed
Test Run with the same seed.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Protocol, cast

from booley.flows.test_runs import (
    TestRun,
    TestRunNameError,
    discovered_test_run,
    rerun_command,
    test_run_document,
    test_run_from_document,
)

from .codec import (
    SimulationCampaignIntegrityError,
    SimulationCampaignManifest,
    UnsupportedSimulationCampaignFormatError,
    decode_simulation_campaign_manifest,
)
from .planning import manifest_digest

__all__ = [
    "ROW_LIMIT",
    "TEST_RUN_GRADES",
    "campaign_test_run_rows",
    "evidence_test_run_rows",
    "informative_test_run_lines",
    "observation_test_run_rows",
    "test_run_grade",
    "test_run_report_lines",
    "test_run_row",
]

#: Most rows (and most rerun commands) one Target's report shows.
ROW_LIMIT = 32


class _Outcome(Protocol):
    @property
    def manifest_path(self) -> Path: ...

    @property
    def observations(self) -> tuple[Mapping[str, object], ...]: ...


#: How a Test Run ended, in report rows: the one vocabulary, with its wording.
TEST_RUN_GRADES = {
    "pass": "passed",
    "fail": "failed",
    "error": "error",
    "inconclusive": "inconclusive",
}


def test_run_grade(*, passed: bool, infrastructure: bool, inconclusive: bool) -> str:
    """One word for how a Test Run ended: the Simulation Campaign's grade table.

    A run that did not pass because of infrastructure is an ``error`` even
    when its verdict is also inconclusive. Campaign observations, direct
    results and coverage runs are all graded here.
    """
    if passed:
        return "pass"
    if infrastructure:
        return "error"
    return "inconclusive" if inconclusive else "fail"


def test_run_row(run: TestRun, grade: str) -> dict[str, object]:
    """One report row: the Test Run's record and how it ended."""
    return {**test_run_document(run), "grade": grade}


# Not pytest tests, although their names start with ``test_``.
test_run_grade.__test__ = False  # type: ignore[attr-defined]
test_run_row.__test__ = False  # type: ignore[attr-defined]


def _grade(observation: Mapping[str, object]) -> str:
    """Grade a campaign observation."""
    return test_run_grade(
        passed=(
            observation["execution"] == "completed"
            and observation["functional"] == "pass"
            and observation["assertions"] != "dirty"
        ),
        infrastructure=observation["failure_class"] == "infrastructure",
        inconclusive=observation["functional"] == "inconclusive",
    )


def _recorded_test_runs(manifest_path: Path) -> dict[tuple[object, str], TestRun]:
    """Every Test Run the manifest at *manifest_path* records; none if unreadable."""
    try:
        manifest = decode_simulation_campaign_manifest(manifest_path.read_bytes())
    except (OSError, SimulationCampaignIntegrityError, UnsupportedSimulationCampaignFormatError):
        return {}
    try:
        return _manifest_test_runs(manifest)
    except TestRunNameError:
        # Decode trusts stored fingerprints; a hand-edited one surfaces here.
        return {}


def _manifest_test_runs(manifest: SimulationCampaignManifest) -> dict[tuple[object, str], TestRun]:
    """Every Test Run *manifest* records, by work item and Test Run name."""
    recorded: dict[tuple[object, str], TestRun] = {}
    items = cast(tuple[Mapping[str, object], ...], manifest.document["work_items"])
    for item in items:
        for document in cast(tuple[object, ...], item.get("test_runs", ())):
            run = test_run_from_document(document)
            recorded[(item["work_item_id"], run.name)] = run
            if run.test is None:
                # Tests an unfiltered Cocotb batch discovers run under this seed.
                recorded[(item["work_item_id"], "")] = run
    return recorded


def _observed_run(
    observation: Mapping[str, object], recorded: Mapping[tuple[object, str], TestRun]
) -> TestRun | None:
    """The Test Run an observation names, as the manifest recorded it."""
    name = observation.get("test_run")
    if not isinstance(name, str):
        return None
    item = observation["work_item_id"]
    run = recorded.get((item, name))
    if run is not None:
        return run
    unnamed = recorded.get((item, ""))
    test = observation["test"]
    if unnamed is None or not isinstance(test, str):
        return None
    return discovered_test_run(test, unnamed)


def campaign_test_run_rows(outcome: _Outcome) -> list[dict[str, object]]:
    """One row per observed Test Run: what it is, and how it ended.

    Each row holds the recorded ``name``, ``test``, ``variant``, ``seed``,
    ``args``, ``env`` and ``fingerprint`` plus ``grade``. A campaign whose
    facts do not name Test Runs (written before they were recorded) has no
    rows.
    """
    recorded = _recorded_test_runs(outcome.manifest_path)
    rows: list[dict[str, object]] = []
    for observation in outcome.observations:
        run = _observed_run(observation, recorded)
        if run is not None:
            rows.append(test_run_row(run, _grade(observation)))
    return rows


class _EvidenceOutcome(_Outcome, Protocol):
    @property
    def acceptance_facts(self) -> _Facts: ...


class _Facts(Protocol):
    @property
    def document(self) -> Mapping[str, object]: ...


def evidence_test_run_rows(outcome: _EvidenceOutcome) -> list[dict[str, object]]:
    """The rows of :func:`campaign_test_run_rows`, authenticated for evidence.

    A record that later matches Goal evidence on a Test Run's ``name`` and
    ``fingerprint`` must not take them from a file that merely sits where the
    manifest should be. The manifest read here must be the one the acceptance
    facts name by digest, and every row must carry the fingerprint its
    observation recorded. Facts that do not name Test Runs (written before
    they were recorded) have no rows.

    Raises :class:`SimulationCampaignIntegrityError` when the manifest is
    missing or is not the campaign's, or when an observation has no recorded
    Test Run with its fingerprint.
    """
    facts = outcome.acceptance_facts.document
    observations = cast(tuple[Mapping[str, object], ...], facts["observations"])
    if any("test_run" not in observation for observation in observations):
        return []
    try:
        manifest = decode_simulation_campaign_manifest(outcome.manifest_path.read_bytes())
    except OSError as exc:
        raise SimulationCampaignIntegrityError(
            f"Simulation Campaign manifest cannot be read for its Test Runs: {exc}"
        ) from exc
    if manifest_digest(manifest) != facts["manifest_sha256"]:
        raise SimulationCampaignIntegrityError(
            "Simulation Campaign manifest is not the one its acceptance facts name"
        )
    return observation_test_run_rows(manifest, observations)


def observation_test_run_rows(
    manifest: SimulationCampaignManifest, observations: Sequence[Mapping[str, object]]
) -> list[dict[str, object]]:
    """One row per observation fact, from the Test Runs the authenticated *manifest* records.

    Observation facts that do not name Test Runs have no rows. Raises
    :class:`SimulationCampaignIntegrityError` when an observation has no
    recorded Test Run with the fingerprint it bound.
    """
    if any("test_run" not in observation for observation in observations):
        return []
    recorded = _manifest_test_runs(manifest)
    rows: list[dict[str, object]] = []
    for observation in observations:
        run = _observed_run(observation, recorded)
        if run is None or run.fingerprint != observation["test_run_fingerprint"]:
            raise SimulationCampaignIntegrityError(
                f"observation of Test Run {observation['test_run']!r} has no recorded "
                "Test Run with its fingerprint in the Simulation Campaign manifest"
            )
        row = test_run_row(run, _grade(observation))
        if row["grade"] == "fail":
            row["functional_failure"] = (
                observation["execution"] == "completed"
                and observation["failure_class"] != "infrastructure"
                and (observation["functional"] == "fail" or observation["assertions"] == "dirty")
            )
            if not row["functional_failure"]:
                row["failure_execution"] = observation["execution"]
        rows.append(row)
    return rows


def informative_test_run_lines(
    selector: str, rows: Sequence[Mapping[str, object]], *, default_seed: int | None
) -> list[str]:
    """Report rows only where they say more than the Target's own line.

    One passing Test Run per Test at the Default Seed is what the Target line
    already reports. The rows appear when a Test Run did not pass (a failure
    with its rerun command), or when the Target ran a Test more than once or
    under a name other than its own.
    """
    tests = [row["test"] for row in rows]
    informative = (
        any(row["grade"] != "pass" for row in rows)
        or len(set(tests)) != len(tests)
        or any(
            row["variant"] is not None or row["seed"] not in {None, default_seed} for row in rows
        )
    )
    return test_run_report_lines(selector, rows) if informative else []


def test_run_report_lines(
    selector: str, rows: Sequence[Mapping[str, object]], *, limit: int = ROW_LIMIT
) -> list[str]:
    """The Test Run rows of *selector*'s report, and a rerun command per failure."""
    if not rows:
        return []
    counts = {grade: sum(1 for row in rows if row["grade"] == grade) for grade in TEST_RUN_GRADES}
    summary = ", ".join(
        f"{count} {TEST_RUN_GRADES[grade]}" for grade, count in counts.items() if count
    )
    lines = [f"  Test Runs: {len(rows)} ({summary})"]
    lines += [f"    {str(row['grade']).upper():<12} {row['name']}" for row in rows[:limit]]
    if len(rows) > limit:
        lines.append(f"    ... and {len(rows) - limit} more")
    failed = [row for row in rows if row["grade"] == "fail"]
    if failed:
        lines.append("  Rerun a failed Test Run with the same seed:")
        lines += [f"    {_rerun(selector, row)}" for row in failed[:limit]]
        if len(failed) > limit:
            lines.append(f"    ... and {len(failed) - limit} more")
    return lines


def _rerun(selector: str, row: Mapping[str, object]) -> str:
    """The rerun command of one row."""
    document = {
        key: value
        for key, value in row.items()
        if key not in {"grade", "functional_failure", "failure_execution"}
    }
    return rerun_command(selector, test_run_from_document(document))


# Not pytest tests, although their names start with ``test_``.
test_run_report_lines.__test__ = False  # type: ignore[attr-defined]
