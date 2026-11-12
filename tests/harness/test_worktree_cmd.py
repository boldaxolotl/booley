"""`booley worktree new <name>`: parsing, venue, and the real worktree script."""

from __future__ import annotations

import argparse
import os
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest

from booley.harness import booley as tlr
from booley.harness import worktree_cmd
from booley.runtime import runtime_context
from tests.project_runtime_git_support import NEW_RUNTIME_PATHS
from tests.subprocess_budget import run_bounded_child

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
    assert "the following arguments are required: {new,list,rm,gc}" in capsys.readouterr().err


def test_worktree_is_sandbox_only_and_project_bound(monkeypatch, capsys) -> None:
    assert tlr.COMMAND_LOCATIONS["worktree"] is tlr.CommandLocation.SESSION_RUNTIME
    assert tlr.COMMAND_PROJECT_BINDINGS["worktree"] is tlr.ProjectBinding.REQUIRED
    monkeypatch.setattr(runtime_context, "inside_session_runtime", lambda: False)

    with pytest.raises(SystemExit) as exc:
        tlr._enforce_runtime_location("worktree")

    assert exc.value.code == 2
    assert "runs inside the Booley Sandbox" in capsys.readouterr().err


def test_worktree_help_describes_the_refusal_and_branch(capsys) -> None:
    with pytest.raises(SystemExit):
        tlr._build_parser().parse_args(["worktree", "new", "--help"])

    help_text = " ".join(capsys.readouterr().out.split())
    assert ".booley/worktrees/<name>" in help_text
    assert "booley/<name>" in help_text
    assert "refused" in help_text
    assert "last commit" in help_text
    assert "post-setup" in help_text


def test_missing_script_fails_without_running_anything(monkeypatch, tmp_path, capsys) -> None:
    # The branch pre-check reads the current commit before the script is looked up.
    for args in (("init",), ("commit", "--allow-empty", "-m", "init")):
        _git(tmp_path, "-c", "user.name=T", "-c", "user.email=t@example.com", *args)
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


@pytest.fixture(autouse=True)
def _ambient_booley_dir_is_the_checkouts_own(monkeypatch) -> None:
    """``booley worktree new`` refuses a foreign ``BOOLEY_DIR``; the harness default is one."""
    monkeypatch.delenv("BOOLEY_DIR", raising=False)


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return run_bounded_child(["git", *args], cwd=cwd)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture
def project(tmp_path: Path, monkeypatch) -> Path:
    """A Git Project with a committed Goal history file and live Goal/session state."""
    monkeypatch.delenv("BOOLEY_DIR", raising=False)
    root = tmp_path / "repo"
    root.mkdir()
    for args in (
        ("init",),
        ("config", "user.name", "Test User"),
        ("config", "user.email", "test@example.com"),
    ):
        assert _git(root, *args).returncode == 0
    data = root / ".booley"
    _write(root / "README.md", "hello\n")
    # Committed Goal history reaches a worktree through Git checkout (-f: an
    # ambient global excludes file may ignore .booley/).
    _write(data / "goals" / "history" / "old-20260101T000000Z.md", "committed summary\n")
    assert _git(root, "add", "-f", "README.md", ".booley/goals/history").returncode == 0
    assert _git(root, "commit", "-m", "init").returncode == 0
    # Untracked files in the Booley Directory: configuration that must be copied ...
    _write(data / "booley.toml", "[submodules]\npaths = []\n")
    # ... and live Goal / session state that must stay behind.
    goal_dir = data / "goals" / "axi-20261006T120000Z"
    _write(goal_dir / "record.json", "{}\n")
    _write(goal_dir / "lock", "")
    _write(goal_dir / "logs" / "receipt.json", "{}\n")
    _write(data / "goals" / "locks" / "worktree-abc.lock", "")
    _write(data / "goals" / "history" / "untracked-20261006T000000Z.md", "local only\n")
    _write(data / "runtime" / "sessions" / "s1" / "state.json", "{}\n")
    # Nested directories that merely share those names are ordinary contents of the Booley Directory.
    _write(data / "cores" / "goals" / "goals.core", "CAPI=2:\n")
    _write(data / "mcp_tools" / "runtime" / "sessions" / "helper.py", "# helper\n")
    return root


def _new(name: str, project_root: Path) -> int:
    return worktree_cmd.run(argparse.Namespace(worktree_command="new", name=name), project_root)


@_real_script
def test_creates_a_worktree_on_its_own_branch_and_prints_its_path(
    project: Path, capsys, tmp_path
) -> None:
    excludes = tmp_path / "empty-excludes"
    excludes.touch()
    assert _git(project, "config", "core.excludesFile", str(excludes)).returncode == 0
    assert _new("goal-entry", project) == 0

    worktree = project / ".booley" / "worktrees" / "goal-entry"
    assert _git(worktree, "symbolic-ref", "--quiet", "HEAD").stdout.strip() == (
        "refs/heads/booley/goal-entry"
    )
    output = capsys.readouterr()
    assert output.out == f"{worktree}\n"
    # Success prints one calm line pointing at the removal docs, not the steps themselves.
    assert output.err == (
        "Filling git submodules from the main checkout...\n"
        "Created worktree 'goal-entry' with a Snapshot. "
        "Remove it later with: booley worktree rm goal-entry "
        "(see https://booley.dev/docs/cli-reference/#worktrees)\n"
    )
    # The Snapshot steps still exist for failures; their commands must work.
    steps = worktree_cmd.worktree_removal_instructions(
        project, worktree, "goal-entry", branch="booley/goal-entry", paired=False
    )
    command = shlex.join(["git", "-C", str(project), "worktree", "remove", str(worktree)])
    assert command in steps, "Snapshot removal instructions missing"
    assert "Snapshot" in steps and "same Sandbox" in steps and "Sandbox paths" in steps
    assert "WARNING" not in steps
    assert "preserve" in steps and "CLI-REFERENCE.md (host doc, not shipped)" in steps
    assert steps.count("git -C") == 2 and "booley-worktree/" not in steps
    assert shlex.join(["git", "-C", str(project), "branch", "-D", "booley/goal-entry"]) in steps
    assert "populated submodules" in steps and "--force" in steps
    assert (worktree / ".git").is_file()
    assert (worktree / "README.md").read_text(encoding="utf-8") == "hello\n"
    head = _git(worktree, "rev-parse", "HEAD").stdout
    assert head == _git(project, "rev-parse", "HEAD").stdout
    # Discard the disposable Snapshot's copied and generated files before removal.
    assert _git(worktree, "clean", "-fdx").returncode == 0
    run_bounded_child(shlex.split(command), check=True, timeout=10)
    assert not worktree.exists()
    assert str(worktree) not in _git(project, "worktree", "list", "--porcelain").stdout


@_real_script
def test_new_refuses_an_existing_branch_and_leaves_it_untouched(project: Path, capsys) -> None:
    _write(project / "other.txt", "later\n")
    assert _git(project, "add", "other.txt").returncode == 0
    assert _git(project, "commit", "-m", "later").returncode == 0
    assert _git(project, "branch", "booley/taken", "HEAD~1").returncode == 0
    taken = _git(project, "rev-parse", "booley/taken").stdout

    assert _new("taken", project) == 1

    assert "booley/taken" in capsys.readouterr().err
    assert not (project / ".booley" / "worktrees" / "taken").exists()
    assert _git(project, "rev-parse", "booley/taken").stdout == taken


@_real_script
def test_new_in_stealth_mode_creates_a_wt_branch(versioned_project: Path) -> None:
    """Stealth Mode keeps the product name out of the worktree's branch (#3608)."""
    assert _new("demo", versioned_project) == 0

    worktree = worktree_cmd.worktree_path(versioned_project, "demo")
    assert _git(worktree, "symbolic-ref", "--quiet", "HEAD").stdout.strip() == "refs/heads/wt/demo"
    assert _git(versioned_project, "rev-parse", "--verify", "booley/demo").returncode != 0
    # The Paired Checkout keeps its branch name.
    paired = worktree / ".booley"
    assert _git(paired, "symbolic-ref", "--short", "HEAD").stdout.strip() == (
        "booley-worktree/demo"
    )


@_real_script
@pytest.mark.parametrize(
    ("stealth", "blocking", "name", "in_the_way"),
    [
        pytest.param(True, "wt", "blocked", "wt", id="stealth-wt"),
        pytest.param(True, "wt/blocked/x", "blocked", "wt/blocked/x", id="stealth-below"),
        pytest.param(False, "booley", "blocked", "booley", id="off-booley"),
    ],
)
def test_new_names_the_branch_in_the_way(
    project: Path, capsys, stealth: bool, blocking: str, name: str, in_the_way: str
) -> None:
    """A branch that occupies the new name makes creation fail naming that branch."""
    if stealth:
        _write(project / ".booley" / "booley.toml", "[stealth]\nenabled = true\n")
    assert _git(project, "branch", blocking).returncode == 0
    before = _git(project, "rev-parse", blocking).stdout

    assert _new(name, project) == 1

    prefix = "wt" if stealth else "booley"
    errors = [line for line in capsys.readouterr().err.splitlines() if line.startswith("ERROR")]
    assert errors == [
        f"ERROR: branch '{in_the_way}' is in the way of new branch '{prefix}/{name}'; "
        "rename or delete it, or choose another name"
    ]
    assert not (project / ".booley" / "worktrees" / name).exists()
    assert _git(project, "rev-parse", blocking).stdout == before


@_real_script
def test_wt_worktree_survives_switching_stealth_off(versioned_project: Path, capsys) -> None:
    """A worktree made under Stealth Mode is listed and removed after it is switched off."""
    from booley.harness import booley as tlr

    assert _new("demo", versioned_project) == 0
    _write(versioned_project / ".booley" / "booley.toml", "[stealth]\nenabled = false\n")
    capsys.readouterr()

    listing = worktree_cmd.run(
        tlr._build_parser().parse_args(["worktree", "list"]), versioned_project
    )
    assert listing == 0
    assert "wt/demo" in capsys.readouterr().out

    args = tlr._build_parser().parse_args(["worktree", "rm", "demo"])
    assert worktree_cmd.run(args, versioned_project) == 0
    assert "Branches kept:" in capsys.readouterr().out
    assert not worktree_cmd.worktree_path(versioned_project, "demo").exists()
    assert _git(versioned_project, "rev-parse", "--verify", "wt/demo").returncode == 0


@_real_script
def test_branch_description_name_checks_out_the_existing_branch(project: Path) -> None:
    assert _git(project, "branch", "feat").returncode == 0

    assert _new("feat--try", project) == 0

    worktree = project / ".booley" / "worktrees" / "feat--try"
    assert _git(worktree, "symbolic-ref", "--short", "HEAD").stdout.strip() == "feat"
    assert (
        _git(project, "show-ref", "--verify", "--quiet", "refs/heads/booley/feat--try").returncode
        != 0
    )


@_real_script
def test_failed_worktree_add_drops_only_its_new_branch(project: Path, capsys) -> None:
    head = _git(project, "rev-parse", "HEAD").stdout
    assert _git(project, "branch", "bystander").returncode == 0
    hook = project / ".git" / "hooks" / "post-checkout"
    _write(hook, "#!/bin/sh\nexit 1\n")
    hook.chmod(0o755)

    assert _new("doomed", project) == 1

    capsys.readouterr()
    assert (
        _git(project, "show-ref", "--verify", "--quiet", "refs/heads/booley/doomed").returncode
        != 0
    )
    assert _git(project, "rev-parse", "bystander").stdout == head
    assert not (project / ".booley" / "worktrees" / "doomed").exists()


@_real_script
def test_a_failed_stamp_is_a_warning_and_the_worktree_is_still_reported(
    project: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    def refuse(common: Path, name: str) -> None:
        raise OSError("read-only folder")

    monkeypatch.setattr(worktree_cmd, "stamp_worktree", refuse)

    assert _new("stampless", project) == 0

    output = capsys.readouterr()
    assert output.out == f"{project / '.booley' / 'worktrees' / 'stampless'}\n"
    assert "WARNING: could not record the last-used time of stampless" in output.err


@_real_script
def test_new_resets_the_last_used_stamp(project: Path) -> None:
    from booley.runtime.worktree_usage import last_used, stamp_worktree

    common = project / ".booley"
    stamp_worktree(common, "fresh")
    stamp = common / "worktrees" / ".last-used" / "fresh"
    os.utime(stamp, (1000, 1000))
    assert last_used(common, "fresh") == 1000

    assert _new("fresh", project) == 0

    assert last_used(common, "fresh") > 1_000_000


@_real_script
def test_copied_booley_dir_leaves_goal_and_session_state_behind(project: Path) -> None:
    assert _new("snapshot", project) == 0

    copied = project / ".booley" / "worktrees" / "snapshot" / ".booley"
    assert (copied / "booley.toml").is_file()
    # goals/history/ is versioned with the RTL (tracked or not); no other Goal state is copied.
    goals = copied / "goals"
    assert sorted(p.relative_to(goals).as_posix() for p in goals.rglob("*")) == [
        "history",
        "history/old-20260101T000000Z.md",
        "history/untracked-20261006T000000Z.md",
    ]
    assert not list(goals.rglob("record.json"))
    assert not (goals / "locks").exists()
    assert not (copied / "runtime" / "sessions").exists()


@_real_script
def test_only_the_top_level_goal_and_session_directories_stay_behind(project: Path) -> None:
    assert _new("nested", project) == 0

    copied = project / ".booley" / "worktrees" / "nested" / ".booley"
    assert (copied / "cores" / "goals" / "goals.core").is_file()
    assert (copied / "mcp_tools" / "runtime" / "sessions" / "helper.py").is_file()


@_real_script
def test_refuses_an_existing_directory_and_leaves_it_untouched(project: Path, capsys) -> None:
    existing = project / ".booley" / "worktrees" / "busy"
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
    worktree = project / ".booley" / "worktrees" / "active"
    _write(worktree / "uncommitted.txt", "agent work\n")
    capsys.readouterr()

    assert _new("active", project) == 1

    assert "destination is not free" in capsys.readouterr().err
    assert (worktree / "uncommitted.txt").read_text(encoding="utf-8") == "agent work\n"
    assert (worktree / ".git").is_file()


@_real_script
def test_refuses_a_registered_worktree_whose_directory_is_gone(project: Path, capsys) -> None:
    assert _new("vanished", project) == 0
    worktree = project / ".booley" / "worktrees" / "vanished"
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


@pytest.fixture
def versioned_project(tmp_path):
    """A clean Stealth outer repository with a Versioned Booley Directory at its HEAD."""
    from booley.runtime.project_gitignore import PROJECT_GITIGNORE
    from tests.goals.conftest import git
    from tests.goals.stealth_support import CORE

    root = tmp_path / "paired user's" / "main"
    root.mkdir(parents=True)
    git(root, "init", "-q", "-b", "main")
    _write(root / "rtl.v", "module top; endmodule\n")
    git(root, "add", ".")
    git(root, "commit", "-qm", "RTL")
    _write(root / ".git/info/exclude", "/.booley\n/.booley-projected-*.core\n")
    source = root / ".booley"
    _write(source / ".gitignore", PROJECT_GITIGNORE)
    _write(
        source / "booley.toml",
        "[stealth]\nenabled=true\n[agent.git]\nname='Worktree Test'\nemail='wt@test.invalid'\n",
    )
    _write(source / "cores/top.core", CORE)
    _write(source / "cores/constraints/top.sdc", "create_clock -period 10 [get_ports clk]\n")
    git(source, "init", "-q", "-b", "main")
    git(source, "add", ".")
    git(source, "commit", "-qm", "Project")
    _write(source / "runtime/doctor/stale.lock", "ignored\n")
    return root


@pytest.fixture
def python_outer(monkeypatch):
    """Exercise Python pairing on every OS without depending on a POSIX script."""
    from tests.goals.conftest import git

    def create(name, root, *, paired_project, new_branch=None):
        destination = worktree_cmd.worktree_path(root, name)
        if new_branch is None:
            git(root, "worktree", "add", "--detach", str(destination), "HEAD")
        else:
            git(
                root,
                "worktree",
                "add",
                "-b",
                new_branch.name,
                str(destination),
                new_branch.start_point,
            )
        _write(destination / ".booley/booley.toml", "# script snapshot\n")

    monkeypatch.setattr(worktree_cmd, "_create_outer", create)


@_real_script
def test_user_command_pairs_committed_project_and_prints_removal(versioned_project, capsys):
    from tests.goals.conftest import git

    root = versioned_project
    source = root / ".booley"
    assert _new("real-paired", root) == 0
    worktree = worktree_cmd.worktree_path(root, "real-paired")
    paired = worktree / ".booley"
    assert (paired / ".git").is_file()
    assert git(paired, "symbolic-ref", "--short", "HEAD") == "booley-worktree/real-paired"
    assert git(paired, "rev-parse", "HEAD") == git(source, "rev-parse", "HEAD")
    assert not (paired / "runtime").exists()
    assert not git(source, "status", "--porcelain")
    for key, value in (
        ("core.autocrlf", "false"),
        ("submodule.recurse", "false"),
        ("diff.ignoreSubmodules", "all"),
        ("user.name", "Worktree Test"),
        ("user.email", "wt@test.invalid"),
    ):
        assert git(paired, "config", "--worktree", "--get", key) == value
    output = capsys.readouterr()
    assert output.out.strip() == str(worktree)
    assert (
        output.err.strip()
        .splitlines()[-1]
        .startswith("Created worktree 'real-paired' with a Paired Checkout.")
    )
    assert "#worktrees" in output.err and "git -C" not in output.err
    assert _new("real-paired", root) == 1
    error = capsys.readouterr().err
    assert "same Sandbox" in error and "Remove the Paired Checkout first" in error
    assert "Sandbox paths" in error and "WARNING" not in error
    commands = [line.split(": ", 1)[1] for line in error.splitlines() if "git -C" in line]
    expected = [
        ["git", "-C", str(source), "worktree", "remove", str(paired)],
        ["git", "-C", str(root), "worktree", "remove", str(worktree)],
        ["git", "-C", str(source), "branch", "-D", "booley-worktree/real-paired"],
        ["git", "-C", str(root), "branch", "-D", "wt/real-paired"],
    ]
    assert commands == [shlex.join(command) for command in expected]
    assert "Optional" in error and "preserving its commits" in error
    assert git(source, "config", "extensions.worktreeConfig") == "true"
    assert git(source, "config", "gc.worktreePruneExpire") == "never"
    for command, removed in zip(commands[:2], (paired, worktree), strict=True):
        run_bounded_child(shlex.split(command), check=True, timeout=10)
        assert not removed.exists()
    assert str(paired) not in git(source, "worktree", "list", "--porcelain")
    assert str(worktree) not in git(root, "worktree", "list", "--porcelain")
    assert git(source, "rev-parse", "booley-worktree/real-paired")
    run_bounded_child(shlex.split(commands[2]), check=True, timeout=10)
    assert _git(source, "rev-parse", "--verify", "booley-worktree/real-paired").returncode != 0


def test_python_pairing_accepts_detached_project_head(versioned_project, python_outer):
    from tests.goals.conftest import git

    root = versioned_project
    source = root / ".booley"
    git(source, "checkout", "--detach")
    try:
        assert _new("detached-project", root) == 0
        paired = worktree_cmd.worktree_path(root, "detached-project") / ".booley"
        assert git(paired, "rev-parse", "HEAD") == git(source, "rev-parse", "HEAD")
        assert git(paired, "symbolic-ref", "--short", "HEAD") == "booley-worktree/detached-project"
    finally:
        git(source, "checkout", "main")


@pytest.mark.parametrize("dirty", ["tracked", "untracked"])
def test_dirty_project_pairs_head_and_warns(versioned_project, python_outer, capsys, dirty):
    from tests.goals.conftest import git

    root = versioned_project
    source = root / ".booley"
    relative = "cores/top.core" if dirty == "tracked" else "untracked.sdc"
    path = source / relative
    committed = path.read_bytes() if path.exists() else None
    path.write_text("work in progress\n", encoding="utf-8")
    name = f"dirty-{dirty}"
    assert _new(name, root) == 0
    error = capsys.readouterr().err
    assert "WARNING" in error and relative in error
    assert "1 uncommitted or untracked file(s)" in error
    paired = worktree_cmd.worktree_path(root, name) / ".booley"
    assert git(paired, "rev-parse", "HEAD") == git(source, "rev-parse", "HEAD")
    assert git(paired, "symbolic-ref", "--short", "HEAD") == f"booley-worktree/{name}"
    if committed is None:
        assert not (paired / relative).exists()
    else:
        assert (paired / relative).read_bytes() == committed
    # The source keeps its uncommitted work untouched.
    assert path.read_text(encoding="utf-8") == "work in progress\n"


def _assert_creation_absent(root, name):
    from tests.goals.conftest import git

    worktree = worktree_cmd.worktree_path(root, name)
    assert not worktree.exists()
    for repository in (root, root / ".booley"):
        listing = git(repository, "worktree", "list", "--porcelain")
        assert worktree.as_posix() not in listing
    assert (
        _git(
            root / ".booley",
            "show-ref",
            "--verify",
            "--quiet",
            f"refs/heads/booley-worktree/{name}",
        ).returncode
        != 0
    )


def test_unborn_project_refuses_and_rolls_back(tmp_path, python_outer, capsys):
    from tests.goals.conftest import git

    root = tmp_path / "unborn"
    root.mkdir()
    git(root, "init", "-q")
    _write(root / "rtl.v", "module top; endmodule\n")
    git(root, "add", ".")
    git(root, "commit", "-qm", "RTL")
    _write(root / ".git/info/exclude", "/.booley\n")
    source = root / ".booley"
    _write(source / ".gitignore", "worktrees/\n")
    git(source, "init", "-q")
    assert _new("unborn", root) == 1
    assert "commit the Booley Directory's repository first" in capsys.readouterr().err
    _assert_creation_absent(root, "unborn")


def test_project_branch_collision_preserves_existing_branch(
    versioned_project, python_outer, capsys
):
    from tests.goals.conftest import git

    root = versioned_project
    source = root / ".booley"
    git(source, "branch", "booley-worktree/collision")
    head = git(source, "rev-parse", "booley-worktree/collision")
    assert _new("collision", root) == 1
    assert "already exists" in capsys.readouterr().err
    assert not worktree_cmd.worktree_path(root, "collision").exists()
    assert git(source, "rev-parse", "booley-worktree/collision") == head


def test_paired_caller_is_refused_before_outer_creation(versioned_project, python_outer, capsys):
    root = versioned_project
    assert _new("paired-caller", root) == 0
    caller = worktree_cmd.worktree_path(root, "paired-caller")
    assert _new("nested", caller) == 1
    assert "run booley worktree new from the primary workspace" in capsys.readouterr().err
    assert not worktree_cmd.worktree_path(caller, "nested").exists()


@pytest.mark.parametrize("failure", ["add", "after-add", "configure", "identity"])
def test_pairing_failure_rolls_back_both_repositories(
    versioned_project, python_outer, monkeypatch, capsys, failure
):
    from booley.runtime import project_worktree_pairing as pairing

    root = versioned_project
    name = f"failure-{failure}"
    if failure == "identity":

        def fail(*_args):
            raise pairing.ProjectPairingError("injected identity failure")

        monkeypatch.setattr(pairing, "apply_git_identity", fail)
    else:
        original = pairing._require_git

        def require(repository, *args):
            if failure == "after-add" and "add" in args:
                # Git created the branch (and possibly the checkout) before failing.
                original(repository, *args)
                raise pairing.ProjectPairingError("injected failure after add")
            if (failure == "add" and "add" in args) or (
                failure == "configure" and args[:2] == ("config", "--worktree")
            ):
                raise pairing.ProjectPairingError("injected failure")
            return original(repository, *args)

        monkeypatch.setattr(pairing, "_require_git", require)
    assert _new(name, root) == 1
    assert "injected" in capsys.readouterr().err
    _assert_creation_absent(root, name)


@_real_script
def test_snapshot_omits_canonical_transient_paths(project):
    paths = [
        "runtime/doctor/stale.lock",
        "runtime/jobs/slots/slot.json",
        "tickets/state/t.json",
        "tickets/waiver-candidates/w.json",
        ".runtime/build.bin",
        "flow-reports/report.json",
        "logs/log.txt",
        ".baseline-wt-stale/result",
        # Common state is never copied (ADR 0082), whatever git thinks of it.
        *NEW_RUNTIME_PATHS,
        "runtime/doctor_stamp.json",
        "runtime/developer_probe.json",
        "runtime/upgrade_review.json",
        "SETUP-REPORT.md",
        "FEEDBACK-REPORT.md",
    ]
    # Nested directories that merely share a transient name are configuration.
    preserved = ["cores/logs/keep.txt", "cores/flow-reports/keep.json"]
    for path in preserved:
        _write(project / ".booley" / path, "keep\n")
    for path in paths:
        _write(project / ".booley" / path, "stale\n")
    assert _new("transient-snapshot", project) == 0
    copied = worktree_cmd.worktree_path(project, "transient-snapshot") / ".booley"
    assert (copied / "booley.toml").is_file()
    assert all(not (copied / path).exists() for path in paths)
    assert all((copied / path).read_text(encoding="utf-8") == "keep\n" for path in preserved)


def _row_sample(pattern: str) -> str:
    """Return a path below (or equal to) one classification row's pattern."""
    path = pattern.replace("*", "x")
    return f"{path}sample.txt" if path.endswith("/") else path


# Rows whose sample the harness cannot plant: a `.booley/.git` makes creation refuse or
# pair (project_worktree_pairing), the layout lock and relocation record belong to the
# layout gate that wraps `booley worktree new`.
_UNPLANTABLE_ROWS = {".git/", "directory-layout.lock", ".directory-relocation.json"}


@_real_script
def test_snapshot_copies_only_versioned_checkout_state(project):
    from booley.core.booley_dir_parts import BOOLEY_DIR_PATHS, BooleyDirClass
    from booley.runtime import handles

    data = project / ".booley"
    samples = {
        pattern: _row_sample(pattern)
        for pattern in BOOLEY_DIR_PATHS
        if pattern not in _UNPLANTABLE_ROWS
    }
    # The planted rows are every row but the explicit exemptions.
    assert set(samples) | _UNPLANTABLE_ROWS == set(BOOLEY_DIR_PATHS)
    for sample in samples.values():
        _write(data / sample, "# planted\n")
    handles.register_job(data, "main-job", "sim", "codex:one")
    handle_store = handles.HandleStore(data, jobs=True).path.relative_to(data)
    assert handle_store.is_relative_to("runtime")

    assert _new("allowlist", project) == 0

    copied = worktree_cmd.worktree_path(project, "allowlist") / ".booley"
    for pattern, sample in samples.items():
        booley_class = BOOLEY_DIR_PATHS[pattern]
        assert (copied / sample).exists() == (booley_class is BooleyDirClass.VERSIONED), sample
    # Issue evidence: these Common and source-tied paths were copied before.
    for leaked in (
        "runtime/session_activity/sample.txt",
        "findings.jsonl",
        "mcp-tool-reports/sample.txt",
        str(handle_store),
    ):
        assert not (copied / leaked).exists(), leaked
    # The main Booley Directory is untouched.
    assert all((data / sample).is_file() for sample in samples.values())


@_real_script
def test_snapshot_skips_and_names_unclassified_paths(project, capsys):
    data = project / ".booley"
    _write(data / "eval_tmp" / "x", "stale\n")
    _write(data / "runtime" / "unknown.json", "{}\n")

    assert _new("unclassified", project) == 0

    copied = worktree_cmd.worktree_path(project, "unclassified") / ".booley"
    assert not (copied / "eval_tmp").exists()
    assert not (copied / "runtime" / "unknown.json").exists()
    skipped = [line for line in capsys.readouterr().err.splitlines() if "skipped" in line]
    assert len(skipped) == 2
    assert any("eval_tmp" in line and "runtime" not in line for line in skipped)
    assert any("runtime/unknown.json" in line for line in skipped)
    assert (data / "eval_tmp" / "x").is_file()


@_real_script
def test_snapshot_keeps_nested_locks_and_tmp_directories(project):
    _write(project / ".booley" / "cores" / "ip" / ".locks" / "keep", "keep\n")
    _write(project / ".booley" / "mcp_tools" / "t" / "tmp" / "keep", "keep\n")

    assert _new("nested-dirs", project) == 0

    copied = worktree_cmd.worktree_path(project, "nested-dirs") / ".booley"
    assert (copied / "cores" / "ip" / ".locks" / "keep").is_file()
    assert (copied / "mcp_tools" / "t" / "tmp" / "keep").is_file()


@_real_script
def test_new_snapshot_passes_the_common_state_check(project):
    from booley.core.booley_dir_parts import BOOLEY_DIR_PATHS, BooleyDirClass
    from booley.harness import doctor

    for pattern, booley_class in BOOLEY_DIR_PATHS.items():
        if booley_class is BooleyDirClass.COMMON and pattern not in _UNPLANTABLE_ROWS:
            _write(project / ".booley" / _row_sample(pattern), "# planted\n")
    assert _new("doctor-clean", project) == 0

    reporter = doctor._Reporter.create()
    doctor._check_common_state_in_worktrees(project, reporter)

    severities = [finding.severity for finding in reporter.findings or ()]
    assert "warn" not in severities and "skip" not in severities
    assert len(severities) == 1


@_real_script
def test_ci_driver_creates_pairing_through_user_command(versioned_project, tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / ".github/scripts"))
    import goal_mode_driver as driver

    from tests.goals.conftest import git

    # Exercise the installed CLI entry point from source in a disposable workspace.
    monkeypatch.setenv("BOOLEY_CONTAINER", "1")
    monkeypatch.setenv("PYTHONPATH", str(Path(__file__).resolve().parents[2] / "src"))
    workspace = driver.create_workspace(versioned_project, versioned_project / ".booley", tmp_path)
    paired = workspace.worktree / ".booley"
    assert (paired / ".git").is_file()
    assert git(paired, "symbolic-ref", "--short", "HEAD") == "booley-worktree/ci-demo"
    assert git(paired, "rev-parse", "HEAD") == git(workspace.booley_dir, "rev-parse", "HEAD")


def test_canonical_transient_classifier_matches_git_including_reincludes(versioned_project):
    from booley.runtime.project_gitignore import is_project_transient_path

    paths = [
        *NEW_RUNTIME_PATHS,
        *(f"{prefix}/{path}" for prefix in ("cores", "selftest") for path in NEW_RUNTIME_PATHS),
        "goals/g1/record.json",
        "goals/history/kept.md",
        "goals/history/tmp/state.json",
        "flow-reports/report.json",
        "cores/flow-reports/report.json",
        "logs/log",
        "cores/logs/log",
        "runtime/doctor_stamp.json",
        "cores/top.core",
        "foo/__pycache__/a.pyc",
        "foo/a.pyc",
        ".baseline-wt-old/state",
        "foo/.baseline-wt-old/state",
    ]
    for path in paths:
        result = _git(versioned_project / ".booley", "check-ignore", "--no-index", "--quiet", path)
        assert result.returncode in {0, 1}
        assert is_project_transient_path(path) == (result.returncode == 0), path


@pytest.mark.parametrize("problem", ["collision", "unborn"])
def test_precheck_refuses_before_outer_creation(versioned_project, monkeypatch, problem):
    from tests.goals.conftest import git

    source = versioned_project / ".booley"
    if problem == "collision":
        git(source, "branch", "booley-worktree/precheck")
    else:
        git(source, "symbolic-ref", "HEAD", "refs/heads/unborn")

    def unexpected(*_args, **_kwargs):
        pytest.fail("prechecks must run before building the outer checkout")

    monkeypatch.setattr(worktree_cmd, "_create_outer", unexpected)
    assert _new("precheck", versioned_project) == 1


@pytest.mark.parametrize("boundary", ["outer", "paired", "hook"])
def test_interrupt_rolls_back_and_reraises(versioned_project, python_outer, monkeypatch, boundary):
    from booley.runtime import project_worktree_pairing as pairing

    def interrupt(*_args, **_kwargs):
        raise KeyboardInterrupt("injected interrupt")

    if boundary == "outer":
        monkeypatch.setattr(worktree_cmd, "pair_project_worktree", interrupt)
    elif boundary == "paired":
        monkeypatch.setattr(pairing, "_configure_checkout", interrupt)
    else:
        _commit_hook(versioned_project / ".booley", "print('never reached')\n")
        monkeypatch.setattr(worktree_cmd, "prepare_project", interrupt)
    with pytest.raises(KeyboardInterrupt, match="injected interrupt"):
        _new("interrupted", versioned_project)
    _assert_creation_absent(versioned_project, "interrupted")


@_real_script
def test_real_script_rollback_removes_only_its_unlocked_name_lock(versioned_project, monkeypatch):
    from booley.runtime import project_worktree_pairing as pairing

    locks = versioned_project / ".booley/worktrees/.locks"
    _write(locks / "other.lock", "preserve\n")

    def interrupt(*_args):
        raise KeyboardInterrupt("after Project add")

    monkeypatch.setattr(pairing, "apply_git_identity", interrupt)
    with pytest.raises(KeyboardInterrupt):
        _new("interrupt-real", versioned_project)
    _assert_creation_absent(versioned_project, "interrupt-real")
    assert not (locks / "interrupt-real.lock").exists()
    assert (locks / "other.lock").read_text(encoding="utf-8") == "preserve\n"


def test_rollback_lock_cleanup_preserves_a_held_lock(tmp_path):
    from booley.runtime.file_lock import nonblocking_file_lock
    from booley.runtime.project_worktree_pairing import remove_creation_lock

    lock = tmp_path / ".booley/worktrees/.locks/held.lock"
    _write(lock, "owned elsewhere\n")
    with lock.open("r+", encoding="utf-8") as handle, nonblocking_file_lock(handle):
        remove_creation_lock(tmp_path, "held")
        assert lock.is_file()
    remove_creation_lock(tmp_path, "held")
    assert not lock.exists()


def test_failed_configuration_keeps_idempotent_repository_capability(
    versioned_project, python_outer, monkeypatch
):
    from booley.runtime import project_worktree_pairing as pairing
    from tests.goals.conftest import git

    source = versioned_project / ".booley"
    original = pairing.apply_git_identity

    def failure(*_args):
        raise pairing.GitIdentityError("identity failed")

    monkeypatch.setattr(pairing, "apply_git_identity", failure)
    assert _new("retry", versioned_project) == 1
    assert git(source, "config", "extensions.worktreeConfig") == "true"
    assert git(source, "config", "gc.worktreePruneExpire") == "never"
    assert Path(git(source, "rev-parse", "--show-toplevel")).samefile(source)
    assert not git(source, "status", "--porcelain")
    _assert_creation_absent(versioned_project, "retry")
    monkeypatch.setattr(pairing, "apply_git_identity", original)
    assert _new("retry", versioned_project) == 0
    assert not git(source, "status", "--porcelain")


@_real_script
def test_script_refusal_has_one_error_prefix(project, capsys):
    _write(worktree_cmd.worktree_path(project, "busy") / "keep.txt", "keep\n")
    assert _new("busy", project) == 1
    error = capsys.readouterr().err
    assert "ERROR: destination" in error or "ERROR: worktree destination" in error
    assert "ERROR: ERROR:" not in error


@_real_script
def test_goal_entry_and_finish_use_real_user_worktree(versioned_project, monkeypatch):
    from types import SimpleNamespace

    from booley.fusesoc.core_projection import reconcile_projected_cores
    from booley.goals.entry import EntryEnvironment, EntryRequest, enter_goal_mode
    from booley.goals.finish import finish_goal
    from booley.goals.model import parse_goal_args
    from booley.runtime.booley_dir import reset_cache
    from tests.goals.conftest import git
    from tests.goals.test_finish import environment, request
    from tests.goals.test_status import publish

    root = versioned_project
    control = root / ".booley"
    monkeypatch.setenv("BOOLEY_DIR", str(control))
    reset_cache()
    try:
        assert _new("user-goal", root) == 0
        worktree = worktree_cmd.worktree_path(root, "user-goal")
        reconcile_projected_cores(worktree)
        record = enter_goal_mode(
            EntryRequest(
                worktree, "user-goal", parse_goal_args([{"family": "LINT", "target": "top"}])
            ),
            EntryEnvironment(control, routing_guidance="Explicit test routing guidance."),
        ).record
        assert record.paired_project_base_sha == git(control, "rev-parse", "HEAD")
        layout = SimpleNamespace(main=root, control=control, worktree=worktree, record=record)
        publish(layout)
        assert finish_goal(request(layout), environment(layout))["status"] == "finished"
    finally:
        reset_cache()


def test_pairing_source_is_discovered_once_and_left_out_files_are_read_under_the_lock(
    versioned_project, python_outer, monkeypatch, capsys
):
    source_query = worktree_cmd.project_pairing_source
    create = worktree_cmd._create_outer
    queries = []

    def discover(root):
        queries.append(root)
        return source_query(root)

    def build(name, root, **kwargs):
        create(name, root, **kwargs)
        _write(root / ".booley/cores/top.core", "changed during outer creation\n")

    monkeypatch.setattr(worktree_cmd, "project_pairing_source", discover)
    monkeypatch.setattr(worktree_cmd, "_create_outer", build)
    assert _new("moving-inputs", versioned_project) == 0
    assert queries == [versioned_project]
    error = capsys.readouterr().err
    assert "WARNING" in error and "cores/top.core" in error
    paired = worktree_cmd.worktree_path(versioned_project, "moving-inputs") / ".booley"
    assert "changed during outer creation" not in (paired / "cores/top.core").read_text("utf-8")


_HOOK_MARKER = """\
import os, pathlib
pathlib.Path("hook-marker.txt").write_text(
    os.getcwd() + "\\n" + os.environ["BOOLEY_DIR"] + "\\n", encoding="utf-8"
)
print("hook says hi")
"""


def _commit_hook(source: Path, body: str) -> None:
    """Commit a ``post-setup.py`` hook into the Versioned Booley Directory."""
    from tests.goals.conftest import git

    _write(source / "hooks/post-setup.py", body)
    git(source, "add", "hooks/post-setup.py")
    git(source, "commit", "-qm", "Add post-setup hook")


def _hook_log(root: Path, name: str) -> Path:
    return root / ".booley/worktrees/.logs" / f"{name}.log"


@pytest.mark.parametrize("layout", ["paired", "snapshot"])
def test_post_setup_hook_runs_in_new_worktree_with_visible_output(layout, request, capsys):
    if layout == "paired":
        request.getfixturevalue("python_outer")
        root = request.getfixturevalue("versioned_project")
        _commit_hook(root / ".booley", _HOOK_MARKER)
        booley_dir = worktree_cmd.worktree_path(root, "hooked") / ".booley"
    else:
        if shutil.which("git") is None or os.name == "nt":
            pytest.skip("the Snapshot script needs git and a POSIX shell")
        root = request.getfixturevalue("project")
        _write(root / ".booley/hooks/post-setup.py", _HOOK_MARKER)
        booley_dir = worktree_cmd.worktree_path(root, "hooked") / ".booley"
    assert _new("hooked", root) == 0
    worktree = worktree_cmd.worktree_path(root, "hooked")
    cwd, hook_booley_dir = (worktree / "hook-marker.txt").read_text("utf-8").splitlines()
    assert Path(cwd).resolve() == worktree.resolve()
    assert Path(hook_booley_dir).resolve() == booley_dir.resolve()
    output = capsys.readouterr()
    assert output.out.strip() == str(worktree)
    lines = output.err.splitlines()
    fill = lines.index("Filling git submodules from the main checkout...")
    running = next(i for i, line in enumerate(lines) if line.startswith("Running post-setup hook"))
    visible = lines.index("hook says hi")
    assert fill < running < visible
    assert f"(log: {_hook_log(root, 'hooked')})" in lines[running]
    assert _hook_log(root, "hooked").read_text("utf-8").splitlines() == ["hook says hi"]


def test_no_hook_prints_only_the_submodule_progress_line(versioned_project, python_outer, capsys):
    assert _new("plain", versioned_project) == 0
    error = capsys.readouterr().err
    assert "Filling git submodules" in error and "post-setup" not in error
    assert not _hook_log(versioned_project, "plain").exists()


def test_post_setup_hook_runs_outside_the_layout_gate(
    versioned_project, python_outer, monkeypatch
):
    from booley.core.layout_lock import layout_gate

    _commit_hook(versioned_project / ".booley", "print('unused')\n")
    entered = []

    def probe(root, worktree, name):
        with layout_gate(root, timeout_s=1):  # raises if the creation still holds the gate
            entered.append(name)

    monkeypatch.setattr(worktree_cmd, "_run_post_setup", probe)
    assert _new("gate-free", versioned_project) == 0
    assert entered == ["gate-free"]


def test_failing_post_setup_hook_rolls_back_and_names_the_log(
    versioned_project, python_outer, capsys
):
    root = versioned_project
    source = root / ".booley"
    _commit_hook(source, "import sys\nprint('boom')\nsys.exit(3)\n")
    assert _new("failing", root) == 1
    error = capsys.readouterr().err
    log = _hook_log(root, "failing")
    assert "rc=3" in error and f"log: {log}" in error and "boom" in error
    assert log.read_text("utf-8").splitlines() == ["boom"]
    _assert_creation_absent(root, "failing")
    leftover = _git(source, "config", "--get-regexp", r"^branch\.booley-worktree/failing\.")
    assert leftover.returncode != 0 and not leftover.stdout
    # The outer branch is rolled back too, so the same name can be retried.
    assert _git(root, "show-ref", "--verify", "--quiet", "refs/heads/wt/failing").returncode != 0
    _commit_hook(source, "print('fixed')\n")
    assert _new("failing", root) == 0


def test_hook_flow_config_error_rolls_back(versioned_project, python_outer, monkeypatch, capsys):
    from booley.flows.execution import FlowConfigError

    def unreadable(*_args, **_kwargs):
        raise FlowConfigError("bad flow table")

    _commit_hook(versioned_project / ".booley", "print('unused')\n")
    monkeypatch.setattr(worktree_cmd, "flow_enabled", unreadable)
    assert _new("bad-flows", versioned_project) == 1
    assert "bad flow table" in capsys.readouterr().err
    _assert_creation_absent(versioned_project, "bad-flows")


def test_failed_regate_during_hook_rollback_is_reported(
    versioned_project, python_outer, monkeypatch, capsys
):
    from booley.core import layout_lock
    from booley.core.file_lock import LockTimeoutError

    _commit_hook(versioned_project / ".booley", "import sys\nsys.exit(1)\n")
    real_gate = layout_lock.layout_gate
    calls = []

    def gate(root, **kwargs):
        calls.append(root)
        if len(calls) == 2:  # the first call admits creation; the second guards the rollback
            raise LockTimeoutError("gate busy")
        return real_gate(root, **kwargs)

    monkeypatch.setattr(layout_lock, "layout_gate", gate)
    assert _new("busy-gate", versioned_project) == 1
    error = capsys.readouterr().err
    assert "outer rollback failed: gate busy" in error and "Remove the Paired Checkout" in error


def test_unexpected_runtime_error_is_rolled_back_and_reraised(
    versioned_project, python_outer, monkeypatch
):
    def unexpected(*_args, **_kwargs):
        raise RuntimeError("unexpected implementation defect")

    monkeypatch.setattr(worktree_cmd, "pair_project_worktree", unexpected)
    with pytest.raises(RuntimeError, match="unexpected implementation defect"):
        _new("unexpected", versioned_project)
    _assert_creation_absent(versioned_project, "unexpected")


@pytest.mark.parametrize("detached", [False, True])
def test_user_worktree_standalone_baseline_pins_pairing_base(
    versioned_project, python_outer, detached
):
    from booley.evidence.acceptance import PairedProjectBaseline
    from booley.flows.baseline_worktree import baseline_worktree
    from tests.goals.conftest import git

    root = versioned_project
    source = root / ".booley"
    base = git(source, "rev-parse", "HEAD")
    if detached:
        git(source, "checkout", "--detach")
    assert _new("baseline", root) == 0
    worktree = worktree_cmd.worktree_path(root, "baseline")
    paired = worktree / ".booley"
    assert git(paired, "config", "branch.booley-worktree/baseline.booleyBase") == base
    assert run_bounded_child(["git", "rev-parse", "@{upstream}"], cwd=paired).returncode != 0
    _write(paired / "cores/constraints/top.sdc", "# later design\n")
    git(paired, "add", ".")
    git(paired, "commit", "-qm", "later")
    with baseline_worktree(
        worktree, "HEAD", paired_project=PairedProjectBaseline.standalone()
    ) as baseline:
        frozen = baseline / ".booley"
        assert git(frozen, "rev-parse", "HEAD") == base
        assert "create_clock" in (frozen / "cores/constraints/top.sdc").read_text()
    assert not baseline.exists()
    assert (paired / "cores/constraints/top.sdc").read_text() == "# later design\n"


@pytest.mark.parametrize("missing_path", [False, True, "admin-only"])
def test_pairing_rollback_preserves_other_missing_registrations_and_removes_locked_own(
    versioned_project, python_outer, monkeypatch, missing_path
):
    from booley.runtime import project_worktree_pairing as pairing
    from tests.goals.conftest import git

    root = versioned_project
    source = root / ".booley"
    others = []
    for repository, path in (
        (root, root.parent / "missing-outer"),
        (source, root.parent / "missing-project"),
    ):
        git(repository, "worktree", "add", "--detach", str(path), "HEAD")
        admin = pairing.git_directories(path).git_dir
        shutil.rmtree(path)
        others.append(admin)

    def fail(_source, nested):
        git(source, "worktree", "lock", str(nested))
        if missing_path:
            shutil.rmtree(nested)
        raise pairing.ProjectPairingError("locked failure")

    if missing_path == "admin-only":
        original = pairing.run_git

        def missing_remove(repository, *args):
            if repository == source and args[:2] == ("worktree", "remove"):
                return subprocess.CompletedProcess(args, 1, "", "missing checkout")
            return original(repository, *args)

        monkeypatch.setattr(pairing, "run_git", missing_remove)
    monkeypatch.setattr(pairing, "_configure_checkout", fail)
    assert _new("locked", root) == 1
    _assert_creation_absent(root, "locked")
    assert all(admin.is_dir() for admin in others)
    assert (
        run_bounded_child(
            ["git", "config", "--get", "branch.booley-worktree/locked.booleyBase"], cwd=source
        ).returncode
        != 0
    )


def test_failed_add_preserves_a_moved_branch_it_did_not_create(
    versioned_project, python_outer, monkeypatch
):
    from booley.runtime import project_worktree_pairing as pairing
    from tests.goals.conftest import git

    source = versioned_project / ".booley"
    original = pairing._require_git
    # A branch with its own commit is never this run's: rollback must keep it.
    moved = git(source, "commit-tree", "HEAD^{tree}", "-p", "HEAD", "-m", "other work")

    def fail(repository, *args):
        if "add" in args:
            git(source, "branch", "booley-worktree/raced", moved)
            raise pairing.ProjectPairingError("branch created by another actor")
        return original(repository, *args)

    monkeypatch.setattr(pairing, "_require_git", fail)
    assert _new("raced", versioned_project) == 1
    assert git(source, "rev-parse", "booley-worktree/raced") == moved
    assert not worktree_cmd.worktree_path(versioned_project, "raced").exists()


def test_dirty_warning_lists_ten_then_count(versioned_project, python_outer, capsys):
    source = versioned_project / ".booley"
    for index in range(12):
        _write(source / f"extra{index:02d}.txt", "uncommitted\n")
    assert _new("bounded", versioned_project) == 0
    error = capsys.readouterr().err
    assert "12 uncommitted or untracked file(s)" in error
    assert "extra09.txt, and 2 more" in error
    assert "extra10.txt" not in error and "extra11.txt" not in error
    assert error.count("WARNING") == 1


@pytest.mark.parametrize("name", ["bad..ref", "bad.lock", "bad."])
def test_invalid_project_branch_is_refused_before_build(
    versioned_project, monkeypatch, capsys, name
):
    monkeypatch.setattr(
        worktree_cmd, "_create_outer", lambda *_a, **_kw: pytest.fail("invalid ref built")
    )
    assert _new(name, versioned_project) == 1
    assert "check-ref-format" in capsys.readouterr().err


@pytest.mark.parametrize("branch", ["booley-ticket/active", "other-user"])
def test_refused_destination_does_not_print_teardown_for_another_branch(
    versioned_project, python_outer, monkeypatch, capsys, branch
):
    from tests.goals.conftest import git

    root = versioned_project
    assert _new("occupied", root) == 0
    capsys.readouterr()
    nested = worktree_cmd.worktree_path(root, "occupied") / ".booley"
    git(nested, "branch", "-m", branch)

    def refuse(*_args, **_kwargs):
        raise worktree_cmd.WorktreeCreationError(
            "choose another name, or remove the old worktree once safe"
        )

    monkeypatch.setattr(worktree_cmd, "_create_outer", refuse)
    assert _new("occupied", root) == 1
    error = capsys.readouterr().err
    assert "choose another name" in error
    assert "git -C" not in error and "branch -D" not in error
    assert git(nested, "symbolic-ref", "--short", "HEAD") == branch


def test_unreadable_project_git_directory_is_refused(tmp_path, monkeypatch, capsys):
    (tmp_path / ".booley/.git").mkdir(parents=True)
    monkeypatch.setattr(
        worktree_cmd, "_create_outer", lambda *_a, **_kw: pytest.fail("broken repo copied")
    )
    assert _new("broken", tmp_path) == 1
    assert "git rev-parse --absolute-git-dir failed" in capsys.readouterr().err


def test_corrupt_project_git_directory_inside_workspace_is_refused(tmp_path, monkeypatch, capsys):
    from tests.goals.conftest import git

    # Git skips the empty .git directory and would otherwise resolve the outer one.
    git(tmp_path, "init", "-q", "-b", "main")
    (tmp_path / ".booley/.git").mkdir(parents=True)
    monkeypatch.setattr(
        worktree_cmd, "_create_outer", lambda *_a, **_kw: pytest.fail("broken repo copied")
    )
    assert _new("broken", tmp_path) == 1
    assert "cannot read the Booley Directory's repository" in capsys.readouterr().err


def test_existing_project_registration_is_refused_and_preserved(
    versioned_project, python_outer, capsys
):
    from tests.goals.conftest import git

    root = versioned_project
    source = root / ".booley"
    nested = worktree_cmd.worktree_path(root, "taken") / ".booley"
    git(source, "worktree", "add", "-q", "-b", "booley-ticket/taken", str(nested), "HEAD")
    git(source, "worktree", "lock", str(nested))
    shutil.rmtree(nested)

    assert _new("taken", root) == 1
    error = capsys.readouterr().err
    assert "same Sandbox" in error, "Registered-checkout removal venue missing"
    assert "already registers a checkout" in error
    assert shlex.join(["git", "-C", str(source), "worktree", "remove", str(nested)]) in error
    assert "preserve" in error and "locks" in error and "forcing removal" in error
    assert "remove --force" not in error
    assert nested.as_posix() in git(source, "worktree", "list", "--porcelain")
    assert git(source, "rev-parse", "booley-ticket/taken")


@_real_script
def test_paired_hint_skips_project_snapshot(versioned_project, monkeypatch):
    from booley.runtime import project_worktree_pairing as pairing

    original = pairing._remove_copy
    seen = []

    def remove(worktree):
        seen.append((worktree / ".booley").exists())
        return original(worktree)

    monkeypatch.setattr(pairing, "_remove_copy", remove)
    assert _new("no-copy", versioned_project) == 0
    assert seen == [False]


@pytest.mark.parametrize(
    ("failure", "message"),
    [
        (subprocess.TimeoutExpired(cmd="bash", timeout=1), "timed out"),
        (OSError("bash missing"), "failed to start: bash missing"),
    ],
)
def test_outer_creation_failures_become_creation_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: BaseException, message: str
) -> None:
    def fail(*_args: object, **_kwargs: object) -> None:
        raise failure

    monkeypatch.setattr(worktree_cmd.subprocess, "run", fail)
    with pytest.raises(worktree_cmd.WorktreeCreationError, match=message):
        worktree_cmd._create_outer("wt", tmp_path, paired_project=False, new_branch=None)


def test_failed_outer_rollback_is_reported_and_noted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def broken_rollback(*_args: object) -> None:
        raise OSError("worktree busy")

    monkeypatch.setattr(worktree_cmd, "rollback_outer_worktree", broken_rollback)
    failure = RuntimeError("pairing failed")

    worktree_cmd._rollback_creation(tmp_path, tmp_path / "wt", "wt", failure)

    assert "outer rollback failed: worktree busy" in capsys.readouterr().err
    assert failure.__notes__ == ["outer rollback failed: worktree busy"]


@pytest.mark.timeout(120)  # Windows runs both init paths; see the headroom guard (#1202).
def test_worktree_new_after_managed_bundle_runtime_init(
    tmp_path, monkeypatch, python_outer, capsys
):
    from tests.harness.test_init_rerun import _managed_git, _run_full_init
    from tests.harness.test_init_rerun import repo as init_repo

    # Reuse host-provisioning isolation only: all authored init writers and
    # managed hook publication remain real on both invocations.
    root = init_repo.__wrapped__(tmp_path, monkeypatch)
    assert _run_full_init(root) == 0
    data = root / ".booley"
    for owner in (root, data):
        _managed_git(owner, "config", "user.name", "Fixture")
        _managed_git(owner, "config", "user.email", "fixture@example.invalid")
    bundle = data / ".managed/project-git-hooks.pyz"
    bundle.unlink()
    _managed_git(data, "add", ".")
    _managed_git(data, "commit", "-qm", "prepared authored Project")
    (root / "rtl.v").write_text("module top; endmodule\n")
    _managed_git(root, "add", ".")
    _managed_git(root, "commit", "-qm", "prepared RTL")
    baseline = _managed_git(data, "rev-parse", "HEAD")
    assert _run_full_init(root) == 0
    assert _new("runtime-init", root) == 0
    assert "WARNING" not in capsys.readouterr().err
    paired = worktree_cmd.worktree_path(root, "runtime-init") / ".booley"
    assert bundle.is_file()
    assert not (paired / ".managed/project-git-hooks.pyz").exists()
    assert _managed_git(data, "rev-parse", "HEAD") == baseline


def test_tracked_bundle_manual_remedy_preserves_copy_and_allows_pairing(
    versioned_project, python_outer, capsys, monkeypatch
):
    import shlex

    from booley.harness.init_cmd import InitContext, _step_booley_dir
    from booley.harness.setup.project_git_hook_reconcile import step_project_git_hooks
    from booley.runtime.booley_dir import reset_cache
    from tests.harness.test_init_rerun import _managed_git

    root = versioned_project
    data = root / ".booley"
    monkeypatch.setenv("BOOLEY_DIR", str(data))
    for owner in (root, data):
        _managed_git(owner, "config", "user.name", "Fixture")
        _managed_git(owner, "config", "user.email", "fixture@example.invalid")
    reset_cache()
    _step_booley_dir(InitContext(project_root=root))
    step_project_git_hooks(InitContext(project_root=root))
    bundle = data / ".managed/project-git-hooks.pyz"
    _managed_git(data, "add", ".")
    _managed_git(data, "add", "-f", str(bundle))
    _managed_git(data, "commit", "-qm", "legacy tracked bundle")
    capsys.readouterr()
    before = _managed_git(data, "ls-files", "--stage", "-z")
    step_project_git_hooks(InitContext(project_root=root))
    command = shlex.join(
        ["git", "-C", str(data), "rm", "--cached", "--", ".managed/project-git-hooks.pyz"]
    )
    assert command in capsys.readouterr().out
    assert _managed_git(data, "ls-files", "--stage", "-z") == before
    run_bounded_child(shlex.split(command), check=True, timeout=10)
    assert bundle.is_file()
    _managed_git(data, "commit", "-qm", "intentional generated bundle removal")
    _step_booley_dir(InitContext(project_root=root))
    step_project_git_hooks(InitContext(project_root=root))
    capsys.readouterr()
    assert _new("manual-remedy", root) == 0
    assert "WARNING" not in capsys.readouterr().err
    paired = worktree_cmd.worktree_path(root, "manual-remedy") / ".booley"
    assert bundle.is_file()
    assert not (paired / ".managed/project-git-hooks.pyz").exists()
    reset_cache()


# ---------------------------------------------------------------------------
# Git submodules in a created worktree and in Goal entry (#1960)
# ---------------------------------------------------------------------------


def _sub_git(cwd: Path, *args: str) -> str:
    """Run git with the file protocol allowed (fixture-only) and return stripped stdout."""
    result = _git(
        cwd,
        "-c",
        "protocol.file.allow=always",
        "-c",
        "user.name=Sub Test",
        "-c",
        "user.email=sub@test.invalid",
        *args,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def _commit_source(repo: Path, text: str) -> str:
    _write(repo / "source.sv", text)
    _sub_git(repo, "add", "-A")
    _sub_git(repo, "commit", "-qm", text.strip())
    return _sub_git(repo, "rev-parse", "HEAD")


def _new_repo(path: Path) -> Path:
    path.mkdir()
    _sub_git(path, "init", "-q", "-b", "main")
    return path


@pytest.fixture
def submodule_project(versioned_project, tmp_path):
    """A versioned Project with selected, nested, unselected and paired submodules.

    The outer repository pins ``vendor/ip`` to the older of two commits, so a
    populated copy that merely follows the source HEAD would be visibly wrong.
    """
    root = versioned_project
    dependency = _new_repo(tmp_path / "dependency")
    pinned = _commit_source(dependency, "old\n")
    _commit_source(dependency, "new\n")
    leaf = _new_repo(tmp_path / "leaf")
    _commit_source(leaf, "leaf\n")
    middle = _new_repo(tmp_path / "middle")
    _commit_source(middle, "middle\n")
    _sub_git(middle, "submodule", "add", str(leaf), "deps/leaf")
    _sub_git(middle, "commit", "-qm", "leaf")
    for path in ("vendor/ip", "vendor/big"):
        _sub_git(root, "submodule", "add", str(dependency), path)
    _sub_git(root / "vendor/ip", "checkout", "-q", pinned)
    _sub_git(root, "submodule", "add", str(middle), "vendor/middle")
    _sub_git(root / "vendor/middle", "submodule", "update", "--init")
    _sub_git(root, "add", "-A")
    _sub_git(root, "commit", "-qm", "submodules")
    control = root / ".booley"
    _sub_git(control, "submodule", "add", "-f", str(dependency), "vendor/paired")
    with (control / "booley.toml").open("a", encoding="utf-8") as config:
        config.write('[submodules]\npaths = ["vendor/ip", "vendor/middle"]\n')
    _sub_git(control, "add", "-A")
    _sub_git(control, "commit", "-qm", "paired submodule")
    return root


def _pin(root: Path) -> str:
    """The commit the outer repository pins ``vendor/ip`` to."""
    return _sub_git(root, "rev-parse", "HEAD:vendor/ip")


def _head(path: Path) -> str:
    return _sub_git(path, "rev-parse", "HEAD")


def test_worktree_new_populates_selected_and_nested_submodules(
    submodule_project, python_outer, monkeypatch
):
    monkeypatch.setenv("GIT_SSH", "/definitely/no/ssh")

    assert _new("sub", submodule_project) == 0

    worktree = worktree_cmd.worktree_path(submodule_project, "sub")
    assert _head(worktree / "vendor/ip") == _pin(submodule_project)
    assert (worktree / "vendor/ip/source.sv").read_text(encoding="utf-8") == "old\n"
    assert (worktree / "vendor/middle/deps/leaf/source.sv").is_file()
    assert not any((worktree / "vendor/big").iterdir())
    assert _sub_git(worktree / "vendor/ip", "remote") == ""


def test_worktree_new_populates_paired_checkout_gitlinks(submodule_project, python_outer):
    assert _new("sub", submodule_project) == 0

    paired = worktree_cmd.worktree_path(submodule_project, "sub") / ".booley"
    assert _head(paired / "vendor/paired") == _sub_git(paired, "rev-parse", "HEAD:vendor/paired")
    assert (paired / "vendor/paired/source.sv").is_file()


@pytest.mark.parametrize(
    ("problem", "message"),
    [
        pytest.param("missing", "initialize it first", id="missing"),
        pytest.param("dirty", "is dirty", id="dirty"),
        pytest.param("shallow", "is shallow", id="shallow"),
        pytest.param("incomplete", "incomplete", id="incomplete"),
    ],
)
def test_worktree_new_invalid_submodule_source_rolls_back(
    submodule_project, python_outer, capsys, problem, message
):
    root = submodule_project
    source = root / "vendor/ip"
    if problem == "missing":
        shutil.rmtree(source)
        source.mkdir()
    elif problem == "dirty":
        _write(source / "scratch.txt", "untracked\n")
    elif problem == "shallow":
        git_dir = Path(_sub_git(source, "rev-parse", "--absolute-git-dir"))
        _write(git_dir / "shallow", _head(source) + "\n")
    else:
        _sub_git(root, "update-index", "--cacheinfo", f"160000,{'1' * 40},vendor/ip")
        _sub_git(root, "commit", "-qm", "unreachable pin")

    assert _new("sub", root) == 1

    error = capsys.readouterr().err
    assert "Submodule setup failed offline:" in error
    assert message in error
    _assert_creation_absent(root, "sub")
    assert (
        _git(root / ".booley", "config", "--get", "branch.booley-worktree/sub.booleyBase").stdout
        == ""
    )


def _enter_goal(worktree: Path, control: Path, on_boundary=None, slug: str = "sub-goal"):
    from booley.goals.entry import EntryEnvironment, EntryRequest, enter_goal_mode
    from booley.goals.model import parse_goal_args

    request = EntryRequest(worktree, slug, parse_goal_args([{"family": "LINT", "target": "top"}]))
    environment = EntryEnvironment(
        control, routing_guidance="Explicit test routing guidance.", on_boundary=on_boundary
    )
    return enter_goal_mode(request, environment)


@pytest.fixture
def goal_project(submodule_project, monkeypatch):
    """The submodule Project with the real worktree script and its Booley Directory selected."""
    from booley.runtime.booley_dir import reset_cache

    control = submodule_project / ".booley"
    monkeypatch.setenv("BOOLEY_DIR", str(control))
    reset_cache()
    yield submodule_project
    reset_cache()


def _new_goal_worktree(root: Path) -> Path:
    from booley.fusesoc.core_projection import reconcile_projected_cores

    assert _new("sub-goal", root) == 0
    worktree = worktree_cmd.worktree_path(root, "sub-goal")
    reconcile_projected_cores(worktree)
    return worktree


def _empty(directory: Path) -> None:
    shutil.rmtree(directory)
    directory.mkdir()


@_real_script
def test_goal_entry_populates_empty_submodules_idempotently(goal_project):
    root, control = goal_project, goal_project / ".booley"
    worktree = _new_goal_worktree(root)
    _empty(worktree / "vendor/ip")

    class Interrupted(BaseException):
        pass

    def interrupt(boundary: str) -> None:
        if boundary == "created":
            raise Interrupted

    with pytest.raises(Interrupted):
        _enter_goal(worktree, control, interrupt)
    assert _head(worktree / "vendor/ip") == _pin(root)
    populated = ((worktree / "vendor/ip/.git").stat().st_ino, _head(worktree / "vendor/ip"))
    status = _sub_git(worktree, "status", "--porcelain", "--ignore-submodules=none")

    _enter_goal(worktree, control, slug="sub-goal-resumed")

    assert (
        (worktree / "vendor/ip/.git").stat().st_ino,
        _head(worktree / "vendor/ip"),
    ) == populated
    assert _sub_git(worktree, "status", "--porcelain", "--ignore-submodules=none") == status


@_real_script
def test_goal_entry_invalid_submodule_source_leaves_no_partial_state(goal_project):
    from booley.goals.entry import GoalEntryError

    root, control = goal_project, goal_project / ".booley"
    worktree = _new_goal_worktree(root)
    _empty(worktree / "vendor/ip")
    _empty(worktree / ".booley/vendor/paired")
    _write(root / ".booley/vendor/paired/scratch.txt", "untracked\n")

    with pytest.raises(GoalEntryError, match=r"Submodule setup failed offline:.*is dirty"):
        _enter_goal(worktree, control)

    assert not any((worktree / "vendor/ip").iterdir())
    assert not any((worktree / ".booley/vendor/paired").iterdir())


@_real_script
def test_goal_worktree_baseline_finds_submodules(goal_project):
    from booley.flows.baseline_worktree import baseline_worktree

    root, control = goal_project, goal_project / ".booley"
    worktree = _new_goal_worktree(root)
    _enter_goal(worktree, control)

    with baseline_worktree(worktree, "HEAD") as baseline:
        assert (baseline / "vendor/ip/source.sv").read_text(encoding="utf-8") == "old\n"


def test_removal_steps_keep_symlinked_paths_as_printed(tmp_path: Path) -> None:
    # Codespaces demo: `.booley` is a symlink to a sibling repo. The steps must
    # name the same path `worktree new` printed, not its resolved target.
    real = tmp_path / "real-project"
    (real / ".booley").mkdir(parents=True)
    link = tmp_path / "linked-project"
    link.symlink_to(real, target_is_directory=True)
    worktree = link / ".booley" / "worktrees" / "zbb"

    steps = worktree_cmd.worktree_removal_instructions(link, worktree, "zbb", branch="booley/zbb")

    assert shlex.join(["git", "-C", str(link), "worktree", "remove", str(worktree)]) in steps
    assert str(real) not in steps


# A relocated or external Booley Directory (#2916)


def _configured_project(tmp_path: Path, monkeypatch, directory: str) -> Path:
    """A committed Git Project whose root ``booley.toml`` sets ``[project].dir``."""
    root = tmp_path / "repo"
    root.mkdir()
    for args in (
        ("init",),
        ("config", "user.name", "Test User"),
        ("config", "user.email", "test@example.com"),
    ):
        assert _git(root, *args).returncode == 0
    _write(root / "README.md", "hello\n")
    _write(root / "booley.toml", f'[project]\ndir = "{directory}"\n')
    assert _git(root, "add", "README.md", "booley.toml").returncode == 0
    assert _git(root, "commit", "-m", "init").returncode == 0
    return root


def _worktree_paths(root: Path) -> list[str]:
    listing = _git(root, "worktree", "list", "--porcelain").stdout
    return [line for line in listing.splitlines() if line.startswith("worktree ")]


@_real_script
def test_relocated_booley_dir_hosts_snapshot_worktree(tmp_path, monkeypatch, capsys) -> None:
    root = _configured_project(tmp_path, monkeypatch, "state")
    _write(root / "state" / "booley.toml", "[submodules]\npaths = []\n")

    assert _new("demo", root) == 0

    worktree = root / "state" / "worktrees" / "demo"
    assert capsys.readouterr().out == f"{worktree}\n"
    assert (worktree / ".git").is_file()
    assert (root / "state" / "worktrees" / ".locks" / "demo.lock").is_file()
    assert (root / "state" / "FUSESOC_IGNORE").is_file()
    assert (worktree / "state" / "booley.toml").is_file()
    assert not (root / ".booley").exists()


@_real_script
def test_relocated_worktree_builds_and_discovers_in_its_own_directory(
    tmp_path, monkeypatch, capsys
) -> None:
    """#2917 (AC1): Flow builds and core discovery use ``<checkout>/state`` in main and worktree.

    Host-side: a Sandbox cannot be issued for a project-authored ``[project].dir``.
    """
    from booley.core.build_paths import work_root_for
    from booley.fusesoc.fusesoc_registry import discover_cores

    root = _configured_project(tmp_path, monkeypatch, "state")
    _write(root / "state" / "booley.toml", "[submodules]\npaths = []\n")
    _write(root / "state" / "cores" / "ip" / "top.core", "CAPI=2:\nname: x:ip:top:1.0\n")

    assert _new("demo", root) == 0

    worktree = root / "state" / "worktrees" / "demo"
    capsys.readouterr()
    for checkout in (root, worktree):
        assert work_root_for(checkout, "sim", "cfg") == (
            checkout / "state" / ".runtime" / "edalize" / "sim" / "cfg"
        )
        assert discover_cores(checkout) == [checkout / "state" / "cores" / "ip" / "top.core"]
    assert not (root / ".booley").exists()
    assert not (worktree / ".booley").exists()


@pytest.mark.parametrize("source", ["project-dir", "ambient"])
@_real_script
def test_booley_dir_outside_checkout_is_refused_without_leftovers(
    tmp_path, monkeypatch, capsys, source
) -> None:
    external = tmp_path / "external"
    external.mkdir()
    root = _configured_project(
        tmp_path, monkeypatch, external.as_posix() if source == "project-dir" else "state"
    )
    _write(root / "state" / "booley.toml", "[submodules]\npaths = []\n")
    if source == "ambient":
        monkeypatch.setenv("BOOLEY_DIR", str(external))
    branches = _git(root, "branch", "--list").stdout

    assert _new("demo", root) == 1

    err = capsys.readouterr().err
    assert str(external) in err and "relative [project].dir" in err
    assert _worktree_paths(root) == [f"worktree {root}"]
    assert _git(root, "branch", "--list").stdout == branches
    assert not (root / ".booley").exists()
    assert not (root / "state" / "worktrees").exists()
    assert not (external / "worktrees").exists()


@pytest.mark.parametrize("case", ["inside-mismatch", "missing"])
@_real_script
def test_booley_dir_mismatch_or_missing_is_refused(tmp_path, monkeypatch, capsys, case) -> None:
    root = _configured_project(tmp_path, monkeypatch, "state")
    if case == "inside-mismatch":
        _write(root / "state" / "booley.toml", "[submodules]\npaths = []\n")
        _write(root / ".booley" / "booley.toml", "[submodules]\npaths = []\n")
        monkeypatch.setenv("BOOLEY_DIR", str(root / ".booley"))
    branches = _git(root, "branch", "--list").stdout

    assert _new("demo", root) == 1

    err = capsys.readouterr().err
    if case == "missing":
        assert f"Booley Directory {root / 'state'} does not exist" in err
    else:
        assert str(root / ".booley") in err and str(root / "state") in err
    assert _worktree_paths(root) == [f"worktree {root}"]
    assert _git(root, "branch", "--list").stdout == branches
    assert not (root / "state" / "worktrees").exists()
    assert not (root / ".booley" / "worktrees").exists()


@_real_script
def test_booley_dir_ambient_matching_own_directory_is_accepted(
    tmp_path, monkeypatch, capsys
) -> None:
    """A Sandbox's ``BOOLEY_DIR`` is an alias of the checkout's own directory."""
    root = _configured_project(tmp_path, monkeypatch, "state")
    _write(root / "state" / "booley.toml", "[submodules]\npaths = []\n")
    alias = tmp_path / "booley-project"
    alias.symlink_to(root / "state", target_is_directory=True)
    monkeypatch.setenv("BOOLEY_DIR", str(alias))

    assert _new("demo", root) == 0

    assert capsys.readouterr().out == f"{root / 'state' / 'worktrees' / 'demo'}\n"
    # Paths recorded by Git never spell the alias.
    worktree = root / "state" / "worktrees" / "demo"
    assert str(alias) not in (worktree / ".git").read_text(encoding="utf-8")


@_real_script
def test_new_worktree_resolves_its_own_booley_dir(tmp_path, monkeypatch, capsys) -> None:
    """A ``<branch>--<desc>`` worktree may carry another ``[project].dir`` than main."""
    root = _configured_project(tmp_path, monkeypatch, "state")
    _write(root / "state" / "booley.toml", "[submodules]\npaths = []\n")
    for branch, directory in (("relocated", "other"), ("external", (tmp_path / "x").as_posix())):
        assert _git(root, "checkout", "-q", "-b", branch).returncode == 0
        _write(root / "booley.toml", f'[project]\ndir = "{directory}"\n')
        assert _git(root, "commit", "-qam", branch).returncode == 0
        assert _git(root, "checkout", "-q", "-").returncode == 0

    assert _new("relocated--try", root) == 0
    worktree = root / "state" / "worktrees" / "relocated--try"
    assert capsys.readouterr().out == f"{worktree}\n"
    assert (worktree / "other" / "booley.toml").is_file()
    assert not (worktree / "state").exists()

    assert _new("external--try", root) == 1
    assert "relative [project].dir" in capsys.readouterr().err
    assert not (root / "state" / "worktrees" / "external--try").exists()
    assert (
        str(root / "state" / "worktrees" / "external--try")
        not in _git(root, "worktree", "list", "--porcelain").stdout
    )


@pytest.fixture
def relocated_versioned_project(tmp_path, monkeypatch):
    """A Stealth outer repository whose Versioned Booley Directory lives at ``state``."""
    from booley.runtime.project_gitignore import PROJECT_GITIGNORE
    from tests.goals.conftest import git
    from tests.goals.stealth_support import CORE

    monkeypatch.delenv("BOOLEY_DIR", raising=False)
    root = tmp_path / "relocated" / "main"
    root.mkdir(parents=True)
    git(root, "init", "-q", "-b", "main")
    _write(root / "rtl.v", "module top; endmodule\n")
    _write(root / "booley.toml", '[project]\ndir = "state"\n')
    git(root, "add", ".")
    git(root, "commit", "-qm", "RTL")
    _write(root / ".git/info/exclude", "/state\n")
    source = root / "state"
    _write(source / ".gitignore", PROJECT_GITIGNORE)
    _write(
        source / "booley.toml",
        "[agent.git]\nname='Worktree Test'\nemail='wt@test.invalid'\n",
    )
    _write(source / "cores/top.core", CORE)
    git(source, "init", "-q", "-b", "main")
    git(source, "add", ".")
    git(source, "commit", "-qm", "Project")
    return root


@_real_script
def test_relocated_versioned_booley_dir_pairs_inside_worktree(
    relocated_versioned_project, capsys
) -> None:
    from tests.goals.conftest import git

    root = relocated_versioned_project
    assert _new("demo", root) == 0

    worktree = root / "state" / "worktrees" / "demo"
    assert capsys.readouterr().out.startswith(str(worktree))
    paired = worktree / "state"
    assert (paired / ".git").is_file()
    assert git(paired, "symbolic-ref", "--short", "HEAD") == "booley-worktree/demo"
    assert (root / "state" / "worktrees" / "FUSESOC_IGNORE").is_file()
    assert worktree_cmd._is_named_pair(worktree, "demo")


def test_relocated_pairing_failure_rolls_back_both_repositories(
    relocated_versioned_project, monkeypatch, capsys
) -> None:
    from booley.runtime import project_worktree_pairing as pairing
    from tests.goals.conftest import git

    root = relocated_versioned_project

    def create(name, project_root, *, paired_project, new_branch=None):
        destination = worktree_cmd.worktree_path(project_root, name)
        git(project_root, "worktree", "add", "--detach", str(destination), "HEAD")

    def fail(*_args):
        raise pairing.ProjectPairingError("injected identity failure")

    monkeypatch.setattr(worktree_cmd, "_create_outer", create)
    monkeypatch.setattr(pairing, "apply_git_identity", fail)

    assert _new("failure", root) == 1

    assert "injected" in capsys.readouterr().err
    worktree = root / "state" / "worktrees" / "failure"
    assert not worktree.exists()
    assert str(worktree) not in git(root, "worktree", "list", "--porcelain")
    assert str(worktree / "state") not in git(root / "state", "worktree", "list", "--porcelain")
    assert (
        _git(
            root / "state", "show-ref", "--verify", "--quiet", "refs/heads/booley-worktree/failure"
        ).returncode
        != 0
    )
