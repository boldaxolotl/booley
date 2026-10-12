"""What one Simulation Flow invocation decided about its Test Runs (ADR 0072).

One :class:`TestRunInvocation` belongs to one invocation of the Simulation
Flow. It holds the decisions that later steps of the same invocation must
reuse rather than make again: the Test Runs resolved for each Target (a drawn
seed is never drawn twice), the Test Runs each planned simulator process
runs, the candidate Test Runs each Cycle Count baseline Target runs, and
whether the invocation records Goal evidence at all.

Domain values only: no CLI, process or Sandbox imports.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType

from booley.flows.target_test_plan import TargetTestPlan
from booley.flows.test_runs import TestRun, TestVariantError, UnseededTargetError

__all__ = [
    "BaselineLink",
    "BaselineRuns",
    "EvidenceGuard",
    "TestRunInvocation",
    "baseline_link_refusal",
    "decide_baseline_runs",
]


@dataclass(frozen=True)
class EvidenceGuard:
    """Whether an invocation's Test Runs let it record Goal evidence.

    INTERIM (#1233 stage S2; removed by stage S3). Goals cannot name a Test
    Variant or a seed until S3, so evidence still matches by bare Test name.
    An invocation that contains any Test Run other than the default Test
    Variant at the Target's Default Seed therefore records no evidence for
    any simulation, Coverage or Cycle Count Goal. So does an invocation whose
    Test Runs could not be resolved: nothing shows they are defaults.

    Withholding evidence is not ``--diagnostic``: it grants none of that
    flag's privileges.
    """

    #: ``"<target>: <test run>"`` for every Test Run that is not a default.
    other_test_runs: tuple[str, ...] = ()
    #: ``"<target>: <reason>"`` for every Target whose Test Runs did not resolve.
    unresolved: tuple[str, ...] = ()
    #: The bare Test names of the request's default Test Runs, in order.
    tests: tuple[str, ...] = ()

    @property
    def withholds_evidence(self) -> bool:
        """Whether the invocation records no Goal evidence."""
        return bool(self.other_test_runs or self.unresolved)


@dataclass(frozen=True)
class BaselineLink:
    """One relative Cycle Count comparison: a candidate Test on a baseline Target.

    ``run`` is the candidate's Test Run of ``test`` in this invocation, the
    one Test Run both revisions run; ``None`` when the candidate runs no Test
    Run of that Test.
    """

    candidate: str
    baseline: str
    test: str
    run: TestRun | None = None


@dataclass(frozen=True)
class BaselineRuns:
    """The candidate Test Runs the Cycle Count baseline revision runs.

    A baseline Target runs every distinct candidate Test Run it is compared
    against, and nothing else: never a Test Run its own checkout would
    resolve. ``refusals`` names why a comparison has no baseline run, by
    ``(candidate Target, Test)``.
    """

    links: tuple[BaselineLink, ...] = ()
    refusals: Mapping[tuple[str, str], str] = field(default_factory=lambda: MappingProxyType({}))

    def runs_of(self, baseline: str) -> tuple[TestRun, ...]:
        """The distinct Test Runs *baseline* runs, in order of first need."""
        runs = (link.run for link in self.links if link.baseline == baseline)
        return tuple(dict.fromkeys(run for run in runs if run is not None))

    def links_of(self, baseline: str) -> tuple[BaselineLink, ...]:
        """The comparisons *baseline* serves."""
        return tuple(link for link in self.links if link.baseline == baseline)


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
    refusals: dict[tuple[str, str], str] = {}
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
            refusals[(link.candidate, link.test)] = reason
    return BaselineRuns(tuple(accepted), MappingProxyType(refusals))


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
    baseline: BaselineRuns = field(default_factory=BaselineRuns)
    guard: EvidenceGuard = field(default_factory=EvidenceGuard)
