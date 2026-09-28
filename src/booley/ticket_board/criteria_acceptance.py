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
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from booley.core.boundary import as_str_list
from booley.criteria.freshness import (
    VerificationFreshness,
    evaluate_verification_freshness,
    verification_freshness_eligible,
)

# NOTE: DevelopmentState is imported function-locally (not here) because the
# test suite patches ``booley.criteria.state.DevelopmentState`` at
# its source module; a module-level binding here would defeat that patch.
from booley.evidence.fields import SOURCE_FINGERPRINT_DETAIL_KEY
from booley.flows.source_fingerprint import compute_source_fingerprint
from booley.runtime.timefmt import utc_now_rfc3339

logger = logging.getLogger(__name__)

__all__ = [
    "VerificationFreshness",
    "evaluate_verification_freshness",
]

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
    # Legacy criteria that declared a "fail -> pass" transition but were only
    # ever observed passing — strict Ticket states reject them before verdict.
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
    from booley.criteria.state import DevelopmentState

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
    _enforce_acceptance_evidence(state, work_dir=work_dir)
    stats = _compute_criteria_stats(state.criteria)
    verdict = _determine_disposition(state, stats)
    verdict.unverified_transitions = _find_unverified_transitions(state.criteria)
    note = verdict.unverified_transitions_note()
    if note:
        logger.warning("%s", note)
    return verdict


def _load_test_registry(
    work_dir: Path | None,
) -> tuple[dict[str, dict], str | None]:
    """Load normalized tests.toml data and report invalid external input."""
    if work_dir is None:
        return {}, None
    from booley.config.project_config import normalize_tests_toml
    from booley.runtime.project_dir import resolve_checkout_project_dir

    try:
        path = resolve_checkout_project_dir(work_dir) / "tests.toml"
        if not path.exists():
            return {}, None
        with path.open("rb") as stream:
            return normalize_tests_toml(tomllib.load(stream)), None
    except (FileNotFoundError, OSError, ValueError, tomllib.TOMLDecodeError) as exc:
        reason = f"tests.toml acceptance registry is invalid: {exc}"
        logger.warning("Could not load %s", reason, exc_info=True)
        return {}, reason


def _sim_evidence_sets(
    detail: dict,
) -> tuple[set[str], set[str], set[str], set[str]] | None:
    """Normalize selected/pass/fail/skip names from one simulation record."""
    selected_raw = detail.get("selected_tests")
    if not isinstance(selected_raw, list) or not all(
        isinstance(name, str) for name in selected_raw
    ):
        return None
    selected = set(selected_raw)
    passed_raw = detail.get("passed_tests")
    if isinstance(passed_raw, list) and all(isinstance(name, str) for name in passed_raw):
        passed = set(passed_raw)
    elif detail.get("tests_passed") == len(selected_raw):
        passed = set(selected)
    else:
        passed = set()
    failed = set(as_str_list(detail.get("failed_tests")))
    skipped = set(as_str_list(detail.get("skipped_tests")))
    return selected, passed, failed, skipped


def _sim_contract_requirements(
    key: str,
    entry,
    registry: dict[str, dict],
    selected: set[str],
) -> tuple[set[str], int | None]:
    """Resolve required names and minimum count from a basis-bound simulation criterion."""
    from booley.config.project_config import lookup_target_section
    from booley.criteria.actions import criterion_target
    from booley.criteria.simulation import resolve_simulation_criterion_contract

    params = entry.params or {}
    target = criterion_target(key, entry, "sim_pass", per_target=True)
    section = lookup_target_section(registry, target) if target else None
    registered = set(section.get("tests", [])) if isinstance(section, dict) else set()
    contract = resolve_simulation_criterion_contract(params, registered, selected)
    return set(contract.required_tests), contract.minimum_total


def _sim_evidence_error(key: str, entry, registry: dict[str, dict]) -> str | None:
    """Return why a passing simulation record does not satisfy its contract."""
    detail = entry.detail or {}
    params = entry.params or {}
    expected_subject = params.get("subject") or params.get("verification_subject") or "dut"
    actual_subject = detail.get("verification_subject")
    if actual_subject in {"dut", "model"} and actual_subject != expected_subject:
        return (
            f"verification subject is {actual_subject!r}, but this Criterion "
            f"requires {expected_subject!r} evidence"
        )
    evidence = _sim_evidence_sets(detail)
    if evidence is None:
        return "simulation evidence does not identify the selected tests"
    selected, passed, failed, skipped = evidence
    required, minimum_total = _sim_contract_requirements(key, entry, registry, selected)
    if minimum_total is None:
        return "simulation Criterion has an invalid minimum_total"
    errors: list[str] = []
    missing_selected = sorted(required - selected)
    if missing_selected:
        errors.append("required tests were not selected: " + ", ".join(missing_selected))
    missing_passed = sorted(required - passed)
    if missing_passed:
        errors.append("required tests did not pass: " + ", ".join(missing_passed))
    if len(selected) < minimum_total:
        errors.append(f"selected {len(selected)} tests, fewer than minimum_total={minimum_total}")
    if failed:
        errors.append("simulation evidence contains failed tests: " + ", ".join(sorted(failed)))
    required_skips = sorted(required & skipped)
    if required_skips:
        errors.append("required tests were skipped: " + ", ".join(required_skips))
    return errors[0] if errors else None


def _has_matching_failing_evidence(entry) -> bool:
    """Whether a fail -> pass criterion retained its own fingerprinted red run."""
    params = entry.params or {}
    required_raw = params.get("required_tests")
    if isinstance(required_raw, list) and all(isinstance(name, str) for name in required_raw):
        required = set(required_raw)
    else:
        selector = params.get("test_selector") or params.get("selector")
        required = (
            {selector} if isinstance(selector, str) and selector not in {"", "all"} else set()
        )

    for record in getattr(entry, "transition_evidence", []) or []:
        if not isinstance(record, dict) or record.get("met") is not False:
            continue
        detail = record.get("detail")
        if not isinstance(detail, dict):
            continue
        stamp = detail.get(SOURCE_FINGERPRINT_DETAIL_KEY)
        if not isinstance(stamp, dict):
            continue
        fingerprint = stamp.get("fingerprint")
        if not (
            (isinstance(fingerprint, dict) and fingerprint)
            or (isinstance(fingerprint, str) and fingerprint)
        ):
            continue
        failed_raw = detail.get("failed_tests")
        if not isinstance(failed_raw, list) or not all(
            isinstance(name, str) for name in failed_raw
        ):
            continue
        failed = set(failed_raw)
        if failed and (not required or failed & required):
            return True
    return False


def _enforce_acceptance_evidence(
    state, *, work_dir: Path | None, persist: bool = True
) -> list[str]:
    """Fail closed on evidence that cannot satisfy the recorded Ticket baseline."""
    if not getattr(state, "strict_criteria", False):
        return []
    registry, registry_error = _load_test_registry(work_dir)
    now = utc_now_rfc3339()
    rejected: list[str] = []
    for key, entry in state.criteria.items():
        if key.startswith("_") or not entry.met:
            continue
        reason: str | None = None
        if (entry.params or {}).get("from_state") == "fail" and not (
            _has_matching_failing_evidence(entry)
        ):
            reason = "basis-bound fail -> pass transition has no matching fingerprinted failing evidence"
        elif key.startswith("sim_pass"):
            reason = registry_error or _sim_evidence_error(key, entry, registry)
        if reason is None:
            continue
        entry.met = False
        entry.updated_at = now
        entry.detail = dict(entry.detail or {})
        entry.detail["acceptance_error"] = reason
        rejected.append(key)

    if rejected:
        _invalidate_submitted_report(state, now=now)
        if persist:
            state.save()
        logger.warning(
            "Rejected insufficient Criterion evidence for %s: %s",
            state.slug,
            ", ".join(rejected),
        )
    return rejected


def _find_unverified_transitions(criteria: dict) -> list[str]:
    """Legacy criteria that promised fail->pass but never saw the fail.

    Strict Ticket states are marked unmet by ``_enforce_acceptance_evidence``
    before this compatibility diagnostic runs. Older state files keep the
    historical warning so an upgrade does not silently change their outcome.
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


def _refresh_verification_entry(
    key: str,
    entry,
    *,
    work_dir: Path,
    fingerprints: dict[str | None, dict],
    now: str,
) -> bool:
    """Refresh one passing criterion against its receipt and source evidence."""
    result = evaluate_verification_freshness(
        key,
        entry,
        work_dir=work_dir,
        fingerprint_provider=compute_source_fingerprint,
        fingerprints=fingerprints,
    )
    assert result is not None
    if not result.stale:
        return False
    _mark_verification_stale(
        entry,
        now=now,
        reason=result.reason,
        changed_categories=result.changed_categories,
        current=result.current_source_fingerprint,
    )
    if result.review_dimensions:
        entry.detail["stale_review_dimensions"] = list(result.review_dimensions)
    return True


def refresh_verification_freshness(
    state, *, work_dir: Path | None, persist: bool = True
) -> list[str]:
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
        if not verification_freshness_eligible(
            key,
            entry,
            include_unobserved_review=True,
        ):
            continue
        if _refresh_verification_entry(
            key,
            entry,
            work_dir=resolved_work_dir,
            fingerprints=fingerprints,
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
        if persist:
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


def _mark_verification_stale(
    entry,
    *,
    now: str,
    reason: str,
    changed_categories: set[str] | list[str] | tuple[str, ...],
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


def _active_declared_block_reason(state, stats: dict) -> str | None:
    """Return the active Developer-declared reason for this projection."""
    all_mandatory_met = stats["mandatory_met"] == stats["mandatory"]
    blocked_entry = state.criteria.get("_blocked_reason")
    if blocked_entry and blocked_entry.met and not all_mandatory_met:
        return blocked_entry.detail.get("reason", "blocked by agent")
    return None


def project_active_declared_block_reason(state_path: Path, *, work_dir: Path | None) -> str | None:
    """Project current acceptance state without persisting invalidations."""
    from booley.criteria.state import DevelopmentState

    state = DevelopmentState.load(state_path)
    if not state.criteria:
        return None
    refresh_verification_freshness(state, work_dir=work_dir, persist=False)
    _enforce_acceptance_evidence(state, work_dir=work_dir, persist=False)
    return _active_declared_block_reason(state, _compute_criteria_stats(state.criteria))


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
    reason = _active_declared_block_reason(state, stats)
    if reason is not None:
        logger.info("Ticket %s blocked: %s", state.slug, reason)
        return CriteriaVerdict(disposition="blocked", blocked_reason=reason, **base)

    if stats["mandatory"] == 0 and not getattr(state, "authorized_zero_mandatory_basis_id", ""):
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
    from booley.evidence.review_dispositions import review_report_required

    unmet_optional = sorted(
        key
        for key, entry in state.criteria.items()
        if not key.startswith("_") and not entry.mandatory and not entry.met
    )
    if (
        not is_run_report_enabled()
        and not unmet_optional
        and not review_report_required(state.criteria)
    ):
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
    "project_active_declared_block_reason",
    "refresh_verification_freshness",
]
