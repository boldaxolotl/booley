"""Auto-requeue tickets blocked by a known-transient Developer Agent crash.

A stream stall (or any other purely server-side hiccup) lands the ticket in
``blocked/`` with a generic reason — usually "Developer Agent exited with N
unmet criteria", because the crash happened mid-run and the criteria were
simply never finished. The crash detail only survives in ``run.log`` and the
run transcript, so triage has to read the whole log just to conclude "nobody
could have done anything about this."

Auto-retry short-circuits that: every developer crash or cancellation is
recorded to a per-ticket sidecar, and when it matches a known resumable
signature and the ticket still has retry budget, the ticket is requeued without
a triage pass.

This is safe because ``booley_state.json`` and the run's git commits both
survive the block -> queue -> run cycle. The next run sees the same criteria
state the crashed one left behind and continues from there — including any
genuine regression the crashed run had already surfaced. The crash and
whatever the run had found are orthogonal events; the developer's own
criteria loop handles the latter without a human-composed hint.

Guardrails:

* **Bounded.** ``[developer.auto_retry].max_attempts`` (default 1) per ticket, counted
  over the ticket's lifetime. A stall that recurs at the same spot is not
  random, and the second occurrence goes to a human.
* **Audited.** Every auto-retry writes an incident to ``incidents.md`` and a
  ``blocked -> queued`` transition with the ``auto-retry`` actor, so a ticket
  that keeps crashing is visible on the board without a triage session.
* **Conservative.** Only the signatures in
  :data:`booley.harness.blocking.TRANSIENT_CRASH_SIGNATURES` qualify. Every
  other block falls through to normal triage unchanged.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from booley.ticket_board.paths import ticket_runtime_dir

from . import ticket_cli
from .blocking import classify_transient_crash
from .models import TicketContext

logger = logging.getLogger(__name__)

DEFAULT_MAX_ATTEMPTS = 1

_ACTOR = "auto-retry"


def _crashes_path(logs_dir: Path) -> Path:
    """Sidecar recording every developer crash for this ticket."""
    return ticket_runtime_dir(logs_dir) / "developer" / "crashes.json"


def _load_crashes(logs_dir: Path) -> list[dict[str, Any]]:
    path = _crashes_path(logs_dir)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Unreadable crash sidecar %s: %s", path, exc)
        return []
    return data if isinstance(data, list) else []


def _save_crashes(logs_dir: Path, crashes: list[dict[str, Any]]) -> None:
    path = _crashes_path(logs_dir)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(crashes, indent=2), encoding="utf-8")
        tmp.replace(path)
    except OSError as exc:
        # Losing the sidecar costs an auto-retry, never a ticket. Stay quiet
        # and let the crash take its normal path to triage.
        logger.warning("Could not write crash sidecar %s: %s", path, exc)


def record_crash(logs_dir: Path, *, run_index: int, reason: str) -> None:
    """Record a developer-agent crash, classifying it as transient or not.

    Called from the crash handlers themselves — before recovery is attempted —
    so the reason survives even when the run goes on to be blocked for an
    unrelated verdict later.
    """
    from .logging_utils import now_iso

    incident_type = classify_transient_crash(reason)
    crashes = _load_crashes(logs_dir)
    crashes.append(
        {
            "run_index": run_index,
            "time": now_iso(),
            "reason": reason,
            "incident_type": incident_type,
            "auto_retried": False,
        }
    )
    _save_crashes(logs_dir, crashes)


def _config(project_root: Path) -> int:
    """Read the auto-retry budget; zero is the single spelling for disabled."""
    from booley.config.settings import _load_booley_toml

    try:
        developer = _load_booley_toml(project_root).get("developer", {})
    except Exception as exc:  # noqa: BLE001 — a bad toml must not block the ticket path
        logger.warning("Could not read [developer.auto_retry] config: %s", exc)
        return DEFAULT_MAX_ATTEMPTS
    section = developer.get("auto_retry", {}) if isinstance(developer, dict) else {}
    if not isinstance(section, dict):
        logger.warning("booley.toml [developer.auto_retry] is not a table; using defaults")
        return DEFAULT_MAX_ATTEMPTS
    if "enabled" in section:
        logger.warning(
            "[developer.auto_retry].enabled is retired; use max_attempts = 0 to disable"
        )

    max_attempts = section.get("max_attempts", DEFAULT_MAX_ATTEMPTS)
    if not isinstance(max_attempts, int) or isinstance(max_attempts, bool) or max_attempts < 0:
        logger.warning(
            "[developer.auto_retry].max_attempts must be a non-negative integer; using %d",
            DEFAULT_MAX_ATTEMPTS,
        )
        max_attempts = DEFAULT_MAX_ATTEMPTS
    return max_attempts


def _feedback(incident_type: str, run_index: int, reason: str) -> str:
    if incident_type == "developer_cancelled":
        return (
            f"Auto-requeued after the Developer Agent was cancelled in run {run_index}: "
            f"{reason}\n\n"
            "Cancellation interrupted the session; it is not a ticket or design failure. "
            "Criteria state and the commits from that run persist, so resume from the "
            "current state and finish any remaining bookkeeping, including "
            "`submit_run_report`."
        )
    return (
        f"Auto-requeued after a transient agent crash in run {run_index} "
        f"(`{incident_type}`): {reason}\n\n"
        "No human diagnosis was performed — this is a server-side failure, not "
        "a ticket or design problem. Criteria state and the commits from that "
        "run persist, so pick up from the current state and keep going. Any "
        "criterion still showing False is real and worth investigating."
    )


def maybe_auto_retry(ctx: TicketContext, project_root: Path, run_index: int) -> bool:
    """Requeue *ctx*'s ticket if this run died to a known transient crash.

    Returns True when the ticket was requeued. Never raises — every failure
    mode falls through to the ticket staying blocked for normal triage.
    """
    try:
        return _maybe_auto_retry(ctx, project_root, run_index)
    except Exception as exc:  # auto-retry is best-effort by design  # noqa: BLE001
        logger.warning("Auto-retry check failed for %s: %s", ctx.slug, exc, exc_info=True)
        return False


def _maybe_auto_retry(ctx: TicketContext, project_root: Path, run_index: int) -> bool:
    from . import terminal
    from .colors import yellow

    crashes = _load_crashes(ctx.logs_dir)
    this_run = [c for c in crashes if c.get("run_index") == run_index and c.get("incident_type")]
    if not this_run:
        return False
    crash = this_run[-1]
    incident_type = str(crash["incident_type"])

    max_attempts = _config(project_root)

    # Only a blocked ticket is ours to requeue. A crash the run recovered from
    # can still end in review/, and requeuing that would redo finished work.
    # Checked before the budget so a recovered run never prints a triage nag.
    if ticket_cli.ticket_status(project_root, ctx.slug) != "blocked":
        return False

    used = sum(1 for c in crashes if c.get("auto_retried"))
    if used >= max_attempts:
        logger.info(
            "Auto-retry budget exhausted for %s (%d/%d) — leaving blocked for triage",
            ctx.slug,
            used,
            max_attempts,
        )
        terminal.raw(
            f"  {yellow('[AUTO-RETRY]')} budget exhausted ({used}/{max_attempts}) — needs triage"
        )
        return False

    attempt = used + 1
    reason = str(crash.get("reason", ""))
    ticket_cli.log_incident(
        project_root,
        ctx.slug,
        incident_type=incident_type,
        step="developer",
        description=f"Run {run_index}: {reason}",
        resolution=f"auto-retried ({attempt}/{max_attempts})",
    )

    ok = ticket_cli.unblock(
        project_root,
        ctx.slug,
        feedback=_feedback(incident_type, run_index, reason),
        actor=_ACTOR,
        detail=f"transient crash ({incident_type}), auto-retry {attempt}/{max_attempts}",
        feedback_heading="Auto-Retry",
    )
    if not ok:
        logger.warning("Auto-retry requeue failed for %s — staying blocked", ctx.slug)
        return False

    crash["auto_retried"] = True
    _save_crashes(ctx.logs_dir, crashes)

    logger.warning(
        "Auto-retried %s after transient crash %s (attempt %d/%d)",
        ctx.slug,
        incident_type,
        attempt,
        max_attempts,
    )
    terminal.raw(
        f"  {yellow('[AUTO-RETRY]')} transient crash ({incident_type}) — "
        f"requeued {attempt}/{max_attempts}"
    )
    return True
