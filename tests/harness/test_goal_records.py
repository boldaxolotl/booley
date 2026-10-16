"""Public retained-record commands and their deletion boundaries."""
# ruff: noqa: F811 — pytest fixture imports intentionally share parameter names.

import argparse
import json
from pathlib import Path

import pytest

from booley.goals.model import GoalState
from booley.goals.paths import record_paths
from booley.goals.store import GoalStore, GoalStoreError
from booley.harness import booley as cli
from tests.goals.conftest import git, layout  # noqa: F401 — shared fixture
from tests.goals.test_finish import complete, environment, request  # noqa: F401 — shared fixture
from tests.goals.test_inventory import remove_checkout, snapshot, terminal
from tests.goals.test_store import repo  # noqa: F401 — shared fixture


def choices():
    parser = cli._build_parser()
    sub = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    return next(
        a for a in sub.choices["goal"]._actions if isinstance(a, argparse._SubParsersAction)
    ).choices


def run(repo, monkeypatch, *args):
    monkeypatch.setenv("BOOLEY_DIR", str(repo["project"]))
    monkeypatch.chdir(repo["main"])
    return cli._cmd_goal(cli._build_parser().parse_args(["goal", *args]), repo["main"])


def test_goal_list_all_lifecycle_records(repo, monkeypatch, capsys):
    assert "list" in choices(), "goal list command is missing"
    records = [terminal(repo, state, state.value) for state in GoalState]
    before = snapshot(repo["project"])
    assert run(repo, monkeypatch, "list") == 0
    text = capsys.readouterr().out
    for record in records:
        assert record.id in text and record.state.value in text
    assert "bytes" in text and "present" in text
    assert snapshot(repo["project"]) == before
    broken = record_paths(repo["project"], records[0].id).record_file
    broken.write_bytes(b"bad json")
    assert run(repo, monkeypatch, "list") == 0
    text = capsys.readouterr().out
    assert "corrupt" in text and records[-1].id in text


def test_goal_show_retained_outcomes_without_worktree(complete, monkeypatch, capsys):
    assert "show" in choices(), "goal show command is missing"
    from booley.goals.finish import finish_goal

    result = finish_goal(request(complete), environment(complete))
    git(complete.main, "worktree", "remove", "--force", str(complete.worktree))
    git(complete.main, "branch", "-D", complete.record.branch)
    repo = {"main": complete.main, "project": complete.control}
    before = snapshot(complete.control / "goals")
    assert run(repo, monkeypatch, "show", complete.record.id) == 0
    text = capsys.readouterr().out
    assert "LINT:top" in text and "passing" in text
    assert result["package"] in text and result["operation_id"] in text
    assert "freshness is not asserted" in text
    assert snapshot(complete.control / "goals") == before
    assert run(repo, monkeypatch, "show", "../bad") == 2


def test_goal_prune_requires_confirm_and_preserves_unselected_data(repo, monkeypatch, capsys):
    assert "prune" in choices(), "goal prune command is missing"
    record = terminal(repo)
    remove_checkout(repo)
    root = record_paths(repo["project"], record.id).root
    history = repo["project"] / "goals/history/keep.md"
    history.parent.mkdir()
    history.write_text("history")
    before = snapshot(repo["project"])
    assert run(repo, monkeypatch, "prune", record.id) == 0
    assert "--confirm" in capsys.readouterr().out
    assert snapshot(repo["project"]) == before
    assert run(repo, monkeypatch, "prune", record.id, "--confirm") == 0
    assert not root.exists() and history.read_text() == "history"
    assert {
        k: v for k, v in snapshot(repo["project"]).items() if not k.endswith("prune.lock")
    } == {k: v for k, v in before.items() if not k.startswith("goals/" + record.id + "/")}


def test_goal_prune_refuses_unsafe_records_and_resumes_own_quarantine(repo, monkeypatch, capsys):
    assert "prune" in choices(), "goal prune command is missing"
    from booley.goals import prune

    record = terminal(repo, GoalState.ACTIVE)
    before = snapshot(repo["project"])
    assert run(repo, monkeypatch, "prune", record.id, "--confirm") == 2
    assert snapshot(record_paths(repo["project"], record.id).root) == {
        k.split(record.id + "/", 1)[1]: v for k, v in before.items() if record.id + "/" in k
    }
    path = record_paths(repo["project"], record.id).record_file
    data = json.loads(path.read_bytes())
    data["state"] = "failed"
    path.write_text(json.dumps(data))
    assert run(repo, monkeypatch, "prune", record.id, "--confirm") == 2
    remove_checkout(repo)
    original = prune._remove_tree
    monkeypatch.setattr(
        prune, "_remove_tree", lambda *_: (_ for _ in ()).throw(OSError("interrupted delete"))
    )
    assert run(repo, monkeypatch, "prune", record.id, "--confirm") == 2
    assert run(repo, monkeypatch, "list") == 0
    assert ".pruning-" in capsys.readouterr().out
    with pytest.raises(GoalStoreError), GoalStore(repo["project"]).record_lock(record.id):
        pytest.fail("pruning record lock admitted writer")
    monkeypatch.setattr(prune, "_remove_tree", original)
    assert run(repo, monkeypatch, "prune", record.id, "--confirm") == 0
    assert not (repo["project"] / "goals" / (".pruning-" + record.id)).exists()


def test_goal_prune_incomplete_terminal_orphan_requires_informed_confirm(
    complete, monkeypatch, capsys
):
    assert "prune" in choices(), "goal prune command is missing"
    from booley.goals.finish import finish_goal

    result = finish_goal(request(complete), environment(complete))
    operation = (
        record_paths(complete.control, complete.record.id).root
        / "operations"
        / result["operation_id"]
    )
    (operation / "result.json").unlink()
    (operation / "result.authority.json").unlink()
    git(complete.main, "worktree", "remove", "--force", str(complete.worktree))
    git(complete.main, "branch", "-D", complete.record.branch)
    repo = {"main": complete.main, "project": complete.control}
    before = snapshot(complete.control / "goals")
    assert run(repo, monkeypatch, "prune", complete.record.id) == 0
    assert "missing sealed result" in capsys.readouterr().out
    assert snapshot(complete.control / "goals") == before
    (operation / "result.json").write_text("{}")
    assert run(repo, monkeypatch, "prune", complete.record.id, "--confirm") == 2
    (operation / "result.json").unlink()
    assert run(repo, monkeypatch, "prune", complete.record.id, "--confirm") == 0
    assert "retry evidence" in capsys.readouterr().out

    _check_incomplete_abandonment(complete, monkeypatch, capsys)


def _check_incomplete_abandonment(complete, monkeypatch, capsys):
    repo = {"main": complete.main, "project": complete.control}
    # Abandonment has separate immutable pre-result authority and no Finish package.
    from types import SimpleNamespace
    from uuid import uuid4

    from booley.goals.finish import finish_goal
    from booley.goals.lifecycle import LifecycleRequest
    from tests.goals.conftest import enter_goals, write_project_files

    worktree = complete.control / "worktrees" / "abandon"
    git(complete.main, "worktree", "add", "-q", "-b", "abandon-work", str(worktree))
    write_project_files(worktree / ".booley")
    abandoned = SimpleNamespace(main=complete.main, control=complete.control, worktree=worktree)
    abandoned.record = enter_goals(abandoned, [{"family": "LINT", "target": "top"}])
    call = LifecycleRequest(
        worktree,
        abandoned.record.id,
        str(uuid4()),
        abandon=True,
        instruction_quote="Stop this Goal",
    )
    result = finish_goal(call, environment(abandoned))
    operation = (
        record_paths(abandoned.control, abandoned.record.id).root
        / "operations"
        / result["operation_id"]
    )
    (operation / "result.json").unlink()
    (operation / "result.authority.json").unlink()
    git(abandoned.main, "worktree", "remove", "--force", str(worktree))
    git(abandoned.main, "branch", "-D", abandoned.record.branch)
    authority = operation / "abandonment.authority.json"
    retained = authority.read_bytes()
    authority.write_bytes(b"bad json")
    assert run(repo, monkeypatch, "prune", abandoned.record.id, "--confirm") == 2
    authority.write_bytes(retained)
    before = snapshot(abandoned.control / "goals")
    assert run(repo, monkeypatch, "prune", abandoned.record.id) == 0
    assert "missing sealed result" in capsys.readouterr().out
    assert snapshot(abandoned.control / "goals") == before
    assert run(repo, monkeypatch, "prune", abandoned.record.id, "--confirm") == 0
    assert "retry evidence" in capsys.readouterr().out


def test_usage_documents_retained_record_commands():
    usage = (Path(__file__).parents[2] / "docs/user/USAGE.md").read_text()
    assert "### Inspecting and cleaning retained Goal Records" in usage, (
        "retained record usage is missing"
    )
    for term in (
        "booley goal list",
        "booley goal show",
        "booley goal prune",
        "--confirm",
        "goal_list",
        "goal_show",
        "shared locks",
        "recorded outcomes",
    ):
        assert term in usage


def test_prune_second_job_read_error_is_cli_refusal(repo, monkeypatch, capsys):
    from booley.goals import prune
    from booley.runtime.job_records import JobRecordError

    record = terminal(repo)
    remove_checkout(repo)
    before = snapshot(record_paths(repo["project"], record.id).root)

    def changed_read(*args, **kwargs):
        raise JobRecordError("second strict Job read changed")

    monkeypatch.setattr(prune, "strict_records", changed_read)
    try:
        code = run(repo, monkeypatch, "prune", record.id, "--confirm")
    except RuntimeError:
        code = None
    assert code == 2, "second Job read escaped prune CLI refusal"
    assert "second strict Job read changed" in capsys.readouterr().err
    assert snapshot(record_paths(repo["project"], record.id).root) == before
