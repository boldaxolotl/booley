"""The CI wrapper reruns a few failed tests once, serially, and records both attempts."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from xml.etree import ElementTree

import coverage
import pytest
from tests.subprocess_budget import run_bounded_child

_ROOT = Path(__file__).parents[2]
_SCRIPTS = _ROOT / ".github/scripts"

_HEADER = """\
import os
import pathlib

import pytest

DIR = pathlib.Path(os.environ["CASE_DIR"])


def attempt(name):
    worker = os.environ.get("PYTEST_XDIST_WORKER", "serial")
    # One numbered file per attempt, claimed by exclusive create: two workers appending to
    # one file can overwrite each other's lines on Windows, where an append seeks, then writes.
    log = DIR / "log"
    log.mkdir(exist_ok=True)
    for number in range(10_000):
        try:
            with (log / f"{number:05d}").open("x") as entry:
                entry.write(f"{name} {worker}\\n")
            break
        except FileExistsError:
            continue
    else:
        raise RuntimeError("attempt log full")
    marker = DIR / f"{name}.seen"
    first = not marker.exists()
    marker.touch()
    return first, worker
"""
_FLAKY = """
class TestGroup:
    def test_flaky(self):
        assert not attempt("test_flaky")[0], "flaked"


def test_crash():
    if attempt("test_crash")[0]:
        os._exit(1)


def test_steady():
    attempt("test_steady")
"""
_TWICE = """

@pytest.mark.parametrize("n", [1, 2])
def test_broken(n):
    attempt("test_broken")
    assert False, "broken"


def test_skips_serially():
    if attempt("test_skips_serially")[1] == "serial":
        pytest.skip("not parallel")
    assert False, "parallel"
"""
_BRANCHES = """
import covmod


def test_flaky():
    assert not attempt("test_flaky")[0], "flaked"
    covmod.pick(True)


def test_other():
    covmod.pick(False)
"""
_COVMOD = "def pick(flag):\n    if flag:\n        return 1\n    return 2\n"

_CASES = {
    "collection-error": ("def test_one():\n    attempt('one')\n    assert False\n", ["-n", "2"]),
    "shard-selection": (
        "def test_one():\n    attempt('one')\n",
        [
            "-n",
            "2",
            "-p",
            "ci_pytest_shard",
            "--ci-shard-group",
            "g",
            "--ci-shard-count",
            "1",
            "--ci-shard-index",
            "3",
            "--ci-shard-manifest",
            "m.json",
        ],
    ),
    "class-collection-error": (
        "class TestBad:\n"
        "    @pytest.mark.parametrize('nope', [1])\n"
        "    def test_a(self):\n"
        "        pass\n\n\n"
        "def test_one():\n"
        "    attempt('one')\n",
        ["-n", "2"],
    ),
    "malformed-junit": (
        "import atexit\n\n\n"
        "def test_one():\n"
        "    attempt('one')\n"
        "    atexit.register((DIR / 'junit.xml').write_text, '<broken')\n"
        "    assert False\n",
        [],
    ),
    "missing-junit": ("def test_one():\n    attempt('one')\n    os._exit(1)\n", []),
    "too-many": (
        "@pytest.mark.parametrize('n', range(6))\n"
        "def test_many(n):\n    attempt(f'many{n}')\n    assert False\n",
        ["-n", "2"],
    ),
    "coverage-crash": (
        "def test_crash():\n    attempt('crash')\n    os._exit(1)\n",
        ["-n", "2", "--cov=covmod", "--cov-report="],
    ),
}
_MESSAGES = {
    "collection-error": "collection failed",
    "shard-selection": "not with test failures",
    "class-collection-error": "collection failed: TestBad",
    "malformed-junit": "malformed JUnit report",
    "missing-junit": "no JUnit report",
    "too-many": "6 tests failed, more than 5",
    "coverage-crash": "worker crashed",
}


def _run(
    work: Path,
    test_source: str,
    pytest_args: list[str],
    *wrapper_args: str,
    quarantine: tuple[str, ...] = (),
) -> tuple[int, str, str]:
    """Run the wrapper in *work*; return its exit code, output, and step summary."""
    work.mkdir(exist_ok=True)
    (work / "pytest.ini").write_text("[pytest]\n", encoding="utf-8", newline="\n")
    (work / "tests").mkdir()
    registry = f"QUARANTINE = {tuple((test, 4242) for test in quarantine)!r}\n"
    (work / "tests/flaky_quarantine.py").write_text(registry, encoding="utf-8", newline="\n")
    (work / "covmod.py").write_text(_COVMOD, encoding="utf-8", newline="\n")
    (work / "pkg").mkdir()
    (work / "pkg/test_case.py").write_text(_HEADER + test_source, encoding="utf-8", newline="\n")
    if "collection" in work.name:
        (work / "pkg/test_bad.py").write_text("import missing_module_4412\n", encoding="utf-8")
    env = {
        name: value
        for name, value in os.environ.items()
        if not name.startswith(("PYTEST_", "COVERAGE_", "COV_CORE_"))
    }
    env.update(
        CASE_DIR=str(work),
        PYTHONPATH=os.pathsep.join([str(_SCRIPTS), str(work)]),
        GITHUB_STEP_SUMMARY=str(work / "summary.md"),
        COVERAGE_FILE=str(work / ".coverage.1"),
    )
    command = [
        sys.executable,
        str(_SCRIPTS / "ci_pytest_rerun.py"),
        "--junitxml",
        str(work / "junit.xml"),
        "--basetemp",
        str(work / "base"),
        *wrapper_args,
        "pytest",
        "pkg",
        "-q",
        "--tb=short",
        "-p",
        "no:cacheprovider",
        *pytest_args,
    ]
    result = run_bounded_child(command, cwd=work, env=env)
    summary = work / "summary.md"
    text = summary.read_text(encoding="utf-8") if summary.exists() else ""
    return result.returncode, result.stdout, text


def _log(work: Path) -> dict[str, list[str]]:
    seen: dict[str, list[str]] = {}
    for entry in sorted((work / "log").iterdir()):
        name, worker = entry.read_text(encoding="utf-8").split()
        seen.setdefault(name, []).append(worker)
    return seen


def _children(work: Path, name: str) -> list[str]:
    tree = ElementTree.parse(work / "junit.xml")
    return [c.tag for case in tree.iter("testcase") if case.get("name") == name for c in case]


_BROKEN = ["test_broken[1]", "test_broken[2]"]
_FAILING = [*_BROKEN, "test_skips_serially"]
#: The tests of ``_TWICE`` that each outcome quarantines.
_QUARANTINED = {"quarantined": _FAILING, "one-quarantined": _BROKEN[:1]}


@pytest.mark.parametrize("outcome", ["passes", "fails-twice", "quarantined", "one-quarantined"])
def test_rerun_absorbs_a_flake_and_reports_a_repeated_failure(
    tmp_path: Path, outcome: str
) -> None:
    source = _FLAKY + ("" if outcome == "passes" else _TWICE)
    held = _QUARANTINED.get(outcome, [])
    ids = tuple(f"pkg/test_case.py::{name}" for name in held)
    code, output, summary = _run(tmp_path / "w", source, ["-n", "2"], quarantine=ids)
    work = tmp_path / "w"
    seen = _log(work)
    suite = ElementTree.parse(work / "junit.xml").getroot().find("testsuite")

    assert len(seen["test_steady"]) == 1
    for name in ("test_flaky", "test_crash"):
        assert seen[name][0].startswith("gw")
        assert seen[name][1:] == ["serial"]
    assert "flakyFailure" in _children(work, "test_flaky")
    assert "flakyError" in _children(work, "test_crash")
    assert "Passed on rerun: `pkg/test_case.py::TestGroup::test_flaky`" in summary
    assert "Passed on rerun: `pkg/test_case.py::test_crash`" in summary
    assert code == (0 if outcome in ("passes", "quarantined") else 1)
    if code == 0:
        assert suite is not None
        assert (suite.get("failures"), suite.get("errors")) == ("0", "0")
    if outcome == "passes":
        return
    assert output.count("FAILED pkg/test_case.py::test_broken") == 4
    for name, test_id in ((name, f"pkg/test_case.py::{name}") for name in _FAILING):
        warning = f"Quarantined test failed again: `{test_id}` (owner issue #4242)"
        if name in held:
            assert _children(work, name) == ["quarantinedFailure", "rerunFailure"]
            assert warning in summary and warning in output
            assert f"::warning title=Quarantined test::{test_id} " in output
        else:
            assert _children(work, name) == ["failure", "rerunFailure"]
            assert f"Failed again: `{test_id}`" in summary
    assert "skipped on rerun" in (work / "junit.xml").read_text(encoding="utf-8")


@pytest.mark.parametrize("case", list(_CASES))
def test_rerun_is_refused_when_the_failure_is_not_a_test_result(tmp_path: Path, case: str) -> None:
    source, args = _CASES[case]
    wrapper = (
        ["--coverage-file", str(tmp_path / "w" / ".coverage.1")] if "--cov=covmod" in args else []
    )
    work = tmp_path / "collection" if case == "collection-error" else tmp_path / "w"
    code, _, summary = _run(work, source, args, *wrapper)

    assert code != 0
    assert _MESSAGES[case] in summary
    seen = _log(work) if (work / "log").exists() else {}
    assert all(len(workers) == 1 for workers in seen.values())


@pytest.mark.parametrize("quarantined", [False, True])
def test_a_killed_coverage_rerun_still_reports_the_repeated_failure(
    tmp_path: Path, quarantined: bool
) -> None:
    source = (
        "def test_dies_serially():\n"
        "    if attempt('dies')[1] == 'serial':\n"
        "        os._exit(1)\n"
        "    assert False\n"
    )
    work = tmp_path / "w"
    args = ["-n", "2", "--cov=covmod", "--cov-report="]
    test_id = "pkg/test_case.py::test_dies_serially"
    code, _, summary = _run(
        work,
        source,
        args,
        "--coverage-file",
        str(work / ".coverage.1"),
        quarantine=(test_id,) if quarantined else (),
    )

    assert code != 0
    assert "No readable rerun report" in summary
    label = "Quarantined test failed again" if quarantined else "Failed again"
    assert f"{label}: `{test_id}`" in summary
    assert "could not be combined" in summary
    tag = "quarantinedFailure" if quarantined else "failure"
    assert _children(work, "test_dies_serially") == [tag, "rerunError"]


def test_a_rerun_that_ends_without_test_results_fails_a_quarantined_test(tmp_path: Path) -> None:
    source = (
        "def test_dies_serially():\n"
        "    if attempt('dies')[1] == 'serial':\n"
        "        os._exit(3)\n"
        "    assert False\n"
    )
    test_id = "pkg/test_case.py::test_dies_serially"
    code, _, summary = _run(tmp_path / "w", source, ["-n", "2"], quarantine=(test_id,))

    assert code == 3
    assert f"Quarantined test failed again: `{test_id}`" in summary


def test_coverage_after_a_rerun_matches_a_run_without_failures(tmp_path: Path) -> None:
    data: list[tuple[set[int], set[tuple[int, int]]]] = []
    for label in ("rerun", "clean"):
        work = tmp_path / label
        if label == "clean":
            work.mkdir()
            (work / "test_flaky.seen").write_text("", encoding="utf-8")
        args = ["-n", "2", "--cov=covmod", "--cov-branch", "--cov-report="]
        wrapper = ["--coverage-file", str(work / ".coverage.1")]
        code, _, _ = _run(work, _BRANCHES, args, *wrapper)
        assert code == 0
        store = coverage.CoverageData(basename=str(work / ".coverage.1"))
        store.read()
        (name,) = [f for f in store.measured_files() if f.endswith("covmod.py")]
        data.append((set(store.lines(name) or ()), set(store.arcs(name) or ())))
        assert ("serial" in _log(work)["test_flaky"]) == (label == "rerun")
        assert not Path(f"{work / '.coverage.1'}.rerun").exists()

    assert data[0] == data[1]
