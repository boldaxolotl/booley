"""SimulateFlow — BooleyFlow for running RTL simulation.

Runs RTL simulation for one or more configs via the Edalize sim flow + a
EDA-tool-specific run-half (sim.verilator_run / sim.iverilog_run).
Supports multi-config sequential execution, per-test filtering, dry-run mode,
cycle count extraction, and structured JSON reporting.
"""

from __future__ import annotations

import json
import logging
import os
import shlex
import sys
from collections import Counter
from collections.abc import Callable, Collection, Iterator, Mapping, Sequence
from contextlib import ExitStack, contextmanager, suppress
from dataclasses import dataclass, field, replace
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from time import monotonic
from typing import Any, ClassVar, cast

from booley.config.project_config import (
    load_test_configuration_field,
    lookup_target_section,
)
from booley.core.build_paths import work_root_for
from booley.criteria.templates import BASELINE_TARGET_PARAM
from booley.criteria.thresholds import has_relative_threshold
from booley.flows.cli_help import CliHelp
from booley.flows.display import format_flow_display_label
from booley.flows.eda_failures import find_missing_executable, new_attempt_token
from booley.flows.endpoint_admission import AdmissionContext
from booley.flows.execution_persistence import (
    EvidenceDiscarded,
    FlowExecutionAdapter,
    execution_goal_name,
    frozen_test_runs_for,
    register_campaign_for,
    wants_campaigns,
)
from booley.flows.frozen_test_runs import FrozenTestRuns, resolve_goal_test_runs
from booley.flows.plan import (
    CommandPlan,
    FlowPlan,
    WorkUnitPlan,
    WorkUnitRole,
    normalize_plan_argv,
    normalize_plan_inputs,
    normalize_plan_path,
    normalize_plan_paths,
    plan_value_fingerprint,
    stable_unit_id,
)
from booley.flows.prepared_invocation import PreparedInvocation
from booley.flows.progress_lifecycle import (
    ProgressLifecycle,
    ProgressPublicationError,
    progress_document,
    supersede_progress,
)
from booley.flows.request import FlowRequest
from booley.flows.run_log import RUN_LOG_NAME, write_run_log
from booley.flows.sim.campaign.test_run_report import ROW_LIMIT as TEST_RUN_ROW_LIMIT
from booley.flows.sim.campaign.test_run_report import (
    campaign_test_run_rows,
    informative_test_run_lines,
)
from booley.flows.sim.campaign_files import document_digest
from booley.flows.sim.campaign_invocation import SimulationCampaignInvocation
from booley.flows.sim.campaign_reports import target_report_directory
from booley.flows.sim.cli import SimArguments
from booley.flows.sim.config import (
    resolve_pre_sim_commands,
    resolve_sim_build_timeout_ms,
    resolve_sim_trace_convert_timeout_ms,
)
from booley.flows.sim.coverage_campaign import CoverageCampaign, CoverageRollup
from booley.flows.sim.coverage_invocation import CoverageTargetPlan
from booley.flows.sim.coverage_json import FrozenJson
from booley.flows.sim.coverage_policy import coverage_metrics
from booley.flows.sim.coverage_progress import CoverageProgress
from booley.flows.sim.coverage_transaction import CoverageTargetOutcome
from booley.flows.sim.live_progress import (
    LiveProgressSink,
    attempt_scope,
    current_progress,
    install_progress,
    observe_stage,
    publish_checkpoint,
)
from booley.flows.sim.request import SimRequest
from booley.flows.sim.verilator_coverage import SimulationExecutionPort
from booley.flows.target_test_plan import (
    TargetTestPlan,
    batch_test_runs,
)
from booley.flows.target_test_plan_loading import load_target_test_plan
from booley.flows.target_test_suite import (
    NoRunnableTestsError,
    require_runnable_target_test_suite,
    resolve_target_test_suite,
)
from booley.flows.test_runs import (
    DEFAULT_SEED,
    SeedCount,
    SeedRequest,
    TestRun,
    parse_seed_request,
    parse_test_selector,
    test_run_from_document,
)
from booley.fusesoc import fusesoc_registry
from booley.runtime import job_slots
from booley.runtime.endpoint_execution import (
    EXIT_CANCELLED,
    EXIT_ERROR,
    EXIT_FAILURE,
    EXIT_SUCCESS,
    EndpointOutcome,
    normalize_completion_error,
)
from booley.runtime.file_lock import LockContentionError
from booley.runtime.filesystem_utils import replace_file
from booley.runtime.platform_paths import posix_relpath
from booley.runtime.timefmt import utc_now_rfc3339
from booley.targets.catalog import PreparedTargetSelection, TargetCatalog
from booley.targets.domain import (
    TargetHandle,
    criterion_matches_target,
)
from booley.targets.selection import TargetSectionConflictError

from .. import edam as edam_layer
from .. import output_budget
from ..base import BuiltinFlow, SubprocessResult
from ..baseline_worktree import (
    BaselineWorktreeError,
    baseline_worktree,
    git_full_sha,
)
from ..eda_parsers import extract_error_gist
from ..invocation import default_timeout_ms, resolve_timeout_ms
from .build import (
    BuildOutcome,
    PreparedSimulationBuild,
    SimulationBuildInfrastructureError,
    SimulationBuildPreparationError,
    classify_build_outcome,
    prepare_simulation_build,
    setup_failure_outcome,
    simulation_build_script,
    simulation_setup_command,
)
from .build_session import (
    SimulationBuildSession,
    SimulationBuildSlotError,
    preview_generation_root,
    project_compile_surface,
    resolve_target_compile_surface,
)
from .campaign import (
    CampaignOutcome,
    CampaignPolicy,
    CampaignRecoveryStatus,
    NewCampaignPreview,
    NewCampaignPreviewRequest,
    NewCampaignRunRequest,
    ResumeCampaignPreview,
    ResumeCampaignPreviewRequest,
    ResumeCampaignRunRequest,
    SimulationCampaign,
    SimulationCampaignCancellationError,
    SimulationCampaignIntegrityError,
    SimulationCampaignManifest,
    SimulationCampaignPlan,
    ValidatedResumeManifest,
    build_artifact_reference,
    encode_artifact_reference,
    encode_simulation_campaign_manifest,
    started_campaign,
    validate_resume_manifest,
)
from .campaign.codec import MANIFEST_MAX_BYTES
from .campaign.coordinator import CampaignPublicationError, CampaignPublicationFailure
from .campaign.coverage_execution import (
    CoverageAggregateExecutor,
    _CoverageAggregateError,
    _endpoint_collection,
    _failure_test,
)
from .campaign.flow_planning import (
    plan_coarse_simulation_campaign,
    plan_ordinary_hdl_campaign,
)
from .campaign.model import simulation_status_from_observations
from .campaign.planning import (
    SimulationCampaignWorkloadMismatchError,
    manifest_digest,
    verify_workload,
)
from .campaign.serial_execution import OrdinaryHdlSerialExecutor
from .coverage_campaign_store import load_coverage_campaign
from .coverage_reference import (
    MAX_REFERENCE_BYTES,
    resolve_coverage_campaign_reference,
)
from .coverage_source_gaps import source_gap_report_lines
from .execution import (
    DefaultSelection,
    NamedTests,
    SelectedTestRuns,
    SimulationExecution,
    SimulationOptions,
    SimulationPreview,
    SimulationSelection,
)
from .execution.artifacts import artifact_path_component as _artifact_path_component
from .execution.engine import TRACE_CLEANUP_MARGIN_S, simulation_adapter_kind, target_test_plan
from .mode import SimulationMode, normalize_simulation_mode
from .standalone import StandaloneMixin, _StandaloneOutcome, _StandalonePlanRecipe
from .test_run_invocation import (
    BaselineLink,
    BaselineRefusal,
    BaselineRuns,
    TestRunInvocation,
    decide_baseline_runs,
)

logger = logging.getLogger(__name__)


def _target_is_cocotb(work_dir: Path, target: str) -> bool:
    """Read Cocotb identity through the Target catalog, failing soft for sizing."""
    try:
        return bool(TargetCatalog.build(work_dir).select(target).cocotb_module)
    except Exception:  # noqa: BLE001 — watchdog sizing degrades to native-HDL counting
        return False


class MissingExecutableError(RuntimeError):
    """A required EDA binary is absent — a Flow error, never a test failure.

    Carries the binary name so :meth:`SimulateFlow._missing_executable_result`
    can name it in the exit-2 verdict instead of inventing a per-test FAIL row
    for a test that never ran.
    """

    def __init__(self, binary: str, context: str = "") -> None:
        super().__init__(f"required executable not found: {binary}")
        self.binary = binary
        self.context = context


@dataclass(frozen=True, slots=True)
class PreparedSimulationEndpoint:
    """Read-only all-Target preflight retained through endpoint execution."""

    targets: tuple[TargetHandle, ...]
    resume: ValidatedResumeManifest | None
    selected_targets: tuple[str, ...]
    test_names_map: dict[str, list[str]]


@dataclass(frozen=True, slots=True)
class ReportTarget:
    """Candidate identity of a resume that has no catalog binding."""

    identity: str
    selector: str
    name: str

    @classmethod
    def from_document(cls, document: Mapping[str, Any]) -> ReportTarget:
        """The Target a campaign manifest records."""
        target = document["target"]
        return cls(
            f"{target['vlnv']}#{target['name']}", str(target["selector"]), str(target["name"])
        )


@dataclass(frozen=True, slots=True)
class CandidateWorkload:
    """One prepared candidate's Tests and the sim_pass Criteria they can judge."""

    handle: TargetHandle | ReportTarget
    selected: frozenset[str]
    eligible: frozenset[str]


_DEFAULT_TIMEOUT_MS = default_timeout_ms("sim")


@dataclass
class ElabOnlyTargetResult:
    """One Target's compile/elaborate/link result in Simulation namespace."""

    target: str
    target_identity: str = ""
    eda_tool: str = ""
    toplevel: str = ""
    compile_command: str = ""
    fileset: dict[str, list[str]] = field(default_factory=dict)
    outcome: BuildOutcome = field(
        default_factory=lambda: BuildOutcome(False, None, "infrastructure")
    )
    log_path: str = ""


def _campaign_observation_counts(
    observations: Sequence[Mapping[str, object]],
) -> dict[str, dict[str, int]]:
    """Bounded structured summary preserving independent observation axes."""
    axes = ("execution", "functional", "assertions")
    return {axis: dict(Counter(str(item[axis]) for item in observations)) for axis in axes}


def _coverage_diagnostic_detail(diagnostics: FrozenJson) -> str | None:
    records = cast(tuple[FrozenJson, ...], diagnostics)
    if not records:
        return None
    first = records[0]
    if isinstance(first, str):
        return first
    diagnostic = cast(Mapping[str, FrozenJson], first)
    return str(diagnostic["code"])


def _compact_coverage_number(value: FrozenJson) -> str:
    assert isinstance(value, int | float) and not isinstance(value, bool)
    rendered = format(Decimal(str(value)), "f")
    return rendered.rstrip("0").rstrip(".") if "." in rendered else rendered


def _failed_metric_detail(metrics: FrozenJson) -> str | None:
    records = cast(tuple[Mapping[str, FrozenJson], ...], metrics)
    failed = next(
        (item for item in records if item["verdict"] == "fail"),
        None,
    )
    if failed is None:
        return None
    return (
        f"{failed['metric']}: observed {failed['covered_points']}/"
        f"{failed['eligible_points']} points; "
        f"displayed {_compact_coverage_number(failed['actual_percent'])}%; "
        f"minimum {_compact_coverage_number(failed['minimum_percent'])}%"
    )


def _coverage_report_suffix(campaign: CoverageCampaign) -> str:
    collection = campaign.collection
    evaluation = campaign.evaluation
    collection_status = str(collection["status"]).upper()
    evaluation_status = str(evaluation["status"]).upper()
    collection_detail = None
    if collection["status"] != "complete":
        collection_detail = _coverage_diagnostic_detail(collection["diagnostics"])
    evaluation_detail = None
    if evaluation["status"] == "blocked":
        evaluation_detail = _coverage_diagnostic_detail(evaluation["diagnostics"])
    elif evaluation["status"] == "fail":
        evaluation_detail = _failed_metric_detail(evaluation["metrics"])
    if collection_detail:
        collection_status += f" ({collection_detail})"
    if evaluation_detail:
        evaluation_status += f" ({evaluation_detail})"
    return f"coverage collection {collection_status} · evaluation {evaluation_status}"


def _truncated_percent(rollup: CoverageRollup, decimals: int) -> str:
    """Return covered/eligible as a percentage truncated (not rounded) to *decimals*.

    Truncation keeps ``100.0%`` meaning full coverage and keeps a printed number on the
    same side of a minimum as the exact verdict, which a rounded value would not.
    """
    scale = 10**decimals
    scaled = Fraction(rollup.covered_points * 100 * scale, rollup.eligible_points)
    whole, fraction = divmod(scaled.numerator // scaled.denominator, scale)
    # Integer formatting: dividing by *scale* as a float can round a long-decimal value up.
    return f"{whole}.{fraction:0{decimals}d}%"


def _coverage_metric_part(rollup: CoverageRollup, threshold: object, met: object) -> str:
    """Render one metric of the card's ``coverage:`` line, with its floor when it has one."""
    decimals = 1
    suffix = ""
    if threshold is not None:
        minimum = _compact_coverage_number(cast(FrozenJson, threshold))
        decimals = max(1, len(minimum.partition(".")[2]))
        suffix = f" (min {minimum}%, {'met' if met else 'missed'})"
    value = "n/a" if rollup.eligible_points == 0 else _truncated_percent(rollup, decimals)
    return f"{rollup.metric} {value}{suffix}"


def _coverage_metrics_line(campaign: CoverageCampaign) -> str | None:
    """Return the card's ``coverage:`` line, or ``None`` unless collection is complete."""
    if campaign.collection["status"] != "complete" or not campaign.rollups:
        return None
    metrics = coverage_metrics(campaign)
    parts = [
        _coverage_metric_part(
            rollup, metrics[rollup.metric]["threshold"], metrics[rollup.metric]["met"]
        )
        for rollup in campaign.rollups
    ]
    return "  coverage: " + " · ".join(parts)


def _coverage_campaign_path(outcome: CampaignOutcome) -> Path:
    """Return the public ``coverage.json`` beside the Target's Campaign directory."""
    return outcome.manifest_path.parents[1] / "coverage.json"


def _coverage_campaign_lines(outcomes: Sequence[CampaignOutcome]) -> list[str]:
    """One ``coverage campaign:`` line per finished coverage outcome."""
    return [
        f"  coverage campaign: {_coverage_campaign_path(outcome).resolve()}"
        for outcome in outcomes
        if getattr(outcome, "coverage_reference", None) is not None
    ]


def _waiver_block_hint_lines(campaign: CoverageCampaign) -> list[str]:
    """Suggest raw numbers when approved waivers, not the evidence, blocked evaluation."""
    evaluation = campaign.evaluation
    if evaluation["status"] != "blocked":
        return []
    diagnostics = cast(tuple[FrozenJson, ...], evaluation["diagnostics"])
    if any(
        str(item.get("code", "")).startswith("COV_WAIVER_")
        for item in diagnostics
        if isinstance(item, Mapping)
    ):
        return [
            "  approved coverage waivers are invalid; fix them or rerun with "
            "--no-waivers for raw coverage"
        ]
    return []


def _resolved_coverage_campaigns(
    outcomes: Sequence[CampaignOutcome],
) -> dict[str, CoverageCampaign]:
    campaigns: dict[str, CoverageCampaign] = {}
    for outcome in outcomes:
        if getattr(outcome, "coverage_reference", None) is None:
            continue
        public = _coverage_campaign_path(outcome)
        selector = str(outcome.target["selector"])
        campaigns[selector] = resolve_coverage_campaign_reference(public).loaded.campaign
    return campaigns


def _campaign_report_lines(
    outcomes: Sequence[CampaignOutcome],
    coverage_campaigns: Mapping[str, CoverageCampaign] | None = None,
    trailing_lines: Sequence[str] | None = (),
    *,
    report_path: Path | None = None,
    default_seeds: Mapping[str, int | None] | None = None,
) -> list[str]:
    if coverage_campaigns is None:
        coverage_campaigns = _resolved_coverage_campaigns(outcomes)
    blocks = _campaign_report_blocks(outcomes, coverage_campaigns, report_path, default_seeds)
    budget = 0
    if trailing_lines is not None:
        stream = min(output_budget.mcp_stdout_budget(), output_budget.mcp_stderr_budget())
        parts = [line for prefix, suffix in blocks for line in (*prefix, *suffix)]
        parts.extend(trailing_lines)
        used = sum(len(line.encode()) + 1 for line in parts) + 1
        budget = min(stream // 4, max(0, stream - used))
    additions = _campaign_cycle_lines(outcomes, budget)
    return [
        "\n".join((*prefix, *extra, *suffix))
        for (prefix, suffix), extra in zip(blocks, additions, strict=True)
    ]


def _campaign_cycle_lines(outcomes: Sequence[CampaignOutcome], budget: int) -> list[list[str]]:
    additions: list[list[str]] = [[] for _ in outcomes]
    records = [
        (index, item)
        for index, outcome in enumerate(outcomes)
        for item in outcome.observations
        if item.get("cycle_count") is not None
    ]
    notice = f"  {len(records)} cycle counts omitted"
    remaining = max(0, budget - len(notice.encode("utf-8")) - 1)
    omitted = len(records)
    for index, item in records[:32]:
        line = _cycle_display_line(item, remaining)
        if line is None:
            continue
        additions[index].append(line)
        remaining -= len(line.encode("utf-8")) + 1
        omitted -= 1
    notice = f"  {omitted} cycle counts omitted"
    if omitted and len(notice.encode("utf-8")) + 1 <= budget:
        additions[-1].append(notice)
    return additions


def _cycle_display_line(item: Mapping[str, object], budget: int) -> str | None:
    try:
        suffix = f": cycles={item['cycle_count']}"
    except ValueError:
        return None
    name = str(item["test"] or "default").replace("\n", " ").replace("\r", " ")
    available = budget - len(suffix.encode("utf-8")) - 3
    if available < 3:
        return None
    if len(name.encode("utf-8")) > available:
        name = name.encode("utf-8")[: available - 3].decode("utf-8", errors="ignore") + "…"
    return f"  {name}{suffix}"


def _observation_reasons(outcome: CampaignOutcome) -> list[str]:
    """Each distinct reason the observations of *outcome* give, in order."""
    reasons: list[str] = []
    for observation in outcome.observations:
        detail = observation.get("detail")
        reason = detail.get("reason") if isinstance(detail, Mapping) else None
        if isinstance(reason, str) and reason and reason not in reasons:
            reasons.append(reason)
    return reasons


def _campaign_report_blocks(
    outcomes: Sequence[CampaignOutcome],
    coverage_campaigns: Mapping[str, CoverageCampaign],
    report_path: Path | None,
    default_seeds: Mapping[str, int | None] | None,
) -> list[tuple[list[str], list[str]]]:
    blocks = []
    for outcome in outcomes:
        acceptance = getattr(outcome, "acceptance_facts", None)
        facts = getattr(acceptance, "document", {})
        campaign_id = facts.get("campaign_id", "unavailable")
        selector = str(outcome.target["selector"])
        label = outcome.aggregate_grade.upper()
        if getattr(outcome, "coverage_reference", None) is not None:
            campaign = coverage_campaigns[selector]
            label = f"simulation {label} · {_coverage_report_suffix(campaign)}"
        line = f"{selector}: {label} (Simulation Campaign {campaign_id})"
        reasons = _observation_reasons(outcome)
        build_detail = _campaign_build_infrastructure_detail([outcome])
        if build_detail and reasons:
            line += f": {reasons[0].splitlines()[0]}"
        coverage_lines: list[str] = []
        if getattr(outcome, "coverage_reference", None) is not None:
            metrics_line = _coverage_metrics_line(campaign)
            coverage_lines = [
                *([metrics_line] if metrics_line else []),
                *_coverage_campaign_lines([outcome]),
            ]
        report_lines = [
            line,
            f"  manifest: {outcome.manifest_path.resolve()}",
            *([f"  report: {report_path.resolve()}"] if report_path is not None else []),
            *coverage_lines,
            *(f"  {reason}" for reason in reasons),
            *_campaign_test_run_lines(
                selector, outcome, _effective_default_seed(default_seeds, selector)
            ),
        ]
        suffix = []
        if getattr(outcome, "coverage_reference", None) is not None:
            suffix.extend(_waiver_block_hint_lines(campaign))
            suffix.extend(source_gap_report_lines(campaign))
        blocks.append((report_lines, suffix))
    return blocks


def _effective_default_seed(
    default_seeds: Mapping[str, int | None] | None, selector: str
) -> int | None:
    """*selector*'s Default Seed for report wording.

    A Target whose own Default Seed is not known here is judged by the
    Booley-wide one, which is what it uses unless its ``tests.toml`` says
    otherwise.
    """
    return (default_seeds or {}).get(selector, DEFAULT_SEED)


def _append_missing_report_lines(result: EndpointOutcome, lines: list[str]) -> EndpointOutcome:
    present = Counter(result.report_text.splitlines())
    missing = []
    for line in lines:
        if present[line]:
            present[line] -= 1
        else:
            missing.append(line)
    if missing:
        result.report_text += "\n" + "\n".join(missing)
    return result


_PRE_SIM_ROLE_LABELS = {"candidate": "Candidate", "cycle_count_baseline": "Cycle Count baseline"}
_PRE_SIM_ROLE_HEADINGS = frozenset(f"{label}:" for label in _PRE_SIM_ROLE_LABELS.values())


def _is_pre_sim_card_line(line: str) -> bool:
    """Whether *line* belongs to the card's Pre-Sim block (firing line or role heading)."""
    return line.startswith("pre_run_commands ") or line in _PRE_SIM_ROLE_HEADINGS


def _pre_sim_integrity_failure(
    result: EndpointOutcome, error: Exception, *, retain_authenticated: bool = False
) -> EndpointOutcome:
    result.exit_code = EXIT_ERROR
    result.criterion_met = False
    if not retain_authenticated:
        references = [record.get("reference") for record in result.detail.get("pre_sim_runs", ())]
        result.detail = {
            key: value for key, value in result.detail.items() if not key.startswith("pre_sim_")
        }
        result.detail["pre_sim_unauthenticated_references"] = references
        result.report_text = "\n".join(
            line for line in result.report_text.splitlines() if not _is_pre_sim_card_line(line)
        )
    result.detail["campaign_integrity_error"] = str(error)
    line = f"Simulation Campaign integrity failure: {error}"
    if line not in result.report_text.splitlines():
        result.report_text += f"\n{line}"
    return result


def _campaign_hook_inputs(outcomes, invocation):
    firings = {f.key: f for outcome in outcomes for f in outcome.pre_sim_firings}
    keys = frozenset(
        key
        for outcome in outcomes
        if invocation is not None
        and outcome.producer_invocation_directory == invocation.absolute()
        for key in outcome.current_pre_sim_keys
    )
    return firings, keys


def _pre_sim_lines(firings, current_keys) -> tuple[list[str], list[str]]:
    """Render each current firing once: ``(flat, card)``.

    *flat* keeps firing order (sim.json); *card* groups the same lines by role, with
    role headings only when several roles are current.
    """
    from .execution.pre_sim_reporting import pre_sim_report_line

    groups: dict[str, list[str]] = {}
    flat: list[str] = []
    for key, firing in firings.items():
        if key in current_keys:
            document = firing.document
            line = pre_sim_report_line(document["target"]["selector"], document)
            flat.append(line)
            groups.setdefault(document["role"], []).append(line)
    card = [
        line
        for role, lines in groups.items()
        for line in ([_PRE_SIM_ROLE_LABELS[role] + ":"] if len(groups) > 1 else []) + lines
    ]
    return flat, card


def _pre_sim_projection(
    firings, current_keys, invocation: Path | None
) -> tuple[dict[str, object], list[str]]:
    """The sim.json Pre-Sim detail and the matching card lines, from one pass."""
    lines, card = _pre_sim_lines(firings, current_keys)
    manifest_refs = {}
    preview = []
    preview_full = False
    roles: dict[str, dict[str, object]] = {}
    for firing in firings.values():
        _register_pre_sim_role(
            roles, manifest_refs, firing, invocation, firing.key in current_keys
        )
        if not preview_full and len(preview) < 32:
            record = _pre_sim_preview_record(firing, invocation)
            if len(json.dumps([*preview, record]).encode("utf-8")) <= 16384:
                preview.append(record)
            else:
                preview_full = True
    detail = {
        "pre_sim_lines": lines,
        "pre_sim_runs": preview,
        "pre_sim_total": len(firings),
        "pre_sim_current": len(lines),
        "pre_sim_historical": len(firings) - len(lines),
        "pre_sim_preview_truncated": len(preview) != len(firings),
        "pre_sim_roles": roles,
    }
    return detail, card


def _register_pre_sim_role(roles, manifest_refs, firing, invocation, current) -> None:
    document = firing.document
    role = str(document["role"])
    role_detail = roles.setdefault(role, {"total": 0, "current": 0, "manifests": []})
    role_detail["total"] += 1
    role_detail["current"] += int(current)
    if firing.manifest_path not in manifest_refs:
        manifest_refs[firing.manifest_path] = _report_artifact_reference(
            firing.manifest_path,
            report_invocation=invocation,
            external_origin_target=firing.manifest_path.parent.parent,
            kind="simulation_campaign_manifest",
            owner=str(document["campaign_id"]),
            maximum=16 * 1024 * 1024,
        )
    manifest_ref = manifest_refs[firing.manifest_path]
    if manifest_ref not in role_detail["manifests"]:
        role_detail["manifests"].append(manifest_ref)
    role_detail["external_origin_requires_context"] = role_detail.get(
        "external_origin_requires_context", False
    ) or (manifest_ref is not None and manifest_ref["path_base"] == "external_origin_target")


def _pre_sim_preview_record(firing, invocation: Path | None) -> dict[str, object]:
    document = firing.document
    record = {
        key: value
        for key, value in document.items()
        if key not in {"stdout_tail", "stderr_tail", "detail", "target", "test_names"}
    }
    record.update(
        target=dict(document["target"]),
        test_count=len(document["test_names"]),
        test_names=list(document["test_names"][:8]),
        test_names_sha256=document_digest(list(document["test_names"])),
        terminal=firing.terminal,
        reference=_report_artifact_reference(
            firing.path,
            report_invocation=invocation,
            external_origin_target=firing.manifest_path.parent.parent,
            kind="pre_sim_commands",
            owner=str(document["attempt_id"]),
            maximum=16 * 1024 * 1024,
        ),
    )
    return record


def _manifest_no_waivers(manifest: SimulationCampaignManifest) -> bool:
    """Whether the frozen workload asked for raw coverage; absent (legacy) means waivers apply."""
    workload = cast(Mapping[str, object], manifest.document["workload"])
    return workload.get("no_waivers") is True


def _coverage_preflight_findings_outcome(findings: Sequence[Any]) -> EndpointOutcome:
    """Exit-2 outcome that reports every atomic coverage preflight finding."""
    return EndpointOutcome(
        exit_code=2,
        report_text="\n".join(item.message for item in findings),
        detail={
            "findings": [
                {"code": item.code, "pointer": item.pointer, "message": item.message}
                for item in findings
            ]
        },
    )


def _campaign_structured_details(
    outcomes: Sequence[CampaignOutcome],
    report_invocation: Path | None = None,
) -> dict[str, object]:
    """Expose one canonical artifact map based on the containing report."""
    return {
        str(outcome.target["selector"]): _campaign_report_detail(outcome, report_invocation)
        for outcome in outcomes
    }


def _coverage_request_target(request: NewCampaignRunRequest) -> Mapping[str, object]:
    """Expose the typed Target projection from one immutable campaign request."""
    return cast(Mapping[str, object], request.plan.manifest.document["target"])


def _coverage_request_selector(request: NewCampaignRunRequest) -> str:
    return str(_coverage_request_target(request)["selector"])


def _campaign_report_detail(
    outcome: CampaignOutcome, report_invocation: Path | None
) -> dict[str, object]:
    acceptance = getattr(outcome, "acceptance_facts", None)
    facts = getattr(acceptance, "document", {})
    owner = str(facts.get("campaign_id", "unavailable"))
    origin = facts.get("origin", {})
    assert isinstance(origin, Mapping)
    paths = _campaign_artifact_paths(outcome)
    artifacts = _campaign_report_artifacts(
        paths,
        report_invocation=report_invocation,
        external_origin_target=outcome.manifest_path.parents[1],
        owner=owner,
    )
    local = report_invocation is not None and outcome.manifest_path.is_relative_to(
        report_invocation
    )
    detail: dict[str, object] = {
        "campaign_id": owner,
        "manifest_sha256": facts.get("manifest_sha256"),
        "origin_invocation_id": origin.get("invocation_id"),
        "dependency": "local_campaign" if local else "external_origin_campaign",
        "dependency_available": "manifest" in artifacts,
        "artifacts": artifacts,
        "grade": outcome.aggregate_grade,
        "complete": outcome.complete,
        "observation_counts": _campaign_observation_counts(outcome.observations),
        **_campaign_observation_preview(outcome.observations),
        **_campaign_test_run_detail(outcome),
    }
    if report_invocation is None:
        detail["resolved_artifacts"] = {
            name: str(path) for name, (path, _kind, _maximum) in paths.items()
        }
    return detail


def _diagnostic_test_runs(
    detail: Mapping[str, object],
    frozen: FrozenTestRuns,
    names: Callable[[str], Collection[str]],
) -> tuple[list[str], list[str]]:
    """Test Runs of *detail*'s campaigns no Goal requires, and the failed ones among them.

    Both as ``target: test run``. *names* gives every name of a campaign's
    Target (selector and identity): a Goal may be written with either.
    """
    campaigns = detail.get("campaigns")
    if not isinstance(campaigns, Mapping):
        return [], []
    diagnostic: list[str] = []
    failures: list[str] = []
    for selector, campaign in campaigns.items():
        rows = campaign.get("test_runs", ()) if isinstance(campaign, Mapping) else ()
        goals = frozen.of_target(names(str(selector)))
        for row in rows:
            if not isinstance(row, Mapping) or any(
                goal.requires(row.get("name"), row.get("fingerprint")) for goal in goals
            ):
                continue
            diagnostic.append(f"{selector}: {row['name']}")
            if row.get("grade") == "fail":
                failures.append(f"{selector}: {row['name']}")
    return diagnostic, failures


def _with_frozen_runs_of_a_diagnostic_run(
    result: EndpointOutcome,
    frozen: FrozenTestRuns,
    names: Callable[[str], Collection[str]],
    goal_name: Callable[[str], str],
) -> EndpointOutcome:
    """Say which Goals' frozen Test Runs a ``--diagnostic`` run executed, and which failed.

    The run records nothing; the report must not let that pass unnoticed
    when what ran is exactly what a Goal is judged by. The report names each
    Goal as *goal_name* presents its key; ``detail`` keeps the keys.
    """
    campaigns = result.detail.get("campaigns")
    ran: dict[str, list[str]] = {}
    failed: dict[str, list[str]] = {}
    for selector, campaign in campaigns.items() if isinstance(campaigns, Mapping) else ():
        rows = campaign.get("test_runs", ()) if isinstance(campaign, Mapping) else ()
        for goal in frozen.of_target(names(str(selector))):
            for row in goal.matching([row for row in rows if isinstance(row, Mapping)]):
                ran.setdefault(goal.key, []).append(str(row["name"]))
                if row.get("grade") == "fail":
                    failed.setdefault(goal.key, []).append(str(row["name"]))
    if not ran:
        return result
    result.detail["diagnostic_test_runs"] = {"goal_frozen_runs": ran, "goal_failures": failed}
    lines = ["Diagnostic run: these frozen Test Runs of Goals ran and nothing was recorded:"]
    lines += [f"  {goal_name(key)}: {', '.join(runs)}" for key, runs in ran.items()]
    if failed:
        lines.append("Diagnostic failures of Goals' frozen Test Runs (not recorded):")
        lines += [f"  {goal_name(key)}: {', '.join(runs)}" for key, runs in failed.items()]
    note = "\n".join(lines)
    result.report_text = f"{result.report_text}\n{note}" if result.report_text else note
    return result


def _diagnostic_test_run_notes(
    fresh: Sequence[str], diagnostic: Sequence[str], failures: Sequence[str]
) -> list[str]:
    """The report lines about Test Runs that are evidence for no Goal."""
    notes: list[str] = []
    if fresh:
        notes.append(
            "Diagnostic: no Goal names these multi-seed Test Runs with this seed count "
            f"({', '.join(fresh[:8])}); their seeds are new and they record no Goal evidence."
        )
    if diagnostic:
        shown = ", ".join(diagnostic[:8])
        more = f", and {len(diagnostic) - 8} more" if len(diagnostic) > 8 else ""
        notes.append(
            f"Diagnostic: these Test Runs match no Goal and record no Goal evidence ({shown}{more})."
        )
    if failures:
        # These failures block no Goal: say so apart from the Target lines.
        notes.append("Diagnostic failures (they block no Goal):")
        notes.extend(f"  {name}" for name in failures[:TEST_RUN_ROW_LIMIT])
        if len(failures) > TEST_RUN_ROW_LIMIT:
            notes.append(f"  ... and {len(failures) - TEST_RUN_ROW_LIMIT} more")
    return notes


def _campaign_test_runs(outcome: CampaignOutcome) -> list[dict[str, object]]:
    """Per-Test-Run rows of *outcome*; empty for an object that has no manifest."""
    if not isinstance(getattr(outcome, "manifest_path", None), Path):
        return []
    return campaign_test_run_rows(outcome)


def _campaign_test_run_lines(
    selector: str, outcome: CampaignOutcome, default_seed: int | None = DEFAULT_SEED
) -> list[str]:
    """Report rows only where they say more than the Target's own line."""
    return informative_test_run_lines(
        selector, _campaign_test_runs(outcome), default_seed=default_seed
    )


def _campaign_test_run_detail(outcome: CampaignOutcome) -> dict[str, object]:
    """The structured rows of every Test Run, bounded like the observations."""
    rows = _campaign_test_runs(outcome)
    if not rows:
        return {}
    return {
        "test_runs": rows[:TEST_RUN_ROW_LIMIT],
        "test_run_total": len(rows),
        "test_runs_truncated": len(rows) > TEST_RUN_ROW_LIMIT,
    }


def _campaign_artifact_paths(
    outcome: CampaignOutcome,
) -> dict[str, tuple[Path, str, int]]:
    paths = {
        "manifest": (
            outcome.manifest_path,
            "simulation_campaign_manifest",
            MANIFEST_MAX_BYTES,
        ),
        "simulation": (
            outcome.manifest_path.parents[1] / "simulation.json",
            "simulation_projection",
            MANIFEST_MAX_BYTES,
        ),
    }
    if outcome.coverage_reference is not None:
        paths["coverage"] = (
            outcome.manifest_path.parents[1] / "coverage.json",
            "coverage_campaign_reference",
            MAX_REFERENCE_BYTES,
        )
    return paths


def _campaign_report_artifacts(
    paths: Mapping[str, tuple[Path, str, int]],
    *,
    report_invocation: Path | None,
    external_origin_target: Path,
    owner: str,
) -> dict[str, object]:
    artifacts: dict[str, object] = {}
    for name, (path, kind, maximum) in paths.items():
        reference = _report_artifact_reference(
            path,
            report_invocation=report_invocation,
            external_origin_target=external_origin_target,
            kind=kind,
            owner=owner,
            maximum=maximum,
        )
        if reference is not None:
            artifacts[name] = reference
    return artifacts


def _report_artifact_reference(
    path: Path,
    *,
    report_invocation: Path | None,
    external_origin_target: Path,
    kind: str,
    owner: str,
    maximum: int,
) -> dict[str, object] | None:
    if report_invocation is None or not path.is_file():
        return None
    reports_root = report_invocation.parent.parent
    if path.is_relative_to(report_invocation):
        base_name, base = "report_invocation", report_invocation
    elif path.is_relative_to(reports_root):
        base_name, base = "reports_root", reports_root
    elif path.is_relative_to(external_origin_target):
        base_name, base = "external_origin_target", external_origin_target
    else:
        return None
    # Imported here: retention pulls in the whole pruning stack the flow rarely needs.
    from .campaign_retention import invocation_booley_dir

    reference = build_artifact_reference(
        path,
        base_name=base_name,
        base=base,
        kind=kind,
        owner=owner,
        maximum=maximum,
        booley_dir=invocation_booley_dir(report_invocation),
    )
    return encode_artifact_reference(reference)


def _campaign_recovery_detail(status: CampaignRecoveryStatus) -> dict[str, object]:
    """Translate campaign-owned recovery state into the endpoint's flat shape."""
    return {
        "manifest_sha256": status.manifest_sha256,
        "completed": list(status.completed),
        "interrupted": list(status.interrupted),
        "pending": list(status.pending),
    }


def _baseline_refusal_line(refusal: BaselineRefusal) -> str:
    """One report line: which Cycle Count comparison is inconclusive, and why."""
    link = refusal.link
    run = f" (Test Run {link.run.name})" if link.run is not None else ""
    return (
        f"Cycle Count comparison inconclusive for {link.candidate} Test {link.test}{run}: "
        f"{refusal.reason}"
    )


def _campaign_preview_entry(preview: NewCampaignPreview) -> dict[str, object]:
    """Summarize one new-campaign preview for the dry-run report and detail."""
    return {
        "target": dict(preview.target),
        "work_items": len(preview.work_items),
        "build_variants": len(preview.build_variants),
        "prerequisites": len(preview.prerequisites),
    }


def _campaign_preview_outcome(previews: Sequence[dict[str, object]]) -> EndpointOutcome:
    """Build the successful, non-mutating dry-run outcome for *previews*."""
    return EndpointOutcome(
        exit_code=EXIT_SUCCESS,
        detail={"campaign_preview": list(previews), "mutated": False},
        report_text="\n".join(
            f"{cast(Mapping[str, object], item['target'])['selector']}: "
            f"{item['work_items']} pending work item(s)"
            for item in previews
        ),
    )


def _link_baseline_prerequisites(
    prerequisites: dict[str, list[Mapping[str, object]]],
    links: Sequence[BaselineLink],
    by_run: Mapping[str, Mapping[str, object]],
) -> None:
    """Give each candidate the baseline observation of its own Test Run.

    Two Test Runs of one candidate cannot read the same baseline work item (one
    Cocotb batch holds several): a campaign links each work item once.
    """
    for link in links:
        assert link.run is not None, "an accepted baseline link has a Test Run"
        document = by_run[link.run.name]
        if document in prerequisites[link.candidate]:
            raise ValueError(
                f"Cycle Count comparison of {link.candidate} Test Run {link.run.name} would "
                f"read a baseline work item of {link.baseline} that another of its Test Runs "
                "already reads; compare these Tests in separate runs"
            )
        prerequisites[link.candidate].append(document)


def _manifest_trace(manifest: SimulationCampaignManifest) -> bool:
    """Whether the campaign *manifest* froze a traced workload."""
    return cast(bool, cast(Mapping[str, object], manifest.document["workload"])["trace"])


def _group_disclosures(
    execution: SimulationExecution,
    handle: TargetHandle,
    groups: Sequence[tuple[str, ...]],
    run_groups: Sequence[tuple[TestRun, ...]],
) -> tuple[dict[str, object], ...]:
    """Disclose the build of each group's Test Runs, not of their Tests' defaults."""
    return tuple(
        execution.plan_ordinary_group(handle, group, runs)
        for group, runs in zip(groups, run_groups, strict=True)
    )


def _manifest_run_groups(
    manifest: SimulationCampaignManifest,
) -> tuple[tuple[TestRun, ...], ...]:
    """The Test Runs of each work item, exactly as the manifest froze them."""
    items = cast(tuple[Mapping[str, object], ...], manifest.document["work_items"])
    return tuple(
        tuple(
            test_run_from_document(entry) for entry in cast(tuple[object, ...], item["test_runs"])
        )
        for item in items
    )


def _is_coverage_manifest(manifest: SimulationCampaignManifest) -> bool:
    """True when resume dispatches the coverage aggregate executor."""
    items = cast(tuple[Mapping[str, object], ...], manifest.document["work_items"])
    return bool(items) and items[0]["kind"] == "coverage_aggregate"


def _unfinished_coverage_work(
    manifest: SimulationCampaignManifest, status: CampaignRecoveryStatus
) -> tuple[str, ...]:
    """Coverage work-item ids without a recorded result; empty for other Campaigns."""
    if not _is_coverage_manifest(manifest):
        return ()
    unfinished = set(status.pending) | set(status.interrupted)
    items = cast(tuple[Mapping[str, object], ...], manifest.document["work_items"])
    return tuple(
        cast(str, item["work_item_id"]) for item in items if item["work_item_id"] in unfinished
    )


def _fresh_campaign_recovery_detail(
    validated: ValidatedResumeManifest,
    observed: CampaignRecoveryStatus,
) -> dict[str, object]:
    """Refresh failure presentation without treating the pre-run view as authority."""
    try:
        status = SimulationCampaign().inspect_resume(validated)
    except (OSError, ValueError) as exc:
        detail = _campaign_recovery_detail(observed)
        detail["recovery_refresh_error"] = str(exc)
        return detail
    return _campaign_recovery_detail(status)


def _campaign_observation_preview(
    observations: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    limit = 32
    preview = [
        {
            "test": item["test"],
            "execution": item["execution"],
            "failure_class": item["failure_class"],
            "functional": item["functional"],
            "assertions": item["assertions"],
            "assertion_count": item["assertion_count"],
            "cycle_count": item.get("cycle_count"),
            "detail": _structured_json(item["detail"]),
        }
        for item in observations[:limit]
    ]
    total = len(observations)
    return {
        "observations": preview,
        "observation_total": total,
        "observations_truncated": total > limit,
        "termination_counts": _campaign_termination_counts(observations),
        "build_stage": _campaign_build_stages(observations),
    }


def _campaign_build_infrastructure_detail(
    outcomes: Sequence[CampaignOutcome],
) -> dict[str, object]:
    """Preserve the endpoint's first-build diagnostic compatibility interface."""
    for outcome in outcomes:
        for item in outcome.observations:
            if item.get("failure_class") != "infrastructure":
                continue
            detail = item.get("detail")
            stage = detail.get("build_stage") if isinstance(detail, Mapping) else None
            if isinstance(stage, Mapping) and stage.get("failure_class") == "infrastructure":
                return {
                    "eda_tool_error": "build_infrastructure",
                    "target": str(outcome.target["selector"]),
                    "build_stage": _structured_json(stage),
                }
    return {}


def _campaign_build_stages(observations: Sequence[Mapping[str, object]]) -> list[object]:
    """Retain bounded, distinct build diagnostics from the complete observation set."""
    stages: list[object] = []
    for item in observations:
        if item.get("failure_class") != "infrastructure":
            continue
        detail = item.get("detail")
        stage = detail.get("build_stage") if isinstance(detail, Mapping) else None
        if isinstance(stage, Mapping):
            projected = _structured_json(stage)
            if projected not in stages and len(stages) < 32:
                stages.append(projected)
    return stages


def _campaign_termination_counts(
    observations: Sequence[Mapping[str, object]],
) -> dict[str, int]:
    """Keep every infrastructure termination visible beyond the bounded preview."""
    counts: dict[str, int] = {}
    for item in observations:
        if item.get("failure_class") != "infrastructure":
            continue
        detail = item.get("detail")
        termination = detail.get("termination") if isinstance(detail, Mapping) else None
        if isinstance(termination, str):
            # Adapter failure codes are external input; aggregate unusual codes together.
            key = termination if len(termination) <= 64 and len(counts) < 32 else "other"
            if termination in counts:
                key = termination
            counts[key] = counts.get(key, 0) + 1
    return counts


def _structured_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _structured_json(item) for key, item in value.items()}
    if isinstance(value, tuple | list):
        return [_structured_json(item) for item in value]
    return value


def _coverage_compatibility_targets(
    outcomes: Sequence[CampaignOutcome],
    report_invocation: Path | None = None,
    coverage_campaigns: Mapping[str, CoverageCampaign] | None = None,
) -> dict[str, object]:
    """Preserve bounded coverage endpoint fields while authority stays referenced."""
    resolved = (
        coverage_campaigns
        if coverage_campaigns is not None
        else _resolved_coverage_campaigns(outcomes)
    )
    targets: dict[str, object] = {}
    for outcome in outcomes:
        if outcome.coverage_reference is None:
            continue
        public = _coverage_campaign_path(outcome)
        selector = str(outcome.target["selector"])
        campaign = resolved[selector]
        target = {
            "target": selector,
            "passed": _campaign_passed(outcome),
            "simulation": simulation_status_from_observations(outcome.observations),
            "collection": campaign.collection["status"],
            "evaluation": campaign.evaluation["status"],
            "coverage_metrics": coverage_metrics(campaign),
            "abort_remaining": False,
        }
        reference = _coverage_reference_artifact(
            public,
            report_invocation,
            str(outcome.acceptance_facts.document["campaign_id"]),
        )
        if reference is not None:
            target["coverage_campaign"] = reference
        targets[selector] = target
    return targets


def _campaign_passed(outcome: CampaignOutcome) -> bool | None:
    status = simulation_status_from_observations(outcome.observations)
    return None if status == "not_run" else outcome.aggregate_grade == "pass"


def _coverage_reference_artifact(
    public: Path, report_invocation: Path | None, owner: str
) -> dict[str, object] | None:
    return _report_artifact_reference(
        public,
        report_invocation=report_invocation,
        external_origin_target=public.parent,
        kind="coverage_campaign_reference",
        owner=owner,
        maximum=MAX_REFERENCE_BYTES,
    )


def _attach_failure_reference(
    detail: dict[str, object],
    public: Path,
    report_invocation: Path | None,
    owner: str,
) -> None:
    try:
        reference = _coverage_reference_artifact(public, report_invocation, owner)
    except (OSError, ValueError):
        return
    if reference is not None:
        detail["coverage_campaign"] = reference


class _CoverageTargetError(RuntimeError):
    """Carry a projected coverage failure across the resume progress lifecycle."""

    def __init__(self, message: str, detail: dict[str, object]) -> None:
        super().__init__(message)
        self.detail = detail


def _observed_failure_detail(
    selector: str, observations: tuple[Mapping[str, object], ...], evaluation: str
) -> dict[str, object]:
    """Report the Simulation truth in `observations` with an infrastructure collection."""
    simulation = simulation_status_from_observations(observations)
    detail: dict[str, object] = {
        "target": selector,
        "passed": None if simulation == "not_run" else simulation == "pass",
        "simulation": simulation,
        "collection": "infrastructure_error",
        "evaluation": evaluation,
    }
    if observations:
        detail["tests"] = [_failure_test(item) for item in observations]
    return detail


def _publication_failure_detail(
    failure: CampaignPublicationFailure, evaluation: str, report_invocation: Path | None
) -> dict[str, object]:
    """Project a publication failure from coordinator-held facts with one nested load."""
    detail = _observed_failure_detail(failure.target_selector, failure.observations, evaluation)
    if failure.nested_campaign is not None:
        try:
            campaign = load_coverage_campaign(failure.nested_campaign).campaign
        except (OSError, ValueError):
            pass  # No committed nested Campaign: keep infrastructure_error and the prepared policy.
        else:
            detail.update(
                collection=_endpoint_collection(campaign.collection["status"]),
                evaluation=campaign.evaluation["status"],
                coverage_metrics=coverage_metrics(campaign),
            )
    if failure.coverage_reference is not None:
        _attach_failure_reference(
            detail,
            failure.target_root / "coverage.json",
            report_invocation,
            str(failure.coverage_reference.document["simulation_campaign_id"]),
        )
    return detail


def _coverage_failed_target(
    failed: str,
    error: Exception,
    evaluation: str,
    outcome: CampaignOutcome | None = None,
    report_invocation: Path | None = None,
) -> dict[str, object]:
    """Use one failure precedence, retaining simulation truth without retrying retention."""
    if isinstance(error, (_CoverageAggregateError, _CoverageTargetError)):
        detail = {str(key): _structured_json(value) for key, value in error.detail.items()}
    elif isinstance(error, CampaignPublicationError):
        detail = _publication_failure_detail(error.failure, evaluation, report_invocation)
    else:
        observations = outcome.observations if outcome is not None else ()
        detail = _observed_failure_detail(failed, observations, evaluation)
    detail.update(error=str(error), abort_remaining=True)
    return detail


def _unmatched_coverage_failure(
    outcomes: Sequence[CampaignOutcome], targets: Mapping[str, object], error: Exception
) -> EndpointOutcome:
    return EndpointOutcome(
        exit_code=EXIT_ERROR,
        detail={
            "coverage": True,
            "campaigns": _campaign_structured_details(outcomes),
            "targets": dict(targets),
        },
        report_text="\n".join(
            [
                "Simulation Campaign coverage execution failed: cannot match failed "
                f"Target to the prepared Coverage plan ({error})",
                *_coverage_campaign_lines(outcomes),
            ]
        ),
    )


def _coverage_failure_outcome(
    outcomes: Sequence[CampaignOutcome],
    targets: Mapping[str, object],
    pending: list[str],
    error: Exception,
    report_invocation: Path | None,
) -> EndpointOutcome:
    return EndpointOutcome(
        exit_code=EXIT_ERROR,
        detail={
            "coverage": True,
            "campaigns": _campaign_structured_details(outcomes, report_invocation),
            "targets": dict(targets),
            "pending_targets": pending,
        },
        report_text="\n".join(
            [
                f"Simulation Campaign coverage execution failed: {error}",
                *_coverage_campaign_lines(outcomes),
            ]
        ),
    )


def _coverage_campaign_exit_code(
    outcomes: Sequence[CampaignOutcome],
    coverage_campaigns: Mapping[str, CoverageCampaign] | None = None,
) -> int:
    """Untrusted coverage forces exit 2; a threshold miss never changes the exit code."""
    resolved = (
        coverage_campaigns
        if coverage_campaigns is not None
        else _resolved_coverage_campaigns(outcomes)
    )
    for outcome in outcomes:
        if outcome.coverage_reference is None:
            continue
        selector = str(outcome.target["selector"])
        campaign = resolved[selector]
        if (
            campaign.collection["status"] != "complete"
            or campaign.evaluation["status"] == "blocked"
        ):
            return EXIT_ERROR
    return 0


def _grade_exit_code(grade: str) -> int:
    """Map an aggregate Simulation grade to an exit code (error 2, any non-pass 1)."""
    if grade == "error":
        return EXIT_ERROR
    return EXIT_SUCCESS if grade == "pass" else EXIT_FAILURE


def _checkpoint_coverage_campaign(
    progress: CoverageProgress, outcome: CampaignOutcome, evaluation: str
) -> None:
    try:
        _retain_coverage_campaign(progress, outcome)
    except (OSError, ValueError) as error:
        detail = _coverage_failed_target(
            str(outcome.target["selector"]), error, evaluation, outcome
        )
        raise _CoverageTargetError(str(error), detail) from error
    progress.checkpoint()


def _retain_coverage_campaign(progress: CoverageProgress, outcome: CampaignOutcome) -> None:
    """Authenticate and retain one completed Target before observational writes."""
    public = outcome.manifest_path.parents[1] / "coverage.json"
    campaign = resolve_coverage_campaign_reference(public).loaded.campaign
    progress.outcomes.append(
        CoverageTargetOutcome(
            str(outcome.target["selector"]),
            _coverage_campaign_exit_code([outcome], {str(outcome.target["selector"]): campaign})
            or _grade_exit_code(outcome.aggregate_grade),
            public,
            public.with_name("simulation.json"),
            campaign.evaluation,
        )
    )


def _build_outcome_entry(outcome: BuildOutcome | None) -> dict[str, Any] | None:
    """JSON shape for one authenticated Simulation build attempt."""
    if outcome is None:
        return None
    return {
        "ran": outcome.ran,
        "verdict": outcome.verdict,
        "failure_class": outcome.failure_kind,
        "returncode": outcome.returncode,
        "elapsed_s": round(outcome.elapsed_s, 3),
        "timed_out": outcome.timed_out,
        "peak_rss_mb": outcome.peak_rss_mb,
        "oom_kill_delta": outcome.oom_kill_delta,
        "terminal_record": outcome.terminal_record,
        "reason": outcome.reason,
        "cache_decision": outcome.cache_decision,
    }


def _build_infrastructure_detail(error: SimulationBuildInfrastructureError) -> dict[str, Any]:
    """Structured endpoint detail for a build that ended without a design verdict."""
    return {
        "eda_tool_error": "build_infrastructure",
        "target": error.target,
        "build_stage": _build_outcome_entry(error.outcome),
    }


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    """Replace *path* atomically so a poll never observes partial JSON."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    replace_file(tmp, path)


def _resolve_sim_timeout_ms(work_dir: Path | None = None) -> int:
    """Per-test simulation timeout in ms, from booley.toml ``[flows.sim]``.

    ``[flows.sim].timeout_ms`` lets a project set a persistent per-test sim
    budget in ``booley.toml`` (mirrors ``[flows.synth].timeout_ms``), so
    a heavy core (e.g. a 400+MB ``vvp`` whose cold rebuild+run exceeds the 600s
    default under load) need not raise it on every call. Precedence lives in the
    caller: an explicit ``--timeout-ms`` arg wins over this knob, which wins over
    :data:`_DEFAULT_TIMEOUT_MS`. Invalid configured values fail at the boundary.
    """
    return resolve_timeout_ms("sim", work_dir, None)


def _get_test_names(work_dir: Path | str) -> dict[str, list[str]]:
    """Load the per-target test names (tests.toml ``tests``)."""
    return load_test_configuration_field(work_dir, "tests")


def _get_test_skips(work_dir: Path | str) -> dict[str, list[str]]:
    """Load per-target known-hang skip lists (tests.toml ``skip``)."""
    return load_test_configuration_field(work_dir, "skip")


def _get_test_envs(work_dir: Path | str) -> dict[str, dict[str, str]]:
    """Load per-Target simulator environment (tests.toml ``env``)."""
    return load_test_configuration_field(work_dir, "env")


def _get_test_selects(work_dir: Path | str) -> dict[str, str]:
    """Load per-target explicit ``select`` templates (tests.toml ``select``).

    Only *explicitly declared* templates appear here (the rendering default,
    ``DEFAULT_TEST_SELECT``, does not) — exactly the set A2 must reject on a
    Cocotb Target, where selection is the ``COCOTB_TEST_FILTER`` env var and a
    plusarg template is a config contradiction.
    """
    return load_test_configuration_field(work_dir, "select")


def _filter_tests(tests: list[str], exact_names: str | Sequence[str]) -> list[str]:
    """Select registered tests by exact name while preserving request order."""
    requested = (exact_names,) if isinstance(exact_names, str) else tuple(exact_names)
    available = set(tests)
    return [name for name in requested if name in available]


def _sizing_identity(work_dir: Path, target: str) -> str:
    """Identity of *target* for sizing; an unresolvable token keeps its bare name.

    The child Flow reports the resolution error, so sizing falls back to the
    bare-name section lookup instead of raising.
    """
    try:
        return TargetCatalog.build(work_dir).select(target, for_flow="sim").identity
    except (fusesoc_registry.FuseSocError, OSError, ValueError):
        return target.rsplit("#", 1)[-1]


def _selected_test_work_units(
    identity: str,
    test_names: Mapping[str, list[str]],
    configured_skips: Mapping[str, list[str]],
    test_selector: Sequence[str] | str | None,
    skip_arg: str | None,
) -> int:
    """Simulator processes a non-cocotb Target will launch, at least one."""
    available = list(lookup_target_section(test_names, identity) or [])
    skips = set(lookup_target_section(configured_skips, identity) or [])
    if skip_arg:
        skips.update(item.strip() for item in skip_arg.split(",") if item.strip())
    if test_selector:
        return max(1, _selector_test_run_count(available, test_selector))
    if not available:
        return 1
    selected = [test for test in available if test not in skips]
    return len(selected or available)


def _selector_test_run_count(available: Sequence[str], test_selector: Sequence[str] | str) -> int:
    """How many Test Runs *test_selector* names among the registered Tests.

    ``test@xN`` counts N. A selector that does not parse, or names an
    unregistered Test, counts nothing: the Flow itself refuses it.
    """
    requested = (test_selector,) if isinstance(test_selector, str) else tuple(test_selector)
    count = 0
    for text in requested:
        try:
            selector = parse_test_selector(text)
        except ValueError:
            continue
        if selector.test in available:
            form = selector.seed_form
            count += form.count if isinstance(form, SeedCount) else 1
    return count


def _resolved_work_units(
    work_dir: Path,
    target: str,
    identity: str,
    registered: Sequence[str],
    skips: Collection[str],
    test_selector: Sequence[str] | None,
    seed: SeedRequest,
) -> int:
    """Simulator processes *target* launches for this request, from its resolved Test Runs.

    The request is resolved the way the Flow resolves it, then grouped into
    processes the way the Flow groups it: one per Test Run on an HDL Target,
    one per environment and seed on a Cocotb Target. A seed Booley would draw
    (``--seed random``, ``test@xN``) is unknown here, so each such Test Run
    counts as its own process, which is the most it can need. Raises
    ``ValueError`` for a request the Flow itself refuses.
    """
    cocotb = _target_is_cocotb(work_dir, target)
    # Sizing only: every HDL simulator launches one process per Test Run, and
    # the seeded one accepts each seed form the real Target might accept.
    plan = load_target_test_plan(
        work_dir, target, identity=identity, adapter="cocotb" if cocotb else "verilator"
    )
    plan = replace(
        plan,
        tests=tuple(registered),
        skipped=tuple(name for name in registered if name in skips),
    )
    drawn: set[int] = set()

    def draw(count: int, *, exclude: Collection[int] = ()) -> tuple[int, ...]:
        taken = drawn | set(exclude)
        seeds = [seed for seed in range(1, count + len(taken) + 1) if seed not in taken][:count]
        drawn.update(seeds)
        return tuple(seeds)

    runs = plan.resolve(test_selector, seed=seed, draw=draw)
    fixed = [run for run in runs if run.seed not in drawn]
    batches = batch_test_runs(fixed, one_process_per_run=not cocotb)
    return len(batches) + len(runs) - len(fixed)


def _sizing_seed(seed: object) -> SeedRequest:
    """The request's ``--seed`` for sizing; a value the Flow refuses counts as none."""
    if seed is None:
        return None
    try:
        return parse_seed_request(seed)
    except ValueError:
        return None


def _resolve_sim_campaign_work_units(
    work_dir: Path,
    target_arg: str,
    test_selector: Sequence[str] | str | None = None,
    skip_arg: str | None = None,
    mode: SimulationMode | str = SimulationMode.SIMULATE,
    seed: object = None,
) -> int:
    """Count sequential simulator processes selected by one sim invocation.

    Counts the work items the request resolves to: its Test Runs, grouped
    into processes as the Flow groups them (``test@xN`` counts N; Cocotb
    batches split by environment and seed, also for a default suite). This
    is a preflight read used by the outer MCP watchdog; validation remains
    the child Flow's responsibility, so a request that does not resolve is
    counted from the Test registry alone.
    """
    targets = [item.strip() for item in target_arg.split(",") if item.strip()]
    if not targets:
        try:
            selection = PreparedTargetSelection.resolve(
                work_dir, "", for_flow="sim", select_omitted=True
            )
        except (fusesoc_registry.FuseSocError, OSError, ValueError):
            return 1
        targets = [handle.selector for handle in selection.handles]
    selected_mode = normalize_simulation_mode(mode)
    if selected_mode is SimulationMode.ELAB_ONLY:
        return len(targets)
    if selected_mode is SimulationMode.ELAB_ONLY_STANDALONE:
        return len(targets) + 1
    test_names = _get_test_names(work_dir)
    configured_skips = _get_test_skips(work_dir)
    selectors = (test_selector,) if isinstance(test_selector, str) else test_selector
    units = 0
    for target in targets:
        identity = _sizing_identity(work_dir, target)
        try:
            registered = list(lookup_target_section(test_names, identity) or [])
            skips = set(lookup_target_section(configured_skips, identity) or [])
        except TargetSectionConflictError:
            units += 1  # the child Flow refuses duplicate section keys
            continue
        skips.update(item.strip() for item in (skip_arg or "").split(",") if item.strip())
        try:
            resolved = _resolved_work_units(
                work_dir,
                target,
                identity,
                registered,
                skips,
                tuple(selectors) if selectors else None,
                _sizing_seed(seed),
            )
        except ValueError:
            resolved = 0
        units += resolved or _selected_test_work_units(
            identity, test_names, configured_skips, test_selector, skip_arg
        )
    return max(1, units)


def _prepared_coverage_display_label(plans: Sequence[CoverageTargetPlan]) -> str | None:
    """Describe the exact Target/test scope selected during coverage preflight."""
    targets = [plan.handle.selector for plan in plans]
    tests = [test for plan in plans for test in plan.selected_tests]
    return format_flow_display_label(targets, tests=tests if tests else None)


class SimulateFlow(StandaloneMixin, BuiltinFlow):
    """Run RTL simulation for one or more Targets."""

    def __init__(
        self,
        *,
        coverage_execution: Callable[
            [TargetHandle, SimulationOptions, tuple[str, ...], str],
            SimulationExecutionPort,
        ]
        | None = None,
        campaign_publication_checkpoint: Callable[[str], None] | None = None,
    ) -> None:
        super().__init__()
        self._coverage_execution_factory = coverage_execution
        self._campaign_publication_checkpoint = campaign_publication_checkpoint

    def _start_invocation(
        self, adapter: FlowExecutionAdapter, request: FlowRequest | None = None
    ) -> None:
        """Start an invocation with no Campaign outcomes: only this Flow keeps them."""
        super()._start_invocation(adapter, request)
        self._simulation_campaign_outcomes: tuple[Any, ...] = ()
        self._simulation_acceptance_outcomes: tuple[Any, ...] = ()

    def begin_invocation(self) -> None:
        """Start this invocation's Test Run decisions afresh.

        Resolved Test Runs (drawn seeds included), baseline
        runs and the Goals' frozen Test Runs belong to one request. A reused Flow
        object must resolve the next request itself, never reuse these.
        """
        self._test_runs = TestRunInvocation()

    request_type = SimRequest
    argument_adapter = SimArguments
    report_schema = "booley.simulation-report/v3"
    target_required = False

    name: str = "sim"
    description: str = (
        "Run RTL simulation for one or more Targets. Set mode=elab_only to "
        "compile, elaborate, and link the ordinary untraced simulator image "
        "without running tests, or mode=elab_only_standalone to add the "
        "reusable-module sweep. "
        "Do NOT use --trace for initial pass/fail checks — tracing adds "
        "overhead and is only useful after a failure, when you need waveforms "
        "for debugging via the B-Wave (`bwave`) MCP tool."
    )
    cli_help: ClassVar[CliHelp | None] = CliHelp(
        summary=(
            "Compile and run the RTL simulation tests of one or more Targets and report "
            "pass or fail for each test. Use it to check that a change still passes its tests. "
            "Add --mode elab-only to check that the design compiles and elaborates "
            "without running tests, or --mode elab-only-standalone to also sweep the "
            "reusable modules on their own. Leave --trace off for pass/fail runs: "
            "tracing slows simulation, so enable it only to capture waveforms of a "
            "failing test, then inspect them with `bwave`."
        ),
        example="booley flow sim --target sim_soc --test reset",
    )
    code_modifying: bool = False

    # Simulation is always admitted as a heavy Sandbox job.
    def _resolve_job_class(self) -> str:
        """Simulation is a heavy Sandbox workload."""
        return job_slots.CLASS_HEAVY

    def _next_invocation_dir(self, report_dir: Path) -> Path:
        """Acquire the Simulation producer lock before publishing its directory."""
        from .campaign_reports import campaign_invocation_lock
        from .campaign_retention import invocation_booley_dir

        endpoint_dir = report_dir / self.name
        endpoint_dir.mkdir(parents=True, exist_ok=True)
        existing = (
            int(path.name.removeprefix(".pruned-"))
            for path in endpoint_dir.iterdir()
            if path.name.removeprefix(".pruned-").isdigit()
        )
        number = max(existing, default=0) + 1
        while number <= 1_000_000:
            invocation = endpoint_dir / str(number)
            # The attempt stack releases the lock on every exit that does not
            # publish the directory: a raced number or any mkdir failure.
            with ExitStack() as attempt:
                try:
                    attempt.enter_context(
                        # The same derivation the artifact references use, so a custom
                        # report root under a linked entry is refused here, up front.
                        campaign_invocation_lock(
                            invocation, booley_dir=invocation_booley_dir(invocation)
                        )
                    )
                    invocation.mkdir()
                except (LockContentionError, FileExistsError):
                    number += 1
                    continue
                self.publication_resources.push(attempt.pop_all())
                return invocation
        raise RuntimeError("Simulation invocation reservation ceiling exceeded")

    def record_flow_owned_acceptance(self, outcome: EndpointOutcome) -> bool:
        """Record Simulation Campaign acceptance; no Campaign outcomes means not handled."""
        from .acceptance import retain_partial_campaign_acceptance

        outcomes = self._simulation_campaign_outcomes
        if not outcomes:
            return False
        try:
            self.record_campaign_acceptance(outcomes)
        except EvidenceDiscarded:
            raise
        except Exception:
            retain_partial_campaign_acceptance(
                outcome, outcomes, self._simulation_acceptance_outcomes
            )
            raise
        return True

    def generic_binding_gate_applies(self) -> bool:
        """A coverage run is authorized by its own preparation, not the generic gate.

        Explicit native collection also supports ungated Targets (#213); in
        simulate mode coverage preparation authorizes the complete selection.
        """
        return not self.args.coverage

    def record_campaign_acceptance(self, outcomes: tuple[object, ...]) -> None:
        """Own campaign-specific reconciliation behind the generic endpoint hook."""
        from .acceptance import record_campaign_acceptance

        record_campaign_acceptance(
            self, outcomes, baseline_refusals=self._test_runs.baseline.refusals
        )

    def _capture_cycle_report(self, outcomes: Sequence[CampaignOutcome]) -> None:
        if self.cycle_report_json is not None:
            return
        try:
            rows = {
                str(outcome.target["selector"]): [
                    {"test": item["test"], "cycle_count": item["cycle_count"]}
                    for item in outcome.observations
                ]
                for outcome in outcomes
            }
            self.cycle_report_json = json.dumps({"cycle_counts": rows}) if rows else "{}"
        except Exception:  # optional metadata cannot change verdict or acceptance
            logger.warning("Simulation cycle count metadata unavailable", exc_info=True)
            self.cycle_report_json = '{"cycle_counts_error":"unavailable"}'

    def refresh_report_detail(self, result: EndpointOutcome) -> None:
        """Bind final projection references after acceptance publication."""
        outcomes = self._simulation_campaign_outcomes
        invocation = self._reserved_invocation_dir
        if outcomes and invocation is not None:
            result.detail["campaigns"] = _campaign_structured_details(outcomes, invocation)

    def report_candidate_workloads(
        self, prepared: PreparedSimulationEndpoint
    ) -> tuple[tuple[CandidateWorkload, ...], bool]:
        """Prepared candidates and their workloads; baseline prerequisites are not candidates."""
        if prepared.resume is None:
            handles = prepared.targets[: len(prepared.selected_targets)]
            coverage = bool(getattr(self.args, "coverage", False))
            # A coverage manifest records the declared suite; any other keeps skips removed.
            skips = {} if coverage else _get_test_skips(self.args.work_dir)
            workloads = tuple(
                self._candidate_workload(
                    handle,
                    {
                        test
                        for test in resolve_target_test_suite(
                            handle.selector,
                            identity=handle.identity,
                            test_names=prepared.test_names_map,
                            test_skips=skips,
                        ).tests
                        if test is not None
                    },
                    {
                        "default" if test is None else test
                        for test in self._resolve_tests_to_run(
                            handle.selector, prepared.test_names_map
                        )
                    },
                )
                for handle in handles
            )
            return workloads, coverage
        manifest = prepared.resume.candidate.manifest
        document: Any = manifest.document
        binding = prepared.resume.binding_for(manifest)
        handle = binding.handle if binding is not None else ReportTarget.from_document(document)
        suite, items = document["required_suite"], document["work_items"]
        registered = set(suite["names"])
        selected = set().union(
            *(set(item["selection"]["names"]) for item in items if item["role"] == "candidate")
        ) or ({"default"} if suite["default_invocation"] else registered)
        coverage = bool(items) and items[0].get("kind") == "coverage_aggregate"
        return (self._candidate_workload(handle, registered, selected),), coverage

    def _candidate_workload(
        self, handle: TargetHandle | ReportTarget, registered: set[str], selected: set[str]
    ) -> CandidateWorkload:
        """Fill an empty selection like the manifest does and attach acceptance eligibility."""
        from .acceptance import eligible_simulation_keys

        selected = selected or ({"default"} if not registered else set())
        eligible = eligible_simulation_keys(
            self.state, handle.identity, handle.selector, handle.name, registered, selected
        )
        return CandidateWorkload(handle, frozenset(selected), frozenset(eligible))

    def prepare_endpoint_invocation(self) -> PreparedInvocation | EndpointOutcome:
        """Prepare a Campaign; elaboration-only modes keep the Target-selection invocation."""
        if self._mode.elaborates_only:
            return super().prepare_endpoint_invocation()
        prepared = self.prepare_simulation_endpoint()
        if isinstance(prepared, EndpointOutcome):
            return prepared
        return SimulationCampaignInvocation(self, prepared)

    def manifest_names_targets(self) -> bool:
        """An explicit resume reads its Targets from the manifest."""
        return self.args.resume_from is not None

    def report_families(self) -> set[str]:
        """Criterion families the selected mode can judge."""
        mode = self._mode
        if mode is SimulationMode.ELAB_ONLY_STANDALONE:
            return {"elab_pass", "elaborate_standalone"}
        if mode is SimulationMode.ELAB_ONLY:
            return {"elab_pass"}
        families = {"sim_pass", "cycle_count"}
        if getattr(self.args, "coverage", False):
            families.add("coverage")
        if self.state._file_path is not None:
            families.add("elab_pass")
        return families

    def conventional_report_families(self) -> set[str]:
        """Families a non-strict Project may judge under their conventional names."""
        if self._mode.elaborates_only:
            return {"elab_pass"}
        if self.state._file_path is None:
            return {"sim_pass"}
        return {"sim_pass", "elab_pass"}

    def prepare_simulation_endpoint(  # noqa: PLR0911 -- ordered pre-admission rejections
        self,
    ) -> PreparedSimulationEndpoint | EndpointOutcome:
        """Resolve and authorize every Target before reservation or admission."""
        from booley.flows.endpoint_admission import authorize_targets

        try:
            prepared = self._prepare_campaign_targets()
            if isinstance(prepared, EndpointOutcome):
                return prepared
            targets, resume, selected_targets, test_names_map = prepared
            if resume is None:
                self.set_report_targets(list(selected_targets))
            if (
                resume is not None
                and (refusal := self._refuse_unfinished_coverage_resume(resume)) is not None
            ):
                return refusal
            if resume is None:
                selection_error = self._validate_prepared_selection(
                    list(selected_targets), test_names_map
                )
                if selection_error is not None:
                    return selection_error
            else:
                resume_error = self._resume_coverage_preflight_if_coverage(resume)
                if resume_error is not None:
                    return resume_error
            self._effective_build_timeout_ms()
            # A malformed conversion budget must refuse before any spawn too.
            resolve_sim_trace_convert_timeout_ms(self.args.work_dir)
            # A malformed Pre-Sim Commands knob must refuse here, before any
            # authorization, admission, build or Pre-Sim spawn.
            resolve_pre_sim_commands(self.args.work_dir)
            if resume is None and getattr(self.args, "coverage", False):
                coverage_error = self._prepare_coverage()
                if coverage_error is not None:
                    return coverage_error
            rejection = authorize_targets(self, targets)
        except (fusesoc_registry.FuseSocError, OSError, ValueError) as exc:
            return EndpointOutcome(
                exit_code=EXIT_ERROR,
                report_text=f"sim campaign preflight failed: {exc}",
            )
        if rejection is not None:
            return rejection
        return PreparedSimulationEndpoint(targets, resume, selected_targets, test_names_map)

    def _refuse_unfinished_coverage_resume(
        self, validated: ValidatedResumeManifest
    ) -> EndpointOutcome | None:
        """Refuse resuming a coverage Campaign whose collection has no result.

        A resumed attempt builds privately and re-runs every test, so it costs
        the same as a new run.  Publication-only resumes (collection complete)
        are untouched, as are all non-coverage Campaigns.
        """
        manifest = validated.manifest
        if not _is_coverage_manifest(manifest):
            return None
        try:
            status = SimulationCampaign().inspect_resume(validated)
        except (OSError, ValueError) as exc:
            return EndpointOutcome(
                exit_code=EXIT_ERROR,
                report_text=f"Simulation Campaign integrity failure: {exc}",
            )
        unfinished = _unfinished_coverage_work(manifest, status)
        if not unfinished:
            return None
        item_id = unfinished[0]
        state = "interrupted" if item_id in status.interrupted else "pending"
        selector = cast(Mapping[str, str], manifest.document["target"])["selector"]
        workload = cast(Mapping[str, object], manifest.document["workload"])
        trace = " --trace" if workload["trace"] is True else ""
        # A raw-coverage run of a gated Target is refused without --diagnostic.
        no_waivers = " --no-waivers --diagnostic" if _manifest_no_waivers(manifest) else ""
        tests = self._frozen_test_run_options(manifest, selector)
        return EndpointOutcome(
            exit_code=EXIT_ERROR,
            report_text=(
                f"sim: cannot resume coverage Simulation Campaign {status.manifest_path}: "
                f"its coverage collection has no recorded result (work item {item_id} is "
                f"{state}; the run was interrupted or is still running). Resuming would "
                "rebuild and re-run every test, so start a new run instead:\n"
                f"  booley flow sim --target {shlex.quote(selector)}{tests} "
                f"--coverage{trace}{no_waivers}\n"
                + (
                    "The command names the Test Runs this campaign froze, so the new "
                    "run repeats their Test Variants and seeds."
                    if tests
                    else "Add the original --test/--tests-file selection if you used one."
                )
            ),
            detail=_campaign_recovery_detail(status),
        )

    def _frozen_test_run_options(self, manifest: SimulationCampaignManifest, selector: str) -> str:
        """``--test`` options that repeat the campaign's Test Runs in a new run.

        Empty when every frozen Test Run is still its Test's default at the
        Default Seed: the plain selection then resolves to the same runs.
        Any other campaign (a Test Variant, a pinned or drawn seed, a default
        that has since changed) is named run by run, so a new run draws and
        resolves nothing again.
        """
        try:
            runs = [run for group in _manifest_run_groups(manifest) for run in group]
        except ValueError:
            return ""
        try:
            plan = self._target_test_plan(selector)
            unchanged = all(plan.is_default_run(run) for run in runs)
        except (fusesoc_registry.FuseSocError, ValueError):
            # Nothing shows the frozen runs are still defaults, so the command
            # names each of them (test_test_run_resolution_errors).
            unchanged = False
        if unchanged or any(run.test is None for run in runs):
            return ""
        return "".join(f" {shlex.quote(f'--test={run.name}')}" for run in runs)

    def _validate_prepared_selection(
        self, targets: list[str], test_names_map: dict[str, list[str]]
    ) -> EndpointOutcome | None:
        """Reject all selection/configuration errors before heavy admission."""
        if (error := self._validate_test_selector(targets, test_names_map)) is not None:
            return error
        if (error := self._validate_cocotb_targets(targets)) is not None:
            return error
        if (error := self._validate_test_plans(targets, test_names_map)) is not None:
            return error
        return self._validate_runnable_tests(targets, test_names_map)

    def run_prepared_simulation(
        self,
        prepared: PreparedSimulationEndpoint,
        admission: object | None,
    ) -> EndpointOutcome:
        """Run one immutable preflight with its explicitly borrowed admission."""
        self._current_published_pre_sim_keys: set[tuple[str, str, str, int]] = set()
        if prepared.resume is not None:
            result = self._with_diagnostic_test_run_note(
                self._run_campaign_resume(prepared.resume, admission)
            )
            return self._attach_published_pre_sim(result, prepared.resume.path)
        result = (
            self._run_coverage_campaigns(admission)
            if getattr(self.args, "coverage", False)
            else self._run_selected_mode(prepared=prepared, admission=admission)
        )
        result.detail["mode"] = self._mode.value
        return self._attach_published_pre_sim(
            self._with_baseline_refusal_note(self._with_diagnostic_test_run_note(result))
        )

    def _record_pre_sim_firing(self, key: tuple[str, str, str, int]) -> None:
        """Record only authenticated firings durably published by this execution."""
        self._current_published_pre_sim_keys.add(key)

    def _attach_published_pre_sim(
        self, result: EndpointOutcome, origin_manifest: Path | None = None
    ) -> EndpointOutcome:
        """Attach authentic hook evidence even when no terminal Campaign outcome exists."""
        from .campaign import collect_pre_sim_firings
        from .campaign_retention import invocation_booley_dir

        invocation = self._reserved_invocation_dir
        if invocation is None:
            return result
        try:
            paths = sorted((invocation / "targets").glob("*/campaign/manifest.json"))
            if len(paths) > 4000:
                raise SimulationCampaignIntegrityError("invocation Campaign count exceeds bound")
            if origin_manifest is not None:
                paths.append(origin_manifest)
            firings = collect_pre_sim_firings(*paths, booley_dir=invocation_booley_dir(invocation))
        except (OSError, ValueError) as exc:
            return self._retain_pre_sim_after_scan_error(result, exc)
        return self._project_published_pre_sim(result, firings, invocation)

    def _project_published_pre_sim(self, result, firings, invocation) -> EndpointOutcome:
        if not firings:
            return result
        firings = {firing.key: firing for firing in firings}
        keys = frozenset(getattr(self, "_current_published_pre_sim_keys", ()))
        detail, card = _pre_sim_projection(firings, keys, invocation)
        result.detail.update(detail)
        result.report_text = "\n".join(
            line for line in result.report_text.splitlines() if not _is_pre_sim_card_line(line)
        )
        return _append_missing_report_lines(result, card)

    def _retain_pre_sim_after_scan_error(
        self, result: EndpointOutcome, error: Exception
    ) -> EndpointOutcome:
        from .campaign import collect_pre_sim_firings
        from .campaign_retention import invocation_booley_dir

        firings = tuple(
            firing
            for outcome in self._simulation_campaign_outcomes
            for firing in outcome.pre_sim_firings
        )
        retained = False
        invocation = self._reserved_invocation_dir
        if firings:
            try:
                result = self._project_published_pre_sim(
                    result,
                    collect_pre_sim_firings(
                        prior=firings,
                        booley_dir=None
                        if invocation is None
                        else invocation_booley_dir(invocation),
                    ),
                    invocation,
                )
                retained = True
            except (OSError, ValueError):
                pass
        self._simulation_campaign_outcomes = ()
        self._pending_criteria_set = ()
        self._report_criteria.evaluated.clear()
        self._report_criteria.known = False
        return _pre_sim_integrity_failure(result, error, retain_authenticated=retained)

    def _prepare_campaign_targets(
        self,
    ) -> (
        tuple[
            tuple[TargetHandle, ...],
            ValidatedResumeManifest | None,
            tuple[str, ...],
            dict[str, list[str]],
        ]
        | EndpointOutcome
    ):
        if self.args.resume_from is not None:

            def revision_root(target: Mapping[str, str]) -> Path:
                if target["role"] == "candidate":
                    return Path(self.args.work_dir)
                return self._shared_baseline_worktree(target["revision"])

            resume = validate_resume_manifest(
                self.args.resume_from,
                project_root=Path(self.args.work_dir),
                revision_root=revision_root,
            )
            return resume.target_handles, resume, (), {}
        requested = self._resolve_requested_targets()
        if isinstance(requested, EndpointOutcome):
            return requested
        if (missing := self._validate_interactive_args(requested)) is not None:
            return missing
        selected = [self._target_handle(target) for target in requested]
        baseline_ref, baseline_targets, baseline_error = self._cycle_baseline_selection(requested)
        if baseline_error is not None:
            return EndpointOutcome(exit_code=EXIT_ERROR, report_text=baseline_error)
        baselines = self._campaign_authorization_baselines(baseline_ref, baseline_targets)
        return (*selected, *baselines), None, tuple(requested), _get_test_names(self.args.work_dir)

    def _campaign_authorization_baselines(
        self, revision: str | None, selectors: Sequence[str]
    ) -> tuple[TargetHandle, ...]:
        """Resolve Criterion baselines from their historical checkout."""
        if revision is None or not selectors:
            return ()
        catalog = TargetCatalog.build(self._shared_baseline_worktree(revision))
        return tuple(catalog.select(selector, for_flow="sim") for selector in selectors)

    def _shared_baseline_worktree(self, revision: str) -> Path:
        """This invocation's one checkout of *revision*, open until publication ends.

        Campaign authorization, Cycle Count planning, campaign preview and
        execution all need the baseline revision. The scratch path depends
        only on the process and revision, so a second opening while the first
        is held fails in ``git worktree add`` (#1959); they share this one.
        Consumers run in that order and only execution writes (build output),
        so sharing never shows a consumer another's changes. Call from the
        candidate checkout: ``self.args.work_dir`` is the Project Root.
        """
        worktrees = self.baseline_worktrees
        if revision not in worktrees:
            resources = self.publication_resources
            worktrees[revision] = resources.enter_context(
                baseline_worktree(
                    Path(self.args.work_dir),
                    revision,
                    paired_project=self._paired_project_baseline,
                )
            )
            # Unwinds before the worktree is removed: no entry outlives its checkout.
            resources.callback(worktrees.pop, revision)
        return worktrees[revision]

    satisfies: ClassVar[list[str]] = [
        "elab_pass",
        "elaborate_standalone",
        "sim_pass",
        "cycle_count",
        "coverage",
    ]
    satisfies_args: ClassVar[dict[str, str]] = {
        "coverage": "--coverage",
        "elab_pass": "--mode elab-only",
        "elaborate_standalone": "--mode elab-only-standalone",
    }
    # The MCP server computes mode-aware build, hook, execution, and finalization
    # headroom. This class default is only its defensive floor.
    default_timeout: ClassVar[int] = (_DEFAULT_TIMEOUT_MS // 1000) * 2 + TRACE_CLEANUP_MARGIN_S

    @property
    def _mode(self) -> SimulationMode:
        """Effective mode without erasing whether the caller supplied it."""
        return self.args.mode or SimulationMode.SIMULATE

    def _resolve_display_label(self) -> str | None:
        """Describe the requested Target/test scope without validating it."""
        targets = self._requested_targets()
        if self._mode.elaborates_only:
            return format_flow_display_label(targets, mode="elaboration")
        if getattr(self.args, "coverage", False) and (
            prepared := getattr(self, "_coverage_prepared", None)
        ):
            return _prepared_coverage_display_label(prepared.targets)
        try:
            test_names = _get_test_names(self.args.work_dir)
            selected = (
                [name for _target in targets for name in self.args.test]
                if self.args.test
                else [
                    test
                    for target in targets
                    for test in self._resolve_tests_to_run(target, test_names)
                ]
            )
        except Exception:  # display metadata must not block a Flow run
            logger.debug("could not resolve simulation display scope", exc_info=True)
            return format_flow_display_label(targets, tests=None)
        if any(test is None for test in selected):
            return format_flow_display_label(targets, tests=None)
        return format_flow_display_label(targets, tests=(str(test) for test in selected))

    def _build_command(self) -> list[str]:
        # Not used — _run() is overridden for multi-config logic
        return []

    def _interpret_result(self, result: SubprocessResult) -> EndpointOutcome:
        # Not used — _run() handles interpretation directly
        return EndpointOutcome()

    def _effective_timeout_ms(self) -> int:
        """Resolve the per-test timeout in ms.

        Precedence: explicit ``--timeout-ms`` CLI/MCP arg > ``[flows.sim]``
        ``timeout_ms`` in booley.toml (F4) > :data:`_DEFAULT_TIMEOUT_MS`. Mirrors
        ``AsicSynthesizeFlow._timeout_ms`` so a large core can persist a higher
        sim budget instead of passing ``--timeout-ms`` on every call.
        """
        return self._timeout_ms()

    def _effective_build_timeout_ms(self) -> int:
        """Resolve and freeze the invocation-wide simulator-image build budget."""
        cached = getattr(self, "_resolved_build_timeout_ms", None)
        if cached is None:
            cached = resolve_sim_build_timeout_ms(self.args.work_dir)
            self._resolved_build_timeout_ms = cached
        return cast(int, cached)

    def _resource_flags(self) -> tuple[str, ...]:
        """Coverage and tracing change a simulation's footprint; key history by them."""
        return tuple(
            flag
            for flag, enabled in (("coverage", self.args.coverage), ("trace", self.args.trace))
            if enabled
        )

    def _get_timeout(self) -> int:
        """Wrapper timeout in seconds, derived from the effective timeout (ms).

        The child runner owns the actual simulator wall-clock budget. Every
        caller that spawns a traced simulation passes its own explicit timeout.
        """
        return self._effective_timeout_ms() // 1000

    def _cocotb_module_for_target(self, target: str) -> str | None:
        """The Target's declared ``cocotb_module``, from a cheap ``.core`` read.

        ADR 0034 decision 2: cocotb-ness is detected from the Target's flow
        options, never marked in tests.toml.
        This cheap read backs validation, dry-run previews and the batch-vs-loop
        dispatch *before* resolution; the run itself re-reads the *resolved*
        flow options (``ResolvedTarget.cocotb_module``) as the authority
        (ADR 0022 decision 6's enumerate-vs-resolve line). ``None`` for a
        non-cocotb Target or when the read fails (degrades to the SV path).
        """
        try:
            handle = self._target_handle(target)
            options = TargetCatalog.build(self.args.work_dir).inspect(handle).flow_options
        except Exception:  # noqa: BLE001 — best-effort cheap read; degrades to non-cocotb
            return None
        module = options.get("cocotb_module")
        return module if isinstance(module, str) and module else None

    def is_cocotb_target(self, target: str) -> bool:
        """True when *target* declares a ``cocotb_module`` (a Cocotb Target)."""
        return self._cocotb_module_for_target(target) is not None

    def _target_sim_env(self, target: str) -> dict[str, str]:
        """*target*'s declared simulator environment (tests.toml ``env``, F-5).

        The Booley answer to an env-parameterized testbench — a cocotb module
        that branches on ``os.getenv("FLAVOR")``, or an SV TB whose C++ main
        reads a config var. Neither existing mechanism covers it:
        ``pre_run_commands`` run in a *separate* shell whose exports die with
        it, and the test filter selects tests, not configuration. Without this
        knob a port has to edit a testbench it does not own.

        Per-Target because that is where the variance lives: the same cocotb
        module run under two RTL flavours is two Targets, and each declares its
        own value. ``{}`` when the Target declares none — the run then inherits
        the ambient environment exactly as before.
        """
        return dict(
            lookup_target_section(
                _get_test_envs(self.args.work_dir), self._target_identity(target)
            )
            or {}
        )

    def _sim_env_preview_lines(self, target: str) -> list[str]:
        """Shell export lines for the Target's configured simulator environment."""
        return [
            f"export {name}={shlex.quote(value)}"
            for name, value in self._target_sim_env(target).items()
        ]

    def _validate_interactive_args(
        self,
        targets: list[str],
    ) -> EndpointOutcome | None:
        """Interactive-Mode guard: a Target must be selected.

        ``tb_top`` left the surface (ADR 0021) — a sim Target's ``toplevel`` is
        its TB top, so it comes from the resolved Target (via
        ``tb_top_for_target``), not a per-call ``--tb-top``. Nothing to validate
        here beyond config selection; the Target resolution downstream raises a
        clear error if a selected Target is malformed.
        """
        if not targets:
            return EndpointOutcome(
                exit_code=EXIT_ERROR,
                report_text=(
                    "sim: --target is required when multiple or zero "
                    "Targets are available. Pass --target <name>; available "
                    "Targets are the .core sim Targets in .booley/."
                ),
            )
        return None

    def _maybe_dispatch_special_run(  # noqa: PLR0911 -- ordered endpoint gates
        self,
        targets: list[str],
        test_names_map: dict[str, list[str]],
    ) -> EndpointOutcome | None:
        """Handle validation + the dry-run preview; ``None`` means "continue".

        Validates ``--test`` the way ``--target`` is already validated
        (``resolve_target_selection`` raises ``UnknownTargetError``) — an
        unknown name here otherwise falls through to a plusarg-less run of
        the TB's default test and reports a false PASS (a CI green on a typo).
        """
        test_error = self._validate_test_selector(targets, test_names_map)
        if test_error is not None:
            return test_error

        cocotb_error = self._validate_cocotb_targets(targets)
        if cocotb_error is not None:
            return cocotb_error

        plan_error = self._validate_test_plans(targets, test_names_map)
        if plan_error is not None:
            return plan_error
        runnable_error = self._validate_runnable_tests(targets, test_names_map)
        if runnable_error is not None:
            return runnable_error

        plan = self._plan_simulation(targets, test_names_map)
        if plan.aggregate_errors:
            return EndpointOutcome(
                exit_code=EXIT_ERROR,
                report_text="sim planning failed: " + "; ".join(plan.aggregate_errors),
                detail={"mode": self._mode.value, "plan": plan.as_dict()},
            )
        if self.args.dry_run:
            return self._preview_ordinary_campaigns(targets, test_names_map)

        return None

    def _plan_simulation(
        self,
        targets: list[str],
        test_names_map: dict[str, list[str]],
    ) -> FlowPlan:
        """Resolve all Simulation work units before any executable is dispatched."""
        baseline_units, baseline_errors = self._plan_cycle_count_baseline_units(
            targets,
            test_names_map,
        )
        candidate_role: WorkUnitRole = "candidate" if baseline_units else "ordinary"
        candidate_units, candidate_errors = self._plan_simulation_targets(
            targets,
            test_names_map,
            role=candidate_role,
            revision=None,
        )
        return FlowPlan(
            flow="sim",
            mode=self._mode.value,
            work_units=(*baseline_units, *candidate_units),
            aggregate_errors=(*baseline_errors, *candidate_errors),
        )

    def _plan_simulation_targets(
        self,
        targets: list[str],
        test_names_map: dict[str, list[str]],
        *,
        role: WorkUnitRole,
        revision: str | None,
        selections: Mapping[str, SimulationSelection] | None = None,
    ) -> tuple[list[WorkUnitPlan], list[str]]:
        """Plan one checkout's selected Simulation Targets.

        *selections* gives a Target the exact selection to plan instead of
        the request's own (a Cycle Count baseline runs the candidate's Test
        Runs, not what its checkout would resolve).
        """
        units: list[WorkUnitPlan] = []
        errors: list[str] = []
        execution = self._simulation_execution()
        for target in targets:
            try:
                preview = execution.preview(
                    self._target_handle(target),
                    selections[target]
                    if selections is not None
                    else self._test_run_selection(target, test_names_map),
                )
                target_units = [
                    self._simulation_work_unit(
                        target, preview, group, command, role, revision, runs
                    )
                    for group, runs, command in zip(
                        preview.groups, preview.run_groups, preview.commands, strict=True
                    )
                ]
            except (
                fusesoc_registry.FuseSocError,
                SimulationBuildPreparationError,
                OSError,
                ValueError,
            ) as exc:
                errors.append(f"{target}: {exc}")
                continue
            units.extend(target_units)
        return units, errors

    def _simulation_work_unit(
        self,
        target: str,
        preview: SimulationPreview,
        group: tuple[str, ...],
        command: tuple[str, ...],
        role: WorkUnitRole,
        revision: str | None,
        runs: tuple[TestRun, ...] = (),
    ) -> WorkUnitPlan:
        """Normalize one resolved Simulation command into the shared plan model."""
        build_root = preview_generation_root(
            self._target_handle(target), "trace" if self.args.trace else ""
        )
        sources = normalize_plan_paths(preview.sources, self.args.work_dir)
        constraints = normalize_plan_paths(preview.constraints, self.args.work_dir)
        environment = self._target_sim_env(target)
        redacted = self._redact_plan_environment(target, command)
        run_log = normalize_plan_path(build_root / RUN_LOG_NAME, self.args.work_dir)
        recipe = {
            "build_timeout_ms": self._effective_build_timeout_ms(),
            "environment_fingerprint": plan_value_fingerprint(environment),
            "flow_options": preview.flow_options,
            "mode": self._mode.value,
            "result_verbosity": self.args.result_verbosity,
            "toplevel": preview.toplevel,
            "trace": self.args.trace,
        }
        return WorkUnitPlan(
            unit_id=stable_unit_id(
                "sim",
                target,
                self._unit_scope(target, group, runs),
                role=role,
                revision=revision,
            ),
            role=role,
            revision=revision,
            selector=target,
            target_identity=preview.target_identity,
            test_or_module_scope=group,
            eda_tool=preview.eda_tool,
            timeout_ms=self._effective_timeout_ms(),
            sources=sources,
            constraints=constraints,
            parameters=preview.parameters,
            recipe=recipe,
            commands=(CommandPlan(normalize_plan_argv(redacted, self.args.work_dir), ".", True),),
            expected_artifacts=(run_log,),
        )

    def _unit_scope(
        self, target: str, group: tuple[str, ...], runs: tuple[TestRun, ...]
    ) -> tuple[str, ...]:
        """What identifies one work unit of *target*: its Test Run names.

        A unit of default Test Runs keeps its bare Test names, so it is the
        same unit whether or not the Target is seeded. Any other unit is named
        by its Test Runs: ``test@xN`` plans N units of one Test.
        """
        if not runs:
            return group
        plan = self._target_test_plan(target)
        if all(plan.is_default_run(run) or run.test is None for run in runs):
            return group
        return tuple(run.name for run in runs)

    def _redact_plan_environment(
        self,
        target: str,
        command: tuple[str, ...],
    ) -> tuple[str, ...]:
        """Retain configured variable names while excluding their values."""
        if len(command) != 3 or command[:2] != ("sh", "-c"):
            return command
        script = command[2]
        for name, value in self._target_sim_env(target).items():
            script = script.replace(
                f"export {name}={shlex.quote(value)}",
                f"export {name}=<configured>",
            )
        return (*command[:2], script)

    def _plan_cycle_count_baseline_units(
        self,
        targets: list[str],
        test_names_map: dict[str, list[str]],
    ) -> tuple[list[WorkUnitPlan], list[str]]:
        """Plan the baseline Simulations: the candidate's Test Runs that can run there."""
        baseline_ref, baseline_targets, error = self._cycle_baseline_selection(targets)
        if error is not None:
            return [], [error]
        if baseline_ref is None:
            return [], []
        expected = {target: self._target_handle(target).identity for target in baseline_targets}
        baseline_tests = self._cycle_baseline_test_names(test_names_map)
        links = self._baseline_links(targets, test_names_map)
        try:
            worktree = self._shared_baseline_worktree(baseline_ref)
            with self._baseline_checkout(worktree, links, baseline_ref) as baseline:
                planned = [target for target in baseline_targets if baseline.runs_of(target)]
                units, errors = self._plan_simulation_targets(
                    planned,
                    baseline_tests,
                    role="baseline",
                    revision=baseline_ref,
                    selections={
                        target: SelectedTestRuns(baseline.runs_of(target)) for target in planned
                    },
                )
                mismatches = [
                    f"{unit.selector}: baseline resolves to {unit.target_identity!r}, "
                    f"expected {expected[unit.selector]!r}"
                    for unit in units
                    if unit.target_identity != expected[unit.selector]
                ]
                return units, [*errors, *mismatches]
        except (BaselineWorktreeError, fusesoc_registry.FuseSocError, OSError, ValueError) as exc:
            return [], [f"sim: Cycle Count baseline planning failed: {exc}"]

    def _baseline_links(
        self, targets: Sequence[str], test_names_map: Mapping[str, list[str]]
    ) -> tuple[BaselineLink, ...]:
        """Every relative Cycle Count comparison of *targets*, with its Test Run.

        A relative Cycle Count compares one Test Run on two revisions, so each
        link carries one concrete candidate Test Run (the same Test Variant
        arguments, environment and seed) for the baseline to run. A Test the
        candidate runs several times (``test@x3``, two Test Variants) gets a
        link per Test Run, so each has its own baseline run or its own named
        refusal. Call in the candidate checkout.
        """
        links: list[BaselineLink] = []
        for candidate, baseline, test in self._campaign_baseline_requirements(targets):
            runs = self._candidate_test_runs(candidate, test, test_names_map)
            # One link per candidate Test Run; without any, one that is refused by name.
            links.extend(BaselineLink(candidate, baseline, test, run) for run in runs or (None,))
        return tuple(links)

    def _candidate_test_runs(
        self, candidate: str, test: str, test_names_map: Mapping[str, list[str]]
    ) -> tuple[TestRun, ...]:
        """Every Test Run of *test* this invocation runs on *candidate*."""
        try:
            runs = self._resolve_test_runs(candidate, test_names_map)
        except (fusesoc_registry.FuseSocError, ValueError):
            # Nothing to compare: the link is refused by name and no baseline
            # runs for it (test_test_run_resolution_errors).
            return ()
        return tuple(run for run in runs if run.test == test)

    @contextmanager
    def _baseline_checkout(
        self, worktree: Path, links: Sequence[BaselineLink], revision: str
    ) -> Iterator[BaselineRuns]:
        """Work in the baseline checkout with the candidate Test Runs it can run.

        Decides there which comparisons the baseline revision can run, keeps
        the decision for the rest of the invocation, and restores the
        candidate checkout on exit.
        """
        project_root = Path(self.args.work_dir)
        self.args.work_dir = worktree
        try:
            self._test_runs.baseline = decide_baseline_runs(
                links, self._target_test_plan, revision
            )
            yield self._test_runs.baseline
        finally:
            self.args.work_dir = project_root

    def _with_baseline_refusal_note(self, result: EndpointOutcome) -> EndpointOutcome:
        """Say which Cycle Count comparisons are inconclusive, and why."""
        refused = self._test_runs.baseline.refused
        if not refused:
            return result
        result.detail["cycle_count_baseline_inconclusive"] = [
            {
                "target": item.link.candidate,
                "test": item.link.test,
                **({"test_run": item.link.run.name} if item.link.run is not None else {}),
                "reason": item.reason,
            }
            for item in refused
        ]
        note = "\n".join(_baseline_refusal_line(item) for item in refused)
        result.report_text = f"{result.report_text}\n{note}" if result.report_text else note
        return result

    def _run(self) -> EndpointOutcome:
        """Run an elaboration-only mode; simulate mode always runs a prepared Campaign."""
        # prepare_endpoint_invocation sends every simulate-mode coverage run to
        # run_prepared_simulation; only elaboration-only modes reach this path.
        result = (
            EndpointOutcome(exit_code=2, report_text="Coverage requires simulation mode")
            if getattr(self.args, "coverage", False)
            else self._validate_mode_args() or self._run_elab_only()
        )
        result.detail["mode"] = self._mode.value
        return self._with_baseline_refusal_note(self._with_diagnostic_test_run_note(result))

    def _run_campaign_resume(
        self, validated: ValidatedResumeManifest, admission: object | None
    ) -> EndpointOutcome:
        """Validate and report one exact durable campaign resume."""
        try:
            observed = SimulationCampaign().inspect_resume(validated)
        except (OSError, ValueError) as exc:
            return EndpointOutcome(
                exit_code=EXIT_ERROR,
                report_text=f"Simulation Campaign integrity failure: {exc}",
            )
        if self.args.dry_run:
            return self._preview_campaign_resume(validated, observed)
        self._record_campaign_tool(validated.manifest)
        return self._execute_campaign_resume(validated, admission, observed)

    def _preview_campaign_resume(
        self, validated: ValidatedResumeManifest, observed: CampaignRecoveryStatus
    ) -> EndpointOutcome:
        origin = cast(Mapping[str, object], validated.manifest.document["origin"])
        try:
            current_plan = self._resume_campaign_plan(
                validated, cast(int, origin["invocation_id"])
            )
            preview = SimulationCampaign().preview(
                ResumeCampaignPreviewRequest(
                    validated,
                    current_plan,
                    Path(self.args.work_dir),
                    self.args.report_dir,
                    self._campaign_policy(),
                )
            )
        except (OSError, ValueError, RuntimeError) as exc:
            return EndpointOutcome(
                exit_code=EXIT_ERROR,
                report_text=f"Simulation Campaign resume preview failed: {exc}",
                detail=_campaign_recovery_detail(observed),
            )
        assert isinstance(preview, ResumeCampaignPreview)
        status = _campaign_recovery_detail(preview.recovery)
        status.update(preview.diagnostic.detail)
        status["required_bundle_variants"] = list(preview.required_bundle_variants)
        lines = [
            f"manifest: {validated.path}",
            f"completed: {len(preview.recovery.completed)}",
            f"interrupted: {len(preview.recovery.interrupted)}",
            f"pending: {len(preview.recovery.pending)}",
            f"mismatches: {len(preview.mismatches)}",
        ]
        if preview.mismatches:
            lines.append(preview.diagnostic.report(verbose=self.args.verbose))
        return EndpointOutcome(
            exit_code=EXIT_ERROR if preview.mismatches else EXIT_SUCCESS,
            detail=status,
            report_text="\n".join(lines),
        )

    def _execute_campaign_resume(self, validated, admission, observed) -> EndpointOutcome:
        return self._with_live_progress(
            lambda: self._execute_observed_campaign_resume(validated, admission, observed)
        )

    def _execute_observed_campaign_resume(
        self,
        validated: ValidatedResumeManifest,
        admission: object | None,
        observed: CampaignRecoveryStatus,
    ) -> EndpointOutcome:
        assert self.args.report_dir is not None, "prepared Flow requires a report root"
        if not isinstance(admission, AdmissionContext):
            return EndpointOutcome(
                exit_code=EXIT_ERROR,
                report_text="sim: no admission context",
                detail=_campaign_recovery_detail(observed),
            )
        invocation = self.reserve_invocation_dir()
        assert invocation is not None
        dependency_error = self._prepare_campaign_resume_dependency(
            validated, invocation, observed
        )
        if dependency_error is not None:
            return dependency_error
        published: list[Path] = []
        try:
            outcome, progress_error = self._execute_validated_resume(
                validated, invocation, admission, self._manifest_publisher(published)
            )
        except SimulationCampaignCancellationError as exc:
            return self._with_campaign_paths(
                self._campaign_cancelled_outcome(
                    exc,
                    _fresh_campaign_recovery_detail(validated, observed),
                ),
                published,
            )
        except (OSError, ValueError, RuntimeError) as exc:
            return self._with_campaign_paths(
                self._campaign_resume_failure(validated, observed, invocation, exc), published
            )
        result = self._campaign_endpoint_outcome([outcome])
        result.detail.update(_campaign_recovery_detail(outcome.recovery))
        if progress_error is not None:
            normalize_completion_error(
                result,
                progress_error,
                "publish Simulation progress",
                path=invocation / "progress.json",
            )
        return result

    def _prepare_campaign_resume_dependency(
        self,
        validated: ValidatedResumeManifest,
        invocation: Path,
        observed: CampaignRecoveryStatus,
    ) -> EndpointOutcome | None:
        from .campaign_reports import campaign_invocation_lock

        # The destination lock is already held; also retain the resumed origin.
        try:
            self.publication_resources.enter_context(
                campaign_invocation_lock(
                    validated.path.parents[3], booley_dir=validated.booley_dir
                )
            )
        except (OSError, ValueError) as exc:
            return EndpointOutcome(
                exit_code=EXIT_ERROR,
                report_text=f"Simulation Campaign resume origin is busy: {exc}",
                detail=_campaign_recovery_detail(observed),
            )
        try:
            from .campaign.dependency import register_campaign_dependency

            register_campaign_dependency(
                validated.path,
                campaign_id=cast(str, validated.manifest.document["campaign_id"]),
                manifest_sha256=validated.sha256,
                dependent_invocation=invocation,
            )
        except (OSError, ValueError) as exc:
            return EndpointOutcome(
                exit_code=EXIT_ERROR,
                report_text=f"Simulation Campaign resume dependency failed: {exc}",
                detail=_campaign_recovery_detail(observed),
            )
        return None

    def _campaign_resume_failure(
        self,
        validated: ValidatedResumeManifest,
        observed: CampaignRecoveryStatus,
        invocation: Path,
        error: Exception,
    ) -> EndpointOutcome:
        detail = _fresh_campaign_recovery_detail(validated, observed)
        message = str(error)
        if isinstance(error, SimulationCampaignWorkloadMismatchError):
            detail.update(error.diagnostic.detail)
            message = error.diagnostic.report(verbose=self.args.verbose)
        if isinstance(error, ProgressPublicationError):
            detail["progress_error"] = str(error)
        if isinstance(error, SimulationBuildInfrastructureError):
            detail.update(_build_infrastructure_detail(error))
        if isinstance(error, (_CoverageAggregateError, _CoverageTargetError)):
            selector = str(error.detail["target"])
            detail["targets"] = {
                selector: _coverage_failed_target(
                    selector, error, str(error.detail["evaluation"]), report_invocation=invocation
                )
            }
        return EndpointOutcome(
            exit_code=EXIT_ERROR,
            report_text=f"Simulation Campaign resume failed: {message}",
            detail=detail,
        )

    def _execute_validated_resume(
        self,
        validated: ValidatedResumeManifest,
        invocation: Path,
        admission: AdmissionContext,
        manifest_published: Callable[[Path], None],
    ) -> tuple[CampaignOutcome, Exception | None]:
        coverage_plan = (
            self._resume_coverage_target_plan(validated)
            if _is_coverage_manifest(validated.manifest)
            else None
        )
        if coverage_plan is None:
            return self._execute_ordinary_resume_progress(
                validated, invocation, admission, manifest_published
            )
        progress = CoverageProgress(invocation, (coverage_plan.handle.selector,))
        lifecycle = ProgressLifecycle(
            lambda phase: progress.checkpoint(complete=True, phase=phase)
        )
        outcome: CampaignOutcome | None = None
        try:
            with lifecycle:
                progress.checkpoint()
                outcome = self._run_validated_resume_campaign(
                    validated, invocation, admission, coverage_plan, manifest_published
                )
                self._capture_cycle_report((outcome,))
                _checkpoint_coverage_campaign(
                    progress,
                    outcome,
                    "not_requested" if coverage_plan.criterion is None else "blocked",
                )
                supersede_progress(
                    validated.path.parents[3] / "progress.json",
                    new_invocation=int(invocation.name),
                    new_run_id=os.environ.get("BOOLEY_RUN_ID", ""),
                    booley_dir=validated.booley_dir,
                )
                lifecycle.complete()
                return outcome, None
        except ProgressPublicationError as exc:
            if outcome is None:
                raise
            failure = exc.__cause__ if isinstance(exc.__cause__, Exception) else exc
            return outcome, failure

    def _execute_ordinary_resume_progress(
        self,
        validated: ValidatedResumeManifest,
        invocation: Path,
        admission: AdmissionContext,
        manifest_published: Callable[[Path], None],
    ) -> tuple[CampaignOutcome, Exception | None]:
        """Retain resume outcomes even when required terminal publication fails."""
        target = cast(Mapping[str, object], validated.manifest.document["target"])
        selector = str(target["selector"])
        outcomes: list[CampaignOutcome] = []
        lifecycle = ProgressLifecycle(
            lambda phase: self._write_campaign_progress(
                (selector,), outcomes, phase=phase, complete=True
            )
        )
        outcome: CampaignOutcome | None = None
        try:
            with lifecycle:
                self._write_campaign_progress(
                    (selector,), outcomes, phase="starting", complete=False
                )
                with attempt_scope(
                    selector,
                    identity=f"{target['vlnv']}#{target['name']}",
                    operation="publication",
                ):
                    outcome = self._run_validated_resume_campaign(
                        validated, invocation, admission, None, manifest_published
                    )
                outcomes.append(outcome)
                self._write_campaign_progress(
                    (selector,), outcomes, phase="running", complete=False
                )
                lifecycle.complete()
                return outcome, None
        except ProgressPublicationError as exc:
            if outcome is None:
                raise
            failure = exc.__cause__ if isinstance(exc.__cause__, Exception) else exc
            return outcome, failure

    def _run_validated_resume_campaign(
        self,
        validated: ValidatedResumeManifest,
        invocation: Path,
        admission: AdmissionContext,
        coverage_plan: CoverageTargetPlan | None,
        manifest_published: Callable[[Path], None],
    ) -> CampaignOutcome:
        plan = self._resume_campaign_plan(
            validated, int(invocation.name), coverage_plan=coverage_plan
        )
        verify_workload(validated.manifest, plan.manifest)
        manifest_published(validated.path)
        executor = self._resume_campaign_executor(coverage_plan)
        try:
            return SimulationCampaign(
                executor, publication_checkpoint=self._campaign_publication_checkpoint
            ).run(
                ResumeCampaignRunRequest(
                    validated,
                    plan,
                    Path(self.args.work_dir),
                    self.args.report_dir,
                    self._campaign_policy(),
                    invocation,
                    admission,
                    pre_sim_firing_published=self._record_pre_sim_firing,
                )
            )
        except CampaignPublicationError as error:
            if coverage_plan is None:
                raise
            detail = _coverage_failed_target(
                coverage_plan.handle.selector,
                error,
                "not_requested" if coverage_plan.criterion is None else "blocked",
                report_invocation=invocation,
            )
            raise _CoverageTargetError(str(error), detail) from error

    def _resume_campaign_executor(self, coverage_plan: CoverageTargetPlan | None):
        if coverage_plan is None:
            return OrdinaryHdlSerialExecutor(invoke=self._execute_boundary)
        return CoverageAggregateExecutor(
            plans={coverage_plan.handle.identity: coverage_plan},
            execution_factory=self._coverage_campaign_execution,
            publication_checkpoint=self._campaign_publication_checkpoint,
        )

    def _resume_campaign_plan(
        self,
        validated: ValidatedResumeManifest,
        invocation_id: int,
        *,
        coverage_plan: CoverageTargetPlan | None = None,
    ):
        """Recompute current workload identity for exact resume comparison."""
        manifest = validated.manifest
        binding = validated.binding_for(manifest)
        handle = binding.handle if binding is not None else validated.target_handles[0]
        inspection = TargetCatalog.build(handle.project_root).inspect(handle)
        items = cast(tuple[Mapping[str, object], ...], manifest.document["work_items"])
        groups = tuple(
            cast(tuple[str, ...], cast(Mapping[str, object], item["selection"])["names"])
            for item in items
        )
        names = tuple(name for group in groups for name in group)
        # Resume runs the manifest's own Test Runs: nothing is resolved from
        # tests.toml again and no seed is drawn again.
        run_groups = _manifest_run_groups(manifest)
        runs = tuple(run for group in run_groups for run in group)
        execution = self._simulation_execution()
        preview = execution.preview(handle, SelectedTestRuns(runs))
        is_coverage = _is_coverage_manifest(manifest)
        if is_coverage:
            preview = replace(preview, groups=(names,), run_groups=(runs,))
        disclosures = (
            () if is_coverage else _group_disclosures(execution, handle, groups, run_groups)
        )
        suite = cast(Mapping[str, object], manifest.document["required_suite"])
        target = cast(Mapping[str, str], manifest.document["target"])
        revision = git_full_sha("HEAD", handle.project_root)
        if revision is None:
            raise SimulationCampaignIntegrityError(
                f"cannot verify resume Target revision at {handle.project_root}"
            )
        if items and items[0]["kind"] in {"coverage_aggregate", "cocotb_batch"}:
            kind = cast(str, items[0]["kind"])
            if kind == "coverage_aggregate" and coverage_plan is None:
                self._resume_coverage_target_plan(validated)
            return plan_coarse_simulation_campaign(
                handle=handle,
                inspection=inspection,
                preview=preview,
                selected_tests=names,
                required_suite=cast(tuple[str, ...], suite["names"]),
                revision=revision,
                invocation_id=invocation_id,
                execution_id="",
                trace=_manifest_trace(manifest),
                kind=kind,
                planning_disclosures=disclosures if kind == "cocotb_batch" else (),
                required_suite_catalog_backed=not cast(bool, suite["default_invocation"]),
                no_waivers=_manifest_no_waivers(manifest),
            )
        return self._ordinary_resume_plan(
            handle,
            inspection,
            preview,
            groups,
            suite,
            target,
            revision,
            invocation_id,
            manifest,
            disclosures,
        )

    def _ordinary_resume_plan(
        self,
        handle,
        inspection,
        preview,
        groups,
        suite,
        target,
        revision,
        invocation_id,
        manifest,
        disclosures,
    ) -> SimulationCampaignPlan:
        return plan_ordinary_hdl_campaign(
            handle=handle,
            inspection=inspection,
            preview=preview,
            groups=groups,
            required_suite=cast(tuple[str, ...], suite["names"]),
            revision=revision,
            invocation_id=invocation_id,
            execution_id="",
            trace=cast(bool, manifest.document["workload"]["trace"]),
            prerequisite_documents=cast(
                tuple[Mapping[str, object], ...], manifest.document["prerequisites"]
            ),
            planning_disclosures=disclosures,
            role=target["role"],
            required_suite_catalog_backed=not cast(bool, suite["default_invocation"]),
        )

    def _resume_coverage_prepared(self, validated: ValidatedResumeManifest) -> tuple[Any, Any]:
        """Run the shared coverage preflight for the manifest's frozen coverage choice."""
        from booley.criteria.state import DevelopmentState
        from booley.flows.sim.coverage_flow_context import coverage_context_for_execution
        from booley.flows.sim.coverage_invocation import (
            CoverageInvocationRequest,
            prepare_coverage_invocation,
        )

        manifest = validated.manifest
        target = cast(Mapping[str, str], manifest.document["target"])
        items = cast(tuple[Mapping[str, object], ...], manifest.document["work_items"])
        names = cast(tuple[str, ...], cast(Mapping[str, object], items[0]["selection"])["names"])
        workload = cast(Mapping[str, object], manifest.document["workload"])
        context = coverage_context_for_execution(
            self.args.work_dir,
            DevelopmentState.load(self.args.state_file)
            if self.args.state_file
            else DevelopmentState(),
            recorder=self._acceptance_recorder,
        )
        runs = tuple(
            test_run_from_document(entry)
            for entry in cast(tuple[object, ...], items[0]["test_runs"])
        )
        prepared = prepare_coverage_invocation(
            CoverageInvocationRequest(
                (target["selector"],),
                tests=names,
                # The manifest froze its Test Runs: resume never resolves again.
                test_runs={target["selector"]: runs},
                trace=cast(bool, workload["trace"]),
                no_waivers=_manifest_no_waivers(manifest),
                diagnostic=self.args.diagnostic,
            ),
            context,
        )
        return context, prepared

    def _resume_coverage_preflight_if_coverage(
        self, validated: ValidatedResumeManifest
    ) -> EndpointOutcome | None:
        items = cast(tuple[Mapping[str, object], ...], validated.manifest.document["work_items"])
        if items and items[0]["kind"] == "coverage_aggregate":
            return self._resume_coverage_preflight(validated)
        return None

    def _resume_coverage_preflight(
        self, validated: ValidatedResumeManifest
    ) -> EndpointOutcome | None:
        """Refuse an unresumable raw-coverage aggregate before admission or reservation.

        Manifests without ``no_waivers`` (all legacy ones) keep their original
        lifecycle: the plan is rebuilt during resume, not at preflight.
        """
        if not _manifest_no_waivers(validated.manifest):
            return None
        context, prepared = self._resume_coverage_prepared(validated)
        if prepared.plan is None:
            return _coverage_preflight_findings_outcome(prepared.findings)
        if len(prepared.plan.targets) == 1:
            self._resume_coverage_plan = prepared.plan.targets[0]
            self._coverage_context = context
        return None

    def _resume_coverage_target_plan(
        self, validated: ValidatedResumeManifest
    ) -> CoverageTargetPlan:
        """Reconstruct the exact native coverage plan bound by the manifest."""
        cached = self.__dict__.get("_resume_coverage_plan")
        if cached is not None:
            return cached
        context, prepared = self._resume_coverage_prepared(validated)
        if prepared.plan is None or len(prepared.plan.targets) != 1:
            detail = "; ".join(item.message for item in prepared.findings)
            raise SimulationCampaignIntegrityError(
                f"cannot reconstruct coverage aggregate: {detail}"
            )
        self._coverage_context = context
        return prepared.plan.targets[0]

    def _pre_state_gate(self) -> EndpointOutcome | None:
        return super()._pre_state_gate()

    def _prepare_coverage(self) -> EndpointOutcome | None:
        from booley.criteria.state import DevelopmentState
        from booley.flows.sim.coverage_flow_context import coverage_context_for_execution
        from booley.flows.sim.coverage_invocation import (
            CoverageInvocationRequest,
            prepare_coverage_invocation,
        )

        if not self._flow_enabled():
            return EndpointOutcome(exit_code=2, report_text="sim is disabled")
        try:
            context = coverage_context_for_execution(
                self.args.work_dir,
                (
                    DevelopmentState.load(self.args.state_file)
                    if self.args.state_file
                    else DevelopmentState()
                ),
                recorder=self._acceptance_recorder,
            )
            selection = self._selected_target_selection()
            prepared = prepare_coverage_invocation(
                CoverageInvocationRequest(
                    tuple(self._requested_targets()),
                    tests=self.args.test or None,
                    test_runs=self._coverage_test_runs(selection.handles),
                    trace=self.args.trace,
                    no_waivers=self.args.no_waivers,
                    diagnostic=self.args.diagnostic,
                ),
                context,
                selection=selection,
            )
        except (ValueError, OSError, fusesoc_registry.FuseSocError) as exc:
            return EndpointOutcome(exit_code=2, report_text=f"Coverage Preflight: {exc}")
        if prepared.plan is None:
            return _coverage_preflight_findings_outcome(prepared.findings)
        self._coverage_prepared = prepared.plan
        for plan in prepared.plan.targets:
            self._record_eda_tool(plan.handle.selector, plan.handle.eda_tool)
        self._coverage_context = context
        return None

    def _coverage_test_runs(
        self, handles: Sequence[TargetHandle]
    ) -> dict[str, tuple[TestRun, ...]]:
        """The Test Runs this invocation resolved for each coverage Target.

        Every selector form is collected: several Test Variants of one Test,
        pinned seeds and ``test@xN``. A request the Target cannot run raises
        (``ValueError`` or ``FuseSocError``), and coverage preflight reports
        it: collecting the Tests' defaults instead would ignore the request.
        """
        test_names_map = _get_test_names(self.args.work_dir)
        resolved: dict[str, tuple[TestRun, ...]] = {}
        for handle in handles:
            runs = self._resolve_test_runs(handle.selector, test_names_map)
            if all(run.test is not None for run in runs):
                resolved[handle.selector] = runs
        return resolved

    def _run_coverage_campaigns(self, admission: object | None) -> EndpointOutcome:
        # A dry run borrows no admission, so it must be answered before the
        # admission check in ``_execute_coverage_campaigns`` (as resume does).
        if self.args.dry_run:
            return self._preview_coverage_campaigns()
        return self._with_live_progress(lambda: self._execute_coverage_campaigns(admission))

    def _preview_coverage_campaigns(self) -> EndpointOutcome:
        """Preview one coverage Campaign per prepared Target.

        Reserves no invocation directory, publishes no manifest, registers no
        campaign with the execution adapter, and builds or simulates nothing.
        """
        campaign = SimulationCampaign()
        try:
            previews = [
                _campaign_preview_entry(
                    campaign.preview(
                        NewCampaignPreviewRequest(
                            # Placeholder ids: a preview reserves no invocation and
                            # starts no execution, so neither is ever read.
                            self._coverage_campaign_plan(plan, invocation_id=1, execution_id=""),
                            Path(self.args.work_dir),
                            self.args.report_dir,
                            self._campaign_policy(),
                        )
                    )
                )
                for plan in self._coverage_prepared.targets
            ]
        except (OSError, ValueError, RuntimeError) as exc:
            return EndpointOutcome(
                exit_code=EXIT_ERROR,
                report_text=f"Simulation Campaign coverage preview failed: {exc}",
            )
        outcome = _campaign_preview_outcome(previews)
        outcome.detail["coverage"] = True
        return outcome

    def _execute_coverage_campaigns(self, admission: object | None) -> EndpointOutcome:
        """Run each native coverage selection as one durable aggregate item."""
        if not isinstance(admission, AdmissionContext):
            return EndpointOutcome(
                exit_code=EXIT_ERROR,
                report_text="sim: Coverage Campaign has no borrowed admission context",
            )
        prepared = self._coverage_prepared
        assert self.args.report_dir is not None, "prepared Flow requires a report root"
        invocation = self.reserve_invocation_dir()
        assert invocation is not None
        progress = CoverageProgress(
            invocation, tuple(plan.handle.selector for plan in prepared.targets)
        )
        lifecycle = ProgressLifecycle(
            lambda phase: progress.checkpoint(complete=True, phase=phase)
        )
        published: list[Path] = []
        result: EndpointOutcome | None = None
        try:
            with lifecycle:
                progress.checkpoint()
                campaign, requests = self._coverage_campaign_session(
                    admission, invocation, self._manifest_publisher(published)
                )
                result = self._execute_coverage_campaign_requests(campaign, requests, progress)
                if result.exit_code != EXIT_ERROR and len(progress.outcomes) == len(
                    progress.targets
                ):
                    lifecycle.complete()
                return self._with_campaign_paths(result, published)
        except (OSError, ValueError, RuntimeError) as exc:
            if result is not None:
                failure = (
                    exc.__cause__
                    if isinstance(exc, ProgressPublicationError)
                    and isinstance(exc.__cause__, Exception)
                    else exc
                )
                return normalize_completion_error(
                    result,
                    failure,
                    "publish coverage progress",
                    path=progress.invocation_dir / "progress.json",
                )
            detail = (
                {"progress_error": str(exc)} if isinstance(exc, ProgressPublicationError) else {}
            )
            return self._with_campaign_paths(
                EndpointOutcome(
                    exit_code=EXIT_ERROR,
                    detail=detail,
                    report_text=f"Simulation Campaign coverage planning failed: {exc}",
                ),
                published,
            )

    def _coverage_campaign_session(
        self,
        admission: AdmissionContext,
        invocation: Path,
        manifest_published: Callable[[Path], None] | None = None,
    ) -> tuple[
        SimulationCampaign,
        list[NewCampaignRunRequest],
    ]:
        """Publish every immutable coverage manifest before admitting work."""
        prepared = self._coverage_prepared
        plans = {plan.handle.identity: plan for plan in prepared.targets}
        executor = CoverageAggregateExecutor(
            plans=plans,
            execution_factory=self._coverage_campaign_execution,
            publication_checkpoint=self._campaign_publication_checkpoint,
        )
        campaign = SimulationCampaign(
            executor,
            publication_checkpoint=self._campaign_publication_checkpoint,
            manifest_published=manifest_published,
        )
        execution_id = str(admission.execution_id or "")
        requests = [
            NewCampaignRunRequest(
                self._coverage_campaign_plan(plan, int(invocation.name), execution_id),
                Path(self.args.work_dir),
                self.args.report_dir,
                self._campaign_policy(),
                invocation,
                admission,
                pre_sim_firing_published=self._record_pre_sim_firing,
            )
            for plan in prepared.targets
        ]
        for request in requests:
            campaign.publish_new(request)
        return campaign, requests

    def _execute_coverage_campaign_requests(
        self,
        campaign: SimulationCampaign,
        requests: list[NewCampaignRunRequest],
        progress: CoverageProgress,
    ) -> EndpointOutcome:
        """Execute published coverage aggregates in deterministic Target order."""
        outcomes: list[CampaignOutcome] = []
        for index, request in enumerate(requests):
            try:
                outcome = campaign.run(request)
            except SimulationCampaignCancellationError as exc:
                self._capture_cycle_report(outcomes)
                return self._with_coverage_campaign_lines(
                    self._campaign_cancelled_outcome(exc), outcomes
                )
            except (OSError, ValueError, RuntimeError) as exc:
                return self._coverage_campaign_failure(outcomes, requests, index, request, exc)
            outcomes.append(outcome)
            try:
                _retain_coverage_campaign(progress, outcome)
            except (OSError, ValueError) as exc:
                return self._coverage_campaign_failure(
                    outcomes[:-1], requests, index, request, exc, failed_outcome=outcome
                )
            try:
                progress.checkpoint()
            except (OSError, ValueError) as exc:
                result = self._campaign_endpoint_outcome(outcomes)
                result.detail = dict(result.detail)
                result.detail["pending_targets"] = [
                    _coverage_request_selector(item) for item in requests[index + 1 :]
                ]
                return normalize_completion_error(
                    result,
                    exc,
                    "publish coverage progress",
                    path=progress.invocation_dir / "progress.json",
                )
        return self._campaign_endpoint_outcome(outcomes)

    @staticmethod
    def _with_coverage_campaign_lines(
        result: EndpointOutcome, outcomes: Sequence[CampaignOutcome]
    ) -> EndpointOutcome:
        """Append the finished Targets' ``coverage campaign:`` lines to a stopped run's card."""
        lines = _coverage_campaign_lines(outcomes)
        if lines:
            result.report_text = "\n".join([result.report_text, *lines])
        return result

    def _coverage_campaign_failure(
        self,
        outcomes: list[CampaignOutcome],
        requests: list[NewCampaignRunRequest],
        index: int,
        request: NewCampaignRunRequest,
        error: Exception,
        *,
        failed_outcome: CampaignOutcome | None = None,
    ) -> EndpointOutcome:
        """Report a fatal aggregate without inventing a terminal Simulation result."""
        self._simulation_campaign_outcomes = tuple(outcomes)
        self._capture_cycle_report(
            (*outcomes, failed_outcome) if failed_outcome is not None else outcomes
        )
        targets = _coverage_compatibility_targets(
            outcomes,
            self._reserved_invocation_dir,
        )
        failed = _coverage_request_selector(request)
        evaluation = self._coverage_failure_evaluation(request, error)
        if evaluation is None:
            return _unmatched_coverage_failure(outcomes, targets, error)
        targets[failed] = _coverage_failed_target(
            failed, error, evaluation, failed_outcome, self._reserved_invocation_dir
        )
        pending = [failed, *[_coverage_request_selector(item) for item in requests[index + 1 :]]]
        return _coverage_failure_outcome(
            outcomes,
            targets,
            pending,
            error,
            self._reserved_invocation_dir,
        )

    def _coverage_failure_evaluation(
        self, request: NewCampaignRunRequest, error: Exception
    ) -> str | None:
        """Preserve nested evaluation or derive policy before an outcome exists."""
        if isinstance(error, _CoverageAggregateError):
            return cast(str, error.detail["evaluation"])
        target = _coverage_request_target(request)
        identity = f"{target['vlnv']}#{target['name']}"
        matches = [
            plan for plan in self._coverage_prepared.targets if plan.handle.identity == identity
        ]
        if len(matches) != 1:
            return None
        return "not_requested" if matches[0].criterion is None else "blocked"

    def _coverage_campaign_execution(
        self,
        plan: CoverageTargetPlan,
        options: SimulationOptions,
        pre_sim_commands: tuple[str, ...],
        pre_sim_build_access: str,
    ) -> SimulationExecutionPort:
        from booley.flows.sim.verilator_coverage_execution import VerilatorCoverageExecution

        return (
            self._coverage_execution_factory(
                plan.handle, options, pre_sim_commands, pre_sim_build_access
            )
            if self._coverage_execution_factory is not None
            else VerilatorCoverageExecution(
                plan.handle,
                invoke=self._execute_boundary,
                options=options,
                pre_sim_commands=pre_sim_commands,
                pre_sim_build_access=pre_sim_build_access,
            )
        )

    def _coverage_campaign_plan(
        self, plan: CoverageTargetPlan, invocation_id: int, execution_id: str
    ) -> SimulationCampaignPlan:
        inspection = TargetCatalog.build(plan.handle.project_root).inspect(plan.handle)
        execution = self._simulation_execution()
        # One aggregate work item records every Test Run the plan collects,
        # in collection order, so each observation binds to its own run.
        preview = execution.preview(plan.handle, SelectedTestRuns(plan.test_runs))
        preview = replace(preview, groups=(plan.selected_tests,), run_groups=(plan.test_runs,))
        return plan_coarse_simulation_campaign(
            handle=plan.handle,
            inspection=inspection,
            preview=preview,
            selected_tests=plan.selected_tests,
            required_suite=plan.declared_tests,
            revision=git_full_sha("HEAD", plan.handle.project_root) or "unversioned",
            invocation_id=invocation_id,
            execution_id=execution_id,
            trace=self.args.trace,
            kind="coverage_aggregate",
            required_suite_catalog_backed=True,
            no_waivers=self.args.no_waivers,
        )

    def _run_selected_mode(
        self,
        *,
        prepared: PreparedSimulationEndpoint,
        admission: object | None,
    ) -> EndpointOutcome:
        """Run the prepared simulate-mode Campaign."""
        mode_error = self._validate_mode_args()
        if mode_error is not None:
            return mode_error
        targets = list(prepared.selected_targets)
        test_names_map = prepared.test_names_map

        special_result = self._maybe_dispatch_special_run(targets, test_names_map)
        if special_result is not None:
            return special_result
        assert self.args.report_dir is not None, "prepared Flow requires a report root"

        return self._run_ordinary_campaigns(targets, test_names_map, admission)

    def _with_live_progress(self, run: Callable[[], EndpointOutcome]) -> EndpointOutcome:
        """Bind execution observations to the original destination invocation."""
        args = self._args
        if args is None or getattr(args, "dry_run", False) or current_progress() is not None:
            return run()
        if not hasattr(args, "work_dir"):
            return run()
        invocation = self.reserve_invocation_dir()
        if invocation is None:
            return run()
        sink = LiveProgressSink(
            invocation / "progress.json",
            Path(self.args.work_dir),
            os.environ.get("BOOLEY_RUN_ID", ""),
        )
        with install_progress(sink):
            result = run()
        if sink.error is not None:
            result.detail["observation_error"] = sink.error
        return result

    def _manifest_publisher(self, published: list[Path]) -> Callable[[Path], None]:
        """Retain committed paths even when console publication is unavailable."""

        def publish(path: Path) -> None:
            path = path.resolve()
            if path in published:
                return
            published.append(path)
            adapter = self.execution_adapter
            diagnostic = getattr(self._args, "diagnostic", False)
            if wants_campaigns(adapter) and diagnostic is not True:
                # Before any of its work runs: a campaign that never returns an
                # outcome still leaves results its Goals must account for.
                register_campaign_for(adapter, started_campaign(path))
            if self._console_publication_requested:
                with suppress(OSError, ValueError):
                    print(f"campaign manifest: {path.resolve()}", file=sys.stderr, flush=True)

        return publish

    def _report_destination(self) -> Path | None:
        """Reserve and name this run's ``report.json``; call it only before ``write_report``."""
        args = self._args
        if args is None or getattr(args, "dry_run", False):
            return None
        if getattr(args, "report_dir", None) is None:
            return None
        try:
            # Reserves the directory write_report later fills, so the path is this run's report.
            invocation = self.reserve_invocation_dir()
        except (OSError, ValueError, RuntimeError):
            return None
        return None if invocation is None else (invocation / "report.json").resolve()

    def _with_campaign_paths(
        self, result: EndpointOutcome, published: Sequence[Path] = ()
    ) -> EndpointOutcome:
        lines = [result.report_text] if result.report_text else []
        existing = set(result.report_text.splitlines())
        for path in published:
            line = f"  manifest: {path.resolve()}"
            if line not in existing:
                lines.append(line)
                existing.add(line)
        report = self._report_destination()
        if report is not None and f"  report: {report}" not in existing:
            lines.append(f"  report: {report}")
        result.report_text = "\n".join(lines)
        return result

    def _run_ordinary_campaigns(self, targets, test_names_map, admission):
        return self._with_live_progress(
            lambda: self._execute_ordinary_campaigns(targets, test_names_map, admission)
        )

    def _execute_ordinary_campaigns(
        self,
        targets: list[str],
        test_names_map: dict[str, list[str]],
        admission: object | None,
    ) -> EndpointOutcome:
        invocation = self.reserve_invocation_dir()
        assert invocation is not None
        if not isinstance(admission, AdmissionContext):
            return EndpointOutcome(
                exit_code=EXIT_ERROR,
                report_text="sim: Simulation Campaign has no borrowed admission context",
            )
        published: list[Path] = []
        campaign = SimulationCampaign(
            OrdinaryHdlSerialExecutor(invoke=self._execute_boundary),
            publication_checkpoint=self._campaign_publication_checkpoint,
            manifest_published=self._manifest_publisher(published),
        )
        outcomes: list[CampaignOutcome] = []
        lifecycle = ProgressLifecycle(
            lambda phase: self._write_campaign_progress(
                targets, outcomes, phase=phase, complete=True
            )
        )
        result: EndpointOutcome | None = None
        try:
            with lifecycle:
                self._write_campaign_progress(targets, outcomes, phase="starting", complete=False)
                baseline_requests, prerequisites = self._plan_campaign_baselines(
                    invocation, admission, targets, test_names_map
                )
                candidate_requests = self._candidate_campaign_requests(
                    targets, test_names_map, prerequisites, invocation, admission
                )
                self._publish_and_run_campaign_requests(
                    campaign,
                    baseline_requests,
                    candidate_requests,
                    outcomes=outcomes,
                    checkpoint=lambda: self._write_campaign_progress(
                        targets, outcomes, phase="running", complete=False
                    ),
                )
                result = self._campaign_endpoint_outcome(outcomes)
                lifecycle.complete()
                return result
        except SimulationCampaignCancellationError as exc:
            self._capture_cycle_report(outcomes)
            return self._with_campaign_paths(self._campaign_cancelled_outcome(exc), published)
        except (OSError, ValueError, RuntimeError) as exc:
            return self._with_campaign_paths(
                self._ordinary_campaign_failure(exc, outcomes, result, invocation), published
            )

    def _ordinary_campaign_failure(
        self,
        error: Exception,
        outcomes: list[CampaignOutcome],
        result: EndpointOutcome | None,
        invocation: Path,
    ) -> EndpointOutcome:
        """Retain the existing ordinary error detail and completion semantics."""
        self._capture_cycle_report(outcomes)
        if result is not None and isinstance(error, ProgressPublicationError):
            failure = error.__cause__ if isinstance(error.__cause__, Exception) else error
            return normalize_completion_error(
                result,
                failure,
                "publish simulation progress",
                path=invocation / "progress.json",
            )
        detail: dict[str, object] = (
            {"campaigns": _campaign_structured_details(outcomes)} if outcomes else {}
        )
        if isinstance(error, ProgressPublicationError):
            detail["progress_error"] = str(error)
        if isinstance(error, SimulationBuildInfrastructureError):
            detail.update(_build_infrastructure_detail(error))
        return EndpointOutcome(
            exit_code=EXIT_ERROR,
            detail=detail,
            report_text=f"Simulation Campaign execution failed: {error}",
        )

    @staticmethod
    def _campaign_cancelled_outcome(
        error: SimulationCampaignCancellationError,
        detail: Mapping[str, object] | None = None,
    ) -> EndpointOutcome:
        return EndpointOutcome(
            exit_code=EXIT_CANCELLED,
            detail=dict(detail or {}),
            report_text=str(error),
        )

    def _candidate_campaign_requests(
        self,
        targets: list[str],
        test_names_map: dict[str, list[str]],
        prerequisites: Mapping[str, Sequence[Mapping[str, object]]],
        invocation: Path,
        admission: AdmissionContext,
    ) -> list[NewCampaignRunRequest]:
        execution_id = str(admission.execution_id or "")
        requests = []
        for target in targets:
            plan = self._ordinary_campaign_plan(
                target,
                test_names_map,
                invocation_id=int(invocation.name),
                execution_id=execution_id,
                prerequisite_documents=prerequisites.get(target, ()),
            )
            self._record_campaign_tool(plan.manifest)
            requests.append(
                NewCampaignRunRequest(
                    plan,
                    Path(self.args.work_dir),
                    self.args.report_dir,
                    self._campaign_policy(),
                    invocation,
                    admission,
                    pre_sim_firing_published=self._record_pre_sim_firing,
                )
            )
        return requests

    @staticmethod
    def _publish_and_run_campaign_requests(
        campaign: SimulationCampaign,
        baselines: Sequence[NewCampaignRunRequest],
        candidates: Sequence[NewCampaignRunRequest],
        *,
        outcomes: list[CampaignOutcome] | None = None,
        checkpoint: Callable[[], None] | None = None,
    ) -> list[CampaignOutcome]:
        completed = outcomes if outcomes is not None else []
        # Reject an unusable literal run_cwd before anything is published or built,
        # including Cycle Count baselines that would otherwise build first.
        for request in (*baselines, *candidates):
            campaign.preflight(request)
        for request in (*baselines, *candidates):
            campaign.publish_new(request)
        for request in baselines:
            target = cast(Mapping[str, object], request.plan.manifest.document["target"])
            with attempt_scope(
                str(target["selector"]),
                identity=f"{target['vlnv']}#{target['name']}",
                role="baseline",
                revision=str(target["revision"]),
                ephemeral_root=request.project_root,
            ):
                campaign.run(request)
        for request in candidates:
            completed.append(campaign.run(request))
            if checkpoint is not None:
                checkpoint()
        return completed

    def _write_campaign_progress(
        self,
        targets: Sequence[str],
        outcomes: Sequence[CampaignOutcome],
        *,
        phase: str,
        complete: bool,
    ) -> None:
        invocation = self.reserve_invocation_dir()
        if invocation is None:
            return
        completed = [str(outcome.target["selector"]) for outcome in outcomes]
        payload = progress_document(
            flow=self.name,
            run_id=os.environ.get("BOOLEY_RUN_ID", ""),
            phase=phase,
            targets=targets,
            completed_targets=completed,
            detail=_campaign_structured_details(outcomes),
            extra={"mode": self._mode.value},
        )
        if complete != bool(payload["complete"]):
            raise ValueError("Simulation Campaign progress complete and phase disagree")
        publish_checkpoint(invocation / "progress.json", payload)

    def _plan_campaign_baselines(
        self,
        invocation: Path,
        admission: Any,
        targets: list[str],
        test_names_map: dict[str, list[str]],
    ) -> tuple[list[NewCampaignRunRequest], dict[str, list[Mapping[str, object]]]]:
        """Plan linked baselines without starting any campaign work."""
        baseline_ref, baseline_targets, error = self._cycle_baseline_selection(targets)
        if error is not None:
            raise BaselineWorktreeError(error)
        if baseline_ref is None:
            return [], {}
        links = self._baseline_links(targets, test_names_map)
        expected = {target: self._target_handle(target).identity for target in baseline_targets}
        prerequisites: dict[str, list[Mapping[str, object]]] = {target: [] for target in targets}
        worktree = self._shared_baseline_worktree(baseline_ref)
        requests: list[NewCampaignRunRequest] = []
        with self._baseline_checkout(worktree, links, baseline_ref) as baseline:
            for target in (item for item in baseline_targets if baseline.runs_of(item)):
                request, by_run = self._planned_baseline_request(
                    target,
                    expected[target],
                    baseline_ref,
                    baseline.runs_of(target),
                    test_names_map,
                    worktree,
                    invocation,
                    admission,
                )
                requests.append(request)
                _link_baseline_prerequisites(prerequisites, baseline.links_of(target), by_run)
        return requests, prerequisites

    def _planned_baseline_request(
        self,
        target: str,
        expected_identity: str,
        baseline_ref: str,
        runs: tuple[TestRun, ...],
        test_names_map: dict[str, list[str]],
        worktree: Path,
        invocation: Path,
        admission: Any,
    ) -> tuple[NewCampaignRunRequest, dict[str, Mapping[str, object]]]:
        handle = self._target_handle(target)
        if handle.identity != expected_identity:
            raise BaselineWorktreeError(
                f"Cycle Count baseline selector {target!r} resolves to "
                f"{handle.identity!r}, expected {expected_identity!r}"
            )
        plan = self._ordinary_campaign_plan(
            target,
            test_names_map,
            invocation_id=int(invocation.name),
            execution_id=str(admission.execution_id or ""),
            role="cycle_count_baseline",
            revision=baseline_ref,
            baseline_runs=runs,
        )
        request = NewCampaignRunRequest(
            plan,
            worktree,
            self.args.report_dir,
            self._campaign_policy(),
            invocation,
            admission,
            pre_sim_firing_published=self._record_pre_sim_firing,
        )
        return request, self._baseline_prerequisites_by_run(target, plan)

    def _baseline_prerequisites_by_run(
        self, target: str, plan: SimulationCampaignPlan
    ) -> dict[str, Mapping[str, object]]:
        """The prerequisite document of each baseline work item, by Test Run name."""
        target_document = cast(Mapping[str, str], plan.manifest.document["target"])
        relative = (
            target_report_directory(
                Path(), f"{target}@baseline-{target_document['revision'][:12]}"
            )
            / "campaign"
            / "manifest.json"
        )
        documents = self._campaign_prerequisite_documents(plan, relative)
        items = cast(tuple[Mapping[str, object], ...], plan.manifest.document["work_items"])
        return {
            str(run["name"]): document
            for item, document in zip(items, documents, strict=True)
            for run in cast(Sequence[Mapping[str, object]], item["test_runs"])
        }

    def _campaign_baseline_requirements(
        self, targets: Sequence[str]
    ) -> tuple[tuple[str, str, str], ...]:
        """Return candidate/baseline/test links required by relative criteria."""
        handles = {target: self._target_handle(target) for target in targets}
        links: list[tuple[str, str, str]] = []
        for key, entry in self.state.criteria.items():
            params = entry.params or {}
            candidate = self._matching_cycle_target(handles, params)
            test = params.get("test")
            if (
                not key.startswith("cycle_count_")
                or candidate is None
                or not has_relative_threshold(params)
                or not isinstance(test, str)
                or not test
            ):
                continue
            baseline = params.get(BASELINE_TARGET_PARAM, candidate)
            if isinstance(baseline, str) and baseline:
                links.append((candidate, baseline, test))
        return tuple(dict.fromkeys(links))

    @staticmethod
    def _campaign_prerequisite_documents(
        plan: SimulationCampaignPlan, manifest_path: Path
    ) -> list[Mapping[str, object]]:
        raw = encode_simulation_campaign_manifest(plan.manifest)
        manifest_ref = {
            "path_base": "origin_invocation",
            "path": manifest_path.as_posix(),
            "bytes": len(raw),
            "sha256": manifest_digest(plan.manifest),
            "kind": "simulation_campaign_manifest",
            "owner": plan.manifest.document["campaign_id"],
        }
        baseline_target = dict(cast(Mapping[str, object], plan.manifest.document["target"]))
        return [
            {
                "role": "cycle_count_baseline",
                "manifest": manifest_ref,
                "campaign_id": plan.manifest.document["campaign_id"],
                "target": baseline_target,
                "required_observation": "cycle_count",
                "work_item_id": item["work_item_id"],
            }
            for item in cast(
                tuple[Mapping[str, object], ...], plan.manifest.document["work_items"]
            )
        ]

    def _preview_ordinary_campaigns(
        self, targets: list[str], test_names_map: dict[str, list[str]]
    ) -> EndpointOutcome:
        """Preview ordinary campaigns without reserving or mutating report state."""
        previews = []
        campaign = SimulationCampaign()
        try:
            prerequisites, baseline_previews = self._preview_campaign_baselines(
                campaign, targets, test_names_map
            )
            previews.extend(baseline_previews)
            for target in targets:
                plan = self._ordinary_campaign_plan(
                    target,
                    test_names_map,
                    invocation_id=1,
                    execution_id="",
                    prerequisite_documents=prerequisites.get(target, ()),
                )
                preview = campaign.preview(
                    NewCampaignPreviewRequest(
                        plan,
                        Path(self.args.work_dir),
                        self.args.report_dir,
                        self._campaign_policy(),
                    )
                )
                previews.append(_campaign_preview_entry(preview))
        except (OSError, ValueError, RuntimeError) as exc:
            return EndpointOutcome(
                exit_code=EXIT_ERROR,
                report_text=f"Simulation Campaign preview failed: {exc}",
            )
        return _campaign_preview_outcome(previews)

    def _preview_campaign_baselines(
        self,
        campaign: SimulationCampaign,
        targets: list[str],
        test_names_map: dict[str, list[str]],
    ) -> tuple[dict[str, list[Mapping[str, object]]], list[dict[str, object]]]:
        """Preview linked baselines through the campaign seam without publication."""
        baseline_ref, baseline_targets, error = self._cycle_baseline_selection(targets)
        if error is not None:
            raise BaselineWorktreeError(error)
        if baseline_ref is None:
            return {}, []
        links = self._baseline_links(targets, test_names_map)
        prerequisites: dict[str, list[Mapping[str, object]]] = {target: [] for target in targets}
        previews: list[dict[str, object]] = []
        worktree = self._shared_baseline_worktree(baseline_ref)
        with self._baseline_checkout(worktree, links, baseline_ref) as baseline:
            for target in (item for item in baseline_targets if baseline.runs_of(item)):
                preview, by_run = self._preview_one_campaign_baseline(
                    campaign,
                    target,
                    baseline_ref,
                    baseline.runs_of(target),
                    test_names_map,
                    worktree,
                )
                previews.append(preview)
                _link_baseline_prerequisites(prerequisites, baseline.links_of(target), by_run)
        return prerequisites, previews

    def _preview_one_campaign_baseline(
        self,
        campaign: SimulationCampaign,
        target: str,
        baseline_ref: str,
        runs: tuple[TestRun, ...],
        test_names_map: dict[str, list[str]],
        worktree: Path,
    ) -> tuple[dict[str, object], dict[str, Mapping[str, object]]]:
        plan = self._ordinary_campaign_plan(
            target,
            test_names_map,
            invocation_id=1,
            execution_id="",
            role="cycle_count_baseline",
            revision=baseline_ref,
            baseline_runs=runs,
        )
        preview = campaign.preview(
            NewCampaignPreviewRequest(
                plan,
                worktree,
                self.args.report_dir,
                self._campaign_policy(),
            )
        )
        summary = {
            "target": dict(preview.target),
            "work_items": len(preview.work_items),
            "build_variants": len(preview.build_variants),
            "prerequisites": 0,
        }
        return summary, self._baseline_prerequisites_by_run(target, plan)

    def _ordinary_campaign_plan(
        self,
        target: str,
        test_names_map: dict[str, list[str]],
        *,
        invocation_id: int,
        execution_id: str,
        prerequisite_documents: Sequence[Mapping[str, object]] = (),
        role: str = "candidate",
        revision: str | None = None,
        baseline_runs: tuple[TestRun, ...] | None = None,
    ) -> SimulationCampaignPlan:
        """Resolve one Target into the same exact plan used by preview and run.

        *baseline_runs* are the candidate Test Runs a Cycle Count baseline
        runs instead of the request's own selection.
        """
        handle = self._target_handle(target)
        inputs = self._ordinary_campaign_inputs(target, test_names_map, handle, baseline_runs)
        if self.is_cocotb_target(target):
            return self._cocotb_campaign_plan(
                handle,
                inputs[0],
                inputs[1],
                inputs[3],
                revision,
                invocation_id,
                execution_id,
                inputs[2],
                inputs[4],
            )
        return plan_ordinary_hdl_campaign(
            handle=handle,
            inspection=inputs[0],
            preview=inputs[1],
            groups=inputs[1].groups,
            required_suite=inputs[3],
            revision=revision or git_full_sha("HEAD", Path(self.args.work_dir)) or "unversioned",
            invocation_id=invocation_id,
            execution_id=execution_id,
            trace=self.args.trace,
            prerequisite_documents=prerequisite_documents,
            planning_disclosures=inputs[2],
            role=role,
            required_suite_catalog_backed=inputs[4],
        )

    def _ordinary_campaign_inputs(self, target, test_names_map, handle, baseline_runs):
        inspection = TargetCatalog.build(handle.project_root).inspect(handle)
        selection = (
            SelectedTestRuns(baseline_runs)
            if baseline_runs is not None
            else self._test_run_selection(target, test_names_map)
        )
        execution = self._simulation_execution()
        preview = execution.preview(handle, selection)
        disclosures = _group_disclosures(execution, handle, preview.groups, preview.run_groups)
        suite = resolve_target_test_suite(
            target,
            identity=handle.identity,
            test_names=test_names_map,
            test_skips=_get_test_skips(self.args.work_dir),
        )
        required = tuple(name for name in suite.tests if name is not None)
        catalog_backed = bool(lookup_target_section(test_names_map, handle.identity) or [])
        return inspection, preview, disclosures, required, catalog_backed

    def _cocotb_campaign_plan(
        self,
        handle,
        inspection,
        preview,
        required,
        revision,
        invocation_id,
        execution_id,
        planning_disclosures,
        catalog_backed,
    ) -> SimulationCampaignPlan:
        selected = tuple(name for group in preview.groups for name in group)
        return plan_coarse_simulation_campaign(
            handle=handle,
            inspection=inspection,
            preview=preview,
            selected_tests=selected,
            required_suite=required,
            revision=revision or git_full_sha("HEAD", Path(self.args.work_dir)) or "unversioned",
            invocation_id=invocation_id,
            execution_id=execution_id,
            trace=self.args.trace,
            kind="cocotb_batch",
            planning_disclosures=planning_disclosures,
            required_suite_catalog_backed=catalog_backed,
        )

    def _record_campaign_tool(self, manifest: SimulationCampaignManifest) -> None:
        """Use the frozen execution recipe, including when execution later fails."""
        document = manifest.document
        target = cast(Mapping[str, str], document["target"])
        workload = cast(Mapping[str, object], document["workload"])
        eda = cast(Mapping[str, str], workload["eda"])
        self._record_eda_tool(str(target["selector"]), str(eda["kind"]))

    def _campaign_policy(self) -> CampaignPolicy:
        return CampaignPolicy(
            timeout_seconds=self._effective_timeout_ms() / 1000,
            build_timeout_seconds=self._effective_build_timeout_ms() / 1000,
            no_kill=self.args.no_kill,
            diagnostic=self.args.diagnostic,
            result_verbosity=self.args.result_verbosity,
        )

    def _campaign_endpoint_outcome(self, outcomes: list[CampaignOutcome]) -> EndpointOutcome:
        self._simulation_campaign_outcomes = tuple(outcomes)
        self._capture_cycle_report(outcomes)
        coverage_campaigns = _resolved_coverage_campaigns(outcomes)
        grades = [outcome.aggregate_grade for outcome in outcomes]
        exit_code = _coverage_campaign_exit_code(outcomes, coverage_campaigns) or max(
            (_grade_exit_code(grade) for grade in grades), default=EXIT_SUCCESS
        )
        threshold_missed = any(
            campaign.evaluation["status"] == "fail" for campaign in coverage_campaigns.values()
        )
        report: dict[str, Any] = {
            "report_path": self._report_destination(),
            "default_seeds": self._test_runs.default_seeds,
        }
        invocation = self._reserved_invocation_dir
        campaigns = _campaign_structured_details(outcomes, invocation)
        coverage_targets = _coverage_compatibility_targets(
            outcomes,
            invocation,
            coverage_campaigns,
        )
        detail: dict[str, object] = {"campaigns": campaigns}
        firings, current_keys = _campaign_hook_inputs(outcomes, invocation)
        pre_sim_detail, hook_lines = _pre_sim_projection(firings, current_keys, invocation)
        detail.update(pre_sim_detail)
        detail.update(_campaign_build_infrastructure_detail(outcomes))
        keys = frozenset(getattr(self, "_current_published_pre_sim_keys", current_keys))
        budget_lines: list[str] | None = None
        if keys.issubset(firings):
            _, budget_lines = _pre_sim_lines(firings, keys)
        lines = _campaign_report_lines(outcomes, coverage_campaigns, budget_lines, **report)
        lines.extend(hook_lines)
        if coverage_targets:
            detail.update(coverage=True, targets=coverage_targets, pending_targets=[])
        return EndpointOutcome(
            exit_code=exit_code,
            cycle_report_json=self.cycle_report_json or "{}",
            criterion_met=exit_code == EXIT_SUCCESS and not threshold_missed,
            detail=detail,
            report_text="\n".join(lines),
            display_label=self._campaign_completed_display_label(outcomes),
        )

    def _campaign_completed_display_label(self, outcomes: Sequence[CampaignOutcome]) -> str | None:
        """Describe the requested Targets and trustworthy observed test scope."""
        observations = [item for outcome in outcomes for item in outcome.observations]
        tests = [item.get("test") for item in observations]
        if not tests or any(not isinstance(test, str) or not test.strip() for test in tests):
            return format_flow_display_label(self._requested_targets(), tests=None)
        return format_flow_display_label(self._requested_targets(), tests=cast(list[str], tests))

    def _validate_mode_args(self) -> EndpointOutcome | None:
        """Refuse bare ``--standalone`` and run-stage arguments meaningless in elab-only mode."""
        if self.args._legacy_standalone_without_elab:
            return EndpointOutcome(
                exit_code=EXIT_ERROR,
                report_text=(
                    "sim: --standalone alone is invalid; use --mode elab-only-standalone"
                ),
            )
        if not self._mode.elaborates_only:
            return None
        conflicts = (
            ("--test", bool(self.args.test)),
            ("--trace", self.args.trace),
            ("--result-verbosity full", self.args.result_verbosity == "full"),
            ("--no-kill", self.args.no_kill),
        )
        for argument, active in conflicts:
            if active:
                return EndpointOutcome(
                    exit_code=EXIT_ERROR,
                    report_text=(
                        f"sim: {argument} conflicts with --mode {self._mode.value}; "
                        f"remove {argument} or use --mode simulate."
                    ),
                )
        return None

    def _run_elab_only(self) -> EndpointOutcome:
        """Validate the build-only selection before reserving execution resources."""
        preflight = self._elab_only_preflight()
        if isinstance(preflight, EndpointOutcome):
            return (
                self._with_campaign_paths(preflight)
                if preflight.exit_code != EXIT_SUCCESS
                else preflight
            )
        return self._with_live_progress(lambda: self._execute_elab_only(preflight))

    def _execute_elab_only(self, targets: list[str]) -> EndpointOutcome:
        """Compile, elaborate, and link selected Simulation Targets without tests."""
        results: list[ElabOnlyTargetResult] = []
        result: EndpointOutcome | None = None
        self._elab_progress_results = results
        lifecycle = ProgressLifecycle(
            lambda phase: self._write_elab_only_progress(
                targets, self._elab_progress_results, phase=phase, complete=True
            )
        )
        try:
            with lifecycle:
                results = self._run_elab_only_campaign(targets)
                self._elab_progress_results = results
                missing = self._elab_only_missing_executable(results)
                if missing is not None:
                    target, exc = missing
                    result = self._missing_executable_result(exc, target)
                    result.detail["mode"] = self._mode.value
                    result.detail["targets"] = [
                        self._elab_only_detail(target_result) for target_result in results
                    ]
                    artifacts = {
                        target_result.target: {"log": target_result.log_path}
                        for target_result in results
                        if target_result.log_path
                    }
                    if artifacts:
                        result.detail["artifacts"] = artifacts
                    lifecycle.complete()
                    return result
                exit_code, standalone = self._run_optional_standalone(targets, results)
                result = self._elab_only_result(targets, results, exit_code, standalone)
                lifecycle.complete()
                return result
        except ProgressPublicationError as exc:
            if result is not None:
                failure = exc.__cause__ if isinstance(exc.__cause__, Exception) else exc
                invocation = self._reserved_invocation_dir
                return normalize_completion_error(
                    result,
                    failure,
                    "publish elaboration progress",
                    path=invocation / "progress.json" if invocation is not None else None,
                )
            return self._with_campaign_paths(
                EndpointOutcome(
                    exit_code=EXIT_ERROR,
                    detail={"progress_error": str(exc)},
                    report_text=f"sim: progress publication failed: {exc}",
                )
            )

    @staticmethod
    def _elab_only_missing_executable(
        results: list[ElabOnlyTargetResult],
    ) -> tuple[str, MissingExecutableError] | None:
        """Return the first absent binary from an inconclusive build attempt."""
        for result in results:
            if result.outcome.failure_kind != "infrastructure":
                continue
            binary = find_missing_executable(result.outcome.output)
            if binary is not None:
                return result.target, MissingExecutableError(
                    binary,
                    context=result.outcome.output,
                )
        return None

    def _elab_only_preflight(self) -> list[str] | EndpointOutcome:
        """Validate compile-only mode and return its selected Targets."""
        if not self._flow_enabled():
            return EndpointOutcome(
                exit_code=EXIT_ERROR,
                report_text="sim is disabled ([flows.sim].enabled = false).",
                detail={"mode": self._mode.value},
            )
        targets_or_error = self._resolve_requested_targets()
        if isinstance(targets_or_error, EndpointOutcome):
            targets_or_error.detail["mode"] = self._mode.value
            return targets_or_error
        targets = targets_or_error
        target_error = self._validate_interactive_args(targets)
        if target_error is not None:
            target_error.detail["mode"] = self._mode.value
            return target_error
        plan = self._plan_elaboration(targets)
        self._flow_plan = plan
        if self.args.dry_run:
            return self._handle_elab_only_dry_run(targets)
        if plan.aggregate_errors:
            return EndpointOutcome(
                exit_code=EXIT_ERROR,
                report_text="sim planning failed: " + "; ".join(plan.aggregate_errors),
                detail={"mode": self._mode.value, "plan": plan.as_dict()},
            )
        return targets

    def _plan_elaboration(self, targets: list[str]) -> FlowPlan:
        """Resolve every elaboration unit, including one cumulative standalone sweep."""
        units: list[WorkUnitPlan] = []
        errors: list[str] = []
        for target in targets:
            try:
                units.append(self._elaboration_work_unit(target))
            except (fusesoc_registry.FuseSocError, OSError, ValueError) as exc:
                errors.append(f"{target}: {exc}")
        standalone, standalone_error = self._planned_standalone_unit(targets)
        if standalone is not None:
            units.append(standalone)
        if standalone_error is not None:
            errors.append(standalone_error)

        return FlowPlan(
            flow="sim",
            mode=self._mode.value,
            work_units=tuple(units),
            aggregate_errors=tuple(errors),
        )

    def _elaboration_work_unit(self, target: str) -> WorkUnitPlan:
        """Normalize one resolved elaboration recipe."""
        handle = self._target_handle(target)
        inspection = TargetCatalog.build(handle.project_root).inspect(handle)
        command = tuple(self._elab_only_dry_command(target))
        if len(command) == 1 and command[0].startswith("ERROR:"):
            raise ValueError(command[0])
        sources, constraints = normalize_plan_inputs(inspection.inputs, self.args.work_dir)
        build_root = preview_generation_root(handle)
        recipe = {
            "build_timeout_ms": self._effective_build_timeout_ms(),
            "environment_fingerprint": plan_value_fingerprint(self._target_sim_env(target)),
            "flow_options": inspection.flow_options,
            "mode": self._mode.value,
            "toplevel": inspection.toplevel,
        }
        return WorkUnitPlan(
            unit_id=stable_unit_id("sim", target, ("elaboration",)),
            role="ordinary",
            revision=None,
            selector=target,
            target_identity=handle.identity,
            test_or_module_scope=("elaboration",),
            eda_tool=inspection.eda_tool,
            timeout_ms=self._effective_build_timeout_ms(),
            sources=sources,
            constraints=constraints,
            parameters=inspection.parameters,
            recipe=recipe,
            commands=(
                CommandPlan(
                    normalize_plan_argv(
                        self._redact_plan_environment(target, command),
                        self.args.work_dir,
                    ),
                    cwd=".",
                    template=True,
                ),
            ),
            expected_artifacts=(
                normalize_plan_path(build_root / RUN_LOG_NAME, self.args.work_dir),
            ),
        )

    def _planned_standalone_unit(
        self,
        targets: list[str],
    ) -> tuple[WorkUnitPlan | None, str | None]:
        """Plan the optional standalone sweep and preserve its execution recipe."""
        if not self._standalone_requested():
            return None, None
        try:
            standalone = self._plan_standalone_check(targets)
        except (fusesoc_registry.FuseSocError, OSError, ValueError) as exc:
            return None, f"standalone: {exc}"
        self._standalone_plan_recipe = standalone
        return self._standalone_work_unit(targets, standalone), None

    def _standalone_work_unit(
        self,
        targets: list[str],
        standalone: _StandalonePlanRecipe,
    ) -> WorkUnitPlan:
        modules = tuple(module for module, _path in standalone.modules)
        sources = tuple(
            dict.fromkeys([path for _module, path in standalone.modules] + list(standalone.shared))
        )
        identities = [self._target_handle(target).identity for target in targets]
        return WorkUnitPlan(
            unit_id=stable_unit_id("sim", "standalone", modules, role="standalone"),
            role="standalone",
            revision=None,
            selector=",".join(targets),
            target_identity="+".join(identities),
            test_or_module_scope=modules,
            eda_tool=standalone.frontend_label,
            timeout_ms=self._effective_timeout_ms(),
            sources=tuple(normalize_plan_path(path, self.args.work_dir) for path in sources),
            parameters={},
            recipe={
                "frontend": standalone.frontend_label,
                "frontends": list(standalone.frontends),
                "mode": self._mode.value,
                "module_sources": [
                    {
                        "module": probe.module,
                        "source": probe.file,
                        "frontend": probe.frontend,
                        "fallback": probe.fallback,
                        "shared_sources": list(probe.prereqs),
                    }
                    for probe in standalone.probes
                ],
                "shared_sources": list(standalone.shared),
            },
            commands=tuple(
                CommandPlan(
                    normalize_plan_argv(command, self.args.work_dir),
                    cwd=".",
                )
                for command in standalone.commands
            ),
            expected_artifacts=(
                normalize_plan_path(
                    work_root_for(
                        self.args.work_dir,
                        "sim",
                        "standalone",
                        variant="sweep",
                    )
                    / RUN_LOG_NAME,
                    self.args.work_dir,
                ),
            ),
        )

    def _run_elab_only_campaign(
        self,
        targets: list[str],
    ) -> list[ElabOnlyTargetResult]:
        """Run and checkpoint each requested build-only Target."""
        self.reserve_invocation_dir()
        results = self._elab_progress_results
        self._write_elab_only_progress(targets, results, phase="starting")
        for target in targets:
            with attempt_scope(target, operation="elaboration"):
                result = self._run_one_elab_only(target)
            self._record_elab_only_criterion(result)
            self._write_elab_only_target_report(result)
            if self.state._file_path is not None:
                self.state.save()
            results.append(result)
            self._write_elab_only_progress(targets, results, phase="running")
        return results

    def _run_optional_standalone(
        self,
        targets: list[str],
        results: list[ElabOnlyTargetResult],
    ) -> tuple[int, _StandaloneOutcome | None]:
        """Merge the optional module sweep into the campaign exit status."""
        exit_code = self._elab_only_exit_code(results)
        if not self._standalone_requested():
            return exit_code, None
        with attempt_scope(targets[0], operation="standalone"):
            standalone = self._run_standalone_check(
                targets,
                primary_ok=all(result.outcome.passed for result in results),
            )
        if standalone.eda_tool_failed:
            exit_code = EXIT_ERROR
        elif not standalone.passed and exit_code == EXIT_SUCCESS:
            exit_code = EXIT_FAILURE
        return exit_code, standalone

    def _elab_only_result(
        self,
        targets: list[str],
        results: list[ElabOnlyTargetResult],
        exit_code: int,
        standalone: _StandaloneOutcome | None,
    ) -> EndpointOutcome:
        """Compose the final compile-only report and MCP result."""
        passed = sum(result.outcome.passed for result in results)
        verdict = {EXIT_SUCCESS: "PASS", EXIT_FAILURE: "FAIL", EXIT_ERROR: "ERROR"}[exit_code]
        lines = [self._elab_only_result_line(result) for result in results]
        if standalone is not None:
            lines.extend(standalone.lines)
        lines += ["", f"RESULT: {verdict} ({passed}/{len(results)})"]
        report_text = self._with_campaign_paths(
            EndpointOutcome(report_text="\n".join(lines))
        ).report_text
        print(report_text)
        for result in results:
            self._record_eda_tool(result.target, result.eda_tool)
        detail: dict[str, Any] = {
            "mode": self._mode.value,
            "targets": [self._elab_only_detail(result) for result in results],
        }
        artifacts = {
            result.target: {"log": result.log_path} for result in results if result.log_path
        }
        if artifacts:
            detail["artifacts"] = artifacts
        display_lines = [
            f"{result.target}: {self._elab_only_status(result)}" for result in results
        ]
        if standalone is not None:
            detail["standalone"] = standalone.detail
            display_lines.append(standalone.display)
        return EndpointOutcome(
            exit_code=exit_code,
            criterion_key=(f"elab_pass_{targets[0]}" if len(targets) == 1 else ""),
            criterion_met=len(results) == 1 and results[0].outcome.passed,
            display_lines=display_lines,
            detail=detail,
            report_text=report_text,
        )

    def _run_one_elab_only(self, target: str) -> ElabOnlyTargetResult:
        """Run one canonical untraced Simulation build and archive its output."""
        from booley.flows.terminal_progress import announce_unit

        observe_stage(target, "preparing")
        announce_unit("configure elaboration-only", target=target)
        started = monotonic()
        handle = self._target_handle(target)
        try:
            compile_surface = resolve_target_compile_surface(handle)
            sources_before = project_compile_surface(compile_surface)
            with SimulationBuildSession(handle) as session:
                candidate = session.new_generation()
                self._open_run_log(target, candidate)
                prepared = self._prepare_elab_only_target(
                    target, started, handle=handle, build_root=candidate
                )
                if isinstance(prepared, ElabOnlyTargetResult):
                    return prepared
                if project_compile_surface(compile_surface) != sources_before:
                    raise SimulationBuildSlotError(
                        "Project compile inputs changed during elaboration preparation"
                    )
                self._register_prepared_build(target, prepared)
                inputs = session.capture_inputs(prepared)
                result = self._execute_elab_only_build(target, prepared)
                if result.outcome.passed:
                    session.authorize_fresh_image(prepared, inputs)
                return result
        except SimulationBuildSlotError as exc:
            outcome = setup_failure_outcome(
                f"Simulation build slot failed: {exc}",
                elapsed_s=monotonic() - started,
            )
            return ElabOnlyTargetResult(target=target, outcome=outcome)

    def _prepare_elab_only_target(
        self,
        target: str,
        started: float,
        *,
        handle: TargetHandle,
        build_root: Path,
    ) -> PreparedSimulationBuild | ElabOnlyTargetResult:
        """Prepare one Target or return its expected setup-error result."""
        try:
            return prepare_simulation_build(
                handle,
                build_root=build_root,
                environment=self._target_sim_env(target),
            )
        except SimulationBuildPreparationError as exc:
            logger.debug("sim elab-only setup failed for %s", target, exc_info=True)
            outcome = setup_failure_outcome(
                f"setup failed: {exc}",
                elapsed_s=monotonic() - started,
            )
            result = ElabOnlyTargetResult(target=target, outcome=outcome)
            result.log_path = self._persist_elab_only_log(target, outcome.output)
            return result

    def _register_prepared_build(
        self,
        target: str,
        prepared: PreparedSimulationBuild,
    ) -> None:
        """Register prepared Target state used by reports and optional sweeps."""
        self._open_run_log(target, Path(prepared.build_root))
        self._record_eda_tool(target, prepared.eda_tool)

    def _execute_elab_only_build(
        self,
        target: str,
        prepared: PreparedSimulationBuild,
    ) -> ElabOnlyTargetResult:
        """Execute and classify one already-prepared build-only Target."""
        token = new_attempt_token()
        command = [
            "sh",
            "-c",
            simulation_build_script(prepared, token),
        ]
        from booley.flows.terminal_progress import announce_unit

        observe_stage(target, "building", evidence_root=prepared.build_root, attempt_token=token)
        announce_unit("elaboration-only build", target=target)
        timeout_s = max(1, self._effective_build_timeout_ms() // 1000)
        proc = self._execute_boundary(command, timeout=timeout_s)
        outcome = classify_build_outcome(proc, token, timeout_s=timeout_s)
        result = ElabOnlyTargetResult(
            target=target,
            target_identity=prepared.target_identity,
            eda_tool=prepared.eda_tool,
            toplevel=prepared.toplevel,
            compile_command=shlex.join(command),
            fileset={name: list(paths) for name, paths in prepared.fileset.items()},
            outcome=outcome,
        )
        result.log_path = self._persist_elab_only_log(target, outcome.output)
        return result

    def _persist_elab_only_log(self, target: str, output: str) -> str:
        """Archive complete build output outside the mutable shared cache."""
        invocation_dir = self.reserve_invocation_dir()
        if invocation_dir is not None:
            log_dir = invocation_dir / "artifacts" / _artifact_path_component(f"sim_{target}")
        else:
            token = new_attempt_token()[:12]
            log_dir = work_root_for(self.args.work_dir, "sim", target) / f"elab-only-{token}"
        try:
            log_dir.mkdir(parents=True, exist_ok=True)
            path = write_run_log(log_dir, output, max_bytes=None)
        except OSError:
            logger.debug("could not persist elab-only log for %s", target, exc_info=True)
            return ""
        return posix_relpath(path, self.args.work_dir)

    def _record_elab_only_criterion(self, result: ElabOnlyTargetResult) -> None:
        """Write elaboration evidence only when a real design verdict exists."""
        if result.outcome.verdict is None:
            return
        self.set_criterion(
            f"elab_pass_{result.target}",
            result.outcome.passed,
            source_target=result.target,
            detail={
                "mode": self._mode.value,
                "target": result.target,
                "elapsed_s": round(result.outcome.elapsed_s, 3),
                "error_gist": (
                    extract_error_gist(result.outcome.output)
                    if result.outcome.design_failed
                    else ""
                ),
            },
        )

    @staticmethod
    def _elab_only_exit_code(results: list[ElabOnlyTargetResult]) -> int:
        if any(result.outcome.failure_kind == "infrastructure" for result in results):
            return EXIT_ERROR
        if any(result.outcome.design_failed for result in results):
            return EXIT_FAILURE
        return EXIT_SUCCESS

    @staticmethod
    def _elab_only_status(result: ElabOnlyTargetResult) -> str:
        if result.outcome.passed:
            return "PASS"
        return "FAIL" if result.outcome.design_failed else "ERROR"

    def _elab_only_result_line(self, result: ElabOnlyTargetResult) -> str:
        status = self._elab_only_status(result)
        line = f"[sim:elab-only] {result.target} {status} {result.outcome.elapsed_s:.1f}s"
        if not result.outcome.passed and result.outcome.reason:
            line += f" — {result.outcome.reason}"
        if result.log_path:
            line += f" (log: {result.log_path})"
        return line

    def _elab_only_detail(self, result: ElabOnlyTargetResult) -> dict[str, Any]:
        return {
            "target": result.target,
            "target_identity": result.target_identity,
            "eda_tool": result.eda_tool,
            "toplevel": result.toplevel,
            "compile_command": result.compile_command,
            "fileset": result.fileset,
            "elapsed_s": round(result.outcome.elapsed_s, 3),
            "passed": result.outcome.passed,
            "verdict": result.outcome.verdict,
            "failure_class": result.outcome.failure_kind,
            "reason": result.outcome.reason,
            "log": result.log_path,
        }

    def _write_elab_only_target_report(self, result: ElabOnlyTargetResult) -> None:
        report_dir = self.args.report_dir
        if report_dir is None:
            return
        report = {
            "flow": "sim",
            "mode": self._mode.value,
            "timestamp": utc_now_rfc3339(),
            **self._elab_only_detail(result),
        }
        report_dir.mkdir(parents=True, exist_ok=True)
        invocation_dir = self.reserve_invocation_dir()
        assert invocation_dir is not None
        path = target_report_directory(invocation_dir, result.target) / "simulation.json"
        _atomic_write_json(path, report)

    def _write_elab_only_progress(
        self,
        targets: list[str],
        results: list[ElabOnlyTargetResult],
        *,
        phase: str,
        complete: bool = False,
    ) -> None:
        """Checkpoint an Elaboration Check campaign after every Target."""
        invocation_dir = self.reserve_invocation_dir()
        if invocation_dir is None:
            return
        completed = [result.target for result in results]
        payload = progress_document(
            flow=self.name,
            run_id=os.environ.get("BOOLEY_RUN_ID", ""),
            phase=phase,
            targets=targets,
            completed_targets=completed,
            detail={result.target: self._elab_only_detail(result) for result in results},
            extra={
                "mode": self._mode.value,
            },
        )
        if complete != bool(payload["complete"]):
            raise ValueError("elaboration progress complete and phase disagree")
        publish_checkpoint(invocation_dir / "progress.json", payload)

    def _handle_elab_only_dry_run(self, targets: list[str]) -> EndpointOutcome:
        del targets
        return self._dry_run_result(self._flow_plan)

    def _elab_only_dry_command(self, target: str) -> list[str]:
        try:
            handle = self._target_handle(target)
            build_root = preview_generation_root(handle)
            setup = simulation_setup_command(
                handle,
                build_root=build_root,
            )
        except (
            fusesoc_registry.TargetResolutionError,
            SimulationBuildPreparationError,
        ) as exc:
            return [f"ERROR: sim elab-only dry-run: {exc}"]
        rel = edam_layer.relpath_for_make(build_root, self.args.work_dir)
        parts = [
            *self._sim_env_preview_lines(target),
            shlex.join(setup),
            shlex.join(edam_layer.make_command(rel)),
        ]
        return ["sh", "-c", " && ".join(parts)]

    def _cycle_baseline_selection(
        self, targets: list[str]
    ) -> tuple[str | None, list[str], str | None]:
        """Return the pinned ref and selected Targets needing relative evidence."""
        from booley.evidence.fields import BASELINE_REF_PARAM

        refs: set[str] = set()
        selected: list[str] = []
        handles = {target: self._target_handle(target) for target in targets}
        for key, entry in self.state.criteria.items():
            params = entry.params or {}
            target = self._matching_cycle_target(handles, params)
            if not key.startswith("cycle_count_") or target is None:
                continue
            ref = params.get(BASELINE_REF_PARAM)
            if not has_relative_threshold(params) or not isinstance(ref, str) or not ref:
                continue
            refs.add(ref)
            baseline = params.get(BASELINE_TARGET_PARAM, target)
            if not isinstance(baseline, str) or not baseline:
                return None, selected, "sim: Cycle Count baseline Target is invalid"
            if baseline not in selected:
                selected.append(baseline)
        if not refs:
            return None, [], None
        if len(refs) != 1:
            return (
                None,
                selected,
                "sim: selected Cycle Count criteria carry conflicting baseline refs",
            )
        ref = next(iter(refs))
        resolved = git_full_sha(ref, Path(self.args.work_dir))
        if resolved is None:
            return (
                ref,
                selected,
                "sim: Cycle Count pinned baseline ref cannot be resolved to a commit",
            )
        return resolved, selected, None

    @staticmethod
    def _matching_cycle_target(handles: dict[str, Any], params: dict[str, Any]) -> str | None:
        return next(
            (
                selector
                for selector, handle in handles.items()
                if criterion_matches_target(
                    params, identity=handle.identity, selector=handle.selector
                )
            ),
            None,
        )

    def _cycle_baseline_test_names(
        self, test_names_map: dict[str, list[str]]
    ) -> dict[str, list[str]]:
        """Include selected relative tests on their actual baseline Targets."""
        result = {target: list(tests) for target, tests in test_names_map.items()}
        for key, entry in self.state.criteria.items():
            params = entry.params or {}
            if not key.startswith("cycle_count_") or not has_relative_threshold(params):
                continue
            baseline = params.get(BASELINE_TARGET_PARAM)
            test = params.get("test")
            if isinstance(baseline, str) and isinstance(test, str):
                tests = result.setdefault(baseline, [])
                if test not in tests:
                    tests.append(test)
        return result

    def _missing_executable_result(
        self,
        exc: MissingExecutableError,
        target: str,
    ) -> EndpointOutcome:
        """Grade an absent EDA binary as a Flow error (exit 2), not a failure.

        The verdict channel must not say "test FAIL" when no test ever ran
        (F-32): with ``fusesoc`` hidden from PATH the old path printed
        ``run_test_001 FAIL 0.0s``, and with ``verilator`` hidden it printed
        ``sim_basic FAIL (0/1 tests)`` under an "elaboration failed" banner
        that named the wrong stage entirely. Both read, to a triage agent, as a
        broken design. The criterion is deliberately left unset — a run that
        observed nothing may not grade anything.
        """
        message = (
            f"sim: required executable {exc.binary!r} was not found in the "
            f"Sandbox while running Target {target!r}. No "
            "simulation ran, so there is no pass/fail verdict about the design. "
            f"Install {exc.binary!r} (or put it on PATH) in the Sandbox "
            "and re-run; `booley doctor` checks the toolchain."
        )
        tail = "\n".join(exc.context.strip().splitlines()[-15:])
        report_text = self._with_campaign_paths(
            EndpointOutcome(
                report_text=f"{message}\n\n--- output tail ---\n{tail}" if tail else message
            )
        ).report_text
        print(report_text)
        return EndpointOutcome(
            exit_code=EXIT_ERROR,
            display_lines=[f"Flow error: {exc.binary} not found"],
            detail={
                "eda_tool_error": "missing_executable",
                "missing_executable": exc.binary,
                "target": target,
            },
            report_text=report_text,
        )

    def _resolve_requested_targets(self) -> list[str] | EndpointOutcome:
        handles = self._selected_target_handles()
        self._target_handles = {handle.selector: handle for handle in handles}
        targets = [handle.selector for handle in handles]
        if targets:
            return targets
        return EndpointOutcome(
            exit_code=EXIT_ERROR,
            report_text=(
                "sim: --target is required. Pass --target <name> (or a "
                "comma-separated list); list available Targets with `booley targets`."
            ),
        )

    def _target_identity(self, target: str) -> str:
        """The identity (``vlnv#name``) of the selected Target written *target*."""
        return self._target_handle(target).identity

    def _target_handle(self, target: str) -> TargetHandle:
        """Return the selected handle for the active checkout.

        Cycle-count baselines intentionally materialize another checkout, so
        they reselect and verify the planned selector in that checkout.
        """
        root = Path(self.args.work_dir).resolve()
        selected = getattr(self, "_target_handles", {}).get(target)
        if selected is not None and selected.project_root == root:
            return selected
        return TargetCatalog.build(root).select(target, for_flow="sim")

    def _record_eda_tool(self, target: str, eda_tool: str | None) -> None:
        """Remember *target*'s raw resolved EDA tool (e.g. ``"verilator"``).

        Raw — not the ``normalize_eda_tool`` run-half family — because this
        feeds report observability (the per-target tool and the run-level
        ``eda_tool`` key), not parser dispatch. Recorded at prepare time.
        """
        self.record_eda_tool(target, eda_tool)

    def _simulation_execution(self) -> SimulationExecution:
        """Compose the execution boundary with the Flow's process transport."""
        override = getattr(self, "_simulation_execution_override", None)
        if override is not None:
            return cast(SimulationExecution, override)
        return SimulationExecution(
            invoke=self._execute_boundary,
            options=SimulationOptions(
                trace=self.args.trace,
                timeout_ms=self.args.timeout_ms,
                build_timeout_ms=self._effective_build_timeout_ms(),
                result_verbosity=self.args.result_verbosity,
            ),
        )

    def _effective_skips(self, target: str) -> set[str]:
        """Return Project-configured exact names excluded by default."""
        return set(
            lookup_target_section(
                _get_test_skips(self.args.work_dir), self._target_identity(target)
            )
            or []
        )

    def _validate_test_selector(
        self,
        targets: list[str],
        test_names_map: dict[str, list[str]],
    ) -> EndpointOutcome | None:
        """Reject an unknown ``--test`` before any sim runs (built-in path).

        A target that declares a test list (tests.toml ``tests``) but whose list
        has no substring match for ``--test`` is a typo, not a selector: running
        it would emit no selection plusarg and silently execute the testbench's
        default test — reporting a false PASS.
        Mirrors the ``UnknownTargetError`` contract ``--target`` already gets.

        Skipped for a target with no declared test list: there the ``--test``
        value is a raw passthrough the TB owns (matching
        ``resolve_target_selection``'s transitional skip when the Target list is
        unknown). Returns an ``EXIT_ERROR`` EndpointOutcome on the first offending
        target, else ``None``.
        """
        selectors = self.args.test
        if not selectors:
            return None
        for target in targets:
            available = lookup_target_section(test_names_map, self._target_identity(target)) or []
            if not available:
                return EndpointOutcome(
                    exit_code=EXIT_ERROR,
                    report_text=(
                        f"sim: exact named selection requires target {target!r} "
                        "to have a registered test catalog"
                    ),
                )
            unknown = [
                name for name in selectors if parse_test_selector(name).test not in available
            ]
            if unknown:
                return EndpointOutcome(
                    exit_code=EXIT_ERROR,
                    report_text=(
                        f"sim: unknown exact test(s) for target {target!r}: "
                        f"{', '.join(unknown)}. Declared tests: {', '.join(available)}."
                    ),
                )
        return None

    def _validate_cocotb_targets(self, targets: list[str]) -> EndpointOutcome | None:
        """Reject a plusarg ``select`` template on a Cocotb Target (A2).

        On a Cocotb Target test selection is the ``COCOTB_TEST_FILTER`` env var
        Booley builds from the ``tests`` list — a plusarg ``select`` template is
        a config contradiction, rejected up front (a setup-time error, ADR 0034
        decision 5) rather than silently ignored. ``skip`` works unchanged.
        Returns an ``EXIT_ERROR`` EndpointOutcome on the first offending target,
        else ``None``.
        """
        selects = _get_test_selects(self.args.work_dir)
        for target in targets:
            if lookup_target_section(
                selects, self._target_identity(target)
            ) is not None and self.is_cocotb_target(target):
                return EndpointOutcome(
                    exit_code=EXIT_ERROR,
                    report_text=(
                        f"sim: tests.toml [{target}] declares a `select` "
                        f"plusarg template, but {target!r} is a Cocotb Target "
                        "(its .core flow options declare a cocotb_module). "
                        "Cocotb test selection is driven by the "
                        "COCOTB_TEST_FILTER environment variable built from "
                        "the `tests` list — remove the `select` key."
                    ),
                )
        return None

    def _target_test_plan(self, target: str) -> TargetTestPlan:
        """The Test Run plan of *target*, checked against its resolved ``.core``."""
        handle = self._target_handle(target)
        inspection = TargetCatalog.build(handle.project_root).inspect(handle)
        adapter = simulation_adapter_kind(inspection.eda_tool, inspection.flow_options)
        return target_test_plan(handle, adapter, inspection.parameters)

    def _validate_test_plans(
        self, targets: list[str], test_names_map: Mapping[str, list[str]] | None = None
    ) -> EndpointOutcome | None:
        """Reject a tests.toml declaration its resolved Target cannot apply.

        The loader cannot see the ``.core`` Target, so the checks that need it
        (ADR 0072) run here before anything is built: a Test Variant that
        overrides a Target plusarg, arguments or a ``seed`` template on a
        Cocotb Target, and ``default_seed`` on a Target that cannot be seeded.
        """
        if test_names_map is None:
            test_names_map = _get_test_names(self.args.work_dir)
        for target in targets:
            try:
                self._resolve_test_runs(target, test_names_map)
            except fusesoc_registry.FuseSocError:
                # Target resolution reports its own error on the run path.
                continue
            except ValueError as exc:
                return EndpointOutcome(exit_code=EXIT_ERROR, report_text=f"sim: {exc}")
        return None

    def _frozen_test_runs(self) -> FrozenTestRuns | None:
        """The Test Runs this run's Goals are frozen to; ``None`` outside Goal Mode.

        Read once per invocation: what the invocation runs and what its
        report calls diagnostic come from one reading.
        """
        if not self._test_runs.frozen:
            self._test_runs.frozen = (frozen_test_runs_for(self.execution_adapter),)
        return self._test_runs.frozen[0]

    def _with_diagnostic_test_run_note(self, result: EndpointOutcome) -> EndpointOutcome:
        """Name the Test Runs of this invocation that are evidence for no Goal (ADR 0072).

        Inside Goal Mode a Test Run is evidence only by matching a frozen Test
        Run of a Goal. Every other Test Run is diagnostic: it is reported, and
        a failing one prominently, but it blocks no Goal.
        """
        frozen = self._frozen_test_runs()
        if frozen is None:
            return result
        if self.args.diagnostic:
            adapter = self.execution_adapter
            return _with_frozen_runs_of_a_diagnostic_run(
                result, frozen, self._target_names, lambda key: execution_goal_name(adapter, key)
            )
        fresh = [
            f"{target}: {selector}"
            for target, selectors in self._test_runs.fresh_seed_selectors.items()
            for selector in selectors
        ]
        diagnostic, failures = _diagnostic_test_runs(result.detail, frozen, self._target_names)
        if not (fresh or diagnostic):
            return result
        result.detail["diagnostic_test_runs"] = {
            "test_runs": diagnostic,
            "failures": failures,
            "fresh_seed_selectors": fresh,
        }
        note = "\n".join(_diagnostic_test_run_notes(fresh, diagnostic, failures))
        result.report_text = f"{result.report_text}\n{note}" if result.report_text else note
        return result

    def _validate_runnable_tests(
        self,
        targets: list[str],
        test_names_map: dict[str, list[str]],
    ) -> EndpointOutcome | None:
        """Reject Targets whose skip policy excludes every declared test."""
        if self.args.test:
            return None  # an explicit selector deliberately overrides skips
        for target in targets:
            available = list(
                lookup_target_section(test_names_map, self._target_identity(target)) or []
            )
            try:
                require_runnable_target_test_suite(
                    target,
                    identity=self._target_identity(target),
                    test_names={target: available},
                    test_skips={target: list(self._effective_skips(target))},
                )
            except NoRunnableTestsError as exc:
                return EndpointOutcome(
                    exit_code=EXIT_ERROR,
                    report_text=(
                        f"sim: {exc}. Tests are excluded by tests.toml `skip`. "
                        "Remove a configured skip or explicitly select a test "
                        "with --test."
                    ),
                )
        return None

    def _resolve_tests_to_run(
        self,
        target: str,
        test_names_map: dict[str, list[str]],
    ) -> list[str | None]:
        """Resolve the list of tests to run for a target, minus skip list.

        Project-configured skips prune known-hanging tests so
        they don't each burn the full per-test wall-clock budget. An explicit
        ``--test`` selector that matches *only* skipped tests still runs them —
        naming a test by hand is a clear override of the skip list. When every
        known test is skipped, pre-run validation rejects the Target rather
        than passing vacuously or executing known-hanging tests.
        """
        try:
            runs = self._resolve_test_runs(target, test_names_map)
        except fusesoc_registry.FuseSocError:
            # The Target does not resolve; the run path reports that. Name the
            # registered Tests so plans and labels still describe the request.
            return self._registered_tests_to_run(target, test_names_map)
        return list(dict.fromkeys(run.test for run in runs))

    def _registered_tests_to_run(
        self, target: str, test_names_map: Mapping[str, list[str]]
    ) -> list[str | None]:
        """Selected Test names from the registry alone, without the ``.core`` Target."""
        if self.args.test:
            available = lookup_target_section(test_names_map, self._target_identity(target)) or []
            wanted = [parse_test_selector(text).test for text in self.args.test]
            return cast(list[str | None], _filter_tests(available, wanted))
        suite = resolve_target_test_suite(
            target,
            identity=self._target_identity(target),
            test_names=test_names_map,
            test_skips=_get_test_skips(self.args.work_dir),
        )
        return list(suite.tests)

    def _resolve_test_runs(
        self,
        target: str,
        test_names_map: Mapping[str, list[str]],
    ) -> tuple[TestRun, ...]:
        """The concrete Test Runs this invocation runs on *target* (ADR 0072).

        Resolved once per checkout and Target, before any plan or manifest is
        built: a seed drawn for ``--seed random`` or ``test@xN`` is concrete
        from then on and is never drawn again. Raises ``ValueError`` (the Test
        Run error types) for a request this Target cannot run.
        """
        cache = self._test_runs.resolved
        key = (str(Path(self.args.work_dir)), target)
        if key not in cache:
            plan = self._registered_test_plan(target, test_names_map)
            self._test_runs.default_seeds.setdefault(target, plan.default_seed)
            cache[key] = self._plan_test_runs(plan, target)
        return cache[key]

    def _plan_test_runs(self, plan: TargetTestPlan, target: str) -> tuple[TestRun, ...]:
        """Resolve this request on *plan*; inside Goal Mode from the frozen Test Runs.

        A request with ``--seed`` names its own seeds and always resolves from
        the current ``tests.toml``. Without it, a Target run without ``--test``
        runs the frozen Test Runs of its Target Goal, and ``test@xN`` runs the
        frozen seeds of the Goal with that Target, Test Variant and N; a
        ``test@xN`` no Goal names draws new seeds and is reported diagnostic.
        """
        selectors = tuple(self.args.test) if self.args.test else None
        seed = getattr(self.args, "seed", None)
        frozen = self._frozen_test_runs()
        if frozen is None or seed is not None:
            return plan.resolve(selectors, seed=seed)
        runs, fresh = resolve_goal_test_runs(plan, selectors, frozen, self._target_names(target))
        if fresh:
            self._test_runs.fresh_seed_selectors[target] = fresh
        return runs

    def _target_names(self, target: str) -> set[str]:
        """What a Goal may call *target*: its selector, and its identity when it resolves."""
        try:
            return {target, self._target_handle(target).identity}
        except fusesoc_registry.FuseSocError:
            return {target}

    def _registered_test_plan(
        self, target: str, test_names_map: Mapping[str, list[str]]
    ) -> TargetTestPlan:
        """*target*'s Test Run plan over the registry this invocation resolved."""
        plan = self._target_test_plan(target)
        registered = tuple(
            lookup_target_section(test_names_map, self._target_identity(target)) or ()
        )
        skipped = tuple(name for name in registered if name in self._effective_skips(target))
        if (registered, skipped) == (plan.tests, plan.skipped):
            return plan
        return replace(plan, tests=registered, skipped=skipped)

    def _test_run_selection(
        self, target: str, test_names_map: Mapping[str, list[str]]
    ) -> SelectedTestRuns | NamedTests | DefaultSelection:
        """The execution selection of *target*: its resolved Test Runs.

        Raises ``FuseSocError`` when the Target does not resolve; planning
        reports it by Target. The Tests' defaults are never selected instead.
        """
        runs = self._resolve_test_runs(target, test_names_map)
        if not runs:
            return DefaultSelection()
        return SelectedTestRuns(runs)


if __name__ == "__main__":
    SimulateFlow().cli()
