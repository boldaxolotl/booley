"""Test Run rows and grades of collected Coverage Campaign runs."""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

from booley.flows.sim.record.test_run_report import test_run_grade, test_run_row
from booley.flows.test_runs import TestRun, build_test_run

if TYPE_CHECKING:
    from .coverage_campaign import CoverageRun


def coverage_test_run_rows(
    test_runs: Sequence[TestRun], runs: Sequence[CoverageRun]
) -> list[dict[str, object]]:
    """One row per collected run: the Test Run it is, and how it ended.

    *test_runs* are the Test Runs the invocation froze before collection; a
    row takes its Test Variant, seed, arguments and environment from there,
    never from the current ``tests.toml``. A run collected without frozen
    Test Runs is the bare Test with no declared inputs. A run whose recorded
    fingerprint is not the frozen Test Run's has no row.
    """
    frozen = {run.name: run for run in test_runs}
    rows: list[dict[str, object]] = []
    for run in runs:
        test_run = frozen.get(run.test_run) or _bare_test_run(run)
        if test_run is None or test_run.fingerprint != run.fingerprint:
            continue
        rows.append(test_run_row(test_run, coverage_run_grade(run)))
    return rows


def _bare_test_run(run: CoverageRun) -> TestRun | None:
    """The Test Run of a run that names only its Test, when that is a Test name."""
    if run.test_run != run.test:
        return None
    try:
        return build_test_run(run.test)
    except ValueError:
        return None


def coverage_run_grade(run: CoverageRun) -> str:
    """Grade a coverage run."""
    return test_run_grade(
        passed=run.simulation_verdict == "pass",
        infrastructure=run.attributes.get("failure_kind") == "infrastructure",
        inconclusive=run.simulation_verdict == "inconclusive",
    )
