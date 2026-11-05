"""The tracked-files digest: what it keys and what it leaves out (ADR 0076 §2)."""

from __future__ import annotations

import os
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from booley.core.booley_dir import reset_cache
from tests.result_cache.git_fixtures import (
    add_submodule,
    all_cases,
    build_layout,
    check,
    clone,
    commit_files,
    digest,
    git,
    isolate,
    new_repo,
    write_files,
)

pytestmark = [
    pytest.mark.skipif(os.name == "nt", reason="the Result Cache runs only in the Linux Sandbox"),
    pytest.mark.usefixtures("isolated_git_excludes"),
]

# A stand-in for Git LFS: clean turns content into a pointer (idempotently), smudge back.
LFS_FILTER = """
import sys
data = sys.stdin.buffer.read()
pointer, content = b"pointer\\n", b"real content\\n"
if sys.argv[1] == "clean":
    data = pointer if data in (pointer, content) else data
elif data == pointer:
    data = content
sys.stdout.buffer.write(data)
"""


@pytest.fixture(autouse=True)
def _isolated(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    isolate(monkeypatch)
    yield
    reset_cache()  # a selection cached by one test must not leak into the next


def lfs_filter(base: Path, smudge: str = "") -> dict[str, str]:
    (base / "lfs.py").write_text(LFS_FILTER)
    run = f"{sys.executable} {base / 'lfs.py'}"
    return {"filter.lfs.clean": f"{run} clean", "filter.lfs.smudge": smudge or f"{run} smudge"}


def clone_with_lfs(origin: Path, target: Path, *options: str, smudge: str = "") -> Path:
    config = lfs_filter(target.parent, smudge)
    flags = [arg for key, text in config.items() for arg in ("-c", f"{key}={text}")]
    return clone(origin, target, *flags, *options)


@all_cases(["sparse-checkout", "skip-worktree", "lfs-pointer", "uninitialised-submodule"])
def test_checkout_variant_changes_tracked_digest(case: str, tmp_path: Path) -> None:
    origin = build_layout(tmp_path / "origin", "tracked").rtl
    files = {"a/x.v": "x\n", "b/y.v": "y\n", ".gitattributes": "big.bin filter=lfs\n"}
    commit_files(origin, files)
    for key, text in lfs_filter(tmp_path).items():
        git(origin, "config", key, text)
    commit_files(origin, {"big.bin": "real content\n"})
    add_submodule(origin, "sub", new_repo(tmp_path / "sub", {"s.v": "s\n"}))
    full = clone_with_lfs(origin, tmp_path / "full", "--recurse-submodules")
    again = clone_with_lfs(origin, tmp_path / "again", "--recurse-submodules")
    recurse = [] if case == "uninitialised-submodule" else ["--recurse-submodules"]
    smudge = "cat" if case == "lfs-pointer" else ""
    variant = clone_with_lfs(origin, tmp_path / "variant", *recurse, smudge=smudge)
    if case == "sparse-checkout":
        git(variant, "sparse-checkout", "set", ".booley", "a")
    if case == "skip-worktree":
        git(variant, "update-index", "--skip-worktree", "b/y.v")
    assert digest(full) == digest(again)
    assert digest(variant) != digest(full)
    if case == "sparse-checkout":  # Git reports a new file in the excluded directory
        write_files(variant, {"b/new.v": "n\n"})
        assert not check(variant).cacheable


@all_cases(["assume-unchanged", "skip-worktree", "executable-bit"])
def test_edit_hidden_from_git_changes_digest(case: str, tmp_path: Path) -> None:
    layout = build_layout(tmp_path, "tracked")
    source = layout.rtl / "src/a.v"
    if case != "executable-bit":  # the flag is keyed too, so measure after setting it
        flag = "--skip-worktree" if case == "skip-worktree" else "--assume-unchanged"
        git(layout.rtl, "update-index", flag, "src/a.v")
    before = digest(layout.project)
    if case != "executable-bit":
        source.write_text("module changed; endmodule\n")
    else:
        git(layout.rtl, "config", "core.fileMode", "false")
        source.chmod(0o755)
    assert git(layout.rtl, "status", "--porcelain") == ""  # Git sees nothing
    assert digest(layout.project) != before


def rename(rtl: Path) -> None:
    git(rtl, "mv", "src/a.v", "src/b.v")  # same bytes, same sort position
    git(rtl, "commit", "-q", "-m", "rename")


def retarget(rtl: Path) -> None:
    git(rtl, "rm", "-q", "link")
    (rtl / "link").symlink_to("src/a.v/")  # same file; only the raw target text differs
    git(rtl, "add", "link")
    git(rtl, "commit", "-q", "-m", "retarget")


def hidden_submodule_edit(rtl: Path) -> None:
    (rtl / "sub/s.v").write_text("hidden edit\n")  # gitlink commit unchanged


@all_cases(["rename", "symlink-retarget", "submodule-hidden-edit"])
def test_committed_change_changes_digest(case: str, tmp_path: Path) -> None:
    layout = build_layout(tmp_path, "tracked")
    (layout.rtl / "link").symlink_to("src/a.v")
    git(layout.rtl, "add", "link")
    git(layout.rtl, "commit", "-q", "-m", "link")
    add_submodule(layout.rtl, "sub", new_repo(tmp_path / "sub", {"s.v": "s\n"}))
    if case == "submodule-hidden-edit":  # hide the edit from Git, then key the flag first
        git(layout.rtl / "sub", "update-index", "--assume-unchanged", "s.v")
    before = digest(layout.project)
    {"rename": rename, "symlink-retarget": retarget}.get(case, hidden_submodule_edit)(layout.rtl)
    assert git(layout.rtl, "status", "--porcelain") == ""
    assert digest(layout.project) != before


def test_head_is_label_only(tmp_path: Path) -> None:
    layout = build_layout(tmp_path, "tracked")
    first = check(layout.project)
    git(layout.rtl, "commit", "-q", "--allow-empty", "-m", "same tree")
    second = check(layout.project)
    assert first.cacheable and second.cacheable
    assert first.participants[0].head != second.participants[0].head
    assert first.tracked_digest == second.tracked_digest


def test_project_root_position_is_keyed(tmp_path: Path) -> None:
    layout = build_layout(tmp_path, "tracked")
    commit_files(layout.rtl, {"a/f.v": "f\n", "b/f.v": "f\n"})
    assert check(layout.rtl / "a").booley_dir == check(layout.rtl / "b").booley_dir
    assert digest(layout.rtl / "a") != digest(layout.rtl / "b")


def test_booley_directory_position_is_keyed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hidden = build_layout(tmp_path / "hidden", "standalone")
    assert hidden.booley is not None
    (tmp_path / "beside").mkdir()
    rtl = clone(hidden.rtl, tmp_path / "beside/rtl")
    booley = clone(hidden.booley, tmp_path / "beside/bd")
    monkeypatch.setenv("BOOLEY_DIR", str(booley))  # selected, not a tracked pointer
    assert digest(rtl) != digest(hidden.project)


@all_cases(["tracked", "standalone", "legacy-pipeline-toml-kept"])
def test_configuration_files_are_not_digested(case: str, tmp_path: Path) -> None:
    layout = build_layout(tmp_path, "standalone" if case == "standalone" else "tracked")
    repo, prefix = (layout.booley, "") if layout.booley else (layout.rtl, ".booley/")
    before = digest(layout.project)

    def change(name: str, text: str, stray: bool = False) -> str:
        commit_files(layout.rtl if stray else repo, {(name if stray else prefix + name): text})
        return digest(layout.project)

    if case == "legacy-pipeline-toml-kept":  # booley.toml wins: pipeline.toml is no config
        before = change("pipeline.toml", "a = 1\n")
        assert change("pipeline.toml", "a = 2\n") != before
        return
    for name in ("booley.toml", "tests.toml", "criteria.toml", "goalsets/feature.md"):
        assert change(name, "[edited]\n") == before
    for name in ("cores/x.core", "goalsets/data.hex"):
        assert change(name, "edited\n") != before
        before = digest(layout.project)
    # Anchored to the Booley Directory: the same names elsewhere are digested.
    stray = "src/tests.toml" if case == "tracked" else "criteria.toml"
    before = change(stray, "a\n", True)
    assert change(stray, "b\n", True) != before


def test_git_listings_override_the_untracked_cache(tmp_path: Path) -> None:
    """A stale untracked cache would hide an untracked file; no portable fixture makes it stale."""
    from booley.result_cache.tracked_files import git_output

    layout = build_layout(tmp_path, "tracked")
    git(layout.rtl, "config", "core.untrackedCache", "true")
    assert git_output(layout.rtl, "config", "core.untrackedCache").strip() == b"false"
