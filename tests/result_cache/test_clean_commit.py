"""Clean-commit participants, the clean rule and symlink containment (ADR 0076 §1)."""

from __future__ import annotations

import os
import shutil
from collections.abc import Callable
from pathlib import Path

import pytest

from tests.result_cache.git_fixtures import (
    BOOLEY_FILES,
    Layout,
    add_submodule,
    all_cases,
    build_layout,
    check,
    commit_files,
    git,
    isolate,
    new_repo,
    write_files,
)

pytestmark = [
    pytest.mark.skipif(os.name == "nt", reason="the Result Cache runs only in the Linux Sandbox"),
    pytest.mark.usefixtures("isolated_git_excludes"),
]
BOOLEY_TREE = {f".booley/{name}": text for name, text in BOOLEY_FILES.items()}


@pytest.fixture(autouse=True)
def _isolated(monkeypatch: pytest.MonkeyPatch) -> None:
    isolate(monkeypatch)


def exclude(repo: Path, pattern: str) -> None:
    # `.git` is a file in a submodule: ask Git where its exclude file is.
    path = repo / git(repo, "rev-parse", "--git-path", "info/exclude")
    with path.open("a") as handle:
        handle.write(f"{pattern}\n")


def assert_refused(project: Path, *fragments: str) -> None:
    result = check(project)
    assert not result.cacheable
    assert result.tracked_digest is None
    assert any(all(part in r for part in fragments) for r in result.reasons), result.reasons


# --- AC1, AC2: dirty participants ----------------------------------------------


def dirty_layout(base: Path) -> Layout:
    """A clean paired Project with submodules and Git settings that hide changes."""
    layout = build_layout(base, "paired")
    rtl, booley = layout.rtl, layout.booley
    assert booley is not None
    commit_files(rtl, {"low.v": "module low; endmodule\n"})
    add_submodule(rtl, "sub", new_repo(base / "sub", {"s.v": "s\n"}))
    add_submodule(booley, "bsub", new_repo(base / "bsub", {"b.v": "b\n"}))
    # `mid` hides its own submodule `inner` from the outer status (`ignore = all`).
    mid = new_repo(base / "mid", {"m.v": "m\n"})
    add_submodule(mid, "inner", new_repo(base / "inner", {"i.v": "i\n"}))
    git(mid, "config", "-f", ".gitmodules", "submodule.inner.ignore", "all")
    commit_files(mid, {".gitmodules": (mid / ".gitmodules").read_text()})
    add_submodule(rtl, "mid", mid)
    git(rtl, "submodule", "update", "--init", "--recursive", "-q")
    hook = base / "monitor.sh"  # a file-system monitor that reports no change
    hook.write_text('#!/bin/sh\nprintf "token\\0"\n')
    hook.chmod(0o755)
    for key, value in (
        ("core.fsmonitor", str(hook)),
        ("core.untrackedCache", "true"),
        ("core.ignoreCase", "true"),
        ("diff.ignoreSubmodules", "all"),  # the parent's plain status ignores submodules
    ):
        git(rtl, "config", key, value)
    git(rtl, "update-index", "--fsmonitor", "--untracked-cache")
    git(rtl, "status", "--porcelain")
    return layout


def plain_status(repo: Path) -> str:
    """Strict status without the check's protective flags, minus the exempt Paired Checkout."""
    status = git(
        repo, "status", "--porcelain", "--untracked-files=all", "--ignore-submodules=none"
    )
    return "\n".join(line for line in status.splitlines() if line != "?? .booley/")


# case -> (participant, files to write, text the reason names)
DIRTY_CASES = {
    "rtl-modified": ("rtl", ["src/a.v"], "src/a.v"),
    "rtl-staged": ("rtl", ["src/a.v"], "src/a.v"),
    "rtl-untracked": ("rtl", ["build.sh"], "build.sh"),
    "rtl-dirty-submodule": ("rtl", ["sub/s.v"], "s.v"),
    "rtl-submodule-off-commit": ("rtl", [], "sub"),
    "rtl-nested-submodule-ignore-all": ("rtl", ["mid/inner/i.v", "mid/inner/new.v"], "i.v"),
    "rtl-modified-hidden-by-fsmonitor": ("rtl", ["src/a.v"], "src/a.v"),
    "rtl-untracked-hidden-by-ignorecase": ("rtl", ["LOW.v"], "LOW.v"),
    "rtl-nested-repository": ("rtl", [], "nested/"),
    "booley-modified": ("booley", ["cores/x.core"], "cores/x.core"),
    "booley-staged": ("booley", ["cores/x.core"], "cores/x.core"),
    "booley-untracked": ("booley", ["new.core"], "new.core"),
    "booley-dirty-submodule": ("booley", ["bsub/b.v"], "b.v"),
}


@all_cases(DIRTY_CASES)
def test_dirty_participant_is_not_clean(case: str, tmp_path: Path) -> None:
    layout = dirty_layout(tmp_path)
    clean = check(layout.project)
    assert clean.cacheable, clean.reasons
    assert [p.label for p in clean.participants] == ["rtl", "booley"]
    label, files, named = DIRTY_CASES[case]
    repo = layout.rtl if label == "rtl" else layout.booley
    assert repo is not None
    write_files(repo, dict.fromkeys(files, "changed\n"))
    if case.endswith("staged"):
        git(repo, "-c", "core.fsmonitor=false", "add", *files)  # the hook would hide the edit
        assert git(repo, "diff", "--cached", "--name-only")  # really staged
    if "hidden" in case or "ignore-all" in case:
        assert plain_status(layout.rtl) == ""  # the unprotected command sees nothing
    if case == "rtl-submodule-off-commit":
        commit_files(layout.rtl / "sub", {"s.v": "moved\n"})
        assert (
            git(layout.rtl, "status", "--porcelain") == "?? .booley/"
        )  # configured status: nothing
    if case == "rtl-nested-repository":
        new_repo(layout.rtl / "nested", {"n": "n\n"})
    assert_refused(layout.project, f"{label} repository", "not clean", named)


def test_core_edit_in_standalone_booley_repository_is_not_clean(tmp_path: Path) -> None:
    layout = build_layout(tmp_path, "standalone")
    assert layout.booley is not None
    clean = check(layout.project)
    assert clean.cacheable, clean.reasons
    assert [p.label for p in clean.participants] == ["rtl", "booley"]
    write_files(layout.booley, {"cores/x.core": "edited\n"})
    assert_refused(layout.project, "booley repository", "not clean", "cores/x.core")


# --- AC4: symlinks leaving the participants ------------------------------------


def commit_links(repo: Path, *links: tuple[str, str]) -> None:
    """Commit each ``(name, target)`` symlink, replacing a tracked file of that name."""
    for name, target in links:
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).unlink(missing_ok=True)
        (repo / name).symlink_to(target)
    git(repo, "add", "--", *(name for name, _ in links))
    git(repo, "commit", "-q", "-m", "links")


def ignored_link(repo: Path, name: str, target: str, pattern: str | None = None) -> None:
    exclude(repo, pattern or name)
    (repo / name).symlink_to(target)


def nested_ignored_link(rtl: Path, out: str) -> None:
    new_repo(rtl / "nested", {"n": "n\n"})
    ignored_link(rtl, "nested/l", out, "nested/")


def submodule_link(rtl: Path, out: str, *, tracked: bool) -> None:
    sub = add_submodule(rtl, "sub", new_repo(rtl.parent / "sub", {"s.v": "s\n"}))
    if tracked:
        commit_links(sub, ("link", out))
    else:
        ignored_link(sub, "ign", out)


def ignored_directory_link(rtl: Path, out: str) -> None:
    (rtl / "build").mkdir()
    ignored_link(rtl, "build/l", out, "build/")


def skip_worktree_link(rtl: Path, out: str) -> None:
    git(rtl, "update-index", "--skip-worktree", "src/a.v")
    (rtl / "src/a.v").unlink()
    (rtl / "src/a.v").symlink_to(out)


def hop_link(rtl: Path, out: str) -> None:
    (rtl.parent / "back").symlink_to(rtl / "src")  # outside, but points back inside
    commit_links(rtl, ("hop", "../back"))


def dotdot_hop_link(rtl: Path, out: str) -> None:
    (rtl.parent / "back").symlink_to(rtl / "src")  # `inc/../back` leaves and returns
    commit_links(rtl, ("inc", "."), ("A", "inc/../back/a.v"))


def sibling_link(rtl: Path, out: str) -> None:
    (rtl.parent / "rtl-x").mkdir()  # a sibling whose name starts with the root's
    commit_links(rtl, ("sib", "../rtl-x"))


# case -> (layout, mutation taking (RTL repository, outside file), "label:path" named)
LINK_CASES: dict[str, tuple[str, Callable[[Path, str], None], str]] = {
    "tracked": ("tracked", lambda rtl, out: commit_links(rtl, ("link", out)), "rtl:link"),
    "ignored": ("tracked", lambda rtl, out: ignored_link(rtl, "ign", out), "rtl:ign"),
    "ignored-in-nested-repository": ("tracked", nested_ignored_link, "rtl:nested/l"),
    "ignored-in-plain-ignored-directory": ("tracked", ignored_directory_link, "rtl:build/l"),
    "hop-through-outside-link": ("tracked", hop_link, "rtl:hop"),
    "into-git-directory": ("tracked", lambda r, o: commit_links(r, ("g", ".git/config")), "rtl:g"),
    "dotdot-hop-back-inside": ("tracked", dotdot_hop_link, "rtl:A"),
    "sibling-prefix": ("tracked", sibling_link, "rtl:sib"),
    # `a/l` is lexically inside (`rtl/secret`) but resolves through `inc -> .` to outside.
    "dotdot-through-inside-link": (
        "tracked",
        lambda r, o: commit_links(r, ("inc", "."), ("a/l", "../inc/../secret")),
        "rtl:a/l",
    ),
    "excluded-configuration-file": (
        "tracked",
        lambda rtl, out: commit_links(rtl, (".booley/criteria.toml", out)),
        "rtl:.booley/criteria.toml",
    ),
    "ignored-in-submodule": (
        "tracked",
        lambda rtl, out: submodule_link(rtl, out, tracked=False),
        "rtl:sub/ign",
    ),
    "tracked-in-submodule": (
        "tracked",
        lambda rtl, out: submodule_link(rtl, out, tracked=True),
        "rtl:sub/link",
    ),
    "skip-worktree-hides-symlink": ("tracked", skip_worktree_link, "rtl:src/a.v"),
    "ignored-in-standalone-booley": (
        "standalone",
        lambda rtl, out: ignored_link(rtl / ".booley", "ign", out),
        "booley:ign",
    ),
}


@all_cases(LINK_CASES)
def test_out_of_tree_symlink_is_not_cacheable(case: str, tmp_path: Path) -> None:
    kind, mutate, where = LINK_CASES[case]
    layout = build_layout(tmp_path, kind)
    assert check(layout.project).cacheable
    mutate(layout.rtl, str(tmp_path / "secret"))
    result = check(layout.project)
    assert not result.cacheable
    label, _, name = where.partition(":")
    root = layout.rtl if label == "rtl" else layout.booley
    assert root is not None
    # The whole reason: participant, link and the raw target (what the digest also keys).
    refusal = (
        "steps back with '..' after a name"
        if case == "dotdot-hop-back-inside"
        else ("resolves outside the participants or into .git")
    )
    expected = f"symlink {where} {refusal} ({os.readlink(root / name)})"  # noqa: PTH115
    assert expected in result.reasons, result.reasons


def test_symlink_inside_participants_stays_cacheable(tmp_path: Path) -> None:
    layout = build_layout(tmp_path, "paired")
    commit_links(layout.rtl, ("file", "src/a.v"), ("root", "."), ("booley", ".booley/cores"))
    assert check(layout.project).cacheable


@all_cases(
    [
        ("tracked", ["rtl"]),
        ("standalone", ["rtl", "booley"]),
        ("paired", ["rtl", "booley"]),
        ("rtl-submodule", ["rtl", "booley"]),
        ("outside-rtl-tree", ["rtl", "booley"]),
    ]
)
def test_clean_layout_is_cacheable(case: tuple[str, list[str]], tmp_path: Path) -> None:
    kind, labels = case
    layout = build_layout(tmp_path, kind)
    result = check(layout.project)
    assert result.cacheable, result.reasons
    assert [p.label for p in result.participants] == labels
    assert (result.tracked_digest or "").startswith("sha256:")
    assert result.booley_dir == (layout.booley or layout.rtl / ".booley").resolve()


def test_booley_directory_at_the_repository_root_is_cacheable(tmp_path: Path) -> None:
    rtl = new_repo(tmp_path / "rtl", {"booley.toml": '[project]\ndir = "."\n', "a.v": "a\n"})
    result = check(rtl)
    assert result.cacheable, result.reasons
    assert [p.label for p in result.participants] == ["rtl"]


def test_missing_booley_directory_is_a_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rtl = new_repo(tmp_path / "rtl", {"a.v": "a\n"})  # no local .booley to outrank the variable
    monkeypatch.setenv("BOOLEY_DIR", str(tmp_path / "nowhere"))  # selected, but absent
    with pytest.warns(UserWarning, match="does not exist"):
        assert_refused(rtl, "neither tracked in the RTL repository")


def outside_work_tree(base: Path) -> Path:
    layout = build_layout(base, "tracked")
    (base / "elsewhere").mkdir()
    git(layout.rtl, "config", "core.worktree", str(base / "elsewhere"))
    return layout.project


def unborn_head(base: Path) -> Path:
    layout = build_layout(base, "standalone")
    shutil.rmtree(layout.rtl / ".booley/.git")
    git(layout.rtl / ".booley", "init", "-q", "-b", "main")
    return layout.project


def ignored_project_root(base: Path) -> Path:
    layout = build_layout(base, "tracked")
    exclude(layout.rtl, "scratch/")
    (layout.rtl / "scratch/proj").mkdir(parents=True)
    return layout.rtl / "scratch/proj"


def named_booley_directory(base: Path, name: str) -> Path:
    files = {"cfgx/booley.toml": "", "booley.toml": f'[project]\ndir = "{name}"\n'}
    rtl = new_repo(base / "rtl", files)
    exclude(rtl, f"{name}/")  # status stays clean: only the literal pathspec rule decides
    write_files(rtl, {f"{name}/booley.toml": ""})
    return rtl


def populated_uninitialised_submodule(base: Path) -> Path:
    layout = build_layout(base, "tracked")
    sub = add_submodule(layout.rtl, "sub", new_repo(base / "sub", {"s.v": "s\n"}))
    (sub / ".git").unlink()  # a populated directory that is no repository
    return layout.project


def special_file(base: Path) -> Path:
    layout = build_layout(base, "tracked")
    (layout.rtl / "src/a.v").unlink()
    os.mkfifo(layout.rtl / "src/a.v")  # a tracked path that is neither a file nor a link
    return layout.project


def gitlink_replaced(base: Path, replace: Callable[[Path], None]) -> Path:
    layout = build_layout(base, "tracked")
    sub = add_submodule(layout.rtl, "sub", new_repo(base / "sub", {"s.v": "s\n"}))
    shutil.rmtree(sub)
    replace(sub)
    return layout.project


def unborn_submodule(base: Path) -> Path:
    layout = build_layout(base, "tracked")
    sub = add_submodule(layout.rtl, "sub", new_repo(base / "sub", {"s.v": "s\n"}))
    shutil.rmtree(sub)
    sub.mkdir()
    git(sub, "init", "-q", "-b", "main")  # a repository with no commit: status prints nothing
    return layout.project


def hidden_off_commit_submodule(base: Path) -> Path:
    layout = build_layout(base, "tracked")
    sub = add_submodule(layout.rtl, "sub", new_repo(base / "sub", {"s.v": "s\n"}))
    git(layout.rtl, "update-index", "--assume-unchanged", "sub")
    commit_files(sub, {"s.v": "moved\n"})
    assert git(layout.rtl, "status", "--porcelain") == ""  # the parent sees nothing
    return layout.project


def unreadable_booley_repository(base: Path) -> Path:
    layout = build_layout(base, "standalone")
    (layout.rtl / ".booley/.git").rename(layout.rtl / ".booley/.git.real")
    (layout.rtl / ".booley/.git").write_text("gitdir: /nonexistent\n")  # a stale gitfile
    return layout.project


def unreadable_nested_booley_repository(base: Path) -> Path:
    layout = build_layout(base, "tracked")  # the config is tracked in the RTL index
    (layout.rtl / ".booley/.git").write_text("gitdir: /nonexistent\n")  # a stale gitfile
    return layout.project


def hidden_symlinked_gitlink(base: Path) -> Path:
    layout = build_layout(base, "tracked")
    sub = add_submodule(layout.rtl, "sub", new_repo(base / "sub", {"s.v": "s\n"}))
    git(layout.rtl, "update-index", "--skip-worktree", "sub")
    shutil.rmtree(sub)
    sub.symlink_to(base / "sub")  # a checkout at the recorded commit
    return layout.project


NOT_TRACKED = "neither tracked in the RTL repository"
UNSELECTABLE_CASES: dict[str, tuple[Callable[[Path], Path], str]] = {
    "not-a-repository": (lambda b: b.joinpath("plain").mkdir() or b / "plain", "not in a Git"),
    "project-root-outside-work-tree": (outside_work_tree, "not inside its repository's work tree"),
    "unborn-head": (unborn_head, "has no commit"),
    "no-booley-directory": (lambda b: new_repo(b / "rtl", {"a": "a\n"}), "no Booley Directory"),
    "ignored-project-root": (ignored_project_root, "is ignored by the RTL repository"),
    "untracked-booley-directory": (
        lambda b: write_files(new_repo(b / "rtl", {"a": "a\n"}), BOOLEY_TREE) or b / "rtl",
        NOT_TRACKED,
    ),
    "third-repository": (  # `.booley` tracked by an outer repository, RTL nested in it
        lambda b: new_repo(new_repo(b / "outer", BOOLEY_TREE) / "rtl", {"a": "a\n"}),
        NOT_TRACKED,
    ),
    "glob-named-booley-directory": (lambda b: named_booley_directory(b, "cfg?"), NOT_TRACKED),
    # Without `--`, Git reads this as an option and lists the whole index.
    "option-named-booley-directory": (
        lambda b: named_booley_directory(b, "--exclude=x"),
        NOT_TRACKED,
    ),
    "populated-uninitialised-submodule": (populated_uninitialised_submodule, "populated but not"),
    "tracked-path-is-a-fifo": (special_file, "neither a file nor a symlink"),
    "gitlink-path-is-a-file": (
        lambda b: gitlink_replaced(b, lambda sub: sub.write_text("x")),
        "populated but not",
    ),
    "gitlink-path-is-absent": (lambda b: gitlink_replaced(b, lambda sub: None), "not clean"),
    "assume-unchanged-submodule-off-commit": (hidden_off_commit_submodule, "not at its recorded"),
    "booley-repository-unreadable": (unreadable_booley_repository, "it cannot read"),
    "tracked-booley-with-unreadable-repository": (
        unreadable_nested_booley_repository,
        "it cannot read",
    ),
    "hidden-gitlink-replaced-by-symlink": (hidden_symlinked_gitlink, "populated but not"),
    "submodule-without-commit": (unborn_submodule, "submodule repository with no commit"),
}


@all_cases(UNSELECTABLE_CASES)
def test_unselectable_run_is_not_cacheable(case: str, tmp_path: Path) -> None:
    build, reason = UNSELECTABLE_CASES[case]
    assert_refused(build(tmp_path), reason)


def test_head_moving_during_the_check_is_not_cacheable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from booley.result_cache import clean_commit

    layout = build_layout(tmp_path, "tracked")
    real_check = clean_commit._check

    def check_then_commit(*args: object) -> object:
        result = real_check(*args)  # type: ignore[arg-type]
        commit_files(layout.rtl, {"src/a.v": "module b; endmodule\n"})  # lands after the digest
        return result

    monkeypatch.setattr(clean_commit, "_check", check_then_commit)
    assert_refused(layout.project, "rtl repository", "changed HEAD during the check")


def test_malformed_git_record_raises_clean_commit_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from booley.result_cache import clean_commit, tracked_files

    layout = build_layout(tmp_path, "tracked")
    monkeypatch.setattr(tracked_files, "git_output", lambda *args: b"malformed\0")
    monkeypatch.setattr(clean_commit, "git_output", tracked_files.git_output)
    with pytest.raises(clean_commit.CleanCommitError):
        check(layout.project)


def test_gitlink_replaced_by_symlink_raises(tmp_path: Path) -> None:
    from booley.result_cache.clean_commit import CleanCommitError

    layout = build_layout(tmp_path, "tracked")
    sub = add_submodule(layout.rtl, "sub", new_repo(tmp_path / "sub", {"s.v": "s\n"}))
    shutil.rmtree(sub)
    sub.symlink_to(tmp_path)  # Git itself refuses a symlinked submodule path
    with pytest.raises(CleanCommitError):
        check(layout.project)


def test_git_failure_raises_clean_commit_error(tmp_path: Path) -> None:
    from booley.result_cache.clean_commit import CleanCommitError

    layout = build_layout(tmp_path, "tracked")
    (layout.rtl / ".git/index").write_bytes(b"not an index")
    with pytest.raises(CleanCommitError):
        check(layout.project)


@pytest.mark.skipif(os.name != "posix" or os.geteuid() == 0, reason="root ignores modes")
@all_cases([("unlistable", 0o000), ("unsearchable", 0o444)])
def test_unreadable_directory_in_ignored_repository_raises(
    case: tuple[str, int], tmp_path: Path
) -> None:
    from booley.result_cache.clean_commit import CleanCommitError

    mode = case[1]
    layout = build_layout(tmp_path, "tracked")
    nested_ignored_link(layout.rtl, str(tmp_path / "secret"))
    (layout.rtl / "nested/deep").mkdir()
    (layout.rtl / "nested/deep/l").symlink_to(tmp_path / "secret")  # unseen when skipped
    (layout.rtl / "nested/deep").chmod(mode)
    try:
        with pytest.raises(CleanCommitError):
            check(layout.project)
    finally:
        (layout.rtl / "nested/deep").chmod(0o755)
