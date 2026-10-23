"""Exercise timeout diagnostics through real pytest and xdist worker exits."""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

# CPython's crash probe bypasses ctypes' Windows SEH wrapper and disables core
# dumps / Windows Error Reporting before dereferencing NULL in native code.
_NATIVE_CRASH = "__import__('faulthandler')._read_null()"
_NATIVE_EXIT_CODES = (
    {0xC0000005}
    if os.name == "nt"
    else {-signal.SIGSEGV, -getattr(signal, "SIGBUS", signal.SIGSEGV)}
)


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


_SPAWN_AND_RECORD_PID = (
    "import subprocess, sys; "
    "proc = subprocess.Popen([sys.executable, '-c', {code!r}], "
    "stdin=subprocess.PIPE, stdout=subprocess.PIPE); "
    "open('child.pid', 'w').write(str(proc.pid))"
)
# Wait for the child to exit without reaping it, so its Popen stays unreaped.
_WAIT_WITHOUT_REAPING = (
    "import _winapi; _winapi.WaitForSingleObject(int(proc._handle), _winapi.INFINITE)"
    if os.name == "nt"
    else "os.waitid(os.P_PID, proc.pid, os.WEXITED | os.WNOWAIT)"
)


def _child_entry(metadata: dict[str, object], evidence_dir: Path) -> dict[str, object]:
    """Return the header's entry for the child PID the nested test recorded."""
    child_pid = int((evidence_dir.parent / "child.pid").read_text())
    assert "children" in metadata, metadata
    entries = [entry for entry in metadata["children"] if entry["pid"] == child_pid]  # type: ignore[attr-defined]
    assert len(entries) == 1, metadata
    return entries[0]


def _read_header(evidence: Path) -> tuple[dict[str, object], list[str]]:
    (report,) = evidence.glob("timeout-*.log")
    lines = report.read_text(encoding="utf-8").splitlines()
    return json.loads(lines[0]), lines[1:]


def test_timeout_evidence_records_live_child(tmp_path: Path) -> None:
    started = datetime.now(UTC).replace(microsecond=0)
    body = (
        _SPAWN_AND_RECORD_PID.format(code="import sys; sys.stdin.read()  # live-child-marker")
        + "; proc.stdout.read()"
    )
    result, evidence = _run_pytest_case(tmp_path, 1, "@pytest.mark.timeout(3)", body)
    finished = datetime.now(UTC).replace(microsecond=0)

    assert result.returncode == 1, result.stdout + result.stderr
    metadata, _stack = _read_header(evidence)
    assert "timed_out_at" in metadata, metadata
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", metadata["timed_out_at"])
    timed_out = datetime.strptime(metadata["timed_out_at"], "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=UTC
    )
    assert started <= timed_out <= finished
    entry = _child_entry(metadata, evidence)
    assert isinstance(entry["args"], list)
    assert "live-child-marker" in entry["args"][-1]
    assert entry["running"] is True
    assert entry["returncode"] is None
    assert 0 <= entry["elapsed_seconds"] <= 30
    assert entry["started_at"].endswith("Z")


def test_timeout_evidence_reports_exited_unreaped_child(tmp_path: Path) -> None:
    body = (
        _SPAWN_AND_RECORD_PID.format(code="raise SystemExit(3)")
        + f"; import os; {_WAIT_WITHOUT_REAPING}; time.sleep(10)"
    )
    result, evidence = _run_pytest_case(tmp_path, 1, "@pytest.mark.timeout(3)", body)

    assert result.returncode == 1, result.stdout + result.stderr
    metadata, _stack = _read_header(evidence)
    entry = _child_entry(metadata, evidence)
    assert entry["running"] is False
    assert entry["returncode"] == 3
    assert "reaped_elsewhere" not in entry


def test_timeout_evidence_collection_error_keeps_stack_and_kill(tmp_path: Path) -> None:
    (tmp_path / "conftest.py").write_text(
        "import timeout_evidence\n"
        "def _boom():\n    raise RuntimeError('collection boom')\n"
        "timeout_evidence._child_evidence = _boom\n",
        encoding="utf-8",
    )
    result, evidence = _run_pytest_case(tmp_path, 1, "")

    assert result.returncode == 1, result.stdout + result.stderr
    assert "Not properly terminated" in result.stdout
    metadata, stack = _read_header(evidence)
    assert {"nodeid", "timeout_seconds", "worker", "pid"} <= metadata.keys()
    assert "children_error" in metadata, metadata
    assert "RuntimeError: collection boom" in metadata["children_error"]
    assert "children" not in metadata
    assert "test_case.py" in "\n".join(stack)


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
        tmp_path, workers, "@pytest.mark.timeout(10)", _NATIVE_CRASH
    )

    assert result.returncode in ({1} if workers else _NATIVE_EXIT_CODES), (
        result.stdout + result.stderr
    )
    reports = [path for path in evidence.glob("fatal-*.log") if path.stat().st_size]
    assert len(reports) == 1
    stack = reports[0].read_text(encoding="utf-8")
    assert "test_case.py" in stack
    assert not list(evidence.glob("timeout-*.log"))
    if workers:
        exit_report = json.loads((evidence / "worker-exit-gw0.json").read_text())
        assert exit_report["exit_code"] in _NATIVE_EXIT_CODES


def test_native_crash_during_unconfigure_retains_fatal_stack(tmp_path: Path) -> None:
    (tmp_path / "conftest.py").write_text(
        "import pytest\n@pytest.hookimpl(tryfirst=True)\ndef pytest_unconfigure(config):\n    "
        + _NATIVE_CRASH
        + "\n",
        encoding="utf-8",
    )
    result, evidence = _run_pytest_case(tmp_path, 0, "@pytest.mark.timeout(10)", "pass")

    assert result.returncode in _NATIVE_EXIT_CODES, result.stdout + result.stderr
    reports = [path for path in evidence.glob("fatal-*.log") if path.stat().st_size]
    assert len(reports) == 1
    assert "conftest.py" in reports[0].read_text(encoding="utf-8")


@pytest.mark.parametrize("workers", [0, 1])
def test_completed_timings_survive_abrupt_pytest_exit(tmp_path: Path, workers: int) -> None:
    (tmp_path / "pytest.ini").write_text("[pytest]\n")
    (tmp_path / "test_case.py").write_text(
        "import os\ndef test_completed():\n    pass\ndef test_terminated():\n    os._exit(17)\n"
    )
    timings = tmp_path / "evidence" / "timings.jsonl"
    environment = _timeout_environment(tmp_path / "evidence")
    environment["BOOLEY_PYTEST_TIMING_FILE"] = str(timings)
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "test_case.py",
            "-q",
            "-p",
            "xdist.plugin",
            "-p",
            "timeout_evidence",
            "-n",
            str(workers),
            "--max-worker-restart=0",
            "--junitxml=result.xml",
        ],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == (17 if workers == 0 else 1), result.stdout + result.stderr
    records = [json.loads(line) for line in timings.read_text().splitlines()]
    completed = [
        record for record in records if record["nodeid"] == "test_case.py::test_completed"
    ]
    assert [record["phase"] for record in completed] == ["setup", "call", "teardown"]
    assert all(record["outcome"] == "passed" for record in completed)
    assert all(record["duration_seconds"] >= 0 for record in records)
    assert all(record["recorded_at"].endswith("Z") for record in records)
    if workers == 0:
        assert not (tmp_path / "result.xml").exists()
