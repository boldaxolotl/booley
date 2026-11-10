"""The generated mutmut ``also_copy`` list covers the checkout, never untracked output."""

from __future__ import annotations

import configparser
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / ".github/scripts"))

from mutmut_also_copy import (  # pyright: ignore[reportMissingImports]
    AlsoCopyError,
    also_copy_entries,
    main,
    missing_after_write,
    write_also_copy,
)

SKIPPED = ["src/", "tests", "setup.cfg", "pyproject.toml"]
GIT_TIMEOUT_SECONDS = 60


def test_lists_every_tracked_entry_mutmut_does_not_copy() -> None:
    """A new hidden folder with one Markdown file is covered; mutmut's own copies are not."""
    tracked = [
        "src/booley/__init__.py",
        "tests/test_a.py",
        "setup.cfg",
        "pyproject.toml",
        "VERSION",
        ".gitignore",
        "docs/a.md",
        "docs/sub/b.md",
        ".newdir/notes.md",
    ]

    assert also_copy_entries(tracked, [], SKIPPED) == [
        ".newdir/",
        "docs/",
        ".gitignore",
        "VERSION",
    ]


def test_splits_a_crate_around_its_untracked_target_tree() -> None:
    """Folders holding untracked build output are copied per tracked child."""
    tracked = [
        "crates/k/Cargo.toml",
        "crates/k/src/lib.rs",
        "crates/k/tests/t.rs",
        "crates/k/fuzz/Cargo.toml",
        "crates/k/fuzz/fuzz_targets/f.rs",
        "VERSION",
    ]
    untracked = ["crates/k/target/", "crates/k/fuzz/target/"]

    # `tests` is skipped by whole path only, so the crate's own tests/ is copied.
    assert also_copy_entries(tracked, untracked, SKIPPED) == [
        "crates/k/fuzz/fuzz_targets/",
        "crates/k/src/",
        "crates/k/tests/",
        "VERSION",
        "crates/k/Cargo.toml",
        "crates/k/fuzz/Cargo.toml",
    ]


def test_rejects_a_file_whose_parent_no_directory_entry_creates() -> None:
    """mutmut copies files without creating parents, so this layout cannot be copied."""
    with pytest.raises(AlsoCopyError, match=r"cannot copy docs/a\.md.*docs/scratch\.txt"):
        also_copy_entries(["docs/a.md"], ["docs/scratch.txt"], SKIPPED)


def test_reports_tracked_paths_mutmut_would_read_back_as_comments(tmp_path: Path) -> None:
    """A name starting with ``#`` vanishes when mutmut re-reads ``setup.cfg``."""
    setup_cfg = tmp_path / "setup.cfg"
    setup_cfg.write_bytes(b"[mutmut]\nsource_paths =\n    src/\n")
    write_also_copy(setup_cfg, ["docs/", "#notes.md"])

    assert missing_after_write(setup_cfg, ["docs/a.md", "#notes.md", "src/x.py"], SKIPPED) == [
        "#notes.md"
    ]


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, timeout=GIT_TIMEOUT_SECONDS)


def test_script_writes_the_list_for_a_real_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Real ``git ls-files`` output: ignored ``target/`` is untracked, other keys survive."""
    for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    files = {
        "setup.cfg": "[mutmut]\nsource_paths =\n    src/\nonly_mutate =\n    src/x/*.py\n",
        "src/x/a.py": "",
        "pyproject.toml": "",
        ".newdir/notes.md": "",
        ".gitignore": "target/\n",
        "crates/k/Cargo.toml": "",
        "crates/k/src/lib.rs": "",
        "crates/k/target/debug/x": "",
    }
    for name, text in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode())
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "add", "--", *(name for name in files if "target" not in name))

    exit_code = main()

    assert exit_code == 0
    parser = configparser.ConfigParser()
    parser.read(tmp_path / "setup.cfg", encoding="utf-8")
    read_back = [line for line in parser.get("mutmut", "also_copy").split("\n") if line]
    assert read_back == [".newdir/", "crates/k/src/", ".gitignore", "crates/k/Cargo.toml"]
    assert parser.get("mutmut", "only_mutate").strip() == "src/x/*.py"


def test_script_fails_before_mutmut_when_the_layout_cannot_be_copied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The step exits 1 with a clear message before mutmut starts."""
    for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    (tmp_path / "setup.cfg").write_bytes(b"[mutmut]\nsource_paths =\n    src/\n")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs/a.md").write_bytes(b"")
    (tmp_path / "docs/scratch.txt").write_bytes(b"")
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "add", "--", "setup.cfg", "docs/a.md")

    exit_code = main()

    assert exit_code == 1
    assert "error: cannot copy docs/a.md" in capsys.readouterr().err
