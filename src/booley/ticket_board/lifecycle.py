"""Canonical ticket lifecycle: states, their names, and the legal transitions.

Historically the ticket board's state lived implicitly across the codebase:
a hand-maintained ``DIR_STATUS_MAP``, two *divergent* ``BOARD_STATES`` tuples,
a partial user-move matrix plus a duplicated help string in ``op_board_move``,
and bare ``status ==`` literals scattered across the package. That drift already
showed up as bugs (a fast-fail path that polled a state it could never leave;
stale terminal results replayed) — the failure class an explicit lifecycle
prevents.

This module is the vocabulary and the transition validator. Where state is
*stored* is ADR 0065 (``docs/adr/0065-store-ticket-state-outside-the-ticket-document-path.md``):
each live Ticket keeps the stable document path ``board/<slug>.md`` and its
state lives in the per-Ticket state record ``state/<slug>.json``, which
:mod:`booley.ticket_board.board_layout` owns. A transition is one atomic
replace of that record under the per-ticket lock.

Pure-stdlib leaf: imports nothing from ``booley`` so any layer (``constants``,
``harness``) can depend on it without an import cycle.
"""

from __future__ import annotations

from enum import Enum
from typing import Literal

# Ticket document conversion stage: a draft or a published executable Ticket.
ConversionStage = Literal["draft", "executable"]


class TicketState(Enum):
    """A ticket's lifecycle state, pairing its board name with its status.

    Each member carries ``(dir_name, status)``. ``dir_name`` is the board name
    users type as a move destination (``move-ticket --to queue``) and was the
    state's directory before ADR 0065; ``status`` is what state records store.
    The historical skew — ``active`` means ``running``, ``queue`` means
    ``queued`` — is encoded exactly ONCE, here. Member order is lifecycle
    order, which board listings follow.
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


# Legal transition graph -----------------------------------------------------
#
# The complete set of lifecycle edges the ticket operations actually perform.
# Each edge is annotated with the operation(s) that drive it. This consolidates
# rules that were previously implicit across op_activate / op_block / op_handoff
# / op_requeue / op_unblock / op_complete / op_promote_waiting / op_archive.
#
# Enforcement happens twice: ``op_board_move`` validates the narrower set of
# human-requested moves, while ``TicketIO.move_and_update`` validates normal
# harness moves from the source state recorded under the per-ticket lock.
# Admin escape hatches (reset/archive) deliberately use separate primitives.
#
# op_archive(slug) abandons a live ticket from any status except done — that
# admin escape hatch is intentionally NOT modelled as an edge from every state
# (it would dilute the normal-lifecycle graph); it closes the Ticket into
# Ticket History instead of writing an archived state record.
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
    # done and archived close the Ticket into Ticket History (ticket_history);
    # a done Ticket stays on the board only until its completion finishes.
    TicketState.DONE: frozenset(),
    TicketState.ARCHIVED: frozenset(),
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
# The directory of live Ticket documents under ``<tickets_dir>/``. The layout
# functions live in :mod:`booley.ticket_board.board_layout`.

BOARD_DIR_NAME = "board"
