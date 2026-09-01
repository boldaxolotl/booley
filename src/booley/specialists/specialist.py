"""Specialist — McpTool subclass for LLM-powered operations.

Handles provider selection from booley.toml, --model tier resolution
with per-Specialist floor enforcement, prompt construction, and transcript
capture. Reuses the existing agent.py / _claude_backend.py /
_codex_backend.py infrastructure.

Code-modifying Specialists: subclasses that set ``code_modifying = True`` can
use ``_commit_agent_changes()`` to commit the agent's work from Python
instead of relying on the agent to run git commands.
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import logging
import os
import subprocess
from abc import abstractmethod
from pathlib import Path
from typing import Any

from booley.agent_workspace.isolation import remove_shadow_package
from booley.core.models import AgentCallParams

# Also re-exported for backward compatibility: tb_coder + tests import
# _git_head_sha / _read_commit_info from this module.
from booley.dev_support.commit_git_io import (
    _git_head_sha,
    _git_revert_files,
    _read_commit_info,
    _save_files_to_logs,
)
from booley.dev_support.commit_message_format import _auto_format_commit_message
from booley.dev_support.validate_commit_msg import ALLOWED_TYPES
from booley.mcp.base import EXIT_ERROR, McpTool, McpToolResult
from booley.runtime import job_slots
from booley.runtime.nested_mcp_capabilities import nested_mcp_tools_for
from booley.runtime.process_tree import descendant_pids as _descendant_pids

from .specialist_workspace import (
    WorkspaceAccess,
    isolated_agent_workspace,
    restore_result_paths,
)

logger = logging.getLogger(__name__)

# Tier ranking for floor enforcement (higher = more capable)
TIER_RANK: dict[str, int] = {
    "light": 0,
    "standard": 1,
    "heavy": 2,
}

VALID_TIERS = tuple(TIER_RANK.keys())


class Specialist(McpTool):
    """Base for Specialists that invoke an LLM agent.

    Subclasses implement ``_build_prompt`` and ``_interpret_output``.
    The base handles model selection, agent invocation, and transcript capture.

    Token/cost tracking: each call via _invoke_agent() accumulates into
    _total_* counters. Subclasses that override _run() and make multiple
    agent calls get automatic aggregation — use _invoke_agent() instead
    of calling _call_agent_sync() directly.
    """

    # Specialists are model-API-bound with ~no local footprint (ADR 0028):
    # every Specialist admits under the LIGHT class pool.
    JOB_CLASS = job_slots.CLASS_LIGHT

    # Minimum model tier (floor) — subclasses override this
    min_model: str = "standard"
    # Default max conversation turns
    default_max_turns: int | None = None
    # Default timeout in seconds
    default_timeout: int = 1800
    # Minimum timeout — developer can't go below this (prevents premature exit-2)
    min_timeout: int = 600
    # Agent capability set (Claude Code capability names)
    agent_capabilities: list[str] | None = None
    # Provider-independent write boundary for the nested agent. Read-only
    # calls run against a disposable snapshot of the current worktree.
    workspace_access: WorkspaceAccess = "read_write"

    # Per-specialist nested-MCP allowlists live in
    # ``booley.runtime.nested_mcp_capabilities.NESTED_MCP_CAPABILITIES`` and are
    # injected by ``_invoke_agent`` — do not redeclare here per subclass.

    def __init__(self) -> None:
        super().__init__()
        self._total_input_tokens = 0
        self._total_output_tokens = 0
        self._total_cached_tokens = 0
        self._total_cache_create_tokens = 0
        self._total_cost_usd = 0.0
        self._last_session_id: str | None = None

    def _add_args(self, parser: argparse.ArgumentParser) -> None:
        """Add agent-specific arguments."""
        parser.add_argument(
            "--model",
            choices=list(VALID_TIERS),
            default=None,
            help=(
                f"Model tier for this invocation ({'/'.join(VALID_TIERS)}). "
                f"Clamped to Specialist's minimum floor ({self.min_model}) if below."
            ),
        )
        parser.add_argument(
            "--instruction",
            default="",
            help="Freeform instruction for the agent (e.g. distilled plan)",
        )
        parser.add_argument(
            "--transcript-dir",
            type=Path,
            default=None,
            help="Directory for agent transcript output",
        )
        parser.add_argument(
            "--max-turns",
            type=int,
            default=self.default_max_turns,
            help="Maximum agent conversation turns",
        )
        parser.add_argument(
            "--timeout",
            type=int,
            default=self.default_timeout,
            help="Agent timeout in seconds",
        )
        self._add_agent_args(parser)

    def _add_agent_args(self, parser: argparse.ArgumentParser) -> None:
        """Hook for subclasses to add additional arguments."""

    def _resolve_model(self) -> str:
        """Resolve the model to run this specialist on.

        Priority:
          1. ``[models.roles] <Specialist name>`` in booley.toml — an explicit pin,
             which deliberately overrides the ``min_model`` floor (the floor
             guards Booley's tier defaults, not a user's stated choice).
          2. ``--model`` tier flag, clamped up to the ``min_model`` floor.
          3. The floor tier itself.
        """
        tier = self._resolve_tier(self.args.model)
        try:
            from booley.config.settings import get_backend_config

            cfg = get_backend_config()
            return cfg.model_for_role(self.name, tier)
        except (ImportError, AttributeError):
            return _DEFAULT_TIER_MODELS.get(tier, "claude-opus-4-8")

    def _resolve_effort(self) -> str | None:
        """Resolve reasoning effort for the active tier."""
        tier = self._resolve_tier(self.args.model)
        try:
            from booley.config.settings import get_backend_config

            cfg = get_backend_config()
            return cfg.effort_for_tier(tier)
        except (ImportError, AttributeError):
            return None

    def _resolve_tier(self, requested: str | None) -> str:
        """Apply floor enforcement: return max(requested, min_model)."""
        if requested is None:
            return self.min_model
        req_rank = TIER_RANK.get(requested, 1)
        floor_rank = TIER_RANK.get(self.min_model, 1)
        if req_rank < floor_rank:
            logger.info(
                "%s: --model %s below floor %s, upgrading",
                self.name,
                requested,
                self.min_model,
            )
            return self.min_model
        return requested

    def _transcript_path(self) -> Path | None:
        """Resolve transcript file path."""
        transcript_dir = self.args.transcript_dir
        if transcript_dir is None:
            return None
        transcript_dir.mkdir(parents=True, exist_ok=True)
        return transcript_dir / f"{self.name}.jsonl"

    # --- Overridable agent parameters ---

    def _system_prompt(self) -> str | None:
        """Return system prompt override. None = use default."""
        return None

    def _output_format(self) -> dict[str, Any] | None:
        """Return JSON schema for structured output. None = free-form."""
        return None

    def _needs_skills(self) -> bool:
        """Whether the agent needs project-level runtime settings loaded."""
        return False

    def _disallowed_agent_capabilities(self) -> list[str] | None:
        """Agent-capability deny patterns for category boundary enforcement. Override in subclasses."""
        return None

    def _workspace_isolation_category(self) -> str | None:
        """Category whose opposite sources must be absent from a read-only snapshot."""
        return None

    @abstractmethod
    def _build_prompt(self) -> str:
        """Build the agent prompt. Implemented by subclasses."""

    @abstractmethod
    def _interpret_output(self, output: str, structured: dict | None) -> McpToolResult:
        """Interpret agent output into a McpToolResult. Implemented by subclasses."""

    def _invoke_agent(
        self,
        params: AgentCallParams,
        on_event: Any = None,
    ) -> Any:
        """Call an agent and accumulate token/cost usage.

        Automatically attaches a streaming callback that writes
        specialist_thinking events to display.jsonl when on_event
        is not provided.

        Also auto-injects the per-specialist nested MCP-tool allowlist
        from ``booley.runtime.nested_mcp_capabilities`` when the caller hasn't
        set ``params.nested_mcp_tools`` — that's the single source of
        truth, so call sites don't pass it.

        The call is wrapped in the parent-death watchdog: an agent turn is
        the one place a Specialist burns money, so a run whose client has
        disconnected must not survive it (SETUP-F-36).
        """
        if on_event is None:
            on_event = self._make_streaming_callback()
        if params.nested_mcp_tools is None:
            params = dataclasses.replace(
                params,
                nested_mcp_tools=nested_mcp_tools_for(self.name),
            )
        with isolated_agent_workspace(
            params,
            self.workspace_access,
            self._workspace_isolation_category(),
        ) as (
            call_params,
            snapshot,
        ):
            with parent_death_watchdog(self.name):
                result = _call_agent_sync(call_params, on_event=on_event)
            if snapshot is not None:
                result = restore_result_paths(result, snapshot)
        self._total_input_tokens += getattr(result, "input_tokens", 0)
        self._total_output_tokens += getattr(result, "output_tokens", 0)
        self._total_cached_tokens += getattr(result, "cached_tokens", 0)
        self._total_cache_create_tokens += getattr(result, "cache_create_tokens", 0)
        self._total_cost_usd += getattr(result, "cost_usd", 0.0)
        # A session id is a string or nothing. Backends have handed back other
        # shapes (and test doubles hand back mocks), and since F-42 gave
        # standalone runs a real persistence path, anything non-str would now
        # reach `Path.write_text` and raise instead of being quietly ignored.
        session_id = getattr(result, "session_id", None)
        self._last_session_id = session_id if isinstance(session_id, str) else None
        return result

    def _invoke_agent_with_resume(
        self,
        params: AgentCallParams,
        on_event: Any = None,
    ) -> Any:
        """Call an agent with resume; fall back to fresh session on failure.

        Only catches SDK/API errors that could be caused by a stale session.
        Timeouts and hard crashes propagate unchanged.
        """
        if not params.resume_session:
            return self._invoke_agent(params, on_event=on_event)
        from claude_agent_sdk import ClaudeSDKError

        from booley.runtime.agent_errors import TransientAPIError

        try:
            return self._invoke_agent(params, on_event=on_event)
        except (ClaudeSDKError, TransientAPIError, RuntimeError) as exc:
            logger.warning(
                "%s: resumed session failed (%s), retrying as fresh invocation",
                self.name,
                exc,
            )
            fresh = dataclasses.replace(params, session_id=None, resume_session=False)
            return self._invoke_agent(fresh, on_event=on_event)

    # --- Session persistence helpers ---

    def _session_file_path(self, key: str) -> Path | None:
        """Return the path for a session_id persistence file, or ``None``.

        Ticket Mode's ``BOOLEY_LOGS_DIR`` is the first choice. Outside a ticket
        that variable is never set, and returning ``None`` there meant every
        retry round cold-started the agent instead of resuming it with its own
        context — the resume-with-failure-log design silently disabled, at full
        re-prompt cost (SETUP-F-42). Standalone runs therefore fall back to the
        project's own persistent runtime tree, the same anchor
        ``mutation_lock.lock_dir`` uses so a lock and its creator session live
        together. ``None`` only when no project is discoverable at all (direct
        test callers with no project on disk).
        """
        logs_dir = os.environ.get("BOOLEY_LOGS_DIR")
        if logs_dir and Path(logs_dir).is_dir():
            return Path(logs_dir) / f"{key}.session_id"
        try:
            from booley.runtime.project_dir import runtime_dir

            return runtime_dir() / "sessions" / f"{key}.session_id"
        except (FileNotFoundError, ImportError, OSError):
            logger.debug("No project runtime dir for session persistence (key=%s)", key)
            return None

    def _persist_session_id(self, key: str) -> None:
        """Write self._last_session_id to disk for cross-invocation resume."""
        if not self._last_session_id:
            return
        path = self._session_file_path(key)
        if path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(self._last_session_id, encoding="utf-8")
        except OSError:
            logger.warning("Failed to persist session_id to %s", path)

    def _load_session_id(self, key: str) -> str | None:
        """Load a previously persisted session_id. Returns None if missing/corrupt."""
        path = self._session_file_path(key)
        if path is None or not path.exists():
            return None
        try:
            sid = path.read_text(encoding="utf-8").strip()
            return sid if sid else None
        except OSError:
            logger.warning("Failed to load session_id from %s", path)
            return None

    def _clear_session_id(self, key: str) -> None:
        """Delete a persisted session_id file."""
        path = self._session_file_path(key)
        if path is not None and path.exists():
            with contextlib.suppress(OSError):
                path.unlink()

    def _build_resume_params(self, params: AgentCallParams, session_id: str) -> AgentCallParams:
        """Return a copy of params configured for session resume."""
        return dataclasses.replace(params, session_id=session_id, resume_session=True)

    def _make_streaming_callback(self) -> Any:
        """Create an on_event callback that streams agent text to display.jsonl."""
        from booley.mcp.base import _specialist_thinking_event, _write_display_event

        def _on_event(event: dict) -> None:
            if event.get("type") == "agent_thinking":
                text = event.get("text", "")
                if text:
                    _write_display_event(_specialist_thinking_event(text))

        return _on_event

    def _validate_interactive_args(self) -> McpToolResult | None:
        """Interactive-Mode argument validation hook.

        Default: no-op. Specialists override this to catch missing arguments
        with a useful message before prompt construction or command assembly.
        """
        return None

    def _run(self) -> McpToolResult:
        """Invoke the agent and interpret results."""
        validation_err = self._validate_interactive_args()
        if validation_err is not None:
            return validation_err

        if self.args.timeout < self.min_timeout:
            logger.warning(
                "%s: --timeout %ds below minimum %ds, clamping",
                self.name,
                self.args.timeout,
                self.min_timeout,
            )
            self.args.timeout = self.min_timeout

        prompt = self._build_prompt()
        model = self._resolve_model()
        effort = self._resolve_effort()
        transcript = self._transcript_path()

        tier = self._resolve_tier(self.args.model)
        logger.info(
            "Invoking agent %s (model=%s, tier=%s, effort=%s, max_turns=%s, timeout=%ds)",
            self.name,
            model,
            tier,
            effort or "default",
            self.args.max_turns,
            self.args.timeout,
        )
        self.emit_progress(f"invoking agent ({tier}, timeout={self.args.timeout}s)")

        try:
            result = self._invoke_agent(
                AgentCallParams(
                    prompt=prompt,
                    model=model,
                    cwd=self.args.work_dir,
                    allowed_agent_capabilities=self.agent_capabilities,
                    disallowed_agent_capabilities=self._disallowed_agent_capabilities(),
                    system_prompt=self._system_prompt(),
                    output_format=self._output_format(),
                    max_turns=self.args.max_turns,
                    timeout_seconds=self.args.timeout,
                    transcript_path=transcript,
                    label=self.name,
                    needs_skills=self._needs_skills(),
                    reasoning_effort=effort,
                )
            )
        except Exception:
            logger.exception("Agent invocation failed for %s", self.name)
            self.emit_progress("agent invocation failed")
            return McpToolResult(exit_code=EXIT_ERROR, report_text="Agent invocation failed")
        finally:
            remove_shadow_package(self.args.work_dir)

        self.emit_progress("agent completed, interpreting output")
        return self._interpret_output(result.output, result.structured)

    # --- Git commit helpers for code-modifying Specialists ---

    # Subclasses override to provide a Specialist-specific fallback message.
    _default_commit_message: str = "wip: apply agent changes"

    # Shared commit-message guidance injected into code-modifying Specialist prompts.
    # NOTE: do not bake the banned-phrase list into this static string — the
    # list is loaded from booley.toml at module import in
    # ``commit_msg_utils`` and is project-specific. Use
    # ``commit_msg_banned_phrase_note()`` to get a dynamic warning string.
    COMMIT_MSG_GUIDANCE = (
        "## Commit Message Format\n"
        "Your `commit_message` output MUST follow conventional commits:\n"
        "  <type>(<scope>): <summary>\n"
        # Built from the validator's ALLOWED_TYPES so the prompt can never
        # advertise a type the commit-msg hook rejects (or omit one it
        # accepts — a hardcoded copy here once missed `chore`).
        f"Valid types: {', '.join(ALLOWED_TYPES)}\n"
        "Scope: lowercase module/file name, e.g. iir_filter, tb, rtl (required).\n"
        "Summary: max 72 chars, lowercase start, no trailing period.\n"
        "Single line only — no body.\n"
        "Examples: 'feat(cla_adder): add carry-lookahead pipeline',\n"
        "  'test(tb): add pipelined adder coverage',\n"
        "  'fix(rtl): correct byte alignment in stage 2'\n"
    )

    @staticmethod
    def commit_msg_banned_phrase_note(project_root: Path | None = None) -> str:
        """Build a "do not use these words" note from the live banned-phrase list.

        Loaded dynamically so project-local overrides (booley.toml
        ``[stealth] banned_words``) are honored. Used by code-modifying Specialists
        to warn the agent up front and avoid burning a full agent run to a
        post-hoc commit-msg rejection (Pattern A4 — see field reports).
        """
        try:
            from booley.dev_support.commit_msg_utils import banned_phrases, stealth_enabled
        except ImportError:
            return ""
        # Stealth mode is opt-out ([stealth] enabled = false); when off, neither
        # the commit-msg hook nor this prompt warning applies.
        phrases = banned_phrases(project_root)
        if not stealth_enabled(project_root) or not phrases:
            return ""
        joined = ", ".join(sorted(set(phrases)))
        return (
            "## Banned Words in Commit Message\n"
            "Your `commit_message` MUST NOT contain any of the following "
            "phrases (case-insensitive, word-boundary match). They are "
            "rejected by the commit-msg validator and will fail the commit "
            "after the work is done, forcing a fallback to a generic "
            f"message:\n  {joined}\n"
            "Pick neutral hardware-design vocabulary instead "
            "(e.g. 'utility', 'helper', 'module', 'block').\n"
        )

    class GitStatusError(RuntimeError):
        """Raised when ``git status`` fails (e.g. ownership / config issues)."""

    def _get_uncommitted_files(self) -> list[str]:
        """Return repo-relative paths of modified/untracked files.

        Raises ``GitStatusError`` if ``git status`` itself fails so callers
        can distinguish "no changes" from "git is broken".
        """
        try:
            result = subprocess.run(
                ["git", "status", "--porcelain", "-uall"],
                cwd=self.args.work_dir,
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            if result.returncode != 0:
                logger.error(
                    "git status failed (rc=%d): %s",
                    result.returncode,
                    result.stderr.strip(),
                )
                raise self.GitStatusError(result.stderr.strip())
        except subprocess.TimeoutExpired as exc:
            logger.error("git status timed out in %s", self.args.work_dir)
            raise self.GitStatusError("git status timed out") from exc
        except FileNotFoundError as exc:
            logger.error("git not found on PATH")
            raise self.GitStatusError("git not found") from exc
        files: list[str] = []
        for line in result.stdout.splitlines():
            if len(line) > 3:
                path = line[3:].split(" -> ")[-1].strip()
                files.append(path.replace("\\", "/"))
        return files

    def _commit_agent_changes(
        self,
        message: str | None = None,
        *,
        scope_files: list[str],
    ) -> dict[str, Any] | None:
        """Stage and commit the agent's file changes from Python.

        If the agent already committed (HEAD advanced past ``_before_sha``),
        returns that commit's info without creating another.

        Returns:
            Dict with ``sha``, ``subject``, ``stat_summary``, ``changed_files``
            on success; ``None`` if nothing to commit.
        """
        work_dir = self.args.work_dir
        before_sha = getattr(self, "_before_sha", None)
        msg = self._prepare_commit_message(message)

        # Agent may have committed despite being told not to — use it.
        current_sha = _git_head_sha(work_dir)
        if before_sha and current_sha and current_sha != before_sha:
            logger.info("Agent already committed — using existing commit")
            return _read_commit_info(work_dir, before_sha)

        # Determine which files to stage, revert out-of-scope
        to_stage, reverted = self._resolve_stageable_files(scope_files)
        if not to_stage:
            return None

        # Stage and commit
        self._git_add_and_commit(work_dir, to_stage, msg)
        info = _read_commit_info(work_dir, before_sha or "")
        if reverted:
            info["reverted_files"] = reverted
        return info

    def _prepare_commit_message(self, message: str | None) -> str:
        """Auto-format and validate commit message, raising on failure."""
        work_dir = self.args.work_dir
        msg = message or self._default_commit_message
        category_hint = getattr(self, "modifies_category", "") or ""
        msg = _auto_format_commit_message(msg, category=category_hint)

        # Guard: never commit in main worktree (must be a linked worktree)
        git_path = Path(work_dir) / ".git"
        if git_path.is_dir():
            raise self.GitStatusError(
                f"Refusing to commit in main worktree ({work_dir}). "
                "Commits must happen in a linked worktree (.git must be a file)."
            )

        from booley.dev_support.validate_commit_msg import validate_message

        errors = validate_message(msg, project_root=work_dir)
        if errors:
            # Salvage: if the only errors are banned-phrase hits in the
            # agent's subject, retry with the Specialist's default commit message.
            # The agent's freeform text is lost (subject only), but this
            # avoids burning a full multi-minute agent run to exit-2 on a
            # stylistic violation the agent could not have known about
            # (banned-phrase list is project-local config).
            if all("Banned phrase" in e for e in errors):
                fallback = _auto_format_commit_message(
                    self._default_commit_message,
                    category=category_hint,
                )
                fallback_errors = validate_message(fallback, project_root=work_dir)
                if not fallback_errors:
                    logger.warning(
                        "Commit message rejected for banned phrase(s) "
                        "(%s); falling back to default %r",
                        "; ".join(errors),
                        fallback,
                    )
                    return fallback
            raise self.GitStatusError(f"Commit message validation failed: {'; '.join(errors)}")
        return msg

    def _resolve_stageable_files(
        self,
        scope_files: list[str],
    ) -> tuple[list[str], list[dict[str, str]]]:
        """Determine which files to stage; revert out-of-scope files.

        Returns (to_stage, reverted_info).
        """
        try:
            uncommitted = self._get_uncommitted_files()
        except self.GitStatusError as exc:
            logger.error("Cannot list changes — git is broken: %s", exc)
            raise
        if not uncommitted:
            return [], []
        allowed = {f.replace("\\", "/") for f in scope_files}
        to_stage = [f for f in uncommitted if f in allowed]
        # Never revert project config — it's not agent-authored scope
        _PROTECTED_PREFIXES = (".booley_project/",)
        skipped = {
            f
            for f in uncommitted
            if f not in allowed and not any(f.startswith(p) for p in _PROTECTED_PREFIXES)
        }
        reverted: list[dict[str, str]] = []
        if skipped:
            logger.warning(
                "Skipping %d out-of-scope files: %s",
                len(skipped),
                ", ".join(sorted(skipped)[:5]),
            )
            reverted = self._revert_out_of_scope(sorted(skipped))
        return to_stage, reverted

    def _git_add_and_commit(
        self,
        work_dir: Path,
        to_stage: list[str],
        msg: str,
    ) -> None:
        """Stage files and create a commit. Raises GitStatusError on failure."""
        from booley.runtime.git import git_run

        add_result = git_run(work_dir, ["add", "--", *to_stage], timeout=30)
        if add_result.returncode != 0:
            logger.error("Git add failed: %s\nstdout: %s", add_result.stderr, add_result.stdout)
            raise self.GitStatusError(f"git add failed: {add_result.stderr.strip()}")

        commit_result = git_run(work_dir, ["commit", "-m", msg], timeout=30)
        if commit_result.returncode != 0:
            logger.error(
                "Git commit failed: %s\nstdout: %s", commit_result.stderr, commit_result.stdout
            )
            raise self.GitStatusError(f"git commit failed: {commit_result.stderr.strip()}")

    def _revert_out_of_scope(self, paths: list[str]) -> list[dict[str, str]]:
        """Copy out-of-scope files to logs, then revert them from worktree.

        Returns a list of dicts with ``original_path`` and ``saved_path``.
        """
        work_dir = Path(self.args.work_dir)
        saved = _save_files_to_logs(work_dir, paths)
        _git_revert_files(work_dir, paths)
        return saved

    @staticmethod
    def _extract_commit_message(structured: dict | None) -> str | None:
        """Extract commit_message from agent's structured output, if present."""
        if structured and isinstance(structured, dict):
            msg = structured.get("commit_message")
            if msg and isinstance(msg, str) and msg.strip():
                return msg.strip()
        return None

    def _finalize_result(self, result: McpToolResult) -> None:
        """Stamp accumulated agent token/cost data onto the McpToolResult."""
        result.input_tokens = self._total_input_tokens
        result.output_tokens = self._total_output_tokens
        result.cached_tokens = self._total_cached_tokens
        result.cache_create_tokens = self._total_cache_create_tokens
        result.cost_usd = self._total_cost_usd
        super()._finalize_result(result)


# ---------------------------------------------------------------------------
# Parent-death watchdog (SETUP-F-36)
# ---------------------------------------------------------------------------
#
# Killing the client of a `docker exec`/`booley session enter` run does NOT
# kill what it started: the in-container Specialist and its agent kept running
# detached, burning tokens with nobody reading the answer. Nothing else in
# the codebase watches for this — orphan_handler and ticket_board reconcile
# *tickets* after the fact, from the outside; run_guard watches disk. So the
# The Specialist watches its own parent: when the launching process dies, it is
# reparented (PID 1 in a container), which is cheap to poll and unambiguous.

_PARENT_WATCHDOG_ENV = "BOOLEY_PARENT_WATCHDOG"
_PARENT_WATCHDOG_INTERVAL_S = 5.0

# Shared rather than re-implemented, and imported under the private name so it
# can be substituted in tests.


def _abort_orphaned_run(label: str, initial_ppid: int) -> None:
    """Tear down this run after its launching process died.

    SIGTERM (not ``os._exit``) so the Specialist's own cleanup still runs — the
    isolation stash restore in particular.

    The agent CLI is the expensive half and it is spawned as a plain child
    (``_codex_backend._codex_spawn`` passes no ``start_new_session``), so it
    only shares our process group when we happen to lead one. That is true
    under MCP dispatch and potentially false for a direct shell launch, where
    the fallback used to signal *only ourselves* and leave the agent
    burning tokens — the exact thing this watchdog exists to stop. So the
    descendants are followed by parent link and signalled first (deepest
    first), and the process group only after, making the teardown independent
    of who happens to lead the group.
    """
    logger.error(
        "%s: launching process %d is gone — aborting this run (agent detached, "
        "tokens still burning). Set %s=0 to disable this watchdog.",
        label,
        initial_ppid,
        _PARENT_WATCHDOG_ENV,
    )
    import signal

    for pid in _descendant_pids(os.getpid()):
        with contextlib.suppress(OSError):
            os.kill(pid, signal.SIGTERM)

    with contextlib.suppress(OSError):
        if os.getpgid(0) == os.getpid():
            # Also covers grandchildren that re-exec'd but stayed in the group.
            os.killpg(os.getpgid(0), signal.SIGTERM)
            return
    with contextlib.suppress(OSError):
        os.kill(os.getpid(), signal.SIGTERM)


@contextlib.contextmanager
def parent_death_watchdog(
    label: str,
    interval: float = _PARENT_WATCHDOG_INTERVAL_S,
) -> Any:
    """Abort the run if the process that launched it disappears.

    No-op when disabled by env, on Windows (no reparenting to observe), or
    when already parented to init (nothing left to lose).
    """
    import threading

    if (os.environ.get(_PARENT_WATCHDOG_ENV, "") or "").strip() in {"0", "false", "no"}:
        yield
        return
    if os.name != "posix":
        yield
        return
    initial_ppid = os.getppid()
    if initial_ppid <= 1:
        yield
        return

    stop = threading.Event()

    def _poll() -> None:
        while not stop.wait(interval):
            if os.getppid() != initial_ppid:
                _abort_orphaned_run(label, initial_ppid)
                return

    thread = threading.Thread(target=_poll, name=f"parent-watchdog-{label}", daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop.set()


# Standalone default models (when harness config is unavailable).
# Source the model strings from _backend_config so the pins live in one place.
def _standalone_default_tier_models() -> dict[str, str]:
    from booley.config.agent import _PROVIDER_TIER_MODELS

    return dict(_PROVIDER_TIER_MODELS["claude"])


_DEFAULT_TIER_MODELS: dict[str, str] = _standalone_default_tier_models()


def _call_agent_sync(
    params: AgentCallParams,
    on_event: Any = None,
) -> Any:
    """Synchronous wrapper around harness.agent.call_agent().

    Runs the async call_agent in an event loop. Returns AgentResult.
    """
    import asyncio

    from booley.runtime.agent import call_agent

    async def _invoke() -> Any:
        return await call_agent(params, on_event=on_event)

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop and loop.is_running():
        import concurrent.futures

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(asyncio.run, _invoke())
            return future.result(timeout=params.timeout_seconds + 30)
    return asyncio.run(_invoke())
