"""Minimal red-capable reproductions for issue #88, finding F-48."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from booley.sim import cocotb_run
from booley.sim.trace_session import TraceSession, _bwave_valid
from tests.conftest import MINIMAL_FST_BYTES


def test_trace_enabled_cocotb_run_rejects_a_store_with_no_readable_hierarchy(
    tmp_path: Path,
    capsys,
) -> None:
    """A traced sim must not pass on a store B-Wave cannot enumerate."""

    def _stream(*_args, **_kwargs):
        (tmp_path / "results.xml").write_text(
            "<testsuites><testsuite>"
            '<testcase name="test_uart" classname="test_uart" time="0.01"/>'
            "</testsuite></testsuites>",
            encoding="utf-8",
        )
        (tmp_path / "trace.fst").write_bytes(MINIMAL_FST_BYTES)
        return ["focused traced sim output\n"], SimpleNamespace(returncode=0), False

    with (
        patch.object(
            cocotb_run,
            "_prepare_invocation",
            return_value=({"COCOTB_TEST_FILTER": "^test_uart\\.test_uart$"}, ["fake-sim"]),
        ),
        patch.object(cocotb_run, "_stream_output", side_effect=_stream),
    ):
        rc = cocotb_run.run_cocotb_sim(
            build_dir=tmp_path,
            eda_tool="verilator",
            cocotb_module="test_uart",
            tests=["test_uart"],
            work_dir=tmp_path,
            vcd=True,
        )

    stdout = capsys.readouterr().out
    trace = tmp_path / "trace.fst"

    # This is the current sim-side gate. It accepts an FST header plus one
    # value-change block without proving that B-Wave can read any hierarchy.
    assert _bwave_valid(trace) is True

    bwave = Path("crates/bwave/target/debug/bwave").resolve()
    probe = subprocess.run(
        [str(bwave), "list", str(trace), "--format", "json"],
        capture_output=True,
        text=True,
        check=False,
    )
    payload = json.loads(probe.stdout) if probe.returncode == 0 and probe.stdout else {}
    assert probe.returncode != 0 or not payload.get("data", {}).get("signals")

    # Desired F-48 contract: a trace-enabled run succeeds only after a cheap
    # probe establishes a top scope and at least one signal.
    assert rc == 1
    assert "TRACE_OK:" not in stdout
    assert TraceSession(tmp_path).find() is None


def test_focused_cocotb_stdout_summarizes_unselected_skips(
    tmp_path: Path,
    capsys,
) -> None:
    """One selected test must not dump the other 79 XML entries to stdout."""
    skipped = "".join(
        f'<testcase name="test_{i:03}" classname="test_uart"><skipped/></testcase>'
        for i in range(1, 80)
    )
    results_xml = tmp_path / "results.xml"
    results_xml.write_text(
        "<testsuites><testsuite>"
        '<testcase name="test_000" classname="test_uart" time="0.01"/>'
        f"{skipped}</testsuite></testsuites>",
        encoding="utf-8",
    )

    combined, passed = cocotb_run._evaluate_verdict(
        "focused sim output",
        returncode=0,
        timed_out=False,
        work_dir=tmp_path,
        results_file=results_xml,
        tests=["test_000"],
    )
    stdout = capsys.readouterr().out

    assert passed is True
    assert "test_079" in results_xml.read_text(encoding="utf-8")
    assert "test_079" not in stdout
    assert "test_079" not in combined
    assert "79 skipped" in stdout
