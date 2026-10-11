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
    build_test_run,
    rerun_command,
    test_run_document,
    test_run_from_document,
)

from .codec import SimulationCampaignIntegrityError, decode_simulation_campaign_manifest

__all__ = ["ROW_LIMIT", "campaign_test_run_rows", "test_run_report_lines"]

#: Most rows (and most rerun commands) one Target's report shows.
ROW_LIMIT = 32


class _Outcome(Protocol):
    @property
    def manifest_path(self) -> Path: ...

    @property
    def observations(self) -> tuple[Mapping[str, object], ...]: ...


def _grade(observation: Mapping[str, object]) -> str:
    """One word for how a Test Run ended: pass, fail, error or inconclusive."""
    if (
        observation["execution"] == "completed"
        and observation["functional"] == "pass"
        and observation["assertions"] != "dirty"
    ):
        return "pass"
    if observation["failure_class"] == "infrastructure":
        return "error"
    if observation["functional"] == "inconclusive":
        return "inconclusive"
    return "fail"


def _recorded_test_runs(manifest_path: Path) -> dict[tuple[object, str], TestRun]:
    """Every Test Run the manifest records, by work item and Test Run name."""
    try:
        manifest = decode_simulation_campaign_manifest(manifest_path.read_bytes())
    except (OSError, SimulationCampaignIntegrityError):
        return {}
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
    try:
        return build_test_run(test, seed=unnamed.seed, env=dict(unnamed.env))
    except ValueError:
        return None


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
            rows.append({**test_run_document(run), "grade": _grade(observation)})
    return rows


def test_run_report_lines(
    selector: str, rows: Sequence[Mapping[str, object]], *, limit: int = ROW_LIMIT
) -> list[str]:
    """The Test Run rows of *selector*'s report, and a rerun command per failure."""
    if not rows:
        return []
    counts = {grade: sum(1 for row in rows if row["grade"] == grade) for grade in _GRADES}
    summary = ", ".join(f"{count} {_GRADES[grade]}" for grade, count in counts.items() if count)
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


_GRADES = {"pass": "passed", "fail": "failed", "error": "error", "inconclusive": "inconclusive"}


def _rerun(selector: str, row: Mapping[str, object]) -> str:
    """The rerun command of one row."""
    document = {key: value for key, value in row.items() if key != "grade"}
    return rerun_command(selector, test_run_from_document(document))


# Not pytest tests, although their names start with ``test_``.
test_run_report_lines.__test__ = False  # type: ignore[attr-defined]
