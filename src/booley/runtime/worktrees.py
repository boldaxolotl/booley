"""Parse Git's ``git worktree list --porcelain`` registration listing.

The parser is pure: callers obtain the listing text through their own Git
runner (each keeps its own working-directory, timeout, and error policy) and
pass it to :func:`parse_worktree_porcelain`. :func:`list_worktrees` is a
convenience for callers whose policy is "run Git with ``-C``, raise on any
failure".

Porcelain format (``git help worktree``): one record per worktree, each a
``worktree <path>`` line followed by attribute lines (``HEAD <sha>``,
``branch <ref>``, ``detached``, ``bare``, ``locked [<reason>]``,
``prunable [<reason>]``), records separated by a blank line. The first record
is the primary worktree.

Paths are taken verbatim from the ``worktree`` line to the end of that
line. Git prints paths raw in this line-based format (only ``locked`` and
``prunable`` reasons are C-quoted), so a path containing a newline cannot
be represented faithfully; that is the format's limitation, and callers that
need such paths use the ``-z`` form with their own parser. Reasons are kept
exactly as Git printed them, quoted or not.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, replace
from pathlib import Path

_LIST_TIMEOUT_S = 30
_WORKTREE_PREFIX = "worktree "
# Attribute keyword -> WorktreeEntry field, by attribute shape.
_VALUE_FIELDS = {"HEAD": "head", "branch": "branch"}  # "<keyword> <value>"
_FLAG_FIELDS = frozenset({"detached", "bare"})  # bare keyword line
_REASON_FIELDS = frozenset({"locked", "prunable"})  # keyword with optional reason


@dataclass(frozen=True)
class WorktreeEntry:
    """One registered worktree as Git reported it.

    ``path`` is the ``worktree`` line's text as Git printed it, not resolved
    and not unquoted. ``locked`` and ``prunable`` are ``None`` when the
    attribute is absent and the (possibly empty, possibly C-quoted) reason
    text when present.
    """

    path: Path
    head: str | None = None
    branch: str | None = None
    detached: bool = False
    bare: bool = False
    locked: str | None = None
    prunable: str | None = None


def parse_worktree_porcelain(text: str) -> tuple[WorktreeEntry, ...]:
    """Return every worktree record in ``text`` in Git's order (primary first).

    A ``worktree`` line starts a new record; a blank line ends the current
    one; a missing final blank line is accepted. Attribute lines outside a
    record, unknown attribute lines, and ``HEAD``/``branch`` lines without a
    value are ignored; when an attribute repeats within a record the last
    value wins. Lines are split with :meth:`str.splitlines`, so CRLF line
    endings are accepted.
    """
    entries: list[WorktreeEntry] = []
    current: WorktreeEntry | None = None
    for line in text.splitlines():
        if line.startswith(_WORKTREE_PREFIX):
            if current is not None:
                entries.append(current)
            current = WorktreeEntry(Path(line.removeprefix(_WORKTREE_PREFIX)))
        elif not line:
            if current is not None:
                entries.append(current)
            current = None
        elif current is not None:
            current = _with_attribute(current, line)
    if current is not None:
        entries.append(current)
    return tuple(entries)


def list_worktrees(repository: Path) -> tuple[WorktreeEntry, ...]:
    """Run ``git -C <repository> worktree list --porcelain`` and parse it.

    Git failures propagate unchanged: :class:`subprocess.CalledProcessError`
    for a non-zero exit, :class:`subprocess.TimeoutExpired` after
    ``_LIST_TIMEOUT_S`` seconds, and :class:`OSError` when Git cannot be started.
    """
    listing = subprocess.run(
        ["git", "-C", str(repository), "worktree", "list", "--porcelain"],
        capture_output=True,
        text=True,
        check=True,
        timeout=_LIST_TIMEOUT_S,
    ).stdout
    return parse_worktree_porcelain(listing)


def _with_attribute(entry: WorktreeEntry, line: str) -> WorktreeEntry:
    """Return ``entry`` updated with one attribute line; unknown lines are ignored."""
    keyword, separator, value = line.partition(" ")
    if keyword in _VALUE_FIELDS and separator:
        return replace(entry, **{_VALUE_FIELDS[keyword]: value})
    if line in _FLAG_FIELDS:
        return replace(entry, **{line: True})
    if keyword in _REASON_FIELDS:
        return replace(entry, **{keyword: value})
    return entry
