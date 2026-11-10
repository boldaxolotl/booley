"""`booley worktree gc`: idle selection, refusals kept with reasons, the age flag and knob."""

from __future__ import annotations

import importlib
import os
import shutil
import time
from pathlib import Path

import pytest

from booley.audit.diagnostic_results import Findings
from booley.harness import booley as tlr
from booley.harness import worktree_cmd
from booley.harness.setup import readiness
from tests.goals.conftest import git
from tests.harness.test_worktree_rm import _add, _commit, _versioned

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")

DAY = 86400
REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A Project whose main checkout holds one commit."""
    monkeypatch.delenv("BOOLEY_DIR", raising=False)
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    # A developer's global ignore file may hide `.booley`; these tests count what Git reports.
    no_excludes = tmp_path / "no-excludes"
    no_excludes.write_bytes(b"")
    git(root, "config", "core.excludesFile", str(no_excludes))
    (root / "base.txt").write_bytes(b"base\n")
    git(root, "add", "base.txt")
    git(root, "commit", "-qm", "base")
    return root


def _idle(root: Path, name: str, days: float) -> Path:
    """Register worktree *name*, last used *days* ago (days from any threshold under test)."""
    path = _add(root, name)
    _stamp(root, name, days)
    return path


def _stamp(root: Path, name: str, days: float) -> None:
    """Record worktree *name*'s last use as *days* ago."""
    stamp = root / ".booley" / "worktrees" / ".last-used" / name
    stamp.parent.mkdir(parents=True, exist_ok=True)
    stamp.write_bytes(b"")
    then = time.time() - days * DAY
    os.utime(stamp, (then, then))


def _gc(root: Path, *flags: str) -> int:
    args = tlr._build_parser().parse_args(["worktree", "gc", *flags])
    return worktree_cmd.run(args, root)


def _knob(root: Path, value: str) -> None:
    (root / ".booley").mkdir(exist_ok=True)
    (root / ".booley" / "booley.toml").write_bytes(
        f"[worktree]\ngc_older_than = {value}\n".encode()
    )


def test_gc_removes_an_idle_clean_worktree(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = _idle(repo, "s1", 10)

    assert _gc(repo) == 0

    out = capsys.readouterr().out
    assert "Removed worktree s1.\nBranches kept: booley/s1\n" in out
    assert out.endswith("Removed 1 worktree(s); kept 0.\n")
    assert not path.exists()


def test_gc_removes_an_idle_paired_worktree_keeping_evidence(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _versioned(repo)
    path = _add(repo, "s1", paired=True)
    _stamp(repo, "s1", 10)
    (path / ".booley" / "handoffs").mkdir()
    (path / ".booley" / "handoffs" / "h.md").write_bytes(b"handoff\n")

    assert _gc(repo) == 0

    (kept,) = (repo / ".booley" / "worktrees" / ".evidence").glob("s1-*")
    assert (kept / "handoffs" / "h.md").read_bytes() == b"handoff\n"
    assert f"Evidence kept in {kept}" in capsys.readouterr().out
    assert not path.exists()


def test_gc_keeps_a_recent_worktree_and_says_why(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = _idle(repo, "s1", 1)

    assert _gc(repo) == 0

    out = capsys.readouterr().out
    assert "Kept worktree s1: last used 1d ago, not older than 7d\n" in out
    assert out.endswith("Removed 0 worktree(s); kept 1.\n")
    assert path.is_dir()


def test_gc_keeps_an_idle_worktree_rm_refuses_and_prints_the_reasons(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = _idle(repo, "s1", 10)
    _commit(path, "work.txt")
    (path / "base.txt").write_bytes(b"edited\n")

    assert _gc(repo) == 0

    out = capsys.readouterr().out
    assert "Kept worktree s1: refused\n" in out
    assert "  - 1 uncommitted change(s) in the worktree\n" in out
    assert "  - 1 commit(s) on booley/s1 not in main\n" in out
    assert (path / "work.txt").is_file()
    assert (path / "base.txt").read_bytes() == b"edited\n"


def test_gc_keeps_a_worktree_without_a_last_used_record(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = _add(repo, "s1")

    assert _gc(repo) == 0

    assert (
        "Kept worktree s1: no last-used record, so its idle time is unknown\n"
        in capsys.readouterr().out
    )
    assert path.is_dir()


def test_gc_keeps_a_registered_worktree_whose_directory_is_gone_and_says_why(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    shutil.rmtree(_idle(repo, "gone", 10))

    assert _gc(repo) == 0

    out = capsys.readouterr().out
    assert "Kept worktree gone: refused\n" in out
    assert "registered, but its directory is missing" in out
    assert "gone" in git(repo, "worktree", "list", "--porcelain")


def test_gc_from_inside_an_idle_worktree_keeps_it_and_removes_the_others(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    inside = _idle(repo, "s1", 10)
    other = _idle(repo, "s2", 10)
    monkeypatch.chdir(inside)

    assert _gc(inside) == 0

    out = capsys.readouterr().out
    assert (
        "Kept worktree s1: you are inside it; run gc from the main checkout or another worktree\n"
        in out
    )
    assert "Removed worktree s2.\n" in out
    assert inside.is_dir() and not other.exists()


def test_gc_reports_an_errored_removal_and_continues(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from booley.harness import worktree_gc
    from booley.harness.worktree_rm import WorktreeRemovalError, remove_worktree

    def fail_for_s1(root: Path, name: str, *, force: bool):
        if name == "s1":
            raise WorktreeRemovalError("disk on fire")
        return remove_worktree(root, name, force=force)

    monkeypatch.setattr(worktree_gc, "remove_worktree", fail_for_s1)
    first = _idle(repo, "s1", 10)
    second = _idle(repo, "s2", 10)

    assert _gc(repo) == 1

    out = capsys.readouterr().out
    assert "could not remove s1: disk on fire\n" in out
    assert "Removed worktree s2.\n" in out
    assert out.endswith("Removed 1 worktree(s); kept 0; failed 1.\n")
    assert first.is_dir() and not second.exists()


def test_gc_without_worktrees_says_so(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (repo / ".booley").mkdir()
    assert _gc(repo) == 0
    assert capsys.readouterr().out == "No worktrees.\n"


def test_older_than_flag_overrides_the_default(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = _idle(repo, "s1", 2)

    assert _gc(repo, "--older-than", "3d") == 0
    assert "not older than 3d" in capsys.readouterr().out
    assert path.is_dir()

    assert _gc(repo, "--older-than", "36h") == 0
    assert not path.exists()


def test_booley_toml_knob_sets_the_default_age(repo: Path) -> None:
    path = _idle(repo, "s1", 2)
    _knob(repo, '"3d"')

    assert _gc(repo) == 0
    assert path.is_dir()

    _knob(repo, '"1d"')
    assert _gc(repo) == 0
    assert not path.exists()


def test_flag_wins_over_the_knob_and_skips_reading_it(repo: Path) -> None:
    path = _idle(repo, "s1", 2)
    _knob(repo, "5")  # not a string: would fail if it were read

    assert _gc(repo, "--older-than", "1d") == 0
    assert not path.exists()


def test_invalid_older_than_is_refused_by_argparse(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        tlr._build_parser().parse_args(["worktree", "gc", "--older-than", "soon"])

    assert exc.value.code == 2
    assert "invalid age 'soon'" in capsys.readouterr().err


def test_invalid_knob_fails_gc(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = _idle(repo, "s1", 10)
    _knob(repo, '"soon"')

    assert _gc(repo) == 1

    assert "ERROR: booley.toml [worktree].gc_older_than" in capsys.readouterr().err
    assert path.is_dir()


def test_invalid_knob_fails_doctor_validation(tmp_path: Path) -> None:
    findings = Findings()

    assert not readiness._validate_booley_toml(
        {"worktree": {"gc_older_than": "soon"}}, tmp_path, findings
    )

    (failure,) = [f for f in findings.report().findings if "gc_older_than" in f.message]
    assert "gc_older_than" in failure.message
    assert 'such as "7d"' in failure.fix


def test_parse_age_grammar() -> None:
    parse_age = importlib.import_module("booley.config.worktree").parse_age

    for text, seconds in (("7d", 7 * DAY), ("1d12h", 129600), ("90m", 5400), ("1h30m15s", 5415)):
        assert parse_age(text) == seconds
    for bad in ("", "7", "0d", "12h1d", "7days", "-1d", "1d1d", "d", " 7d", "1" * 400 + "d"):
        with pytest.raises(ValueError, match="age"):
            parse_age(bad)


def test_cli_reference_and_config_document_gc() -> None:
    cli = (REPO_ROOT / "docs/user/CLI-REFERENCE.md").read_text(encoding="utf-8")
    config = (REPO_ROOT / "docs/user/CONFIG.md").read_text(encoding="utf-8")
    packaged = (REPO_ROOT / "src/booley/data/docs/CONFIG.md").read_text(encoding="utf-8")

    assert "booley worktree gc [--older-than AGE]" in cli
    assert "gc_older_than" in config
    assert "gc_older_than" in packaged
