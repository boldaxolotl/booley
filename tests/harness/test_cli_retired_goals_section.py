"""``booley`` refuses a ``booley.toml`` that still has a ``[goals]`` section (Goal Mode is removed)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from booley.harness import booley as cli
from booley.runtime import booley_dir

RETIRED = b'[project]\nname = "unit"\n\n[goals]\nquiet_after = 60\n'


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


def test_project_commands_refuse_a_goals_section(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _project(tmp_path / "proj")
    for argv in (("targets",), ("resources",), ("flow", "sim", "--dry-run")):
        assert _run(root, monkeypatch, *argv) == 2, argv
        err = capsys.readouterr().err
        assert "[goals] section (line 4)" in err, argv
        assert "delete the [goals] tables (lines 4-5)" in err, argv
        assert "type /booley-heal in a Developer Agent chat" in err, argv


def test_the_heal_route_is_not_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _project(tmp_path / "proj")
    reached: list[str] = []
    monkeypatch.setattr(
        cli, "_dispatch_selected", lambda _args, command, *_rest: reached.append(command) or 0
    )
    for argv in (
        ("doctor",),
        ("upgrade", "status", "--json"),
        ("session", "enter", "--", "true"),
        ("session", "prepare"),
    ):
        assert _run(root, monkeypatch, *argv) == 0, argv
    assert reached == ["doctor", "upgrade", "session", "session"]
