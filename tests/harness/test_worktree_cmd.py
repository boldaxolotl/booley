"""`booley worktree new <name>`: parsing, venue, and the real worktree script."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from booley.harness import booley as tlr
from booley.harness import worktree_cmd
from booley.runtime import runtime_context

# ---------------------------------------------------------------------------
# Parser and venue
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["axi-fix", "v1.2_rc", "feature--try", "A"])
def test_safe_names_parse(name: str) -> None:
    args = tlr._build_parser().parse_args(["worktree", "new", name])

    assert args.command == "worktree"
    assert args.worktree_command == "new"
    assert args.name == name


@pytest.mark.parametrize(
    "name", ["../escape", "a/b", ".hidden", "-dash", "", "with space", "..", "a\\b"]
)
def test_unsafe_names_are_rejected_by_the_parser(name: str, capsys) -> None:
    with pytest.raises(SystemExit) as exc:
        # "--" keeps argparse from reading "-dash" as an option.
        tlr._build_parser().parse_args(["worktree", "new", "--", name])

    assert exc.value.code == 2
    assert "invalid worktree name" in capsys.readouterr().err


def test_worktree_requires_a_subcommand(capsys) -> None:
    with pytest.raises(SystemExit) as exc:
        tlr._build_parser().parse_args(["worktree"])

    assert exc.value.code == 2
    assert "the following arguments are required: {new}" in capsys.readouterr().err


def test_worktree_is_sandbox_only_and_project_bound(monkeypatch, capsys) -> None:
    assert tlr.COMMAND_LOCATIONS["worktree"] is tlr.CommandLocation.SESSION_RUNTIME
    assert tlr.COMMAND_PROJECT_BINDINGS["worktree"] is tlr.ProjectBinding.REQUIRED
    monkeypatch.setattr(runtime_context, "inside_session_runtime", lambda: False)

    with pytest.raises(SystemExit) as exc:
        tlr._enforce_runtime_location("worktree")

    assert exc.value.code == 2
    assert "runs inside the Booley Sandbox" in capsys.readouterr().err


def test_worktree_help_describes_the_refusal_and_detached_head(capsys) -> None:
    with pytest.raises(SystemExit):
        tlr._build_parser().parse_args(["worktree", "new", "--help"])

    help_text = " ".join(capsys.readouterr().out.split())
    assert ".booley_project/worktrees/<name>" in help_text
    assert "detached HEAD" in help_text
    assert "refused" in help_text


def test_missing_script_fails_without_running_anything(monkeypatch, tmp_path, capsys) -> None:
    monkeypatch.setattr(worktree_cmd, "worktree_create_script", lambda: tmp_path / "absent.sh")
    args = tlr._build_parser().parse_args(["worktree", "new", "x"])

    assert worktree_cmd.run(args, tmp_path) == 1
    assert "worktree script not found" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Real Git + the packaged bash script
# ---------------------------------------------------------------------------

_real_script = pytest.mark.skipif(
    shutil.which("git") is None or os.name == "nt",
    reason="needs git and a POSIX shell; `booley worktree new` is Sandbox-only "
    "(the script's Windows path is covered by test_setup_workspace)",
)


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
        timeout=60,
    )


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture
def project(tmp_path: Path, monkeypatch) -> Path:
    """A Git Project with a committed Goal history file and live Goal/session state."""
    monkeypatch.delenv("BOOLEY_PROJECT_DIR", raising=False)
    root = tmp_path / "repo"
    root.mkdir()
    for args in (
        ("init",),
        ("config", "user.name", "Test User"),
        ("config", "user.email", "test@example.com"),
    ):
        assert _git(root, *args).returncode == 0
    data = root / ".booley_project"
    _write(root / "README.md", "hello\n")
    # Committed Goal history reaches a worktree through Git checkout (-f: an
    # ambient global excludes file may ignore .booley_project/).
    _write(data / "goals" / "history" / "old-20260101T000000Z.md", "committed summary\n")
    assert _git(root, "add", "-f", "README.md", ".booley_project/goals/history").returncode == 0
    assert _git(root, "commit", "-m", "init").returncode == 0
    # Untracked Project data: configuration that must be copied ...
    _write(data / "booley.toml", "[submodules]\npaths = []\n")
    # ... and live Goal / session state that must stay behind.
    goal_dir = data / "goals" / "axi-20261006T120000Z"
    _write(goal_dir / "record.json", "{}\n")
    _write(goal_dir / "lock", "")
    _write(goal_dir / "logs" / "receipt.json", "{}\n")
    _write(data / "goals" / "locks" / "worktree-abc.lock", "")
    _write(data / "goals" / "history" / "untracked-20261006T000000Z.md", "local only\n")
    _write(data / "runtime" / "sessions" / "s1" / "state.json", "{}\n")
    # Nested directories that merely share those names are ordinary Project data.
    _write(data / "cores" / "goals" / "goals.core", "CAPI=2:\n")
    _write(data / "mcp_tools" / "runtime" / "sessions" / "helper.py", "# helper\n")
    return root


def _new(name: str, project_root: Path) -> int:
    args = tlr._build_parser().parse_args(["worktree", "new", name])
    return worktree_cmd.run(args, project_root)


@_real_script
def test_creates_a_detached_worktree_and_prints_its_path(project: Path, capsys) -> None:
    assert _new("goal-entry", project) == 0

    worktree = project / ".booley_project" / "worktrees" / "goal-entry"
    assert capsys.readouterr().out.strip() == str(worktree)
    assert (worktree / ".git").is_file()
    assert (worktree / "README.md").read_text(encoding="utf-8") == "hello\n"
    assert _git(worktree, "symbolic-ref", "--quiet", "HEAD").returncode != 0
    head = _git(worktree, "rev-parse", "HEAD").stdout
    assert head == _git(project, "rev-parse", "HEAD").stdout


@_real_script
def test_copied_project_dir_leaves_goal_and_session_state_behind(project: Path) -> None:
    assert _new("snapshot", project) == 0

    copied = project / ".booley_project" / "worktrees" / "snapshot" / ".booley_project"
    assert (copied / "booley.toml").is_file()
    # The tracked history file arrives through Git; nothing else under goals/.
    goals = copied / "goals"
    assert sorted(p.relative_to(goals).as_posix() for p in goals.rglob("*")) == [
        "history",
        "history/old-20260101T000000Z.md",
    ]
    assert not list(goals.rglob("record.json"))
    assert not (goals / "locks").exists()
    assert not (copied / "runtime" / "sessions").exists()


@_real_script
def test_only_the_top_level_goal_and_session_directories_stay_behind(project: Path) -> None:
    assert _new("nested", project) == 0

    copied = project / ".booley_project" / "worktrees" / "nested" / ".booley_project"
    assert (copied / "cores" / "goals" / "goals.core").is_file()
    assert (copied / "mcp_tools" / "runtime" / "sessions" / "helper.py").is_file()


@_real_script
def test_refuses_an_existing_directory_and_leaves_it_untouched(project: Path, capsys) -> None:
    existing = project / ".booley_project" / "worktrees" / "busy"
    _write(existing / "work-in-progress.txt", "precious\n")
    _write(existing / ".creating", "")

    assert _new("busy", project) == 1

    err = capsys.readouterr().err
    assert "destination is not free" in err
    assert "already exists" in err
    assert (existing / "work-in-progress.txt").read_text(encoding="utf-8") == "precious\n"
    assert (existing / ".creating").is_file()
    assert sorted(p.name for p in existing.iterdir()) == [".creating", "work-in-progress.txt"]
    assert str(existing) not in _git(project, "worktree", "list").stdout


@_real_script
def test_refuses_an_active_worktree(project: Path, capsys) -> None:
    assert _new("active", project) == 0
    worktree = project / ".booley_project" / "worktrees" / "active"
    _write(worktree / "uncommitted.txt", "agent work\n")
    capsys.readouterr()

    assert _new("active", project) == 1

    assert "destination is not free" in capsys.readouterr().err
    assert (worktree / "uncommitted.txt").read_text(encoding="utf-8") == "agent work\n"
    assert (worktree / ".git").is_file()


@_real_script
def test_refuses_a_registered_worktree_whose_directory_is_gone(project: Path, capsys) -> None:
    assert _new("vanished", project) == 0
    worktree = project / ".booley_project" / "worktrees" / "vanished"
    shutil.rmtree(worktree)
    capsys.readouterr()

    assert _new("vanished", project) == 1

    assert "already registered" in capsys.readouterr().err
    assert not worktree.exists()


def test_help_never_names_the_preview_surface(capsys) -> None:
    """`worktree new` is released; its help must not mention hidden Goal Mode state."""
    with pytest.raises(SystemExit):
        tlr._build_parser().parse_args(["worktree", "new", "--help"])

    assert "Goal" not in capsys.readouterr().out
