"""Ticket intake: parse, validate, atomically claim the ticket, and detect resume state."""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import shutil
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from booley.dev_support.criteria import CriteriaTemplate, find_retired_criteria
from booley.dev_support.development_state import DevelopmentState
from booley.ticket_board.helpers import tickets_dir_from_project_root
from booley.ticket_board.paths import (
    existing_runtime_file,
    migrate_runtime_file,
    ticket_log_dir,
    ticket_runtime_dir,
)

from .. import ticket_cli
from ..blocking import FatalError
from ..models import OnSuccess, TicketContext

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers (called by run())
# ---------------------------------------------------------------------------


def _auto_select_ticket(project_root: Path) -> str:
    """Pick and claim an executable ticket, or raise FatalError."""
    classified = ticket_cli.classify(project_root)
    executable = classified.get("executable", [])
    if not executable:
        reasons = []
        if classified.get("blocked"):
            reasons.append(f"{len(classified['blocked'])} blocked")
        if classified.get("waiting"):
            reasons.append(f"{len(classified['waiting'])} waiting on deps")
        if classified.get("review"):
            reasons.append(f"{len(classified['review'])} in review")
        reason_str = ", ".join(reasons) if reasons else "no tickets in queue"
        raise FatalError(f"No executable tickets: {reason_str}")

    # `classify` already returns executable tickets in claim order: priority
    # first, then in-progress, then oldest-created. Walk that order and take
    # the first ticket whose atomic claim succeeds (§7).
    #
    # This used to shuffle, to de-correlate concurrent runners working from the
    # same classify snapshot — but that made `priority:` inert at claim time
    # (F-51), which is a worse problem than a redundant claim attempt. The
    # claim is atomic, so a runner that loses the race simply falls through to
    # the next candidate; de-correlation was an optimization, not correctness.
    for candidate in executable:
        slug_candidate = candidate.get("slug", Path(candidate.get("file", "")).stem)
        if ticket_cli.claim(project_root, slug_candidate):
            logger.info("Claimed ticket: %s", slug_candidate)
            return slug_candidate
        logger.debug("Ticket %s already claimed, trying next", slug_candidate)

    raise FatalError("All executable tickets claimed by other runners")


def _resolve_and_validate(
    project_root: Path,
    ticket_path_or_slug: str,
) -> tuple[Path, str]:
    """Resolve ticket path from slug/path and validate it."""
    ticket_path = _resolve_ticket_path(project_root, ticket_path_or_slug)
    slug = ticket_path.stem
    validation = ticket_cli.validate_ticket(project_root, str(ticket_path), check_git=False)
    if not validation.get("valid", False):
        errors = validation.get("errors", ["unknown validation error"])
        raise FatalError(f"Ticket validation failed: {'; '.join(errors)}", slug=slug)
    return ticket_path, slug


def _build_context(
    project_root: Path,
    ticket_path: Path,
    slug: str,
    fields: dict,
) -> TicketContext:
    """Construct a TicketContext from parsed frontmatter fields."""
    # Migration: reject retired plan-file fields (the planner specialists were pruned).
    retired_plan_fields = [
        k for k in ("plan_file", "rtl_plan_file", "verification_plan_file") if fields.get(k)
    ]
    if retired_plan_fields:
        raise FatalError(
            f"Retired field(s) in ticket YAML: {', '.join(retired_plan_fields)}. Remove them — "
            "the planner specialists were pruned; put the plan in the ticket body instead.",
            slug=slug,
        )

    return TicketContext(
        slug=slug,
        ticket_path=ticket_path,
        ticket_type=fields.get("type", "feature"),
        branch=fields.get("branch", "master"),
        summary=fields.get("summary", ""),
        scope_raw=fields.get("scope", []),
        spec=fields.get("spec", ""),
        on_success=OnSuccess.from_dict(fields.get("on_success")),
        dependencies=fields.get("dependencies", []),
        priority=fields.get("priority", "medium"),
        base_sha=fields.get("base_sha", ""),
        feature_branch=fields.get("feature_branch", ""),
        completed_steps=fields.get("steps_completed", []),
        current_step=fields.get("stage", ""),
        project_root=project_root,
        criteria=fields.get("criteria", {}),
    )


def _check_dependencies(ctx: TicketContext) -> None:
    """Raise FatalError if any ticket dependencies are unmet."""
    if not ctx.dependencies:
        return
    # Check done/ directory directly -- classify() only returns actionable
    # tickets (executable/blocked/waiting/review/orphaned), not done ones.
    done_dir = tickets_dir_from_project_root(ctx.project_root) / "board" / "done"
    done_slugs: set[str] = set()
    if done_dir.is_dir():
        for f in done_dir.glob("*.md"):
            done_slugs.add(f.stem)
    unmet = [dep for dep in ctx.dependencies if dep not in done_slugs]
    if unmet:
        raise FatalError(f"Unmet dependencies: {', '.join(unmet)} -- leaving in queue")


def _detect_and_apply_resume(ctx: TicketContext, fields: dict) -> str:
    """Detect resume state, update ctx stages, activate ticket. Returns action."""
    project_root = ctx.project_root
    resume_info = ticket_cli.resume(project_root, ctx.slug)
    action = resume_info.get("action", "fresh")
    resume_stage = resume_info.get("stage", "")
    logger.debug("Resume action for %s: %s (stage=%s)", ctx.slug, action, resume_stage)

    # Load progress.json for steps_completed and blocked_reason
    # (resume_detect doesn't return these -- they live in progress.json)
    progress = _load_progress(project_root, ctx.slug)
    persisted_intent = progress.get("workspace_intent", "fresh")
    if persisted_intent not in {"fresh", "resume"}:
        logger.warning(
            "Ignoring invalid workspace_intent %r for %s",
            persisted_intent,
            ctx.slug,
        )
        persisted_intent = "fresh"
    ctx.workspace_intent = (
        "resume" if action in {"continue", "resume_blocked"} else persisted_intent
    )

    ctx.execution_id = uuid.uuid4().hex
    try:
        from booley.config.project_config import ENV_PREFIX as _proj_env

        _orch_env = f"{_proj_env}_DEVELOPER_PID"
    except (ImportError, AttributeError):
        _orch_env = "BOOLEY_DEVELOPER_PID"
    developer_pid = int(os.environ.get(_orch_env, "0")) or os.getpid()

    if action == "fresh":
        ticket_cli.init_ticket(
            project_root,
            str(ctx.ticket_path),
            execution_id=ctx.execution_id,
            owner_pid=developer_pid,
        )
        ctx.completed_steps = []
        ctx.current_step = ""
    elif action == "continue":
        _apply_continue(ctx, progress, fields)
    elif action == "resume_blocked":
        _apply_resume_blocked(ctx, progress, fields)

    # Ensure ticket.md exists in logs dir — it may be missing if a prior
    # run failed at validation before init_ticket() had a chance to copy it.
    _ensure_ticket_snapshot(project_root, ctx.slug, ctx.ticket_path)

    # Activate ticket for non-fresh resume.
    # init_ticket (fresh path) moves to active/. For all other resume
    # actions, the ticket may be in queue/ after reset/unblock.
    # activate() checks PID ownership: if another live runner owns
    # the ticket, it returns False and we abort.
    if action != "fresh" and not ticket_cli.activate(
        project_root,
        ctx.slug,
        owner_pid=developer_pid,
        execution_id=ctx.execution_id,
    ):
        # No slug= here: the ticket belongs to another live runner, so we
        # must NOT fail/block it.  Omitting slug makes the developer's
        # FatalError handler skip ticket_cli.fail(), leaving the ticket
        # untouched in active/.
        raise FatalError(
            f"Ticket '{ctx.slug}' is already being executed by another runner",
        )

    # PID stamp for orphan detection: _ticket_lock() (called by both
    # init_ticket and activate) already stamps the developer PID
    # from the *_DEVELOPER_PID env var.  No separate write needed.
    return action


def _ensure_ticket_snapshot(project_root: Path, slug: str, ticket_path: Path) -> None:
    """Copy ticket.md into logs dir if missing (e.g. prior run failed before init)."""
    logs_dir = ticket_log_dir(tickets_dir_from_project_root(project_root) / "logs", slug)
    ticket_md = logs_dir / "ticket.md"
    if ticket_md.exists():
        return
    logs_dir.mkdir(parents=True, exist_ok=True)
    # ticket_path may point at queue/ or blocked/ — find the actual file
    for candidate in (
        ticket_path,
        ticket_path.parent.parent / "active" / ticket_path.name,
        ticket_path.parent.parent / "blocked" / ticket_path.name,
        ticket_path.parent.parent / "queue" / ticket_path.name,
    ):
        if candidate.exists():
            shutil.copy2(str(candidate), str(ticket_md))
            logger.info("Recovered missing ticket.md from %s", candidate)
            return
    logger.warning("Could not find ticket source to copy ticket.md for %s", slug)


def _load_progress(project_root: Path, slug: str) -> dict:
    """Load progress.json for a ticket, returning {} on missing/corrupt file."""
    logs_dir = tickets_dir_from_project_root(project_root) / "logs"
    prog_path = existing_runtime_file(logs_dir, slug, "progress.json")
    if not prog_path.exists():
        return {}
    with contextlib.suppress(json.JSONDecodeError, OSError):
        return json.loads(prog_path.read_text(encoding="utf-8"))
    return {}


def _apply_continue(ctx: TicketContext, progress: dict, fields: dict) -> None:
    """Apply 'continue' resume state to context."""
    ctx.completed_steps = progress.get("steps_completed", fields.get("steps_completed", []))
    # current_step must be the LAST COMPLETED stage, not the resume target.
    # The main loop calls next_stage(current_step) to get what to run next,
    # so setting current_step = resume_stage would SKIP that stage.
    ctx.current_step = ctx.completed_steps[-1] if ctx.completed_steps else ""


def _apply_resume_blocked(ctx: TicketContext, progress: dict, fields: dict) -> None:
    """Apply 'resume_blocked' state — verify questions answered if needed."""
    block_reason = progress.get("blocked_reason", "")
    ctx.completed_steps = progress.get("steps_completed", fields.get("steps_completed", []))
    # current_step must be the LAST COMPLETED stage before the blocked one,
    # so the main loop's next_stage() call re-runs the blocked stage (not skips it).
    ctx.current_step = ctx.completed_steps[-1] if ctx.completed_steps else ""
    # Only require questions.md verification for question-type blocks
    if "question" in block_reason.lower() or "unresolved" in block_reason.lower():
        _verify_questions_answered(ctx.logs_dir / "questions.md")

    # Clear stale _blocked_reason from booley_state.json so the new run
    # isn't poisoned by the previous run's block verdict.
    _clear_stale_blocked_reason(ctx)


def _clear_stale_blocked_reason(ctx: TicketContext) -> None:
    """Remove _blocked_reason from booley_state.json on re-run."""
    state_path = existing_runtime_file(ctx._tickets_dir / "logs", ctx.slug, "booley_state.json")
    if not state_path.exists():
        return
    state = DevelopmentState.load(state_path)
    if "_blocked_reason" in state.criteria:
        del state.criteria["_blocked_reason"]
        state.save()
        logger.info("Cleared stale _blocked_reason from %s", state_path)


def _verify_questions_answered(questions_path: Path) -> None:
    """Raise FatalError if questions.md has unanswered questions."""
    if not questions_path.exists():
        return
    content = questions_path.read_text(encoding="utf-8", errors="replace")
    unanswered = re.findall(
        r"\*\*Answer:\*\*\s*\n(?=\s*(?:\n|##|$))",
        content,
    )
    if unanswered:
        raise FatalError(
            f"{len(unanswered)} question(s) in questions.md not yet answered -- "
            "fill in answers first"
        )


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


async def run(ticket_path_or_slug: str, project_root: Path) -> TicketContext:
    """Parse ticket, validate, detect resume state, return populated context.

    Args:
        ticket_path_or_slug: Path to .md file, slug, or empty for auto-select.
        project_root: Absolute path to project root.

    Returns:
        Fully populated TicketContext.

    Raises:
        FatalError: On validation failures or missing dependencies.
    """
    if not ticket_path_or_slug:
        ticket_path_or_slug = _auto_select_ticket(project_root)

    ticket_path, slug = _resolve_and_validate(project_root, ticket_path_or_slug)

    parsed = ticket_cli.parse_ticket(project_root, str(ticket_path))
    fields = parsed.get("fields", {})

    ctx = _build_context(project_root, ticket_path, slug, fields)
    _check_dependencies(ctx)

    action = _detect_and_apply_resume(ctx, fields)

    if action == "fresh" or _criteria_state_needs_reinit(ctx):
        _init_criteria_state(ctx)

    return ctx


def _resolve_ticket_path(project_root: Path, path_or_slug: str) -> Path:
    """Resolve a ticket path or slug to an absolute .md path."""
    p = Path(path_or_slug)

    # Already absolute path
    if p.is_absolute() and p.exists():
        return p

    # Relative path from project root
    candidate = project_root / p
    if candidate.exists():
        return candidate

    # Relative to tickets dir (classify output uses this format)
    tickets_dir = tickets_dir_from_project_root(project_root)
    candidate = tickets_dir / p
    if candidate.exists():
        return candidate

    # Try as slug -- search board/ directories
    for subdir in [
        "board/queue",
        "board/active",
        "board/blocked",
        "board/waiting",
        "board/review",
    ]:
        candidate = tickets_dir / subdir / f"{path_or_slug}.md"
        if candidate.exists():
            return candidate

    # Try with .md extension
    if not path_or_slug.endswith(".md"):
        return _resolve_ticket_path(project_root, path_or_slug + ".md")

    raise FatalError(f"Ticket not found: {path_or_slug}")


def _init_criteria_state(ctx: TicketContext) -> None:
    """Initialize booley_state.json from ticket criteria section.

    Parses criteria via CriteriaTemplate, expands per-target, seeds project
    criteria, and writes the initial DevelopmentState.
    """
    if ctx.criteria:
        template = CriteriaTemplate.from_yaml(ctx.criteria)
    else:
        template = CriteriaTemplate.for_ticket_type(ctx.ticket_type)

    targets = ctx.sim_targets
    expanded = template.expand(targets)
    category_overrides = template.category_overrides(targets)
    aliases = template.flow_key_aliases()
    criterion_params = template.expand_params(targets)
    _freeze_synthesis_recipe_fingerprints(ctx, expanded, criterion_params)
    _freeze_fpga_recipe_fingerprints(ctx, expanded, criterion_params)

    # Migration: reject retired criterion keys by name. This has to hard-error --
    # an unrecognized key is otherwise created as *optional* (development_state),
    # which would silently downgrade a mandatory gate to a no-op. slug=ctx.slug is
    # essential: without it the developer's FatalError handler skips ticket_cli.fail(),
    # so this specific, actionable error never reaches any ticket-scoped log and the
    # ticket is left orphaned in active/ (later swept as a bogus "SIGINT" crash).
    stale = find_retired_criteria(expanded)
    if stale:
        keys = ", ".join(k for k, _ in stale)
        details = "; ".join(f"{k} -> {hint}" for k, hint in stale)
        raise FatalError(
            f"Retired criterion key(s) in ticket YAML: {keys}. {details}.",
            slug=ctx.slug,
        )

    _seed_project_criteria(ctx.project_root, expanded, category_overrides, targets)
    # Internal mandatory criterion (hidden from users via `_` prefix) -- the
    # developer must call submit_run_report as its final action so a human
    # reviewer gets a structured summary of what was done and why. Projects
    # that don't consume the reports opt out via [developer] run_report =
    # false; the criterion is then never seeded and the acceptance gate
    # (criteria_acceptance) skips its check.
    from booley.config.project_config import is_run_report_enabled

    if is_run_report_enabled():
        expanded["_report_submitted"] = True

    state_path = migrate_runtime_file(ctx.logs_dir, "booley_state.json")
    state = DevelopmentState.load(state_path)
    state.slug = ctx.slug
    state.ticket_type = ctx.ticket_type
    state.init_criteria(
        expanded,
        category_overrides=category_overrides,
        flow_key_aliases=aliases,
        criterion_params=criterion_params,
    )
    state.save()

    logger.info(
        "Initialized criteria state for %s: %d criteria (%d mandatory)",
        ctx.slug,
        len(expanded),
        sum(1 for v in expanded.values() if v),
    )


def _freeze_synthesis_recipe_fingerprints(
    ctx: TicketContext,
    expanded: dict[str, bool],
    criterion_params: dict[str, dict[str, Any]],
) -> None:
    """Freeze each synthesis criterion's normalized Target recipe at intake."""
    from booley.flows.synth.recipe import default_recipe_args, synthesis_recipe_snapshot

    _freeze_recipe_family(
        ctx,
        expanded,
        criterion_params,
        prefix="synthesis_ok_",
        flow_label="Synthesis",
        snapshot_builder=lambda resolved, target: synthesis_recipe_snapshot(
            resolved,
            default_recipe_args(),
            target=target,
        ),
    )


def _freeze_fpga_recipe_fingerprints(
    ctx: TicketContext,
    expanded: dict[str, bool],
    criterion_params: dict[str, dict[str, Any]],
) -> None:
    """Freeze each FPGA criterion's normalized Target recipe at intake."""
    from booley.flows.fpga.recipe import fpga_recipe_snapshot

    _freeze_recipe_family(
        ctx,
        expanded,
        criterion_params,
        prefix="fpga_impl_ok_",
        flow_label="FPGA implementation",
        snapshot_builder=lambda resolved, target: fpga_recipe_snapshot(
            resolved,
            target=target,
        ),
    )


def _freeze_recipe_family(
    ctx: TicketContext,
    expanded: dict[str, bool],
    criterion_params: dict[str, dict[str, Any]],
    *,
    prefix: str,
    flow_label: str,
    snapshot_builder: Callable[[Any, str], dict[str, Any]],
) -> None:
    """Freeze one implementation criterion family's revision-owned recipes."""
    from booley.flows.recipe_evidence import (
        RECIPE_FINGERPRINT_PARAM,
        RECIPE_SNAPSHOT_PARAM,
        recipe_snapshot_fingerprint,
    )

    keys = [key for key in expanded if key.startswith(prefix)]
    recipe_root = ticket_runtime_dir(ctx.logs_dir) / "recipe-freeze" / prefix.rstrip("_")
    for key in keys:
        target = key.removeprefix(prefix)
        params = criterion_params.setdefault(key, {})
        needs_baseline = _pin_recipe_baseline(ctx, key, params, flow_label)
        build_root = recipe_root / target
        shutil.rmtree(build_root, ignore_errors=True)
        snapshot = _snapshot_intake_recipe(
            ctx,
            key,
            target,
            build_root,
            needs_baseline,
            flow_label,
            snapshot_builder,
        )
        if snapshot is None:
            continue
        params[RECIPE_FINGERPRINT_PARAM] = recipe_snapshot_fingerprint(snapshot)
        params[RECIPE_SNAPSHOT_PARAM] = snapshot


def _pin_recipe_baseline(
    ctx: TicketContext,
    key: str,
    params: dict[str, Any],
    flow_label: str,
) -> bool:
    """Pin relative recipe evidence to the ticket baseline, returning whether needed."""
    from booley.flows.recipe_evidence import BASELINE_REF_PARAM

    needs_baseline = _has_relative_threshold(params)
    if needs_baseline and not ctx.base_sha:
        raise FatalError(
            f"{flow_label} criterion {key!r} requires a baseline-relative "
            "threshold, but the ticket has no base_sha",
            slug=ctx.slug,
        )
    if needs_baseline:
        params[BASELINE_REF_PARAM] = ctx.base_sha
    return needs_baseline


def _snapshot_intake_recipe(
    ctx: TicketContext,
    key: str,
    target: str,
    build_root: Path,
    needs_baseline: bool,
    flow_label: str,
    snapshot_builder: Callable[[Any, str], dict[str, Any]],
) -> dict[str, Any] | None:
    """Resolve one intake Target and return its normalized recipe when it exists."""
    from booley.core.boundary import BoundaryError
    from booley.fusesoc import fusesoc_registry

    try:
        fusesoc_registry.resolve_ref(ctx.work_dir, target)
    except fusesoc_registry.UnknownTargetError:
        if needs_baseline:
            raise FatalError(
                f"{flow_label} criterion {key!r} requires baseline metrics, but "
                f"Target {target!r} does not exist at ticket intake",
                slug=ctx.slug,
            ) from None
        logger.info(
            "%s Target %r is not authored at ticket intake; deferring validation",
            flow_label,
            target,
        )
        return None
    except fusesoc_registry.FuseSocError as exc:
        raise FatalError(
            f"Cannot freeze {flow_label.lower()} recipe for Target {target!r}: {exc}",
            slug=ctx.slug,
        ) from exc
    try:
        resolved = fusesoc_registry.resolve_target(
            target,
            project_root=ctx.work_dir,
            build_root=build_root,
        )
        return snapshot_builder(resolved, target)
    except (fusesoc_registry.TargetResolutionError, BoundaryError, OSError) as exc:
        raise FatalError(
            f"Cannot freeze {flow_label.lower()} recipe for Target {target!r}: {exc}",
            slug=ctx.slug,
        ) from exc


def _has_relative_threshold(params: dict[str, Any]) -> bool:
    """Whether criterion params require baseline metrics."""
    return any(
        key.endswith(("_increase_at_most", "_reduce_at_least"))
        for key in params
        if not key.startswith("_")
    )


def _criteria_state_needs_reinit(ctx: TicketContext) -> bool:
    """Return True when persisted state is missing or cannot enforce the ticket."""
    state_path = existing_runtime_file(ctx._tickets_dir / "logs", ctx.slug, "booley_state.json")
    if not state_path.exists():
        return True
    state = DevelopmentState.load(state_path)
    if state.slug != ctx.slug:
        return True
    template = (
        CriteriaTemplate.from_yaml(ctx.criteria)
        if ctx.criteria
        else (CriteriaTemplate.for_ticket_type(ctx.ticket_type))
    )
    expected = template.expand(ctx.sim_targets)
    expected_mandatory = {k for k, mandatory in expected.items() if mandatory}
    if not expected_mandatory:
        return False
    actual_mandatory = {
        k for k, entry in state.criteria.items() if entry.mandatory and not k.startswith("_")
    }
    return not expected_mandatory.issubset(actual_mandatory)


def _seed_project_criteria(
    project_root: Path,
    expanded: dict[str, bool],
    category_overrides: dict[str, str],
    targets: list[str],
) -> None:
    """Seed project criteria from .booley_project/criteria.toml into state dicts.

    Adds project criteria as optional (mandatory=False) since they're
    defined at the project level, not the ticket level. If a ticket's
    YAML also declares the same criterion, the ticket wins (already in expanded).
    """
    try:
        from booley.dev_support.criteria import (
            expand_criteria_defs,
            load_project_criteria,
        )
    except ImportError:
        return

    criteria_path = project_root / ".booley_project" / "criteria.toml"
    project_defs = load_project_criteria(criteria_path)
    if not project_defs:
        return

    # Filter per-target criteria by each Target's declared EDA tool (decision 11).
    # Empty (no .core authored yet) leaves the expansion unfiltered.
    try:
        from booley.fusesoc.fusesoc_registry import target_eda_tools

        target_eda_tool_map = target_eda_tools(project_root)
    except Exception:  # noqa: BLE001 — no .core / registry error leaves expansion unfiltered
        target_eda_tool_map = {}
    project_expanded = expand_criteria_defs(project_defs, targets, target_eda_tool_map)
    for key, crit_def in project_expanded.items():
        if key not in expanded:
            # Add as optional — project criteria are available but not
            # mandatory unless the ticket explicitly requires them
            expanded[key] = False
        # Always set category override for invalidation cascade
        if crit_def.category != "none":
            category_overrides[key] = crit_def.category
