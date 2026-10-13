"""The Test Run plans of one checkout's Targets, as Goal Mode reads them (ADR 0072).

Goal Mode freezes a Goal's Test Runs, and later checks them, against the
plan the Simulation Flow would build for the Goal's Target:
:func:`booley.flows.target_test_plan_loading.load_checkout_target_test_plan`.
:class:`TargetPlans` loads each Target's plan at most once and tells apart a
Target that does not exist yet (the Goal waits for it, ADR 0067) from one
whose plan cannot be built (an error the caller reports).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from booley.config.tests_toml_rules import UnknownVariantError
from booley.flows import target_test_plan_loading
from booley.flows.target_test_plan import TargetTestPlan, UnknownTestError
from booley.flows.test_runs import PinnedSeed, TestSelector, UnpinnedSeed
from booley.goals.test_run_goals import CanonicalNames
from booley.targets.domain import FuseSocError, UnknownTargetError

PlanLoader = Callable[[Path, str], TargetTestPlan]


class TargetPlanError(ValueError):
    """A Target exists but its Test Run plan cannot be built."""


def load_plan(work_dir: Path, target: str) -> TargetTestPlan:
    """The default :data:`PlanLoader`: the plan the Simulation Flow would build."""
    return target_test_plan_loading.load_checkout_target_test_plan(work_dir, target)


def load_identity(work_dir: Path, target: str) -> str:
    """The default Target identity: the one the Target catalog of the checkout gives."""
    return target_test_plan_loading.checkout_target_identity(work_dir, target)


@dataclass
class TargetPlans:
    """The Test Run plans of the checkout at ``work_dir``, each loaded once."""

    work_dir: Path
    loader: PlanLoader = load_plan
    identities: Callable[[Path, str], str] = load_identity
    _identities: dict[str, str] = field(default_factory=dict[str, str])
    _plans: dict[str, TargetTestPlan | None] = field(
        default_factory=dict[str, TargetTestPlan | None]
    )

    def plan(self, target: str) -> TargetTestPlan | None:
        """*target*'s plan, or ``None`` while the Target does not exist.

        Raises :class:`TargetPlanError` when the Target exists but cannot be
        inspected, or its ``tests.toml`` section cannot be applied to it.
        """
        if target not in self._plans:
            try:
                self._plans[target] = self.loader(self.work_dir, target)
            except UnknownTargetError:
                self._plans[target] = None
            except (FuseSocError, ValueError, OSError) as exc:
                raise TargetPlanError(
                    f"the Test Runs of Target {target!r} cannot be resolved: {exc}"
                ) from exc
        return self._plans[target]

    def identity(self, target: str) -> str:
        """The identity of *target* however it is written; itself while it does not exist.

        Raises :class:`TargetPlanError` when the Target catalog cannot be read.
        """
        if target not in self._identities:
            try:
                self._identities[target] = self.identities(self.work_dir, target)
            except UnknownTargetError:
                self._identities[target] = target
            except (FuseSocError, ValueError, OSError) as exc:
                raise TargetPlanError(f"Target {target!r} cannot be identified: {exc}") from exc
        return self._identities[target]

    def resolver(self) -> CanonicalNames:
        """Canonical names for :func:`booley.goals.translate.translate_goals`."""

        def resolve(target: str, selector: TestSelector) -> TestSelector | None:
            plan = self.plan(target)
            if plan is None:
                return None
            try:
                base = plan.run(selector.test, selector.variant)
            except (UnknownTestError, UnknownVariantError):
                return None
            form = selector.seed_form
            if isinstance(form, UnpinnedSeed) and base.seed is not None:
                form = PinnedSeed(base.seed)
            return TestSelector(selector.test, base.variant, form)

        return CanonicalNames(resolve, self.identity)
