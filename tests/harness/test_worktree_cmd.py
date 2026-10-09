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


@pytest.fixture(scope="module")
def versioned_project(tmp_path_factory):
    """A clean Stealth outer repository with a standalone Project at its HEAD."""
    from booley.runtime.project_gitignore import PROJECT_GITIGNORE
    from tests.goals.conftest import git
    from tests.goals.test_stealth_inputs import CORE

    root = tmp_path_factory.mktemp("paired-user") / "main"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    _write(root / "rtl.v", "module top; endmodule\n")
    git(root, "add", ".")
    git(root, "commit", "-qm", "RTL")
    _write(root / ".git/info/exclude", "/.booley_project\n/.booley-projected-*.core\n")
    source = root / ".booley_project"
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

    def create(name, root):
        destination = worktree_cmd.worktree_path(root, name)
        git(root, "worktree", "add", "--detach", str(destination), "HEAD")
        _write(destination / ".booley_project/booley.toml", "# script snapshot\n")

    monkeypatch.setattr(worktree_cmd, "_create_outer", create)


@_real_script
def test_user_command_pairs_committed_project_and_prints_removal(versioned_project, capsys):
    from tests.goals.conftest import git

    root = versioned_project
    source = root / ".booley_project"
    assert _new("real-paired", root) == 0
    worktree = worktree_cmd.worktree_path(root, "real-paired")
    paired = worktree / ".booley_project"
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
    assert output.err.index("git -C .booley_project worktree remove") < output.err.index(
        "Then: git worktree remove"
    )
    assert _new("real-paired", root) == 1
    assert "remove the paired Project first" in capsys.readouterr().err


def test_python_pairing_accepts_detached_project_head(versioned_project, python_outer):
    from tests.goals.conftest import git

    root = versioned_project
    source = root / ".booley_project"
    git(source, "checkout", "--detach")
    try:
        assert _new("detached-project", root) == 0
        paired = worktree_cmd.worktree_path(root, "detached-project") / ".booley_project"
        assert git(paired, "rev-parse", "HEAD") == git(source, "rev-parse", "HEAD")
        assert git(paired, "symbolic-ref", "--short", "HEAD") == "booley-worktree/detached-project"
    finally:
        git(source, "checkout", "main")


@pytest.mark.parametrize("dirty", ["tracked", "untracked"])
def test_dirty_project_refuses_and_rolls_back(versioned_project, python_outer, capsys, dirty):
    from tests.goals.conftest import git

    root = versioned_project
    source = root / ".booley_project"
    path = source / ("cores/top.core" if dirty == "tracked" else "untracked.sdc")
    before = path.read_bytes() if path.exists() else None
    path.write_text("work in progress\n", encoding="utf-8")
    name = f"dirty-{dirty}"
    try:
        assert _new(name, root) == 1
        error = capsys.readouterr().err
        assert path.relative_to(source).as_posix() in error
        assert "commit them in `.booley_project` first" in error
        _assert_creation_absent(root, name)
        assert path.read_text(encoding="utf-8") == "work in progress\n"
    finally:
        if before is None:
            path.unlink()
        else:
            path.write_bytes(before)
        assert not git(source, "status", "--porcelain")


def _assert_creation_absent(root, name):
    from tests.goals.conftest import git

    worktree = worktree_cmd.worktree_path(root, name)
    assert not worktree.exists()
    for repository in (root, root / ".booley_project"):
        listing = git(repository, "worktree", "list", "--porcelain")
        assert worktree.as_posix() not in listing
    assert (
        _git(
            root / ".booley_project",
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
    _write(root / ".git/info/exclude", "/.booley_project\n")
    source = root / ".booley_project"
    _write(source / ".gitignore", "worktrees/\n")
    git(source, "init", "-q")
    assert _new("unborn", root) == 1
    assert "commit the Project repository first" in capsys.readouterr().err
    _assert_creation_absent(root, "unborn")


def test_project_branch_collision_preserves_existing_branch(
    versioned_project, python_outer, capsys
):
    from tests.goals.conftest import git

    root = versioned_project
    source = root / ".booley_project"
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


@pytest.mark.parametrize("failure", ["add", "configure", "identity"])
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
            if (failure == "add" and "add" in args) or (
                failure == "configure" and args[:2] == ("config", "--worktree")
            ):
                if failure == "add":
                    original(repository, *args)
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
    ]
    for path in paths:
        _write(project / ".booley_project" / path, "stale\n")
    assert _new("transient-snapshot", project) == 0
    copied = worktree_cmd.worktree_path(project, "transient-snapshot") / ".booley_project"
    assert (copied / "booley.toml").is_file()
    assert all(not (copied / path).exists() for path in paths)


@_real_script
def test_ci_driver_creates_pairing_through_user_command(versioned_project, tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / ".github/scripts"))
    import goal_mode_driver as driver

    from tests.goals.conftest import git

    # Exercise the installed CLI entry point from source in a disposable workspace.
    monkeypatch.setenv("BOOLEY_CONTAINER", "1")
    monkeypatch.setenv("PYTHONPATH", str(Path(__file__).resolve().parents[2] / "src"))
    workspace = driver.create_workspace(
        versioned_project, versioned_project / ".booley_project", tmp_path
    )
    paired = workspace.worktree / ".booley_project"
    assert (paired / ".git").is_file()
    assert git(paired, "symbolic-ref", "--short", "HEAD") == "booley-worktree/ci-demo"
    assert git(paired, "rev-parse", "HEAD") == git(workspace.project_dir, "rev-parse", "HEAD")
