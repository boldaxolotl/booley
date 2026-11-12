"""The Base of a measured commit: default, --base normalization, changed files, edits."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.runtime.test_commit_export import (
    FAKE_OID,
    add_gitlink,
    commit_all,
    git,
    make_submodule,
    new_repository,
    write,
)


def with_origin(repository: Path) -> None:
    """Push ``main`` to a bare ``origin`` and make it the remote default branch."""
    origin = repository.parent / "origin.git"
    git(repository.parent, "init", "--bare", "-q", str(origin))
    git(repository, "remote", "add", "origin", str(origin))
    git(repository, "push", "-q", "origin", "main")
    git(repository, "remote", "set-head", "origin", "main")


def branch_commit(repository: Path, branch: str, start: str, name: str) -> str:
    """Commit one new file on a new *branch* cut from *start*."""
    git(repository, "checkout", "-q", "-b", branch, start)
    write(repository / name, f"{name}\n")
    return commit_all(repository, name)


def test_default_base_is_the_merge_base_with_the_remote_default_branch(tmp_path: Path) -> None:
    from booley.measurement.base import BaseChoice, resolve_base

    repo = new_repository(tmp_path / "repo")
    write(repo / "rtl.v", "rtl\n")
    pushed = commit_all(repo, "pushed")
    with_origin(repo)
    write(repo / "local_only.v", "x\n")
    commit_all(repo, "local main moves on, unpushed")
    head = branch_commit(repo, "feature", "main", "f.v")

    base = resolve_base(repo, head)

    assert base.commit == pushed  # local main (one commit later) would give a later base
    assert base.choice is BaseChoice.REMOTE_DEFAULT
    assert base.requested is None
    assert base.no_base is None


def test_default_base_falls_back_to_local_main_without_origin_head(tmp_path: Path) -> None:
    from booley.measurement.base import BaseChoice, resolve_base

    repo = new_repository(tmp_path / "repo")
    write(repo / "rtl.v", "rtl\n")
    fork = commit_all(repo)
    head = branch_commit(repo, "feature", "main", "f.v")

    base = resolve_base(repo, head)

    assert (base.commit, base.choice) == (fork, BaseChoice.LOCAL_MAIN)


def test_commit_on_the_default_branch_has_no_base(tmp_path: Path) -> None:
    from booley.measurement.base import BaseChoice, resolve_base

    repo = new_repository(tmp_path / "repo")
    write(repo / "rtl.v", "rtl\n")
    head = commit_all(repo)
    with_origin(repo)

    base = resolve_base(repo, head)

    assert base.commit is None
    assert base.choice is BaseChoice.REMOTE_DEFAULT
    assert base.no_base is not None and "is its own base" in base.no_base


def test_no_default_branch_means_no_base_and_says_why(tmp_path: Path) -> None:
    from booley.measurement.base import resolve_base

    repo = new_repository(tmp_path / "repo")
    write(repo / "rtl.v", "rtl\n")
    head = commit_all(repo)
    git(repo, "branch", "-m", "trunk")

    base = resolve_base(repo, head)

    assert (base.commit, base.choice) == (None, None)
    assert base.no_base == "neither origin/HEAD nor local main exists"


def test_default_branch_without_shared_history_means_no_base_and_says_why(
    tmp_path: Path,
) -> None:
    from booley.measurement.base import resolve_base

    repo = new_repository(tmp_path / "repo")
    write(repo / "rtl.v", "rtl\n")
    commit_all(repo)
    git(repo, "checkout", "-q", "--orphan", "unrelated")
    write(repo / "other.v", "other\n")
    head = commit_all(repo, "unrelated root")

    base = resolve_base(repo, head)

    assert base.commit is None
    assert base.no_base is not None and "local main shares no history" in base.no_base


def test_base_on_another_branch_is_normalized_to_the_merge_base_naming_both_commits(
    tmp_path: Path,
) -> None:
    from booley.measurement.base import BaseChoice, resolve_base

    repo = new_repository(tmp_path / "repo")
    write(repo / "rtl.v", "rtl\n")
    fork = commit_all(repo)
    other = branch_commit(repo, "other", fork, "o.v")
    head = branch_commit(repo, "feature", fork, "f.v")

    normalized = resolve_base(repo, head, requested=other)
    as_given = resolve_base(repo, head, requested="main")
    own = resolve_base(repo, fork, requested="feature")

    assert (normalized.commit, normalized.requested) == (fork, other)
    assert normalized.choice is BaseChoice.NORMALIZED
    assert (as_given.commit, as_given.requested) == (fork, fork)  # main is the merge-base
    assert as_given.choice is BaseChoice.REQUESTED
    assert own.commit is None  # a descendant --base leaves the commit as its own base


def test_base_that_is_not_a_commit_or_shares_no_history_is_an_error_naming_it(
    tmp_path: Path,
) -> None:
    from booley.measurement.base import BaseError, resolve_base

    repo = new_repository(tmp_path / "repo")
    write(repo / "rtl.v", "rtl\n")
    head = commit_all(repo)
    git(repo, "checkout", "-q", "--orphan", "unrelated")
    write(repo / "other.v", "other\n")
    commit_all(repo, "unrelated root")

    for requested, message in (
        ("no-such-revision", "not a commit"),
        ("unrelated", "shares no history"),
    ):
        with pytest.raises(BaseError, match=rf"--base {requested}: {message}"):
            resolve_base(repo, head, requested=requested)


def test_file_changed_only_on_the_default_branch_after_the_fork_is_not_listed(
    tmp_path: Path,
) -> None:
    from booley.measurement.base import changed_files, resolve_base

    repo = new_repository(tmp_path / "repo")
    write(repo / "a.v", "a\n")
    write(repo / "b.v", "b\n")
    fork = commit_all(repo)
    git(repo, "checkout", "-q", "-b", "feature")
    write(repo / "a.v", "a changed on the branch\n")
    head = commit_all(repo, "branch edit")
    git(repo, "checkout", "-q", "main")
    write(repo / "b.v", "b changed on main\n")
    commit_all(repo, "main moves on")

    base = resolve_base(repo, head)
    changed = changed_files(repo, base.commit or "", head)

    assert base.commit == fork
    assert [file.path for file in changed.files] == ["a.v"]


def test_constraint_edit_is_marked(tmp_path: Path) -> None:
    from booley.measurement.base import ChangedFile, changed_files

    repo = new_repository(tmp_path / "repo")
    write(repo / "rtl.v", "rtl\n")
    write(repo / "clk.SDC", "create_clock 10\n")
    write(repo / "gone.xdc", "set_property\n")
    fork = commit_all(repo)
    git(repo, "checkout", "-q", "-b", "feature")
    write(repo / "rtl.v", "rtl2\n")
    write(repo / "clk.SDC", "create_clock 5\n")
    write(repo / "new.xdc", "set_property 2\n")
    (repo / "gone.xdc").unlink()
    head = commit_all(repo)

    changed = changed_files(repo, fork, head)

    assert changed.files == (
        ChangedFile("clk.SDC", "M", constraint=True),
        ChangedFile("gone.xdc", "D", constraint=True),
        ChangedFile("new.xdc", "A", constraint=True),
        ChangedFile("rtl.v", "M", constraint=False),
    )
    assert changed.submodules == ()


def test_submodule_bump_is_one_entry_naming_both_commits_and_its_file_count(
    tmp_path: Path,
) -> None:
    from booley.measurement.base import SubmoduleChange, changed_files

    parent, _, sub_pin, _ = make_submodule(tmp_path)
    fork = commit_all(parent)
    sub = parent / "sub"
    write(sub / "lib.v", "lib v2\n")
    write(sub / "extra.v", "extra\n")
    sub_head = commit_all(sub, "move sub")
    head = commit_all(parent, "bump sub")

    changed = changed_files(parent, fork, head)

    assert changed.files == ()
    assert changed.submodules == (
        SubmoduleChange("sub", sub_pin, sub_head, ("sub/extra.v", "sub/lib.v")),
    )


def test_added_and_removed_submodule_list_the_files_of_the_side_that_exists(
    tmp_path: Path,
) -> None:
    from booley.measurement.base import SubmoduleChange, changed_files

    parent, _, sub_pin, _ = make_submodule(tmp_path)
    with_sub = commit_all(parent)
    git(parent, "rm", "-q", "-f", "--cached", "sub")
    without_sub = commit_all(parent, "drop sub", staged_only=True)
    names = ("sub/.gitmodules", "sub/inner", "sub/lib.v")  # a nested gitlink is one path

    added = changed_files(parent, without_sub, with_sub)
    removed = changed_files(parent, with_sub, without_sub)

    assert added.submodules == (SubmoduleChange("sub", None, sub_pin, names),)
    assert removed.submodules == (SubmoduleChange("sub", sub_pin, None, names),)


def test_submodule_without_objects_has_an_unknown_count_and_is_protected_when_a_protected_path_lies_under_it(
    tmp_path: Path,
) -> None:
    from booley.measurement.base import SubmoduleChange, changed_files, protected_edits

    repo = new_repository(tmp_path / "repo")
    write(repo / "top.v", "top\n")
    git(repo, "add", "top.v")
    add_gitlink(repo, "libs/core")
    fork = commit_all(repo, staged_only=True)
    add_gitlink(repo, "libs/core", "b" * 40)
    head = commit_all(repo, staged_only=True)

    changed = changed_files(repo, fork, head)

    assert changed.submodules == (SubmoduleChange("libs/core", FAKE_OID, "b" * 40, None),)
    assert protected_edits(changed, ["libs/core/rtl.v"]) == ("libs/core",)
    assert protected_edits(changed, ["top.v", "libs/other/rtl.v"]) == ()


def test_protected_edits_match_files_directory_prefixes_and_submodule_files() -> None:
    from booley.measurement.base import (
        ChangedFile,
        ChangedFiles,
        SubmoduleChange,
        is_protected,
        protected_edits,
    )

    changed = ChangedFiles(
        tuple(
            ChangedFile(name, "M", constraint=False)
            for name in ("booley.toml", "vectors/a.hex", "rtl.v")
        ),
        (SubmoduleChange("sub", "a", "b", ("sub/tb.v", "sub/lib.v")),),
    )

    assert is_protected("vectors/a.hex", ["vectors/"])
    assert not is_protected("vectors", ["vectors/"])
    assert protected_edits(changed, ["booley.toml", "vectors/"]) == (
        "booley.toml",
        "vectors/a.hex",
    )
    assert protected_edits(changed, ["sub/tb.v"]) == ("sub",)
    assert protected_edits(changed, ["sub/"]) == ("sub",)
    assert protected_edits(changed, ["sub"]) == ("sub",)  # the submodule path itself
    assert protected_edits(changed, ["sub/other.v"]) == ()
