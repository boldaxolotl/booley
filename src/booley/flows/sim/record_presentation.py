"""Simulation Campaign result presentation: report lines, details, artifacts, cards and exit grading."""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Callable, Collection, Mapping, Sequence
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from typing import Any, cast

from booley.flows.frozen_test_runs import FrozenTestRuns
from booley.flows.sim.coverage_campaign import CoverageCampaign, CoverageRollup
from booley.flows.sim.coverage_json import FrozenJson
from booley.flows.sim.coverage_policy import coverage_metrics
from booley.flows.sim.coverage_progress import CoverageProgress
from booley.flows.sim.coverage_transaction import CoverageTargetOutcome
from booley.flows.sim.record.test_run_files import TestRunFiles, campaign_test_run_files
from booley.flows.sim.record.test_run_report import ROW_LIMIT as TEST_RUN_ROW_LIMIT
from booley.flows.sim.record.test_run_report import (
    campaign_test_run_lines,
    campaign_test_run_rows,
)
from booley.flows.sim.record_files import document_digest
from booley.flows.test_runs import (
    DEFAULT_SEED,
)
from booley.runtime.endpoint_execution import (
    EXIT_ERROR,
    EXIT_FAILURE,
    EXIT_SUCCESS,
    EndpointOutcome,
)

from .. import output_budget
from .build import (
    BuildOutcome,
    SimulationBuildInfrastructureError,
)
from .coverage_campaign_store import load_coverage_campaign
from .coverage_reference import (
    MAX_REFERENCE_BYTES,
    resolve_coverage_campaign_reference,
)
from .coverage_source_gaps import source_gap_report_lines
from .metrics import coverage_campaign_path
from .record import (
    CampaignOutcome,
    CampaignRecoveryStatus,
    NewCampaignPreview,
    NewCampaignRunRequest,
    build_artifact_reference,
    encode_artifact_reference,
)
from .record.codec import MANIFEST_MAX_BYTES
from .record.coordinator import CampaignPublicationError, CampaignPublicationFailure
from .record.coverage_execution import (
    _CoverageAggregateError,
    _endpoint_collection,
    _failure_test,
)
from .record.model import simulation_status_from_observations
from .test_run_invocation import (
    BaselineRefusal,
)


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


def _coverage_campaign_lines(outcomes: Sequence[CampaignOutcome]) -> list[str]:
    """One ``coverage campaign:`` line per finished coverage outcome."""
    return [
        f"  coverage campaign: {coverage_campaign_path(outcome).resolve()}"
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
        public = coverage_campaign_path(outcome)
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
    result.detail["record_integrity_error"] = str(error)
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
            kind="simulation_record_manifest",
            owner=str(document["simulation_record_id"]),
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
        "simulation_record_id": owner,
        "manifest_sha256": facts.get("manifest_sha256"),
        "origin_invocation_id": origin.get("invocation_id"),
        "dependency": "local_record" if local else "external_origin_record",
        "dependency_available": "manifest" in artifacts,
        "artifacts": artifacts,
        "grade": outcome.aggregate_grade,
        "complete": outcome.complete,
        "observation_counts": _campaign_observation_counts(outcome.observations),
        **_campaign_observation_preview(
            outcome.observations,
            campaign_test_run_files(outcome),
            outcome.manifest_path.parents[1],
        ),
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
    campaigns = detail.get("records")
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
    campaigns = result.detail.get("records")
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
    """Report rows only where they say more than the Target's own line.

    The rows name each Test Run's evidence files: where its log and waveform are.
    """
    if not isinstance(getattr(outcome, "manifest_path", None), Path):
        return []
    return campaign_test_run_lines(selector, outcome, default_seed=default_seed)


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
            "simulation_record_manifest",
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
            "coverage_record_reference",
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
    from .record_retention import invocation_booley_dir

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
        detail={"record_preview": list(previews), "mutated": False},
        report_text="\n".join(
            f"{cast(Mapping[str, object], item['target'])['selector']}: "
            f"{item['work_items']} pending work item(s)"
            for item in previews
        ),
    )


def _campaign_observation_preview(
    observations: Sequence[Mapping[str, object]],
    files: Sequence[TestRunFiles],
    target_directory: Path,
) -> dict[str, object]:
    """The bounded observation rows, each with its Test Run's ``log`` and ``trace`` paths.

    *files* is aligned with *observations*; the paths are relative to
    *target_directory*, the directory of the Target's ``simulation.json``, and
    ``None`` where the file does not exist.
    """
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
            **found.relative_to(target_directory),
        }
        for item, found in zip(observations[:limit], files[:limit], strict=True)
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
        public = coverage_campaign_path(outcome)
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
            target["coverage_record"] = reference
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
        kind="coverage_record_reference",
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
        detail["coverage_record"] = reference


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
            str(failure.coverage_reference.document["simulation_record_id"]),
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
            "records": _campaign_structured_details(outcomes),
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
            "records": _campaign_structured_details(outcomes, report_invocation),
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
