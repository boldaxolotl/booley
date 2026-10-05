"""Commit Ticket History records to the repository that tracks them (ADR 0065).

Closing a Ticket writes ``history/<slug>.md``; Booley then commits that one
file itself as ``chore(<slug>): close Ticket (<outcome>)`` on the branch checked
out in the repository that contains the tickets directory (the inner project
repository when the Project directory is its own repository). When that is the
inner project repository and the Ticket names a ``project_destination_ref``,
the record is committed only while that branch is checked out; otherwise the
commit is refused and retried later rather than landing on another branch.

The commit is recovered from Git state alone; there is no separate journal.
The history file on disk is the intent, and a record is pending while the
checked-out commit does not contain it:

1. check the Project's ``[stealth]`` commit policy (message and identity) when
   history lands in the Project's own repository, the one its Git hooks guard,
   redacting banned phrases from the message first;
   the inner project repository is Booley's state repository, which Booley
   already commits to directly;
2. hash the history file and stage that one path in the user's index, so a
   commit the user makes meanwhile carries the record too;
3. build a commit of the current head's tree plus that blob, using a private
   index so the user's other staged work is never committed;
4. compare-and-swap the branch from the head the commit was built on.

Steps 2-4 run under the acceptance publication lock and only while no
acceptance publication is in flight, because acceptance fails when its
destination branch moves between preparation and publication.

Steps 2-4 are the generic :func:`booley.runtime.history_commit.commit_file`
sequence; this module supplies the Ticket policy around it (repository, message,
commit policy, destination branch check on every attempt, locking, recovery).

A crash after any step is recovered by repeating the sequence: restaging is
idempotent, an unreferenced commit is garbage, and a branch that moved is
rebuilt on its new head (bounded). A failed commit never reopens the Ticket;
:func:`recover_ticket_history` retries it on the next board operation, and
Doctor warns about any record still uncommitted.
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import yaml

from booley.commit_policy.policy import identity_allowed, redact_banned, stealth_policy
from booley.commit_policy.validation import validate_message
from booley.runtime import history_commit
from booley.runtime.file_lock import LockContentionError
from booley.runtime.history_commit import (
    BranchKeptMovingError,
    DetachedHeadError,
    FileCommitError,
)
from booley.runtime.project_repositories import resolve_inner_project_repo

from .board_layout import history_document_path, history_root, history_slug
from .frontmatter import parse_frontmatter
from .ticket_history import (
    ClosedTicket,
    TicketHistoryError,
    closed_ticket_documents,
    finish_closing,
    interrupted_closings,
    read_closed_ticket,
)

if TYPE_CHECKING:
    from .io import TicketIO

_REFLOG_MESSAGE = "booley: close Ticket"


class HistoryCommitError(RuntimeError):
    """A Ticket History record could not be committed; the Ticket stays closed."""


@dataclass(frozen=True)
class HistoryRepository:
    """The repository whose working tree contains Ticket History."""

    worktree: Path
    tickets_dir: Path

    def record_path(self, slug: str) -> str:
        """Return the repository-relative POSIX path of the record for *slug*."""
        path = history_document_path(self.tickets_dir, slug).resolve()
        return path.relative_to(self.worktree).as_posix()

    def history_prefix(self) -> str:
        return history_root(self.tickets_dir).resolve().relative_to(self.worktree).as_posix()


@contextmanager
def _history_errors() -> Iterator[None]:
    """Report a generic commit-mechanics failure as :class:`HistoryCommitError`."""
    try:
        yield
    except FileCommitError as exc:
        raise HistoryCommitError(str(exc)) from exc


def _git(repository: Path, *args: str) -> subprocess.CompletedProcess[str]:
    with _history_errors():
        return history_commit.git(repository, *args)


def _require_git(repository: Path, *args: str) -> str:
    with _history_errors():
        return history_commit.require_git(repository, *args)


def _optional_git(repository: Path, *args: str) -> str | None:
    """Return stdout, or ``None`` when git reports the object or ref is absent."""
    with _history_errors():
        return history_commit.optional_git(repository, *args)


def _containing_repository(tickets_dir: Path) -> HistoryRepository | None:
    """Return the Git working tree containing Ticket History, ignored or not."""
    tickets_dir = Path(tickets_dir)
    if not tickets_dir.is_dir():
        return None
    top = _optional_git(tickets_dir, "rev-parse", "--show-toplevel")
    if not top:
        return None
    repository = HistoryRepository(Path(top).resolve(), tickets_dir)
    try:
        repository.record_path("probe")
    except ValueError:
        return None
    return repository


def _is_ignored(repository: HistoryRepository) -> bool:
    # check-ignore exits 0 when the probe path is ignored.
    probe = repository.record_path("probe")
    return _git(repository.worktree, "check-ignore", "-q", "--", probe).returncode == 0


def history_repository(tickets_dir: Path) -> HistoryRepository | None:
    """Return the repository whose working tree contains Ticket History, if any.

    Returns ``None`` when the tickets directory is not inside a Git working
    tree or the history directory is ignored there: nothing tracks history, so
    nothing is committed (Doctor warns about the ignored case, see
    :func:`history_ignored`).
    """
    repository = _containing_repository(tickets_dir)
    if repository is None or _is_ignored(repository):
        return None
    return repository


def history_ignored(tickets_dir: Path) -> bool:
    """Return whether a Git working tree contains Ticket History but ignores it.

    Closed Tickets' history records are then never committed, so they are
    lost with the checkout.
    """
    repository = _containing_repository(tickets_dir)
    return repository is not None and _is_ignored(repository)


# Inspection ----------------------------------------------------------------------


def pending_history_commits(tickets_dir: Path) -> list[str]:
    """Return slugs of history records the checked-out commit does not contain yet."""
    records = {path.stem for path in closed_ticket_documents(tickets_dir)}
    if not records:
        return []
    repository = history_repository(tickets_dir)
    if repository is None:
        return []
    raw = _require_git(
        repository.worktree,
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=all",
        "--",
        repository.history_prefix(),
    )
    with _history_errors():
        head = history_commit.checked_out_commit(repository.worktree)
        pending: set[str] = set()
        for entry in raw.split("\0"):
            # "XY <path>"; renames add a bare path element, which has no status prefix.
            if len(entry) < 4 or entry[2] != " ":
                continue
            slug = history_slug(tickets_dir, repository.worktree / entry[3:])
            if slug not in records:
                continue
            path = repository.record_path(slug)
            if history_commit.tree_blob(repository.worktree, head, path) is None:
                pending.add(slug)
    return sorted(pending)


# Commit --------------------------------------------------------------------------


def _policy_message(repository: Path, message: str, policy_root: Path) -> str:
    """Return *message* made clean for the Project's commit policy, or refuse.

    Under the Stealth policy every banned phrase (the default list bans
    "ticket") becomes the redaction placeholder, as the ``commit-msg`` hook
    does for subjects; otherwise a Project whose repository tracks history
    could never commit a record. Other policy errors (identity, body length)
    still refuse the commit.
    """
    policy = stealth_policy(policy_root)
    if policy.enabled:
        message = redact_banned(message, policy_root)
    errors = validate_message(message, project_root=policy_root)
    if policy.enabled and policy.allowed_authors:
        for role in ("AUTHOR", "COMMITTER"):
            ident = _require_git(repository, "var", f"GIT_{role}_IDENT")
            name, _, rest = ident.partition(" <")
            email = rest.partition(">")[0]
            if not identity_allowed(name, email, list(policy.allowed_authors)):
                errors.append(f"{role.lower()} not in [stealth] allowed_authors: {name} <{email}>")
    if errors:
        raise HistoryCommitError("commit policy refuses the history commit: " + "; ".join(errors))
    return message


def commit_history_record(tickets_dir: Path, slug: str, *, policy_root: Path) -> bool:
    """Commit ``history/<slug>.md``; return whether a repository tracks it.

    *policy_root* is the Project root; its ``[stealth]`` commit policy applies
    when history is tracked by that repository.
    Idempotent: a record the checked-out commit already holds needs nothing.
    Raises :class:`HistoryCommitError` when the commit cannot be made now.
    """
    repository = history_repository(tickets_dir)
    if repository is None:
        return False
    try:
        closed = read_closed_ticket(tickets_dir, slug)
    except TicketHistoryError as exc:
        raise HistoryCommitError(str(exc)) from exc
    if closed is None:
        raise HistoryCommitError(f"Ticket {slug!r} has no history record to commit")
    message = f"chore({slug}): close Ticket ({closed.block.outcome.status})"
    if repository.worktree == Path(policy_root).resolve():
        message = _policy_message(repository.worktree, message, policy_root)
    destination = _destination_ref(closed, repository, Path(policy_root))
    from .acceptance_journal import AcceptanceOperationError, publication_idle

    try:
        with publication_idle(policy_root):
            _commit_locked(repository, slug, closed.path, message, destination)
    except LockContentionError as exc:
        raise HistoryCommitError("an acceptance is publishing; retrying later") from exc
    except AcceptanceOperationError as exc:
        raise HistoryCommitError(f"{exc}; retrying after it publishes") from exc
    return True


def _destination_ref(closed: ClosedTicket, repository: HistoryRepository, root: Path) -> str:
    """Return the branch ref the record must land on, or ``""`` for the checked-out one.

    ``project_destination_ref`` names a branch of the inner project repository,
    so it binds only when history lives in that repository. A document whose
    frontmatter cannot be parsed (an abandoned malformed draft) never opened a
    paired workspace and names no destination.
    """
    inner = resolve_inner_project_repo(root)
    if inner is None or inner.resolve() != repository.worktree:
        return ""
    try:
        fields, _body = parse_frontmatter(closed.document)
    except (ValueError, yaml.YAMLError):
        return ""
    destination = fields.get("project_destination_ref")
    return destination if isinstance(destination, str) else ""


def _require_destination(worktree: Path, slug: str, ref: str, destination: str) -> None:
    """Refuse to commit a record onto a branch other than its Ticket's destination."""
    if destination and ref != destination:
        raise HistoryCommitError(
            f"{worktree} has {ref} checked out but Ticket {slug!r} closes into "
            f"{destination}; check out {destination} and the commit is retried on the "
            "next Ticket Board operation"
        )


def _commit_locked(
    repository: HistoryRepository, slug: str, record: Path, message: str, destination: str
) -> None:
    worktree = repository.worktree
    path = repository.record_path(slug)
    try:
        history_commit.commit_file(
            worktree,
            path,
            record,
            message,
            # Runs at the start of every attempt, as the branch may change between them.
            check_branch=lambda ref: _require_destination(worktree, slug, ref, destination),
            # Looked up at call time so tests can replace either step.
            build=_build_commit,
            swap=_compare_and_swap,
        )
    except DetachedHeadError as exc:
        raise HistoryCommitError(
            f"{worktree} has a detached HEAD; check out a branch so Booley can "
            "commit Ticket History"
        ) from exc
    except BranchKeptMovingError as exc:
        raise HistoryCommitError(
            f"branch in {worktree} kept moving; Ticket History commit for {slug!r} will be retried"
        ) from exc
    except FileCommitError as exc:
        raise HistoryCommitError(str(exc)) from exc


_build_commit = history_commit.build_commit


def _compare_and_swap(repository: Path, ref: str, commit: str, expected: str | None) -> bool:
    """Move *ref* from *expected* to *commit* with the Ticket close reflog message."""
    return history_commit.compare_and_swap(
        repository, ref, commit, expected, reflog_message=_REFLOG_MESSAGE
    )


# Recovery ------------------------------------------------------------------------


def publish_history_record(tickets_dir: Path, slug: str, *, policy_root: Path) -> bool:
    """Commit one fresh record, warning instead of failing: the Ticket is closed.

    Returns whether the record is committed (or nothing tracks history).
    """
    try:
        commit_history_record(tickets_dir, slug, policy_root=policy_root)
    except HistoryCommitError as exc:
        print(
            f"Warning: Ticket {slug!r} is closed but its history record is not "
            f"committed yet: {exc}. Booley retries on the next board operation.",
            file=sys.stderr,
        )
        return False
    return True


def recover_ticket_history(tio: TicketIO) -> None:
    """Finish interrupted closings, then retry uncommitted history records.

    Runs at the start of board operations that change the board. Failures are
    reported and left for the next run; they never stop the operation that
    triggered recovery.
    """
    tickets_dir = Path(tio.tickets_dir)
    try:
        for slug in interrupted_closings(tickets_dir):
            with tio._ticket_lock(slug):
                finish_closing(tickets_dir, slug)
        pending = pending_history_commits(tickets_dir)
    except (HistoryCommitError, TicketHistoryError, OSError, TimeoutError, ValueError) as exc:
        print(f"Warning: Ticket History recovery failed: {exc}", file=sys.stderr)
        return
    for slug in pending:
        publish_history_record(tickets_dir, slug, policy_root=Path(tio._project_root))
