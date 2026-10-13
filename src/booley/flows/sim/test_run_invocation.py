"""What one Simulation Flow invocation decided about its Test Runs (ADR 0072).

One :class:`TestRunInvocation` belongs to one invocation of the Simulation
Flow. It holds the decisions that later steps of the same invocation must
reuse rather than make again: the Test Runs resolved for each Target (a drawn
seed is never drawn twice), the Test Runs each planned simulator process
runs, the candidate Test Runs each Cycle Count baseline Target runs, and,
inside Goal Mode, the Test Runs the Goals are frozen to.

Domain values only: no CLI, process or Sandbox imports.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType

from booley.flows.frozen_test_runs import FrozenTestRuns
from booley.flows.target_test_plan import TargetTestPlan
from booley.flows.test_runs import TestRun, TestVariantError, UnseededTargetError

__all__ = [
    "BaselineLink",
    "BaselineRefusal",
    "BaselineRuns",
    "TestRunInvocation",
    "baseline_link_refusal",
    "baseline_refusal_key",
    "decide_baseline_runs",
]


@dataclass(frozen=True)
class BaselineLink:
    """One relative Cycle Count comparison: a candidate Test on a baseline Target.

    ``run`` is one Test Run of ``test`` the candidate runs in this invocation,
    the Test Run both revisions run. A candidate that runs several Test Runs
    of the Test (``test@x3``, two Test Variants) has one link per Test Run.
    ``None`` when the candidate runs no Test Run of that Test.
    """

    candidate: str
    baseline: str
    test: str
    run: TestRun | None = None


@dataclass(frozen=True)
class BaselineRefusal:
    """A comparison the baseline revision does not run, and why."""

    link: BaselineLink
    reason: str


@dataclass(frozen=True)
class BaselineRuns:
    """The candidate Test Runs the Cycle Count baseline revision runs.

    A baseline Target runs every distinct candidate Test Run it is compared
    against, and nothing else: never a Test Run its own checkout would
    resolve. Every candidate Test Run of a compared Test is either in
    ``links`` or in ``refused`` with its reason; so is a compared Test the
    candidate does not run at all.
    """

    links: tuple[BaselineLink, ...] = ()
    refused: tuple[BaselineRefusal, ...] = ()

    @property
    def refusals(self) -> Mapping[tuple[str, str], str]:
        """Why a comparison has no baseline run, by candidate Target and Test Run.

        The second key is the candidate's Test Run name, or the bare Test
        when the candidate runs no Test Run of it.
        """
        return MappingProxyType(
            {
                (item.link.candidate, baseline_refusal_key(item.link.test, item.link.run)): (
                    item.reason
                )
                for item in self.refused
            }
        )

    def runs_of(self, baseline: str) -> tuple[TestRun, ...]:
        """The distinct Test Runs *baseline* runs, in order of first need."""
        runs = (link.run for link in self.links if link.baseline == baseline)
        return tuple(dict.fromkeys(run for run in runs if run is not None))

    def links_of(self, baseline: str) -> tuple[BaselineLink, ...]:
        """The comparisons *baseline* serves."""
        return tuple(link for link in self.links if link.baseline == baseline)


def baseline_refusal_key(test: str, run: TestRun | str | None) -> str:
    """What names one comparison in :attr:`BaselineRuns.refusals`.

    The candidate's Test Run name (*run* may be the run or its name), or the
    bare Test when the candidate observed no Test Run of it.
    """
    if run is None:
        return test
    return run if isinstance(run, str) else run.name


def baseline_link_refusal(
    plan: TargetTestPlan | None, link: BaselineLink, revision: str
) -> str | None:
    """Why the baseline revision cannot run the candidate's Test Run of *link*.

    *plan* is the baseline checkout's own plan of the baseline Target; it is
    needed only for a link that has a Test Run. ``None`` when it can. A Cocotb Target without a registry discovers its
    Tests in the module, so only a registry that omits the Test refuses it.
    """
    where = f"baseline revision {revision[:12]}"
    if link.run is None:
        return (
            f"this invocation runs no Test Run of Test {link.test!r} on Target "
            f"{link.candidate!r}, so there is nothing to compare"
        )
    assert plan is not None, "a link with a Test Run needs the baseline Target's plan"
    discovers = plan.adapter == "cocotb" and not plan.tests
    if link.test not in plan.tests and not discovers:
        return f"{where} does not register Test {link.test!r} on Target {plan.target!r}"
    try:
        plan.require_applicable(link.run)
    except UnseededTargetError as exc:
        return f"{where} cannot apply the seed: {exc}"
    except TestVariantError as exc:
        return f"{where} cannot apply the Test Variant arguments: {exc}"
    return None


def decide_baseline_runs(
    links: Sequence[BaselineLink],
    plan_of: Callable[[str], TargetTestPlan],
    revision: str,
) -> BaselineRuns:
    """Decide which candidate Test Runs each baseline Target runs.

    *plan_of* builds the baseline checkout's plan of a baseline Target; it is
    asked once per Target, and only for a link that has a Test Run. A link the
    baseline cannot run is refused with its reason. So is a link whose Test
    Run has the name of another Test Run already accepted for the same
    baseline Target but other declared inputs: one campaign cannot hold two
    Test Runs of one name.
    """
    accepted: list[BaselineLink] = []
    refused: list[BaselineRefusal] = []
    named: dict[tuple[str, str], TestRun] = {}
    plans: dict[str, TargetTestPlan] = {}
    for link in links:
        if link.run is not None and link.baseline not in plans:
            plans[link.baseline] = plan_of(link.baseline)
        reason = baseline_link_refusal(plans.get(link.baseline), link, revision)
        if reason is None:
            assert link.run is not None
            first = named.setdefault((link.baseline, link.run.name), link.run)
            if first != link.run:
                reason = (
                    f"Test Run {link.run.name!r} has other declared arguments or "
                    f"environment on Target {link.candidate!r} than on another candidate "
                    f"compared against baseline Target {link.baseline!r}"
                )
        if reason is None:
            accepted.append(link)
        else:
            refused.append(BaselineRefusal(link, reason))
    return BaselineRuns(tuple(accepted), tuple(refused))


@dataclass
class TestRunInvocation:
    """The Test Run decisions of one Simulation Flow invocation."""

    __test__ = False

    #: Resolved Test Runs by ``(checkout, Target)``; resolved once, never redrawn.
    resolved: dict[tuple[str, str], tuple[TestRun, ...]] = field(default_factory=dict)
    #: The Test Runs of each planned simulator process, by ``(role, Target)``.
    planned_groups: dict[tuple[str, str], tuple[tuple[TestRun, ...], ...]] = field(
        default_factory=dict
    )
    #: Each candidate Target's effective Default Seed, recorded when its Test
    #: Runs are resolved. Reports use it to tell a plain run from a seeded one.
    default_seeds: dict[str, int | None] = field(default_factory=dict)
    baseline: BaselineRuns = field(default_factory=BaselineRuns)
    #: The Test Runs the run's Goals are frozen to (ADR 0072): empty until
    #: read, then the one reading of the invocation (``None`` outside Goal Mode).
    frozen: tuple[FrozenTestRuns | None, ...] = ()
    #: ``@xN`` selectors that matched no Goal, by Target: their seeds were
    #: drawn for this invocation and their Test Runs are diagnostic.
    fresh_seed_selectors: dict[str, tuple[str, ...]] = field(default_factory=dict)
