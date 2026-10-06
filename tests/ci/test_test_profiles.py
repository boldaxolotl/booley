"""Profile prerequisites and fail-fast pytest controller contracts."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from booley.dev_support import agent_readiness as readiness
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


def test_full_readiness_blocks_and_offers_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(readiness, "native_test_problem", lambda root: "missing")
    monkeypatch.setattr(readiness.shutil, "which", lambda name: "cargo")
    check = readiness._native_test_check(tmp_path)
    assert check.status is readiness.Status.BLOCKED
    assert check.required
    assert check.commands[0].argv == profiles.BWAVE_BUILD
    commands = readiness._verification_commands(Path(sys.executable), tmp_path, "full", False)
    assert all(command.purpose != "test" for command in commands)


def test_full_readiness_advertises_guarded_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(readiness, "native_test_problem", lambda root: None)
    assert readiness._native_test_check(tmp_path).status is readiness.Status.READY
    command = readiness._verification_commands(Path(sys.executable), tmp_path, "full")[-1]
    assert command.id == "pytest.broad"
    assert command.required_at == "optional-broad-verification"
    assert command.argv[4:6] == ("--test-profile", "full")


@pytest.mark.parametrize("phase", ["prepare", "publish"])
def test_profile_is_develop_only(phase: str) -> None:
    argv = ["--phase", phase, "--test-profile", "full"]
    if phase == "prepare":
        argv.extend(["--branch", "codex/example"])
    with pytest.raises(SystemExit):
        readiness._parse_args(argv)


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
        )
    (suite / "conftest.py").write_text(source)
    (suite / "test_sample.py").write_text(
        "import pytest\n"
        "def test_python() -> None: pass\n"
        "@pytest.mark.native_bwave\n"
        f"def test_native() -> None: {native_body}\n"
    )
    (tmp_path / "pytest.ini").write_text(
        "[pytest]\nmarkers =\n    native_bwave: native integration\n"
    )
    env = {key: value for key, value in os.environ.items() if not key.startswith("PYTEST_")}
    env["PYTHONPATH"] = os.pathsep.join([str(ROOT / "src"), str(ROOT)])
    return subprocess.run(
        [sys.executable, "-m", "pytest", str(suite), "-q", *options],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=90,
        check=False,
    )


@pytest.mark.parametrize(
    "options,summary",
    [([], "1 passed, 1 skipped"), (["--test-profile", "python"], "1 passed, 1 deselected")],
)
def test_python_profile_and_unprofiled_pytest_selection(
    tmp_path: Path, options: list[str], summary: str
) -> None:
    result = _run_suite(tmp_path, options)
    assert result.returncode == 0, result.stdout + result.stderr
    assert summary in result.stdout


def test_full_profile_fails_before_xdist_workers_start(tmp_path: Path) -> None:
    result = _run_suite(tmp_path, ["--test-profile", "full", "-n", "2"])
    assert result.returncode == pytest.ExitCode.USAGE_ERROR
    assert "native B-Wave debug binary is missing" in result.stderr
    assert "cargo build --locked" in result.stderr
    assert "bringing up nodes" not in result.stdout
    assert "passed" not in result.stdout


def test_full_profile_rejects_missing_prerequisite_skips_in_workers(tmp_path: Path) -> None:
    result = _run_suite(tmp_path, ["--test-profile", "full", "-n", "2"], preflight_ready=True)
    assert result.returncode == pytest.ExitCode.TESTS_FAILED, result.stdout + result.stderr
    assert "Full profile requires native test execution" in result.stdout
    assert "1 failed, 1 passed" in result.stdout


def test_explicit_python_profile_combines_existing_marker_filter(tmp_path: Path) -> None:
    result = _run_suite(tmp_path, ["--test-profile", "python", "-m", "native_bwave"])
    assert result.returncode == pytest.ExitCode.NO_TESTS_COLLECTED
    assert "2 deselected" in result.stdout


def test_unprofiled_native_ci_filter_keeps_native_selection(tmp_path: Path) -> None:
    result = _run_suite(tmp_path, ["-m", "native_bwave"])
    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 skipped, 1 deselected" in result.stdout


def test_missing_cargo_requires_toolchain_before_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(readiness, "native_test_problem", lambda root: "missing")
    monkeypatch.setattr(readiness.shutil, "which", lambda name: None)
    check = readiness._native_test_check(tmp_path)
    assert check.status is readiness.Status.BLOCKED
    assert "install Rust/Cargo" in check.summary
    assert not check.commands


def test_full_profile_help_does_not_require_binary(tmp_path: Path) -> None:
    result = _run_suite(tmp_path, ["--test-profile", "full", "--help"])
    assert result.returncode == 0, result.stdout + result.stderr
    assert "--test-profile" in result.stdout


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


def test_full_profile_preserves_expected_xfail(tmp_path: Path) -> None:
    result = _run_suite(
        tmp_path,
        ["--test-profile", "full"],
        preflight_ready=True,
        native_body="pytest.xfail('expected contract failure')",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 passed, 1 xfailed" in result.stdout


def test_native_test_fixture_refuses_runtime_fallback(tmp_path: Path) -> None:
    body = "__import__('booley.runtime.paths', fromlist=['native_bwave_binary']).native_bwave_binary() is None"
    result = _run_suite(tmp_path, [], native_body=f"assert {body}", seed_fallback=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "2 passed" in result.stdout


def test_full_profile_allows_only_actual_fifo_platform_skip(tmp_path: Path) -> None:
    result = _run_suite(
        tmp_path,
        ["--test-profile", "full"],
        preflight_ready=True,
        native_body="pytest.skip('FIFO requires POSIX')",
    )
    assert result.returncode == (pytest.ExitCode.TESTS_FAILED if os.name == "posix" else 0)
