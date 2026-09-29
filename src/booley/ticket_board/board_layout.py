"""Ticket Board layout and Ticket state records (ADR 0065).

Every live Ticket document sits at the stable path ``<tickets>/board/<slug>.md``
whatever its state. Lifecycle state lives beside it in an ignored per-Ticket
state record, ``<tickets>/state/<slug>.json``, together with the Ticket's
execution identity and runtime progress fields. The record is the
compare-and-swap target of every transition: callers read it and replace it
while holding the per-Ticket lock, and one atomic replace commits the
transition, the way the old ``board/<state>/`` directory rename did.

A document without a state record is a draft. Only an absent record means
draft: a record that cannot be read or parsed, has an unknown schema, or has
invalid fields raises :class:`StateRecordError`, so every command on that Ticket
fails without changing anything.

No other module joins board or state paths; callers go through the functions
here, or through :class:`booley.ticket_board.io.TicketIO` for locked writes.
"""

from __future__ import annotations

import copy
import json
import logging
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .lifecycle import BOARD_DIR_NAME, STATE_BY_STATUS, ConversionStage, TicketState
from .persistence import atomic_replace_bytes, durable_unlink

logger = logging.getLogger(__name__)

STATE_DIR_NAME = "state"
STATE_RECORD_SCHEMA = 1

# Runtime fields a state record carries next to its state, with their defaults.
# These replaced ``logs/<slug>/.runtime/progress.json``.
RUNTIME_DEFAULTS: dict[str, Any] = {
    "step": "",
    "steps_completed": [],
    "workspace_intent": "fresh",
    "last_update": "",
    "failed_step": None,
    "error": None,
    "blocked_reason": None,
    "blocked_step": None,
    "execution_id": "",
    "execution_owner_pid": None,
}

_REQUIRED_STRINGS = ("step", "workspace_intent", "last_update", "execution_id")
_OPTIONAL_STRINGS = ("failed_step", "error", "blocked_reason", "blocked_step")


class StateRecordError(RuntimeError):
    """A Ticket state record exists but cannot be trusted; nothing was changed."""


def runtime_default(key: str) -> Any:
    """Return a fresh copy of the default value of one runtime field."""
    return copy.deepcopy(RUNTIME_DEFAULTS[key])


@dataclass(frozen=True)
class StateRecord:
    """One Ticket's lifecycle state plus its runtime progress fields.

    ``runtime`` always holds exactly the keys of :data:`RUNTIME_DEFAULTS`.
    Drafts have no record, so ``state`` is never :attr:`TicketState.DRAFT`.
    """

    state: TicketState
    runtime: Mapping[str, Any] = field(default_factory=lambda: copy.deepcopy(RUNTIME_DEFAULTS))

    def __post_init__(self) -> None:
        if self.state is TicketState.DRAFT:
            raise StateRecordError("a draft Ticket has no state record")
        runtime = copy.deepcopy(dict(self.runtime))
        _validate_runtime(runtime)
        object.__setattr__(self, "runtime", runtime)

    @classmethod
    def fresh(cls, state: TicketState, **runtime: Any) -> StateRecord:
        """Return a record in *state* with default runtime fields plus *runtime*."""
        return cls(state, {**copy.deepcopy(RUNTIME_DEFAULTS), **runtime})

    @property
    def execution_id(self) -> str:
        return self.runtime["execution_id"]

    def progress(self) -> dict[str, Any]:
        """Return a mutable copy of the runtime fields."""
        return copy.deepcopy(dict(self.runtime))

    def with_state(self, state: TicketState) -> StateRecord:
        return StateRecord(state, self.runtime)

    def with_runtime(self, runtime: Mapping[str, Any]) -> StateRecord:
        """Return this record with *runtime* merged over its runtime fields."""
        return StateRecord(self.state, {**self.runtime, **runtime})

    def to_json(self) -> dict[str, Any]:
        return {"schema": STATE_RECORD_SCHEMA, "state": self.state.status, **self.runtime}

    def to_bytes(self) -> bytes:
        """Return the canonical on-disk encoding of this record."""
        return (json.dumps(self.to_json(), indent=2, sort_keys=True) + "\n").encode()


def _validate_runtime(runtime: dict[str, Any]) -> None:
    if set(runtime) != set(RUNTIME_DEFAULTS):
        unexpected = sorted(set(runtime) ^ set(RUNTIME_DEFAULTS))
        raise StateRecordError(f"state record has invalid fields: {', '.join(unexpected)}")
    for key in _REQUIRED_STRINGS:
        if not isinstance(runtime[key], str):
            raise StateRecordError(f"state record field {key!r} must be a string")
    for key in _OPTIONAL_STRINGS:
        if runtime[key] is not None and not isinstance(runtime[key], str):
            raise StateRecordError(f"state record field {key!r} must be a string or null")
    steps = runtime["steps_completed"]
    if not isinstance(steps, list) or not all(isinstance(step, str) for step in steps):
        raise StateRecordError("state record field 'steps_completed' must be a list of strings")
    owner = runtime["execution_owner_pid"]
    # bool is an int subclass; a PID is never true/false.
    if owner is not None and (isinstance(owner, bool) or not isinstance(owner, int)):
        raise StateRecordError("state record field 'execution_owner_pid' must be an integer")


def parse_state_record(value: Any) -> StateRecord:
    """Validate decoded JSON as a state record, raising :class:`StateRecordError`."""
    if not isinstance(value, dict):
        raise StateRecordError("state record is not a JSON object")
    schema = value.get("schema")
    if isinstance(schema, bool) or schema != STATE_RECORD_SCHEMA:
        raise StateRecordError(f"state record schema {schema!r} is unsupported")
    status = value.get("state")
    state = STATE_BY_STATUS.get(status) if isinstance(status, str) else None
    if state is None or state is TicketState.DRAFT:
        raise StateRecordError(f"state record state {status!r} is invalid")
    runtime = {key: item for key, item in value.items() if key not in {"schema", "state"}}
    return StateRecord(state, runtime)


# Paths -----------------------------------------------------------------------


def _require_slug(slug: str) -> str:
    """Refuse names that would leave the board or state directory."""
    if not slug or slug in {".", ".."} or Path(slug).name != slug or "\\" in slug:
        raise ValueError(f"invalid Ticket slug: {slug!r}")
    return slug


def board_root(tickets_dir: Path) -> Path:
    """Return ``<tickets_dir>/board``, the directory of live Ticket documents."""
    return Path(tickets_dir) / BOARD_DIR_NAME


def state_root(tickets_dir: Path) -> Path:
    """Return ``<tickets_dir>/state``, the directory of Ticket state records."""
    return Path(tickets_dir) / STATE_DIR_NAME


def required_board_directories(tickets_dir: Path) -> list[Path]:
    """Return the directories ``booley init`` creates and doctor requires."""
    return [board_root(tickets_dir), state_root(tickets_dir)]


def board_relative_document_path(slug: str) -> Path:
    """Return the document path for *slug*, relative to the tickets dir."""
    return Path(BOARD_DIR_NAME, f"{_require_slug(slug)}.md")


def ticket_document_path(tickets_dir: Path, slug: str) -> Path:
    """Return the stable path of the Ticket document for *slug*."""
    return Path(tickets_dir) / board_relative_document_path(slug)


def state_record_path(tickets_dir: Path, slug: str) -> Path:
    """Return the path of the state record for *slug*."""
    return state_root(tickets_dir) / f"{_require_slug(slug)}.json"


# State records -----------------------------------------------------------------


def read_state_record(tickets_dir: Path, slug: str) -> StateRecord | None:
    """Return the state record for *slug*, or ``None`` when it is absent (draft).

    Raises :class:`StateRecordError` for a record that exists but is unreadable,
    malformed, of an unknown schema, or has invalid fields.
    """
    path = state_record_path(tickets_dir, slug)
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise StateRecordError(f"state record for {slug!r} is unreadable: {path}: {exc}") from exc
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise StateRecordError(f"state record for {slug!r} is not valid JSON: {path}") from exc
    try:
        return parse_state_record(value)
    except StateRecordError as exc:
        raise StateRecordError(f"state record for {slug!r} is invalid: {path}: {exc}") from exc


def ticket_state(tickets_dir: Path, slug: str) -> TicketState:
    """Return the lifecycle state of *slug*: its record's state, else draft."""
    record = read_state_record(tickets_dir, slug)
    return TicketState.DRAFT if record is None else record.state


def write_state_record(tickets_dir: Path, slug: str, record: StateRecord) -> None:
    """Atomically replace the state record for *slug*. Caller holds the Ticket lock."""
    atomic_replace_bytes(state_record_path(tickets_dir, slug), record.to_bytes(), mode=0o644)


def delete_state_record(tickets_dir: Path, slug: str) -> bool:
    """Remove the state record for *slug*, making it a draft. Caller holds the lock."""
    return durable_unlink(state_record_path(tickets_dir, slug))


# Board documents ----------------------------------------------------------------


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


def is_board_document(tickets_dir: Path, path: Path) -> bool:
    """Return whether *path* names a Ticket document directly on this board."""
    path = Path(path)
    return path.suffix == ".md" and _same_directory(path.parent, board_root(tickets_dir))


def document_state(tickets_dir: Path, path: Path) -> TicketState | None:
    """Return the state of the Ticket whose board document is *path*.

    Returns ``None`` for any path that is not a ``.md`` document directly on this
    board: a runtime snapshot such as ``logs/<slug>/ticket.md``, a journal
    candidate, or a user-supplied file elsewhere. Raises ``OSError`` when the
    board cannot be inspected and :class:`StateRecordError` for a bad record.
    """
    path = Path(path)
    if not is_board_document(tickets_dir, path):
        return None
    return ticket_state(tickets_dir, path.stem)


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


def board_documents(tickets_dir: Path) -> list[Path]:
    """Return every Ticket document on the board, sorted by path."""
    root = board_root(tickets_dir)
    if not root.is_dir():
        return []
    return sorted(path for path in root.glob("*.md") if path.is_file())


def iter_board_records(tickets_dir: Path) -> Iterator[tuple[Path, StateRecord | None]]:
    """Yield every Ticket document with its state record (``None``: draft).

    Order is lifecycle order, then path. Each record is read once, so a caller
    sees one consistent state and runtime per Ticket. A Ticket whose record is
    corrupt, or whose file name is not a valid slug, is skipped (views report it
    through :func:`unreadable_board_documents`) so one bad entry never hides the
    rest of the board; commands addressed to that Ticket still fail through
    :func:`locate_document`.
    """
    order = {state: index for index, state in enumerate(TicketState)}
    entries, unreadable = _read_board(tickets_dir)
    for path, reason in unreadable:
        # Debug only: board views report these once via unreadable_board_documents,
        # and commands on the Ticket itself fail loudly.
        logger.debug("Skipping Ticket %r: %s", path.stem, reason)

    def sort_key(entry: tuple[Path, StateRecord | None]) -> tuple[int, Path]:
        record = entry[1]
        return (order[TicketState.DRAFT if record is None else record.state], entry[0])

    yield from sorted(entries, key=sort_key)


def unreadable_board_documents(tickets_dir: Path) -> list[tuple[Path, str]]:
    """Return the board documents listings skip, each with the reason, sorted by path.

    These are Tickets whose state record cannot be trusted (or whose file name
    is not a valid slug); views use this to say what they are not showing.
    """
    return _read_board(tickets_dir)[1]


def _read_board(
    tickets_dir: Path,
) -> tuple[list[tuple[Path, StateRecord | None]], list[tuple[Path, str]]]:
    """Read every board document's record once: (readable entries, unreadable ones)."""
    entries: list[tuple[Path, StateRecord | None]] = []
    unreadable: list[tuple[Path, str]] = []
    for path in board_documents(tickets_dir):
        try:
            entries.append((path, read_state_record(tickets_dir, path.stem)))
        except (StateRecordError, ValueError) as exc:
            unreadable.append((path, str(exc)))
    return entries, unreadable


def iter_board_documents(tickets_dir: Path) -> Iterator[tuple[Path, TicketState]]:
    """Yield every Ticket document with its state, in lifecycle order.

    Skips untrustworthy entries exactly like :func:`iter_board_records`.
    """
    for path, record in iter_board_records(tickets_dir):
        yield path, TicketState.DRAFT if record is None else record.state


def documents_in_state(tickets_dir: Path, state: TicketState) -> list[Path]:
    """Return the Ticket documents in *state*, sorted by path."""
    return [path for path, current in iter_board_documents(tickets_dir) if current is state]


def locate_document(tickets_dir: Path, slug: str) -> tuple[Path, TicketState] | None:
    """Return the document and state of the Ticket named *slug*, if any.

    Matching compares listed file stems rather than probing ``<slug>.md`` so a
    slug can never traverse out of the board and case-insensitive filesystems
    still require the exact spelling. Raises :class:`StateRecordError` when the
    Ticket's state record is corrupt.
    """
    for path in board_documents(tickets_dir):
        if path.stem == slug:
            return path, ticket_state(tickets_dir, slug)
    return None
