"""Main execution loop -- criteria-based developer with agent escalation.

Steps 00 (parse/validate) and 01 (setup) run as Python functions.
All subsequent work is handled by the Developer Agent invoking Flows and Specialists
against acceptance criteria defined in the ticket.

EXIT INVARIANT: the ticket MUST be transitioned out of active/ before
the developer stops -- either to review/ (success), blocked/, or
archived/. Every exit path (normal completion, BlockingError, FatalError,
unexpected exception) upholds this via block_ticket() or fail_ticket().
"""

from __future__ import annotations

import asyncio
import json as _json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING

from booley.dev_support.development_state import (
    DevelopmentState,
)
from booley.runtime.developer_budget import DeveloperBudget, run_with_developer_budget
from booley.runtime.git import git_run
from booley.runtime.platform_paths import bash_bin
from booley.runtime.project_dir import resolve_project_dir
from booley.runtime.prompt_artifacts import write_prompt_artifacts
from booley.runtime.timefmt import compact_utc_now
from booley.ticket_board.paths import (
    existing_ticket_runtime_file,
    migrate_runtime_file,
    ticket_human_log_file,
    ticket_runtime_dir,
    ticket_runtime_file,
)

from . import terminal, ticket_cli
from .auto_retry import maybe_auto_retry, record_crash
from .blocking import ContextExhaustedError, FatalError, block_ticket, fail_ticket
from .colors import bold_green, green, yellow
from .console_metrics import WorktreeLineCounter
from .developer_display import (
    DisplayWatcher,
    _attach_click_links,
    _console_activity,
    _console_setup_msg,
    _display_ticket_banner,
    _make_console_event_handler,
    _push_initial_criteria,
    _refresh_link_ctx_post_setup,
    _wire_console_callbacks,
    agent_event_handler,
)
from .logging_utils import set_current_step, setup_file_logging, teardown_file_logging
from .models import AgentResult, TicketContext
from .preflight import run_preflight
from .terminal import (
    close_log,
    open_log,
    step_end_header,
    step_footer,
    step_line,
    step_start_header,
)
from .worktree_health import check_worktree_health

if TYPE_CHECKING:
    from .developer_guardrails import DirtyFile

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Lightweight helpers that replace Checkpoint
# ---------------------------------------------------------------------------


def _recover_setup_state(ctx: TicketContext, project_root: Path) -> None:
    """Restore worktree_path and feature_branch from setup step metadata on resume."""
    if "setup" not in ctx.completed_steps:
        return

    # Discover worktree from the deterministic path convention used by setup/workspace.py.
    if not ctx.worktree_path:
        expected_wt = (
            resolve_project_dir(project_root) / "worktrees" / ctx.slug
            if ctx.target_contract is not None
            else project_root / ".booley_project" / "worktrees" / ctx.slug
        )
        if (expected_wt / ".git").exists():
            ctx.worktree_path = expected_wt
            logger.debug("Recovered worktree_path from filesystem: %s", expected_wt)

    # Recover feature_branch from the worktree's checked-out branch.
    if not ctx.feature_branch and ctx.worktree_path:
        try:
            result = subprocess.run(
                ["git", "branch", "--show-current"],
                cwd=ctx.worktree_path,
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            branch = result.stdout.strip()
            if branch:
                ctx.feature_branch = branch
                logger.debug("Recovered feature_branch from worktree: %s", branch)
        except (OSError, subprocess.TimeoutExpired):
            pass


def _write_status(logs_dir: Path, slug: str, step: str) -> None:
    """Write minimal status.json for heartbeat display."""
    from booley.runtime.timefmt import utc_now_rfc3339

    path = ticket_runtime_file(logs_dir, "status.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(
        _json.dumps(
            {
                "slug": slug,
                "step": step,
                "last_updated": utc_now_rfc3339(),
            }
        ),
        encoding="utf-8",
    )
    tmp.replace(path)


def _status_step(logs_dir: Path) -> str:
    """Return the latest persisted step, degrading safely for old runs."""
    try:
        value = _json.loads(ticket_runtime_file(logs_dir, "status.json").read_text())
    except (OSError, _json.JSONDecodeError):
        return ""
    return str(value.get("step", "")) if isinstance(value, dict) else ""


async def run_ticket(
    ticket_path_or_slug: str,
    project_root: Path | None = None,
    *,
    save_transcripts: bool = True,
    use_console: bool = False,
) -> None:
    """Execute the full developer flow for a ticket.

    Args:
        ticket_path_or_slug: Path to ticket .md file, or slug for resume.
        project_root: Project root directory. Defaults to cwd.
        save_transcripts: Write per-agent JSONL transcripts to logs dir.
        use_console: Use full-screen Console TUI instead of log mode.

    In console mode the TUI is launched first and preflight/parse-validate
    run inside its worker (with SetupProgress events for visibility) so the
    user never sees pre-TUI chrome flash by.
    """
    if project_root is None:
        project_root = Path.cwd()

    if use_console:
        await _run_with_console(ticket_path_or_slug, project_root, save_transcripts)
    else:
        await _run_log_mode(ticket_path_or_slug, project_root, save_transcripts)


async def _prepare_ticket(
    ticket_path_or_slug: str,
    project_root: Path,
    save_transcripts: bool,
) -> TicketContext:
    """Run config-load, preflight, and parse-validate. Returns the ready ctx.

    Shared by log-mode and console-mode entry paths so the bring-up sequence
    stays identical.
    """
    from booley.config.settings import load_models_config

    from .setup.intake import run as parse_validate

    load_models_config(project_root)
    run_preflight(project_root)
    try:
        ctx = await parse_validate(ticket_path_or_slug, project_root)
    except FatalError as e:
        if e.slug:
            logger.error("Ticket intake failed for %s: %s", e.slug, e.error)
            ticket_cli.fail(project_root, e.slug, error=e.error, step="parse-validate")
        raise
    ctx.save_transcripts = save_transcripts
    return ctx


async def _run_log_mode(
    ticket_path_or_slug: str,
    project_root: Path,
    save_transcripts: bool,
) -> None:
    """Original (no-TUI) flow: prepare, then run the ticket body inline."""
    exec_start = time.monotonic()
    ctx = await _prepare_ticket(ticket_path_or_slug, project_root, save_transcripts)
    setup_file_logging(ticket_human_log_file(ctx.logs_dir, "harness.log"))
    open_log(ticket_human_log_file(ctx.logs_dir, "run.log"))
    try:
        await _run_ticket_body(ctx, project_root, exec_start)
    finally:
        close_log()
        teardown_file_logging()


def _invalidate_missing_worktree(ctx: TicketContext, project_root: Path) -> None:
    """If setup claims complete but the worktree is unusable, reset for re-run."""
    if "setup" not in ctx.completed_steps or not ctx.worktree_path:
        return

    health = check_worktree_health(project_root, ctx.worktree_path)
    if health.ok:
        return

    logger.warning(
        "Worktree unusable (%s) -- re-running setup: %s",
        ctx.worktree_path,
        health.reason,
    )
    ctx.completed_steps.remove("setup")
    ctx.worktree_path = None
    ctx.feature_branch = ""
    ctx.current_step = ""


async def _run_setup_step(ctx: TicketContext, project_root: Path) -> bool:
    """Run the setup step. Returns True if ticket was blocked."""
    set_current_step("setup")
    _write_status(ctx.logs_dir, ctx.slug, "setup")
    step_start_header("setup")
    _console_setup_msg("setup: creating worktree and branch...")
    from .setup.workspace import run as setup_stage

    result = await setup_stage(ctx)
    if result.block_reason:
        block_ticket(ctx, result.block_reason, "setup")
        step_line(yellow(f"[BLOCK] {result.block_reason}"))
        _console_setup_msg(f"setup: BLOCKED - {result.block_reason}")
        step_end_header("setup")
        step_footer()
        return True
    ticket_cli.update_board(project_root, ctx.slug, append_step="setup")
    step_line(f"{green('[PASS] Branch')} {bold_green(ctx.feature_branch)} {green('ready')}")
    _console_setup_msg(f"setup: branch {ctx.feature_branch} ready")
    step_end_header("setup")
    step_footer()
    return False


def _log_final_cost(ctx: TicketContext, exec_start: float) -> None:
    """Log total execution time and cost, print to terminal."""
    from booley.ticket_board.helpers import fmt_duration

    exec_elapsed = time.monotonic() - exec_start
    state_path = existing_ticket_runtime_file(ctx.logs_dir, "booley_state.json")
    cost = 0.0
    if state_path.exists():
        fs = DevelopmentState.load(state_path)
        cost = fs.total_cost()
        logger.info("Execution complete (%s, total cost $%.2f)", fmt_duration(exec_elapsed), cost)
    else:
        logger.info("Execution complete (%s)", fmt_duration(exec_elapsed))
    terminal.run_totals(exec_elapsed, cost)


async def _run_ticket_body(ctx: TicketContext, project_root: Path, exec_start: float) -> None:
    """Inner execution body -- separated so teardown_file_logging always runs."""
    logger.debug(
        "Execution started for %s (type=%s, branch=%s)", ctx.slug, ctx.ticket_type, ctx.branch
    )
    _display_ticket_banner(ctx)

    # On resume, restore worktree_path + feature_branch from setup step metadata
    setup_was_complete = "setup" in ctx.completed_steps
    _recover_setup_state(ctx, project_root)
    _invalidate_missing_worktree(ctx, project_root)
    resume_uses_existing_setup = setup_was_complete and "setup" in ctx.completed_steps

    # ---- Setup (if not already completed) ----
    if "setup" not in ctx.completed_steps:
        setup_blocked = await _run_setup_step(ctx, project_root)
        if setup_blocked:
            await _prepare_blocked_triage(ctx, project_root)
            return

    # A blocked ticket may have received an expanded scope during triage while
    # retaining its worktree and completed setup marker. Refresh the persisted
    # guard before the developer runs so newly authorized paths are not rejected
    # by the old .scope.json or hook installation.
    if resume_uses_existing_setup and ctx.worktree_path:
        from .setup.workspace import refresh_scope_guards

        try:
            refresh_scope_guards(
                ctx.worktree_path,
                ctx.scope,
                project_root=project_root,
            )
        except OSError as exc:
            fail_ticket(ctx, f"scope guard refresh failed: {exc}", "setup")
            return

    # Setup created the worktree -- refresh the click-link resolver so
    # post-setup file clicks resolve against the worktree copy with a
    # real fork-base for diffs. No-op when Console isn't active.
    _refresh_link_ctx_post_setup(ctx)

    if not ctx.current_step:
        ctx.current_step = "setup"

    # ---- Worktree cleanup on resume ----
    try:
        if ctx.current_step != "setup" and ctx.worktree_path:
            _reset_worktree_if_dirty(ctx)
    except (OSError, subprocess.SubprocessError, ValueError) as e:
        fail_ticket(
            ctx, f"worktree cleanup failed: {type(e).__name__}: {e}", ctx.current_step or "setup"
        )
        return

    # ---- Developer Agent (criteria-based) ----
    await _run_developer_path(ctx, project_root)

    set_current_step("")
    _log_final_cost(ctx, exec_start)


# ---------------------------------------------------------------------------
# Console TUI integration
# ---------------------------------------------------------------------------


async def _run_with_console(
    ticket_path_or_slug: str,
    project_root: Path,
    save_transcripts: bool,
) -> None:
    """Run the full ticket flow inside the Console TUI.

    The Textual app launches FIRST, with a placeholder header; preflight
    and parse-validate then run inside the app's worker so the user never
    sees pre-TUI INFO logs flash before the screen takeover. The header
    is filled in once parse-validate has produced a ticket context.

    On Textual ImportError, falls back to log mode. Errors raised inside
    the worker are captured and re-raised after the app exits, so the
    outer entry point can map them to the right exit code (preflight=2,
    user-quit=EXIT_USER_QUIT, etc.).
    """
    try:
        from .console.app import ConsoleApp, ConsolePhase
        from .console.events import SetupProgress
        from .console.widgets import TicketHeader
    except ImportError:
        logger.warning("Textual not available, falling back to log mode")
        await _run_log_mode(ticket_path_or_slug, project_root, save_transcripts)
        return

    from .blocking import UserQuitError

    # Empty/placeholder header -- TicketHeader renders blank until
    # parse-validate succeeds and set_ticket_info() is called below.
    app = ConsoleApp()

    worker_error: list[BaseException] = []
    harness_started = False
    harness_completed = False

    async def harness_work() -> None:
        nonlocal harness_started, harness_completed
        harness_started = True
        exec_start = time.monotonic()
        try:
            app.post_message(SetupProgress("loading model/backend config..."))
            app.post_message(SetupProgress("running preflight checks..."))
            app.post_message(SetupProgress("parsing & validating ticket..."))
            ctx = await _prepare_ticket(
                ticket_path_or_slug,
                project_root,
                save_transcripts,
            )
            # Now we know the slug/type/branch -- fill in the real header.
            app.query_one(TicketHeader).set_ticket_info(
                ctx.slug,
                ctx.ticket_type,
                ctx.branch,
            )
            # Attach the click-link resolver context to the main pane.
            # Worktree info is unknown until setup runs — attached lazily
            # there via link_ctx.attach_worktree(). See ctx._link_ctx below.
            _attach_click_links(app, ctx, project_root)
            setup_file_logging(ticket_human_log_file(ctx.logs_dir, "harness.log"))
            open_log(ticket_human_log_file(ctx.logs_dir, "run.log"))
            try:
                # Setup is finished — we're now in the ticket-execution loop.
                # Past this point MCP endpoint/Criteria/Agent events route normally.
                app.transition_to(ConsolePhase.RUNNING)
                await _run_ticket_body(ctx, project_root, exec_start)
                harness_completed = True
            finally:
                close_log()
                teardown_file_logging()
        except BaseException as e:
            # Capture for re-raise after app exits; log to file for post-mortem.
            worker_error.append(e)
            logger.exception("Harness worker failed in console mode")
        finally:
            app.exit()

    app._harness_work = harness_work
    terminal.set_console_active(True, app=app)

    try:
        await app.run_async()
    except Exception:
        logger.exception("Console crashed")
        terminal.set_console_active(False)
        app.transition_to(ConsolePhase.EXITED)
        # If the worker never started, we can still retry in log mode.
        if not harness_started:
            await _run_log_mode(ticket_path_or_slug, project_root, save_transcripts)
            return
        if not harness_completed:
            logger.error("Console crashed mid-run -- harness was in progress, cannot safely retry")
    finally:
        terminal.set_console_active(False)
        app.transition_to(ConsolePhase.EXITED)

    if worker_error:
        raise worker_error[0]

    if getattr(app, "_user_quit", False) and not harness_completed:
        raise UserQuitError("User quit Console TUI")


# ---------------------------------------------------------------------------
# Worktree utilities (stay in developer — pre-loop concerns)
# ---------------------------------------------------------------------------


def _is_safe_worktree(ctx: TicketContext) -> bool:
    """Verify we're in an isolated execution worktree, not the main repo."""
    wt = ctx.worktree_path
    if wt is None:
        return False
    wt_resolved = wt.resolve()
    root_resolved = ctx.project_root.resolve()
    if wt_resolved == root_resolved:
        return False
    allowed_parents = [root_resolved / ".booley_project" / "worktrees"]
    if ctx.target_contract is not None:
        allowed_parents.append(resolve_project_dir(root_resolved) / "worktrees")
    return any(wt_resolved.is_relative_to(p) for p in allowed_parents)


def _scope_expects_rtl_output(scope: list[str]) -> bool:
    """Return True when declared scope implies live RTL should exist."""
    for raw in scope:
        entry = raw.removesuffix(" [new]").strip()
        if entry.startswith(("rtl/", "rtl\\", "fw/", "fw\\")):
            return True
    return False


def _has_live_rtl_output(worktree: Path) -> bool:
    """Check for RTL files anywhere under the active worktree output dir.

    Recursive on purpose: the standard OpenCores layout nests sources one
    level down (``rtl/verilog/*.v``), which a non-recursive glob misses (F-15).
    """
    rtl_dir = worktree / "rtl"
    if not rtl_dir.is_dir():
        return False
    return any(rtl_dir.rglob("*.sv")) or any(rtl_dir.rglob("*.v"))


def _nested_rtl_output_files(worktree: Path) -> list[str]:
    """Return HDL files below an accidental nested rtl/ source root."""
    nested = worktree / "rtl" / "rtl"
    if not nested.is_dir():
        return []
    files = [*nested.rglob("*.sv"), *nested.rglob("*.v")]
    return sorted(path.relative_to(worktree).as_posix() for path in files)


def _is_scorer_consumed_path(path: str) -> bool:
    """True for dirty files that could affect final benchmark scoring."""
    normalized = path.replace("\\", "/")
    return normalized.startswith(("rtl/", "fw/"))


def _has_duplicated_source_root(path: str) -> bool:
    """True when a dirty path repeats a source root such as rtl/rtl/foo.sv."""
    parts = [part for part in path.replace("\\", "/").split("/") if part]
    return len(parts) >= 2 and parts[0] in {"rtl", "fw"} and parts[1] == parts[0]


def _record_scorer_dirty_guardrail(
    ctx: TicketContext,
    state_path: Path,
    scorer_dirty: list[str],
    *,
    run_index: int,
) -> tuple[str, bool]:
    """Persist dirty scorer-file details and return a triage reason."""
    try:
        state = DevelopmentState.load(state_path)
        all_done = state.all_mandatory_met()
    except Exception:  # noqa: BLE001 — unreadable state degrades to "not done" so triage still proceeds
        all_done = False

    malformed_dirty = [path for path in scorer_dirty if _has_duplicated_source_root(path)]
    reason_kind = "DONE_BUT_DIRTY" if all_done else "DIRTY_SCORER_FILES"
    if malformed_dirty and not all_done:
        reason_kind = "MALFORMED_SCORER_OUTPUT"
    payload = {
        "kind": reason_kind,
        "slug": ctx.slug,
        "run_index": run_index,
        "all_mandatory_met": all_done,
        "scope": ctx.scope_raw,
        "dirty_files": scorer_dirty,
        "malformed_dirty_files": malformed_dirty,
    }
    report = ticket_runtime_file(ctx.logs_dir, "dirty_scorer_files.json")
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(_json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    if malformed_dirty:
        reason = (
            f"{reason_kind}: duplicated source root in scorer-consumed dirty file(s): "
            f"{', '.join(malformed_dirty[:5])}. Details: .runtime/{report.name}"
        )
    else:
        reason = (
            f"{reason_kind}: scorer-consumed file(s) still uncommitted after handoff: "
            f"{', '.join(scorer_dirty[:5])}. Details: .runtime/{report.name}"
        )
    return reason, all_done


def _record_malformed_rtl_guardrail(
    ctx: TicketContext,
    nested_files: list[str],
    *,
    run_index: int,
) -> str:
    """Persist malformed committed RTL-output details and return a block reason."""
    report = ticket_runtime_file(ctx.logs_dir, "malformed_rtl_output.json")
    report.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "kind": "MALFORMED_SCORER_OUTPUT",
        "slug": ctx.slug,
        "run_index": run_index,
        "scope": ctx.scope_raw,
        "nested_rtl_files": nested_files,
    }
    report.write_text(_json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return (
        "MALFORMED_SCORER_OUTPUT: nested RTL source root under rtl/rtl: "
        f"{', '.join(nested_files[:5])}. Details: .runtime/{report.name}"
    )


def _reset_worktree_if_dirty(ctx: TicketContext) -> None:
    """Reset worktree to last commit if dirty (uncommitted changes).

    Called once before the main loop on resume
    so each step starts from a clean, committed state.
    """
    wt = ctx.worktree_path
    if not _is_safe_worktree(ctx):
        logger.warning(
            "Skipping worktree reset -- not in an execution worktree (worktree=%s, project=%s)",
            wt,
            ctx.project_root,
        )
        return

    status = git_run(wt, ["status", "--porcelain", "--ignore-submodules"])
    if status.returncode != 0:
        raise RuntimeError(
            f"git status failed in worktree {wt} (rc={status.returncode}): "
            f"{(status.stderr or status.stdout).strip()}"
        )
    if not status.stdout.strip():
        logger.debug("Worktree clean -- no reset needed")
        return

    dirty_lines = status.stdout.strip().split("\n")
    logger.info("Resetting worktree (%d dirty entries)", len(dirty_lines))
    for line in dirty_lines[:10]:
        logger.debug("  dirty: %s", line)
    if len(dirty_lines) > 10:
        logger.debug("  ... and %d more", len(dirty_lines) - 10)

    _snapshot_dirty_worktree(ctx, status.stdout)

    reset_r = git_run(wt, ["reset", "--hard", "HEAD"])
    if reset_r.returncode != 0:
        raise RuntimeError(f"git reset --hard failed: {reset_r.stderr.strip()}")

    clean_r = git_run(wt, ["clean", "-fd"])
    if clean_r.returncode != 0:
        raise RuntimeError(f"git clean -fd failed: {clean_r.stderr.strip()}")

    logger.debug("Worktree reset to HEAD complete")


def _snapshot_dirty_worktree(ctx: TicketContext, porcelain: str) -> Path:
    """Persist dirty state before destructive cleanup."""
    wt = ctx.worktree_path
    if wt is None:
        raise RuntimeError("cannot snapshot dirty worktree without worktree_path")
    stamp = f"{compact_utc_now()}-{os.getpid()}"
    snapshot_dir = (
        ctx.project_root
        / ".booley_project"
        / "tickets"
        / "logs"
        / ctx.slug
        / ".runtime"
        / "dirty-snapshots"
        / stamp
    )
    snapshot_dir.mkdir(parents=True, exist_ok=False)
    (snapshot_dir / "status.txt").write_text(porcelain, encoding="utf-8")

    diff = git_run(wt, ["diff", "--binary", "HEAD"], timeout=30)
    if diff.returncode != 0:
        raise RuntimeError(f"git diff snapshot failed: {diff.stderr.strip()}")
    (snapshot_dir / "tracked.patch").write_text(diff.stdout, encoding="utf-8")

    for line in porcelain.splitlines():
        if not line.startswith("?? "):
            continue
        rel = line[3:].strip()
        src = wt / rel
        dst = snapshot_dir / "untracked" / rel
        if src.is_file():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
        elif src.is_dir():
            shutil.copytree(src, dst, symlinks=True, dirs_exist_ok=True)
    logger.warning("Saved dirty worktree snapshot before reset: %s", snapshot_dir)
    return snapshot_dir


def _ensure_worktree_populated(work_dir: Path) -> None:
    """Ensure the worktree has source files checked out.

    After a prune/crash the worktree directory may exist (with .git pointer)
    but have no working tree content.  Detect this and checkout from HEAD.
    """
    if not work_dir.is_dir():
        logger.warning("Worktree directory does not exist: %s", work_dir)
        return

    # Quick heuristic: if fewer than 3 entries (just .git or empty), populate
    entries = list(work_dir.iterdir())
    non_git = [e for e in entries if e.name != ".git"]
    if non_git:
        return  # Already has content

    git_pointer = work_dir / ".git"
    if not git_pointer.exists():
        logger.warning("Worktree %s has no .git pointer — cannot populate", work_dir)
        return

    logger.info("Worktree %s appears empty — running git checkout HEAD", work_dir)
    r = git_run(work_dir, ["checkout", "HEAD", "--", "."], timeout=60)
    if r.returncode != 0:
        logger.error("git checkout HEAD -- . failed (rc=%d): %s", r.returncode, r.stderr.strip())
    else:
        logger.info("Worktree populated (%d files)", len(list(work_dir.iterdir())) - 1)


# ---------------------------------------------------------------------------
# Criteria-based developer path (Phase 5)
# ---------------------------------------------------------------------------


def _developer_run_key(transcript: Path) -> str:
    """Collapse -retryN transcript variants onto their parent run's identity."""
    stem = transcript.stem
    if "-retry" in stem:
        stem = stem[: stem.index("-retry")]
    return stem


def _detect_crash_recovery(logs_dir: Path) -> tuple[bool, Path | None, int, Path]:
    """Detect prior runs and return (is_recovery, crash_transcript, run_index, transcript_path)."""
    transcript_dir = ticket_runtime_dir(logs_dir) / "developer"
    transcript_dir.mkdir(parents=True, exist_ok=True)
    # Only developer transcripts count as prior runs: the rotated naming
    # (developer.run_NNN[-retryN].jsonl), legacy flat naming (developer.jsonl,
    # developer-retryN.jsonl), and legacy bare run_NNN.jsonl. Specialists may
    # write other labels into this tree; those must not inflate the run index.
    prior_transcripts = sorted(
        set(transcript_dir.glob("developer*.jsonl")) | set(transcript_dir.glob("run_*.jsonl"))
    )
    is_crash_recovery = len(prior_transcripts) > 0
    # Most recent by mtime (name as deterministic tie-break) is the crash site.
    crash_transcript = (
        max(prior_transcripts, key=lambda p: (p.stat().st_mtime, p.name))
        if prior_transcripts
        else None
    )
    # -retryN variants belong to the run that spawned them: one run, not two.
    run_index = len({_developer_run_key(p) for p in prior_transcripts}) + 1
    transcript_path = transcript_dir / f"run_{run_index:03d}.jsonl"
    if is_crash_recovery:
        logger.info(
            "Crash recovery: resuming from run %d (prior transcript: %s)",
            run_index,
            crash_transcript,
        )
    return is_crash_recovery, crash_transcript, run_index, transcript_path


@dataclass(frozen=True)
class DiscoveredMcpSurface:
    """Result of MCP endpoint discovery, exposure, and environment plumbing."""

    discovered_mcp_tools: list
    mcp_tool_names: list[str]
    mcp_tool_config: dict
    flow_config: dict
    booley_src: Path
    project_mcp_tools_dir: Path


async def _discover_mcp_surface(
    project_root: Path,
    ctx: TicketContext,
) -> DiscoveredMcpSurface:
    """Discover the MCP endpoints exposed to this ticket."""
    booley_src = Path(__file__).resolve().parent.parent
    project_mcp_tools_dir = project_root / ".booley_project" / "mcp_tools"
    mcp_tool_config, flow_config = _load_endpoint_config(project_root)

    from booley.mcp.registry import discover_mcp_tools

    discovered_mcp_tools = discover_mcp_tools(
        booley_src=booley_src,
        project_mcp_tools_dir=project_mcp_tools_dir,
        mcp_tool_config=mcp_tool_config,
        flow_config=flow_config,
    )
    mcp_tool_names = [t.name for t in discovered_mcp_tools]
    _validate_required_endpoints_available(ctx.criteria, mcp_tool_names)
    if "sim" in mcp_tool_names and "bwave" not in mcp_tool_names:
        # B-Wave is hand-registered by mcp_server.py rather than discovered
        # from a Python endpoint module because its subcommand surface is not schema-extractable.
        # Keep it visible to autonomous Developer Agents whenever simulation is
        # visible, otherwise explicit BOOLEY_MCP_TOOLS filtering hides it.
        mcp_tool_names.append("bwave")

    if not mcp_tool_names:
        raise RuntimeError(
            "MCP tool discovery found zero endpoints — developer requires MCP tools. "
            "Check package installation and project MCP tool/Flow config."
        )
    return DiscoveredMcpSurface(
        discovered_mcp_tools=discovered_mcp_tools,
        mcp_tool_names=mcp_tool_names,
        mcp_tool_config=mcp_tool_config,
        flow_config=flow_config,
        booley_src=booley_src,
        project_mcp_tools_dir=project_mcp_tools_dir,
    )


def _validate_required_endpoints_available(criteria: dict, mcp_tool_names: list[str]) -> None:
    """Fail early when a ticket-contract Flow or Specialist was not discovered."""
    required = _builtin_endpoints_required_by_criteria(criteria) | {"submit_run_report"}
    missing = sorted(required - set(mcp_tool_names))
    if not missing:
        return
    raise RuntimeError(
        "Ticket run requires unavailable Booley Flow(s) or Specialist(s): "
        f"{', '.join(missing)}. Check [flows.<name>].enabled, "
        "[mcp_tools.<name>].enabled, and package installation."
    )


def _builtin_endpoints_required_by_criteria(
    criteria: dict,
) -> set[str]:
    """Return built-in Flow and Specialist names implied by criteria keys."""
    required: set[str] = set()
    for key in _iter_criteria_keys(criteria):
        if key.startswith("review_"):
            required.add("reviewer")
        elif key.startswith("mutation_score"):
            required.add("mutation_tester")
        if key.startswith("sim_"):
            required.add("sim")
        elif key.startswith("elab_"):
            required.add("elab")
        elif key.startswith("lint_"):
            required.add("lint")
        elif key.startswith("synthesis_"):
            required.add("synth")
    return required


def _iter_criteria_keys(criteria: dict) -> list[str]:
    """Collect criteria keys from the normalized ticket criteria structure."""
    keys: list[str] = []
    for section in ("mandatory", "optional"):
        values = criteria.get(section, {}) if isinstance(criteria, dict) else {}
        if isinstance(values, dict):
            keys.extend(str(k) for k in values)
    return keys


def _detect_backend_key() -> str:
    """Return 'codex' or 'claude' based on active backend config."""
    from booley.config.settings import get_backend_config as _get_cfg

    _cfg = _get_cfg()
    _backend_name = getattr(_cfg.active_backend, "name", "").lower()
    return "codex" if "codex" in _backend_name else "claude"


def _start_display_watcher(
    ctx: TicketContext,
) -> tuple[DisplayWatcher, Callable[[dict], None]]:
    """Build+start the display watcher and pick the matching event handler."""
    console_app = terminal.get_console_app()
    if console_app:
        line_counter = (
            WorktreeLineCounter(
                ctx.worktree_path,
                ctx.branch,
                reported_root=ctx.project_root,
            )
            if ctx.worktree_path
            else None
        )
        watcher = DisplayWatcher(
            existing_ticket_runtime_file(ctx.logs_dir, "display.jsonl"),
            poll_interval_s=0.15,
        )
        _wire_console_callbacks(watcher, console_app, line_counter)
        on_event = _make_console_event_handler(
            console_app,
            line_counter,
            watcher.endpoint_active,
        )
    else:
        watcher = DisplayWatcher(existing_ticket_runtime_file(ctx.logs_dir, "display.jsonl"))
        on_event = partial(agent_event_handler, endpoint_active=watcher.endpoint_active)

    watcher.start()

    # Push initial criteria to the console so they appear immediately
    if console_app:
        _push_initial_criteria(
            existing_ticket_runtime_file(ctx.logs_dir, "booley_state.json"),
            console_app,
        )
        if line_counter is not None:
            from .console.events import EditsChanged

            counts = line_counter.snapshot()
            if counts is not None:
                console_app.post_message(EditsChanged(*counts))
    return watcher, on_event


async def _invoke_developer_agent(
    ctx: TicketContext,
    project_root: Path,
    user_prompt: str,
    system_prompt: str,
    transcript_path: Path,
    mcp_tool_names: list[str],
    run_index: int,
    budget: DeveloperBudget,
) -> object | None:
    """Launch agent with display watcher; return result or None on failure."""
    from .colors import dim, green

    terminal.raw(f"  {dim('developer')} {green('launching')} {dim('(in-container)')}")
    _console_setup_msg("developer: launching agent (in-container)...")

    watcher, on_event = _start_display_watcher(ctx)
    budget.set_on_event(on_event)

    try:
        return await _launch_developer_agent(
            user_prompt,
            system_prompt=system_prompt,
            cwd=ctx.work_dir,
            slug=ctx.slug,
            ticket_type=ctx.ticket_type,
            transcript_path=transcript_path,
            state_path=ticket_runtime_file(ctx.logs_dir, "booley_state.json"),
            logs_dir=ctx.logs_dir,
            mcp_tools=mcp_tool_names,
            project_root=project_root,
            on_event=on_event,
            developer_budget=budget,
        )
    except ContextExhaustedError as e:
        logger.error("Context window exhausted (%s): %s", e.provider, e)
        record_crash(
            ctx.logs_dir,
            run_index=run_index,
            reason=f"Context window exhausted ({e.provider}): {e}",
        )
        recovered = _recover_if_criteria_met(
            ctx,
            run_index,
            crash_reason=f"Context window exhausted ({e.provider}): {e}",
        )
        if recovered is not None:
            return recovered
        fail_ticket(
            ctx,
            f"Context window exhausted ({e.provider}): {e}",
            "developer",
            run_index=run_index,
            crashed=True,
        )
        return None
    except asyncio.CancelledError as e:
        # Cancellation means the surrounding session stopped, not that the
        # Developer failed. Persist it as a retry-safe incident before
        # restoring cancellation to the caller. The ordinary run-finalizer
        # moves the temporary block back to queue, preserving commits and
        # criteria so a resume can finish submit_run_report.
        reason = f"Developer Agent cancelled: {type(e).__name__}: {e}"
        logger.warning("%s", reason)
        record_crash(ctx.logs_dir, run_index=run_index, reason=reason)
        fail_ticket(ctx, reason, "developer", run_index=run_index, crashed=True)
        raise
    except Exception as e:
        logger.error("Developer Agent failed: %s", e, exc_info=True)
        record_crash(
            ctx.logs_dir,
            run_index=run_index,
            reason=f"Developer Agent error: {type(e).__name__}: {e}",
        )
        recovered = _recover_if_criteria_met(
            ctx,
            run_index,
            crash_reason=f"Developer Agent error: {type(e).__name__}: {e}",
        )
        if recovered is not None:
            return recovered
        fail_ticket(
            ctx,
            f"Developer Agent error: {type(e).__name__}: {e}",
            "developer",
            run_index=run_index,
            crashed=True,
        )
        return None
    except BaseException as e:
        # Non-Exception termination must still honor the exit invariant. An
        # asyncio cancellation is handled separately above because it is safe
        # to resume; other BaseExceptions remain fatal.
        logger.error("Developer Agent killed by %s: %s", type(e).__name__, e, exc_info=True)
        record_crash(
            ctx.logs_dir,
            run_index=run_index,
            reason=f"Developer Agent killed: {type(e).__name__}: {e}",
        )
        fail_ticket(
            ctx,
            f"Developer Agent killed: {type(e).__name__}: {e}",
            "developer",
            run_index=run_index,
            crashed=True,
        )
        raise
    finally:
        budget.stop_active()
        watcher.stop()


def _recover_if_criteria_met(
    ctx: TicketContext,
    run_index: int,
    *,
    crash_reason: str,
) -> AgentResult | None:
    """If state shows all mandatory criteria met, return a stub result so the
    caller continues to post-guardrails / post-hook / disposition instead of
    failing the ticket. Returns None when no recovery is possible.

    Motivated by parser/streaming crashes after the agent has already driven
    every MCP tool call to completion — losing the post-hook there means a fully
    completed ticket sits in blocked/ for no reason.
    """
    from .colors import yellow

    state_path = existing_ticket_runtime_file(ctx.logs_dir, "booley_state.json")
    if not state_path.exists():
        return None
    try:
        state = DevelopmentState.load(state_path)
    except Exception as exc:  # noqa: BLE001 — unreadable state aborts crash recovery; caller falls through safely
        logger.warning("Could not load state for crash recovery: %s", exc)
        return None
    # all_mandatory_met is a METHOD — it must be called. (A getattr on the name
    # returns the bound method, which is always truthy, turning every crash into
    # a bogus "criteria met" recovery.) An empty criteria dict means the agent
    # crashed before doing any work, so there is nothing to recover either:
    # all() over nothing would be vacuously true.
    if not state.criteria or not state.all_mandatory_met():
        return None

    logger.warning(
        "Developer Agent crashed but criteria are met — recovering (slug=%s, reason=%s)",
        ctx.slug,
        crash_reason,
    )
    terminal.raw(
        f"  {yellow('[RECOVER]')} agent crashed but criteria met — continuing to post-hook"
    )
    return AgentResult()


def _record_agent_result(result: object, state_path: Path, ctx: TicketContext) -> None:
    """Record developer cost and log timeout/max-turns warnings."""
    from .colors import bold_red, yellow

    orch_state = DevelopmentState.load(state_path)
    orch_state.record_mcp_tool_run("developer_agent", 0, cost_usd=result.cost_usd)
    orch_state.save()

    if result.timed_out:
        logger.error("Developer Agent timed out for %s", ctx.slug)
        terminal.raw(f"  {bold_red('[TIMEOUT]')} developer timed out")
    if result.max_turns_exhausted:
        logger.warning("Developer Agent hit max turns (200) for %s", ctx.slug)
        terminal.raw(f"  {yellow('[MAX_TURNS]')} developer exhausted 200 turns")


def _guard_scorer_restore_artifacts(
    ctx: TicketContext,
    state_path: Path,
    scorer_artifacts: list[str],
    run_index: int,
) -> bool:
    """Block on scorer-consumed files deleted under a `` [new]`` glob. True => block."""
    from .colors import yellow

    reason, all_done = _record_scorer_dirty_guardrail(
        ctx,
        state_path,
        scorer_artifacts,
        run_index=run_index,
    )
    block_ticket(ctx, reason, "developer", run_index=run_index)
    label = "[DONE_BUT_DIRTY]" if all_done else "[BLOCK]"
    terminal.raw(f"  {yellow(label)} scorer files deleted but not committable")
    return True


def _check_ticket_dirty_statuses(worktree: Path) -> list[DirtyFile]:
    """Return dirty paths from the outer and paired project repositories."""
    from booley.runtime.ticket_repositories import ticket_repositories

    from .developer_guardrails import DirtyFile, check_uncommitted_code_statuses

    dirty: list[DirtyFile] = []
    for repository in ticket_repositories(worktree):
        dirty.extend(
            DirtyFile(repository.ticket_path(entry.path), entry.status)
            for entry in check_uncommitted_code_statuses(repository.worktree)
        )
    return dirty


def _commit_ticket_paths(ctx: TicketContext, paths: list[str], message: str) -> None:
    """Commit authorized paths in each repository, attempting every repository."""
    from booley.runtime.git import commit_scope
    from booley.runtime.ticket_repositories import ticket_repositories

    from .blocking import BlockingError

    assert ctx.worktree_path is not None
    repositories = ticket_repositories(ctx.worktree_path)
    project_prefixes = {repo.path_prefix for repo in repositories if repo.path_prefix}
    failures: list[str] = []
    for repository in repositories:
        if repository.path_prefix:
            selected = [path for path in paths if path.startswith(f"{repository.path_prefix}/")]
        else:
            selected = [
                path
                for path in paths
                if not any(path.startswith(f"{prefix}/") for prefix in project_prefixes)
            ]
        if not selected:
            continue
        local_paths = [repository.local_path(path) for path in selected]
        try:
            commit_scope(repository.worktree, local_paths, message, literal=True)
        except BlockingError as exc:
            label = repository.path_prefix or "outer repository"
            failures.append(f"{label}: {exc}")
    if failures:
        raise BlockingError("; ".join(failures))


def _run_post_guardrails(
    ctx: TicketContext,
    state_path: Path,
    run_index: int,
) -> bool:
    """Run post-developer guardrails. Returns True if ticket was blocked."""
    from .colors import yellow
    from .developer_guardrails import (
        GitStatusError,
    )
    from .scope_policy import ScopeTier, classify_path, is_restore_artifact

    # Leftover uncommitted edits are fine to have, but review/merge only sees
    # committed branch history, so commit them before handoff.
    if ctx.worktree_path:
        try:
            dirty = _check_ticket_dirty_statuses(ctx.worktree_path)
        except GitStatusError as exc:
            logger.warning("Cannot inspect uncommitted edits for %s: %s", ctx.slug, exc)
            block_ticket(
                ctx,
                f"Cannot inspect uncommitted edits before handoff: {exc}",
                "developer",
                run_index=run_index,
            )
            terminal.raw(f"  {yellow('[BLOCK]')} cannot inspect uncommitted edits")
            # Triage is told to read the deviation report; leaving none behind
            # on the block path is exactly when a missing file is most confusing.
            _report_scope_deviations(ctx)
            return True
        # A deletion under a `` [new]`` glob is worktree fallout, not work, so it
        # is dropped before tiering rather than committed. Dropping one the
        # scorer reads is a different matter: the criteria were measured on a
        # tree missing that file and the branch still carries it, so the result
        # is not reproducible and the ticket must stop.
        artifacts = [e.path for e in dirty if is_restore_artifact(ctx.scope_raw, e.path, e.status)]
        scorer_artifacts = [path for path in artifacts if _is_scorer_consumed_path(path)]
        if scorer_artifacts and _guard_scorer_restore_artifacts(
            ctx, state_path, scorer_artifacts, run_index
        ):
            return True
        if artifacts:
            logger.info(
                "Ignoring %d worktree restore artifact(s): %s",
                len(artifacts),
                ", ".join(artifacts[:5]),
            )
        tiers = [
            (entry.path, classify_path(ctx.scope_raw, entry.path, entry.status))
            for entry in dirty
            if entry.path not in artifacts
        ]
        committable = [path for path, tier in tiers if tier is ScopeTier.OWNED]
        advisory = [path for path, tier in tiers if tier is ScopeTier.ADVISORY]
        forbidden = [path for path, tier in tiers if tier is ScopeTier.FORBIDDEN]
        if forbidden:
            # Deliberately left uncommitted rather than blocking: the pre-commit
            # hook already rejects any intentional attempt to stage these, and a
            # stray dirty bookkeeping file should not fail an otherwise good run.
            logger.warning(
                "Leaving %d harness-owned dirty file(s) uncommitted: %s",
                len(forbidden),
                ", ".join(forbidden[:5]),
            )
        if advisory:
            logger.warning(
                "Leaving %d out-of-scope file(s) uncommitted for triage: %s",
                len(advisory),
                ", ".join(advisory[:5]),
            )
            terminal.raw(
                f"  {yellow('[WARN]')} leaving {len(advisory)} out-of-scope "
                f"file(s) uncommitted for triage: {', '.join(advisory[:5])}"
            )
        if committable and _commit_leftover_edits(ctx, committable, state_path, run_index):
            _report_scope_deviations(ctx)
            return True

        _report_scope_deviations(ctx)

        # Malformed output is a property of the tree, not of the Scope, so the
        # nested-`rtl/rtl/` check runs on every ticket. Gating it on
        # `_scope_expects_rtl_output` would let a verification ticket leave
        # malformed `rtl/rtl/dut.sv` output unremarked.
        if _guard_malformed_rtl_output(ctx, run_index):
            return True

        if _scope_expects_rtl_output(ctx.scope_raw) and _guard_live_rtl_output(
            ctx,
            run_index,
        ):
            return True

    return False


def _report_scope_deviations(ctx: TicketContext) -> None:
    """Record which committed files the ticket's Scope did not name.

    Purely informational: triage reads the report, the run never changes
    disposition because of it. Runs against the finished branch rather than
    the dirty tree, because the branch is what review and merge see.
    """
    from .colors import yellow
    from .scope_policy import DEVIATION_REPORT_NAME, committed_deviations, write_deviation_report

    base_ref = ctx.target_contract.outer_sha if ctx.target_contract is not None else ctx.branch
    result = committed_deviations(ctx.worktree_path, base_ref, ctx.scope_raw)
    write_deviation_report(
        ticket_runtime_file(ctx.logs_dir, DEVIATION_REPORT_NAME),
        slug=ctx.slug,
        base_branch=base_ref,
        scope=ctx.scope_raw,
        result=result,
    )
    deviations = result[0] if result else []
    if deviations:
        logger.info(
            "Ticket %s committed %d file(s) outside its Scope: %s",
            ctx.slug,
            len(deviations),
            ", ".join(deviations[:5]),
        )
        terminal.raw(f"  {yellow('SCOPE')} {len(deviations)} file(s) outside ticket scope")


def _guard_malformed_rtl_output(ctx: TicketContext, run_index: int) -> bool:
    """Block on nested ``rtl/rtl/`` output anywhere in the worktree. True => block."""
    from .colors import yellow

    nested_rtl = _nested_rtl_output_files(ctx.worktree_path)
    if not nested_rtl:
        return False
    reason = _record_malformed_rtl_guardrail(ctx, nested_rtl, run_index=run_index)
    block_ticket(ctx, reason, "developer", run_index=run_index)
    terminal.raw(f"  {yellow('[BLOCK]')} malformed nested RTL output")
    return True


def _guard_live_rtl_output(ctx: TicketContext, run_index: int) -> bool:
    """Verify live RTL output exists after handoff. True => block.

    Malformed nesting is checked separately by :func:`_guard_malformed_rtl_output`,
    which is not gated on Scope.
    """
    from .colors import yellow

    status = git_run(ctx.worktree_path, ["status", "--porcelain", "--ignore-submodules"])
    if status.returncode != 0:
        block_ticket(
            ctx,
            "Cannot verify live RTL output because git status failed: "
            f"{(status.stderr or status.stdout).strip()}",
            "developer",
            run_index=run_index,
        )
        terminal.raw(f"  {yellow('[BLOCK]')} cannot verify live RTL output")
        return True
    if not _has_live_rtl_output(ctx.worktree_path):
        block_ticket(
            ctx,
            "No live RTL output found under active worktree rtl/**/*.sv|*.v at handoff.",
            "developer",
            run_index=run_index,
        )
        terminal.raw(f"  {yellow('[BLOCK]')} no live RTL output")
        return True
    return False


# Ticket type -> conventional-commit type. Anything unrecognised is housekeeping.
_TICKET_TYPE_TO_COMMIT_TYPE = {
    "feature": "feat",
    "bugfix": "fix",
    "refactor": "refactor",
    "verification": "test",
}

# Fallback used when the composed message trips commit-message validation
# (banned terms in a ticket title, an odd slug, a repo-specific body cap).
# Blocking a finished ticket over its own commit subject would be absurd.
_LEFTOVER_FALLBACK_MESSAGE = "fix: commit leftover edits"

# Conservative slug shape: the commit-message scope grammar allows only these.
_COMMIT_SCOPE_RE = re.compile(r"^[a-zA-Z0-9_-]+$")


def _leftover_commit_message(ctx: TicketContext, committable: list[str]) -> str:
    """Compose an informative subject for the terminal leftover-edits commit.

    The Developer Agent does not commit during a run — code-modifying Specialists
    commit on its behalf — so anything it edited with its own file operations lands
    in this one catch-all commit at handoff. "fix: commit leftover edits" told
    a reviewer nothing about what it contains (F-52), so name the ticket and
    how much it swept up. The file names themselves are already in the commit.

    Falls back to the old fixed message if the composed one fails validation.
    """
    from booley.dev_support.validate_commit_msg import MAX_SUMMARY_LEN, validate_message

    commit_type = _TICKET_TYPE_TO_COMMIT_TYPE.get(ctx.ticket_type, "chore")
    scope = f"({ctx.slug})" if _COMMIT_SCOPE_RE.match(ctx.slug or "") else ""

    title = (ctx.summary or ctx.slug or "ticket").strip().replace("\n", " ")
    count = f" ({len(committable)} file{'s' if len(committable) != 1 else ''})"
    budget = MAX_SUMMARY_LEN - len(count)
    if len(title) > budget:
        title = title[: budget - 1].rstrip() + "…"

    message = f"{commit_type}{scope}: {title}{count}"
    if validate_message(message):
        logger.warning(
            "Composed leftover-edit subject failed validation, using the generic one: %r",
            message,
        )
        return _LEFTOVER_FALLBACK_MESSAGE
    return message


def _commit_leftover_edits(
    ctx: TicketContext,
    committable: list[str],
    state_path: Path,
    run_index: int,
) -> bool:
    """Commit leftover edits and verify none remain. True => block.

    *committable* contains only paths authorized by the ticket Scope. Other
    dirty paths remain in the worktree for explicit triage.
    """
    from .blocking import BlockingError
    from .colors import yellow
    from .developer_guardrails import (
        GitStatusError,
    )
    from .scope_policy import ScopeTier, classify_path

    logger.warning(
        "Committing %d leftover edited file(s): %s", len(committable), ", ".join(committable[:5])
    )
    terminal.raw(f"  {yellow('LEFTOVER EDITS')} committing {len(committable)} file(s)")
    try:
        _commit_ticket_paths(ctx, committable, _leftover_commit_message(ctx, committable))
    except BlockingError as exc:
        logger.warning("Leftover-edit commit failed for %s: %s", ctx.slug, exc)
        block_ticket(
            ctx,
            f"Leftover edits could not be committed: {exc}",
            "developer",
            run_index=run_index,
        )
        terminal.raw(f"  {yellow('[BLOCK]')} leftover edits could not be committed")
        return True

    try:
        remaining = _check_ticket_dirty_statuses(ctx.worktree_path)
    except GitStatusError as exc:
        logger.warning("Cannot recheck leftover edits for %s: %s", ctx.slug, exc)
        block_ticket(
            ctx,
            f"Cannot recheck leftover edits after scoped commit: {exc}",
            "developer",
            run_index=run_index,
        )
        terminal.raw(f"  {yellow('[BLOCK]')} cannot recheck leftover edits")
        return True
    # Only authorized paths were meant to be committed. Out-of-scope and
    # harness-owned leftovers are intentionally preserved for triage.
    still_dirty = [
        entry.path
        for entry in remaining
        if classify_path(ctx.scope_raw, entry.path, entry.status) is ScopeTier.OWNED
    ]
    if not still_dirty:
        return False

    logger.warning(
        "Scoped commit left %d uncommitted file(s): %s",
        len(still_dirty),
        ", ".join(still_dirty[:5]),
    )
    # An authorized scorer path still being dirty means the scoped commit did
    # not take. Preserve the existing detailed dirty-output diagnosis.
    scorer_dirty = [path for path in still_dirty if _is_scorer_consumed_path(path)]
    if scorer_dirty:
        reason, all_done = _record_scorer_dirty_guardrail(
            ctx,
            state_path,
            scorer_dirty,
            run_index=run_index,
        )
        block_ticket(ctx, reason, "developer", run_index=run_index)
        label = "[DONE_BUT_DIRTY]" if all_done else "[BLOCK]"
        terminal.raw(f"  {yellow(label)} scorer files dirty after commit")
        return True

    block_ticket(
        ctx,
        f"Uncommitted files remain after scoped commit: {', '.join(still_dirty[:5])}",
        "developer",
        run_index=run_index,
    )
    terminal.raw(f"  {yellow('[BLOCK]')} uncommitted edits remain")
    return True


async def _prepare_review_handoff(
    ctx: TicketContext,
    project_root: Path,
    run_index: int,
) -> bool:
    """Prepare enabled review artifacts; block and return False on failure."""
    from .colors import dim, green, yellow
    from .review_prep import prepare_review

    _write_status(ctx.logs_dir, ctx.slug, "post-processing")
    _console_setup_msg("post-processing: preparing review artifacts...")
    _console_activity("post-processing")
    terminal.raw(f"  {green('all criteria met')} {dim('→ post-processing')}")
    try:
        outcome = await prepare_review(project_root, ctx.slug)
    finally:
        _console_activity("")
    if outcome.ready:
        detail = str(outcome.html_path) if outcome.html_path is not None else outcome.message
        terminal.raw(f"  {green('review artifacts ready')} {dim(detail)}")
        return True

    reason = f"Review post-processing did not complete: {outcome.message}"
    logger.warning("Review post-processing failed for %s: %s", ctx.slug, outcome.message)
    block_ticket(ctx, reason, "post-processing", run_index=run_index)
    terminal.raw(f"  {yellow('[BLOCK]')} review post-processing failed")
    return False


async def _resolve_ticket_disposition(
    ctx: TicketContext,
    state_path: Path,
    project_root: Path,
    run_index: int,
) -> None:
    """Read final state, check criteria acceptance, and transition the ticket."""
    if _block_changed_target_contract(ctx, run_index):
        return
    from booley.ticket_board.criteria_acceptance import (
        build_criteria_summary_lines,
        check_criteria_acceptance,
    )

    from .colors import bold_red, dim, green, yellow

    verdict = check_criteria_acceptance(state_path, work_dir=ctx.work_dir)
    logger.info("Criteria verdict for %s: %s", ctx.slug, verdict.disposition)

    # Print per-criterion summary table before the verdict line
    crit_lines, totals_line = build_criteria_summary_lines(state_path)
    if crit_lines:
        terminal.criteria_summary(crit_lines, totals_line)

    # A fail->pass criterion met without a recorded failure proved less than it
    # promised. Not a blocker, but the reviewer must not have to infer it (F-53).
    transition_note = verdict.unverified_transitions_note()
    if transition_note:
        terminal.raw(f"  {yellow('[WARN]')} {transition_note}")

    # The harness MUST NOT auto-archive (delete) tickets. Failed-criteria
    # tickets land in blocked/ for human triage; archive is a human-only
    # operation (see booley.ticket_board.archive.op_archive).
    if verdict.disposition == "blocked":
        block_ticket(ctx, verdict.blocked_reason, "developer", run_index=run_index)
        terminal.raw(f"  {yellow('[BLOCK]')} {verdict.blocked_reason}")
    elif verdict.disposition == "review":
        logger.info("All mandatory criteria met for %s", ctx.slug)
        if ctx.on_success.destination == "review" and ctx.on_success.triage_report:
            if not await _prepare_review_handoff(ctx, project_root, run_index):
                return
            terminal.raw(f"  {green('post-processing complete')} {dim('→ review')}")
        else:
            terminal.raw(f"  {green('all criteria met')} {dim('→ review')}")
        if ctx.on_success.destination == "review" and ctx.on_success.triage_report:
            from .review_prep import ReviewPrepError, verify_review_handoff

            try:
                verify_review_handoff(project_root, ctx.slug)
            except ReviewPrepError as exc:
                reason = f"Review package changed before handoff: {exc}"
                logger.warning("Review handoff verification failed for %s: %s", ctx.slug, exc)
                block_ticket(ctx, reason, "post-processing", run_index=run_index)
                terminal.raw(f"  {yellow('[BLOCK]')} review package verification failed")
                return
        ownership = {"expected_execution_id": ctx.execution_id} if ctx.execution_id else {}
        ticket_cli.handoff(project_root, ctx.slug, **ownership)
    elif verdict.disposition == "failed":
        fail_ticket(
            ctx,
            f"Developer Agent exited with {len(verdict.unmet_mandatory)} unmet criteria: "
            f"{', '.join(verdict.unmet_mandatory[:5])}",
            "developer",
            run_index=run_index,
        )
        terminal.raw(f"  {bold_red('[FAIL]')} {len(verdict.unmet_mandatory)} unmet criteria")
    else:
        raise ValueError(
            f"Unknown criteria verdict disposition {verdict.disposition!r} for "
            f"{ctx.slug} — expected one of: review, blocked, failed"
        )


def _block_changed_target_contract(ctx: TicketContext, run_index: int) -> bool:
    """Fail closed before review handoff when the sealed surface has changed."""
    contract = ctx.target_contract
    if contract is None:
        logger.warning("Legacy ticket %s reaches handoff without a Target contract", ctx.slug)
        return False
    from booley.ticket_board.target_contract import (
        CONTRACT_BLOCK_REASON,
        TargetContractError,
        verify_surface,
    )

    try:
        verify_surface(contract, ctx.work_dir)
    except (OSError, TargetContractError) as exc:
        reason = f"{CONTRACT_BLOCK_REASON}: {exc}"
        block_ticket(ctx, reason, "developer", run_index=run_index)
        return True
    return False


def _build_prompt_context(
    ctx: TicketContext,
    state_path: Path,
    discovered_mcp_tools: list,
    mcp_tool_config: dict,
    flow_config: dict,
    booley_src: str,
    project_mcp_tools_dir: str | None,
    is_crash_recovery: bool,
    crash_transcript: Path | None,
    crash_summary: Path | None,
) -> tuple[str, str]:
    """Build developer system + user prompts from context."""
    from booley.config.project_config import is_human_in_loop, is_run_report_enabled

    from .developer_prompt import DeveloperPromptContext, build_developer_prompt

    return build_developer_prompt(
        DeveloperPromptContext(
            ticket_path=ctx.ticket_path,
            state_path=state_path,
            logs_dir=ctx.logs_dir,
            slug=ctx.slug,
            ticket_type=ctx.ticket_type,
            criteria=ctx.criteria,
            mcp_tools=discovered_mcp_tools,
            mcp_tool_config=mcp_tool_config,
            flow_config=flow_config,
            booley_src=booley_src,
            project_mcp_tools_dir=project_mcp_tools_dir,
            is_crash_recovery=is_crash_recovery,
            crash_summary_path=crash_summary,
            work_dir=str(ctx.work_dir),
            backend=_detect_backend_key(),
            human_in_the_loop=is_human_in_loop(),
            run_report=is_run_report_enabled(),
        )
    )


def _write_developer_prompt_snapshot(
    ctx: TicketContext,
    *,
    run_index: int,
    transcript_path: Path,
    crash_transcript: Path | None,
    system_prompt: str,
    user_prompt: str,
    mcp_tool_names: list[str],
) -> None:
    """Persist the exact developer prompt before launching the agent."""
    from booley.config.settings import get_backend_config

    cfg = get_backend_config()
    attempt = os.environ.get("BOOLEY_ORACLE_FEEDBACK_ATTEMPT")
    feedback_path: Path | None = None
    if attempt and attempt.isdigit() and int(attempt) > 1:
        feedback_path = (
            ctx.logs_dir / "oracle_feedback" / f"attempt_{int(attempt) - 1}_feedback.md"
        )

    write_prompt_artifacts(
        transcript_path,
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        metadata={
            "backend": _detect_backend_key(),
            "label": "developer",
            "model": cfg.model_for_role("developer", "heavy"),
            "reasoning_effort": cfg.effort_for_tier("heavy"),
            "run_index": run_index,
            "slug": ctx.slug,
            "ticket_type": ctx.ticket_type,
            "work_dir": str(ctx.work_dir),
            "state_path": str(ticket_runtime_file(ctx.logs_dir, "booley_state.json")),
            "transcript_path": str(transcript_path),
            "crash_transcript_path": str(crash_transcript) if crash_transcript else "",
            "oracle_feedback": os.environ.get("BOOLEY_ORACLE_FEEDBACK"),
            "oracle_feedback_attempt": attempt,
            "oracle_feedback_max_attempts": os.environ.get("BOOLEY_ORACLE_FEEDBACK_MAX_ATTEMPTS"),
            "oracle_feedback_label": os.environ.get("BOOLEY_ORACLE_FEEDBACK_LABEL"),
            "oracle_feedback_path": str(feedback_path) if feedback_path else "",
            "blocked_path": str(ctx.logs_dir / "blocked.md"),
            "mcp_tools": ",".join(mcp_tool_names),
        },
    )


async def _run_developer_path(
    ctx: TicketContext,
    project_root: Path,
) -> None:
    """Run the developer agent for criteria-based tickets.

    Flow: detect crash recovery -> build prompt -> launch agent ->
    guardrails -> hook -> ticket disposition.
    """
    set_current_step("developer")
    _write_status(ctx.logs_dir, ctx.slug, "developer")
    state_path = migrate_runtime_file(ctx.logs_dir, "booley_state.json")
    run_index = 0
    budget: DeveloperBudget | None = None

    try:
        is_crash_recovery, crash_transcript, run_index, transcript_path = _detect_crash_recovery(
            ctx.logs_dir
        )

        # Distill the crash transcript into a bounded summary and point the
        # recovery prompt at it; the raw JSONL embeds the full prior context
        # and rereading it compounds token cost across retries.
        crash_summary: Path | None = None
        if is_crash_recovery and crash_transcript is not None:
            from .transcript_distill import write_distilled_summary

            crash_summary = write_distilled_summary(crash_transcript)

        mcp_surface = await _discover_mcp_surface(project_root, ctx)

        system_prompt, user_prompt = _build_prompt_context(
            ctx,
            state_path,
            mcp_surface.discovered_mcp_tools,
            mcp_surface.mcp_tool_config,
            mcp_surface.flow_config,
            mcp_surface.booley_src,
            mcp_surface.project_mcp_tools_dir,
            is_crash_recovery,
            crash_transcript,
            crash_summary,
        )
        _write_developer_prompt_snapshot(
            ctx,
            run_index=run_index,
            transcript_path=transcript_path,
            crash_transcript=crash_transcript,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            mcp_tool_names=mcp_surface.mcp_tool_names,
        )

        _ensure_worktree_populated(ctx.work_dir)

        from booley.config.settings import load_developer_limits_config

        budget = DeveloperBudget(
            load_developer_limits_config(project_root),
            persist_path=ticket_runtime_file(ctx.logs_dir, "developer_budget.json"),
        )
        budget.start(run_index)

        result = await _invoke_developer_agent(
            ctx,
            project_root,
            user_prompt,
            system_prompt,
            transcript_path,
            mcp_surface.mcp_tool_names,
            run_index,
            budget,
        )
        if result is None:
            return

        await _drain_outstanding_ticket_jobs(ctx, budget)

        _record_agent_result(result, state_path, ctx)
        budget.raise_if_exhausted()

        guardrail_blocked = _run_post_guardrails(ctx, state_path, run_index)
        budget.raise_if_exhausted()
        if guardrail_blocked:
            return

        hook_blocked = _run_post_developer_hook(
            ctx,
            state_path,
            ctx.logs_dir,
            run_index=run_index,
            budget=budget,
        )
        budget.raise_if_exhausted()
        if hook_blocked:
            return
        await run_with_developer_budget(
            _resolve_ticket_disposition(ctx, state_path, project_root, run_index),
            budget,
        )
    except Exception as e:
        logger.error("Developer Agent path failed before/after agent: %s", e, exc_info=True)
        record_crash(
            ctx.logs_dir,
            run_index=run_index,
            reason=f"Developer Agent path error: {type(e).__name__}: {e}",
        )
        fail_ticket(
            ctx,
            f"Developer Agent path error: {type(e).__name__}: {e}",
            "developer",
            run_index=run_index,
            crashed=True,
        )
    finally:
        if budget is not None:
            budget.finish()
        # Runs after every exit path — including the early returns for
        # guardrail/hook blocks — so a transient crash gets its retry no
        # matter which disposition the run finally landed on.
        retried = maybe_auto_retry(ctx, project_root, run_index)
        if not retried:
            await _prepare_blocked_triage(ctx, project_root)


async def _prepare_blocked_triage(ctx: TicketContext, project_root: Path) -> None:
    """Run best-effort blocked triage unless review post-processing caused the block."""
    if (
        ticket_cli.ticket_status(project_root, ctx.slug) != "blocked"
        or _status_step(ctx.logs_dir) == "post-processing"
    ):
        return
    from .blocked_prep import prepare_blocked_dossier

    _write_status(ctx.logs_dir, ctx.slug, "post-processing")
    outcome = await prepare_blocked_dossier(project_root, ctx.slug)
    if outcome.ready:
        logger.info("Blocked triage dossier ready for %s", ctx.slug)
    else:
        logger.warning("Blocked triage dossier unavailable for %s: %s", ctx.slug, outcome.message)


def _resolve_booley_project_dir(project_root: Path) -> Path:
    """Resolve the project data directory the way the Runner does everywhere.

    Precedence: explicit ``BOOLEY_PROJECT_DIR`` env (set by the devcontainer
    to the mounted ``/booley-project``), then the co-located
    ``.booley_project/``, then the legacy ``.booley/project`` fallback.
    """
    env_dir = os.environ.get("BOOLEY_PROJECT_DIR")
    if env_dir:
        return Path(env_dir).resolve()
    project_dir = project_root / ".booley_project"
    if not project_dir.is_dir():
        project_dir = project_root / ".booley" / "project"
    return project_dir


def _ticket_project_dir(ctx: TicketContext) -> Path:
    """Return ticket-authored project content, falling back to control-plane data."""
    if ctx.worktree_path is not None:
        from booley.runtime.ticket_repositories import paired_project_repository

        repository = paired_project_repository(ctx.worktree_path)
        if repository is not None:
            return repository.worktree
    return _resolve_booley_project_dir(ctx.project_root)


def _find_hook_script(ctx: TicketContext) -> Path | None:
    """Find post-developer hook script (.sh, .py, or bare) in project hooks dir."""
    hook_dir = _resolve_booley_project_dir(ctx.project_root) / "hooks"
    for suffix in (".sh", ".py", ""):
        candidate = hook_dir / f"post-developer{suffix}"
        if candidate.exists():
            return candidate
    logger.debug("No post-developer hook in %s", hook_dir)
    return None


def _build_hook_env(
    ctx: TicketContext,
    state_path: Path,
    logs_dir: Path,
) -> dict[str, str]:
    """Build environment dict for hook subprocess."""
    # Resolve current ticket location (may have moved between statuses)
    ticket_file = str(ctx.ticket_path)
    if not ctx.ticket_path.exists():
        board_dir = ctx.ticket_path.parent.parent
        for status in ("active", "queue", "waiting", "blocked", "review", "done", "archived"):
            candidate_path = board_dir / status / f"{ctx.slug}.md"
            if candidate_path.exists():
                ticket_file = str(candidate_path)
                break

    project_dir = _ticket_project_dir(ctx)

    return {
        **os.environ,
        "BOOLEY_WORKTREE": str(ctx.worktree_path or ""),
        "BOOLEY_PROJECT_DIR": str(project_dir),
        "BOOLEY_TICKET_SLUG": ctx.slug,
        "BOOLEY_TICKET_FILE": ticket_file,
        "BOOLEY_STATE_FILE": str(state_path),
        "BOOLEY_LOGS_DIR": str(logs_dir),
        "BOOLEY_RUNTIME_DIR": str(ticket_runtime_dir(logs_dir)),
    }


def _execute_hook(
    hook: Path,
    ctx: TicketContext,
    env: dict[str, str],
    run_index: int | None,
    budget: DeveloperBudget | None = None,
) -> bool:
    """Execute hook subprocess. Return True when it blocks the ticket."""
    from .colors import yellow

    timeout_seconds = 900.0
    wall_limited = False
    if budget is not None:
        budget.raise_if_exhausted()
        timeout_seconds = min(timeout_seconds, budget.remaining_wall_seconds())
        wall_limited = timeout_seconds < 900.0
    try:
        cmd = [bash_bin(), str(hook)] if hook.suffix == ".sh" else [sys.executable, str(hook)]
        result = subprocess.run(
            cmd,
            cwd=str(ctx.project_root),
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            env=env,
            check=False,
        )
        if result.stdout.strip():
            for line in result.stdout.strip().splitlines()[:20]:
                logger.info("  hook: %s", line)
        if result.returncode != 0:
            err_msg = result.stderr.strip() or f"exit code {result.returncode}"
            logger.warning("Post-developer hook failed: %s", err_msg)
            terminal.raw(f"  {yellow('[HOOK]')} post-developer failed: {err_msg}")
            block_ticket(ctx, f"post-developer hook: {err_msg}", "developer", run_index=run_index)
            return True
        logger.info("Post-developer hook completed successfully")
        return False
    except subprocess.TimeoutExpired as exc:
        if budget is not None and wall_limited:
            raise budget.timeout_error("wall") from exc
        logger.error("Post-developer hook timed out (900s)")
        terminal.raw(f"  {yellow('[HOOK]')} post-developer timed out")
        block_ticket(ctx, "post-developer hook timed out", "developer", run_index=run_index)
        return True
    except Exception as e:
        logger.error("Post-developer hook error: %s", e, exc_info=True)
        terminal.raw(f"  {yellow('[HOOK]')} error: {e}")
        block_ticket(ctx, f"post-developer hook error: {e}", "developer", run_index=run_index)
        return True


async def _drain_outstanding_ticket_jobs(
    ctx: TicketContext, budget: DeveloperBudget | None = None
) -> None:
    """Fence final state bookkeeping behind every detached MCP endpoint child."""
    from .job_fence import active_ticket_jobs, wait_for_ticket_jobs

    active = active_ticket_jobs(ctx.logs_dir)
    if not active:
        return
    names = ", ".join(rec.endpoint for rec in active)
    terminal.raw(f"  waiting for outstanding ticket jobs: {names}")
    wait = wait_for_ticket_jobs(ctx.logs_dir)
    if budget is None:
        await wait
        return
    await run_with_developer_budget(wait, budget)


def _run_post_developer_hook(
    ctx: TicketContext,
    state_path: Path,
    logs_dir: Path,
    run_index: int | None = None,
    budget: DeveloperBudget | None = None,
) -> bool:
    """Run .booley_project/hooks/post-developer.{sh,py} if it exists.

    Returns True when the hook blocks the ticket. Hook stdout/stderr is logged.
    """
    from .colors import dim

    hook = _find_hook_script(ctx)
    if hook is None:
        return False

    logger.info("Running post-developer hook: %s", hook)
    terminal.raw(f"  {dim('post-developer hook')} {hook.name}")

    env = _build_hook_env(ctx, state_path, logs_dir)
    return _execute_hook(hook, ctx, env, run_index, budget)


async def _launch_developer_agent(
    prompt: str,
    *,
    system_prompt: str,
    cwd: Path,
    slug: str,
    ticket_type: str = "",
    transcript_path: Path | None = None,
    state_path: Path,
    logs_dir: Path,
    mcp_tools: list[str] | None = None,
    project_root: Path | None = None,
    on_event: object = None,
    developer_budget: DeveloperBudget | None = None,
) -> object:
    """Launch the developer agent natively inside the Session Runtime.

    ADR 0028 (container-only): the Runner process itself already executes
    inside the devcontainer, so the developer is a plain in-container
    agent session on the configured backend — no sibling Docker container,
    no runtime mounts, no path remapping. Every path handed to the agent
    (and to the stdio Booley MCP server it spawns) is the Runner's real path.

    Env contract: the BOOLEY_* vars below are EXPORTED into ``os.environ`` so
    the agent CLI and its stdio MCP server inherit the parent environment
    (Claude directly; Codex via the per-ticket HOME config that bakes the
    current BOOLEY_* env). ``BOOLEY_PROJECT_DIR`` is scoped to the backend
    call: ticket-authored content may come from a paired checkout, while
    post-agent Ticket Board transitions must return to the control-plane
    project directory.
    """
    from booley.config.settings import get_backend_config

    from .models import AgentCallParams

    cfg = get_backend_config()
    # Resolve off the live config rather than MODEL_MAP: the map is a module
    # global only refreshed by load_models_config(), so reading the config
    # directly is what makes a [models.roles] developer pin authoritative here.
    model = cfg.model_for_role("developer", "heavy")

    endpoint_env = {
        "BOOLEY_SLUG": slug,
        "BOOLEY_TICKET_TYPE": ticket_type,
        "BOOLEY_TICKET_FILE": str(logs_dir / "ticket.md"),
        "BOOLEY_LOGS_DIR": str(logs_dir),
        "BOOLEY_RUNTIME_DIR": str(ticket_runtime_dir(logs_dir)),
        "BOOLEY_STATE_FILE": str(state_path),
        # Propagate the provider so nested specialists run on the same backend
        # as the developer instead of falling back to the codex default.
        "BOOLEY_PRIMARY_PROVIDER": cfg.provider,
        "BOOLEY_PRIMARY_AUTH": cfg.auth,
        # Job admission role (ADR 0028): everything spawned under this
        # Developer Agent queues behind interactive work. Absent ⇒ interactive.
        "BOOLEY_AGENT_ROLE": "ticket",
    }
    if project_root is not None:
        from booley.runtime.project_dir import resolve_checkout_project_dir

        endpoint_env["BOOLEY_PROJECT_DIR"] = str(resolve_checkout_project_dir(Path(cwd)))
    if mcp_tools is not None:
        # Explicit MCP-exposure allowlist for the developer's stdio server
        # (mcp_server._explicit_mcp_allowlist). Filters MCP visibility only;
        # nested-agent markers (BOOLEY_NESTED_AGENT) are NOT set here — the
        # developer must see the full specialist surface.
        endpoint_env["BOOLEY_MCP_TOOLS"] = ",".join(mcp_tools)
    params = AgentCallParams(
        prompt=prompt,
        model=model,
        cwd=cwd,
        allowed_agent_capabilities=["Read", "Glob", "Grep", "Bash"],
        system_prompt=system_prompt,
        max_turns=200,
        # Nested/specialist calls still use this backend-local stall timeout.
        # The Developer call itself is governed by developer_budget below.
        timeout_seconds=7200,
        transcript_path=transcript_path,
        label="developer",
        reasoning_effort=cfg.effort_for_tier("heavy"),
        # Marks this call developer-level for the Codex backend, which
        # routes it through a per-ticket HOME (config.toml with BOOLEY_* env
        # + this MCP allowlist, no nested markers). Claude ignores the field.
        developer_mcp_tools=list(mcp_tools) if mcp_tools is not None else None,
    )
    backend_kwargs = {"on_event": on_event}
    if developer_budget is not None:
        backend_kwargs["developer_budget"] = developer_budget
    previous_project_dir = os.environ.get("BOOLEY_PROJECT_DIR")
    os.environ.update(endpoint_env)
    try:
        return await cfg.active_backend.call(params, **backend_kwargs)
    finally:
        if previous_project_dir is None:
            os.environ.pop("BOOLEY_PROJECT_DIR", None)
        else:
            os.environ["BOOLEY_PROJECT_DIR"] = previous_project_dir


def _is_pid_alive(pid: int) -> bool:
    """Check if a process with given PID is alive.

    Thin wrapper around the canonical implementation in
    ``ticket_board.helpers`` so lock-liveness behavior stays consistent
    across the codebase (worktree locks, MCP endpoint mutex, orphan detection).
    """
    from booley.ticket_board.helpers import is_pid_alive

    return is_pid_alive(pid)


def _load_endpoint_config(project_root: Path) -> tuple[dict, dict]:
    """Load the ``[mcp_tools]`` and ``[flows]`` config namespaces."""
    from booley.config.settings import _load_booley_toml

    data = _load_booley_toml(project_root)
    if "tools" in data:
        raise ValueError(
            "booley.toml [tools] is retired; use [flows.*] for deterministic "
            "Flows and [mcp_tools.*] for Specialists"
        )
    return data.get("mcp_tools", {}), data.get("flows", {})
