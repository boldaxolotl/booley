"""In-process ticket operations: Protocol + DirectTicketOps.

Replaces subprocess calls to ``python -m ticket_board`` with direct
function calls, eliminating process-spawn overhead and enabling mock
injection for tests.
"""

from __future__ import annotations

import contextlib
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from booley.runtime.timefmt import parse_timestamp
from booley.ticket_board.analytics import (
    attribute_tokens_to_steps,
    collect_all_messages,
    collect_step_transcript_usage,
    collect_step_usage,
    compute_step_durations,
    parse_transitions_log,
    usage_entries_to_steps,
)
from booley.ticket_board.cli_handlers import (
    _cmd_update_board,
)
from booley.ticket_board.constants import STEP_ORDER, VALID_TYPES
from booley.ticket_board.evidence import op_collect_evidence
from booley.ticket_board.execution import (
    classify_tickets,
    next_from_planned,
    resume_detect,
)
from booley.ticket_board.helpers import tickets_dir_from_project_root
from booley.ticket_board.io import TicketIO, scan_all_tickets
from booley.ticket_board.lifecycle import SETTLED_STATUSES
from booley.ticket_board.logs import load_progress
from booley.ticket_board.operations import (
    op_activate,
    op_block,
    op_claim,
    op_complete,
    op_fail,
    op_handoff,
    op_promote_waiting,
    op_unblock,
)
from booley.ticket_board.paths import existing_human_log_file
from booley.ticket_board.reporting import format_timing_report
from booley.ticket_board.ticket_document import (
    convert_ticket_document,
    ticket_conversion_context,
)
from booley.ticket_board.validation import (
    format_validate_logs_report,
    owned_draft_dirty_paths,
    validate_git_state,
    validate_ticket_spec,
)
from booley.ticket_board.validation import validate_logs as tb_validate_logs

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Exception (shared by all implementations)
# ---------------------------------------------------------------------------


class TicketCLIError(Exception):
    """A ticket operation failed."""

    def __init__(self, subcommand: str, returncode: int, stderr: str) -> None:
        self.subcommand = subcommand
        self.returncode = returncode
        self.stderr = stderr
        super().__init__(f"ticket_board {subcommand} failed (rc={returncode}): {stderr}")


@dataclass
class CreateTicketParams:
    """Path to the complete human-readable Ticket document."""

    document_file: str


def _check(ok: bool, cmd: str, slug: str) -> None:
    """Raise TicketCLIError when an op_* function returns False."""
    if not ok:
        raise TicketCLIError(cmd, 2, f"operation failed for '{slug}'")


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class TicketOps(Protocol):
    """Abstract interface for ticket board operations.

    Every method mirrors the corresponding ``ticket_cli`` module-level
    function signature so the facade can delegate 1:1.
    """

    # Read-only
    def classify(self, project_root: Path) -> dict[str, Any]: ...
    def parse_ticket(self, project_root: Path, path: str) -> dict[str, Any]: ...
    def validate_ticket(
        self, project_root: Path, path: str, *, check_git: bool = False
    ) -> dict[str, Any]: ...
    def resume(self, project_root: Path, slug: str) -> dict[str, Any]: ...
    def next_step(
        self, project_root: Path, type_or_slug: str, current: str, skip: str = ""
    ) -> str | None: ...
    def collect_evidence(self, project_root: Path, slug: str) -> dict[str, Any]: ...
    def ticket_status(self, project_root: Path, slug: str) -> str: ...

    # State-changing
    def activate(
        self,
        project_root: Path,
        slug: str,
        *,
        owner_pid: int | None = None,
        execution_id: str | None = None,
    ) -> bool: ...
    def claim(self, project_root: Path, slug: str) -> bool: ...
    def init_ticket(
        self,
        project_root: Path,
        ticket_path: str,
        *,
        execution_id: str = "",
        owner_pid: int | None = None,
    ) -> dict[str, Any]: ...
    def update_board(
        self,
        project_root: Path,
        slug: str,
        *,
        set_fields: dict[str, str] | None = None,
        append_step: str = "",
        reset_steps: bool = False,
        reset_steps_from: str = "",
        log: bool = False,
    ) -> None: ...
    def log_incident(
        self,
        project_root: Path,
        slug: str,
        *,
        incident_type: str,
        step: str,
        description: str,
        resolution: str = "unresolved",
    ) -> None: ...
    def block(
        self,
        project_root: Path,
        slug: str,
        *,
        reason: str,
        step: str,
        expected_execution_id: str | None = None,
    ) -> None: ...
    def fail(self, project_root: Path, slug: str, *, error: str, step: str) -> None: ...
    def handoff(
        self,
        project_root: Path,
        slug: str,
        *,
        expected_execution_id: str | None = None,
    ) -> None: ...
    def unblock(
        self,
        project_root: Path,
        slug: str,
        *,
        feedback: str = "",
        actor: str = "ticket-triage",
        detail: str = "user answered questions",
        feedback_heading: str = "Human Response",
    ) -> bool: ...
    def promote_waiting(self, project_root: Path) -> str: ...
    def validate_logs(self, project_root: Path, slug: str) -> tuple[bool, str]: ...
    def timing(self, project_root: Path, slug: str, *, save: bool = False) -> str: ...
    def generate_slug(self, project_root: Path, summary: str) -> str: ...
    def create_ticket_file(
        self, project_root: Path, slug: str, params: CreateTicketParams
    ) -> Path: ...
    def enqueue(self, project_root: Path, slug: str) -> None: ...
    def complete(self, project_root: Path, slug: str) -> None: ...


# ---------------------------------------------------------------------------
# Direct (in-process) implementation
# ---------------------------------------------------------------------------


class DirectTicketOps:
    """Calls ticket_board functions directly — no subprocess."""

    @staticmethod
    def _tio(project_root: Path):
        # Resolve the tickets dir through the one canonical helper (honors
        # TICKETS_DIR -> BOOLEY_PROJECT_DIR -> .booley_project convention).
        # Hand-joining project_root/.booley_project here diverged from every
        # other caller when BOOLEY_PROJECT_DIR points at a *separate* bind mount
        # of the same dir (the devcontainer mounts .booley_project at both
        # /booley-project and /work/.booley_project): the board move then
        # crossed the two mounts and shutil.move raised EXDEV (ADR 0028).
        tickets_dir = tickets_dir_from_project_root(project_root)
        return TicketIO(tickets_dir, project_root=project_root)

    # -- Read-only ---------------------------------------------------------

    def classify(self, project_root: Path) -> dict[str, Any]:
        tio = self._tio(project_root)
        return classify_tickets(
            scan_all_tickets(tio.tickets_dir, project_root=tio._project_root),
            logs_dir=tio.logs_dir,
        )

    def parse_ticket(self, project_root: Path, path: str) -> dict[str, Any]:
        tio = self._tio(project_root)
        p = Path(path)
        if not p.exists():
            raise TicketCLIError("parse-ticket", 2, f"File not found: {path}")
        with p.open(encoding="utf-8") as f:
            text = f.read()
        stage = (
            "executable"
            if p.parent.name
            in {"queue", "waiting", "active", "blocked", "review", "done", "archived"}
            else "draft"
        )
        with ticket_conversion_context(project_root, p.stem, stage) as context:
            converted = convert_ticket_document(text, context)
        if converted.document is None:
            detail = "; ".join(item.message for item in converted.diagnostics)
            raise TicketCLIError("parse-ticket", 1, detail)
        document = converted.document
        fields = {**document.spec.fields, **document.generated}
        body = document.spec.body
        progress = load_progress(tio.logs_dir, p.stem)
        if progress is not None:
            fields.update(progress)
        return {"fields": fields, "body": body}

    def validate_ticket(
        self, project_root: Path, path: str, *, check_git: bool = False
    ) -> dict[str, Any]:
        p = Path(path)
        if not p.exists():
            return {"errors": [f"File not found: {path}"]}
        stage = (
            "executable"
            if p.parent.name
            in {"queue", "waiting", "active", "blocked", "review", "done", "archived"}
            else "draft"
        )
        tio = self._tio(project_root)
        workspace, workspace_error = self._validation_workspace(project_root, p)
        if workspace_error is not None:
            return workspace_error
        with ticket_conversion_context(project_root, p.stem, stage) as context:
            converted = convert_ticket_document(p.read_text(encoding="utf-8"), context)
        if converted.document is None:
            return {
                "errors": [
                    f"{item.line}:{item.column}: {item.message}" for item in converted.diagnostics
                ]
            }
        spec = converted.document.spec
        validation_root = workspace if workspace is not None else project_root
        allowed = owned_draft_dirty_paths(p, tio.tickets_dir)
        errors = validate_ticket_spec(
            spec,
            project_root=validation_root,
            check_git=check_git and validation_root == project_root,
            allowed_dirty_paths=allowed,
        )
        if check_git and validation_root != project_root:
            errors.extend(validate_git_state(dict(spec.fields), project_root, allowed))
        if workspace is not None:
            from booley.ticket_board.workspace_ops import (
                validate_ticket_spec_authoring_inputs,
            )

            try:
                validate_ticket_spec_authoring_inputs(project_root, workspace, spec)
            except (RuntimeError, ValueError, OSError) as exc:
                errors.append(str(exc))
        if errors:
            return {"errors": errors}
        return {"errors": [], "valid": True}

    @staticmethod
    def _validation_workspace(
        project_root: Path, path: Path
    ) -> tuple[Path | None, dict[str, Any] | None]:
        if path.parent.name != "drafts" or not (project_root / ".git").exists():
            return None, None
        from booley.ticket_board.workspace_ops import ensure_ticket_workspace

        try:
            return ensure_ticket_workspace(project_root, path, path.stem).outer, None
        except (RuntimeError, ValueError, OSError) as exc:
            return None, {"errors": [f"Ticket workspace preparation failed: {exc}"]}

    def resume(self, project_root: Path, slug: str) -> dict[str, Any]:
        tio = self._tio(project_root)
        entry = tio.find_ticket(slug)
        if not entry:
            raise TicketCLIError("resume", 1, f"Ticket '{slug}' not found")
        return resume_detect(entry)

    def next_step(
        self, project_root: Path, type_or_slug: str, current: str, skip: str = ""
    ) -> str | None:
        tio = self._tio(project_root)
        ticket_type = type_or_slug
        planned = None
        if ticket_type not in VALID_TYPES:
            entry = tio.find_ticket(ticket_type)
            if entry:
                planned = entry.get("planned_steps", [])
                ticket_type = entry.get("type", "feature")
                if ticket_type not in VALID_TYPES:
                    ticket_type = "feature"
            else:
                raise TicketCLIError(
                    "next-stage",
                    1,
                    f"'{type_or_slug}' is not a valid ticket type "
                    f"and no ticket with that slug was found",
                )
        extra = [s.strip() for s in skip.split(",") if s.strip()] if skip else None
        if not planned:
            skip_set = set(extra) if extra else set()
            planned = [s for s in STEP_ORDER if s not in skip_set]
        result = next_from_planned(planned, current)
        return result or None

    def collect_evidence(self, project_root: Path, slug: str) -> dict[str, Any]:
        tio = self._tio(project_root)
        evidence = op_collect_evidence(tio, slug)
        if evidence is None:
            raise TicketCLIError("collect-evidence", 1, f"Ticket '{slug}' not found")
        return evidence

    def ticket_status(self, project_root: Path, slug: str) -> str:
        """Current board status, or "" when the ticket is not on the board."""
        entry = self._tio(project_root).find_ticket(slug)
        return entry.get("status", "") if entry else ""

    # -- State-changing ----------------------------------------------------

    def activate(
        self,
        project_root: Path,
        slug: str,
        *,
        owner_pid: int | None = None,
        execution_id: str | None = None,
    ) -> bool:
        return op_activate(
            self._tio(project_root),
            slug,
            owner_pid=owner_pid,
            execution_id=execution_id,
        )

    def claim(self, project_root: Path, slug: str) -> bool:
        """Atomically claim a queued ticket. Returns True on success."""
        return op_claim(self._tio(project_root), slug)

    def init_ticket(
        self,
        project_root: Path,
        ticket_path: str,
        *,
        execution_id: str = "",
        owner_pid: int | None = None,
    ) -> dict[str, Any]:
        tio = self._tio(project_root)
        result = tio.init_ticket(
            ticket_path,
            execution_id=execution_id,
            owner_pid=owner_pid,
        )
        if not result:
            raise TicketCLIError("init", 2, f"init failed for '{ticket_path}'")
        return result

    def update_board(
        self,
        project_root: Path,
        slug: str,
        *,
        set_fields: dict[str, str] | None = None,
        append_step: str = "",
        reset_steps: bool = False,
        reset_steps_from: str = "",
        log: bool = False,
    ) -> None:
        # Delegates to the CLI handler to preserve step-gate logic exactly
        from types import SimpleNamespace

        tio = self._tio(project_root)
        args = SimpleNamespace(
            slug=slug,
            set=([f"{k}={v}" for k, v in set_fields.items()] if set_fields else None),
            append_step=append_step,
            reset_steps=reset_steps,
            reset_steps_from=reset_steps_from,
            log=log,
        )
        rc = _cmd_update_board(tio, args)
        if rc != 0:
            raise TicketCLIError("update-board", rc, f"update-board failed for '{slug}'")

    def log_incident(
        self,
        project_root: Path,
        slug: str,
        *,
        incident_type: str,
        step: str,
        description: str,
        resolution: str = "unresolved",
    ) -> None:
        tio = self._tio(project_root)
        tio.locked_append_incident(slug, incident_type, step, description, resolution)

    def block(
        self,
        project_root: Path,
        slug: str,
        *,
        reason: str,
        step: str,
        expected_execution_id: str | None = None,
    ) -> None:
        _check(
            op_block(
                self._tio(project_root),
                slug,
                reason,
                step,
                expected_execution_id=expected_execution_id,
            ),
            "block",
            slug,
        )

    def fail(self, project_root: Path, slug: str, *, error: str, step: str) -> None:
        _check(op_fail(self._tio(project_root), slug, error, step), "fail", slug)

    def handoff(
        self,
        project_root: Path,
        slug: str,
        *,
        expected_execution_id: str | None = None,
    ) -> None:
        _check(
            op_handoff(
                self._tio(project_root),
                slug,
                expected_execution_id=expected_execution_id,
            ),
            "handoff",
            slug,
        )

    def unblock(
        self,
        project_root: Path,
        slug: str,
        *,
        feedback: str = "",
        actor: str = "ticket-triage",
        detail: str = "user answered questions",
        feedback_heading: str = "Human Response",
    ) -> bool:
        return op_unblock(
            self._tio(project_root),
            slug,
            feedback,
            actor=actor,
            detail=detail,
            feedback_heading=feedback_heading,
        )

    def promote_waiting(self, project_root: Path) -> str:
        promoted = op_promote_waiting(self._tio(project_root))
        if promoted:
            lines = [f"Newly executable tickets ({len(promoted)}):"]
            for p in promoted:
                lines.append(f"  - {p['summary']} ({p['slug']})")
            return "\n".join(lines)
        return "No waiting tickets are newly executable."

    def validate_logs(self, project_root: Path, slug: str) -> tuple[bool, str]:
        tio = self._tio(project_root)
        entry = tio.find_ticket(slug)
        if not entry:
            raise TicketCLIError("validate-logs", 1, f"Ticket '{slug}' not found")
        ticket_type = entry.get("type", "feature")
        steps_completed = entry.get("steps_completed", [])
        ticket_fields = {}
        ticket_path = tio.logs_dir / slug / "ticket.md"
        if ticket_path.exists():
            tio.load_basis(slug, runtime_ticket_path=ticket_path)
            with ticket_path.open(encoding="utf-8") as f:
                with ticket_conversion_context(project_root, slug, "executable") as context:
                    converted = convert_ticket_document(f.read(), context)
                if converted.document is None:
                    raise TicketCLIError("validate-logs", 1, "Ticket snapshot is invalid")
                ticket_fields = {**converted.document.spec.fields, **converted.document.generated}
        result = tb_validate_logs(tio.logs_dir, slug, ticket_type, steps_completed, ticket_fields)
        report, error_count = format_validate_logs_report(result, slug)
        return error_count == 0, report

    def timing(self, project_root: Path, slug: str, *, save: bool = False) -> str:
        tio = self._tio(project_root)
        transitions_path = existing_human_log_file(tio.logs_dir, slug, "transitions.log")
        if not transitions_path.exists():
            raise TicketCLIError("timing", 2, f"transitions.log not found for {slug}")
        transitions = parse_transitions_log(transitions_path)
        end_time = _resolve_end_time(tio, slug)
        durations = compute_step_durations(transitions, end_time=end_time)
        step_meta: dict = {}
        _enrich_step_meta_with_tokens(tio, slug, step_meta, transitions)
        title = f"Step Timing -- {slug}"
        report = format_timing_report(durations, title, step_meta=step_meta or None)
        if save:
            out_path = tio.logs_dir / slug / "timing.md"
            out_path.parent.mkdir(parents=True, exist_ok=True)
            with out_path.open("w", encoding="utf-8") as f:
                f.write(report + "\n")
        return report

    def generate_slug(self, project_root: Path, summary: str) -> str:
        from booley.ticket_board.helpers import generate_slug as tb_generate_slug

        return tb_generate_slug(summary)

    def create_ticket_file(
        self, project_root: Path, slug: str, params: CreateTicketParams
    ) -> Path:
        tio = self._tio(project_root)
        content = Path(params.document_file).read_text(encoding="utf-8")
        result = tio.create_ticket_document(slug, content)
        if result is None:
            raise TicketCLIError("create-file", 2, f"create-file failed for '{slug}'")
        return result

    def enqueue(self, project_root: Path, slug: str) -> None:
        tio = self._tio(project_root)
        ok = tio.enqueue_ticket(slug)
        if not ok:
            raise TicketCLIError("enqueue", 2, f"enqueue failed for '{slug}'")

    def complete(self, project_root: Path, slug: str) -> None:
        tio = self._tio(project_root)
        ok = op_complete(tio, slug)
        if not ok:
            raise TicketCLIError("complete", 1, f"complete failed for '{slug}'")


def _resolve_end_time(tio, slug: str):
    """Resolve end_time for timing from ticket status."""

    entry = tio.find_ticket(slug)
    if entry and entry.get("status") in SETTLED_STATUSES:
        last_update = entry.get("last_update", "")
        if last_update:
            with contextlib.suppress(ValueError, TypeError):
                return parse_timestamp(last_update)
    return None


def _enrich_step_meta_with_tokens(
    tio,
    slug: str,
    step_meta: dict,
    transitions: list,
) -> None:
    """Merge token usage from step-level, transcript, or JSONL sources into step_meta."""
    usage_entries = collect_step_usage(tio.logs_dir, slug)
    if usage_entries:
        _merge_usage_into_meta(usage_entries_to_steps(usage_entries), step_meta)
        return

    step_transcript_usage = collect_step_transcript_usage(tio.logs_dir, slug)
    if step_transcript_usage:
        _merge_usage_into_meta(step_transcript_usage, step_meta)
        return

    # Fall back to transcript.jsonl attribution
    transcript_path = tio.logs_dir / slug / "transcript.jsonl"
    if not transcript_path.exists() or step_meta is None:
        return
    try:
        all_messages = collect_all_messages(transcript_path)
        step_tokens = attribute_tokens_to_steps(all_messages, transitions)
        _merge_usage_into_meta(step_tokens, step_meta)
    except (OSError, KeyError, TypeError, ValueError) as exc:
        logger.debug("Token attribution failed: %s", exc)


def _merge_usage_into_meta(step_usage: dict, step_meta: dict) -> None:
    """Write token totals from a step_usage dict into step_meta."""
    for sname, totals in step_usage.items():
        total_tok = totals.get("input_tokens", 0) + totals.get("output_tokens", 0)
        if total_tok > 0:
            step_meta.setdefault(sname, {})["tokens"] = total_tok


# ---------------------------------------------------------------------------
# Module-level accessor (dependency injection point)
# ---------------------------------------------------------------------------

_ops: TicketOps | None = None


def get_ticket_ops() -> TicketOps:
    """Return the active TicketOps instance (default: DirectTicketOps)."""
    global _ops
    if _ops is None:
        _ops = DirectTicketOps()
    return _ops


def set_ticket_ops(ops: TicketOps | None) -> None:
    """Replace the active TicketOps instance (use None to reset to default)."""
    global _ops
    _ops = ops
