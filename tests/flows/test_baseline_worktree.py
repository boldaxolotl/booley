"""Tests for the ephemeral baseline worktree helper (:mod:`._baseline_worktree`).

These exercise the real ``git worktree`` mechanism against a throwaway repo — no
mocking — since the whole point of the helper is that it interacts correctly with
git without disturbing the caller's working tree.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from booley.flows import baseline_worktree as baseline_module
from booley.flows.baseline_worktree import (
    BaselineWorktreeError,
    baseline_worktree,
    git_short_sha,
)
from booley.runtime.submodule_materialization import materialize_submodules


def test_paired_project_basis_uses_runtime_ticket_slug(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ticket = tmp_path / "ticket.md"
    ticket.write_text("ticket\n", encoding="utf-8")
    loaded_slugs = []

    class FakeTicketIO:
        def __init__(self, *_args, **_kwargs):
            pass

        def load_basis(self, slug, **_kwargs):
            loaded_slugs.append(slug)
            return type("Basis", (), {"project_sha": "a" * 40})()

    monkeypatch.setenv("BOOLEY_TICKET_FILE", str(ticket))
    monkeypatch.setenv("BOOLEY_SLUG", "actual-ticket")
    monkeypatch.setattr("booley.ticket_board.helpers.detect_project_root", lambda: tmp_path)
    monkeypatch.setattr(
        "booley.runtime.project_dir.resolve_checkout_project_dir", lambda _root: tmp_path
    )
    monkeypatch.setattr("booley.ticket_board.io.TicketIO", FakeTicketIO)
    monkeypatch.setattr(
        "booley.ticket_board.acceptance_targets.resolve_commit",
        lambda _worktree, commit: commit,
    )

    assert baseline_module._paired_project_base_sha(tmp_path) == "a" * 40
    assert loaded_slugs == ["actual-ticket"]


def test_paired_project_basis_rejects_unsafe_runtime_ticket_slug(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ticket = tmp_path / "ticket.md"
    ticket.write_text("ticket\n", encoding="utf-8")
    monkeypatch.setenv("BOOLEY_TICKET_FILE", str(ticket))
    monkeypatch.setenv("BOOLEY_SLUG", "../../outside")

    with pytest.raises(BaselineWorktreeError, match="unsafe ticket slug"):
        baseline_module._paired_project_base_sha(tmp_path)


def _git(repo: Path, *a: str) -> None:
    subprocess.run(["git", *a], cwd=repo, check=True, capture_output=True, text=True)


def _commit_all(repo: Path, message: str) -> None:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", message)


def _init_repo(repo: Path) -> None:
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "f.txt").write_text("v1\n", encoding="utf-8")
    _commit_all(repo, "one")
    (repo / "f.txt").write_text("v2\n", encoding="utf-8")
    _commit_all(repo, "two")


def _add_private_project_submodule(project: Path, dependency: Path) -> None:
    dependency.mkdir()
    _init_repo(dependency)
    _git(
        project,
        "-c",
        "protocol.file.allow=always",
        "submodule",
        "add",
        str(dependency),
        "vendor/dependency",
    )
    _git(
        project,
        "config",
        "--file",
        ".gitmodules",
        "submodule.vendor/dependency.url",
        "git@example.invalid:private/dependency.git",
    )


def test_yields_baseline_content_and_leaves_tree_untouched(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    # A dirty working-tree edit that an in-place checkout would clobber.
    (tmp_path / "f.txt").write_text("dirty\n", encoding="utf-8")

    with baseline_worktree(tmp_path, "HEAD~1") as wt:
        # The worktree holds the *baseline* content...
        assert (wt / "f.txt").read_text(encoding="utf-8") == "v1\n"
        # ...it lives under .booley_project/ and is PID-tagged.
        assert wt.parent.name == ".booley_project"
        assert ".baseline-wt-" in wt.name
        # ...while the caller's tree still has the dirty edit.
        assert (tmp_path / "f.txt").read_text(encoding="utf-8") == "dirty\n"

    # Cleaned up on exit; caller's tree untouched throughout.
    assert not wt.exists()
    assert (tmp_path / "f.txt").read_text(encoding="utf-8") == "dirty\n"


def test_worktree_removed_even_when_body_raises(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    captured: list[Path] = []

    with pytest.raises(RuntimeError, match="boom"), baseline_worktree(tmp_path, "HEAD~1") as wt:
        captured.append(wt)
        assert wt.exists()
        raise RuntimeError("boom")

    assert captured and not captured[0].exists()


def test_detached_ref_already_checked_out_elsewhere(tmp_path: Path) -> None:
    """``--detach`` means a ref that is the current branch's HEAD (already
    "checked out" in the main worktree) can still be materialized."""
    _init_repo(tmp_path)
    with baseline_worktree(tmp_path, "HEAD") as wt:
        assert (wt / "f.txt").read_text(encoding="utf-8") == "v2\n"


def test_untracked_root_quarantine_marker_is_copied(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    (tmp_path / "FUSESOC_IGNORE").write_text("quarantine\n", encoding="utf-8")

    with baseline_worktree(tmp_path, "HEAD~1") as wt:
        assert (wt / "FUSESOC_IGNORE").read_text(encoding="utf-8") == "quarantine\n"


def test_bad_ref_raises_baseline_worktree_error(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    with (
        pytest.raises(BaselineWorktreeError, match="worktree add"),
        baseline_worktree(tmp_path, "no-such-ref"),
    ):
        pass


def test_submodule_sources_are_checked_out_at_baseline_revision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    submodule = tmp_path / "submodule"
    submodule.mkdir()
    _init_repo(submodule)
    old_submodule_sha = subprocess.run(
        ["git", "rev-parse", "HEAD~1"],
        cwd=submodule,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    monkeypatch.setenv("GIT_ALLOW_PROTOCOL", "file")
    (repo / ".gitmodules").write_text(
        '[submodule "ip"]\n\tpath = vendor/ip\n\turl = git@example.invalid:private/ip.git\n',
        encoding="utf-8",
    )
    (repo / "vendor").mkdir()
    _git(repo / "vendor", "clone", str(submodule), "ip")
    _git(repo / "vendor/ip", "checkout", "-q", old_submodule_sha)
    _commit_all(repo, "add baseline submodule")
    _git(repo / "vendor/ip", "checkout", "-q", "master")
    _commit_all(repo, "update submodule")
    monkeypatch.setenv("GIT_SSH", "/definitely/no/ssh")

    with baseline_worktree(repo, "HEAD~1") as wt:
        source = wt / "vendor" / "ip" / "f.txt"
        assert source.read_text(encoding="utf-8") == "v1\n"


def test_submodule_checkout_failure_is_an_infrastructure_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _init_repo(tmp_path)
    (tmp_path / ".gitmodules").write_text(
        '[submodule "missing"]\n\tpath = missing\n\turl = /no/such/repository\n',
        encoding="utf-8",
    )
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    _git(tmp_path, "add", ".gitmodules")
    _git(tmp_path, "update-index", "--add", "--cacheinfo", f"160000,{sha},missing")
    _git(tmp_path, "commit", "-qm", "add broken submodule")
    monkeypatch.setenv("GIT_ALLOW_PROTOCOL", "file")

    with (
        pytest.raises(BaselineWorktreeError, match="initializing submodules"),
        baseline_worktree(tmp_path, "HEAD"),
    ):
        pass
    assert not list((tmp_path / ".booley_project").glob(".baseline-wt-*"))


def test_stealth_cores_copied_into_worktree(tmp_path: Path) -> None:
    """ADR 0036 stealth cores live in the git-excluded ``.booley_project/``,
    so a bare checkout has none — the helper must mirror them in, or a
    stealth-only project's baseline run would scan zero cores."""
    _init_repo(tmp_path)
    stealth = tmp_path / ".booley_project" / "cores" / "ip"
    stealth.mkdir(parents=True)
    (stealth / "top.core").write_text("CAPI=2:\nname: x:ip:top:1.0\n", encoding="utf-8")

    with baseline_worktree(tmp_path, "HEAD~1") as wt:
        copied = wt / ".booley_project" / "cores" / "ip" / "top.core"
        assert copied.read_text(encoding="utf-8").startswith("CAPI=2:")

    # The copied (untracked) files must not break force-removal on exit.
    assert not wt.exists()
    # ...and the real stealth cores are untouched.
    assert (stealth / "top.core").is_file()


def test_paired_project_uses_ticket_fork_recipe(tmp_path: Path) -> None:
    """A ticket's paired project repo contributes its old Target definition."""
    outer = tmp_path / "outer"
    outer.mkdir()
    _init_repo(outer)
    (outer / ".git" / "info" / "exclude").write_text(
        "/.booley_project\n",
        encoding="utf-8",
    )

    project = outer / ".booley_project"
    (project / "cores").mkdir(parents=True)
    _git(project, "init", "-q")
    _git(project, "config", "user.email", "t@example.com")
    _git(project, "config", "user.name", "Test")
    (project / ".gitignore").write_text("/worktrees/\n", encoding="utf-8")
    core = project / "cores" / "top.core"
    core.write_text("recipe: baseline\n", encoding="utf-8")
    _add_private_project_submodule(project, tmp_path / "dependency")
    _commit_all(project, "project baseline")

    ticket = project / "worktrees" / "ticket"
    _git(outer, "worktree", "add", "-b", "ticket", str(ticket), "HEAD")
    paired = ticket / ".booley_project"
    _git(project, "worktree", "add", "-b", "booley-ticket/ticket", str(paired), "master")
    _git(paired, "branch", "--set-upstream-to=master")
    materialize_submodules(project, paired)
    (paired / "cores" / "top.core").write_text("recipe: current\n", encoding="utf-8")
    _commit_all(paired, "change target recipe")

    with baseline_worktree(ticket, "HEAD") as baseline:
        frozen = baseline / ".booley_project" / "cores" / "top.core"
        assert frozen.read_text(encoding="utf-8") == "recipe: baseline\n"
        assert (baseline / ".booley_project" / ".git").is_file()
        dependency_file = baseline / ".booley_project/vendor/dependency/f.txt"
        assert dependency_file.read_text(encoding="utf-8") == "v2\n"

    assert (paired / "cores" / "top.core").read_text(encoding="utf-8") == "recipe: current\n"


def _stealth_core_linking_to_rtl(repo: Path, link_name: str = "rtl") -> Path:
    """A stealth core dir whose fileset reaches repo-root ``rtl/`` via a
    core-relative resolution link (ADR 0036). Returns the link path."""
    core_dir = repo / ".booley_project" / "cores" / "ip"
    core_dir.mkdir(parents=True)
    (core_dir / "top.core").write_text("CAPI=2:\nname: x:ip:top:1.0\n", encoding="utf-8")
    link = core_dir / link_name
    # cores/ip -> cores -> .booley_project -> repo root
    link.symlink_to("../../../rtl")
    return link


def test_resolution_symlink_is_preserved_not_dereferenced(tmp_path: Path) -> None:
    """The delta-lying bug: ``copytree`` dereferencing a resolution link copies
    the LIVE working tree's RTL into the baseline worktree, so both sides of a
    ``--baseline`` run synthesize the modified design and report +0.0%."""
    _init_repo(tmp_path)
    rtl = tmp_path / "rtl"
    rtl.mkdir()
    (rtl / "top.v").write_text("baseline\n", encoding="utf-8")
    _commit_all(tmp_path, "add rtl")
    (rtl / "top.v").write_text("modified\n", encoding="utf-8")
    _commit_all(tmp_path, "modify rtl")
    core_dir = _stealth_core_linking_to_rtl(tmp_path).parent
    # The reported shape was a per-file link; a whole-dir link is the other
    # half of the same convention. Both must survive the copy.
    (core_dir / "top.v").symlink_to("../../../rtl/top.v")

    with baseline_worktree(tmp_path, "HEAD~1") as wt:
        copied_file = wt / ".booley_project" / "cores" / "ip" / "top.v"
        assert copied_file.is_symlink(), "resolution link was dereferenced into a real file"
        assert copied_file.read_text(encoding="utf-8") == "baseline\n"

        copied = wt / ".booley_project" / "cores" / "ip" / "rtl"
        assert copied.is_symlink(), "resolution link was dereferenced into a real dir"
        # It resolves inside the worktree, onto the *baseline* revision...
        assert (copied / "top.v").read_text(encoding="utf-8") == "baseline\n"
        assert (copied / "top.v").resolve() == (wt / "rtl" / "top.v").resolve()
        # ...while the live tree keeps the modified RTL.
        assert (rtl / "top.v").read_text(encoding="utf-8") == "modified\n"


def test_absolute_in_project_symlink_is_rebased_onto_the_worktree(tmp_path: Path) -> None:
    """An absolute link still names the live tree after a verbatim copy — it
    must be rewritten to the worktree's own copy of the same path."""
    _init_repo(tmp_path)
    rtl = tmp_path / "rtl"
    rtl.mkdir()
    (rtl / "top.v").write_text("baseline\n", encoding="utf-8")
    _commit_all(tmp_path, "add rtl")
    (rtl / "top.v").write_text("modified\n", encoding="utf-8")
    _commit_all(tmp_path, "modify rtl")
    core_dir = tmp_path / ".booley_project" / "cores" / "ip"
    core_dir.mkdir(parents=True)
    (core_dir / "rtl").symlink_to(rtl)  # absolute target

    with baseline_worktree(tmp_path, "HEAD~1") as wt:
        copied = wt / ".booley_project" / "cores" / "ip" / "rtl"
        assert copied.is_symlink()
        assert (copied / "top.v").read_text(encoding="utf-8") == "baseline\n"


def test_symlink_outside_the_project_is_left_verbatim(tmp_path: Path) -> None:
    """A link to a shared tree outside the project names the same thing from
    either worktree; rebasing it would invent a path that does not exist."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    shared = tmp_path / "shared"
    shared.mkdir()
    (shared / "lib.v").write_text("vendor\n", encoding="utf-8")
    core_dir = repo / ".booley_project" / "cores" / "ip"
    core_dir.mkdir(parents=True)
    (core_dir / "vendor").symlink_to(shared)

    with baseline_worktree(repo, "HEAD") as wt:
        copied = wt / ".booley_project" / "cores" / "ip" / "vendor"
        assert copied.is_symlink()
        assert (copied / "lib.v").read_text(encoding="utf-8") == "vendor\n"


def test_link_into_the_state_dir_follows_the_live_tree(tmp_path: Path) -> None:
    """ADR 0036 blesses ``cores/worktrees -> ../worktrees`` (a core pinning a
    frozen checkout). ``.booley_project/`` is git-excluded, so no checkout has
    one — the link must keep naming the LIVE state dir instead of dangling."""
    _init_repo(tmp_path)
    frozen = tmp_path / ".booley_project" / "worktrees" / "pinned"
    frozen.mkdir(parents=True)
    (frozen / "top.v").write_text("frozen\n", encoding="utf-8")
    core_dir = tmp_path / ".booley_project" / "cores" / "ip"
    core_dir.mkdir(parents=True)
    (core_dir / "worktrees").symlink_to("../../worktrees")

    with baseline_worktree(tmp_path, "HEAD") as wt:
        copied = wt / ".booley_project" / "cores" / "ip" / "worktrees"
        assert (copied / "pinned" / "top.v").read_text(encoding="utf-8") == "frozen\n"


def test_link_within_the_cores_tree_stays_verbatim(tmp_path: Path) -> None:
    """A link between two mirrored cores resolves inside the copy already."""
    _init_repo(tmp_path)
    cores = tmp_path / ".booley_project" / "cores"
    (cores / "common").mkdir(parents=True)
    (cores / "common" / "util.v").write_text("util\n", encoding="utf-8")
    (cores / "ip").mkdir()
    (cores / "ip" / "shared").symlink_to("../common")

    with baseline_worktree(tmp_path, "HEAD") as wt:
        copied = wt / ".booley_project" / "cores" / "ip" / "shared"
        assert copied.readlink() == Path("../common")
        assert (copied / "util.v").read_text(encoding="utf-8") == "util\n"


def test_link_target_missing_from_the_ref_is_refused(tmp_path: Path) -> None:
    """The target exists in the working tree but not in the checkout (untracked
    or added after the ref) — the baseline cannot be built from that ref, and
    saying so beats a dangling path the Flows resolve however they like."""
    _init_repo(tmp_path)
    (tmp_path / "rtl").mkdir()
    (tmp_path / "rtl" / "top.v").write_text("generated\n", encoding="utf-8")  # never committed
    _stealth_core_linking_to_rtl(tmp_path)

    with (
        pytest.raises(BaselineWorktreeError, match="no source for stealth-core"),
        baseline_worktree(tmp_path, "HEAD"),
    ):
        pass
    # The failed setup still cleaned its worktree up.
    assert not list((tmp_path / ".booley_project").glob(".baseline-wt-*"))


def test_no_stealth_cores_is_a_noop(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    with baseline_worktree(tmp_path, "HEAD") as wt:
        assert not (wt / ".booley_project").exists()


def test_git_short_sha_falls_back_on_bad_ref(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    # A resolvable ref returns a real short sha (hex, <= 40 chars).
    sha = git_short_sha("HEAD", tmp_path)
    assert sha and all(c in "0123456789abcdef" for c in sha)
    # An unresolvable ref degrades to a truncated echo rather than raising.
    assert git_short_sha("no-such-ref", tmp_path) == "no-such-"
