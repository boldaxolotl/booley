"""Estimate a Job's phase memory and wall-clock time from the Resource History (ADR 0087).

Read-only and in memory: the caller reads the records with
``resource_history.load_records`` and hands them over. A Flow request becomes
its phase keys (the keys the Flow's recorder writes), and each phase is
estimated from the retained records:

* **own**: the largest value over all records of the phase's key, times
  :data:`ESTIMATE_MARGIN`;
* **borrowed**: with no own record, the largest estimate for the same Target
  and phase (any Test Run or flags), else for that phase anywhere in the
  Project;
* **unknown**: with neither, the phase has no estimate.

Memory and wall-clock time follow the same rules, each on its own values. Every
outcome counts: an OOM-killed or timed-out run is a lower bound, so it can
raise an estimate but never lower it.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, TypeVar

from booley.flows.target_test_plan import TargetTestPlan, batch_test_runs
from booley.flows.test_runs import TestRun
from booley.runtime.resource_history import HistoryKey, history_key

#: Headroom over the largest value seen (ADR 0087).
ESTIMATE_MARGIN = 1.25

#: Flow names with their own phase keys; any other name is a Custom Flow.
_SIM_FLOW = "sim"
_SINGLE_PHASE_FLOWS = {"lint": "lint", "fpga": "fpga_impl", "synth": "synth"}
_COVERAGE_FLAG = "coverage"

_K = TypeVar("_K")


@dataclass(frozen=True)
class JobRequest:
    """What a Job will run, in the terms the Resource History keys use."""

    #: Canonical Flow name (``sim``, ``lint``, ``synth``, ``fpga``) or a Custom Flow's name.
    flow: str
    #: The Target string the recorder keys on (for a Custom Flow, its ``--target`` value).
    target: str
    #: Sim only: the Resource History Test Run name of each simulator process, in
    #: run order (``""`` is the unnamed invocation; ``()`` runs no simulator,
    #: as in ``--mode elab-only``). Build it with :func:`sim_job_request`.
    test_runs: tuple[str, ...] = ("",)
    #: Instrumentation flags (``coverage``, ``trace``); order does not matter.
    flags: tuple[str, ...] = ()


def sim_job_request(
    plan: TargetTestPlan, runs: Sequence[TestRun], *, flags: tuple[str, ...] = ()
) -> JobRequest:
    """The sim request that runs *runs* (resolved by ``plan.resolve``) on *plan*'s Target.

    Test Runs are grouped into simulator processes and named exactly as the
    simulation engine does: one process per Test Run on an HDL Target, Test Runs
    sharing environment and seed batched on a Cocotb Target. Coverage runs one
    collector process per Test Run on either. A name is ``test`` or
    ``test+variant``; seeds never appear.

    The request always lists simulator processes; a ``--mode elab-only`` Job
    runs none, so its caller builds ``JobRequest(..., test_runs=())`` itself.
    """
    one_process_per_run = plan.adapter != "cocotb" or _COVERAGE_FLAG in flags
    groups = batch_test_runs(runs, one_process_per_run=one_process_per_run)
    names = tuple(
        ",".join(plan.history_test_name(run) for run in group if run.test is not None)
        for group in groups
    )
    return JobRequest(_SIM_FLOW, plan.target, test_runs=names, flags=flags)


def phase_keys(request: JobRequest) -> tuple[HistoryKey, ...]:
    """The history keys of *request*'s phases, in run order.

    A phase is listed once per key, however many commands the Job runs under it
    (physical synthesis runs two ``synth`` commands; baseline reruns repeat
    keys). A Job's memory reservation is a maximum, so it stays right; a phase's
    wall-clock estimate is per command, so a wall-clock summed over phases
    under-counts such Jobs.

    Synthesis lists its whole-command ``synth`` key only: the split ``sv2v``,
    ``yosys`` and ``openroad`` keys depend on the synthesis mode and frontend,
    and a listed phase that never runs would leave the Job unknown forever.
    ``sim_pre_run`` is never listed (nothing records it), nor are the
    per-module probe keys of ``--mode elab-only-standalone``.
    """
    flags = tuple(sorted(request.flags))

    def key(phase: str, test_run: str = "") -> HistoryKey:
        return HistoryKey(request.target, phase, flags, test_run)

    if request.flow == _SIM_FLOW:
        coverage = _COVERAGE_FLAG in request.flags
        keys = [key("sim_prepare")] if coverage else []
        keys.append(key("sim_build"))
        keys.extend(key("sim_run", name) for name in request.test_runs)
        if coverage:
            keys.append(key("sim_postprocess"))
    else:
        # A Custom Flow's unlabelled command is keyed by the Flow's name.
        keys = [key(_SINGLE_PHASE_FLOWS.get(request.flow, request.flow))]
    return tuple(dict.fromkeys(keys))


class Source(StrEnum):
    """Where an estimate came from."""

    OWN = "own"
    TARGET = "target"
    PROJECT = "project"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class Estimate:
    """One estimated value (``None`` when unknown) and where it came from."""

    value: float | None
    source: Source


@dataclass(frozen=True)
class PhaseEstimate:
    """The estimates of one phase."""

    key: HistoryKey
    memory_mb: Estimate
    wall_s: Estimate


class Fit(StrEnum):
    """How a Job's reservation compares with a memory budget."""

    FITS = "fits"
    #: Some phase has no memory estimate: the Job must run alone.
    RUN_ALONE = "run_alone"
    #: The reservation is larger than the budget: the Job also runs alone, with a warning.
    OVER_BUDGET = "over_budget"


@dataclass(frozen=True)
class JobEstimate:
    """A Job's phase estimates, in run order.

    A borrowed or unknown wall-clock is not history: a caller that jumps a Job
    ahead of the queue because it is short must require
    ``Source.OWN`` on the wall-clock of every phase. See :func:`phase_keys` for
    why a summed wall-clock can under-count a Job.
    """

    phases: tuple[PhaseEstimate, ...]

    @property
    def reservation_mb(self) -> float | None:
        """The starting reservation: the largest known phase memory (``None`` if none is known)."""
        known = [p.memory_mb.value for p in self.phases if p.memory_mb.value is not None]
        return max(known, default=None)

    def fit(self, budget_mb: float) -> Fit:
        """Compare the reservation with *budget_mb*; over-budget wins so a caller can warn."""
        reservation = self.reservation_mb
        if reservation is not None and reservation > budget_mb:
            return Fit.OVER_BUDGET
        if any(p.memory_mb.source is Source.UNKNOWN for p in self.phases):
            return Fit.RUN_ALONE
        return Fit.FITS


class _Maxima:
    """The largest value of one record field, by key, by Target and phase, and by phase."""

    def __init__(self, records: Iterable[dict[str, Any]], field: str) -> None:
        self._own: dict[HistoryKey, float] = {}
        self._target: dict[tuple[str, str], float] = {}
        self._project: dict[str, float] = {}
        for record in records:
            raw = record[field]
            if raw is None:  # unmeasured: says nothing
                continue
            value = float(raw)
            key = history_key(record)
            _raise_to(self._own, key, value)
            _raise_to(self._target, (key.target, key.phase), value)
            _raise_to(self._project, key.phase, value)

    def estimate(self, key: HistoryKey) -> Estimate:
        """Own, else the Target's, else the Project's largest for *key*'s phase, with margin."""
        for source, found in (
            (Source.OWN, self._own.get(key)),
            (Source.TARGET, self._target.get((key.target, key.phase))),
            (Source.PROJECT, self._project.get(key.phase)),
        ):
            if found is not None:
                return Estimate(found * ESTIMATE_MARGIN, source)
        return Estimate(None, Source.UNKNOWN)


def _raise_to(largest: dict[_K, float], key: _K, value: float) -> None:
    """Record *value* under *key* when it is the largest seen."""
    if key not in largest or value > largest[key]:
        largest[key] = value


class ResourceEstimator:
    """Estimates Jobs from a snapshot of Resource History records."""

    def __init__(self, records: Sequence[dict[str, Any]]) -> None:
        self._memory = _Maxima(records, "peak_memory_mb")
        self._wall = _Maxima(records, "wall_s")

    def estimate(self, requests: Sequence[JobRequest]) -> JobEstimate:
        """Estimate a Job: one request per Target, phases concatenated in order."""
        return JobEstimate(
            tuple(
                PhaseEstimate(key, self._memory.estimate(key), self._wall.estimate(key))
                for request in requests
                for key in phase_keys(request)
            )
        )
