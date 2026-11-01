"""Coordinate typed execution units for Target-scoped campaigns."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Generic, TypeVar

from booley.flows.target_criteria import (
    TargetCriterion,
    resolve_campaign_scope,
    resolve_target_criteria,
)
from booley.flows.target_test_suite import (
    TargetTestSuite,
    require_runnable_target_test_suite,
)

_ResultT = TypeVar("_ResultT")


@dataclass(frozen=True)
class CampaignUnit:
    """One physical campaign invocation and the logical tests it represents."""

    test_name: str | None
    display_name: str
    selected_tests: tuple[str, ...] = ()


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
    criteria: tuple[TargetCriterion, ...]
    suite: TargetTestSuite

    @property
    def scope_arg(self) -> str:
        """Return the scope in the existing comma-separated CLI form."""
        return ",".join(self.scope)

    def params_for(self, base_key: str) -> Mapping[str, Any]:
        """Return one metric criterion's parameters, or an empty mapping."""
        for criterion in self.criteria:
            if criterion.base_key == base_key:
                return criterion.params
        return MappingProxyType({})

    def execution_units(
        self,
        *,
        batched: bool = False,
        batches: Sequence[tuple[str, ...]] | None = None,
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
    values: Sequence[_ResultT],
    predicate: Callable[[_ResultT], bool],
) -> bool:
    """Return true only when a non-empty campaign satisfies *predicate*."""
    return bool(values) and all(predicate(value) for value in values)


def resolve_target_campaign(
    target: str,
    criterion_keys: Sequence[str],
    criteria: Mapping[str, object],
    *,
    identity: str,
    explicit_scope: str | Sequence[str] | None = None,
    test_names: Mapping[str, list[str]] | None = None,
    test_skips: Mapping[str, list[str]] | None = None,
    enforce_scope_match: bool = False,
) -> TargetCampaign:
    """Resolve criterion parameters, authorized scope, and runnable tests."""
    resolved_criteria = resolve_target_criteria(target, criterion_keys, criteria)
    scope = resolve_campaign_scope(
        explicit_scope,
        resolved_criteria,
        enforce_match=enforce_scope_match,
    )
    suite = require_runnable_target_test_suite(
        target,
        identity=identity,
        test_names=test_names,
        test_skips=test_skips,
    )
    return TargetCampaign(target, scope, resolved_criteria, suite)


def describe_target_campaign(
    target: str,
    *,
    identity: str,
    criterion_keys: Sequence[str] = (),
    criteria: Mapping[str, object] | None = None,
    test_names: Mapping[str, list[str]] | None = None,
    test_skips: Mapping[str, list[str]] | None = None,
) -> TargetCampaign:
    """Describe test mechanics when criterion scope is not needed by a caller."""
    suite = require_runnable_target_test_suite(
        target,
        identity=identity,
        test_names=test_names,
        test_skips=test_skips,
    )
    resolved = resolve_target_criteria(target, criterion_keys, criteria or {})
    return TargetCampaign(target, (), resolved, suite)
