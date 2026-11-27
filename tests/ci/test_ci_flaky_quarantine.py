"""The flaky-test quarantine guard, run as the ``lint`` job runs it."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from tests.path_tool_fake import PathToolFake
from tests.subprocess_budget import run_bounded_child

_ROOT = Path(__file__).parents[2]
_SCRIPTS = _ROOT / ".github/scripts"
_SAMPLE = "tests/ci/test_sample.py"
_CASE = f"{_SAMPLE}::test_p[1]"
_METHOD = f"{_SAMPLE}::TestC::test_m"
_SOURCE = """\
import pytest


def _helper():
    pass


@pytest.mark.parametrize("n", [1, 2])
def test_p(n):
    pass


class TestC:
    def test_m(self):
        pass
"""


def _guard(root: Path, entries: object) -> subprocess.CompletedProcess[str]:
    """Run the guard in a checkout at *root* whose registry lists *entries*."""
    (root / "tests/ci").mkdir(parents=True)
    (root / "pytest.ini").write_bytes(b"[pytest]\n")
    (root / _SAMPLE).write_bytes(_SOURCE.encode())
    (root / "tests/ci/test_broken.py").write_bytes(b"import missing_module_5611\n")
    (root / "tests/flaky_quarantine.py").write_bytes(f"QUARANTINE = {entries!r}\n".encode())
    env = {
        name: value
        for name, value in os.environ.items()
        if not name.startswith(("PYTEST_", "COVERAGE_", "COV_CORE_"))
    }
    env["PYTHONPATH"] = os.pathsep.join([str(_SCRIPTS), str(_ROOT / "src")])
    script = str(_SCRIPTS / "ci_flaky_quarantine.py")
    return run_bounded_child([sys.executable, script], cwd=root, env=env)


def _answer(
    gh: PathToolFake,
    number: int,
    test_id: str,
    state: str = "open",
    label: str = "flaky",
    **extra: object,
) -> None:
    issue = {"state": state, "title": f"Flaky test: {test_id}", "labels": [{"name": label}]}
    gh.answer(f"repos/{{owner}}/{{repo}}/issues/{number}", stdout=json.dumps(issue | extra))


_OWNED = ((_CASE, 11),)
_NO_ID = f"{_SAMPLE}::_helper"


@pytest.mark.parametrize(
    ("entries", "issue", "problems"),
    [
        pytest.param(
            ((_CASE,), (_METHOD, 0)),
            None,
            [f"{_CASE}: no owner issue", f"{_METHOD}: no owner issue"],
            id="no-owner",
        ),
        pytest.param(
            (("tests/ci/test_gone.py::test_p", 11),),
            None,
            ["tests/ci/test_gone.py::test_p: no such test"],
            id="unknown-module",
        ),
        pytest.param(((_NO_ID, 11),), None, [f"{_NO_ID}: no such test"], id="helper-function"),
        pytest.param(
            ((f"{_SAMPLE}::test_p[9]", 11),),
            None,
            [f"{_SAMPLE}::test_p[9]: no such test"],
            id="unknown-case",
        ),
        pytest.param(_OWNED * 2, None, [f"{_CASE}: listed twice"], id="listed-twice"),
        pytest.param(
            (("tests/ci/test_broken.py::test_x", 11),),
            None,
            ["pytest collection exited 2", "missing_module_5611"],
            id="broken-module",
        ),
        pytest.param(
            _OWNED,
            {"state": "closed"},
            [f"{_CASE}: owner issue #11 is closed"],
            id="closed-issue",
        ),
        pytest.param(
            _OWNED,
            {"label": "bug"},
            [f"{_CASE}: owner issue #11 lacks the flaky label"],
            id="not-flaky",
        ),
        pytest.param(
            _OWNED,
            {"test_id": f"{_SAMPLE}::test_p[2]"},
            [f"{_CASE}: owner issue #11 has a title that does not name the test"],
            id="other-test",
        ),
        pytest.param(
            _OWNED,
            {"pull_request": {}},
            [f"{_CASE}: owner issue #11 is a pull request"],
            id="pull-request",
        ),
        pytest.param(
            ((_METHOD, 12),),
            {},
            [f"{_METHOD}: owner issue #12: ", "no answer for api"],
            id="unreadable-issue",
        ),
    ],
)
def test_the_guard_names_each_bad_entry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    entries: object,
    issue: dict[str, object] | None,
    problems: list[str],
) -> None:
    gh = PathToolFake.install(tmp_path / "bin", "gh", monkeypatch)
    if issue is not None:
        _answer(gh, 11, **{"test_id": _CASE, **issue})  # type: ignore[arg-type]

    result = _guard(tmp_path / "w", entries)

    assert result.returncode == 1, result.stderr
    assert [problem for problem in problems if problem not in result.stderr] == []


def test_open_owner_issues_pass(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    gh = PathToolFake.install(tmp_path / "bin", "gh", monkeypatch)
    _answer(gh, 11, _CASE)
    _answer(gh, 12, _METHOD)

    result = _guard(tmp_path / "w", ((_CASE, 11), (_METHOD, 12)))

    assert (result.returncode, result.stderr) == (0, "")
    assert "Quarantine entries checked: 2" in result.stdout
    assert [call.args[-1] for call in gh.calls()] == [
        "repos/{owner}/{repo}/issues/11",
        "repos/{owner}/{repo}/issues/12",
    ]
