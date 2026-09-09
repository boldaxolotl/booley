"""Read-only all-Target preparation for an explicit Coverage Campaign invocation."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType

from booley.runtime.timefmt import utc_now_rfc3339
from booley.targets.catalog import TargetCatalog
from booley.targets.domain import FuseSocError, TargetHandle, immutable_mapping

from .coverage_acceptance import CoverageAcceptance
from .coverage_campaign import (
    CoverageFinding,
    DurableTargetIdentity,
    FrozenJson,
    freeze_coverage_mapping,
)
from .coverage_policy import CoverageCriterion
from .coverage_provenance import coverage_digest, coverage_source_closure
from .coverage_waivers import CoverageRepositoryRoots, CoverageWaiverConfig
from .verilator_coverage import CoverageCollectionRequest, coverage_request_finding
from .verilator_coverage_execution import prepare_coverage_collection


@dataclass(frozen=True)
class CoverageInvocationRequest:
    """Explicit coverage selection; absent test selection means policy then suite."""

    targets: tuple[str, ...]
    tests: tuple[str, ...] | None = None
    trace: bool = False
    test_filter: str | None = None
    skip: tuple[str, ...] = ()


@dataclass(frozen=True)
class CoverageProjectContext:
    """Explicit repository roots and snapshotted registered test declarations."""

    rtl_repository: Path
    project_data_repository: Path
    test_names: Mapping[str, tuple[str, ...]]
    criteria: Mapping[str, CoverageCriterion] = field(default_factory=dict)
    waiver_config: CoverageWaiverConfig | None = None

    skipped_tests: Mapping[str, tuple[str, ...]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "test_names", immutable_mapping(self.test_names))
        object.__setattr__(self, "criteria", MappingProxyType(dict(self.criteria)))
        object.__setattr__(
            self,
            "skipped_tests",
            MappingProxyType({key: tuple(value) for key, value in self.skipped_tests.items()}),
        )


@dataclass(frozen=True)
class CoverageTargetPlan:
    """One resolved Target, before invocation paths or execution are allocated."""

    handle: TargetHandle
    declared_tests: tuple[str, ...]
    selected_tests: tuple[str, ...]
    trace: bool
    criterion: CoverageCriterion | None = None
    criterion_key: str = ""
    collection_request: CoverageCollectionRequest | None = None
    source_closure: Mapping[str, FrozenJson] = field(default_factory=dict)
    recipe_fingerprint: str = ""
    target_fingerprint: str = ""
    acceptance: CoverageAcceptance | None = None
    invocation_dir: Path | None = None
    started_at: str = field(default_factory=utc_now_rfc3339)
    waiver_config: CoverageWaiverConfig | None = None
    roots: CoverageRepositoryRoots | None = None
    known_targets: tuple[DurableTargetIdentity, ...] = ()


@dataclass(frozen=True)
class CoverageInvocationPlan:
    targets: tuple[CoverageTargetPlan, ...]


@dataclass(frozen=True)
class CoveragePreflightResult:
    plan: CoverageInvocationPlan | None
    findings: tuple[CoverageFinding, ...] = ()


def _finding(code: str, message: str, pointer: str = "/targets") -> CoverageFinding:
    return CoverageFinding("error", code, pointer, message)


def _suite(
    handle: TargetHandle, request: CoverageInvocationRequest, context: CoverageProjectContext
):
    declared = tuple(
        context.test_names.get(handle.selector, context.test_names.get(handle.name, ()))
    )
    matched = [
        (key, policy)
        for key, policy in context.criteria.items()
        if policy.target == handle.identity
    ]
    if len(matched) > 1:
        raise ValueError("Target binds more than one Coverage Criterion")
    key, criterion = matched[0] if matched else ("", None)
    required = criterion.tests if criterion and criterion.tests is not None else declared
    selected = request.tests if request.tests is not None else required
    if request.test_filter is not None:
        selected = tuple(name for name in declared if request.test_filter in name)
    skipped = set(
        context.skipped_tests.get(handle.selector, context.skipped_tests.get(handle.name, ()))
    ) | set(request.skip)
    selected = tuple(name for name in selected if name not in skipped)
    if criterion is not None:
        metrics = [threshold.metric for threshold in criterion.thresholds]
        if (
            not metrics
            or len(set(metrics)) != len(metrics)
            or set(metrics) - {"line", "branch", "expression", "toggle", "cover_property"}
        ):
            raise ValueError("Coverage Criterion must name unique V1 metrics")
        if any(
            isinstance(t.minimum_percent, bool) or not 0 < t.minimum_percent <= 100
            for t in criterion.thresholds
        ):
            raise ValueError("Coverage thresholds must be finite numbers in (0, 100]")
    for names in (declared, required, selected):
        if not names or any(not isinstance(name, str) or not name.strip() for name in names):
            raise ValueError("Coverage requires an exact non-empty registered test suite")
        if len(set(names)) != len(names) or set(names) - set(declared):
            raise ValueError("Coverage suite contains duplicate or unregistered tests")
    return tuple(sorted(declared)), tuple(sorted(selected)), key, criterion


def _prepare_target(
    handle: TargetHandle,
    catalog: TargetCatalog,
    request: CoverageInvocationRequest,
    context: CoverageProjectContext,
):
    try:
        declared, selected, key, criterion = _suite(handle, request, context)
    except ValueError as exc:
        return None, _finding("COV_SUITE_INVALID", f"{handle.selector}: {exc}")
    inspection = catalog.inspect(handle)
    collection = prepare_coverage_collection(
        handle, selected_tests=selected, artifact_root=Path(), trace=request.trace
    )
    finding = coverage_request_finding(collection)
    if finding is not None:
        return None, finding
    closure = coverage_source_closure(inspection)
    recipe = {
        "identity": handle.identity,
        "options": inspection.flow_options,
        "parameters": inspection.parameters,
        "sources": closure,
        "trace": request.trace,
    }
    plan = CoverageTargetPlan(
        handle,
        declared,
        selected,
        request.trace,
        criterion=criterion,
        criterion_key=key,
        waiver_config=context.waiver_config,
        roots=CoverageRepositoryRoots(context.rtl_repository, context.project_data_repository),
        known_targets=tuple(DurableTargetIdentity(item.identity) for item in catalog.list()),
        collection_request=collection,
        source_closure=freeze_coverage_mapping(closure),
        recipe_fingerprint=coverage_digest(recipe),
        target_fingerprint=coverage_digest(
            {"core": handle.core_file.read_text(), "identity": handle.identity}
        ),
    )
    return plan, None


def prepare_coverage_invocation(
    request: CoverageInvocationRequest,
    project_context: CoverageProjectContext,
) -> CoveragePreflightResult:
    """Resolve the complete invocation without EDA or artifact/build mutations."""
    try:
        catalog = TargetCatalog.build(project_context.rtl_repository)
    except (FuseSocError, ValueError, OSError) as exc:
        return CoveragePreflightResult(None, (_finding("COV_TARGET_INVALID", str(exc)),))
    targets, findings, seen = [], [], set()
    if not request.targets:
        findings.append(_finding("COV_TARGET_INVALID", "Select at least one Target"))
    for token in sorted(request.targets):
        try:
            handle = catalog.select(token, for_flow="sim")
            if handle.eda_tool != "verilator":
                findings.append(
                    _finding("COV_TOOL_UNSUPPORTED", f"{token}: coverage requires Verilator")
                )
                continue
            if handle.identity in seen:
                findings.append(
                    _finding("COV_TARGET_DUPLICATE", f"{token}: Target selected twice")
                )
                continue
            seen.add(handle.identity)
            plan, finding = _prepare_target(handle, catalog, request, project_context)
            if finding is not None:
                findings.append(finding)
            else:
                targets.append(plan)
        except (FuseSocError, ValueError, OSError) as exc:
            findings.append(_finding("COV_TARGET_INVALID", f"{token}: {exc}"))
    return CoveragePreflightResult(
        None
        if findings
        else CoverageInvocationPlan(tuple(sorted(targets, key=lambda p: p.handle.identity))),
        tuple(findings),
    )
