"""`booley worktree prepare`: parsing, venue, and preparation."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from booley.core.host_shell import quote_arg
from booley.harness import booley as tlr
from booley.harness import incontainer_post_setup, worktree_cmd
from booley.runtime import runtime_context
from tests.project_runtime_git_support import NEW_RUNTIME_PATHS
from tests.subprocess_budget import run_bounded_child


def test_worktree_requires_a_subcommand(capsys) -> None:
    with pytest.raises(SystemExit) as exc:
        tlr._build_parser().parse_args(["worktree"])

    assert exc.value.code == 2
    assert "the following arguments are required: {prepare}" in capsys.readouterr().err


def test_worktree_is_sandbox_only_and_project_bound(monkeypatch, capsys) -> None:
    assert tlr.COMMAND_LOCATIONS["worktree"] is tlr.CommandLocation.SESSION_RUNTIME
    assert tlr.COMMAND_PROJECT_BINDINGS["worktree"] is tlr.ProjectBinding.REQUIRED
    monkeypatch.setattr(runtime_context, "inside_session_runtime", lambda: False)

    with pytest.raises(SystemExit) as exc:
        tlr._enforce_runtime_location("worktree")

    assert exc.value.code == 2
    assert "runs inside the Booley Sandbox" in capsys.readouterr().err


@pytest.fixture(autouse=True)
def _ambient_booley_dir_is_the_checkouts_own(monkeypatch) -> None:
    """``booley worktree prepare`` refuses a foreign ``BOOLEY_DIR``; the harness default is one."""
    monkeypatch.delenv("BOOLEY_DIR", raising=False)


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return run_bounded_child(["git", *args], cwd=cwd)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture
def project(tmp_path: Path, monkeypatch) -> Path:
    """A Git Project with a tracked README and untracked configuration and session state."""
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
    assert _git(root, "add", "README.md").returncode == 0
    assert _git(root, "commit", "-m", "init").returncode == 0
    # Untracked files in the Booley Directory: configuration that must be copied ...
    _write(data / "booley.toml", "[submodules]\npaths = []\n")
    # ... and live session state that must stay behind.
    _write(data / "runtime" / "sessions" / "s1" / "state.json", "{}\n")
    # Nested directories that merely share those names are ordinary contents of the Booley Directory.
    _write(data / "cores" / "runtime" / "local.core", "CAPI=2:\n")
    _write(data / "mcp_tools" / "runtime" / "sessions" / "helper.py", "# helper\n")
    return root


@pytest.fixture
def versioned_project(tmp_path):
    """A clean Stealth outer repository with a Versioned Booley Directory at its HEAD."""
    from booley.runtime.project_gitignore import PROJECT_GITIGNORE
    from tests.git_support import CORE, git

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


def _assert_creation_absent(root, name):
    from tests.git_support import git

    nested = root.parent / name / ".booley"
    assert not nested.exists()
    assert nested.as_posix() not in git(root / ".booley", "worktree", "list", "--porcelain")
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


@pytest.mark.parametrize("failure", ["add", "after-add", "configure", "identity"])
def test_pairing_failure_rolls_back_both_repositories(
    versioned_project, monkeypatch, capsys, failure
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
    assert _prepare(_git_worktree(root, name)) == 1
    assert "injected" in capsys.readouterr().err
    _assert_creation_absent(root, name)


def test_canonical_transient_classifier_matches_git_including_reincludes(versioned_project):
    from booley.runtime.project_gitignore import is_project_transient_path

    paths = [
        *NEW_RUNTIME_PATHS,
        *(f"{prefix}/{path}" for prefix in ("cores", "selftest") for path in NEW_RUNTIME_PATHS),
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


_HOOK_MARKER = """\
import os, pathlib
pathlib.Path("hook-marker.txt").write_text(
    os.getcwd() + "\\n" + os.environ["BOOLEY_DIR"] + "\\n", encoding="utf-8"
)
print("hook says hi")
"""


def _commit_hook(source: Path, body: str) -> None:
    """Commit a ``post-setup.py`` hook into the Versioned Booley Directory."""
    from tests.git_support import git

    _write(source / "hooks/post-setup.py", body)
    git(source, "add", "hooks/post-setup.py")
    git(source, "commit", "-qm", "Add post-setup hook")


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


def _registrations(root: Path) -> list[Path]:
    """Every administrative directory of the Project Root's and the Booley Directory's repository."""
    found: list[Path] = []
    for repository in (root / ".git", root / ".booley" / ".git"):
        found.extend(sorted((repository / "worktrees").glob("*")))
    return found


def _bytes_of(directory: Path) -> dict[str, bytes]:
    return {
        path.relative_to(directory).as_posix(): path.read_bytes()
        for path in sorted(directory.rglob("*"))
        if path.is_file()
    }


def _listed(repository: Path) -> str:
    return _git(repository, "worktree", "list", "--porcelain").stdout


# `booley worktree prepare`: a worktree made by Git or T3 Code
# ---------------------------------------------------------------------------


def _git_worktree(root: Path, folder: str) -> Path:
    """A linked worktree made by plain Git beside *root*, as T3 Code makes one."""
    worktree = root.parent / folder
    assert _git(root, "worktree", "add", "-b", f"t3/{folder}", str(worktree)).returncode == 0
    return worktree


def _prepare(path: Path) -> int:
    return worktree_cmd.run(argparse.Namespace(worktree_command="prepare", path=path), path)


@pytest.mark.parametrize("git_repository", [False, True], ids=["plain-folder", "git-repository"])
def test_prepare_outside_a_project_changes_nothing(
    git_repository: bool, tmp_path: Path, monkeypatch, capsys
) -> None:
    folder = tmp_path / "plain"
    _write(folder / "file.txt", "x\n")
    if git_repository:
        assert _git(folder, "init").returncode == 0
    before = _bytes_of(folder)
    monkeypatch.setattr(runtime_context, "inside_session_runtime", lambda: True)
    monkeypatch.setattr(sys, "argv", ["booley", "worktree", "prepare", str(folder)])

    assert tlr.main() == 0
    assert "nothing to prepare" in capsys.readouterr().err
    assert _bytes_of(folder) == before


@pytest.mark.parametrize("case", ["main", "outside", "linked", "dirty-crlf", "missing"])
def test_prepare_refuses_the_main_checkout_and_an_outside_booley_dir(
    case: str, request, tmp_path: Path, monkeypatch, capsys
) -> None:
    external = tmp_path / "external"
    external.mkdir()
    if case == "outside":
        root = _configured_project(tmp_path, monkeypatch, external.as_posix())
    else:
        root = request.getfixturevalue("project")
    if case == "dirty-crlf":
        assert _git(root, "config", "core.autocrlf", "true").returncode == 0
    target = root if case == "main" else tmp_path / "absent"
    if case not in ("main", "missing"):
        target = _git_worktree(root, "t3-refused")
    if case == "linked":
        if os.name == "nt":
            pytest.skip("needs symlinks")
        (target / ".booley").symlink_to(root / ".booley", target_is_directory=True)
    if case == "dirty-crlf":
        _write(target / "README.md", "edited\n")

    assert _prepare(target) == 1

    message = {
        "main": "linked worktree",
        "outside": "relative [project].dir",
        "linked": "link out of the worktree",
        "dirty-crlf": "uncommitted changes",
        "missing": "not a directory",
    }[case]
    assert message in capsys.readouterr().err
    assert list(external.iterdir()) == []
    if case == "dirty-crlf":
        assert (target / "README.md").read_text(encoding="utf-8") == "edited\n"


@pytest.mark.parametrize("layout", ["tracked", "snapshot", "versioned"])
def test_prepare_gives_the_worktree_its_booley_directory(layout: str, request, capsys) -> None:
    if layout == "versioned":
        root = request.getfixturevalue("versioned_project")
        source = root / ".booley"
        _write(source / "cores/top.core", "changed after the last commit\n")
    else:
        root = request.getfixturevalue("project")
    if layout == "tracked":
        assert _git(root, "add", ".booley/booley.toml").returncode == 0
        assert _git(root, "commit", "-m", "track").returncode == 0
    worktree = _git_worktree(root, "t3-one")
    nested = worktree / ".booley"
    if layout == "versioned":
        # A stale registration at the very path the Paired Checkout needs.
        assert _git(source, "worktree", "add", "--detach", str(nested)).returncode == 0
        shutil.rmtree(nested)

    assert _prepare(worktree) == 0

    err = capsys.readouterr().err
    files = _bytes_of(nested)
    if layout == "tracked":
        assert list(files) == ["booley.toml"]
        assert _git(worktree, "status", "--porcelain").stdout == ""
    elif layout == "snapshot":
        assert (
            "cores/runtime/local.core" in files and "runtime/sessions/s1/state.json" not in files
        )
    else:
        assert _git(nested, "symbolic-ref", "--short", "HEAD").stdout.strip() == (
            "booley-worktree/t3-one"
        )
        assert _git(nested, "rev-parse", "HEAD").stdout == _git(source, "rev-parse", "HEAD").stdout
        assert files["cores/top.core"] != b"changed after the last commit\n"
        assert "WARNING" in err and "cores/top.core" in err
        assert _listed(source).count("worktree ") == 2


@pytest.mark.parametrize("outcome", ["ok", "fails"])
def test_prepare_runs_the_hook_and_rolls_back_its_failure(
    outcome: str, versioned_project: Path, capsys
) -> None:
    root = versioned_project
    _commit_hook(
        root / ".booley", _HOOK_MARKER if outcome == "ok" else "import sys\nsys.exit(3)\n"
    )
    worktree = _git_worktree(root, "t3-hook")
    nested = worktree / ".booley"

    code = _prepare(worktree)

    if outcome == "ok":
        assert code == 0
        cwd, hook_dir = (worktree / "hook-marker.txt").read_text(encoding="utf-8").splitlines()
        assert Path(cwd).resolve() == worktree.resolve()
        assert Path(hook_dir).resolve() == nested.resolve()
        return
    assert code == 1 and "rc=3" in capsys.readouterr().err
    assert not nested.exists()
    assert "booley-worktree/t3-hook" not in _git(root / ".booley", "branch", "--list").stdout
    assert worktree.as_posix() in _listed(root)


@pytest.mark.parametrize("outcome", ["ok", "external", "fails"])
def test_main_checkout_hook_runs_and_a_failure_only_warns(
    outcome: str, project: Path, tmp_path: Path, monkeypatch, capsys
) -> None:
    data = project / ".booley"
    if outcome == "external":
        data = data.rename(tmp_path / "external")
        monkeypatch.setenv("BOOLEY_DIR", str(data))
    body = "import sys\nsys.exit(3)\n" if outcome == "fails" else _HOOK_MARKER
    _write(data / "hooks/post-setup.py", body)

    assert incontainer_post_setup.prepare_main_checkout(project) == 0

    log = data / "worktrees" / ".logs" / "main-checkout.log"
    if outcome == "fails":
        (warning,) = [
            line for line in capsys.readouterr().err.splitlines() if line.startswith("warning: ")
        ]
        for part in ("rc=3", "post-setup.py", "main-checkout.log", "the Sandbox starts anyway"):
            assert part in warning
        return
    cwd, hook_dir = (project / "hook-marker.txt").read_text(encoding="utf-8").splitlines()
    assert Path(cwd).resolve() == project.resolve()
    assert Path(hook_dir).resolve() == data.resolve()
    assert "hook says hi" in log.read_text(encoding="utf-8")
    # A log written under the checkout would shadow an external Booley Directory.
    assert (project / ".booley").exists() is (outcome == "ok")


@pytest.mark.parametrize("layout", ["snapshot", "versioned"])
def test_prepare_rerun_changes_nothing(layout: str, request) -> None:
    root = request.getfixturevalue("versioned_project" if layout == "versioned" else "project")
    tracked = "rtl.v" if layout == "versioned" else "README.md"
    worktree = _git_worktree(root, "t3-again")
    assert _prepare(worktree) == 0
    _write(worktree / tracked, "edited\n")
    _write(worktree / ".booley/booley.toml", "# edited\n")
    before = (_listed(root), _git(worktree / ".booley", "rev-parse", "HEAD").stdout)

    assert _prepare(worktree) == 0

    assert (worktree / tracked).read_text(encoding="utf-8") == "edited\n"
    assert (worktree / ".booley/booley.toml").read_text(encoding="utf-8") == "# edited\n"
    assert (_listed(root), _git(worktree / ".booley", "rev-parse", "HEAD").stdout) == before


def test_prepare_help_documents_the_command(capsys) -> None:
    with pytest.raises(SystemExit):
        tlr._build_parser().parse_args(["worktree", "prepare", "--help"])

    # argparse may wrap a line after the hyphen of "post-setup".
    help_text = " ".join(capsys.readouterr().out.split()).replace("- ", "-")
    for text in ("PATH", "booley-worktree/<folder>", "post-setup", "Snapshot"):
        assert text in help_text


def test_prepare_turns_line_ending_conversion_off_in_a_clean_worktree(project: Path) -> None:
    assert _git(project, "config", "core.autocrlf", "true").returncode == 0
    worktree = _git_worktree(project, "t3-crlf")

    assert _prepare(worktree) == 0
    assert _git(worktree, "config", "--get", "core.autocrlf").stdout.strip() == "false"


def test_prepare_refuses_before_it_writes_any_setting(project: Path, capsys) -> None:
    if os.name == "nt":
        pytest.skip("needs symlinks")
    worktree = _git_worktree(project, "t3-early")
    (worktree / ".booley").symlink_to(project / ".booley", target_is_directory=True)

    assert _prepare(worktree) == 1

    assert "link out of the worktree" in capsys.readouterr().err
    assert _git(worktree, "config", "--get", "core.autocrlf").stdout == ""


def test_prepare_refuses_a_folder_inside_a_booley_directory(
    versioned_project: Path, capsys
) -> None:
    worktree = _git_worktree(versioned_project, "t3-inside")
    assert _prepare(worktree) == 0
    capsys.readouterr()

    assert _prepare(worktree / ".booley") == 1

    assert "is a Booley Directory" in capsys.readouterr().err
    assert _git(versioned_project / ".booley", "status", "--porcelain").stdout == ""
    assert not (worktree / ".booley/.booley").exists()


def test_prepare_in_a_submodule_prepares_the_worktree(project: Path, tmp_path: Path) -> None:
    library = tmp_path / "library"
    library.mkdir()
    for args in (
        ("init",),
        ("config", "user.name", "T"),
        ("config", "user.email", "t@example.com"),
    ):
        assert _git(library, *args).returncode == 0
    _write(library / "lib.v", "module lib; endmodule\n")
    assert _git(library, "add", ".").returncode == 0
    assert _git(library, "commit", "-m", "lib").returncode == 0
    allow = ("-c", "protocol.file.allow=always")
    assert _git(project, *allow, "submodule", "add", library.as_posix(), "lib").returncode == 0
    assert _git(project, "commit", "-m", "add lib").returncode == 0
    worktree = _git_worktree(project, "t3-sub")
    assert _git(worktree, *allow, "submodule", "update", "--init").returncode == 0

    assert _prepare(worktree / "lib") == 0

    assert (worktree / ".booley/booley.toml").is_file()
    assert not (worktree / "lib/.booley").exists()


def test_prepare_keeps_the_root_fusesoc_marker_a_worktree_has(project: Path) -> None:
    _write(project / "FUSESOC_IGNORE", "from main\n")
    fresh = _git_worktree(project, "t3-fresh")
    edited = _git_worktree(project, "t3-edited")
    _write(edited / "FUSESOC_IGNORE", "mine\n")

    assert _prepare(fresh) == 0
    assert _prepare(edited) == 0

    assert (fresh / "FUSESOC_IGNORE").read_text(encoding="utf-8") == "from main\n"
    assert (edited / "FUSESOC_IGNORE").read_text(encoding="utf-8") == "mine\n"


def test_prepare_keeps_a_checkout_that_lost_its_git_file(versioned_project: Path, capsys) -> None:
    cut_short = _git_worktree(versioned_project, "t3-cut")
    assert _prepare(cut_short) == 0
    (cut_short / ".booley/.git").unlink()
    other = _git_worktree(versioned_project, "t3-other")
    registered = _registrations(versioned_project)

    assert _prepare(other) == 1

    assert "lost its .git file" in capsys.readouterr().err
    assert _registrations(versioned_project) == registered
    assert not (other / ".booley").exists()


def _branches(source: Path) -> set[str]:
    return set(
        _git(source, "for-each-ref", "--format=%(refname:short)", "refs/heads").stdout.split()
    )


def _prepare_with_own_commits(root: Path, folders: list[str], *, own_commit: bool) -> list[Path]:
    made = [_git_worktree(root, folder) for folder in folders]
    for worktree in made:
        assert _prepare(worktree) == 0
        if own_commit:
            assert (
                _git(worktree / ".booley", "commit", "--allow-empty", "-m", "own").returncode == 0
            )
    return made


@pytest.mark.parametrize("own_commit", [False, True], ids=["merged", "unmerged"])
def test_prepare_deletes_only_merged_paired_branches_whose_worktree_is_gone(
    versioned_project: Path, own_commit: bool
) -> None:
    from booley.harness import doctor

    gone, _live = _prepare_with_own_commits(
        versioned_project, ["t3-gone", "t3-live"], own_commit=own_commit
    )
    source = versioned_project / ".booley"
    assert _git(source, "branch", "keep/merged").returncode == 0
    shutil.rmtree(gone)

    assert _prepare(_git_worktree(versioned_project, "t3-next")) == 0

    kept = {"main", "keep/merged", "booley-worktree/t3-live", "booley-worktree/t3-next"}
    assert _branches(source) == kept | ({"booley-worktree/t3-gone"} if own_commit else set())
    reporter = doctor._Reporter.create()
    doctor._check_kept_worktree_branches(versioned_project, reporter)
    warned = [finding for finding in reporter.findings or () if finding.severity == "warn"]
    assert [finding.subject for finding in warned] == (
        ["booley-worktree/t3-gone"] if own_commit else []
    )
    name = quote_arg("booley-worktree/t3-gone")
    assert all(f"branch -D {name}" in w.fix and f"merge {name}" in w.fix for w in warned)


def test_prepare_refuses_a_reused_name_over_an_unmerged_branch(
    versioned_project: Path, capsys
) -> None:
    (old,) = _prepare_with_own_commits(versioned_project, ["t3-o'reused"], own_commit=True)
    source = versioned_project / ".booley"
    assert _git(source, "branch", "keep/merged").returncode == 0
    shutil.rmtree(old)
    assert _git(versioned_project, "worktree", "prune").returncode == 0
    assert _git(versioned_project, "worktree", "add", "-b", "t3/renamed", str(old)).returncode == 0

    assert _prepare(old) == 1

    last = capsys.readouterr().err.splitlines()[-1]
    assert "branch -D 'booley-worktree/t3-o'\"'\"'reused'" in last
    assert "merge" in last
    assert "retry" in last
    assert {"booley-worktree/t3-o'reused", "keep/merged"} <= _branches(source)


def test_prepare_keeps_every_branch_while_the_booley_directory_is_detached(
    versioned_project: Path,
) -> None:
    (gone,) = _prepare_with_own_commits(versioned_project, ["t3-gone"], own_commit=True)
    source = versioned_project / ".booley"
    shutil.rmtree(gone)
    assert _git(source, "checkout", "--detach", "booley-worktree/t3-gone").returncode == 0

    assert _prepare(_git_worktree(versioned_project, "t3-next")) == 0

    assert "booley-worktree/t3-gone" in _branches(source)


def test_prepare_deletes_a_branch_whole_when_its_name_holds_unicode_whitespace(
    versioned_project: Path,
) -> None:
    source = versioned_project / ".booley"
    assert _git(source, "branch", "booley-worktree/a\u00a0keep/merged").returncode == 0
    assert _git(source, "branch", "keep/merged").returncode == 0

    assert _prepare(_git_worktree(versioned_project, "t3-next")) == 0

    assert _branches(source) == {"main", "keep/merged", "booley-worktree/t3-next"}


def test_prepare_drops_a_killed_runs_snapshot_leftovers(project: Path) -> None:
    worktree = _git_worktree(project, "t3-killed")
    _write(worktree / f".booley.prepare-{os.getpid()}/stale.txt", "old\n")

    assert _prepare(worktree) == 0

    assert "stale.txt" not in _bytes_of(worktree / ".booley")
    assert not (worktree / f".booley.prepare-{os.getpid()}").exists()
