"""Goals that name Test Runs: keys, written selectors, canonical identity (ADR 0072).

Three Goal families are judged by Test Runs: simulation, Cycle Count and
coverage. A Goal names them with the Test Run selector grammar
(:func:`booley.flows.test_runs.parse_test_selector`):

- a simulation Goal either names one selector (a **Test Goal**) or none: the
  **Target Goal**, every Test of the Target as its default Test Variant;
- a Cycle Count Goal names one selector without a seed count;
- a coverage Goal names ``"all"`` (as a Target Goal does) or a list of
  selectors.

Keys come from the written form, so a Goal an older Booley wrote keeps its
key. Two written forms may still name the same Test Run (``stress`` and
``stress+short`` when ``short`` is the default Test Variant); the canonical
identity (:func:`canonical_identity`), built with the Target's Test Run plan
at entry, is what entry merges on.

Pure: no filesystem, Git or record access.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from booley.flows.test_runs import (
    SEED_SEPARATOR,
    VARIANT_SEPARATOR,
    PinnedSeed,
    SeedCount,
    TestSelector,
    UnpinnedSeed,
    parse_test_selector,
)
from booley.goals.model import GoalFamily, GoalSpec

#: Marks the key of a simulation Goal that names a Test Run selector. The key
#: stays in the ``sim_pass`` Criterion family, so everything keyed on that
#: family (admission of the Target, source categories, presentation) applies
#: to a Test Goal as it does to a Target Goal.
SIMULATION_RUN_KEY_MARK = "_run_"

#: Goal families whose evidence is matched against frozen Test Runs.
TEST_RUN_FAMILIES: frozenset[GoalFamily] = frozenset(
    {GoalFamily.SIM, GoalFamily.CYCLE_COUNT, GoalFamily.COVERAGE}
)


def _as_written(target: str) -> str:
    return target


@dataclass(frozen=True)
class CanonicalNames:
    """What the names Goals are written with stand for in one checkout.

    ``selector`` gives the canonical form of a selector on its Target: the
    default Test Variant named and the Default Seed substituted; ``None``
    while the Target or Test does not exist yet. ``target`` gives the
    identity of a Target however it is written (``top`` and ``vlnv#top`` are
    one Target); a Target that does not exist yet stands for itself.
    """

    selector: Callable[[str, TestSelector], TestSelector | None]
    target: Callable[[str], str] = _as_written


def variant_text(selector: TestSelector) -> str:
    """``test`` or ``test+variant``: the selector without its seed form."""
    if selector.variant is None:
        return selector.test
    return f"{selector.test}{VARIANT_SEPARATOR}{selector.variant}"


def selector_text(selector: TestSelector) -> str:
    """The written form of *selector*; :func:`parse_test_selector` reads it back."""
    form = selector.seed_form
    if isinstance(form, PinnedSeed):
        return f"{variant_text(selector)}{SEED_SEPARATOR}{form.seed}"
    if isinstance(form, SeedCount):
        return f"{variant_text(selector)}{SEED_SEPARATOR}x{form.count}"
    return variant_text(selector)


def _seed_form_tag(selector: TestSelector) -> str:
    """``s<seed>``, ``d`` (Default Seed) or ``x`` (a seed count, whatever its N)."""
    form = selector.seed_form
    if isinstance(form, PinnedSeed):
        return f"s{form.seed}"
    return "x" if isinstance(form, SeedCount) else "d"


def simulation_run_key(target: str, selector: TestSelector) -> str:
    """The key of the simulation Goal that names *selector* on *target*.

    ``sim_pass_<target>_run_<hex(test[+variant])>_<s<seed>|d|x>``. The count
    of a multi-seed Goal is not part of the key: changing N changes the Goal,
    not which Goal it is.
    """
    encoded = variant_text(selector).encode("utf-8").hex()
    tag = _seed_form_tag(selector)
    return f"sim_pass_{target}{SIMULATION_RUN_KEY_MARK}{encoded}_{tag}"


def names_test_runs(goal: GoalSpec) -> bool:
    """Whether *goal* is judged by Test Runs of a Target."""
    return goal.family in TEST_RUN_FAMILIES and goal.target is not None


def goal_selectors(goal: GoalSpec) -> tuple[TestSelector, ...] | None:
    """The selectors *goal* names, or ``None`` for every Test of its Target.

    Raises ``ValueError`` for a stored selector the grammar refuses; a Goal
    translated from a validated argument never holds one.
    """
    if goal.family is GoalFamily.COVERAGE:
        tests = goal.params.get("tests")
        if isinstance(tests, str):
            return None
        return tuple(parse_test_selector(str(text)) for text in tests or ())
    test = goal.params.get("test")
    if goal.family is GoalFamily.SIM and test is None:
        return None
    return (parse_test_selector(str(test)),)


def seed_count(goal: GoalSpec) -> int | None:
    """N of a simulation Goal written ``…@xN``; ``None`` for every other Goal."""
    if goal.family is not GoalFamily.SIM:
        return None
    selectors = goal_selectors(goal)
    if not selectors or not isinstance(selectors[0].seed_form, SeedCount):
        return None
    return selectors[0].seed_form.count


def canonical_identity(
    target: str, selector: TestSelector, names: CanonicalNames | None
) -> tuple[str, str, str]:
    """What two selectors share when they name the same Test Runs of one Target.

    ``(Target identity, test[+variant], seed form)`` with the default Test
    Variant named and the Default Seed substituted. Without *names*, or
    while the Target or Test does not exist, the written form stands for
    itself. A pinned seed and a seed count never share an identity.
    """
    resolved = names.selector(target, selector) if names is not None else None
    identity = names.target(target) if names is not None else target
    canonical = selector if resolved is None else resolved
    if isinstance(canonical.seed_form, UnpinnedSeed):
        return identity, variant_text(canonical), "d"
    return identity, variant_text(canonical), _seed_form_tag(canonical)
