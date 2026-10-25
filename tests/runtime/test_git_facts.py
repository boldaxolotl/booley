"""The repository-fact memo of one read-only computation (#2160)."""

from __future__ import annotations

import subprocess
import threading
from collections.abc import Callable

import pytest

from booley.runtime.git_facts import repository_facts, run_remembered


class Fact:
    """A stand-in for one Git launch: counts calls, returns the next scripted exit code."""

    def __init__(self, *codes: int) -> None:
        self.calls = 0
        self.codes = list(codes) or [0]

    def __call__(self) -> subprocess.CompletedProcess[str]:
        self.calls += 1
        code = self.codes[min(self.calls, len(self.codes)) - 1]
        return subprocess.CompletedProcess(["git"], code, "out\n", "")


def ask(run: Callable[[], subprocess.CompletedProcess[str]], cwd: str = "/x") -> int:
    argv = ["git", "rev-parse", "--git-dir"]
    return run_remembered(argv, cwd, {"A": "1"}, text=True, run=run).returncode


def test_repository_facts_remembers_within_one_scope_only() -> None:
    fact = Fact()
    ask(fact)
    ask(fact)
    assert fact.calls == 2  # no scope: every call asks Git
    with repository_facts():
        ask(fact)
        with repository_facts():  # nested scopes share the outermost memo
            ask(fact)
        ask(fact, cwd="/other")
    assert fact.calls == 4
    with repository_facts():
        ask(fact)
    assert fact.calls == 5  # a new scope forgets the previous one


def test_repository_facts_never_stores_failures() -> None:
    fact = Fact(1, 0)
    with repository_facts():
        assert [ask(fact), ask(fact), ask(fact)] == [1, 0, 0]
    assert fact.calls == 2


def test_repository_facts_inactive_on_other_threads() -> None:
    fact = Fact()
    with repository_facts():
        worker = threading.Thread(target=lambda: (ask(fact), ask(fact)))
        worker.start()
        worker.join()
    assert fact.calls == 2


def test_repository_facts_rejects_queries_that_can_change() -> None:
    with pytest.raises(ValueError, match="not a rememberable repository fact"):
        run_remembered(["git", "rev-parse", "HEAD"], "/x", None, text=True, run=Fact())


def test_repository_facts_closed_scope_is_not_reused_by_a_copied_context() -> None:
    import contextvars

    fact = Fact()
    with repository_facts():
        ask(fact)
        survivor = contextvars.copy_context()
    survivor.run(ask, fact)
    survivor.run(ask, fact)
    assert fact.calls == 3  # the copy outlived its scope, so it remembers nothing


def test_repository_facts_keeps_differently_decoded_answers_apart() -> None:
    fact = Fact()
    argv = ["git", "rev-parse", "--git-dir"]
    with repository_facts():
        for decoding in ("", "replace", ""):
            run_remembered(argv, "/x", {"A": "1"}, text=True, decoding=decoding, run=fact)
    assert fact.calls == 2
