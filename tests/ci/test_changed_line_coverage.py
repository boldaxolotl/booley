"""Local changed-line coverage gate: stamp freshness, merge base, and CI parity."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from booley.dev_support import changed_line_coverage as gate

_SOURCE_ROOT = Path(__file__).resolve().parents[2]
_LAUNCHER = _SOURCE_ROOT / ".github/scripts/changed_line_coverage.py"
_CODE = "def check(value):\n    return value\n"
_MESSAGE = 'MESSAGE = "Session Runtime unavailable"\n'


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ("git", "-c", "user.name=T", "-c", "user.email=t@example.com", *args),
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    ).stdout.strip()


def _commit(repo: Path, files: dict[str, str], message: str) -> str:
    for name, text in files.items():
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_text(text, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path: Path, isolated_git_attributes: None) -> Path:
    """A checkout whose branch changes one code file and renames one message."""
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    script = ".github/scripts/terminology_diff_exclusions.py"
    _commit(
        root,
        {
            script: (_SOURCE_ROOT / script).read_text(encoding="utf-8"),
            "src/booley/code.py": "VALUE = 1\n",
            "src/booley/message.py": _MESSAGE,
        },
        "base",
    )
    _git(root, "switch", "-q", "-c", "codex/feature")
    _commit(
        root,
        {
            "src/booley/code.py": "VALUE = 1\n" + _CODE,
            "src/booley/message.py": _MESSAGE.replace("Session Runtime", "Sandbox"),
        },
        "feature",
    )
    return root


def _report(root: Path, covered: set[int]) -> Path:
    """Write a stamped report where only ``covered`` lines of code.py ran and message.py never ran."""
    lines = "".join(
        f'<line number="{number}" hits="{int(number in covered)}"/>' for number in (1, 2, 3)
    )
    xml = root.parent / "cache" / "coverage.xml"
    xml.parent.mkdir(exist_ok=True)
    xml.write_text(
        '<?xml version="1.0" ?><coverage version="7.17.0"><sources><source></source></sources>'
        '<packages><package name="src.booley"><classes>'
        f'<class name="code.py" filename="src/booley/code.py"><lines>{lines}</lines></class>'
        '<class name="message.py" filename="src/booley/message.py">'
        '<lines><line number="1" hits="0"/></lines></class>'
        "</classes></package></packages></coverage>",
        encoding="utf-8",
    )
    gate.finish_stamp(root, xml, gate.begin_stamp(root, xml))
    return xml


@pytest.mark.parametrize(("covered", "status"), [({1, 2, 3}, 0), ({1, 2}, 1)])
def test_gate_matches_ci_threshold_and_exclusions(repo, covered, status, capsys):
    """One of two changed code lines is 50%; the terminology-only file never counts."""
    xml = _report(repo, covered)
    assert gate.check(repo, xml, sys.executable) == status
    assert "terminology-only files: 1" in capsys.readouterr().out


def test_stamp_refuses_missing_failed_and_stale_runs(repo, capsys):
    xml = _report(repo, {1, 2, 3})
    assert gate.stale_reason(repo, xml) is None

    (repo / "src/booley/code.py").write_text("VALUE = 2\n" + _CODE, encoding="utf-8")
    assert gate.check(repo, xml, sys.executable) == 2
    assert "pytest.broad" in capsys.readouterr().err
    assert gate.stale_reason(repo, xml) == "the coverage report measured a different source tree"

    fingerprint = gate.begin_stamp(repo, xml)  # a new run starts...
    assert "no stamped coverage report" in str(gate.stale_reason(repo, xml))
    _commit(repo, {"src/booley/code.py": "VALUE = 3\n" + _CODE}, "edited mid-run")
    assert not gate.finish_stamp(repo, xml, fingerprint)  # ...and measured mixed source

    xml.unlink()
    gate.finish_stamp(repo, xml, gate.begin_stamp(repo, xml))
    assert not gate.stamp_path(xml).exists()


def test_merge_base_prefers_the_newest_main(repo):
    first = _git(repo, "rev-parse", "main")
    _git(repo, "switch", "-q", "main")
    newer = _commit(repo, {"README": "x\n"}, "main moves")
    _git(repo, "switch", "-q", "codex/feature")
    _git(repo, "merge", "-q", "--no-edit", "main")
    _git(repo, "update-ref", "refs/remotes/origin/main", first)  # stale fetch
    assert gate.merge_base(repo) == newer
    _git(repo, "update-ref", "refs/remotes/origin/main", newer)
    _git(repo, "update-ref", "refs/heads/main", first)  # stale local main
    assert gate.merge_base(repo) == newer

    _git(repo, "switch", "-q", "--orphan", "codex/unrelated")
    _commit(repo, {"other": "x\n"}, "unrelated")
    with pytest.raises(gate.CoverageGateError, match="no history"):
        gate.merge_base(repo)


def test_launcher_outside_a_checkout_explains_and_fails(tmp_path):
    result = subprocess.run(
        (sys.executable, str(_LAUNCHER), "--coverage-xml", str(tmp_path / "coverage.xml")),
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 2
    assert "changed-line coverage: git rev-parse --show-toplevel failed" in result.stderr
