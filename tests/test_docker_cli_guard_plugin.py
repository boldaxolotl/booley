"""The Docker CLI guard plugin, exercised through a nested pytest session."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from tests.subprocess_budget import run_nested_pytest

_ROOT = Path(__file__).resolve().parents[1]
_HEADER = """\
import contextlib, subprocess, threading
import pytest
from tests.fake_docker_executable import install_fake_docker
def _docker(program="docker"):
    subprocess.run([program, "version"], capture_output=True, check=False)
def _swallow():
    with contextlib.suppress(BaseException):
        _docker()
"""
_REFUSED = """\
def test_reaches_docker():
    _docker()
def test_thread_swallows_the_failure():
    worker = threading.Thread(target=_swallow)
    worker.start()
    worker.join()
def test_swallows_on_the_test_thread():
    _swallow()
"""
# A path with no program: emptying PATH would not hide a ``docker.exe`` in Windows' System32.
_STARTS = """\
def test_starts(tmp_path):
    with pytest.raises(FileNotFoundError):
        _docker(str(tmp_path / "missing" / "docker"))
"""
_FAKED = """\
def test_with_fake(tmp_path, monkeypatch):
    install_fake_docker(tmp_path, monkeypatch, {})
    with pytest.raises(FileNotFoundError):
        _docker(str(tmp_path / "missing" / "docker"))
"""


def _run(root: Path, modules: dict[str, str]) -> subprocess.CompletedProcess[str]:
    """Run *modules* under ``root/tests`` in a private session that loads the guard."""
    (root / "pytest.ini").write_bytes(b"[pytest]\nmarkers = docker_cli\n")
    for name, body in modules.items():
        module = root / "tests" / name
        module.parent.mkdir(parents=True, exist_ok=True)
        module.write_bytes((_HEADER + body).encode())
    env = {name: value for name, value in os.environ.items() if not name.startswith("PYTEST_")}
    env["PYTHONPATH"] = os.pathsep.join((str(_ROOT), str(_ROOT / "src")))
    env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    plugins = ["-p", "tests.docker_cli_guard_plugin", "-p", "no:cacheprovider"]
    arguments = ["tests", "-rA", *plugins, f"--basetemp={root / 'inner'}"]
    return run_nested_pytest(arguments, cwd=root, env=env)


def test_guarded_docker_calls_fail_at_once_or_at_teardown(tmp_path: Path) -> None:
    result = _run(tmp_path, {"test_case.py": _REFUSED})
    output = result.stdout + result.stderr

    assert "1 failed, 2 passed, 2 errors" in output, output
    assert "FAILED tests/test_case.py::test_reaches_docker" in output, output
    assert "test reached the Docker CLI: docker version" in output, output
    assert "mark the test docker_cli" in output, output
    assert "ERROR at teardown of test_thread_swallows_the_failure" in output, output
    assert "ERROR at teardown of test_swallows_on_the_test_thread" in output, output
    assert "was swallowed" in output and "['docker version']" in output, output


def test_marked_faked_and_suite_tests_are_not_guarded(tmp_path: Path) -> None:
    modules = {
        "test_case.py": "import pytest\n@pytest.mark.docker_cli\n" + _STARTS + _FAKED,
        "docker/test_docker_suite.py": _STARTS,
        "smoke/test_smoke_suite.py": _STARTS,
    }
    result = _run(tmp_path, modules)

    assert result.returncode == 0 and "4 passed" in result.stdout, result.stdout + result.stderr


def test_the_repository_suite_loads_the_guard(pytestconfig: pytest.Config) -> None:
    assert pytestconfig.pluginmanager.has_plugin("tests.docker_cli_guard_plugin")
