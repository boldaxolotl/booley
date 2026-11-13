"""``booley`` refuses a ``booley.toml`` that still sets a retired ``[jobs]`` key (ADR 0087)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from booley.harness import booley as cli
from booley.runtime import booley_dir

RETIRED = b'[project]\nname = "unit"\n\n[jobs]\nmax_heavy = 2\nheavy_memory = "8g"\n'


def _project(root: Path, toml: bytes = RETIRED) -> Path:
    """A Git Project with one Booley Directory holding *toml*."""
    root.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(root)], check=True)
    (root / ".booley").mkdir()
    (root / ".booley" / "booley.toml").write_bytes(toml)
    return root


def _run(root: Path, monkeypatch: pytest.MonkeyPatch, *argv: str) -> int:
    monkeypatch.chdir(root)
    monkeypatch.delenv("BOOLEY_DIR", raising=False)
    monkeypatch.setattr("sys.argv", ["booley", *argv])
    booley_dir.reset_cache()
    return cli.main()


def test_project_commands_refuse_retired_jobs_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _project(tmp_path / "proj")
    for argv in (
        ("targets",),
        ("resources",),
        ("flow", "sim", "--dry-run"),
    ):
        assert _run(root, monkeypatch, *argv) == 2, argv
        err = capsys.readouterr().err
        assert "max_heavy (line 5)" in err, argv
        assert "heavy_memory (line 6)" in err, argv
        assert "delete these lines" in err, argv
        assert "memory admission replaced them" in err, argv
        assert "booley-heal" in err, argv


def test_doctor_and_upgrade_are_not_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _project(tmp_path / "proj")
    reached: list[str] = []
    monkeypatch.setattr(
        cli, "_dispatch_selected", lambda _args, command, *_rest: reached.append(command) or 0
    )
    for argv in (("doctor",), ("upgrade", "status", "--json")):
        assert _run(root, monkeypatch, *argv) == 0, argv
    assert reached == ["doctor", "upgrade"]


def test_a_linked_worktree_copy_is_not_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Only the Main Booley Directory's copy counts: an exported checkout stays measurable."""
    main = _project(tmp_path / "main", b'[project]\nname = "unit"\n')
    subprocess.run(
        ["git", "-C", str(main), "-c", "user.name=t", "-c", "user.email=t@t", "commit",
         "-q", "--allow-empty", "-m", "init"],
        check=True,
    )  # fmt: skip
    worktree = tmp_path / "wt"
    subprocess.run(["git", "-C", str(main), "worktree", "add", "-q", str(worktree)], check=True)
    (worktree / ".booley").mkdir(exist_ok=True)
    (worktree / ".booley" / "booley.toml").write_bytes(RETIRED)
    reached: list[str] = []
    monkeypatch.setattr(
        cli, "_dispatch_selected", lambda _args, command, *_rest: reached.append(command) or 0
    )

    assert _run(worktree, monkeypatch, "targets") == 0
    assert reached == ["targets"]
    assert "retired" not in capsys.readouterr().err
