"""Per-Test-Run rows of one Simulation Campaign, for reports (ADR 0072).

A campaign's acceptance facts name the Test Run of every observation, and its
manifest records what each Test Run is: Test Variant, seed, declared
arguments and environment. This module joins the two into one row per Test
Run and renders the report lines, including the command that reruns a failed
Test Run with the same seed. A Test an unfiltered batch discovered cannot be
rerun alone (``--test`` needs a registered Test), so its failure is answered
with the command that reruns its whole batch.
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
    SimulationRecordIntegrityError,
    SimulationRecordManifest,
    UnsupportedSimulationRecordFormatError,
    decode_simulation_record_manifest,
)
from .planning import manifest_digest
from .test_run_files import TestRunFiles, simulation_record_test_run_files

__all__ = [
    "PATH_ROW_LIMIT",
    "ROW_LIMIT",
    "TEST_RUN_GRADES",
    "evidence_test_run_rows",
    "informative_test_run_lines",
    "observation_grade",
    "observation_test_run_rows",
    "simulation_record_test_run_files_by_row",
    "simulation_record_test_run_lines",
    "simulation_record_test_run_rows",
    "test_run_grade",
    "test_run_report_lines",
    "test_run_row",
]

#: Most rows (and most rerun commands) one Target's report shows.
ROW_LIMIT = 32

#: Most Test Runs of one Target whose log and waveform paths the card prints.
#: An absolute evidence path is long: every row of a full report would push the
#: card's head out of the tail an MCP client keeps. ``simulation.json`` carries
#: the paths of every Test Run.
PATH_ROW_LIMIT = 8


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


def observation_grade(observation: Mapping[str, object]) -> str:
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
        manifest = decode_simulation_record_manifest(manifest_path.read_bytes())
    except (OSError, SimulationRecordIntegrityError, UnsupportedSimulationRecordFormatError):
        return {}
    try:
        return _manifest_test_runs(manifest)
    except TestRunNameError:
        # Decode reads Test Runs raw; a hand-edited record surfaces here.
        return {}


def _manifest_test_runs(manifest: SimulationRecordManifest) -> dict[tuple[object, str], TestRun]:
    """Every Test Run *manifest* records, by work item and Test Run name."""
    recorded: dict[tuple[object, str], TestRun] = {}
    items = cast(tuple[Mapping[str, object], ...], manifest.document["work_items"])
    for item in items:
        for document in cast(tuple[object, ...], item["test_runs"]):
            run = test_run_from_document(document)
            recorded[(item["work_item_id"], run.name)] = run
            if run.test is None:
                # Tests an unfiltered Cocotb batch discovers run under this seed.
                recorded[(item["work_item_id"], "")] = run
    return recorded


def _resolve_observation(
    observation: Mapping[str, object], recorded: Mapping[tuple[object, str], TestRun]
) -> tuple[TestRun | None, TestRun | None]:
    """The Test Run an observation names, and the batch it was discovered in.

    The batch is the work item's unnamed invocation when the observation is a
    Test the unfiltered batch discovered (a named run the manifest does not
    record); it is ``None`` for a recorded run and for the unnamed invocation's
    own result.
    """
    name = cast(str, observation["test_run"])
    item = observation["work_item_id"]
    run = recorded.get((item, name))
    if run is not None:
        return run, None
    unnamed = recorded.get((item, ""))
    test = observation["test"]
    if unnamed is None or not isinstance(test, str):
        return None, None
    selector = cast(Mapping[str, object], observation["target"])["selector"]
    discovered = discovered_test_run(test, unnamed, target=cast(str, selector))
    return discovered, (None if discovered is unnamed else unnamed)


def _observed_run(
    observation: Mapping[str, object], recorded: Mapping[tuple[object, str], TestRun]
) -> TestRun | None:
    """The Test Run an observation names, as the manifest recorded it."""
    return _resolve_observation(observation, recorded)[0]


def _observed_rows(outcome: _Outcome) -> list[tuple[dict[str, object], int]]:
    """Each observed Test Run's row with the index of its observation."""
    recorded = _recorded_test_runs(outcome.manifest_path)
    rows: list[tuple[dict[str, object], int]] = []
    for index, observation in enumerate(outcome.observations):
        run = _observed_run(observation, recorded)
        if run is not None:
            rows.append((test_run_row(run, observation_grade(observation)), index))
    return rows


def simulation_record_test_run_rows(outcome: _Outcome) -> list[dict[str, object]]:
    """One row per observed Test Run: what it is, and how it ended.

    Each row holds the recorded ``name``, ``test``, ``variant``, ``seed``,
    ``args``, ``env`` and ``fingerprint`` plus ``grade``. A campaign whose
    manifest cannot be read has no rows.
    """
    return [row for row, _ in _observed_rows(outcome)]


def simulation_record_test_run_files_by_row(outcome: _EvidenceOutcome) -> tuple[TestRunFiles, ...]:
    """The evidence files of each row of :func:`campaign_test_run_rows`, in its order."""
    files = simulation_record_test_run_files(outcome)
    return tuple(files[index] for _, index in _observed_rows(outcome))


def simulation_record_test_run_lines(
    selector: str, outcome: _EvidenceOutcome, *, default_seed: int | None
) -> list[str]:
    """The report lines of *outcome*'s Test Runs, per :func:`informative_test_run_lines`.

    A failed Test that the unfiltered batch discovered is answered with the
    command that reruns the batch, since it has no registered Test to select.
    """
    recorded = _recorded_test_runs(outcome.manifest_path)
    evidence = simulation_record_test_run_files(outcome)
    rows: list[dict[str, object]] = []
    files: list[TestRunFiles] = []
    batches: dict[str, TestRun] = {}
    for index, observation in enumerate(outcome.observations):
        run, batch = _resolve_observation(observation, recorded)
        if run is None:
            continue
        rows.append(test_run_row(run, observation_grade(observation)))
        files.append(evidence[index])
        if batch is not None:
            batches[run.name] = batch
    return informative_test_run_lines(
        selector, rows, default_seed=default_seed, batches=batches, files=files
    )


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
    observation recorded.

    Raises :class:`SimulationCampaignIntegrityError` when the manifest is
    missing or is not the campaign's, or when an observation has no recorded
    Test Run with its fingerprint.
    """
    facts = outcome.acceptance_facts.document
    observations = cast(tuple[Mapping[str, object], ...], facts["observations"])
    try:
        manifest = decode_simulation_record_manifest(outcome.manifest_path.read_bytes())
    except OSError as exc:
        raise SimulationRecordIntegrityError(
            f"Simulation Campaign manifest cannot be read for its Test Runs: {exc}"
        ) from exc
    if manifest_digest(manifest) != facts["manifest_sha256"]:
        raise SimulationRecordIntegrityError(
            "Simulation Campaign manifest is not the one its acceptance facts name"
        )
    return observation_test_run_rows(manifest, observations)


def observation_test_run_rows(
    manifest: SimulationRecordManifest, observations: Sequence[Mapping[str, object]]
) -> list[dict[str, object]]:
    """One row per observation fact, from the Test Runs the authenticated *manifest* records.

    Raises :class:`SimulationCampaignIntegrityError` when an observation has no
    recorded Test Run with the fingerprint it bound.
    """
    recorded = _manifest_test_runs(manifest)
    rows: list[dict[str, object]] = []
    for observation in observations:
        run = _observed_run(observation, recorded)
        if run is None or run.fingerprint != observation["test_run_fingerprint"]:
            raise SimulationRecordIntegrityError(
                f"observation of Test Run {observation['test_run']!r} has no recorded "
                "Test Run with its fingerprint in the Simulation Campaign manifest"
            )
        row = test_run_row(run, observation_grade(observation))
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
    selector: str,
    rows: Sequence[Mapping[str, object]],
    *,
    default_seed: int | None,
    batches: Mapping[str, TestRun] | None = None,
    files: Sequence[TestRunFiles] = (),
) -> list[str]:
    """Report rows only where they say more than the Target's own line.

    One passing Test Run per Test at the Default Seed is what the Target line
    already reports. The rows appear when a Test Run did not pass (a failure
    with its rerun command), when the Target ran a Test more than once or
    under a name other than its own, or when a passing Test Run captured a
    waveform (a ``--trace`` run) whose path the rows then name. *files* holds
    each row's evidence files, in the rows' order. *batches* maps the names of
    discovered Tests to the batch that reruns them (see
    :func:`test_run_report_lines`).
    """
    tests = [row["test"] for row in rows]
    informative = (
        any(row["grade"] != "pass" for row in rows)
        or len(set(tests)) != len(tests)
        or any(
            row["variant"] is not None or row["seed"] not in {None, default_seed} for row in rows
        )
        or any(found.trace is not None for found in files)
    )
    return (
        test_run_report_lines(selector, rows, files=files, batches=batches) if informative else []
    )


def test_run_report_lines(
    selector: str,
    rows: Sequence[Mapping[str, object]],
    *,
    limit: int = ROW_LIMIT,
    batches: Mapping[str, TestRun] | None = None,
    files: Sequence[TestRunFiles] = (),
) -> list[str]:
    """The Test Run rows of *selector*'s report, and a rerun command per failure.

    *files*, aligned with *rows*, adds the evidence paths under the first
    :data:`PATH_ROW_LIMIT` rows that have any: ``log:`` and ``waveform:`` under a
    Test Run that did not pass, only ``waveform:`` under one that passed.

    A failure gets its own rerun command, or its batch's when its name is in
    *batches*: a discovered Test cannot be selected alone, so every discovered
    failure of one batch shares one command, printed once.
    """
    if not rows:
        return []
    counts = {grade: sum(1 for row in rows if row["grade"] == grade) for grade in TEST_RUN_GRADES}
    summary = ", ".join(
        f"{count} {TEST_RUN_GRADES[grade]}" for grade, count in counts.items() if count
    )
    paths, omitted = _path_lines(rows[:limit], files)
    lines = [f"  Test Runs: {len(rows)} ({summary})"]
    for index, row in enumerate(rows[:limit]):
        lines.append(f"    {str(row['grade']).upper():<12} {row['name']}")
        lines += paths.get(index, [])
    if len(rows) > limit:
        lines.append(f"    ... and {len(rows) - limit} more")
    if omitted:
        lines.append(
            f"  Log and waveform paths of {omitted} more Test Runs: simulation.json tests[]"
        )
    return lines + _rerun_lines(selector, rows, limit, batches or {})


def _rerun_lines(
    selector: str,
    rows: Sequence[Mapping[str, object]],
    limit: int,
    batches: Mapping[str, TestRun],
) -> list[str]:
    """The rerun block of the failed *rows*: a command each, one per shared batch."""
    failed = [row for row in rows if row["grade"] == "fail"]
    if not failed:
        return []
    commands = list(
        dict.fromkeys(
            rerun_command(selector, batches[str(row["name"])])
            if row["name"] in batches
            else _rerun(selector, row)
            for row in failed
        )
    )
    heading = (
        "  Rerun the whole batch with the same seed "
        "(register its test functions as Tests in tests.toml to rerun one alone):"
        if any(row["name"] in batches for row in failed)
        else "  Rerun a failed Test Run with the same seed:"
    )
    lines = [heading] + [f"    {command}" for command in commands[:limit]]
    if len(commands) > limit:
        lines.append(f"    ... and {len(commands) - limit} more")
    return lines


def _row_path_lines(row: Mapping[str, object], found: TestRunFiles) -> list[str]:
    """The evidence lines under one row: ``log:`` only for a Test Run that did not pass."""
    lines: list[str] = []
    if found.log is not None and row["grade"] != "pass":
        lines.append(f"      log: {found.log.resolve()}")
    if found.trace is not None:
        lines.append(f"      waveform: {found.trace.resolve()}")
    return lines


def _path_lines(
    rows: Sequence[Mapping[str, object]], files: Sequence[TestRunFiles]
) -> tuple[dict[int, list[str]], int]:
    """The evidence lines to print by row index, and how many rows lost theirs to the budget.

    At most :data:`PATH_ROW_LIMIT` rows print paths. A Test Run that did not
    pass has first claim, so a long traced run never hides a failure's log
    behind passing waveforms.
    """
    found = {
        index: _row_path_lines(row, files[index]) for index, row in enumerate(rows[: len(files)])
    }
    wanted = {index: lines for index, lines in found.items() if lines}
    failing = [index for index in wanted if rows[index]["grade"] != "pass"]
    passing = [index for index in wanted if rows[index]["grade"] == "pass"]
    chosen = (failing + passing)[:PATH_ROW_LIMIT]
    return {index: wanted[index] for index in sorted(chosen)}, len(wanted) - len(chosen)


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
