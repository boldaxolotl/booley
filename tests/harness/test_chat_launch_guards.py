"""Launch-time guards of `booley chat` found in review of the exit step (#2613).

Fixtures and helpers come from `test_chat_checkout.py` and `test_chat_exit.py`, which hold
the real-Git setups; the tests here only add the cases those files do not cover.
"""

from __future__ import annotations

import argparse
import os
import time

from booley.harness import chat_cmd, worktree_cmd
from booley.harness.worktree_rm import remove_worktree
from booley.runtime.worktree_usage import last_used
from tests.harness.test_chat_checkout import (  # noqa: F401  (fixtures)
    _inline_claude_launch,
    _start,
    _worktrees,
    project,
)
from tests.harness.test_chat_exit import (  # noqa: F401  (fixtures)
    _add,
    _chat_exit,
    _dirty,
    _Terminal,
    repo,
    terminal,
)


def test_launch_without_fork_makes_no_exit_step(project, monkeypatch, capsys):  # noqa: F811
    # Bug caught: a worktree launch built the exit step's pipe on a host without `os.fork`
    # (Windows), where `os.set_blocking` fails on Python 3.11 before the agent even starts.
    def no_pipe() -> None:
        raise AssertionError("no exit step without os.fork")

    monkeypatch.delattr(os, "fork")
    monkeypatch.setattr(chat_cmd, "CreatedWorktree", no_pipe)

    started = _start(project, monkeypatch, capsys, flag="always")

    assert started.cwd == project / ".booley" / "worktrees" / "s1"
    assert _worktrees(project) == ["s1"]


def test_launch_inside_a_worktree_counts_as_its_use(project, monkeypatch, capsys):  # noqa: F811
    # Bug caught: an agent started in place inside an idle worktree left its last-used stamp
    # old, so another launch's `gc` removed the worktree under the running agent.
    assert worktree_cmd.run(argparse.Namespace(worktree_command="new", name="s9"), project) == 0
    inside = project / ".booley" / "worktrees" / "s9"
    stamp = project / ".booley" / "worktrees" / ".last-used" / "s9"
    ten_days_ago = time.time() - 10 * 86400
    os.utime(stamp, (ten_days_ago, ten_days_ago))
    monkeypatch.chdir(inside)

    _start(project, monkeypatch, capsys)

    stamped = last_used(project / ".booley", "s9")
    assert stamped is not None
    assert time.time() - stamped < 3600


def test_interrupted_forced_removal_hints_force(repo, terminal, monkeypatch, capsys):  # noqa: F811
    # Bug caught: the hint named plain `rm`, which refuses again after an interrupted force.
    path = _add(repo, "s1")
    _dirty(path)

    def remove(root, name, *, force: bool):
        if force:
            raise KeyboardInterrupt
        return remove_worktree(root, name, force=False)  # refuses: the change is uncommitted

    monkeypatch.setattr(_chat_exit(), "remove_worktree", remove)
    terminal.type("d\ny\n")

    terminal.end(repo)

    assert capsys.readouterr().err.endswith(
        "Removal interrupted; run booley worktree rm s1 --force to finish it.\n"
    )
