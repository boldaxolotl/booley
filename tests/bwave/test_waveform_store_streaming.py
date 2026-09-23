"""Real-native tests for B-Wave-owned FIFO conversion."""

from __future__ import annotations

import os
import subprocess
import textwrap

import pytest

from booley.bwave.waveform_store import (
    inspect_store,
    native_bwave_binary,
    start_streaming_conversion,
)
from booley.flows.sim.trace_session import TraceSession

pytestmark = pytest.mark.skipif(os.name != "posix", reason="FIFO requires POSIX")

MINIMAL_VCD = textwrap.dedent("""\
    $timescale 1ns $end
    $scope module top $end
    $var wire 1 ! clk $end
    $upscope $end
    $enddefinitions $end
    #0
    0!
    #5
    1!
""")


def _require_binary() -> None:
    if native_bwave_binary() is None:
        pytest.skip("native B-Wave binary not built")


def _write_fifo(path, data: bytes) -> None:
    descriptor = os.open(str(path), os.O_WRONLY)
    try:
        os.write(descriptor, data)
    finally:
        os.close(descriptor)


def test_streaming_conversion_produces_queryable_store(tmp_path) -> None:
    _require_binary()
    fifo = tmp_path / "trace.fifo"
    store = tmp_path / "trace.fst"
    conversion = start_streaming_conversion(
        fifo,
        store,
        stderr_path=tmp_path / "trace.fst.stderr",
    )
    assert conversion is not None

    _write_fifo(fifo, MINIMAL_VCD.encode())
    result = conversion.finish()

    assert result.success is True
    assert not fifo.exists()
    assert inspect_store(store).queryable is True


def test_trace_session_streams_into_its_own_cache_destination(tmp_path) -> None:
    _require_binary()
    work = tmp_path / "sim"
    work.mkdir()
    session = TraceSession(work)
    conversion = session.start_fifo()
    assert conversion is not None
    assert conversion.store_path == session.bwave_path

    _write_fifo(session.fifo_path, MINIMAL_VCD.encode())
    session.cleanup_fifo(conversion)

    assert session.find() == session.work_bwave_path
    probe = subprocess.run(
        [native_bwave_binary(), "signal", str(session.work_bwave_path), "-s", "*"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert probe.returncode == 0, probe.stderr
