"""Cross-ticket execution metrics: cost, duration, success rates, failure modes, token efficiency."""

from __future__ import annotations

from typing import Any

from .constants import STEP_ORDER
from .helpers import fmt_duration, fmt_tokens

# ---------------------------------------------------------------------------
# Terminal states -- tickets whose run is finished
# ---------------------------------------------------------------------------

_TERMINAL_STATES = {"done", "review", "archived"}


# ---------------------------------------------------------------------------
# Report formatting
# ---------------------------------------------------------------------------


def _fmt_overview_section(s):
    """Format the overview summary table."""
    return [
        "## Overview",
        "",
        "| Metric | Value |",
        "|--------|------:|",
        f"| Total tickets | {s['total_tickets']} |",
        f"| Done | {s['done']} |",
        f"| Blocked | {s['blocked']} |",
        f"| In review | {s['review']} |",
        f"| Running | {s['running']} |",
        f"| Queued | {s['queued']} |",
        f"| Waiting | {s['waiting']} |",
        f"| **Success rate** | **{s['success_rate']:.0f}%** |",
        f"| Total cost | ${s['total_cost']:.2f} |",
        f"| Total tokens | {fmt_tokens(s['total_tokens'])} |",
        f"| Avg cost/completed ticket | ${s['avg_cost_per_ticket']:.2f} |",
        f"| Avg duration/completed ticket | {fmt_duration(s['avg_duration_completed'])} |",
        "",
    ]


def _fmt_type_stats_section(type_stats):
    """Format the per-ticket-type breakdown table."""
    if not type_stats:
        return []
    lines = [
        "## By Ticket Type",
        "",
        "| Type | Total | Done | Blocked | Success% | Avg Cost | Avg Duration |",
        "|------|------:|-----:|--------:|---------:|---------:|-------------:|",
    ]
    for ttype in sorted(type_stats.keys()):
        ts = type_stats[ttype]
        completed = ts["done"]
        sr = (ts["done"] / completed * 100) if completed else 0
        avg_cost = (ts["total_cost"] / completed) if completed else 0
        avg_dur = (ts["total_duration"] / completed) if completed else 0
        lines.append(
            f"| {ttype} | {ts['count']} | {ts['done']} | {ts['blocked']} | "
            f"{sr:.0f}% | ${avg_cost:.2f} | {fmt_duration(avg_dur)} |"
        )
    lines.append("")
    return lines


def _fmt_step_performance_section(step_stats):
    """Format the per-step performance table."""
    if not step_stats or not any(v["count"] > 0 for v in step_stats.values()):
        return []
    lines = [
        "## Step Performance",
        "",
        "| Stage | Runs | Avg Duration | Avg Tokens | Avg Cost | Failures |",
        "|-------|-----:|-------------:|-----------:|---------:|---------:|",
    ]
    for step in STEP_ORDER:
        ss = step_stats.get(step, {})
        if ss.get("count", 0) == 0:
            continue
        lines.append(
            f"| {step} | {ss['count']} | "
            f"{fmt_duration(ss['avg_duration'])} | "
            f"{fmt_tokens(int(ss['avg_tokens']))} | "
            f"${ss['avg_cost']:.2f} | {ss['failure_count']} |"
        )
    lines.append("")
    return lines


def _fmt_failure_sections(metrics):
    """Format failure hotspots and recent failure details."""
    lines = []
    failure_modes = metrics.get("failure_modes", {})
    if failure_modes:
        lines.extend(["## Failure Hotspots", "", "| Stage | Failures |", "|-------|---------:|"])
        for stage, count in sorted(failure_modes.items(), key=lambda x: -x[1]):
            lines.append(f"| {stage} | {count} |")
        lines.append("")

    failure_errors = metrics.get("failure_errors", [])
    if failure_errors:
        lines.extend(
            ["## Recent Failures", "", "| Ticket | Stage | Error |", "|--------|-------|-------|"]
        )
        for fe in failure_errors:
            lines.append(f"| {fe['slug']} | {fe['stage']} | {fe['error']} |")
        lines.append("")
    return lines


def _fmt_cost_and_efficiency_sections(tickets):
    """Format the per-ticket cost table and token efficiency summary."""
    completed = [t for t in tickets if t["status"] in _TERMINAL_STATES and t["cost"] > 0]
    if not completed:
        return []
    completed.sort(key=lambda t: -t["cost"])
    lines = [
        "## Cost Per Ticket",
        "",
        "| Ticket | Type | Status | Cost | Tokens | Duration | Stages |",
        "|--------|------|--------|-----:|-------:|---------:|-------:|",
    ]
    for t in completed:
        lines.append(
            f"| {t['slug'][:40]} | {t['type']} | {t['status']} | "
            f"${t['cost']:.2f} | {fmt_tokens(t['total_tokens'])} | "
            f"{fmt_duration(t['duration_secs'])} | {t['steps_completed']} |"
        )
    lines.append("")

    costs = [t["cost"] for t in completed]
    median_idx = len(costs) // 2
    lines.extend(
        [
            "## Token Efficiency",
            "",
            f"- **Cheapest ticket**: ${min(costs):.2f}",
            f"- **Most expensive ticket**: ${max(costs):.2f}",
            f"- **Median cost**: ${sorted(costs)[median_idx]:.2f}",
            "",
        ]
    )
    return lines


def format_metrics_report(metrics: dict[str, Any]) -> str:
    """Format run metrics as a markdown dashboard report."""
    lines = ["# Run Metrics Dashboard", ""]
    lines.extend(_fmt_overview_section(metrics["summary"]))
    lines.extend(_fmt_type_stats_section(metrics.get("type_stats", {})))
    lines.extend(_fmt_step_performance_section(metrics.get("step_stats", {})))
    lines.extend(_fmt_failure_sections(metrics))
    lines.extend(_fmt_cost_and_efficiency_sections(metrics["tickets"]))
    return "\n".join(lines)
