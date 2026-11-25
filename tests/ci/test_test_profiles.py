"""Profile prerequisites and fail-fast pytest controller contracts."""

from __future__ import annotations

import os
import subprocess
import textwrap
from pathlib import Path

import pytest
from tests.subprocess_budget import run_nested_pytest
from tests.time_budget import hang_guard_s

from booley.dev_support import test_profiles as profiles

ROOT = Path(__file__).resolve().parents[2]


def test_native_path_is_checkout_local_and_platform_specific(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BOOLEY_BWAVE_BIN", str(tmp_path / "override"))
    monkeypatch.setattr(profiles.sys, "platform", "win32")
    assert (
        profiles.native_test_binary(tmp_path)
        == tmp_path.resolve() / "crates/bwave/target/debug/bwave.exe"
    )


def test_release_and_override_do_not_satisfy_debug_prerequisite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    release = tmp_path / "crates/bwave/target/release/bwave"
    release.parent.mkdir(parents=True)
    release.write_bytes(b"release")
    monkeypatch.setenv("BOOLEY_BWAVE_BIN", str(release))
    assert profiles.native_test_problem(tmp_path) == "native B-Wave debug binary is missing"


@pytest.mark.parametrize(
    "case", ["directory", "permission", "wrapper", "broken", "timeout", "wrong-version", "healthy"]
)
def test_native_probe_checks_launchability(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    binary = profiles.native_test_binary(tmp_path)
    binary.parent.mkdir(parents=True)
    if case == "directory":
        binary.mkdir()
    else:
        binary.write_bytes(b"#!/usr/bin/python\n" if case == "wrapper" else b"native executable")
    monkeypatch.setattr(profiles.os, "access", lambda *_: case != "permission")
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        if case == "timeout":
            raise subprocess.TimeoutExpired(argv, 5)
        return subprocess.CompletedProcess(
            argv,
            int(case == "broken"),
            "other 1.0" if case == "wrong-version" else "bwave 1.0\n",
            "",
        )

    monkeypatch.setattr(profiles.subprocess, "run", run)
    problem = profiles.native_test_problem(tmp_path)
    if case == "permission" and os.name == "nt":
        assert problem is None
        return
    assert (problem is None) == (case == "healthy")
    if calls:
        argv, kwargs = calls[0]
        assert argv == [str(binary), "--version"]
        assert kwargs["cwd"] == tmp_path.resolve()
        assert kwargs["timeout"] == 5
        assert "shell" not in kwargs


def _run_suite(
    tmp_path: Path,
    options: list[str],
    *,
    preflight_ready: bool = False,
    native_body: str = "pytest.skip('missing prerequisite')",
    seed_fallback: bool = False,
) -> subprocess.CompletedProcess[str]:
    suite = tmp_path / "tests"
    suite.mkdir()
    source = (ROOT / "tests/conftest.py").read_text()
    if preflight_ready:
        source += "\nfrom booley.dev_support import test_profiles\ntest_profiles.native_test_problem = lambda root: None\n"
    if seed_fallback:
        source += (
            "\nfrom booley.runtime import paths\n"
            "_fallback = Path(__file__).parent / 'fallback-bwave'\n"
            "_fallback.write_bytes(b'controlled native fallback')\n"
            "paths._native_bwave_candidates = lambda: [_fallback]\n"
            "from booley.bwave import cli\n"
        )
    (suite / "conftest.py").write_text(source)
    (suite / "test_sample.py").write_text(
        "import pytest\n"
        "def test_python() -> None: pass\n"
        "@pytest.mark.native_bwave\n"
        f"def test_native() -> None:\n{textwrap.indent(native_body, '    ')}\n"
    )
    (tmp_path / "pytest.ini").write_text(
        "[pytest]\nmarkers =\n    native_bwave: native integration\n"
    )
    env = {key: value for key, value in os.environ.items() if not key.startswith("PYTEST_")}
    env["PYTHONPATH"] = os.pathsep.join([str(ROOT / "src"), str(ROOT)])
    return run_nested_pytest(
        [str(suite), "-q", f"--basetemp={tmp_path / 'pytest-temp'}", *options],
        cwd=tmp_path,
        env=env,
    )


_FAILED = ("Full profile requires native test execution", "1 failed, 1 passed")
_FIFO = pytest.ExitCode.TESTS_FAILED if os.name == "posix" else 0
_NONE = pytest.ExitCode.NO_TESTS_COLLECTED
_FULL, _PYTHON = ["--test-profile", "full"], ["--test-profile", "python"]
# case: (options, native preflight ready, native test body, exit code, expected stdout text)
_SELECTION = {
    "unprofiled": ([], False, None, 0, ("1 passed, 1 skipped",)),
    "python": (_PYTHON, False, None, 0, ("1 passed, 1 deselected",)),
    "python-native": ([*_PYTHON, "-m", "native_bwave"], False, None, _NONE, ("2 deselected",)),
    "native-filter": (["-m", "native_bwave"], False, None, 0, ("1 skipped, 1 deselected",)),
    "full-skip": ([*_FULL, "-n", "2"], True, None, pytest.ExitCode.TESTS_FAILED, _FAILED),
    "full-help": ([*_FULL, "--help"], False, None, 0, ("--test-profile",)),
    "xfail": (_FULL, True, "pytest.xfail('expected')", 0, ("1 passed, 1 xfailed",)),
    "fifo": (_FULL, True, "pytest.skip('FIFO requires POSIX')", _FIFO, ()),
}


@pytest.mark.parametrize("case", _SELECTION)
def test_profile_selection_and_preflight(tmp_path, case):
    options, ready, body, code, texts = _SELECTION[case]
    kwargs = {"native_body": body} if body else {}
    result = _run_suite(tmp_path, options, preflight_ready=ready, **kwargs)
    assert result.returncode == code, result.stdout + result.stderr
    assert all(text in result.stdout for text in texts)


def test_full_profile_fails_before_xdist_workers_start(tmp_path: Path) -> None:
    result = _run_suite(tmp_path, ["--test-profile", "full", "-n", "2"])
    assert result.returncode == pytest.ExitCode.USAGE_ERROR
    assert "native B-Wave debug binary is missing" in result.stderr
    assert "cargo build --locked" in result.stderr
    assert "bringing up nodes" not in result.stdout
    assert "passed" not in result.stdout


@pytest.mark.parametrize(
    "options",
    [
        ["-m", "not native_bwave"],
        ["-k", "python"],
        ["--deselect", "tests/test_sample.py::test_native"],
        ["--sw-skip"],
        ["--sw-reset"],
    ],
)
def test_full_profile_rejects_partial_selection(tmp_path: Path, options: list[str]) -> None:
    result = _run_suite(tmp_path, ["--test-profile", "full", *options], preflight_ready=True)
    assert result.returncode == pytest.ExitCode.USAGE_ERROR
    assert "complete tests/ suite without filters" in result.stderr


@pytest.mark.parametrize("consumer", ["runtime", "cached_cli", "child_cli"])
def test_native_test_fixture_refuses_runtime_fallback(tmp_path: Path, consumer: str) -> None:
    bodies = {
        "runtime": "from booley.runtime import paths\nassert paths.native_bwave_binary() is None",
        "cached_cli": (
            "from booley.bwave import cli\n"
            "with pytest.raises(FileNotFoundError, match='native B-Wave debug binary is missing'):\n"
            "    cli.native_bwave_binary()"
        ),
        "child_cli": (
            "import os, subprocess, sys\nfrom pathlib import Path\n"
            'script = "import sys; from pathlib import Path; from booley.runtime import paths; '
            "fallback=Path(sys.argv.pop(1)); paths._native_bwave_candidates=lambda:[fallback]; "
            'from tests.bwave.native_binary import main; main()"\n'
            "result = subprocess.run([sys.executable, '-c', script, "
            "str(Path(__file__).with_name('fallback-bwave')), os.environ['BOOLEY_BWAVE_BIN'], "
            "'query', 'build', 'missing.vcd'], env={**os.environ, 'BOOLEY_CONTAINER': '1'}, "
            f"capture_output=True, text=True, timeout={hang_guard_s()}, check=False)\n"
            "assert result.returncode != 0\n"
            "assert 'native B-Wave debug binary is missing' in result.stderr\n"
            "assert 'building from' not in result.stderr"
        ),
    }
    result = _run_suite(tmp_path, [], native_body=bodies[consumer], seed_fallback=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "2 passed" in result.stdout
