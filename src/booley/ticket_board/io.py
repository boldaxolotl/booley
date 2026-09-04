"""TicketIO -- filesystem-based ticket operations with directory-as-status."""

from __future__ import annotations

import contextlib
import copy
import hashlib
import logging
import os
import shutil
import sys
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .acceptance_basis import AcceptanceBasis

logger = logging.getLogger(__name__)


@dataclass
class TicketFileSpec:
    """Metadata for creating a ticket file on disk."""

    summary: str
    ticket_type: str
    branch: str
    project_destination_ref: str = ""
    scope: list[str] | None = None
    spec: str = ""
    dependencies: list[str] | None = None
    priority: str = "medium"
    criteria: dict[str, Any] | None = None
    on_success: dict[str, Any] | None = None
    body: str = ""


from .acceptance_journal import acceptance_state
from .constants import RUNTIME_FIELDS, normalize_dir
from .frontmatter import format_frontmatter, parse_frontmatter, update_frontmatter
from .helpers import compute_done_slugs, lock_fd, now_iso, slug_from_file, unlock_fd
from .lifecycle import (
    STATE_BY_DIR,
    STATE_BY_STATUS,
    TicketState,
    can_transition,
    format_transition_error,
)
from .logs import (
    PROGRESS_DEFAULTS,
    load_progress,
    progress_default,
    save_progress,
)
from .logs import (
    append_incident as _append_incident_unlocked,
)
from .paths import (
    existing_runtime_file,
    human_log_file,
    migrate_runtime_file,
    ticket_log_dir,
)

# Extracted to scanner.py — re-export for backward compatibility
from .scanner import find_ticket_file, scan_all_tickets
from .validation import validate_ticket_fields

# ---------------------------------------------------------------------------
# TicketIO class -- thin I/O wrapper (filesystem-based, no board.json)
# ---------------------------------------------------------------------------


class TicketIO:
    """Handles filesystem operations for tickets using directories as source of truth."""

    LOCK_TIMEOUT: int = 30  # seconds

    def __init__(self, tickets_dir: str | Path, project_root: str | Path | None = None) -> None:
        self.tickets_dir = Path(tickets_dir)
        self.logs_dir = self.tickets_dir / "logs"
        # project_root: main repo root, used for file-existence validation.
        # Detect layout: .booley_project/tickets/ (2 levels) vs .booley/project/tickets/ (3 levels)
        if project_root is not None:
            self._project_root = Path(project_root)
        elif "PROJECT_ROOT" in os.environ:
            # Agree with validate-ticket / detect_project_root (QA_REPORT D1):
            # both commands must resolve the SAME root, otherwise validate
            # passes and enqueue rejects the identical ticket. enqueue reached
            # here ignoring PROJECT_ROOT and derived the wrong root.
            self._project_root = Path(os.environ["PROJECT_ROOT"])
        else:
            parent = self.tickets_dir.parent  # .booley_project or project
            if parent.name == ".booley_project":
                root = parent.parent
            else:
                root = parent.parent.parent
            # The Session Runtime can mount the data dir as a top-level sibling
            # (/booley-project, no dot), so the structural walk lands on the
            # filesystem root — never a real project root (QA_REPORT D1).
            # Recover from the cwd, mirroring detect_project_root().
            if root == root.parent:
                cwd = Path.cwd().resolve()
                for cand in [cwd, *cwd.parents]:
                    if (cand / ".booley_project").is_dir():
                        root = cand
                        break
            self._project_root = root

    @staticmethod
    def _resolve_developer_pid():
        """Resolve the PID to stamp in ticket.lock (developer or self)."""
        try:
            from booley.config.project_config import ENV_PREFIX as _proj_env

            _orch_env = f"{_proj_env}_DEVELOPER_PID"
        except (ImportError, AttributeError):
            _orch_env = "BOOLEY_DEVELOPER_PID"
        return os.environ.get(_orch_env) or str(os.getpid())

    def _acquire_lock(self, lock_file, slug, lock_path, pid_to_stamp):
        """Spin-wait to acquire OS-level file lock and stamp PID."""
        deadline = time.monotonic() + self.LOCK_TIMEOUT
        while True:
            try:
                lock_fd(lock_file)
                lock_file.seek(0)
                lock_file.truncate()
                lock_file.write(pid_to_stamp)
                lock_file.flush()
                return
            except BlockingIOError as lock_err:
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        f"Could not acquire lock for '{slug}' within "
                        f"{self.LOCK_TIMEOUT}s -- another process may hold it. "
                        f"Check/remove {lock_path} if stale."
                    ) from lock_err
                time.sleep(0.2)

    @contextlib.contextmanager
    def _ticket_lock(self, slug):
        """Per-ticket OS-level file lock at logs/<slug>/.runtime/ticket.lock.

        Uses msvcrt (Windows) or fcntl (Unix) for real byte-range locking.
        Raises TimeoutError if the lock cannot be acquired within LOCK_TIMEOUT.
        """
        log_dir = ticket_log_dir(self.logs_dir, slug)
        log_dir.mkdir(parents=True, exist_ok=True)
        lock_path = migrate_runtime_file(log_dir, "ticket.lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        locked = False
        pid_to_stamp = self._resolve_developer_pid()
        with lock_path.open("a+", encoding="utf-8") as lock_file:
            try:
                self._acquire_lock(lock_file, slug, lock_path, pid_to_stamp)
                locked = True
                yield
            finally:
                if locked:
                    with contextlib.suppress(OSError):
                        unlock_fd(lock_file)

    def find_ticket(self, slug: str) -> dict[str, Any] | None:
        """Find a ticket by slug, parse frontmatter, return dict with status/file/feature_branch.

        Runtime fields are loaded from .runtime/progress.json when available,
        falling back to frontmatter for backward compatibility.

        Returns dict or None.
        """
        file_path, status = find_ticket_file(self.tickets_dir, slug)
        if file_path is None:
            return None

        with file_path.open(encoding="utf-8") as f:
            text = f.read()
        fields, _ = parse_frontmatter(text)

        # Derive relative file path
        rel = file_path.relative_to(self.tickets_dir)

        entry = dict(fields)
        entry["status"] = status
        entry["file"] = str(rel).replace("\\", "/")
        # Prefer frontmatter feature_branch (matches scan_all_tickets);
        # fall back to filename stem for legacy tickets without the field.
        entry["feature_branch"] = fields.get("feature_branch") or file_path.stem
        journal_state = acceptance_state(self.tickets_dir, file_path.stem)
        if journal_state is not None:
            entry["acceptance_state"] = str(journal_state)

        # Overlay runtime fields from .runtime/progress.json (backward compat fallback)
        progress = load_progress(self.logs_dir, file_path.stem)
        if progress is not None:
            for k in RUNTIME_FIELDS:
                entry[k] = progress.get(k, progress_default(k))
        else:
            # Backward compat defaults when no .runtime/progress.json exists
            if "steps_completed" not in entry:
                entry["steps_completed"] = []
            if "step" not in entry:
                entry["step"] = ""

        return entry

    def load_basis(
        self, slug: str, *, runtime_ticket_path: str | Path | None = None
    ) -> AcceptanceBasis:
        """Load an executable basis from Board authority and cross-check its snapshot."""
        with self._ticket_lock(slug):
            return self._load_basis_unlocked(slug, runtime_ticket_path=runtime_ticket_path)

    def _load_basis_unlocked(
        self, slug: str, *, runtime_ticket_path: str | Path | None = None
    ) -> AcceptanceBasis:
        from .acceptance_basis import AcceptanceBasisError, load_acceptance_basis

        board_path, status = find_ticket_file(self.tickets_dir, slug)
        if board_path is None or status in {None, "draft"}:
            raise AcceptanceBasisError(f"executable Board Ticket {slug!r} is unavailable")
        board_fields, board_body = parse_frontmatter(board_path.read_text(encoding="utf-8"))
        basis = load_acceptance_basis(self._project_root, slug, board_fields, board_body)
        if runtime_ticket_path is None:
            return basis
        snapshot_path = Path(runtime_ticket_path)
        snapshot_fields, snapshot_body = parse_frontmatter(
            snapshot_path.read_text(encoding="utf-8")
        )
        snapshot = load_acceptance_basis(
            self._project_root,
            slug,
            snapshot_fields,
            snapshot_body,
        )
        if snapshot.as_dict() != basis.as_dict():
            raise AcceptanceBasisError(
                "acceptance-input-change-required: runtime Ticket snapshot names another basis"
            )
        return basis

    def _load_or_bootstrap_progress(self, slug, file_path):
        """Load .runtime/progress.json, bootstrapping from frontmatter if missing."""
        progress = load_progress(self.logs_dir, slug)
        if progress is None:
            with file_path.open(encoding="utf-8") as f:
                text = f.read()
            fields, _ = parse_frontmatter(text)
            progress = {k: fields.get(k, progress_default(k)) for k in RUNTIME_FIELDS}
        return progress

    def _apply_updates(self, progress, updates, append_step=None):
        """Partition updates into spec vs runtime, apply to progress. Returns spec_updates dict."""
        spec_updates = {}
        for k, v in updates.items():
            if k in RUNTIME_FIELDS:
                progress[k] = progress_default(k) if v is None or v == "" else v
            else:
                spec_updates[k] = v
        if append_step:
            stages = progress.get("steps_completed", [])
            if not isinstance(stages, list):
                stages = []
            if append_step not in stages:
                # Enforce monotonic forward progress: new stage must come
                # after all existing completed steps in STEP_ORDER order.
                from .constants import STEP_ORDER

                if stages:
                    try:
                        last_idx = STEP_ORDER.index(stages[-1])
                        new_idx = STEP_ORDER.index(append_step)
                        if new_idx <= last_idx:
                            import logging

                            logging.getLogger(__name__).warning(
                                "Non-monotonic step append: %s (idx %d) after %s (idx %d)",
                                append_step,
                                new_idx,
                                stages[-1],
                                last_idx,
                            )
                    except ValueError:
                        pass  # step not in STEP_ORDER — skip check
                stages.append(append_step)
            progress["steps_completed"] = stages
        progress["last_update"] = now_iso()
        return spec_updates

    def _write_spec_fields(self, file_path, spec_updates):
        """Write spec field updates to frontmatter (atomic via temp+rename)."""
        if not spec_updates:
            return
        update_frontmatter(file_path, spec_updates)

    def move_ticket_file(self, slug: str, to_dir: str) -> bool:
        """Move a ticket .md file to a different directory (queue, active, etc.)."""
        with self._ticket_lock(slug):
            file_path, _ = find_ticket_file(self.tickets_dir, slug)
            if file_path is None:
                print(f"Error: ticket '{slug}' not found", file=sys.stderr)
                return False

            new_dir = self.tickets_dir / normalize_dir(to_dir)
            new_dir.mkdir(parents=True, exist_ok=True)
            new_path = new_dir / file_path.name
            if new_path.exists() and new_path != file_path:
                print(f"Error: destination already exists: {new_path}", file=sys.stderr)
                return False
            shutil.move(str(file_path), str(new_path))
        return True

    def _validated_destination(
        self,
        slug: str,
        file_path: Path,
        source_status: str | None,
        to_dir: str,
        *,
        enforce_lifecycle: bool,
    ) -> tuple[Path, TicketState | None, TicketState | None] | None:
        """Resolve a conflict-free destination and validate its lifecycle edge."""
        normalized = normalize_dir(to_dir)
        source = STATE_BY_STATUS.get(source_status or "")
        destination = STATE_BY_DIR.get(Path(normalized).name)
        if enforce_lifecycle:
            if source is None or destination is None:
                print(
                    f"Error: cannot resolve lifecycle move for '{slug}': "
                    f"{source_status!r} -> {to_dir!r}",
                    file=sys.stderr,
                )
                return None
            if not can_transition(source, destination):
                print(f"Error: {format_transition_error(source, destination)}", file=sys.stderr)
                return None
        new_dir = self.tickets_dir / normalized
        new_path = new_dir / file_path.name
        if new_path.exists() and new_path != file_path:
            print(f"Error: destination already exists: {new_path}", file=sys.stderr)
            return None
        return new_path, source, destination

    @staticmethod
    def _canonical_transition(
        transition: tuple[str, str, str, str] | None,
        source: TicketState | None,
        destination: TicketState | None,
    ) -> tuple[str, str, str, str] | None:
        """Replace possibly stale status prefixes with the locked move states."""
        if transition is None or source is None or destination is None:
            return transition

        def with_status(value: str, status: str) -> str:
            _old, separator, suffix = value.partition(":")
            return f"{status}:{suffix}" if separator else status

        from_state, to_state, actor, detail = transition
        return (
            with_status(from_state, source.status),
            with_status(to_state, destination.status),
            actor,
            detail,
        )

    def move_and_update(
        self,
        slug: str,
        to_dir: str,
        updates: dict[str, Any],
        append_step: str | None = None,
        transition: tuple[str, str, str, str] | None = None,
        *,
        enforce_lifecycle: bool = False,
        expected_status: str | None = None,
        expected_execution_id: str | None = None,
        before_move: Callable[[], bool] | None = None,
    ) -> bool:
        """Atomic move + field update under per-ticket lock.

        Runtime fields are routed to .runtime/progress.json.
        Spec fields are written to frontmatter before the file move.

        Args:
            transition: Optional (from_state, to_state, actor, detail) tuple.
                        If provided, the transition is logged inside the lock
                        to prevent interleaved writes under concurrent
                        execution (docs/PRINCIPLES §7).
            enforce_lifecycle: Derive the locked source state and reject moves
                               outside the canonical lifecycle graph.
            expected_status: Compare-and-swap guard. When set, reject the move
                             unless the locked filesystem state still matches.
            expected_execution_id: Reject unless the locked execution generation
                                   matches the caller's activation generation.
            before_move: Optional integrity callback run after compare-and-swap
                         validation while the per-ticket lock is still held.

        Returns True on success, False if ticket not found.
        """
        with self._ticket_lock(slug):
            # Find ticket inside lock to avoid TOCTOU race
            file_path, source_status = find_ticket_file(self.tickets_dir, slug)
            if file_path is None:
                print(f"Error: ticket '{slug}' not found after lock", file=sys.stderr)
                return False
            if expected_status is not None and source_status != expected_status:
                print(
                    f"Error: ticket '{slug}' changed concurrently: expected "
                    f"{expected_status}, found {source_status}",
                    file=sys.stderr,
                )
                return False

            progress = self._load_or_bootstrap_progress(slug, file_path)
            actual_execution_id = progress.get("execution_id", "")
            if expected_execution_id is not None and actual_execution_id != expected_execution_id:
                print(
                    f"Error: ticket '{slug}' execution changed concurrently: expected "
                    f"{expected_execution_id}, found {actual_execution_id or '<none>'}",
                    file=sys.stderr,
                )
                return False

            resolved = self._validated_destination(
                slug,
                file_path,
                source_status,
                to_dir,
                enforce_lifecycle=enforce_lifecycle,
            )
            if resolved is None:
                return False
            new_path, source, destination = resolved
            transition = self._canonical_transition(transition, source, destination)
            if before_move is not None and not before_move():
                return False

            spec_updates = self._apply_updates(progress, updates, append_step)
            save_progress(self.logs_dir, slug, progress)
            self._write_spec_fields(file_path, spec_updates)

            # Move file (.runtime/progress.json stays in logs/<slug>/.runtime/)
            new_dir = new_path.parent
            new_dir.mkdir(parents=True, exist_ok=True)
            if file_path.exists():
                shutil.move(str(file_path), str(new_path))

            if transition:
                self._append_transition_unlocked(slug, *transition)

        return True

    def _init_ticket_locked(
        self,
        ticket_path: Path,
        slug: str,
        execution_id: str,
        owner_pid: int | None,
    ) -> Path:
        """Perform the locked init work: move, copy, stamp, create progress.

        Returns the log_dir path.
        """
        with ticket_path.open(encoding="utf-8") as f:
            text = f.read()
        fields, _body = parse_frontmatter(text)

        # Move file to board/active/. Guard on identity, not just string
        # inequality: the source may already BE the destination reached via a
        # different bind mount of the same dir (devcontainer mounts
        # .booley_project at both /booley-project and /work/.booley_project),
        # where a plain shutil.move would raise EXDEV/SameFileError instead of
        # no-op'ing an already-active ticket (ADR 0028).
        active_path = self.tickets_dir / "board" / "active" / ticket_path.name
        active_path.parent.mkdir(parents=True, exist_ok=True)
        already_active = active_path.exists() and ticket_path.samefile(active_path)
        if ticket_path != active_path and not already_active:
            shutil.move(str(ticket_path), str(active_path))

        # Create logs directory and copy ticket
        log_dir = ticket_log_dir(self.logs_dir, slug)
        log_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(active_path), str(log_dir / "ticket.md"))

        # Stamp 'created' in frontmatter (immutable, set once)
        if not fields.get("created"):
            update_frontmatter(active_path, {"created": now_iso()})

        # Create .runtime/progress.json with initial runtime state
        initial_progress = copy.deepcopy(PROGRESS_DEFAULTS)
        initial_progress["step"] = "init"
        initial_progress["steps_completed"] = []
        initial_progress["execution_id"] = execution_id
        initial_progress["execution_owner_pid"] = owner_pid
        initial_progress["last_update"] = now_iso()
        save_progress(self.logs_dir, slug, initial_progress)

        # Log transition (lock already held)
        self._append_transition_unlocked(
            slug, "queued:init", "running:init", "ticket-execute", "picked up"
        )
        return log_dir

    def init_ticket(
        self,
        ticket_path: str | Path,
        *,
        execution_id: str = "",
        owner_pid: int | None = None,
    ) -> dict[str, str] | None:
        """Initialize a fresh ticket: move to active/, create logs/, copy ticket.

        Updates frontmatter with step, steps_completed, created, last_update.
        Returns dict with slug and logs_dir path, or None if the ticket was
        already claimed by another process.
        """
        ticket_path = Path(ticket_path)
        if not ticket_path.exists():
            print(f"Error: ticket file not found: {ticket_path}", file=sys.stderr)
            return None

        # Canonical slug = filename stem (immutable after creation).
        slug = ticket_path.stem
        with ticket_path.open(encoding="utf-8") as stream:
            fields, _body = parse_frontmatter(stream.read())
        contract_errors = self._validate_enqueue_contract(slug, fields)
        if contract_errors:
            print(
                "Error: acceptance-input-change-required: ticket must have a published "
                "Acceptance Basis before fresh execution:",
                file=sys.stderr,
            )
            for error in contract_errors:
                print(f"  - {error}", file=sys.stderr)
            return None

        with self._ticket_lock(slug):
            if not ticket_path.exists():
                print(f"Error: ticket '{slug}' claimed by another process", file=sys.stderr)
                return None
            log_dir = self._init_ticket_locked(ticket_path, slug, execution_id, owner_pid)

        return {"slug": slug, "logs_dir": str(log_dir)}

    def stamp_execution(
        self,
        slug: str,
        execution_id: str,
        owner_pid: int,
        *,
        expected_execution_id: str,
    ) -> bool:
        """Replace an active ticket's execution generation under its lock."""
        with self._ticket_lock(slug):
            file_path, status = find_ticket_file(self.tickets_dir, slug)
            if file_path is None or status != "running":
                return False
            progress = self._load_or_bootstrap_progress(slug, file_path)
            if progress.get("execution_id", "") != expected_execution_id:
                return False
            progress["execution_id"] = execution_id
            progress["execution_owner_pid"] = owner_pid
            progress["last_update"] = now_iso()
            save_progress(self.logs_dir, slug, progress)
        return True

    @staticmethod
    def _build_ticket_fields(spec: TicketFileSpec) -> dict[str, Any]:
        """Build the frontmatter fields dict from a TicketFileSpec."""
        on_success = spec.on_success
        if on_success is None:
            on_success = {
                "destination": "review",
                "merge": True,
                "cleanup": True,
                "triage_report": True,
                "remove_targets": [],
            }
        fields = {
            "summary": spec.summary,
            "type": spec.ticket_type,
            "branch": spec.branch,
            "scope": spec.scope or [],
            "criteria": spec.criteria or {},
            "on_success": on_success,
            "priority": spec.priority,
        }
        if spec.project_destination_ref:
            fields["project_destination_ref"] = spec.project_destination_ref
        if spec.spec:
            fields["spec"] = spec.spec
        # Legacy escape hatch for pre-authored plans. Normal tickets should let
        # the Developer Agent plan inline (tb_coder writes
        # verification_plan.md in logs/ when it runs).
        if spec.dependencies:
            fields["dependencies"] = spec.dependencies
        return fields

    # Git branch names derived from slugs must fit in filesystem paths;
    # 80 chars keeps worktree paths well under OS limits.
    MAX_SLUG_LEN = 80

    def create_ticket_file(self, slug: str, spec: TicketFileSpec) -> Path | None:
        """Create a new ticket .md file in board/drafts/.

        Returns the Path to the created file, or None if it already exists.
        Does NOT stamp created/last_update -- that's enqueue_ticket's job.

        Uses O_CREAT | O_EXCL for atomic duplicate detection — the
        find_ticket_file scan is kept as a fast-path early-out, but the
        actual creation is race-free.
        """
        if len(slug) > self.MAX_SLUG_LEN:
            print(
                f"Error: slug too long ({len(slug)} chars, max {self.MAX_SLUG_LEN}): "
                f"{slug[:50]}...",
                file=sys.stderr,
            )
            return None
        existing, status = find_ticket_file(self.tickets_dir, slug)
        if existing is not None:
            print(f"Error: ticket '{slug}' already exists ({status}): {existing}", file=sys.stderr)
            return None

        fields = self._build_ticket_fields(spec)
        body = spec.body or "\n## Description\n\nTODO: Add description.\n"

        drafts_dir = self.tickets_dir / "board" / "drafts"
        drafts_dir.mkdir(parents=True, exist_ok=True)
        file_path = drafts_dir / f"{slug}.md"

        created = self._atomic_write_ticket(file_path, fields, body)
        if created is not None and (self._project_root / ".git").exists():
            try:
                from .contract_ops import open_contract

                worktrees = open_contract(self._project_root, created, slug)
                print(f"Ticket workspace: {worktrees.outer}")
                if worktrees.project is not None:
                    print(f"Project workspace: {worktrees.project}")
            except (RuntimeError, ValueError, OSError) as exc:
                print(
                    f"Warning: ticket draft was created but its workspace could not be "
                    f"materialized: {exc}",
                    file=sys.stderr,
                )
        return created

    @staticmethod
    def _atomic_write_ticket(file_path: Path, fields: dict[str, Any], body: str) -> Path | None:
        """Atomically create a ticket file using O_CREAT | O_EXCL."""
        content = format_frontmatter(fields, body)
        try:
            fd = os.open(str(file_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            print(f"Error: ticket file already exists: {file_path}", file=sys.stderr)
            return None
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(content)
        except BaseException:
            file_path.unlink(missing_ok=True)
            raise
        print(f"Created ticket: {file_path}")
        return file_path

    def _resolve_enqueue_path(self, slug):
        """Find ticket file for enqueue, checking it hasn't been stamped yet.

        Returns (ticket_path, False) on success, (None, True) to skip.
        """
        file_path, status = find_ticket_file(self.tickets_dir, slug)
        if file_path is not None:
            with file_path.open(encoding="utf-8") as f:
                fields, _ = parse_frontmatter(f.read())
            if fields.get("created"):
                print(
                    f"Warning: ticket '{slug}' already exists ({status}), skipping enqueue",
                    file=sys.stderr,
                )
                return None, True

        ticket_path = file_path
        if ticket_path is None or not ticket_path.exists():
            for candidate_dir in ("board/drafts", "board/queue"):
                candidate = self.tickets_dir / candidate_dir / f"{slug}.md"
                if candidate.exists():
                    ticket_path = candidate
                    break
        if ticket_path is None or not ticket_path.exists():
            print(f"Error: ticket file not found for '{slug}'", file=sys.stderr)
            return None, True
        return ticket_path, False

    def _check_deps(self, slug, deps):
        """Check for circular deps and unmet deps. Returns (has_unmet, error)."""
        if not deps:
            return False, False
        all_tickets = scan_all_tickets(self.tickets_dir)
        cycle = self._detect_dep_cycle(slug, deps, all_tickets=all_tickets)
        if cycle:
            print(
                f"Error: circular dependency detected: {' -> '.join(cycle)} — refusing to enqueue",
                file=sys.stderr,
            )
            return False, True
        done_slugs = compute_done_slugs(all_tickets)
        return not all(d in done_slugs for d in deps), False

    def _finish_enqueue_publication(self, journal) -> bool:
        """Roll a prepared enqueue forward through its exact-once side effects."""
        from .enqueue_publication import (
            finish_enqueue,
            publish_enqueue,
            write_enqueue_journal,
        )

        self._validate_enqueue_journal_basis(journal)
        if journal.state == "prepared":
            journal = publish_enqueue(self._project_root, journal)
        initial_progress = copy.deepcopy(PROGRESS_DEFAULTS)
        initial_progress["last_update"] = journal.created
        save_progress(self.logs_dir, journal.slug, initial_progress)
        if journal.state == "published":
            self._append_enqueue_transition_once(journal)
            journal = journal.with_state("transitioned")
            write_enqueue_journal(self._project_root, journal)
        finish_enqueue(self._project_root, journal)
        return True

    def _validate_enqueue_journal_basis(self, journal) -> None:
        """Revalidate immutable basis evidence before resuming publication."""
        from .acceptance_basis import AcceptanceBasis, load_basis_receipt
        from .contract_ops import validate_basis_refs

        basis = AcceptanceBasis.from_mapping(journal.basis)
        receipt = load_basis_receipt(self._project_root, journal.slug, journal.basis)
        if receipt != journal.receipt:
            raise RuntimeError("enqueue journal receipt differs from write-once evidence")
        outer = basis.participant("outer")
        errors = validate_basis_refs(
            self._project_root,
            basis,
            slug=journal.slug,
            destination_branch=outer.destination_ref.removeprefix("refs/heads/"),
            exact_ticket_heads=True,
            exact_destination_heads=True,
        )
        if errors:
            raise RuntimeError("enqueue journal Acceptance Basis is invalid: " + "; ".join(errors))

    def _append_enqueue_transition_once(self, journal) -> None:
        marker = f"enqueue operation {journal.operation_id}"
        path = human_log_file(self.logs_dir, journal.slug, "transitions.log")
        if path.exists() and marker in path.read_text(encoding="utf-8"):
            return
        state = "waiting:init" if journal.has_unmet else "queued:init"
        detail = "ticket created (waiting on deps)" if journal.has_unmet else "ticket created"
        self._append_transition_unlocked(
            journal.slug, "---", state, "ticket-create", f"{detail}; {marker}"
        )

    def enqueue_ticket(
        self,
        slug: str,
        summary: str | None = None,
        ticket_type: str | None = None,
        branch: str | None = None,
        on_success: dict | None = None,
        integration_base: str = "",
    ) -> bool:
        """Stamp created/last_update, validate, and move to queue/ or waiting/.

        Returns True/False.
        """
        if self._retired_enqueue_argument(integration_base):
            return False
        with self._ticket_lock(slug):
            from .enqueue_publication import load_enqueue_journal

            pending = load_enqueue_journal(self._project_root, slug)
            if pending is not None:
                return self._finish_enqueue_publication(pending)
            ticket_path, skip = self._resolve_enqueue_path(slug)
            if skip or ticket_path is None:
                return False
            prepared = self._prepare_enqueue_fields(slug, ticket_path, on_success)
            if prepared is None:
                return False
            fields, body, receipt = prepared
            has_unmet, dep_error = self._check_deps(slug, fields.get("dependencies", []))
            if dep_error:
                return False
            journal = self._prepare_enqueue_publication(
                slug, ticket_path, fields, body, has_unmet, receipt
            )
            return self._finish_enqueue_publication(journal)

    @staticmethod
    def _retired_enqueue_argument(integration_base: str) -> bool:
        if not integration_base:
            return False
        print(
            "Error: --integration-base is retired; Acceptance Basis Tickets publish "
            "their recorded refs directly to destination refs",
            file=sys.stderr,
        )
        return True

    def _prepare_enqueue_fields(
        self, slug: str, ticket_path: Path, on_success: dict[str, Any] | None
    ) -> tuple[dict[str, Any], str, dict[str, Any]] | None:
        authored = self._draft_enqueue_fields(ticket_path, on_success)
        if authored is None:
            return None
        effective_fields, body = authored
        if not self._validate_authored_enqueue(slug, ticket_path, effective_fields, body):
            return None
        try:
            from .acceptance_basis import write_basis_receipt
            from .contract_ops import prepare_acceptance_basis

            basis = prepare_acceptance_basis(
                self._project_root,
                ticket_path,
                slug,
                effective_fields=effective_fields,
            )
            receipt = write_basis_receipt(
                self._project_root,
                slug,
                basis,
                source_sha256=hashlib.sha256(ticket_path.read_bytes()).hexdigest(),
                operation_id=uuid.uuid4().hex,
            )
            effective_fields["acceptance_basis"] = basis.as_dict()
        except (RuntimeError, ValueError, OSError) as exc:
            self._print_enqueue_errors("Acceptance Basis publication failed", [str(exc)])
            return None
        contract_errors = self._validate_enqueue_contract(slug, effective_fields)
        if contract_errors:
            self._print_enqueue_errors("ticket Acceptance Basis is invalid", contract_errors)
            return None
        return effective_fields, body, receipt

    def _draft_enqueue_fields(
        self, ticket_path: Path, on_success: dict[str, Any] | None
    ) -> tuple[dict[str, Any], str] | None:
        with ticket_path.open(encoding="utf-8") as handle:
            fields, body = parse_frontmatter(handle.read())
        effective_fields = dict(fields)
        if on_success:
            effective_fields["on_success"] = on_success
        if effective_fields.get("target_contract") is not None:
            self._print_enqueue_errors(
                "unsupported Ticket format",
                [
                    "legacy Target Contract tickets are unsupported after the hard cutoff; "
                    "recreate the Ticket"
                ],
            )
            return None
        if effective_fields.get("acceptance_basis") is not None:
            self._print_enqueue_errors(
                "invalid draft", ["draft Tickets cannot contain an Acceptance Basis"]
            )
            return None
        if not (self._project_root / ".git").exists():
            self._print_enqueue_errors(
                "Acceptance Basis publication failed",
                ["enqueue requires a Git-backed Project; the draft was left unchanged"],
            )
            return None
        return effective_fields, body

    def _validate_authored_enqueue(
        self,
        slug: str,
        ticket_path: Path,
        fields: dict[str, Any],
        body: str,
    ) -> bool:
        if (self._project_root / ".git").exists():
            try:
                from .contract_ops import open_contract

                open_contract(self._project_root, ticket_path, slug)
            except (RuntimeError, ValueError, OSError) as exc:
                self._print_enqueue_errors("Ticket workspace preparation failed", [str(exc)])
                return False
        validation_root = self._enqueue_validation_root(slug, fields)
        results = validate_ticket_fields(
            fields,
            body,
            check_files=(validation_root / ".booley").is_dir(),
            check_git=False,
            project_root=validation_root,
        )
        for warning in results:
            if warning.startswith("[warning] "):
                logger.warning(warning)
        errors = [item for item in results if not item.startswith("[warning] ")]
        if errors:
            self._print_enqueue_errors("ticket validation failed", errors)
            return False
        return True

    def _prepare_enqueue_publication(
        self,
        slug: str,
        ticket_path: Path,
        fields: dict[str, Any],
        body: str,
        has_unmet: bool,
        receipt: dict[str, Any],
    ):
        from .acceptance_basis import AcceptanceBasis
        from .enqueue_publication import prepare_enqueue

        basis = AcceptanceBasis.from_mapping(fields["acceptance_basis"])
        created = now_iso()
        candidate_fields = {**fields, "created": created}
        destination_dir = "waiting" if has_unmet else "queue"
        destination = self.tickets_dir / "board" / destination_dir / ticket_path.name
        content = format_frontmatter(candidate_fields, body).encode()
        return prepare_enqueue(
            self._project_root,
            slug,
            ticket_path,
            destination,
            content,
            has_unmet=has_unmet,
            created=created,
            basis=basis.as_dict(),
            receipt=receipt,
        )

    @staticmethod
    def _print_enqueue_errors(summary: str, errors: list[str]) -> None:
        print(f"Error: {summary}:", file=sys.stderr)
        for error in errors:
            print(f"  - {error}", file=sys.stderr)

    def _enqueue_validation_root(self, slug: str, fields: dict[str, Any]) -> Path:
        """Validate sealed tickets against their immutable authoring checkout."""
        if not (self._project_root / ".git").exists():
            return self._project_root
        from booley.runtime.project_dir import resolve_project_dir

        return resolve_project_dir(self._project_root) / "worktrees" / slug

    def _validate_enqueue_contract(self, slug: str, fields: dict[str, Any]) -> list[str]:
        """Require durable sealed refs before a real Git project becomes executable."""
        if not (self._project_root / ".git").exists():
            return []  # lightweight filesystem-only consumers cannot verify Git identities
        from .acceptance_basis import AcceptanceBasis, AcceptanceBasisError
        from .contract_ops import validate_basis_refs

        if fields.get("target_contract") is not None:
            return ["legacy Target Contract tickets are unsupported after the hard cutoff"]
        raw = fields.get("acceptance_basis")
        if raw is None:
            return ["acceptance_basis is required for executable Tickets"]
        try:
            contract = AcceptanceBasis.from_mapping(raw)
        except AcceptanceBasisError as exc:
            return [str(exc)]
        try:
            return validate_basis_refs(
                self._project_root,
                contract,
                slug=slug,
                destination_branch=str(fields.get("branch", "")),
            )
        except (RuntimeError, ValueError, OSError) as exc:
            return [str(exc)]

    def return_to_draft(self, slug: str) -> dict[str, str]:
        """Preserve a blocked generation and reopen a new draft workspace."""
        from .draft_transition import return_to_draft, transition_pending

        ticket_path, status = find_ticket_file(self.tickets_dir, slug)
        pending = transition_pending(self._project_root, slug)
        if (ticket_path is None or status is None) and not pending:
            raise FileNotFoundError(f"ticket {slug!r} does not exist")
        self._validate_return_to_draft_preconditions(slug, check_owner=not pending)
        with self._ticket_lock(slug):
            self._validate_return_to_draft_preconditions(slug, check_owner=False)
            current_path, current_status = find_ticket_file(self.tickets_dir, slug)
            result = return_to_draft(
                self._project_root,
                current_path or ticket_path or self.tickets_dir / "board/blocked" / f"{slug}.md",
                slug,
                status=current_status or status or "",
                logs_dir=self.logs_dir,
                append_transition=lambda detail: self._append_return_transition_once(slug, detail),
            )
        return result.as_dict()

    def _validate_return_to_draft_preconditions(
        self, slug: str, *, check_owner: bool = True
    ) -> None:
        from booley.harness.job_fence import active_ticket_jobs
        from booley.runtime.pid import is_pid_alive

        from .helpers import read_lock_pid

        lock = existing_runtime_file(self.logs_dir, slug, "ticket.lock")
        owner = read_lock_pid(lock)
        if check_owner and owner is not None and is_pid_alive(owner):
            raise RuntimeError(f"ticket {slug!r} is owned by live process {owner}")
        active = active_ticket_jobs(ticket_log_dir(self.logs_dir, slug))
        if active:
            names = ", ".join(f"{job.endpoint} ({job.run_id})" for job in active)
            raise RuntimeError(f"ticket {slug!r} has active endpoint Jobs: {names}")
        state = acceptance_state(self.tickets_dir, slug)
        if state is not None and state.publication_pending:
            raise RuntimeError(
                f"ticket {slug!r} has an Acceptance publication in progress ({state})"
            )

    def _append_return_transition_once(self, slug: str, detail: str) -> None:
        operation_id = detail.rsplit("; ", maxsplit=1)[-1]
        path = human_log_file(self.logs_dir, slug, "transitions.log")
        if path.exists() and operation_id in path.read_text(encoding="utf-8"):
            return
        self._append_transition_unlocked(slug, "blocked", "draft", "return-to-draft", detail)

    def _detect_dep_cycle(self, slug, deps, all_tickets=None):
        """Check for circular dependencies. Returns cycle path list or None.

        Args:
            all_tickets: Pre-scanned ticket list (avoids redundant scan if caller
                         already has it). If None, scans tickets automatically.
        """
        if all_tickets is None:
            all_tickets = scan_all_tickets(self.tickets_dir)
        dep_map = {}
        for t in all_tickets:
            fb = t.get("feature_branch") or slug_from_file(t.get("file", ""))
            if fb:
                dep_map[fb] = t.get("dependencies", [])
        dep_map[slug] = list(deps)

        # DFS cycle detection
        visited, in_stack = set(), set()

        def dfs(node: str, path: list[str]) -> list[str] | None:
            if node in in_stack:
                return [*path, node]
            if node in visited:
                return None
            visited.add(node)
            in_stack.add(node)
            for dep in dep_map.get(node, []):
                result = dfs(dep, [*path, node])
                if result:
                    return result
            in_stack.discard(node)
            return None

        return dfs(slug, [])

    def _append_transition_unlocked(self, slug, from_state, to_state, actor, detail):
        """Write a transition line — caller MUST already hold _ticket_lock."""
        log_dir = self.logs_dir / slug
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = human_log_file(self.logs_dir, slug, "transitions.log")
        log_file.parent.mkdir(parents=True, exist_ok=True)
        line = f"{now_iso()} | {from_state} -> {to_state} | {actor} | {detail}\n"
        with log_file.open("a", encoding="utf-8") as f:
            f.write(line)

    def append_transition(
        self, slug: str, from_state: str, to_state: str, actor: str, detail: str
    ) -> None:
        """Append a line to logs/<slug>/human-logs/transitions.log.

        Acquires per-ticket lock to prevent interleaved writes under
        concurrent execution (docs/PRINCIPLES §7).  Internal callers that
        already hold the lock should use _append_transition_unlocked.
        """
        with self._ticket_lock(slug):
            self._append_transition_unlocked(slug, from_state, to_state, actor, detail)

    def locked_append_incident(
        self,
        slug: str,
        incident_type: str,
        stage: str,
        description: str,
        resolution: str = "unresolved",
    ) -> int:
        """Append an incident entry under per-ticket lock.

        Wraps the module-level append_incident to prevent the
        read-count-append race on incidents.md.
        """
        with self._ticket_lock(slug):
            return _append_incident_unlocked(
                self.logs_dir, slug, incident_type, stage, description, resolution
            )
