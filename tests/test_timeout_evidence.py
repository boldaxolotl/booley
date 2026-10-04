"""Exercise timeout diagnostics through real pytest and xdist worker exits."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize("workers", [0, 1])
def test_thread_timeout_retains_evidence_after_process_exit(tmp_path: Path, workers: int) -> None:
    result, evidence = _run_pytest_case(tmp_path, workers, "")

    assert result.returncode == 1, result.stdout + result.stderr
    if workers:
        assert "Not properly terminated" in result.stdout
        exit_report = json.loads((evidence / "worker-exit-gw0.json").read_text())
        assert exit_report["exit_code"] == 1
    reports = list(evidence.glob("timeout-*.log"))
    assert len(reports) == 1
    lines = reports[0].read_text(encoding="utf-8").splitlines()
    metadata = json.loads(lines[0])
    assert metadata["nodeid"] == "test_case.py::test_slow"
    assert metadata["timeout_seconds"] == 1
    assert metadata["worker"] == ("gw0" if workers else "controller")
    assert metadata["pid"] > 0
    assert "test_case.py" in "\n".join(lines[1:])


def test_integration_timeout_overrides_unit_budget_without_false_evidence(tmp_path: Path) -> None:
    result, evidence = _run_pytest_case(tmp_path, 1, "@pytest.mark.timeout(10)")

    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 passed" in result.stdout
    assert not list(evidence.glob("timeout-*.log"))


def _run_pytest_case(
    tmp_path: Path, workers: int, marker: str, body: str = "time.sleep(2)"
) -> tuple[subprocess.CompletedProcess[str], Path]:
    (tmp_path / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    (tmp_path / "test_case.py").write_text(
        f"import time\nimport pytest\n{marker}\ndef test_slow():\n    {body}\n",
        encoding="utf-8",
    )
    evidence = tmp_path / "evidence"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "test_case.py",
            "-q",
            "-p",
            "pytest_timeout",
            "-p",
            "faulthandler",
            "-p",
            "xdist.plugin",
            "-p",
            "timeout_evidence",
            "-n",
            str(workers),
            "--max-worker-restart=0",
            "--timeout=1",
            "--timeout-method=thread",
        ],
        cwd=tmp_path,
        env=_timeout_environment(evidence),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    return result, evidence


def _timeout_environment(evidence: Path) -> dict[str, str]:
    environment = {
        name: value for name, value in os.environ.items() if not name.startswith("PYTEST_")
    }
    environment.update(
        PYTEST_DISABLE_PLUGIN_AUTOLOAD="1",
        PYTHONPATH=str(Path(__file__).parent),
        BOOLEY_PYTEST_EVIDENCE_DIR=str(evidence),
    )
    return environment


def test_failed_evidence_write_still_terminates_worker(tmp_path: Path) -> None:
    body = (
        "import os; from pathlib import Path; "
        "(Path(os.environ['BOOLEY_PYTEST_EVIDENCE_DIR']) / "
        "f'timeout-gw0-{os.getpid()}.log').mkdir(); time.sleep(2)"
    )
    result, _evidence = _run_pytest_case(tmp_path, 1, "", body)

    assert result.returncode == 1, result.stdout + result.stderr
    assert "Not properly terminated" in result.stdout


@pytest.mark.parametrize("workers", [0, 1])
def test_native_crash_retains_fatal_stack(tmp_path: Path, workers: int) -> None:
    result, evidence = _run_pytest_case(
        tmp_path, workers, "@pytest.mark.timeout(10)", _native_crash_body()
    )

    assert result.returncode != 0, result.stdout + result.stderr
    reports = [path for path in evidence.glob("fatal-*.log") if path.stat().st_size]
    assert len(reports) == 1
    stack = reports[0].read_text(encoding="utf-8")
    assert "test_case.py" in stack
    assert not list(evidence.glob("timeout-*.log"))
    if workers:
        exit_report = json.loads((evidence / "worker-exit-gw0.json").read_text())
        assert exit_report["exit_code"] not in (None, 0, 1)


def test_native_crash_during_unconfigure_retains_fatal_stack(tmp_path: Path) -> None:
    (tmp_path / "conftest.py").write_text(
        "import pytest\n@pytest.hookimpl(tryfirst=True)\ndef pytest_unconfigure(config):\n    "
        + _native_crash_body()
        + "\n",
        encoding="utf-8",
    )
    result, evidence = _run_pytest_case(tmp_path, 0, "@pytest.mark.timeout(10)", "pass")

    assert result.returncode != 0, result.stdout + result.stderr
    reports = [path for path in evidence.glob("fatal-*.log") if path.stat().st_size]
    assert len(reports) == 1
    assert "conftest.py" in reports[0].read_text(encoding="utf-8")


def _native_crash_body() -> str:
    # Avoid POSIX core files and Windows Error Reporting delays in crash probes.
    # 0x0002 is SEM_NOGPFAULTERRORBOX (SetErrorMode).
    return (
        "import os; "
        "exec('import resource; resource.setrlimit(resource.RLIMIT_CORE, (0, 0))' "
        "if os.name == 'posix' else ''); "
        "exec('import ctypes; ctypes.windll.kernel32.SetErrorMode(0x0002)' "
        "if os.name == 'nt' else ''); "
        "__import__('ctypes').string_at(0)"
    )
