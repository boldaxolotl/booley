"""Coordinate typed execution units for Target-scoped campaigns."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Generic, TypeVar

from booley.flows.target_test_suite import TargetTestSuite, require_runnable_target_test_suite

_ResultT = TypeVar("_ResultT")


@dataclass(frozen=True)
class CampaignUnit:
    """One physical campaign invocation and the logical tests it represents."""

    test_name: str | None
    display_name: str
    selected_tests: tuple[str, ...] = ()

    @property
    def tests(self) -> tuple[str, ...]:
        """The tests this unit runs: the selected ones of a batched unit, else its own name."""
        return self.selected_tests or (self.display_name,)


@dataclass(frozen=True)
class CampaignUnitResult(Generic[_ResultT]):
    """The typed association between a campaign unit and its result."""

    unit: CampaignUnit
    value: _ResultT


@dataclass(frozen=True)
class CampaignResults(Generic[_ResultT]):
    """Typed results in campaign execution order."""

    units: tuple[CampaignUnitResult[_ResultT], ...]

    @property
    def values(self) -> tuple[_ResultT, ...]:
        """Return result values in campaign execution order."""
        return tuple(item.value for item in self.units)


@dataclass(frozen=True)
class TargetCampaign:
    """Resolved shared mechanics for one Target-scoped campaign."""

    target: str
    scope: tuple[str, ...]
    suite: TargetTestSuite

    @property
    def scope_arg(self) -> str:
        """Return the scope in the existing comma-separated CLI form."""
        return ",".join(self.scope)

    def execution_units(
        self, *, batched: bool = False, batches: Sequence[tuple[str, ...]] | None = None
    ) -> tuple[CampaignUnit, ...]:
        """Describe physical invocations for individual or batched runners.

        A batched runner launches one process per entry of *batches* (the
        Tests each process runs); without *batches* it launches one process
        for the whole suite.
        """
        if not batched:
            return tuple(
                CampaignUnit(test, test if test is not None else "<default>")
                for test in self.suite.tests
            )
        if batches is None:
            batches = (tuple(test for test in self.suite.tests if test is not None),)
        if len(batches) == 1:
            return (CampaignUnit(None, "<cocotb-suite>", batches[0]),)
        return tuple(
            CampaignUnit(None, f"<cocotb-suite {index}/{len(batches)}>", batch)
            for index, batch in enumerate(batches, start=1)
        )

    def execute(
        self,
        behavior: Callable[[CampaignUnit], _ResultT],
        *,
        batched: bool = False,
        batches: Sequence[tuple[str, ...]] | None = None,
    ) -> CampaignResults[_ResultT]:
        """Execute campaign units through injected metric-specific behavior."""
        results = tuple(
            CampaignUnitResult(unit, behavior(unit))
            for unit in self.execution_units(batched=batched, batches=batches)
        )
        return CampaignResults(results)


def all_campaign_results_match(
    values: Sequence[_ResultT], predicate: Callable[[_ResultT], bool]
) -> bool:
    """Return true only when a non-empty campaign satisfies *predicate*."""
    return bool(values) and all(predicate(value) for value in values)


def _scope_paths(scope: str) -> tuple[str, ...]:
    """Split a comma-separated scope into its non-blank, stripped entries."""
    return tuple(value.strip() for value in scope.split(",") if value.strip())


def resolve_target_campaign(
    target: str,
    scope: str,
    *,
    identity: str,
    test_names: Mapping[str, list[str]] | None = None,
    test_skips: Mapping[str, list[str]] | None = None,
) -> TargetCampaign:
    """Resolve the scope entries and the runnable tests of *target*."""
    suite = require_runnable_target_test_suite(
        target, identity=identity, test_names=test_names, test_skips=test_skips
    )
    return TargetCampaign(target, _scope_paths(scope), suite)


def describe_target_campaign(
    target: str,
    *,
    identity: str,
    test_names: Mapping[str, list[str]] | None = None,
    test_skips: Mapping[str, list[str]] | None = None,
) -> TargetCampaign:
    """Describe test mechanics when a caller needs no scope."""
    suite = require_runnable_target_test_suite(
        target, identity=identity, test_names=test_names, test_skips=test_skips
    )
    return TargetCampaign(target, (), suite)
