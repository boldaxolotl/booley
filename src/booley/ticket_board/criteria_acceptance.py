"""Criteria-based acceptance — developer path.

Reads ``booley_state.json`` after the developer agent exits and determines
ticket disposition:
  - all mandatory met → review
  - any mandatory unmet → failed (with unmet list)
  - blocked_reason in state → blocked

Note: "failed" tickets land in blocked/ for human triage — the harness
never archives (deletes) tickets. Archive is a human-only operation
(see booley.ticket_board.archive.op_archive, invoked by ticket-triage).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from booley.core.boundary import as_str_list

# NOTE: DevelopmentState is imported function-locally (not here) because the
# test suite patches ``booley.dev_support.development_state.DevelopmentState`` at
# its source module; a module-level binding here would defeat that patch.
from booley.dev_support.development_state import (
    SOURCE_FINGERPRINT_DETAIL_KEY,
    compute_source_fingerprint,
)
from booley.runtime.timefmt import utc_now_rfc3339

logger = logging.getLogger(__name__)

_REPORT_CRITERION = "_report_submitted"
_JUSTIFIED_OPTIONAL_DETAIL = "unmet_optional_criteria"


@dataclass
class CriteriaVerdict:
    """Result of criteria-based acceptance check."""

    disposition: str  # "review" | "failed" | "blocked"
    total: int = 0
    met: int = 0
    mandatory: int = 0
    mandatory_met: int = 0
    unmet_mandatory: list[str] = field(default_factory=list)
    blocked_reason: str = ""
    # Criteria that declared a "fail -> pass" transition but were only ever
    # observed passing — see unverified_transitions_note() (F-53).
    unverified_transitions: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return self.disposition == "review"

    def unverified_transitions_note(self) -> str:
        """One-line warning for transitions that degraded to a plain pass.

        Empty when there is nothing to say, so callers can `if note:` it.
        """
        if not self.unverified_transitions:
            return ""
        names = ", ".join(sorted(self.unverified_transitions))
        return (
            f"UNVERIFIED TRANSITION: {names} declared 'fail -> pass' but no "
            "failing run was ever recorded — the criterion was satisfied by a "
            "pass alone, so it proves the test passes, not that the fix "
            "changed anything."
        )


def check_criteria_acceptance(
    state_path: Path,
    *,
    work_dir: Path | None = None,
) -> CriteriaVerdict:
    """Read state file and determine ticket disposition.

    Args:
        state_path: Path to ``booley_state.json``.

    Returns:
        CriteriaVerdict with disposition and summary stats.
    """
    from booley.dev_support.development_state import DevelopmentState

    if not state_path.exists():
        logger.warning("State file not found: %s", state_path)
        return CriteriaVerdict(
            disposition="failed",
            blocked_reason="booley_state.json not found — developer may have crashed",
        )

    state = DevelopmentState.load(state_path)
    if not state.criteria:
        logger.warning("State file has no criteria: %s", state_path)
        return CriteriaVerdict(
            disposition="failed",
            blocked_reason="booley_state.json contains no criteria",
        )

    refresh_verification_freshness(state, work_dir=work_dir)
    stats = _compute_criteria_stats(state.criteria)
    verdict = _determine_disposition(state, stats)
    verdict.unverified_transitions = _find_unverified_transitions(state.criteria)
    note = verdict.unverified_transitions_note()
    if note:
        logger.warning("%s", note)
    return verdict


def _find_unverified_transitions(criteria: dict) -> list[str]:
    """Criteria that promised a fail->pass transition but never saw the fail.

    Design note (F-53): the transition is *reported*, not *enforced*. Enforcing
    it would mean refusing to accept a ticket whose test was green on the first
    run, which an unattended runner cannot recover from — the agent would have
    to un-fix the bug to prove it existed. The ticket-authoring validator has
    always treated the `fail` leg as advisory (a warning, never a hard error),
    so hard-failing at acceptance time would also contradict the contract the
    ticket was written against. What was wrong before was not the leniency but
    the silence: the degraded contract left no trace anywhere.
    """
    unverified = []
    for key, entry in criteria.items():
        if key.startswith("_") or not entry.met:
            continue
        if (entry.params or {}).get("from_state") != "fail":
            continue
        if not entry.ever_failed:
            unverified.append(key)
    return unverified


def refresh_verification_freshness(state, *, work_dir: Path | None) -> list[str]:
    """Persistently invalidate passing checks whose source fingerprint drifted."""
    resolved_work_dir = work_dir
    if resolved_work_dir is None and getattr(state, "work_dir", ""):
        resolved_work_dir = Path(state.work_dir)
    if resolved_work_dir is None:
        return []

    now = utc_now_rfc3339()
    stale_keys: list[str] = []
    fingerprints: dict[str | None, dict] = {}
    for key, entry in state.criteria.items():
        if key.startswith("_") or not entry.mandatory or not entry.met:
            continue
        categories = _verification_fingerprint_categories(key)
        if not categories:
            continue
        stamp = (entry.detail or {}).get(SOURCE_FINGERPRINT_DETAIL_KEY)
        target = stamp.get("target") if isinstance(stamp, dict) else None
        target = target if isinstance(target, str) and target else None
        try:
            if target not in fingerprints:
                fingerprints[target] = compute_source_fingerprint(
                    resolved_work_dir,
                    target=target,
                )
            current = fingerprints[target]
        except OSError:
            logger.debug("Could not compute final source fingerprint", exc_info=True)
            continue
        if _stale_verification_entry(
            entry,
            categories=categories,
            current=current,
            now=now,
        ):
            stale_keys.append(key)

    if stale_keys:
        _invalidate_submitted_report(state, now=now)
        logger.warning(
            "Marked stale verification criteria unmet for %s: %s",
            state.slug,
            ", ".join(stale_keys),
        )
        state.save()
    return stale_keys


def _invalidate_submitted_report(state, *, now: str) -> None:
    """Require a new report after newly discovered stale verification evidence."""
    report_entry = state.criteria.get(_REPORT_CRITERION)
    if report_entry is None or not report_entry.met or report_entry.locked:
        return
    report_entry.met = False
    report_entry.stale = True
    report_entry.updated_at = now
    report_entry.detail = dict(report_entry.detail or {})
    report_entry.detail["stale_reason"] = (
        "Verification evidence became stale after this report was submitted."
    )


def _stale_verification_entry(
    entry,
    *,
    categories: set[str],
    current: dict,
    now: str,
) -> bool:
    """Mark a passing verification entry stale if its source fingerprint drifted.

    Returns ``True`` when the entry was marked stale, ``False`` otherwise.
    """
    stamp = (entry.detail or {}).get(SOURCE_FINGERPRINT_DETAIL_KEY)
    if not isinstance(stamp, dict):
        _mark_verification_stale(
            entry,
            now=now,
            reason=(
                "Passing verification criterion has no source fingerprint; "
                "re-run the relevant Flow or Specialist."
            ),
            changed_categories=categories,
            current=current,
        )
        return True
    previous = stamp.get("fingerprint", {})
    stamped_categories = stamp.get("categories", [])
    if not isinstance(previous, dict) or not isinstance(stamped_categories, list):
        _mark_verification_stale(
            entry,
            now=now,
            reason=(
                "Passing verification criterion has an invalid source "
                "fingerprint; re-run the relevant Flow or Specialist."
            ),
            changed_categories=categories,
            current=current,
        )
        return True

    changed_categories: list[str] = []
    for category in stamped_categories:
        old_digest = (previous.get(category, {}) or {}).get("digest")
        new_digest = (current.get(category, {}) or {}).get("digest")
        if old_digest != new_digest:
            changed_categories.append(str(category))
    if not changed_categories:
        return False

    _mark_verification_stale(
        entry,
        now=now,
        reason=(
            "RTL/testbench sources changed after the last passing "
            "verification check; re-run the relevant Flow or Specialist."
        ),
        changed_categories=changed_categories,
        current=current,
    )
    return True


def _verification_fingerprint_categories(key: str) -> set[str]:
    """Return source categories a verification criterion must fingerprint."""
    if key.startswith(("mutation_score_", "coverage_")):
        return {"rtl", "tb", "campaign"}
    if key.startswith(("sim_", "elab_")):
        return {"rtl", "tb"}
    if key.startswith(("lint_", "synthesis_", "fpga_impl_")):
        return {"rtl"}
    return set()


def _mark_verification_stale(
    entry,
    *,
    now: str,
    reason: str,
    changed_categories: set[str] | list[str],
    current: dict,
) -> None:
    """Mark a passing verification entry unmet with a stale diagnostic."""
    entry.met = False
    entry.stale = True
    entry.updated_at = now
    entry.detail = dict(entry.detail or {})
    entry.detail["stale_reason"] = reason
    entry.detail["stale_source_categories"] = sorted(changed_categories)
    entry.detail["current_source_fingerprint"] = current


def _compute_criteria_stats(
    criteria: dict,
) -> dict:
    """Compute criteria stats excluding internal entries (_-prefixed)."""
    real = {k: e for k, e in criteria.items() if not k.startswith("_")}
    total = len(real)
    met = sum(1 for e in real.values() if e.met)
    mandatory = sum(1 for e in real.values() if e.mandatory)
    mandatory_met = sum(1 for e in real.values() if e.mandatory and e.met)
    unmet = [k for k, e in real.items() if e.mandatory and not e.met]
    return {
        "total": total,
        "met": met,
        "mandatory": mandatory,
        "mandatory_met": mandatory_met,
        "unmet": unmet,
    }


def _determine_disposition(state, stats: dict) -> CriteriaVerdict:
    """Decide ticket disposition from criteria stats and blocked reason."""
    all_mandatory_met = stats["mandatory_met"] == stats["mandatory"]
    base = {
        "total": stats["total"],
        "met": stats["met"],
        "mandatory": stats["mandatory"],
        "mandatory_met": stats["mandatory_met"],
    }

    # Check _blocked_reason — only when mandatory criteria are NOT all met.
    blocked_entry = state.criteria.get("_blocked_reason")
    if blocked_entry and blocked_entry.met and not all_mandatory_met:
        reason = blocked_entry.detail.get("reason", "blocked by agent")
        logger.info("Ticket %s blocked: %s", state.slug, reason)
        return CriteriaVerdict(disposition="blocked", blocked_reason=reason, **base)

    if stats["mandatory"] == 0:
        logger.warning(
            "State for %s has no visible mandatory criteria -- failing instead "
            "of treating 0/0 as success.",
            _state_slug_for_log(state),
        )
        return CriteriaVerdict(
            disposition="failed",
            unmet_mandatory=["_mandatory_criteria_missing"],
            **base,
        )

    if all_mandatory_met:
        report_error = _run_report_gate_error(state)
        if report_error:
            logger.warning(
                "All visible mandatory criteria met for %s, but %s -- "
                "failing so the developer resubmits the report on the next run.",
                state.slug,
                report_error,
            )
            return CriteriaVerdict(
                disposition="failed",
                unmet_mandatory=[_REPORT_CRITERION],
                **base,
            )
        logger.info("All %d mandatory criteria met for %s", stats["mandatory"], state.slug)
        return CriteriaVerdict(disposition="review", **base)

    logger.warning(
        "%d/%d mandatory criteria unmet for %s: %s",
        len(stats["unmet"]),
        stats["mandatory"],
        state.slug,
        ", ".join(stats["unmet"]),
    )
    return CriteriaVerdict(
        disposition="failed",
        unmet_mandatory=stats["unmet"],
        **base,
    )


def _run_report_gate_error(state) -> str | None:
    """Return why final report evidence is insufficient, or ``None``."""
    from booley.config.project_config import is_run_report_enabled

    unmet_optional = sorted(
        key
        for key, entry in state.criteria.items()
        if not key.startswith("_") and not entry.mandatory and not entry.met
    )
    if not is_run_report_enabled() and not unmet_optional:
        return None

    report_entry = state.criteria.get(_REPORT_CRITERION)
    if report_entry is None or not report_entry.met:
        return "run report was not submitted"

    justified = set(as_str_list(report_entry.detail.get(_JUSTIFIED_OPTIONAL_DETAIL), default=[]))
    unjustified = [key for key in unmet_optional if key not in justified]
    if unjustified:
        return "run report does not justify optional criteria: " + ", ".join(unjustified)
    return None


def _state_slug_for_log(state) -> str:
    """Return a stable slug-ish label for diagnostics when state.slug is empty."""
    if getattr(state, "slug", ""):
        return state.slug
    path = getattr(state, "_file_path", None)
    if path is None:
        return "<unknown>"
    try:
        return path.parent.parent.name if path.parent.name == ".runtime" else path.parent.name
    except Exception:  # noqa: BLE001 — diagnostic label only; any path oddity degrades to a placeholder
        return "<unknown>"


# Terminal display (format_criteria_verdict, build_criteria_summary_lines, and
# their formatting helpers) lives in criteria_summary_format.py -- rendering a
# summary is a different reason to change than deciding disposition. Re-import
# here purely so existing `from criteria_acceptance import ...` call sites keep
# working unchanged. Safe (no import cycle): criteria_summary_format only
# reaches back into this module via a function-local import.
from .criteria_summary_format import (
    build_criteria_summary_lines,
    format_criteria_verdict,
)

__all__ = [
    "CriteriaVerdict",
    "build_criteria_summary_lines",
    "check_criteria_acceptance",
    "format_criteria_verdict",
    "refresh_verification_freshness",
]
