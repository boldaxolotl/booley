"""Ticket History: closing a Ticket and reading Closed Tickets (ADR 0065).

A Ticket closes exactly once, with outcome done or archived. Closing moves its
document from the Ticket Board to ``history/<slug>.md`` in three steps:

1. write the history document: the board document plus a machine-only
   ``closed: {outcome, date, generation}`` block;
2. remove ``board/<slug>.md``;
3. delete the Ticket's state record.

A crash can leave the board document or the state record behind. Presence in
history wins: :func:`finish_closing` (and every later close attempt) removes the
leftovers and never rewrites the history document, so a Ticket's recorded
outcome never changes. Closed Tickets are never reopened, and their slugs stay
taken (:func:`slug_is_closed`).

The ``closed:`` block is always the last key of the document's frontmatter, so
it can be read and stripped textually even when the rest of the frontmatter is
not valid YAML (an abandoned draft can be malformed).
"""

from __future__ import annotations

import contextlib
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from booley.core.boundary import BoundaryError, as_dict, require_dict, require_str_value
from booley.runtime.timefmt import MACHINE_TIMESTAMP_FORMAT, parse_timestamp, utc_now_rfc3339

from . import waiver_candidates
from .board_layout import (
    delete_state_record,
    history_document_path,
    history_documents,
    state_record_path,
    ticket_document_path,
    waiver_candidates_path,
)
from .lifecycle import TicketState
from .persistence import WriteOnceConflictError, atomic_write_once, durable_unlink

logger = logging.getLogger(__name__)

CLOSED_KEY = "closed"
CLOSED_OUTCOMES = frozenset({TicketState.DONE, TicketState.ARCHIVED})
_DELIMITER = "---"
_GENERATION_RE = re.compile(r"(?:[0-9a-f]{16}|[0-9a-f]{32})?")


class TicketHistoryError(RuntimeError):
    """A Ticket History document is missing, malformed, or cannot be written."""


@dataclass(frozen=True)
class ClosedBlock:
    """The machine-only record of how and when a Ticket closed."""

    outcome: TicketState
    date: str
    generation: str = ""

    def __post_init__(self) -> None:
        if self.outcome not in CLOSED_OUTCOMES:
            raise TicketHistoryError(f"closed outcome {self.outcome.status!r} is not closing")
        try:
            canonical = parse_timestamp(self.date).strftime(MACHINE_TIMESTAMP_FORMAT)
        except ValueError as exc:
            raise TicketHistoryError(f"closed date {self.date!r} is invalid") from exc
        if canonical != self.date:
            raise TicketHistoryError(f"closed date {self.date!r} is not canonical UTC")
        if _GENERATION_RE.fullmatch(self.generation) is None:
            raise TicketHistoryError(f"closed generation {self.generation!r} is invalid")

    @classmethod
    def now(cls, outcome: TicketState, generation: str = "") -> ClosedBlock:
        """Return a block closing with *outcome* at the current UTC second."""
        return cls(outcome, utc_now_rfc3339(), generation)

    def render(self) -> str:
        """Return the YAML lines of this block, ending with a newline."""
        value = {
            CLOSED_KEY: {
                "outcome": self.outcome.status,
                "date": self.date,
                "generation": self.generation,
            }
        }
        return yaml.safe_dump(value, sort_keys=False, default_flow_style=False)


@dataclass(frozen=True)
class ClosedTicket:
    """One Closed Ticket read from Ticket History."""

    slug: str
    path: Path
    block: ClosedBlock
    document: str  # the Ticket document as it was when it closed, without the block


# Document text -----------------------------------------------------------------


def _frontmatter_bounds(lines: list[str]) -> tuple[int, int] | None:
    """Return (first, end) line indexes of the frontmatter body, if the text has one."""
    if not lines or lines[0].rstrip("\r\n") != _DELIMITER:
        return None
    for index in range(1, len(lines)):
        if lines[index].rstrip("\r\n") == _DELIMITER:
            return 1, index
    return None


def with_closed_block(document: str, block: ClosedBlock) -> str:
    """Return *document* with *block* appended as the last frontmatter key.

    Readers take the last ``closed:`` key, so an authored key of that name in an
    abandoned draft cannot shadow the block.
    """
    lines = document.splitlines(keepends=True)
    bounds = _frontmatter_bounds(lines)
    if bounds is None:
        return f"{_DELIMITER}\n{block.render()}{_DELIMITER}\n{document}"
    _first, end = bounds
    return "".join([*lines[:end], block.render(), *lines[end:]])


def _split_closed(text: str) -> tuple[str, str]:
    """Split *text* into (document without the closed block, the block's YAML)."""
    lines = text.splitlines(keepends=True)
    bounds = _frontmatter_bounds(lines)
    if bounds is None:
        return text, ""
    first, end = bounds
    starts = [
        index for index in range(first, end) if lines[index].rstrip("\r\n") == f"{CLOSED_KEY}:"
    ]
    if not starts:
        return text, ""
    start = starts[-1]
    block = "".join(lines[start:end])
    remaining = [*lines[:start], *lines[end:]]
    if start == first and remaining[first].rstrip("\r\n") == _DELIMITER:
        # The block was the whole frontmatter: it was prepended to a document
        # that had none, so drop the frontmatter it created.
        remaining = remaining[first + 1 :]
    return "".join(remaining), block


def parse_closed_document(text: str) -> tuple[str, ClosedBlock]:
    """Return (the original document, its closed block) from a history document."""
    document, raw = _split_closed(text)
    if not raw:
        raise TicketHistoryError("history document has no closed block")
    try:
        value = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise TicketHistoryError(f"closed block is not valid YAML: {exc}") from exc
    return document, _parse_block(value)


def _parse_block(value: Any) -> ClosedBlock:
    try:
        block = require_dict(as_dict(value, default={}).get(CLOSED_KEY), field=CLOSED_KEY)
        if set(block) != {"outcome", "date", "generation"}:
            raise BoundaryError("closed block must hold exactly outcome, date and generation")
        outcome = require_str_value(block["outcome"], field="closed outcome")
        date = require_str_value(block["date"], field="closed date")
        generation = require_str_value(
            block["generation"], field="closed generation", allow_empty=True
        )
    except BoundaryError as exc:
        raise TicketHistoryError(str(exc)) from exc
    state = next((item for item in CLOSED_OUTCOMES if item.status == outcome), None)
    if state is None:
        raise TicketHistoryError(f"closed outcome {outcome!r} is invalid")
    return ClosedBlock(state, date, generation)


# Reading -------------------------------------------------------------------------


def read_closed_ticket(tickets_dir: Path, slug: str) -> ClosedTicket | None:
    """Return the Closed Ticket named exactly *slug*, or ``None`` if it never closed.

    A history file without a ``closed:`` block (a README, say) is not a Closed
    Ticket. Raises :class:`TicketHistoryError` when a record's block is malformed.
    """
    for path in history_documents(tickets_dir):
        if path.stem == slug:
            return read_history_record(path) if _is_record(path) else None
    return None


def _text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise TicketHistoryError(f"history document is unreadable: {path}: {exc}") from exc


def _is_record(path: Path) -> bool:
    return bool(_split_closed(_text(path))[1])


def closed_ticket_documents(tickets_dir: Path) -> list[Path]:
    """Return the Ticket History records, skipping (and logging) other files there."""
    records = []
    for path in history_documents(tickets_dir):
        if _is_record(path):
            records.append(path)
        else:
            logger.warning("Ignoring %s: it has no closed block, so it is no Closed Ticket", path)
    return records


def read_history_record(path: Path) -> ClosedTicket:
    """Return the Closed Ticket recorded at *path*; a malformed record raises."""
    text = _text(path)
    try:
        document, block = parse_closed_document(text)
    except TicketHistoryError as exc:
        raise TicketHistoryError(f"history document {path} is invalid: {exc}") from exc
    return ClosedTicket(path.stem, path, block, document)


def closed_outcomes(tickets_dir: Path) -> dict[str, TicketState]:
    """Return the outcome of every Closed Ticket, keyed by slug.

    An unreadable history document or a record with a malformed closed block
    raises: dependency decisions must never treat a Ticket of unknown outcome
    as absent. A history document with no ``closed:`` block records no outcome,
    so a dependency on its slug stays unsatisfied (the Ticket keeps waiting)
    while Ticket Board listings show that document as an error row.
    """
    return {
        path.stem: read_history_record(path).block.outcome
        for path in closed_ticket_documents(tickets_dir)
    }


def done_slugs(tickets_dir: Path) -> set[str]:
    """Return the slugs of Tickets that closed done; only these satisfy dependencies."""
    return {
        slug
        for slug, outcome in closed_outcomes(tickets_dir).items()
        if outcome is TicketState.DONE
    }


def slug_is_closed(tickets_dir: Path, slug: str) -> bool:
    """Return whether *slug* belongs to a Closed Ticket (and so cannot be reused).

    Probes the path directly, so a case-insensitive filesystem also refuses a
    differently cased reuse.
    """
    return history_document_path(tickets_dir, slug).exists()


# Closing -------------------------------------------------------------------------


def close_ticket(tickets_dir: Path, slug: str, block: ClosedBlock) -> ClosedBlock:
    """Close *slug* with *block*; the caller holds the Ticket lock.

    Returns the block Ticket History records. That is *block* unless the Ticket
    had already closed, in which case the earlier record wins and only the
    board leftovers are removed.
    """
    history = history_document_path(tickets_dir, slug)
    if not history.exists():
        board = ticket_document_path(tickets_dir, slug)
        try:
            document = board.read_text(encoding="utf-8")
        except FileNotFoundError as exc:
            raise TicketHistoryError(f"Ticket {slug!r} has no board document to close") from exc
        history.parent.mkdir(parents=True, exist_ok=True)
        # A concurrent close that already recorded an outcome wins.
        with contextlib.suppress(WriteOnceConflictError):
            atomic_write_once(history, with_closed_block(document, block).encode(), mode=0o644)
    recorded = read_history_record(history).block
    finish_closing(tickets_dir, slug)
    return recorded


def finish_closing(tickets_dir: Path, slug: str) -> bool:
    """Remove a Closed Ticket's board document, state record, and candidates (lock held).

    Returns whether anything was left to remove. Safe to repeat.
    """
    removed_document = durable_unlink(ticket_document_path(tickets_dir, slug))
    removed_record = delete_state_record(tickets_dir, slug)
    # ADR 0066: a Closed Ticket's candidates and rejections are disposable state.
    removed_candidates = waiver_candidates.discard(tickets_dir, slug)
    return removed_document or removed_record or removed_candidates


def interrupted_closings(tickets_dir: Path) -> list[str]:
    """Return Closed Tickets that still have a board document or a state record."""
    return [
        path.stem
        for path in closed_ticket_documents(tickets_dir)
        if ticket_document_path(tickets_dir, path.stem).exists()
        or state_record_path(tickets_dir, path.stem).exists()
        or waiver_candidates_path(tickets_dir, path.stem).exists()
    ]


def ticket_generation(root: Path, slug: str, fields: dict[str, Any]) -> str:
    """Return the generation a Closed Ticket records: its machine or draft identity."""
    from .workspace_ops import TicketBaselineOperationError, load_draft_generation

    machine = fields.get("machine")
    if isinstance(machine, dict) and isinstance(machine.get("generation"), str):
        return machine["generation"]
    try:
        return load_draft_generation(root, slug)
    except TicketBaselineOperationError:
        return ""  # A draft that never materialized a workspace has no generation.
