"""A traced sim run registers its waveforms as bwave sessions (issue #2578).

Tests run a real Campaign through the faked process edge of ``simulation_record_boundary``
(so the traces are the evidence files a Campaign records) and then look the
sessions up the way an agent shell does: a separate process in the run's
checkout, with its own temporary directory, that resolves the checkout's bwave
session store (issue #3483).
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from pathlib import Path

import pytest

import booley
from booley.flows.base import SubprocessResult
from booley.flows.sim import bwave_registration
from tests.flows.sim.simulation_record_boundary import (
    Launch,
    SimulationRecordSimRun,
    run_simulation_record_sim,
)
from tests.subprocess_budget import run_bounded_child

# The child prints the trace a bwave argv resolves to (what ``bwave`` would query).
_RESOLVE_CHILD = (
    "import sys; from booley.bwave import cli; print(cli._resolve_trace(sys.argv[1:]))"
)


@pytest.fixture
def checkout_store(tmp_path: Path) -> Path:
    """The session store of the run's checkout (``tmp_path`` is the Project Root)."""
    return tmp_path / ".booley" / ".runtime" / "bwave" / "bwave_sessions.json"


def _sessions(tmp_path: Path) -> dict[str, dict[str, str]]:
    """The checkout store's entries with each trace joined to the checkout when relative."""
    store = tmp_path / ".booley" / ".runtime" / "bwave" / "bwave_sessions.json"
    sessions = json.loads(store.read_text("utf-8"))
    for entry in sessions.values():
        entry["trace"] = str(tmp_path / entry["trace"])
    return sessions


def _simulate(launch: Launch, *, failing: frozenset[str], traced: bool) -> SubprocessResult:
    """The fake simulator: fails the Tests in *failing*; when *traced* it dumps a waveform."""
    failed = bool(failing & set(launch.tests))
    output = f"[SIM_RESULT] {'FAILED' if failed else 'PASSED'}\n"
    if traced:
        dump = launch.cwd / f"{'+'.join(launch.tests)}.fst"
        dump.write_bytes(b"waveform")
        output += f"TRACE_OK: {dump}\n"
    return SubprocessResult(returncode=int(failed), stdout=output, duration_s=0.1)


def _run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    targets: dict[str, list[str]],
    *,
    failing: tuple[str, ...] = (),
    traced: bool = True,
    target: str = "",
) -> SimulationRecordSimRun:
    """Run *targets* (all of them unless *target* names one) with the fake simulator."""
    return run_simulation_record_sim(
        tmp_path,
        monkeypatch,
        targets,
        lambda launch: _simulate(launch, failing=frozenset(failing), traced=traced),
        target=target or ",".join(targets),
        trace=traced,
    )


def _trace_of(tmp_path: Path, target: str, test: str) -> Path:
    """The waveform ``simulation.json`` records for *test* of *target*, resolved."""
    directory = tmp_path / "reports" / "sim" / "1" / "targets" / target
    rows = json.loads((directory / "simulation.json").read_text("utf-8"))["tests"]
    (row,) = [row for row in rows if row["name"] == test]
    return (directory / row["trace"]).resolve()


def _bwave_resolves(tmp_path: Path, *argv: str) -> subprocess.CompletedProcess[str]:
    """Run ``bwave`` trace resolution for *argv* from the checkout, with its own ``TMPDIR``."""
    shell_tmp = tmp_path / "shell-tmp"
    shell_tmp.mkdir(exist_ok=True)
    env = {
        **os.environ,
        "TMPDIR": str(shell_tmp),
        "TEMP": str(shell_tmp),
        "TMP": str(shell_tmp),
        "PYTHONPATH": str(Path(booley.__file__).resolve().parents[1]),
    }
    env.pop("BOOLEY_DIR", None)
    return run_bounded_child([sys.executable, "-c", _RESOLVE_CHILD, *argv], cwd=tmp_path, env=env)


def test_traced_run_registers_the_target_session_for_a_later_bwave_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _run(tmp_path, monkeypatch, {"lite": ["smoke"]})

    child = _bwave_resolves(tmp_path, "@lite", "list")

    assert child.returncode == 0, child.stderr
    assert Path(child.stdout.strip()) == _trace_of(tmp_path, "lite", "smoke")


def test_pathless_bwave_query_resolves_the_runs_newest_trace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _run(tmp_path, monkeypatch, {"lite": ["smoke"], "big": ["smoke"]})

    default = _bwave_resolves(tmp_path, "list")
    first = _bwave_resolves(tmp_path, "@lite", "list")

    assert default.returncode == 0, default.stderr
    # Targets run in request order, so the last one is the newest trace.
    assert Path(default.stdout.strip()) == _trace_of(tmp_path, "big", "smoke")
    assert Path(first.stdout.strip()) == _trace_of(tmp_path, "lite", "smoke")


def test_session_prefers_a_non_passing_test_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _run(tmp_path, monkeypatch, {"lite": ["a", "b", "c"]}, failing=("b",))

    assert _sessions(tmp_path)["lite"]["trace"] == str(_trace_of(tmp_path, "lite", "b"))


def test_session_takes_the_last_test_run_when_all_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _run(tmp_path, monkeypatch, {"lite": ["a", "b", "c"]})

    assert _sessions(tmp_path)["lite"]["trace"] == str(_trace_of(tmp_path, "lite", "c"))


def test_traced_run_registers_into_its_checkout_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, checkout_store: Path
):
    _run(tmp_path, monkeypatch, {"lite": ["smoke"]})

    assert "lite" in json.loads(checkout_store.read_text("utf-8"))
    # A later process of the checkout finds it whatever its temporary directory is.
    child = _bwave_resolves(tmp_path, "@lite", "list")
    assert child.returncode == 0, child.stderr
    assert Path(child.stdout.strip()) == _trace_of(tmp_path, "lite", "smoke")


def test_untraced_run_leaves_existing_sessions_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, checkout_store: Path
):
    _run(tmp_path, monkeypatch, {"lite": ["smoke"]})
    assert "lite" in _sessions(tmp_path)
    before = checkout_store.read_bytes()

    _run(tmp_path, monkeypatch, {"lite": ["smoke"]}, traced=False)

    assert checkout_store.read_bytes() == before
    child = _bwave_resolves(tmp_path, "@lite", "list")
    assert Path(child.stdout.strip()) == _trace_of(tmp_path, "lite", "smoke")


def test_untraced_run_registers_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, checkout_store: Path
):
    resolved: list[Path] = []
    monkeypatch.setattr(bwave_registration, "session_store", resolved.append)

    _run(tmp_path, monkeypatch, {"lite": ["smoke"]}, traced=False)

    assert (
        resolved == []
    )  # A raise inside registration is swallowed as a warning, so record calls.
    assert not checkout_store.parent.exists()


def test_registration_failure_keeps_the_verdict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
):
    for case, failing, exit_code in (("passing", (), 0), ("failing", ("smoke",), 1)):
        checkout = tmp_path / case
        blocker = checkout / ".booley" / ".runtime" / "bwave"
        blocker.parent.mkdir(parents=True)
        blocker.write_text("a file where the store's directory should be", encoding="utf-8")
        caplog.clear()

        with caplog.at_level(logging.WARNING, logger="booley.flows.sim.bwave_registration"):
            run = _run(checkout, monkeypatch, {"lite": ["smoke"]}, failing=failing)

        assert run.result.exit_code == exit_code
        assert ("FAIL" if failing else "PASS") in run.result.outcome.report_text
        warnings = [r for r in caplog.records if r.name == "booley.flows.sim.bwave_registration"]
        assert [r.levelno for r in warnings] == [logging.WARNING]
        assert "bwave" in warnings[0].getMessage()
