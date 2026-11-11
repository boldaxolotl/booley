"""A traced sim run registers its waveforms as bwave sessions (issue #2578).

Tests run a real Campaign through the faked process edge of ``campaign_boundary``
(so the traces are the evidence files a Campaign records) and then look the
sessions up the way an agent shell does: a separate process that resolves the
bwave session store through its own default temporary directory.
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
from booley.bwave import cli as bwave_cli
from booley.bwave.sessions import _load_sessions
from booley.flows.base import SubprocessResult
from tests.flows.sim.campaign_boundary import CampaignSimRun, Launch, run_campaign_sim

# The child prints the trace a bwave argv resolves to (what ``bwave`` would query).
_RESOLVE_CHILD = (
    "import sys; from booley.bwave import cli; print(cli._resolve_trace(sys.argv[1:]))"
)


@pytest.fixture(autouse=True)
def shell_session_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the in-process store at the default a child with this ``TMPDIR`` resolves."""
    shell_tmp = tmp_path / "shell-tmp"
    shell_tmp.mkdir()
    store = shell_tmp / "bwave" / "bwave_sessions.json"
    monkeypatch.setattr(bwave_cli, "SESSION_FILE", store)
    return store


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
) -> CampaignSimRun:
    """Run *targets* (all of them unless *target* names one) with the fake simulator."""
    return run_campaign_sim(
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
    """Run ``bwave`` trace resolution for *argv* in a fresh process with its own ``TMPDIR``."""
    shell_tmp = str(tmp_path / "shell-tmp")
    env = {
        **os.environ,
        "TMPDIR": shell_tmp,
        "TEMP": shell_tmp,
        "TMP": shell_tmp,
        "PYTHONPATH": str(Path(booley.__file__).resolve().parents[1]),
    }
    return subprocess.run(
        [sys.executable, "-c", _RESOLVE_CHILD, *argv],
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )


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

    assert _load_sessions()["lite"]["trace"] == str(_trace_of(tmp_path, "lite", "b"))


def test_session_takes_the_last_test_run_when_all_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _run(tmp_path, monkeypatch, {"lite": ["a", "b", "c"]})

    assert _load_sessions()["lite"]["trace"] == str(_trace_of(tmp_path, "lite", "c"))


def test_untraced_run_leaves_existing_sessions_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, shell_session_store: Path
):
    _run(tmp_path, monkeypatch, {"lite": ["smoke"]})
    assert "lite" in _load_sessions()
    before = shell_session_store.read_bytes()

    (tmp_path / "again").mkdir()
    _run(tmp_path / "again", monkeypatch, {"lite": ["smoke"]}, traced=False)

    assert shell_session_store.read_bytes() == before
    child = _bwave_resolves(tmp_path, "@lite", "list")
    assert Path(child.stdout.strip()) == _trace_of(tmp_path, "lite", "smoke")


def test_untraced_run_registers_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, shell_session_store: Path
):
    _run(tmp_path, monkeypatch, {"lite": ["smoke"]}, traced=False)

    assert not shell_session_store.parent.exists()


@pytest.mark.parametrize(
    ("failing", "exit_code"), [((), 0), (("smoke",), 1)], ids=["pass", "fail"]
)
def test_registration_failure_keeps_the_verdict(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    failing: tuple[str, ...],
    exit_code: int,
):
    blocker = tmp_path / "blocker"
    blocker.write_text("a file where the store's directory should be", encoding="utf-8")
    monkeypatch.setattr(bwave_cli, "SESSION_FILE", blocker / "bwave_sessions.json")

    with caplog.at_level(logging.WARNING, logger="booley.flows.sim.bwave_registration"):
        run = _run(tmp_path, monkeypatch, {"lite": ["smoke"]}, failing=failing)

    assert run.result.exit_code == exit_code
    assert ("FAIL" if failing else "PASS") in run.result.outcome.report_text
    warnings = [r for r in caplog.records if r.name == "booley.flows.sim.bwave_registration"]
    assert [r.levelno for r in warnings] == [logging.WARNING]
    assert "bwave" in warnings[0].getMessage()
