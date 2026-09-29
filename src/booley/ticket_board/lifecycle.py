"""Canonical ticket lifecycle: the single source of truth for board state.

Historically the ticket board's state lived implicitly across the codebase:
a hand-maintained ``DIR_STATUS_MAP``, two *divergent* ``BOARD_STATES`` tuples
(``harness/init_cmd.py`` vs ``harness/doctor.py``), a partial user-move matrix
plus a duplicated help string in ``op_board_move``, and ~11 bare ``status ==``
literals scattered across the package. That drift already showed up as bugs
(a fast-fail path that polled a state it could never leave; stale terminal
results replayed) — the exact failure class an explicit lifecycle prevents.

This module is a **read-model + transition validator**, NOT a new authoritative
store. The filesystem — which ``board/<dir>/`` a ticket's ``.md`` lives in —
remains the single atomic source of truth, mutated under the per-ticket lock by
both the CLI and the harness (sometimes across the daemon/privilege boundary,
under concurrent runners). :class:`TicketState` is *derived from* that directory;
the directory move stays the one commit of a transition. Making the enum
authoritative would be the risky path — this deliberately does not.

This module also owns the **Ticket Board layout**: every caller that needs to
know where a Ticket document lives, which state a document path represents, or
which documents sit in a state goes through the board-layout functions below
(:func:`ticket_document_path`, :func:`document_state`, :func:`locate_document`,
:func:`documents_in_state`, :func:`iter_board_documents`). No other module
joins ``board/<state>`` paths, so the storage of state can change here alone.

Pure-stdlib leaf: imports nothing from ``booley`` so any layer (``constants``,
``harness``) can depend on it without an import cycle.
"""

from __future__ import annotations

from collections.abc import Iterator
from enum import Enum
from pathlib import Path
from typing import Literal

# Ticket document conversion stage: a draft or a published executable Ticket.
ConversionStage = Literal["draft", "executable"]


class TicketState(Enum):
    """A ticket's lifecycle state, pairing its board directory with its status.

    Each member carries ``(dir_name, status)`` so the historical name skew —
    directory ``active`` maps to status ``running``, directory ``queue`` maps to
    status ``queued`` — is encoded exactly ONCE, here, instead of in a
    hand-kept ``DIR_STATUS_MAP`` plus two board-state tuples that had already
    diverged. Member order matches the legacy ``TICKET_DIRS`` list so anything
    that iterates it is byte-for-byte unchanged.
    """

    DRAFT = ("drafts", "draft")
    QUEUED = ("queue", "queued")
    WAITING = ("waiting", "waiting")
    RUNNING = ("active", "running")
    BLOCKED = ("blocked", "blocked")
    REVIEW = ("review", "review")
    DONE = ("done", "done")
    ARCHIVED = ("archived", "archived")

    def __init__(self, dir_name: str, status: str) -> None:
        self.dir_name = dir_name
        self.status = status

    @property
    def conversion_stage(self) -> ConversionStage:
        """Ticket document conversion stage: ``draft`` for drafts, else ``executable``."""
        return "draft" if self is TicketState.DRAFT else "executable"

    @property
    def is_terminal(self) -> bool:
        """True for states where the ticket's run has fully concluded.

        ``DONE`` (accepted/merged) and ``ARCHIVED`` are terminal. ``REVIEW`` is
        deliberately NOT terminal — it awaits a human decision and can still
        move on to ``DONE`` or be reset through the separate destructive reset
        operation. See :data:`SETTLED_STATES` for the "run has stopped, timing
        can close" notion that *does* include review.
        """
        return self in (TicketState.DONE, TicketState.ARCHIVED)


# Lookup indexes -------------------------------------------------------------

STATE_BY_STATUS: dict[str, TicketState] = {s.status: s for s in TicketState}
STATE_BY_DIR: dict[str, TicketState] = {s.dir_name: s for s in TicketState}


def parse_board_target(value: str) -> TicketState | None:
    """Resolve a user-facing move destination to its state.

    Accepts a bare directory name (``queue``) or its ``board/``-prefixed form
    (``board/queue``), the spellings ``move-ticket --to`` has always taken.
    Returns ``None`` for anything else.
    """
    return STATE_BY_DIR.get(value.removeprefix(f"{BOARD_DIR_NAME}/"))


def board_target_choices() -> list[str]:
    """Every spelling :func:`parse_board_target` accepts, prefixed forms first."""
    prefixed = [f"{BOARD_DIR_NAME}/{s.dir_name}" for s in TicketState]
    return prefixed + [s.dir_name for s in TicketState]


# "Settled" = the run has ended, so end-time / timing can be closed. Unlike
# is_terminal this INCLUDES review (awaiting the user is a resting state for
# timing). Replaces the ("done", "review") tuple duplicated at two call sites.
SETTLED_STATES: frozenset[TicketState] = frozenset({TicketState.REVIEW, TicketState.DONE})
SETTLED_STATUSES: frozenset[str] = frozenset(s.status for s in SETTLED_STATES)


# Ticket Board states whose directories ``booley init`` must create and
# ``doctor`` must find. Excludes ``archived`` (created on demand), preserving
# the exact set both the legacy init_cmd.BOARD_STATES and
# doctor.required_states hard-coded.
REQUIRED_BOARD_STATES: tuple[TicketState, ...] = tuple(
    s for s in TicketState if s is not TicketState.ARCHIVED
)


# Legal transition graph -----------------------------------------------------
#
# The complete set of lifecycle edges the ticket operations actually perform.
# Each edge is annotated with the operation(s) that drive it. This consolidates
# rules that were previously implicit across op_activate / op_block / op_handoff
# / op_requeue / op_unblock / op_complete / op_promote_waiting / op_archive.
#
# Enforcement happens twice: ``op_board_move`` validates the narrower set of
# human-requested moves, while ``TicketIO.move_and_update`` validates normal
# harness moves from the source directory observed under the per-ticket lock.
# Admin escape hatches (reset/archive) deliberately use separate primitives.
#
# op_archive(slug) can archive a ticket "from any status" — that admin escape
# hatch is intentionally NOT modelled as an edge from every state (it would
# dilute the normal-lifecycle graph); callers that archive out-of-band should
# not route through validate_transition.
TRANSITIONS: dict[TicketState, frozenset[TicketState]] = {
    TicketState.DRAFT: frozenset(
        {TicketState.QUEUED, TicketState.WAITING}  # enqueue (deps met / unmet)
    ),
    TicketState.QUEUED: frozenset(
        {TicketState.RUNNING, TicketState.BLOCKED, TicketState.WAITING}
        # activate/claim; block (e.g. validation); deps-gate back to waiting
    ),
    TicketState.WAITING: frozenset(
        {TicketState.QUEUED, TicketState.BLOCKED}
        # promote_waiting; failed pre-execution Basis Refresh
    ),
    TicketState.RUNNING: frozenset(
        {
            TicketState.REVIEW,  # handoff
            TicketState.DONE,  # handoff destination=done -> complete
            TicketState.QUEUED,  # requeue
            TicketState.BLOCKED,  # block / fail
            TicketState.RUNNING,  # activate resume (self-loop)
        }
    ),
    TicketState.BLOCKED: frozenset(
        {
            TicketState.QUEUED,  # unblock
            TicketState.RUNNING,  # explicit ticket run resumes blocked work
            TicketState.REVIEW,  # board review --request publishes an unaccepted inspection
        }
    ),
    TicketState.REVIEW: frozenset({TicketState.DONE}),  # approve/complete
    TicketState.DONE: frozenset({TicketState.ARCHIVED}),  # archive
    TicketState.ARCHIVED: frozenset(),  # terminal
}


def can_transition(src: TicketState, dst: TicketState) -> bool:
    """Return True if ``src -> dst`` is a legal lifecycle edge."""
    return dst in TRANSITIONS.get(src, frozenset())


def format_transition_error(src: TicketState, dst: TicketState) -> str:
    """Return an actionable error for a rejected normal lifecycle edge."""
    allowed = ", ".join(sorted(state.status for state in TRANSITIONS[src])) or "(none)"
    return (
        f"illegal ticket transition {src.status} -> {dst.status}; "
        f"legal from {src.status}: {allowed}"
    )


# User-initiated board moves ------------------------------------------------
#
# The subset of TRANSITIONS a human may trigger via `update-board move`
# (op_board_move). Ordered for a stable, drift-free help string. The harness
# reaches the other edges through its own operations, not this gate.
USER_BOARD_MOVES: tuple[tuple[TicketState, TicketState], ...] = (
    (TicketState.DRAFT, TicketState.QUEUED),  # enqueue from drafts
    (TicketState.BLOCKED, TicketState.QUEUED),  # unblock (optional feedback)
    (TicketState.REVIEW, TicketState.DONE),  # complete (merge/cleanup overrides)
    (TicketState.RUNNING, TicketState.QUEUED),  # requeue
)


def is_user_board_move(src: TicketState, dst: TicketState) -> bool:
    """True if ``src -> dst`` is a move a human may request via op_board_move."""
    return (src, dst) in USER_BOARD_MOVES


def format_user_board_moves() -> str:
    """Render the legal user moves as ``draft->queue, blocked->queue, ...``.

    Uses ``src.status`` and ``dst.dir_name`` to match the legacy help string
    (e.g. status ``running`` moves to directory ``queue`` -> ``running->queue``).
    """
    return ", ".join(f"{src.status}->{dst.dir_name}" for src, dst in USER_BOARD_MOVES)


# Board layout ----------------------------------------------------------------
#
# Where Ticket documents live under ``<tickets_dir>/board/``. Today each state
# is a directory and a document's state is the directory holding it; these
# functions are the only code that knows that.

BOARD_DIR_NAME = "board"


def board_root(tickets_dir: Path) -> Path:
    """Return ``<tickets_dir>/board``."""
    return Path(tickets_dir) / BOARD_DIR_NAME


def state_directory(tickets_dir: Path, state: TicketState) -> Path:
    """Return the directory holding Ticket documents in *state*."""
    return board_root(tickets_dir) / state.dir_name


def required_board_directories(tickets_dir: Path) -> list[Path]:
    """Return the board directories ``booley init`` creates and doctor requires."""
    return [state_directory(tickets_dir, state) for state in REQUIRED_BOARD_STATES]


def board_relative_document_path(slug: str, state: TicketState) -> Path:
    """Return the document path for *slug* in *state*, relative to the tickets dir."""
    return Path(BOARD_DIR_NAME, state.dir_name, f"{slug}.md")


def ticket_document_path(tickets_dir: Path, slug: str, state: TicketState) -> Path:
    """Return where the document for *slug* lives while the Ticket is in *state*."""
    return Path(tickets_dir) / board_relative_document_path(slug, state)


def _same_directory(left: Path, right: Path) -> bool:
    """Compare directories by resolved path, then by identity across bind mounts.

    A directory that does not exist cannot be the board, so a missing side
    compares unequal. Any other ``OSError`` (for example a permission error
    on the board) propagates: an unreadable board is not an off-board path.
    """
    if left.resolve() == right.resolve():
        return True
    try:
        return left.samefile(right)
    except FileNotFoundError:
        return False


def document_state(tickets_dir: Path, path: Path) -> TicketState | None:
    """Return the state a Ticket document path represents on this board.

    Returns ``None`` for any path that is not a ``.md`` document directly in one
    of this board's state directories: a runtime snapshot such as
    ``logs/<slug>/ticket.md``, a journal candidate, or a user-supplied file
    elsewhere. Raises ``OSError`` when the board cannot be inspected.
    """
    path = Path(path)
    state = STATE_BY_DIR.get(path.parent.name)
    if state is None or path.suffix != ".md":
        return None
    return state if _same_directory(path.parent.parent, board_root(tickets_dir)) else None


def document_stage(
    tickets_dir: Path, path: Path, *, off_board: ConversionStage
) -> ConversionStage:
    """Return the conversion stage for a Ticket document path.

    A board document converts at its state's stage. A path off this board
    (a runtime snapshot, a journal candidate, a user-supplied file) has no
    state, so the caller names the stage it has always used for such paths
    with *off_board*.
    """
    state = document_state(tickets_dir, path)
    return state.conversion_stage if state is not None else off_board


def documents_in_state(tickets_dir: Path, state: TicketState) -> list[Path]:
    """Return the Ticket documents in *state*, sorted by path."""
    directory = state_directory(tickets_dir, state)
    if not directory.is_dir():
        return []
    return sorted(directory.glob("*.md"))


def iter_board_documents(tickets_dir: Path) -> Iterator[tuple[Path, TicketState]]:
    """Yield every Ticket document on the board with its state, in lifecycle order."""
    for state in TicketState:
        for path in documents_in_state(tickets_dir, state):
            yield path, state


def locate_document(tickets_dir: Path, slug: str) -> tuple[Path, TicketState] | None:
    """Return the document and state of the Ticket named *slug*, if any.

    States are searched in lifecycle order, so a slug duplicated across states
    resolves to the earliest one. Matching compares listed file stems rather
    than probing ``<slug>.md`` so a slug can never traverse out of the board
    and case-insensitive filesystems still require the exact spelling.
    """
    for path, state in iter_board_documents(tickets_dir):
        if path.stem == slug:
            return path, state
    return None
