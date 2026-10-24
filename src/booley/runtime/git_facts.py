"""Ask Git each repository fact once per read-only computation.

Goal Mode reads the same HEAD-independent facts over and over while it digests
one set of inputs or projects one status: where a checkout's Git directory is,
which toplevel contains a path, which object format a repository uses. Inside
:func:`repository_facts` those answers are remembered, so the second identical
question costs no process launch.

Scope contract
==============

A scope wraps **one read-only computation** and nothing else. Goal Mode
re-reads repository facts on purpose after side effects (a freeze, a Git
write, a rollback) to catch a substituted checkout, so a scope never spans such
a step and a re-check opens its own scope or none. Outside a scope, and on any
thread other than the one that opened it, nothing is remembered and every call
runs Git exactly as it did before.

Only answers that cannot change inside one read-only computation are
remembered: checkout identity queries and content addressed reads of a full
object id. Never HEAD, refs, the index, status, configuration, or a write.
Only a successful answer is stored, so a failure is asked again every time.
"""

from __future__ import annotations

import os
import re
import subprocess
import threading
from collections.abc import Callable, Generator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path

_IDENTITY_OPTIONS = frozenset(
    {
        "--git-common-dir",
        "--git-dir",
        "--show-toplevel",
        "--show-prefix",
        "--show-object-format",
        "--path-format=absolute",
    }
)
_OBJECT_ID = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")

_Key = tuple[tuple[str, ...], str, tuple[tuple[str, str], ...], bool, str]


@dataclass
class _Memo:
    """The remembered answers of one scope and the thread allowed to use them."""

    owner: int
    closed: bool = False
    answers: dict[_Key, subprocess.CompletedProcess] = field(default_factory=dict)


_active: ContextVar[_Memo | None] = ContextVar("repository_facts", default=None)


@contextmanager
def repository_facts() -> Generator[None]:
    """Remember repository facts for the duration of one read-only computation.

    Reentrant: a scope opened inside an active one shares its memo, which ends
    with the outermost scope.
    """
    if _active.get() is not None:
        yield
        return
    memo = _Memo(threading.get_ident())
    token = _active.set(memo)
    try:
        yield
    finally:
        memo.closed = True  # a context copied inside the scope must not reuse it
        _active.reset(token)


def is_rememberable(args: Sequence[str]) -> bool:
    """Whether Git *args* are an identity query or a content addressed read of a full id."""
    command, rest = args[0], list(args[1:])
    if command == "rev-parse":
        return bool(rest) and all(option in _IDENTITY_OPTIONS for option in rest)
    if command == "cat-file":
        return len(rest) == 2 and rest[0] == "-t" and bool(_OBJECT_ID.fullmatch(rest[1]))
    if command == "ls-tree":
        return rest[:2] == ["-r", "-z"] and len(rest) == 3 and bool(_OBJECT_ID.fullmatch(rest[2]))
    return False


def run_remembered(
    argv: Sequence[str],
    cwd: Path | str,
    env: Mapping[str, str] | None,
    *,
    text: bool,
    decoding: str = "",
    run: Callable[[], subprocess.CompletedProcess],
) -> subprocess.CompletedProcess:
    """Return ``run()``, remembering a successful answer inside an active scope.

    *argv* is the full ``git ...`` command line, *cwd* and *env* are exactly
    what *run* passes to Git (``None`` env means the process environment), and
    *text* says whether *run* captures text or bytes, and *decoding* names a
    decoding that differs between callers asking the same question (their
    answers are never exchanged). The raw result is shared,
    so each caller post-processes it into its own type.
    """
    if not is_rememberable(argv[1:]):
        raise ValueError(f"git {' '.join(argv[1:])} is not a rememberable repository fact")
    memo = _active.get()
    if memo is None or memo.closed or memo.owner != threading.get_ident():
        return run()
    environment = os.environ if env is None else env
    key: _Key = (tuple(argv), str(cwd), tuple(sorted(environment.items())), text, decoding)
    answer = memo.answers.get(key)
    if answer is None:
        answer = run()
        if answer.returncode == 0:
            memo.answers[key] = answer
    return answer
