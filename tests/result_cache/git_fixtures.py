"""Repository, submodule and Booley-layout builders for the Result Cache tests."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from booley.core.booley_dir import reset_cache
from booley.runtime.git_environment import REPOSITORY_SELECTION_VARIABLES
from tests.subprocess_budget import run_bounded_child

BOOLEY_FILES = {
    "booley.toml": 'name = "x"\n',
    "tests.toml": "",
    "criteria.toml": "",
    "goalsets/feature.md": "# feature\n",
    "goalsets/data.hex": "00\n",
    "cores/x.core": "CAPI=2:\n",
}
RTL_FILES = {"src/a.v": "module a; endmodule\n", "README": "readme\n"}


@dataclass(frozen=True)
class Layout:
    project: Path  # the Project Root
    rtl: Path  # the RTL repository
    booley: Path | None  # the Booley Directory's own repository, when it has one


def isolate(monkeypatch: pytest.MonkeyPatch) -> None:
    """No inherited Booley Directory, cached selection, repository selection or identity."""
    monkeypatch.delenv("BOOLEY_DIR", raising=False)
    for name in REPOSITORY_SELECTION_VARIABLES:
        monkeypatch.delenv(name, raising=False)
    for who in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{who}_NAME", "Test")
        monkeypatch.setenv(f"GIT_{who}_EMAIL", "test@example.com")
    reset_cache()


def check(project: Path) -> Any:
    from booley.result_cache.clean_commit import check_clean_commit

    reset_cache()
    return check_clean_commit(project)


def digest(project: Path) -> str:
    result = check(project)
    assert result.cacheable, result.reasons
    assert result.tracked_digest is not None
    return result.tracked_digest


def all_cases(cases: Iterable[Any]) -> Callable[[Callable[..., None]], Callable[[Path], None]]:
    """Run a ``(case, tmp_path)`` body for every case as one test, naming a failing case."""

    def decorate(run: Callable[..., None]) -> Callable[[Path], None]:
        def test(tmp_path: Path) -> None:
            for case in cases:
                name = case if isinstance(case, str) else case[0]
                (tmp_path / name).mkdir()
                try:
                    run(case, tmp_path / name)
                except Exception as exc:
                    exc.add_note(f"case {name}")
                    raise

        test.__name__ = run.__name__
        return test

    return decorate


def git(cwd: Path, *args: str) -> str:
    """Run Git in *cwd* (file-protocol submodules allowed) and return stripped stdout."""
    result = run_bounded_child(
        # No detached auto-maintenance: it holds objects/maintenance.lock while tests rmtree.
        ["git", "-c", "protocol.file.allow=always", "-c", "maintenance.auto=false", *args],
        cwd=cwd,
        check=True,
    )
    return result.stdout.strip()


def write_files(root: Path, files: dict[str, str]) -> None:
    for name, text in files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text)


def commit_files(repo: Path, files: dict[str, str], message: str = "change") -> None:
    write_files(repo, files)
    git(repo, "add", "--", *files)
    git(repo, "commit", "-q", "-m", message)


def new_repo(path: Path, files: dict[str, str]) -> Path:
    path.mkdir(parents=True)
    git(path, "init", "-q", "-b", "main")
    commit_files(path, files, "initial")
    return path


def add_submodule(parent: Path, name: str, source: Path) -> Path:
    git(parent, "submodule", "add", "-q", str(source), name)
    git(parent, "commit", "-q", "-m", f"add {name}")
    return parent / name


def clone(source: Path, target: Path, *options: str) -> Path:
    git(source.parent, "clone", "-q", *options, str(source), str(target))
    return target


def build_layout(base: Path, kind: str) -> Layout:
    """One clean Project: ``tracked``, ``standalone``, ``paired``, ``rtl-submodule``, ``outside-rtl-tree``."""
    rtl = base / "rtl"
    if kind == "tracked":
        new_repo(rtl, {**RTL_FILES, **{f".booley/{k}": v for k, v in BOOLEY_FILES.items()}})
        return Layout(rtl, rtl, None)
    if kind == "outside-rtl-tree":
        booley = new_repo(base / "bd", BOOLEY_FILES)
        new_repo(rtl, {**RTL_FILES, "booley.toml": '[project]\ndir = "../bd"\n'})
        return Layout(rtl, rtl, booley)
    new_repo(rtl, RTL_FILES)
    if kind == "standalone":  # a stealth Booley Directory: hidden from the RTL repository
        (rtl / ".git/info/exclude").write_text(".booley/\n")
        return Layout(rtl, rtl, new_repo(rtl / ".booley", BOOLEY_FILES))
    source = new_repo(base / "bsrc", BOOLEY_FILES)
    if kind == "rtl-submodule":
        return Layout(rtl, rtl, add_submodule(rtl, ".booley", source))
    assert kind == "paired"  # untracked linked worktree: RTL status shows `?? .booley/`
    git(source, "worktree", "add", "-q", "-b", "linked", str(rtl / ".booley"))
    return Layout(rtl, rtl, rtl / ".booley")
