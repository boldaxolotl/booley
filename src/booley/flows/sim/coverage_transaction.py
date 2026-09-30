"""One Target's ordered collection, evaluation and durable Campaign publication."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Protocol, cast

from booley.flows.execution_persistence import AcceptanceRecordingError
from booley.runtime.timefmt import utc_now_rfc3339

from .campaign_reports import target_report_directory, write_campaign_json
from .coverage_campaign import (
    CoverageCampaign,
    CoverageCampaignValidationError,
    CoverageCollector,
    CoverageTarget,
    FrozenJson,
    derive_coverage_rollups,
    freeze_coverage_mapping,
)
from .coverage_campaign_store import POINT_STORE_NAME, publish_coverage_campaign
from .coverage_invocation import CoverageTargetPlan
from .coverage_policy import evaluate_coverage_campaign
from .coverage_provenance import coverage_digest, validate_coverage_sources
from .coverage_source_gaps import source_gap_findings
from .coverage_waivers import CoverageWaiverValidationError, load_approved_waiver_set
from .execution.contract import PreSimStatus, pre_sim_failure_message
from .verilator_coverage import CoverageCollectionResult, SimulationExecutionPort, collect


@dataclass(frozen=True)
class CoverageTargetOutcome:
    target: str
    exit_code: int
    campaign_path: Path
    simulation_path: Path
    detail: Mapping[str, FrozenJson]
    abort_remaining: bool = False


class CoverageProgressSink(Protocol):
    """Observational checkpoint emitted only after a Target transaction finishes."""

    def completed(self, outcome: CoverageTargetOutcome) -> None: ...


def _campaign(plan: CoverageTargetPlan, result: CoverageCollectionResult) -> CoverageCampaign:
    assert plan.invocation_dir is not None
    build = _build_metadata(plan, result)
    incompatible = result.native_format.compatibility == "incompatible"
    points = () if incompatible else result.points
    scoreable = result.status == "complete" and not incompatible
    return CoverageCampaign(
        campaign_id=f"campaign:{plan.started_at}:{plan.invocation_dir.name}:{plan.handle.identity}",
        invocation=freeze_coverage_mapping(
            {"id": int(plan.invocation_dir.name), "started_at": plan.started_at}
        ),
        target=CoverageTarget(plan.handle.identity, plan.handle.selector),
        collector=_collector(result),
        build=build,
        coverage_window=freeze_coverage_mapping(
            {
                "mode": result.coverage_window.mode,
                "hook_evidence_artifacts": result.coverage_window.hook_artifacts,
            }
        ),
        fingerprints=_fingerprints(plan, build),
        source_closure=plan.source_closure,
        declared_tests=plan.declared_tests,
        selected_tests=plan.selected_tests,
        runs=result.runs,
        artifacts=result.artifacts,
        normalization=_normalization(result),
        points=points,
        rollups=derive_coverage_rollups(points) if scoreable else (),
        collection=_collection_metadata(result),
        findings=(
            *result.findings,
            *(
                source_gap_findings(
                    result.build.declarations,
                    plan.source_closure,
                    result.native_sources,
                    result.native_source_diagnostics,
                )
                if scoreable and result.native_format.compatibility == "compatible"
                else ()
            ),
        ),
        evaluation=_ungated_evaluation(),
    )


def _build_metadata(
    plan: CoverageTargetPlan, result: CoverageCollectionResult
) -> Mapping[str, FrozenJson]:
    return freeze_coverage_mapping(
        {
            "recipe_fingerprint": plan.recipe_fingerprint,
            "instrumentation": result.build.instrumentation,
            "trace": plan.trace,
            "coverage": True,
            "verilator": {"tag": result.collector.tag, "commit": result.collector.commit},
        }
    )


def _fingerprints(
    plan: CoverageTargetPlan, build: Mapping[str, FrozenJson]
) -> Mapping[str, FrozenJson]:
    return freeze_coverage_mapping(
        {
            "target_definition": plan.target_fingerprint,
            "rtl_sources": coverage_digest(plan.source_closure["rtl"]),
            "testbench_sources": coverage_digest(plan.source_closure["testbench"]),
            "test_declarations": coverage_digest(plan.declared_tests),
            "instrumented_build": coverage_digest(build),
        }
    )


def _collector(result: CoverageCollectionResult) -> CoverageCollector:
    return CoverageCollector(
        "verilator",
        freeze_coverage_mapping({"tag": result.collector.tag, "commit": result.collector.commit}),
        freeze_coverage_mapping(
            {
                "name": result.native_format.name,
                "compatibility": result.native_format.compatibility,
            }
        ),
        result.capabilities,
    )


def _normalization(result: CoverageCollectionResult) -> Mapping[str, FrozenJson]:
    status = "complete" if result.status == "complete" else "partial"
    if result.native_format.compatibility == "incompatible":
        status = "incompatible"
    elif status == "complete" and result.unrecognized_records:
        status = "complete_with_unknown_records"
    return freeze_coverage_mapping(
        {"status": status, "unrecognized_records": result.unrecognized_records}
    )


def run_coverage_target(
    plan: CoverageTargetPlan,
    execution: SimulationExecutionPort,
    progress: CoverageProgressSink,
    *,
    embedded_root: Path | None = None,
    publication_checkpoint: Callable[[str], None] | None = None,
) -> CoverageTargetOutcome:
    """Publish Campaign, Simulation, acceptance and state, then checkpoint progress."""
    assert plan.invocation_dir is not None and plan.collection_request is not None
    root = embedded_root or target_report_directory(plan.invocation_dir, plan.handle.selector)
    outcome = _execute_coverage_transaction(
        plan,
        execution,
        root,
        embedded=embedded_root is not None,
        publication_checkpoint=publication_checkpoint,
    )
    return _checkpoint_progress(progress, outcome)


def _execute_coverage_transaction(
    plan: CoverageTargetPlan,
    execution: SimulationExecutionPort,
    root: Path,
    *,
    embedded: bool,
    publication_checkpoint: Callable[[str], None] | None,
) -> CoverageTargetOutcome:
    result = None
    campaign = None
    campaign_published = False

    def record_campaign_publication() -> None:
        nonlocal campaign_published
        campaign_published = True

    try:
        validate_coverage_sources(plan)
        _start_target(root)
        result = collect(replace(plan.collection_request, artifact_root=root), execution)
        validate_coverage_sources(plan)
        from .coverage_declaration_artifact import publish_declaration_evidence

        result = publish_declaration_evidence(root, result)
        campaign = _evaluate(plan, _campaign(plan, result))
        outcome = _publish(
            plan,
            result,
            root,
            campaign,
            embedded=embedded,
            publication_checkpoint=publication_checkpoint,
            campaign_published=record_campaign_publication,
        )
    except (OSError, ValueError, AcceptanceRecordingError) as exc:
        return _transaction_error(
            plan, root, result, exc, campaign, campaign_published=campaign_published
        )
    return outcome


def _checkpoint_progress(
    progress: CoverageProgressSink, outcome: CoverageTargetOutcome
) -> CoverageTargetOutcome:
    try:
        progress.completed(outcome)
    except OSError as exc:
        outcome = replace(
            outcome,
            exit_code=2,
            abort_remaining=True,
            detail=freeze_coverage_mapping(
                {
                    **outcome.detail,
                    "error": f"Progress checkpoint failed: {exc}",
                    "abort_remaining": True,
                }
            ),
        )
    return outcome


def _start_target(root: Path) -> None:
    if any((root / name).exists() for name in ("coverage.json", POINT_STORE_NAME, "native")):
        raise ValueError("Coverage attempts cannot resume; allocate a new invocation")
    root.mkdir(parents=True, exist_ok=True)
    # Exclusive creation also rejects an interrupted build with no native output.
    with (root / ".collection-started").open("x", encoding="utf-8"):
        pass


def _publish(
    plan: CoverageTargetPlan,
    result: CoverageCollectionResult,
    root: Path,
    campaign: CoverageCampaign,
    *,
    embedded: bool,
    publication_checkpoint: Callable[[str], None] | None,
    campaign_published: Callable[[], None],
) -> CoverageTargetOutcome:
    campaign_path, simulation_path = root / "coverage.json", root / "simulation.json"
    checkpoint = publication_checkpoint or (lambda _boundary: None)
    checkpoint("before:coverage_campaign")
    publish_coverage_campaign(root, campaign)
    campaign_published()
    checkpoint("after:coverage_campaign")
    observed = _has_completed_run(result)
    passed = all(run.simulation_verdict == "pass" for run in result.runs) if observed else None
    detail = _simulation_projection(plan, campaign, result, passed)
    if not embedded:
        write_campaign_json(simulation_path, detail)
    status = campaign.evaluation["status"]
    exit_code = _coverage_exit_code(result, status, passed)
    outcome = CoverageTargetOutcome(
        plan.handle.selector,
        exit_code,
        campaign_path,
        simulation_path,
        freeze_coverage_mapping(detail),
        abort_remaining=result.infrastructure_error,
    )
    if plan.acceptance is not None and not embedded:
        plan.acceptance.publish(plan, campaign, campaign_path)
    return outcome


def _evaluate(plan: CoverageTargetPlan, campaign: CoverageCampaign) -> CoverageCampaign:
    """Evaluate the Campaign; approved waivers load in every run unless ``--no-waivers``."""
    assert plan.roots is not None
    try:
        waivers = load_approved_waiver_set(plan.waiver_config, plan.roots, plan.known_targets)
    except CoverageWaiverValidationError as exc:
        evaluation: dict[str, object] = dict(campaign.evaluation)
        evaluation.update(
            status="blocked",
            diagnostics=[
                {"code": finding.code, "pointer": finding.pointer, "message": finding.message}
                for finding in exc.findings
            ],
        )
        return replace(
            campaign,
            evaluation=freeze_coverage_mapping(evaluation),
            findings=(*campaign.findings, *exc.findings),
        )
    return evaluate_coverage_campaign(campaign, plan.criterion, waivers)


def _transaction_error(
    plan, root, result, exc, campaign, *, campaign_published: bool
) -> CoverageTargetOutcome:
    detail = {
        "target": plan.handle.selector,
        "passed": None,
        "simulation": "not_run",
        "collection": "infrastructure_error",
        "evaluation": "not_requested" if plan.criterion is None else "blocked",
        "error": str(exc),
        "abort_remaining": True,
    }
    if isinstance(exc, CoverageCampaignValidationError):
        detail["findings"] = [
            {"code": f.code, "pointer": f.pointer, "message": f.message} for f in exc.findings
        ]
    if result is not None:
        observed = _has_completed_run(result)
        detail.update(
            passed=(
                all(run.simulation_verdict == "pass" for run in result.runs) if observed else None
            ),
            tests=[_coverage_test_projection(run) for run in result.runs],
            simulation=_simulation_status(result) if observed else "not_run",
        )
        if campaign_published and campaign is not None and not result.infrastructure_error:
            if campaign.collection["status"] != "collector_error":
                detail["collection"] = campaign.collection["status"]
            detail["evaluation"] = campaign.evaluation["status"]
    return CoverageTargetOutcome(
        plan.handle.selector,
        2,
        root / "coverage.json",
        root / "simulation.json",
        freeze_coverage_mapping(detail),
        True,
    )


def _has_completed_run(result: CoverageCollectionResult) -> bool:
    return any(
        run.attributes.get("execution") not in {"not_completed", "not_run"} for run in result.runs
    )


def _has_design_abort(result: CoverageCollectionResult) -> bool:
    return any(run.attributes.get("failure_kind") == "missing_input" for run in result.runs)


def _coverage_exit_code(
    result: CoverageCollectionResult, evaluation: object, passed: bool | None
) -> int:
    """Exit reflects simulation and collection trust; a threshold miss is recorded, not exited."""
    if _has_design_abort(result):
        return 1
    if result.status != "complete" or evaluation == "blocked":
        return 2
    return 1 if not passed else 0


def _collection_metadata(result: CoverageCollectionResult) -> Mapping[str, FrozenJson]:
    status = "incomplete" if _has_design_abort(result) else result.status
    return freeze_coverage_mapping(
        {
            "status": "incompatible"
            if result.native_format.compatibility == "incompatible"
            else status,
            "merge": {"status": result.merge.status, "artifact": result.merge.artifact},
            "diagnostics": [
                {"code": f.code, "pointer": f.pointer, "message": f.message}
                for f in result.findings
                if f.severity == "error"
            ],
        }
    )


def _ungated_evaluation() -> Mapping[str, FrozenJson]:
    return freeze_coverage_mapping(
        {
            "status": "not_requested",
            "criterion_fingerprint": None,
            "suite": {"status": "not_evaluated"},
            "thresholds": {},
            "metrics": [],
            "diagnostics": [],
        }
    )


def _simulation_projection(
    plan: CoverageTargetPlan,
    campaign: CoverageCampaign,
    result: CoverageCollectionResult,
    passed: bool | None,
) -> dict[str, object]:
    tests = [_coverage_test_projection(run) for run in result.runs]
    document: dict[str, object] = {
        "flow": "sim",
        "mode": "simulate",
        "timestamp": utc_now_rfc3339(),
        "complete": True,
        "target": plan.handle.selector,
        "target_identity": plan.handle.identity,
        "tb_top": plan.collection_request.target.toplevel if plan.collection_request else "",
        "eda_tool": "verilator",
        "passed": passed,
        "simulation": _simulation_status(result) if passed is not None else "not_run",
        "abort_remaining": result.infrastructure_error,
        "tests": tests,
        "collection": _public_collection_status(campaign, result),
        "evaluation": campaign.evaluation["status"],
        "coverage_campaign": "coverage.json",
    }
    infrastructure_error = next(
        (finding.message for finding in result.findings if finding.severity == "error"), ""
    )
    errors = [str(item["error_tail"]) for item in tests if item.get("error_tail")]
    if result.infrastructure_error and infrastructure_error:
        document["error"] = infrastructure_error
    elif errors:
        document["error"] = errors[0]
    return document


def _public_collection_status(
    campaign: CoverageCampaign, result: CoverageCollectionResult
) -> object:
    if result.infrastructure_error:
        return "infrastructure_error"
    return campaign.collection["status"]


def _coverage_test_projection(run) -> dict[str, object]:
    entry: dict[str, object] = {
        "name": run.test,
        "verdict": run.simulation_verdict,
        "passed": run.simulation_verdict == "pass",
        "collection": run.collection,
    }
    for field in (
        "execution",
        "termination",
        "failure_kind",
        "error_tail",
        "simulator_returncode",
    ):
        value = run.attributes.get(field)
        if (isinstance(value, str) and value) or (
            field == "simulator_returncode" and isinstance(value, int)
        ):
            entry[field] = value
    pre_sim = run.attributes.get("pre_sim")
    if isinstance(pre_sim, Mapping):
        projected = dict(pre_sim)
        entry["pre_sim"] = projected
        if projected.get("status") != "passed":
            status = projected.get("status")
            assert status in {"failed", "timed_out", "spawn_error"}
            detail = str(projected.get("detail") or "").strip()
            entry["error_tail"] = pre_sim_failure_message(cast(PreSimStatus, status), detail)
    return entry


def _simulation_status(result: CoverageCollectionResult) -> str:
    if any(
        isinstance(run.attributes.get("termination"), str)
        and run.attributes.get("termination") != "completed"
        for run in result.runs
    ):
        return "aborted"
    verdicts = {run.simulation_verdict for run in result.runs}
    for verdict in ("elab_error", "fail", "timeout", "inconclusive", "pass"):
        if verdict in verdicts:
            return verdict
    return "not_run"
