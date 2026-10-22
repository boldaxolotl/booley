"""Read-only all-Target preparation for an explicit Coverage Campaign invocation."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType

from booley.config.project_config import lookup_target_section
from booley.flows.test_runs import TestRun
from booley.runtime.timefmt import utc_now_rfc3339
from booley.targets.catalog import PreparedTargetSelection, TargetCatalog
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
from .coverage_source_gaps import rtl_module_candidates
from .coverage_waivers import CoverageRepositoryRoots, CoverageWaiverConfig
from .verilator_coverage import CoverageCollectionRequest, coverage_request_finding
from .verilator_coverage_execution import prepare_coverage_collection


@dataclass(frozen=True)
class CoverageInvocationRequest:
    """Explicit coverage selection; absent test names mean the registered suite minus skips.

    ``test_runs`` holds the concrete Test Runs already resolved for a Target
    (ADR 0072), keyed by Target selector; they replace ``tests`` for that
    Target, and several may be runs of one Test. A Target without an entry
    runs each selected Test as its default Test Variant at the Default Seed.
    """

    targets: tuple[str, ...]
    tests: tuple[str, ...] | None = None
    test_runs: Mapping[str, tuple[TestRun, ...]] = field(default_factory=dict)
    trace: bool = False
    # Raw coverage: evaluate with no approved waivers.
    no_waivers: bool = False
    # The caller records no Criteria (a non-acceptance run).
    diagnostic: bool = False


@dataclass(frozen=True)
class CoverageProjectContext:
    """Explicit repository roots and snapshotted registered test declarations."""

    rtl_repository: Path
    project_data_repository: Path
    test_names: Mapping[str, tuple[str, ...]]
    criteria: Mapping[str, CoverageCriterion] = field(default_factory=dict)
    waiver_config: CoverageWaiverConfig | None = None

    skipped_tests: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    criterion_names: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "test_names", immutable_mapping(self.test_names))
        object.__setattr__(self, "criteria", MappingProxyType(dict(self.criteria)))
        object.__setattr__(self, "criterion_names", MappingProxyType(dict(self.criterion_names)))
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
    #: Every Test Run the campaign collects, grouped in ``selected_tests`` order.
    test_runs: tuple[TestRun, ...] = ()
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
    #: Sorted module-bearing RTL paths from the Preflight lexical scan.
    rtl_candidates: tuple[str, ...] = ()


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
    handle: TargetHandle,
    request: CoverageInvocationRequest,
    context: CoverageProjectContext,
    runs: tuple[TestRun, ...] | None = None,
):
    declared = tuple(lookup_target_section(context.test_names, handle.identity) or ())
    matched = [
        (key, policy)
        for key, policy in context.criteria.items()
        if policy.target == handle.identity
    ]
    if len(matched) > 1:
        raise ValueError("Target binds more than one Coverage Criterion")
    key, criterion = matched[0] if matched else ("", None)
    required = criterion.tests if criterion and criterion.tests is not None else declared
    skipped = set(lookup_target_section(context.skipped_tests, handle.identity) or ())
    # Selection never consults the Coverage Criterion: its test list is required
    # evidence, compared with what ran by evaluate_coverage_campaign (suite mismatch).
    if runs is not None:
        selected = tuple(dict.fromkeys(str(run.test) for run in runs))
    elif request.tests is not None:
        selected = request.tests
    else:
        selected = tuple(name for name in declared if name not in skipped)
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


def _collection_recipe(
    handle: TargetHandle, inspection, closure, trace: bool
) -> dict[str, object]:
    """What decides a Target's coverage collection, for its recipe fingerprint."""
    return {
        "identity": handle.identity,
        "options": inspection.flow_options,
        "parameters": inspection.parameters,
        "sources": closure,
        "trace": trace,
    }


def _prepare_target(
    handle: TargetHandle,
    catalog: TargetCatalog,
    request: CoverageInvocationRequest,
    context: CoverageProjectContext,
):
    runs = request.test_runs.get(handle.selector)
    try:
        declared, selected, key, criterion = _suite(handle, request, context, runs)
    except ValueError as exc:
        return None, _finding("COV_SUITE_INVALID", f"{handle.selector}: {exc}")
    if request.no_waivers and criterion is not None and not request.diagnostic:
        return None, _finding(
            "COV_NO_WAIVERS_CRITERION",
            f"{handle.selector}: --no-waivers skips the approved waivers that "
            f"{context.criterion_names.get(key, 'Coverage Criterion ' + key)} is evaluated with; "
            "add --diagnostic for a "
            "non-acceptance run with raw numbers, or drop --no-waivers",
            "/no_waivers",
        )
    inspection = catalog.inspect(handle)
    collection = prepare_coverage_collection(
        handle,
        selected_tests=selected,
        artifact_root=Path(),
        trace=request.trace,
        # Grouped by Test in ``selected`` order; a Test's runs keep request order.
        test_runs=None if runs is None else tuple(sorted(runs, key=lambda run: str(run.test))),
    )
    finding = coverage_request_finding(collection)
    if finding is not None:
        return None, finding
    closure = coverage_source_closure(inspection)
    recipe = _collection_recipe(handle, inspection, closure, request.trace)
    plan = CoverageTargetPlan(
        handle,
        declared,
        selected,
        request.trace,
        test_runs=tuple(item.run for item in collection.selected_tests if item.run is not None),
        criterion=criterion,
        criterion_key=key,
        waiver_config=None if request.no_waivers else context.waiver_config,
        roots=CoverageRepositoryRoots(context.rtl_repository, context.project_data_repository),
        known_targets=tuple(DurableTargetIdentity(item.identity) for item in catalog.list()),
        collection_request=collection,
        source_closure=freeze_coverage_mapping(closure),
        rtl_candidates=rtl_module_candidates(inspection),
        recipe_fingerprint=coverage_digest(recipe),
        target_fingerprint=coverage_digest(
            {"core": handle.core_file.read_text(), "identity": handle.identity}
        ),
    )
    return plan, None


def prepare_coverage_invocation(
    request: CoverageInvocationRequest,
    project_context: CoverageProjectContext,
    *,
    selection: PreparedTargetSelection | None = None,
) -> CoveragePreflightResult:
    """Resolve the complete invocation without EDA or artifact/build mutations."""
    if selection is None:
        try:
            catalog = TargetCatalog.build(project_context.rtl_repository)
        except (FuseSocError, ValueError, OSError) as exc:
            return CoveragePreflightResult(None, (_finding("COV_TARGET_INVALID", str(exc)),))
        handles = None
    else:
        catalog = selection.catalog
        handles = selection.handles
    targets, findings, seen = [], [], set()
    selections = (
        tuple((handle.selector, handle) for handle in handles)
        if handles is not None
        else tuple((token, None) for token in request.targets)
    )
    if not selections:
        findings.append(_finding("COV_TARGET_INVALID", "Select at least one Target"))
    for token, prepared_handle in selections:
        try:
            handle = prepared_handle or catalog.select(token, for_flow="sim")
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
        None if findings else CoverageInvocationPlan(tuple(targets)),
        tuple(findings),
    )
