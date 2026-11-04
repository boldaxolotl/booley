"""Goal status names the repositories whose uncommitted changes would block Finish (issue #2638).

Every test drives the public entry points (``goal_status`` through the MCP
dispatcher, ``enter_goal_mode``, ``finish_goal``) so that status is compared
against what Finish really refuses.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from booley.goals import entry as entry_module
from booley.goals.entry import EntryResult
from booley.goals.finish import finish_goal
from booley.goals.lifecycle import LifecycleError
from booley.mcp.goal_tools import dispatch_goal_tool
from booley.runtime.history_commit import FileCommitError
from tests.goals.conftest import git
from tests.goals.test_finish import (
    Crash,
    environment,
    paired_publication_layout,
    publication_layout,
    request,
)

pytestmark = pytest.mark.timeout(120)

#: The sentence the rules carry only for a Goal whose Booley Directory is a Paired Checkout.
PAIRED_RULE = "is a Paired Checkout: a separate Git repository on its own branch"


@pytest.fixture(autouse=True)
def _top_level_preview(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOOLEY_TOP_LEVEL", "1")


def goal_status(layout: SimpleNamespace, *, rules: bool = False) -> tuple[str, dict]:
    """Text and structured output of ``goal_status`` called from the Goal worktree."""
    arguments = {"work_dir": str(layout.worktree)}
    if rules:
        arguments["rules"] = True
    result = asyncio.run(dispatch_goal_tool("goal_status", arguments, booley_dir=layout.control))
    assert result.is_error is False, result.value[0][0].text
    return result.value[0][0].text, result.value[1]["records"][0]


def entered_results(monkeypatch: pytest.MonkeyPatch) -> list[EntryResult]:
    """Collect what every ``enter_goal_mode`` call of the test returns."""
    results: list[EntryResult] = []
    real = entry_module.enter_goal_mode

    def recording(*args, **kwargs) -> EntryResult:
        results.append(real(*args, **kwargs))
        return results[-1]

    monkeypatch.setattr(entry_module, "enter_goal_mode", recording)
    return results


def test_paired_rules_state_the_paired_checkout_commit(layout, tmp_path, monkeypatch):
    entered = entered_results(monkeypatch)
    paired = paired_publication_layout(layout, tmp_path)
    assert PAIRED_RULE in entered[-1].rules
    text, _ = goal_status(paired, rules=True)
    assert PAIRED_RULE in text


def test_snapshot_rules_omit_the_paired_checkout_commit(layout, monkeypatch):
    from tests.goals.conftest import enter_goals

    entered = entered_results(monkeypatch)
    layout.record = enter_goals(layout)
    assert PAIRED_RULE not in entered[-1].rules
    text, _ = goal_status(layout, rules=True)
    assert "Finish needs every Goal met" in text
    assert PAIRED_RULE not in text


def test_status_lists_dirty_rtl_repository_of_snapshot_worktree(goal_mode):
    worktree = goal_mode.worktree
    text, record = goal_status(goal_mode)
    assert record["uncommitted"] == []
    assert "uncommitted changes" not in text
    # The Snapshot Booley Directory is git-excluded: Finish never sees its files.
    (worktree / ".booley" / "note.md").write_text("scratch\n", encoding="utf-8")
    text, record = goal_status(goal_mode)
    assert record["uncommitted"] == []
    assert "uncommitted changes" not in text
    (worktree / "scratch").write_text("work\n", encoding="utf-8")
    text, record = goal_status(goal_mode)
    assert record["uncommitted"] == [{"path": str(worktree), "paired_checkout": False}]
    assert record["uncommitted_error"] is None
    assert f"WARNING: uncommitted changes in {worktree} block Finish" in text


def test_status_lists_dirty_paired_checkout_like_finish(layout, tmp_path):
    paired_layout = paired_publication_layout(layout, tmp_path)
    worktree = paired_layout.worktree
    paired = worktree / ".booley"
    # The outer repository really lists the Paired Checkout, so only Finish's
    # paired-root exemption keeps it from counting as an RTL change.
    assert "?? .booley/" in git(worktree, "status", "--porcelain=v1")
    text, record = goal_status(paired_layout)
    assert record["uncommitted"] == []
    assert "uncommitted changes" not in text

    (paired / "note.txt").write_text("work\n", encoding="utf-8")
    text, record = goal_status(paired_layout)
    assert record["uncommitted"] == [{"path": str(paired), "paired_checkout": True}]
    assert f"WARNING: uncommitted changes in {paired} (Paired Checkout" in text
    with pytest.raises(LifecycleError, match=re.escape(f"commit changes in {paired} before")):
        finish_goal(request(paired_layout), environment(paired_layout))

    (worktree / "scratch").write_text("work\n", encoding="utf-8")
    _, record = goal_status(paired_layout)
    assert record["uncommitted"] == [
        {"path": str(worktree), "paired_checkout": False},
        {"path": str(paired), "paired_checkout": True},
    ]


@pytest.mark.timeout(180)
def test_status_exempts_retained_summary_of_revalidated_attempt(layout):
    complete = publication_layout(layout)
    first = request(complete)

    def stop(name: str) -> None:
        if name == "destination-materialized":
            raise Crash(name)

    with pytest.raises(Crash):
        finish_goal(first, environment(complete, stop))
    git(complete.worktree, "commit", "--allow-empty", "-qm", "unrelated committed work")
    assert finish_goal(first, environment(complete))["status"] == "revalidation_required"
    old = Path(f".booley/goals/history/{complete.record.id}.md")
    listed = git(complete.worktree, "status", "--porcelain=v1", "--untracked-files=all")
    assert f"?? {old.as_posix()}" in listed
    text, record = goal_status(complete)
    assert record["uncommitted"] == []
    assert "uncommitted changes" not in text
    assert finish_goal(request(complete), environment(complete))["status"] == "finished"


def test_status_reports_an_uncheckable_worktree_as_a_warning(goal_mode, monkeypatch):
    from booley.goals import input_view

    real = input_view.raw_git

    def failing(root, *args, **kwargs):
        if args[:1] == ("status",):
            raise FileCommitError("git status failed in the Goal worktree")
        return real(root, *args, **kwargs)

    monkeypatch.setattr(input_view, "raw_git", failing)
    text, record = goal_status(goal_mode)
    assert "WARNING: uncommitted changes cannot be checked: git status failed" in text
    assert record["uncommitted"] is None
    assert "git status failed" in record["uncommitted_error"]
