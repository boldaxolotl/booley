"""Launch-time guards of `booley chat` found in review of the exit step (#2613).

Fixtures and helpers come from `test_chat_checkout.py` and `test_chat_exit.py`, which hold
the real-Git setups; the tests here only add the cases those files do not cover.
"""

from __future__ import annotations

import argparse
import os
import time

import pytest

from booley.harness import booley as cli
from booley.harness import chat_cmd, worktree_cmd
from booley.harness.worktree_rm import remove_worktree
from booley.runtime import booley_dir
from booley.runtime.worktree_usage import last_used
from tests.harness.test_chat_checkout import (  # noqa: F401  (fixtures)
    _inline_claude_launch,
    _start,
    _worktrees,
    _write_toml,
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
from tests.time_budget import scaled


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
    assert time.time() - stamped < scaled(3600)


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


@pytest.mark.parametrize("retired", ["[jobs]\nmax_heavy = 2\n", "[goals]\nquiet_after = 60\n"])
def test_chat_opens_while_booley_toml_is_retired(project, monkeypatch, retired):  # noqa: F811
    # Bug caught: the retired-config refusal also refused the chat that runs /booley-heal.
    _write_toml(project, retired)
    monkeypatch.setenv("BOOLEY_CONTAINER", "1")
    booley_dir.reset_cache()
    execs: list[list[str]] = []
    monkeypatch.setattr(chat_cmd.os, "execvp", lambda _exe, argv: execs.append(argv) or 0)
    monkeypatch.setattr("sys.argv", ["booley", "chat"])
    cli.main()  # a real execvp never returns
    assert execs == [["S1", "-n", "S1"]]
