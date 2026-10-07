"""The simulation contract for Goals (ADR 0067 Phase 3, B9).

A simulation Goal names a Target only. Its suite is the Target's complete
currently-resolved suite: the ``tests.toml`` tests of that Target (bare or
VLNV-qualified table, shared test lists expanded) minus its durable skips,
or the Target's single default invocation when it registers no tests.

Evidence records the resolved suite under :data:`GOAL_SUITE_DETAIL_KEY` when
it is published, and a passing run marks the Goal met only when that suite
is non-empty and the run passed every test in it; a run that covers part of
the suite, or a Target whose tests are all skipped, never does. A later
change of the resolved suite (a test added, removed, or skipped) makes the
Goal stale (:mod:`booley.goals.freshness`).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, cast

from booley.config.project_config import load_test_configuration_field
from booley.flows.target_test_suite import resolve_target_test_suite

#: Detail key holding the suite a simulation Goal's evidence was judged against.
GOAL_SUITE_DETAIL_KEY = "goal_required_tests"
#: The name the default invocation of a Target without registered tests has.
DEFAULT_TEST_NAME = "default"


def resolved_simulation_suite(work_dir: Path, target: str) -> tuple[str, ...]:
    """The complete suite *target* resolves to in *work_dir* now, sorted.

    Raises ``ValueError`` (including ``tomllib.TOMLDecodeError``) for a
    malformed ``tests.toml`` and ``OSError`` when it cannot be read.
    """
    suite = resolve_target_test_suite(
        target,
        test_names=load_test_configuration_field(work_dir, "tests"),
        test_skips=load_test_configuration_field(work_dir, "skip"),
    )
    return tuple(sorted(DEFAULT_TEST_NAME if name is None else name for name in suite.tests))


def simulation_contract_violation(detail: Mapping[str, Any], suite: Sequence[str]) -> str:
    """Why passing evidence with *detail* does not satisfy *suite*; empty when it does.

    A producer names the tests it required as ``required_tests`` (the
    Simulation Flow) or, for a Coverage campaign, as the ``selected_tests``
    it ran: Coverage selects the registered suite minus durable skips, the
    same set the Flow requires, and a ``--tests`` subset is then a partial
    run. The Coverage detail is not changed for this (Ticket bytes).
    """
    if not suite:
        return "the Target's resolved simulation suite is empty"
    required = _names(detail.get("required_tests"))
    if required is None:
        required = _names(detail.get("selected_tests"))
    passed = _names(detail.get("passed_tests"))
    if required is None or passed is None:
        return "the evidence does not name the tests it required and passed"
    if required != frozenset(suite):
        return "the run did not require the complete resolved suite"
    missing = sorted(frozenset(suite) - passed)
    if missing:
        return f"the run did not pass {', '.join(missing)}"
    return ""


def _names(value: object) -> frozenset[str] | None:
    if not isinstance(value, list):
        return None
    items = cast("list[object]", value)
    if not all(isinstance(item, str) for item in items):
        return None
    return frozenset(cast("list[str]", items))
