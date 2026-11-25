"""Directory migration moves only worktrees whose submodules can move with them."""

import argparse
import os

import pytest

from booley.harness.init_cmd import run_init
from tests.fake_docker_executable import install_fake_docker
from tests.git_support import git

_WINDOWS_DOCKER = pytest.mark.skipif(
    os.name == "nt",
    reason="the fake docker is docker.cmd, which a bare `docker` spawn cannot find on Windows: "
    "the runner's own docker.exe, with no daemon, answers the Sandbox inventory instead",
)


@pytest.mark.parametrize(
    "layout", ["native", pytest.param("standalone", marks=_WINDOWS_DOCKER), "broken"]
)
def test_init_check_moves_only_worktrees_with_standalone_submodules(
    tmp_path, monkeypatch, capsys, layout
):
    install_fake_docker(
        tmp_path / "bin", monkeypatch, {}
    )  # no live Sandboxes, whatever the host runs
    dependency, root = tmp_path / "dependency", tmp_path / "root"
    for repository in (dependency, root):
        repository.mkdir()
        git(repository, "init", "-q", "-b", "main")
        git(repository, "commit", "-q", "--allow-empty", "-m", "base")
    git(root, "-c", "protocol.file.allow=always", "submodule", "add", str(dependency), "ip")
    git(root, "commit", "-qm", "submodule")
    worktree = root / ".booley_project" / "worktrees" / "one"
    git(root, "worktree", "add", "--detach", str(worktree))
    if layout == "native":
        git(worktree, "-c", "protocol.file.allow=always", "submodule", "update", "--init")
    else:  # a standalone repository, as Booley materializes submodules
        git(worktree / "ip", "clone", "-q", str(dependency), ".")
    if layout == "broken":
        with (worktree / "ip/.git/config").open("a", encoding="utf-8") as config:
            config.write("[broken\n")

    assert run_init(argparse.Namespace(check_only=True), root) == 2

    out = capsys.readouterr().out
    if layout == "native":
        assert "worktree contains native Git submodule 'ip'" in out
        assert "`git submodule deinit --all`" in out
        assert "deinit -f" not in out
    elif layout == "broken":
        assert "git rev-parse --show-toplevel failed" in out
    else:
        assert "Would rename the Booley Directory and repair linked worktrees" in out
