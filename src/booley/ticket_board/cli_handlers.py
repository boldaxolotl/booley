"""Command handler functions for the ticket board CLI.

Each handler has signature: _cmd_<name>(tio: TicketIO, args) -> int
"""

from __future__ import annotations

import contextlib
import json
import sys
from pathlib import Path

from booley.core.boundary import BoundaryError, require_dict
from booley.core.models import OnSuccess
from booley.runtime.project_dir import resolve_project_dir
from booley.runtime.timefmt import parse_timestamp

from .analytics import (
    attribute_tokens_to_steps,
    collect_all_messages,
    collect_step_transcript_usage,
    collect_step_usage,
    compute_cost_detailed,
    compute_step_cost,
    compute_step_durations,
    parse_transitions_log,
    usage_entries_to_steps,
)
from .archive import op_archive
from .constants import (
    VALID_TYPES,
    normalize_dir,
)
from .evidence import op_collect_evidence
from .execution import (
    classify_tickets,
    next_from_planned,
    resume_detect,
    select_mutation_config,
)
from .frontmatter import parse_frontmatter
from .helpers import detect_project_root, generate_slug
from .io import TicketFileSpec, scan_all_tickets
from .lifecycle import SETTLED_STATUSES
from .operations import (
    op_activate,
    op_approve,
    op_block,
    op_complete,
    op_fail,
    op_handoff,
    op_promote_waiting,
    op_requeue,
    op_reset,
    op_unblock,
)
from .paths import (
    existing_human_log_file,
    existing_runtime_file,
    ticket_log_dir,
    ticket_runtime_dir,
)
from .reporting import (
    display_board,
    format_timing_report,
    format_usage_report,
)
from .scanner import _load_state_data
from .validation import (
    format_validate_logs_report,
    owned_draft_dirty_paths,
    validate_logs,
    validate_ticket_fields,
)

# ---------------------------------------------------------------------------
# Pure output commands (no side effects)
# ---------------------------------------------------------------------------


def _cmd_board(tio, args):
    tickets = scan_all_tickets(tio.tickets_dir)
    display_board(tickets, tickets_dir=Path(tio.tickets_dir))
    return 0


def _cmd_slug(tio, args):
    print(generate_slug(args.summary))
    return 0


def _cmd_read_board(tio, args):
    tickets = scan_all_tickets(tio.tickets_dir)
    json.dump({"tickets": tickets}, sys.stdout, indent=2, ensure_ascii=False)
    print()
    return 0


def _cmd_show(tio, args):
    """Show one ticket's paths, branch, and criteria split -- or the board.

    Triage otherwise has to reconstruct the ticket file, log dir, worktree,
    and branch by guessing directory conventions and ``find``-ing across three
    trees. With no slug this stays a plain alias for ``board``.
    """
    slug = getattr(args, "slug", None)
    if not slug:
        return _cmd_board(tio, args)

    entry = tio.find_ticket(slug)
    if entry is None:
        print(f"Error: ticket '{slug}' not found", file=sys.stderr)
        return 2

    # find_ticket accepts a copied-from-board ``<slug>.md``; re-derive the
    # canonical slug from the resolved file so log/worktree paths stay correct.
    slug = Path(entry["file"]).stem
    ticket_file = Path(tio.tickets_dir) / entry["file"]
    logs_dir = ticket_log_dir(tio.logs_dir, slug)
    worktree_root = (
        resolve_project_dir(tio._project_root)
        if entry.get("target_contract") is not None
        else tio._project_root / ".booley_project"
    )
    worktree = worktree_root / "worktrees" / slug
    criteria = entry.get("criteria") or {}
    mandatory = criteria.get("mandatory") or {}
    optional = criteria.get("optional") or {}

    # Cross-reference live met-status from booley_state.json so the counts match
    # the board rather than the (possibly stale) ticket frontmatter.
    state_data = _load_state_data(existing_runtime_file(tio.logs_dir, slug, "booley_state.json"))
    state_crit = (state_data or {}).get("criteria", {}) if isinstance(state_data, dict) else {}

    def _met(key: str) -> bool:
        entry = state_crit.get(key)
        return bool(entry.get("met")) if isinstance(entry, dict) else False

    mand_met = sum(1 for k in mandatory if _met(k))
    opt_met = sum(1 for k in optional if _met(k))

    wt_note = "" if worktree.is_dir() else "  (absent)"
    print(f"ticket:    {slug}")
    print(f"status:    {entry.get('status', '')}")
    if entry.get("acceptance_state"):
        print(f"acceptance: {entry['acceptance_state']}")
    print(f"file:      {ticket_file}")
    print(f"logs:      {logs_dir}")
    print(f"worktree:  {worktree}{wt_note}")
    print(f"branch:    {entry.get('branch', '') or '(none)'}")
    print(f"feature:   {entry.get('feature_branch', slug)}")
    print(
        f"criteria:  mandatory {mand_met}/{len(mandatory)} met, "
        f"optional {opt_met}/{len(optional)} met"
    )
    return 0


def _cmd_parse_ticket(tio, args):
    path = Path(args.path)
    if not path.exists():
        print(json.dumps({"error": f"File not found: {args.path}"}))
        return 2
    with path.open(encoding="utf-8") as f:
        text = f.read()
    fields, body = parse_frontmatter(text)
    # Merge runtime fields from progress.json (backward compat: falls back to frontmatter)
    from .logs import load_progress

    progress = load_progress(tio.logs_dir, path.stem)
    if progress is not None:
        fields.update(progress)
    json.dump({"fields": fields, "body": body}, sys.stdout, indent=2, ensure_ascii=False)
    print()
    return 0


def _cmd_validate_ticket(tio, args):
    path = Path(args.path)
    if not path.exists():
        print(json.dumps({"errors": [f"File not found: {args.path}"]}))
        return 1
    with path.open(encoding="utf-8") as f:
        text = f.read()
    fields, body = parse_frontmatter(text)
    results = validate_ticket_fields(
        fields,
        body,
        check_files=True,
        check_git=args.check_git,
        project_root=str(detect_project_root()),
        allowed_dirty_paths=owned_draft_dirty_paths(path, tio.tickets_dir),
    )
    warnings = [e for e in results if e.startswith("[warning] ")]
    errors = [e for e in results if not e.startswith("[warning] ")]
    for w in warnings:
        print(f"Warning: {w}", file=sys.stderr)
    if errors:
        print(json.dumps({"errors": errors}, indent=2))
        return 1
    print(json.dumps({"errors": [], "warnings": warnings, "valid": True}))
    return 0


def _cmd_next_step(tio, args):
    return _cmd_next_step_or_steps(tio, args, "next-step")


def _cmd_steps(tio, args):
    return _cmd_next_step_or_steps(tio, args, "stages")


def _cmd_next_step_or_steps(tio, args, command):
    """Shared logic for next-step and steps commands."""
    # Resolve type_or_slug: accept either a ticket type or a slug
    ticket_type = args.type_or_slug
    if ticket_type not in VALID_TYPES:
        entry = tio.find_ticket(ticket_type)
        if entry:
            ticket_type = entry.get("type", "feature")
            if ticket_type not in VALID_TYPES:
                ticket_type = "feature"
        else:
            print(
                f"Error: '{args.type_or_slug}' is not a valid ticket type "
                f"({', '.join(sorted(VALID_TYPES))}) and no ticket with "
                f"that slug was found.",
                file=sys.stderr,
            )
            return 1

    from .constants import STEP_ORDER

    extra = [s.strip() for s in args.skip.split(",") if s.strip()] if args.skip else None
    skip_set = set(extra) if extra else set()
    steps = [s for s in STEP_ORDER if s not in skip_set]

    if command == "next-step":
        result = next_from_planned(steps, args.current)
        print(result if result else "done")
    else:
        json.dump(steps, sys.stdout, indent=2)
        print()
    return 0


def _cmd_classify(tio, args):
    tickets = scan_all_tickets(tio.tickets_dir)
    result = classify_tickets(tickets, logs_dir=tio.logs_dir)
    if args.format == "counts":
        for key in ("executable", "active", "blocked", "waiting", "review", "orphaned"):
            print(f"{key}={len(result.get(key, []))}")
    else:
        json.dump(result, sys.stdout, indent=2, ensure_ascii=False)
        print()
    return 0


def _cmd_detect_orphans(tio, args):
    tickets = scan_all_tickets(tio.tickets_dir)
    result = classify_tickets(tickets, orphan_threshold_min=args.threshold, logs_dir=tio.logs_dir)
    orphaned = result.get("orphaned", [])
    if not orphaned:
        print("No orphaned tickets found.")
        return 0
    exit_code = 1
    for t in orphaned:
        slug = Path(t.get("file", "unknown.md")).stem
        age = t.get("_orphan_age_min", -1)
        step = t.get("step", "unknown")
        age_str = f"{age}m" if age > 0 else "unknown age"
        print(f"ORPHAN: {slug} (step: {step}, idle: {age_str})")
        if args.force_fail:
            ok = op_fail(tio, slug, "orphaned: agent died without cleanup", step)
            if ok:
                print("  → moved to blocked")
            else:
                print("  → ERROR: could not move to blocked", file=sys.stderr)
    return exit_code


def _cmd_mutation_config(tio, args):
    result = select_mutation_config(args.targets)
    if result:
        print(result)
        return 0
    print("Error: no targets provided", file=sys.stderr)
    return 1


def _cmd_resume(tio, args):
    entry = tio.find_ticket(args.slug)
    if not entry:
        print(json.dumps({"error": f"Ticket '{args.slug}' not found"}))
        return 1
    result = resume_detect(entry)
    json.dump(result, sys.stdout, indent=2)
    print()
    return 0


# ---------------------------------------------------------------------------
# Side-effect commands
# ---------------------------------------------------------------------------


def _parse_set_args(args_set):
    """Parse --set K=V arguments into a dict. Returns (updates, error_code) or (updates, None)."""
    updates = {}
    if not args_set:
        return updates, None
    for kv in args_set:
        if "=" not in kv:
            print(f"Error: invalid --set format '{kv}', expected K=V", file=sys.stderr)
            return {}, 1
        k, v = kv.split("=", 1)
        updates[k] = v if v else None
    return updates, None


def _cmd_update_board(tio, args):
    """Update ticket fields and log transitions."""
    from .io import find_ticket_file

    resolved_path, _ = find_ticket_file(tio.tickets_dir, args.slug)
    if resolved_path is None:
        print(f"Error: ticket '{args.slug}' not found", file=sys.stderr)
        return 2
    canonical_slug = resolved_path.stem

    old_entry = tio.find_ticket(canonical_slug) if args.log else None
    old_status = old_entry.get("status", "running") if old_entry else "running"
    old_step = old_entry.get("step", "") if old_entry else ""

    updates, err = _parse_set_args(args.set)
    if err is not None:
        return err

    # Guard: only op_approve can move review -> done
    if old_status == "review" and updates.get("status", old_status) == "done":
        print(
            "Error: cannot move ticket from review to done via update-board. "
            "Use 'approve' command instead.",
            file=sys.stderr,
        )
        return 1

    with tio._ticket_lock(canonical_slug):
        return _apply_board_update(tio, canonical_slug, args, updates, old_status, old_step)


def _apply_board_update(tio, slug, args, updates, old_status, old_step):
    """Apply field updates and log transition (caller holds lock)."""
    from .io import find_ticket_file
    from .logs import save_progress

    file_path, _ = find_ticket_file(tio.tickets_dir, slug)
    if file_path is None:
        print(f"Error: ticket '{slug}' not found", file=sys.stderr)
        return 2

    progress = tio._load_or_bootstrap_progress(slug, file_path)
    if args.reset_steps:
        progress["steps_completed"] = []
    if args.reset_steps_from:
        stages = progress.get("steps_completed", [])
        if args.reset_steps_from in stages:
            idx = stages.index(args.reset_steps_from)
            progress["steps_completed"] = stages[: idx + 1]

    spec_updates = tio._apply_updates(progress, updates, args.append_step)
    save_progress(tio.logs_dir, slug, progress)
    tio._write_spec_fields(file_path, spec_updates)

    if args.log:
        new_status = updates.get("status", old_status)
        new_step = updates.get("step", args.append_step or old_step)
        tio._append_transition_unlocked(
            slug,
            f"{old_status}:{old_step}",
            f"{new_status}:{new_step}",
            "ticket-execute",
            "step complete",
        )

    return 0


def _cmd_log_transition(tio, args):
    tio.append_transition(args.slug, args.from_state, args.to_state, args.actor, args.detail)
    return 0


def _cmd_move_ticket(tio, args):
    # Review exits go through operations that preserve their distinct semantics.
    entry = tio.find_ticket(args.slug)
    cur_status = entry.get("status", "") if entry else ""
    norm_to = normalize_dir(args.to)
    if cur_status == "review" and norm_to == "board/done":
        print(
            "Error: cannot move ticket from review to done via move-ticket. "
            "Use 'approve' command instead.",
            file=sys.stderr,
        )
        return 1
    if cur_status == "review" and norm_to == "board/queue":
        print(
            "Error: cannot move ticket from review to queue via move-ticket. "
            "Use 'reset' for a clean run.",
            file=sys.stderr,
        )
        return 1
    success = tio.move_ticket_file(args.slug, norm_to)
    return 0 if success else 2


def _cmd_activate(tio, args):
    ok = op_activate(tio, args.slug)
    return 0 if ok else 2


def _cmd_block(tio, args):
    ok = op_block(tio, args.slug, args.reason, args.step)
    return 0 if ok else 2


def _cmd_fail(tio, args):
    ok = op_fail(tio, args.slug, args.error, args.step)
    return 0 if ok else 2


def _cmd_requeue(tio, args):
    ok = op_requeue(tio, args.slug, args.reason)
    return 0 if ok else 2


def _cmd_handoff(tio, args):
    ok = op_handoff(tio, args.slug)
    return 0 if ok else 2


def _cmd_unblock(tio, args):
    ok = op_unblock(tio, args.slug, feedback=getattr(args, "feedback", ""))
    return 0 if ok else 2


def _cmd_reset(tio, args):
    ok = op_reset(
        tio,
        args.slug,
        force=getattr(args, "force", False),
        reason=getattr(args, "reason", "user reset ticket"),
    )
    return 0 if ok else 2


def _cmd_reset_to_deprecated(tio, args):
    print("ERROR: reset-to has been removed. Use 'reset' for a full reset.", file=sys.stderr)
    return 1


def _cmd_approve(tio, args):
    ok = op_approve(tio, args.slug, actor=args.actor, detail=args.detail)
    if ok:
        # Check if any waiting tickets are now executable
        promoted = op_promote_waiting(tio)
        if promoted:
            print(f"\nNewly executable tickets ({len(promoted)}):")
            for p in promoted:
                print(f"  - {p['summary']} ({p['slug']})")
    return 0 if ok else 2


def _cmd_complete(tio, args):
    ok = op_complete(tio, args.slug)
    return 0 if ok else 1


def _cmd_promote_waiting(tio, args):
    promoted = op_promote_waiting(tio)
    if promoted:
        print(f"Newly executable tickets ({len(promoted)}):")
        for p in promoted:
            print(f"  - {p['summary']} ({p['slug']})")
    else:
        print("No waiting tickets are newly executable.")
    return 0


def _cmd_init(tio, args):
    result = tio.init_ticket(args.ticket_path)
    if result:
        json.dump(result, sys.stdout, indent=2)
        print()
        return 0
    return 2


_ON_SUCCESS_REQUIRED_KEYS = frozenset({"destination", "merge", "cleanup", "triage_report"})
_ON_SUCCESS_KEYS = _ON_SUCCESS_REQUIRED_KEYS | {"remove_targets"}


def _parse_on_success_arg(value: str) -> tuple[dict[str, object] | None, str | None]:
    """Parse a complete successful-run disposition from the CLI boundary."""
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        return None, f"invalid JSON: {exc}"

    try:
        mapping = require_dict(parsed, field="--on-success")
    except BoundaryError as exc:
        return None, str(exc)

    missing = _ON_SUCCESS_REQUIRED_KEYS - mapping.keys()
    unknown = mapping.keys() - _ON_SUCCESS_KEYS
    key_errors = []
    if missing:
        key_errors.append(f"missing keys: {', '.join(sorted(missing))}")
    if unknown:
        key_errors.append(f"unknown keys: {', '.join(sorted(unknown))}")
    if key_errors:
        return None, "; ".join(key_errors)

    mapping.setdefault("remove_targets", [])
    model = OnSuccess.from_dict(mapping)
    errors = model.validate()
    if errors:
        return None, "; ".join(errors)
    return {
        "destination": model.destination,
        "merge": model.merge,
        "cleanup": model.cleanup,
        "triage_report": model.triage_report,
        "remove_targets": list(model.remove_targets),
    }, None


def _cmd_create_file(tio, args):
    # Parse criteria: --criteria-file takes precedence over --criteria
    criteria = None
    if args.criteria_file:
        try:
            criteria = json.loads(Path(args.criteria_file).read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            print(f"Error: invalid --criteria-file: {e}", file=sys.stderr)
            return 2
    elif args.criteria:
        try:
            criteria = json.loads(args.criteria)
        except json.JSONDecodeError as e:
            print(f"Error: invalid --criteria JSON: {e}", file=sys.stderr)
            return 2

    on_success = None
    if args.on_success is not None:
        on_success, error = _parse_on_success_arg(args.on_success)
        if error:
            print(f"Error: invalid --on-success: {error}", file=sys.stderr)
            return 2

    # Read body from file if --body-file given
    body = args.body
    if args.body_file:
        body = Path(args.body_file).read_text(encoding="utf-8")

    result = tio.create_ticket_file(
        args.slug,
        TicketFileSpec(
            summary=args.summary,
            ticket_type=args.ticket_type,
            branch=args.branch,
            scope=args.scope,
            spec=args.spec,
            dependencies=args.dependencies,
            priority=args.priority,
            criteria=criteria,
            on_success=on_success,
            body=body,
        ),
    )
    return 0 if result else 2


def _cmd_contract_open(tio, args):
    try:
        result = tio.contract_open(args.slug)
    except (FileNotFoundError, RuntimeError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    json.dump(result, sys.stdout, indent=2)
    print()
    return 0


def _cmd_contract_seal(tio, args):
    try:
        result = tio.contract_seal(args.slug)
    except (FileNotFoundError, RuntimeError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    json.dump(result, sys.stdout, indent=2)
    print()
    return 0


def _cmd_revise_contract(tio, args):
    try:
        result = tio.contract_revise(args.slug)
    except (FileNotFoundError, RuntimeError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    json.dump(result, sys.stdout, indent=2)
    print()
    return 0


def _cmd_enqueue(tio, args):
    on_success = None
    dest = getattr(args, "destination", None)
    merge = getattr(args, "merge", None)
    cleanup = getattr(args, "cleanup", None)
    triage_report = getattr(args, "triage_report", None)
    remove_targets = getattr(args, "remove_targets", None)
    if any(value is not None for value in (dest, merge, cleanup, triage_report, remove_targets)):
        on_success = {
            "destination": dest or "review",
            "merge": merge if merge is not None else True,
            "cleanup": cleanup if cleanup is not None else True,
            "triage_report": triage_report if triage_report is not None else True,
            "remove_targets": remove_targets or [],
        }
    success = tio.enqueue_ticket(
        args.slug,
        summary=getattr(args, "summary", None),
        ticket_type=getattr(args, "ticket_type", None),
        branch=getattr(args, "branch", None),
        on_success=on_success,
        integration_base=getattr(args, "integration_base", ""),
    )
    return 0 if success else 2


def _cmd_archive(tio, args):
    slug = getattr(args, "slug", None)
    archived = op_archive(
        tio, slug=slug, keep_logs=args.keep_logs, force=getattr(args, "force", False)
    )
    if archived:
        print(f"Archived {len(archived)} ticket(s):")
        for name in archived:
            print(f"  - {name}")
        return 0
    print("No tickets to archive.")
    # A named ticket that was not archived (missing or refused) is a failure;
    # the no-slug sweep legitimately finds nothing.
    return 1 if slug else 0


def _cmd_log_incident(tio, args):
    n = tio.locked_append_incident(
        args.slug, args.incident_type, args.step, args.description, args.resolution
    )
    print(f"Logged incident #{n}")
    return 0


def _validate_logs_report(tio, slug):
    """Run the log-artifact validation for *slug*.

    Returns ``(report_markdown, error_count, raw_result)``, or None when the
    ticket is not on the board.
    """
    entry = tio.find_ticket(slug)
    if not entry:
        return None

    ticket_type = entry.get("type", "feature")
    steps_completed = entry.get("steps_completed", [])

    # Load ticket fields for context-aware checks (e.g. synthesis: false)
    ticket_fields = {}
    ticket_path = tio.logs_dir / slug / "ticket.md"
    if ticket_path.exists():
        with ticket_path.open(encoding="utf-8") as f:
            ticket_fields, _ = parse_frontmatter(f.read())

    result = validate_logs(tio.logs_dir, slug, ticket_type, steps_completed, ticket_fields)
    report, error_count = format_validate_logs_report(result, slug)
    return report, error_count, result


def _cmd_validate_logs(tio, args):
    validated = _validate_logs_report(tio, args.slug)
    if validated is None:
        print(json.dumps({"error": f"Ticket '{args.slug}' not found"}))
        return 1
    report, error_count, result = validated
    print(report)

    # Also output machine-readable JSON to stderr for programmatic use
    print(json.dumps(result, indent=2), file=sys.stderr)
    return 1 if error_count > 0 else 0


def _cmd_collect_evidence(tio, args):
    evidence = op_collect_evidence(tio, args.slug)
    if evidence is None:
        print(json.dumps({"error": f"Ticket '{args.slug}' not found"}))
        return 1
    json.dump(evidence, sys.stdout, indent=2)
    print()
    return 0


def _augment_meta_with_tokens(tio, slug, step_meta, transitions):
    """Augment step_meta with token counts from usage logs or transcripts."""
    # Try per-agent usage.jsonl first
    usage_entries = collect_step_usage(tio.logs_dir, slug)
    if usage_entries:
        step_usage = usage_entries_to_steps(usage_entries)
        for step, totals in step_usage.items():
            total_tok = totals.get("input_tokens", 0) + totals.get("output_tokens", 0)
            if total_tok > 0:
                step_meta.setdefault(step, {})["tokens"] = total_tok
        return

    # Try per-step transcript usage
    step_transcript_usage = collect_step_transcript_usage(tio.logs_dir, slug)
    if step_transcript_usage:
        for step, totals in step_transcript_usage.items():
            total_tok = totals.get("input_tokens", 0) + totals.get("output_tokens", 0)
            if total_tok > 0:
                step_meta.setdefault(step, {})["tokens"] = total_tok
        return

    # Legacy transcript fallback
    transcript_path = tio.logs_dir / slug / "transcript.jsonl"
    if not transcript_path.exists():
        return
    try:
        all_messages = collect_all_messages(transcript_path)
        step_tokens = attribute_tokens_to_steps(all_messages, transitions)
        for step, totals in step_tokens.items():
            total_tok = totals.get("input_tokens", 0) + totals.get("output_tokens", 0)
            if total_tok > 0:
                step_meta.setdefault(step, {})["tokens"] = total_tok
    except (json.JSONDecodeError, OSError, KeyError, ValueError) as exc:
        print(f"Warning: transcript analysis failed: {exc}", file=sys.stderr)


def _cmd_endpoint_table(tio, args):
    """Print the per-endpoint execution table from booley_state.json's timeline.

    One row per call (Flow, MCP tool, Specialist, or agent; then exit code,
    duration, cost, and criteria delta) —
    ``timing`` only reports per-STEP wall-time, which is dominated by queue
    idle. This stays for when the call-by-call sequence is what you want.
    Returns 2 when no timeline is available.
    """
    state_data = _load_state_data(
        existing_runtime_file(tio.logs_dir, args.slug, "booley_state.json")
    )
    timeline = (state_data or {}).get("timeline", []) if isinstance(state_data, dict) else []
    if not timeline:
        print(f"Error: no timeline found for {args.slug}", file=sys.stderr)
        return 2

    rows: list[str] = []
    total_cost = 0.0
    for e in timeline:
        cost = e.get("cost_usd") or 0.0
        with contextlib.suppress(TypeError, ValueError):
            total_cost += float(cost)
        dur = e.get("duration_s")
        dur_str = f"{float(dur):.1f}s" if isinstance(dur, (int, float)) else "-"
        cost_str = f"${float(cost):.4f}" if cost else "-"
        endpoint = e.get("flow") or e.get("mcp_tool") or e.get("agent") or ""
        rows.append(
            f"| {endpoint} | {e.get('exit_code', '')} | {dur_str} | "
            f"{cost_str} | {len(e.get('criteria_set', []))} |"
        )

    lines = [
        f"Endpoint Execution -- {args.slug}",
        "",
        "| Endpoint | Exit | Duration | Cost | Δcriteria |",
        "|------|------|----------|------|-----------|",
        *rows,
        "|------|------|----------|------|-----------|",
        f"| **Total** | | | **${total_cost:.4f}** | |",
    ]
    report = "\n".join(lines)
    if getattr(args, "save", False):
        out_path = tio.logs_dir / args.slug / "endpoint-table.md"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(report + "\n", encoding="utf-8")
        print(f"Saved to {out_path}", file=sys.stderr)
    print(report)
    return 0


def _cmd_timing(tio, args):
    """Show per-step timing for a ticket."""
    if getattr(args, "by_endpoint", False):
        return _cmd_endpoint_table(tio, args)
    transitions_path = existing_human_log_file(tio.logs_dir, args.slug, "transitions.log")
    if not transitions_path.exists():
        print(f"Error: transitions.log not found for {args.slug}", file=sys.stderr)
        return 2
    transitions = parse_transitions_log(transitions_path)

    # Use last_update as end_time for completed tickets
    end_time = None
    entry = tio.find_ticket(args.slug)
    if entry and entry.get("status") in SETTLED_STATUSES:
        last_update = entry.get("last_update", "")
        if last_update:
            with contextlib.suppress(ValueError, TypeError):
                end_time = parse_timestamp(last_update)

    durations = compute_step_durations(transitions, end_time=end_time)
    step_meta: dict = {}
    _augment_meta_with_tokens(tio, args.slug, step_meta, transitions)

    title = f"Step Timing -- {args.slug}"
    report = format_timing_report(durations, title, step_meta=step_meta or None)
    if args.save:
        out_path = tio.logs_dir / args.slug / "timing.md"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with out_path.open("w", encoding="utf-8") as f:
            f.write(report + "\n")
        print(f"Saved to {out_path}", file=sys.stderr)
    print(report)
    return 0


def _resolve_usage_from_slug(tio, slug):
    """Resolve usage from the current run only.

    A full reset moves the prior runtime under ``runs/<N>``. Never recurse into
    that archive: ticket economics describe the run now awaiting review, not
    every discarded attempt that happened to share the slug.
    """
    log_dir = ticket_log_dir(tio.logs_dir, slug)
    runtime_dir = ticket_runtime_dir(log_dir)
    transcripts = sorted((runtime_dir / "developer").glob("*.jsonl"))
    transcripts += sorted((runtime_dir / "transcripts").glob("*/*/*.jsonl"))
    transcripts += sorted((runtime_dir / "triage-prep").glob("*.jsonl"))
    if transcripts:
        return [], {}, transcripts, None

    usage_entries = collect_step_usage(tio.logs_dir, slug)
    if usage_entries:
        return usage_entries, {}, [], None

    step_transcript_usage = collect_step_transcript_usage(tio.logs_dir, slug)
    if step_transcript_usage:
        return [], step_transcript_usage, [], None

    steps_dir = log_dir / "stages"
    transcripts = sorted(steps_dir.glob("*/transcripts/*.jsonl"))
    if transcripts:
        return [], {}, transcripts, None

    print(
        f"Error: no current-run usage data found under {runtime_dir} or {steps_dir}",
        file=sys.stderr,
    )
    return [], {}, [], 2


def _resolve_usage_sources(tio, args):
    """Resolve transcript/usage data sources. Returns (usage_entries, step_transcript_usage, transcripts, err_code)."""
    if args.transcript:
        t = Path(args.transcript)
        if not t.exists():
            print(f"Error: transcript not found: {t}", file=sys.stderr)
            return [], {}, [], 2
        return [], {}, [t], None

    if not args.slug:
        print("Error: provide a transcript path or --slug", file=sys.stderr)
        return [], {}, [], 2

    return _resolve_usage_from_slug(tio, args.slug)


def _authoritative_run_cost(tio, slug: str | None) -> float | None:
    """Sum persisted current-run call costs when ticket state is available."""
    if not slug:
        return None
    state = _load_state_data(existing_runtime_file(tio.logs_dir, slug, "booley_state.json"))
    timeline = state.get("timeline", []) if isinstance(state, dict) else []
    costs = [
        float(entry["cost_usd"])
        for entry in timeline
        if isinstance(entry, dict)
        and isinstance(entry.get("cost_usd"), (int, float))
        and not isinstance(entry["cost_usd"], bool)
    ]
    return sum(costs) if costs else None


def _usage_total_tokens(step_data) -> int:
    """Combined input and output tokens; cache is already part of input."""
    return sum(
        int(data.get("input_tokens", 0)) + int(data.get("output_tokens", 0))
        for data in step_data.values()
    )


def _cmd_usage(tio, args):
    """Show token usage for a ticket or transcript."""
    usage_entries, step_transcript_usage, transcripts, err = _resolve_usage_sources(tio, args)
    if err is not None:
        return err

    # Load transitions for step attribution
    transitions_path = (
        Path(args.transitions)
        if args.transitions
        else (
            existing_human_log_file(tio.logs_dir, args.slug, "transitions.log")
            if args.slug
            else None
        )
    )
    transitions = []
    if transitions_path and transitions_path.exists():
        transitions = parse_transitions_log(transitions_path)

    # Build step_data + total_cost from the best source
    if usage_entries:
        step_data = usage_entries_to_steps(usage_entries)
        total_cost = sum(compute_step_cost(v) for v in step_data.values())
    elif step_transcript_usage:
        step_data = step_transcript_usage
        total_cost = sum(compute_step_cost(v) for v in step_data.values())
    else:
        all_messages = []
        for t in transcripts:
            all_messages.extend(collect_all_messages(t))
        all_messages.sort(key=lambda m: m["timestamp"])
        if not all_messages:
            print("No token usage data found in transcript.", file=sys.stderr)
            return 1
        step_data = attribute_tokens_to_steps(all_messages, transitions)
        total_cost = compute_cost_detailed(all_messages)

    authoritative_cost = _authoritative_run_cost(tio, args.slug)
    if authoritative_cost is not None:
        total_cost = authoritative_cost
    if getattr(args, "summary", False):
        print(f"{_usage_total_tokens(step_data):,} tokens · ${total_cost:.2f}")
        return 0

    durations = compute_step_durations(transitions) if transitions else None
    title = f"Token Usage -- {args.slug}" if args.slug else "Token Usage Report"
    report = format_usage_report(step_data, total_cost, title, step_durations=durations)
    print(report)
    return 0
