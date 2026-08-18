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

Pure-stdlib leaf: imports nothing from ``booley`` so any layer (``constants``,
``harness``) can depend on it without an import cycle.
"""

from __future__ import annotations

from enum import Enum


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
    def board_dir(self) -> str:
        """``board/``-prefixed directory, e.g. ``TicketState.RUNNING.board_dir`` -> ``board/active``."""
        return f"board/{self.dir_name}"

    @property
    def is_terminal(self) -> bool:
        """True for states where the ticket's run has fully concluded.

        ``DONE`` (accepted/merged) and ``ARCHIVED`` are terminal. ``REVIEW`` is
        deliberately NOT terminal — it awaits a human decision and can still
        move on to ``DONE`` or be requeued. See :data:`SETTLED_STATES` for the
        "run has stopped, timing can close" notion that *does* include review.
        """
        return self in (TicketState.DONE, TicketState.ARCHIVED)


# Lookup indexes -------------------------------------------------------------

STATE_BY_STATUS: dict[str, TicketState] = {s.status: s for s in TicketState}
STATE_BY_DIR: dict[str, TicketState] = {s.dir_name: s for s in TicketState}


# "Settled" = the run has ended, so end-time / timing can be closed. Unlike
# is_terminal this INCLUDES review (awaiting the user is a resting state for
# timing). Replaces the ("done", "review") tuple duplicated at two call sites.
SETTLED_STATES: frozenset[TicketState] = frozenset({TicketState.REVIEW, TicketState.DONE})
SETTLED_STATUSES: frozenset[str] = frozenset(s.status for s in SETTLED_STATES)


# Ticket Board directories that ``booley init`` must create and ``doctor`` must find.
# Excludes ``archived`` (created on demand), preserving the exact set both the
# legacy init_cmd.BOARD_STATES and doctor.required_states hard-coded.
REQUIRED_BOARD_DIRS: tuple[str, ...] = tuple(
    s.dir_name for s in TicketState if s is not TicketState.ARCHIVED
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
    TicketState.WAITING: frozenset({TicketState.QUEUED}),  # promote_waiting
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
        }
    ),
    TicketState.REVIEW: frozenset(
        {TicketState.DONE, TicketState.QUEUED}  # approve/complete; requeue
    ),
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
