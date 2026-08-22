"""Typed mechanics shared by Target-scoped verification campaigns.

Coverage and mutation retain their metric-specific execution and scoring
policy.  This module owns the common campaign contract: criterion parameters,
authorized RTL scope, runnable tests, execution units, non-vacuous aggregation,
and freshness evidence.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Generic, TypeVar

from booley.config.project_config import lookup_target_section
from booley.flows.source_fingerprint import compute_source_fingerprint

_ResultT = TypeVar("_ResultT")


class CampaignScopeError(ValueError):
    """The campaign has no unambiguous authorized RTL scope."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class NoRunnableTestsError(ValueError):
    """A Target's durable skip policy excludes every declared test."""

    target: str
    skipped: tuple[str, ...]

    def __init__(self, target: str, skipped: tuple[str, ...]) -> None:
        self.target = target
        self.skipped = skipped
        super().__init__(
            f"Target {target!r} has no runnable tests; "
            f"every declared test is skipped: {', '.join(skipped)}"
        )


@dataclass(frozen=True)
class TargetCriterion:
    """One Target-specific criterion and its immutable parameter view."""

    base_key: str
    key: str
    params: Mapping[str, Any]


@dataclass(frozen=True)
class TargetTestSuite:
    """Tests one Target will run and durable skips excluded from the suite."""

    tests: tuple[str | None, ...]
    skipped: tuple[str, ...] = ()

    @property
    def display_names(self) -> tuple[str, ...]:
        """Human-readable names, including the native default invocation."""
        return tuple(test if test is not None else "<default>" for test in self.tests)

    @property
    def all_skipped(self) -> bool:
        """Whether the Target declared tests but excluded every one of them."""
        return not self.tests and bool(self.skipped)


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
    """Results with aggregation helpers that cannot pass an empty campaign."""

    units: tuple[CampaignUnitResult[_ResultT], ...]

    @property
    def values(self) -> tuple[_ResultT, ...]:
        """Return result values in campaign execution order."""
        return tuple(item.value for item in self.units)

    def all_match(self, predicate: Callable[[_ResultT], bool]) -> bool:
        """Return true only when at least one result exists and all match."""
        return bool(self.units) and all(predicate(item.value) for item in self.units)

    def any_match(self, predicate: Callable[[_ResultT], bool]) -> bool:
        """Return true when at least one result matches *predicate*."""
        return any(predicate(item.value) for item in self.units)


@dataclass(frozen=True)
class CampaignFreshness:
    """Serializable freshness evidence for one Target campaign."""

    target: str | None
    categories: tuple[str, ...]
    fingerprint: Mapping[str, Any]

    def to_detail(self) -> dict[str, Any]:
        """Return the established criterion-detail representation."""
        return {
            "categories": list(self.categories),
            "fingerprint": dict(self.fingerprint),
            "target": self.target,
        }


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

    def execution_units(self, *, batched: bool = False) -> tuple[CampaignUnit, ...]:
        """Describe physical invocations for individual or batched runners."""
        if batched:
            selected = tuple(test for test in self.suite.tests if test is not None)
            return (CampaignUnit(None, "<cocotb-suite>", selected),)
        return tuple(
            CampaignUnit(test, test if test is not None else "<default>")
            for test in self.suite.tests
        )

    def execute(
        self,
        behavior: Callable[[CampaignUnit], _ResultT],
        *,
        batched: bool = False,
    ) -> CampaignResults[_ResultT]:
        """Execute campaign units through injected metric-specific behavior."""
        results = tuple(
            CampaignUnitResult(unit, behavior(unit))
            for unit in self.execution_units(batched=batched)
        )
        return CampaignResults(results)

    def freshness(self, work_dir: Path, categories: Sequence[str]) -> CampaignFreshness:
        """Build source and recipe freshness evidence for this campaign."""
        fingerprint = compute_source_fingerprint(work_dir, target=self.target)
        return CampaignFreshness(
            target=self.target,
            categories=tuple(sorted(set(categories))),
            fingerprint=fingerprint,
        )


def configured_test_names() -> Mapping[str, list[str]]:
    """Return the tests.toml test registry, or an empty registry if unavailable."""
    try:
        from booley.config.project_config import TEST_NAMES

        return TEST_NAMES
    except ImportError:
        return {}


def configured_test_skips() -> Mapping[str, list[str]]:
    """Return durable tests.toml skips, or an empty registry if unavailable."""
    try:
        from booley.config.project_config import TEST_SKIP

        return TEST_SKIP
    except ImportError:
        return {}


def resolve_target_test_suite(
    target: str,
    *,
    test_names: Mapping[str, list[str]] | None = None,
    test_skips: Mapping[str, list[str]] | None = None,
) -> TargetTestSuite:
    """Resolve runnable tests and preserve the complete durable skip set."""
    names = configured_test_names() if test_names is None else test_names
    skips_by_target = configured_test_skips() if test_skips is None else test_skips
    available = list(lookup_target_section(names, target) or [])
    if not available:
        return TargetTestSuite((None,))

    durable_skips = set(lookup_target_section(skips_by_target, target) or [])
    runnable = [test for test in available if test not in durable_skips]
    if not runnable:
        return TargetTestSuite((), tuple(available))
    skipped = tuple(test for test in available if test in durable_skips)
    return TargetTestSuite(tuple(runnable), skipped)


def require_runnable_target_test_suite(
    target: str,
    *,
    test_names: Mapping[str, list[str]] | None = None,
    test_skips: Mapping[str, list[str]] | None = None,
) -> TargetTestSuite:
    """Resolve *target* and reject an all-skipped, vacuous campaign."""
    suite = resolve_target_test_suite(
        target,
        test_names=test_names,
        test_skips=test_skips,
    )
    if suite.all_skipped:
        raise NoRunnableTestsError(target, suite.skipped)
    return suite


def resolve_target_campaign(
    target: str,
    criterion_keys: Sequence[str],
    criteria: Mapping[str, object],
    *,
    explicit_scope: str | Sequence[str] | None = None,
    test_names: Mapping[str, list[str]] | None = None,
    test_skips: Mapping[str, list[str]] | None = None,
) -> TargetCampaign:
    """Resolve criterion parameters, authorized scope, and runnable tests."""
    resolved_criteria = _resolve_criteria(target, criterion_keys, criteria)
    scope = _resolve_scope(explicit_scope, resolved_criteria)
    suite = require_runnable_target_test_suite(
        target,
        test_names=test_names,
        test_skips=test_skips,
    )
    return TargetCampaign(target, scope, resolved_criteria, suite)


def describe_target_campaign(
    target: str,
    *,
    criterion_keys: Sequence[str] = (),
    criteria: Mapping[str, object] | None = None,
    test_names: Mapping[str, list[str]] | None = None,
    test_skips: Mapping[str, list[str]] | None = None,
) -> TargetCampaign:
    """Describe test mechanics when criterion scope is not needed by a caller."""
    suite = require_runnable_target_test_suite(
        target,
        test_names=test_names,
        test_skips=test_skips,
    )
    resolved = _resolve_criteria(target, criterion_keys, criteria or {})
    return TargetCampaign(target, (), resolved, suite)


def build_campaign_freshness(
    work_dir: Path,
    *,
    target: str | None,
    categories: Sequence[str],
) -> CampaignFreshness:
    """Build freshness evidence for Targeted and project-wide criteria."""
    fingerprint = compute_source_fingerprint(work_dir, target=target)
    return CampaignFreshness(
        target=target,
        categories=tuple(sorted(set(categories))),
        fingerprint=fingerprint,
    )


def _resolve_criteria(
    target: str,
    criterion_keys: Sequence[str],
    criteria: Mapping[str, object],
) -> tuple[TargetCriterion, ...]:
    resolved: list[TargetCriterion] = []
    for base_key in criterion_keys:
        key = f"{base_key}_{target}"
        if key not in criteria:
            continue
        params = _entry_params(criteria[key])
        resolved.append(TargetCriterion(base_key, key, MappingProxyType(dict(params))))
    return tuple(resolved)


def _entry_params(entry: object) -> Mapping[str, Any]:
    params = getattr(entry, "params", None)
    if isinstance(params, Mapping):
        return params
    if isinstance(entry, Mapping):
        nested = entry.get("params")
        if isinstance(nested, Mapping):
            return nested
    return {}


def _resolve_scope(
    explicit_scope: str | Sequence[str] | None,
    criteria: Sequence[TargetCriterion],
) -> tuple[str, ...]:
    explicit = _scope_paths(explicit_scope)
    if explicit:
        return explicit
    scopes = {_scope_paths(criterion.params.get("scope")) for criterion in criteria}
    scopes.discard(())
    if len(scopes) == 1:
        return next(iter(scopes))
    reason = "missing" if not scopes else "conflicting"
    raise CampaignScopeError(reason)


def _scope_paths(raw_scope: object) -> tuple[str, ...]:
    if isinstance(raw_scope, str):
        values = raw_scope.split(",")
    elif isinstance(raw_scope, Sequence) and not isinstance(raw_scope, (bytes, bytearray)):
        values = [value for value in raw_scope if isinstance(value, str)]
    else:
        return ()
    return tuple(value.strip() for value in values if value.strip())
