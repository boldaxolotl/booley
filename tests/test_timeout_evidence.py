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
    result, evidence = _run_timeout_case(tmp_path, workers, "")

    assert result.returncode == 1, result.stdout + result.stderr
    if workers:
        assert "Not properly terminated" in result.stdout
    reports = list(evidence.glob("*.log"))
    assert len(reports) == 1
    lines = reports[0].read_text(encoding="utf-8").splitlines()
    metadata = json.loads(lines[0])
    assert metadata["nodeid"] == "test_case.py::test_slow"
    assert metadata["timeout_seconds"] == 1
    assert metadata["worker"] == ("gw0" if workers else "controller")
    assert metadata["pid"] > 0
    assert "test_case.py" in "\n".join(lines[1:])


def test_integration_timeout_overrides_unit_budget_without_false_evidence(tmp_path: Path) -> None:
    result, evidence = _run_timeout_case(tmp_path, 1, "@pytest.mark.timeout(10)")

    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 passed" in result.stdout
    assert not list(evidence.glob("*.log"))


def _run_timeout_case(
    tmp_path: Path, workers: int, marker: str
) -> tuple[subprocess.CompletedProcess[str], Path]:
    (tmp_path / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    (tmp_path / "test_case.py").write_text(
        f"import time\nimport pytest\n{marker}\ndef test_slow():\n    time.sleep(2)\n",
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
    (tmp_path / "evidence").write_text("not a directory", encoding="utf-8")

    result, _evidence = _run_timeout_case(tmp_path, 1, "")

    assert result.returncode == 1, result.stdout + result.stderr
    assert "Not properly terminated" in result.stdout
