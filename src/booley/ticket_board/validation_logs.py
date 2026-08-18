"""Validate a ticket's run logs + state file and format the report.

What: given a ticket's log directory, checks that the expected log artifacts
(transitions.log, booley_state.json, progress.json) exist, that the developer
state file passes its gate checks, and that no expected steps were skipped; then
renders the result as a human-readable Markdown report.

Why: this "validate run artifacts" responsibility is distinct from the ticket
field/criteria/git-state validation that stays in ``validation.py`` (SRP,
principle 8). Split out so each module owns a single reason to change.

Consumers: ``_ticket_ops.py`` and ``cli.py`` (``_cmd_validate_logs``) import
``validate_logs`` + ``format_validate_logs_report`` -- historically from the
``validation`` module, which now re-exports them from here for backward compat.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .constants import STEP_ORDER
from .paths import existing_human_log_file, existing_runtime_file


def _find_skipped_steps(steps_completed, expected_steps):
    """Find steps that should have run but are missing from steps_completed."""
    if not steps_completed:
        return []
    completed_set = set(steps_completed)
    last_completed_idx = max(
        (expected_steps.index(s) for s in steps_completed if s in expected_steps),
        default=-1,
    )
    return [
        {"step": stage, "reason": "expected by step order but not in steps_completed"}
        for i, stage in enumerate(expected_steps)
        if i <= last_completed_idx and stage not in completed_set
    ]


def _validate_state_file(state_path: Path) -> list[dict[str, str]]:
    """Read booley_state.json and return developer gate failures (if any)."""
    gate_failures: list[dict[str, str]] = []
    import json

    try:
        state = json.loads(state_path.read_text(encoding="utf-8-sig"))
    except (json.JSONDecodeError, OSError):
        return [{"step": "developer", "message": "booley_state.json is unreadable"}]
    # Boundary: external JSON may decode to any type — a non-object is malformed.
    if not isinstance(state, dict):
        return [{"step": "developer", "message": "booley_state.json is not an object"}]

    if not state.get("slug"):
        gate_failures.append(
            {
                "step": "developer",
                "message": "booley_state.json has empty slug",
            }
        )
    criteria = state.get("criteria", {})
    # Dict check MUST precede the .items() iteration below.
    if not isinstance(criteria, dict):
        return gate_failures
    visible = {
        k: v for k, v in criteria.items() if isinstance(v, dict) and not str(k).startswith("_")
    }
    mandatory = [k for k, v in visible.items() if v.get("mandatory")]
    if visible and not mandatory:
        gate_failures.append(
            {
                "step": "developer",
                "message": "booley_state.json has visible criteria but none are mandatory",
            }
        )
    return gate_failures


def validate_logs(
    logs_dir: str | Path,
    slug: str,
    ticket_type: str,
    steps_completed: list[str],
    ticket_fields: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Validate log artifacts for completed steps.

    Returns dict with missing_files, missing_meta, skipped_steps,
    warnings, gate_failures, gate_warnings.
    """
    warnings = []
    if not existing_human_log_file(logs_dir, slug, "transitions.log").exists():
        warnings.append("transitions.log is missing")

    missing_files = []
    gate_failures = []
    state_path = existing_runtime_file(logs_dir, slug, "booley_state.json")
    progress_path = existing_runtime_file(logs_dir, slug, "progress.json")
    if not state_path.exists():
        missing_files.append({"step": "developer", "file": "booley_state.json"})
    else:
        gate_failures.extend(_validate_state_file(state_path))
    if not progress_path.exists():
        missing_files.append({"step": "runtime", "file": "progress.json"})

    legacy_steps = [
        step for step in steps_completed if step in STEP_ORDER and step not in {"setup", "summary"}
    ]

    return {
        "missing_files": missing_files,
        "missing_meta": [],
        "skipped_steps": (
            _find_skipped_steps(steps_completed, list(STEP_ORDER)) if legacy_steps else []
        ),
        "warnings": warnings,
        "gate_failures": gate_failures,
        "gate_warnings": [],
    }


def _fmt_validation_section(title, items, fmt_fn, lines):
    """Append a validation report section. Returns count of items added."""
    if not items:
        return 0
    lines.append(f"## {title}")
    for item in items:
        lines.append(fmt_fn(item))
    lines.append("")
    return len(items)


def format_validate_logs_report(result: dict[str, Any], slug: str) -> tuple[str, int]:
    """Format validate_logs result as a human-readable report.

    Returns (report_str, error_count) tuple.
    """
    lines = [f"# Log Validation -- {slug}", ""]
    errors = 0

    errors += _fmt_validation_section(
        "Missing Files",
        result["missing_files"],
        lambda i: f"- **{i['step']}**: `{i['file']}` not found",
        lines,
    )
    errors += _fmt_validation_section(
        "Missing Step Metadata",
        result["missing_meta"],
        lambda i: f"- **{i['step']}**: missing keys: {', '.join(f'`{k}`' for k in i['keys'])}",
        lines,
    )
    errors += _fmt_validation_section(
        "Skipped Steps",
        result["skipped_steps"],
        lambda i: f"- **{i['step']}**: {i['reason']}",
        lines,
    )
    errors += _fmt_validation_section(
        "Gate Failures (HARD)",
        result.get("gate_failures", []),
        lambda i: f"- **{i['step']}**: {i['message']}",
        lines,
    )

    # Gate warnings (soft) — don't count as errors
    _fmt_validation_section(
        "Gate Warnings (SOFT)",
        result.get("gate_warnings", []),
        lambda i: f"- **{i['step']}**: {i['message']}",
        lines,
    )

    errors += _fmt_validation_section("Warnings", result["warnings"], lambda w: f"- {w}", lines)

    if errors == 0:
        lines.append("All log artifacts present and complete.")
    else:
        lines.append(f"**{errors} issue(s) found.**")

    return "\n".join(lines), errors
