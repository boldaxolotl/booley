"""Integration tests for the FIFO-based VCD→.fst streaming pipeline.

These tests exercise the real bwave binary through actual FIFOs,
catching deadlock regressions that mocked unit tests cannot.
POSIX-only (FIFOs don't exist on Windows).
"""

from __future__ import annotations

import os
import subprocess
import textwrap

import pytest

# Skip entire module on non-POSIX (Windows)
pytestmark = pytest.mark.skipif(os.name != "posix", reason="FIFO requires POSIX")


def _find_bwave() -> str | None:
    from booley.sim.bwave_fifo import _find_bwave_bin

    return _find_bwave_bin()


def _skip_if_no_bwave():
    if _find_bwave() is None:
        pytest.skip("bwave binary not found")


def _assert_valid_fst_store(bwave_path) -> None:
    """The store is valid iff the bwave binary itself can query it.

    FST has no fixed ASCII magic to sniff (the retired ``.bwave`` container's
    ``BWAV`` bytes are gone — ADR 0041: FST is the waveform store), so
    validity is asserted the way production consumes the file: a real query
    through the same binary that built it.
    """
    assert bwave_path.exists(), f"{bwave_path.name} not created"
    assert bwave_path.stat().st_size >= 16, f"{bwave_path.name} too small"
    result = subprocess.run(
        [_find_bwave(), "signal", str(bwave_path), "-s", "*"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, f"store unreadable by bwave: {result.stderr}"


# Minimal valid VCD that produces an .fst store with at least one transition
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
    #10
    0!
""")


class TestFifoPipelineHappyPath:
    """Start FIFO, write minimal VCD, close keepalive, verify .fst output."""

    def test_produces_valid_fst_store(self, tmp_path):
        _skip_if_no_bwave()
        from booley.sim.bwave_fifo import cleanup_bwave, start_bwave_fifo

        fifo_path = tmp_path / "trace.fifo"
        bwave_path = tmp_path / "trace.fst"
        os.mkfifo(str(fifo_path))

        bwave_proc, use_fifo, keepalive_fd = start_bwave_fifo(
            tmp_path,
            bwave_path,
            trace_scope=None,
        )
        assert use_fifo is True
        assert bwave_proc is not None
        assert keepalive_fd is not None

        # Write VCD through FIFO — open as O_WRONLY so bwave sees data
        wr_fd = os.open(str(fifo_path), os.O_WRONLY)
        os.write(wr_fd, MINIMAL_VCD.encode())
        os.close(wr_fd)

        # Signal EOF to bwave
        os.close(keepalive_fd)

        cleanup_bwave(bwave_proc, bwave_path, fifo_path)

        _assert_valid_fst_store(bwave_path)


class TestTraceSessionFifoRoundTrip:
    """The streamer must write the store its own session later looks for.

    F-24: ``setup_bwave_paths`` re-derived the cache bucket as
    ``_bwave_cache_root() / work_dir.name`` while ``TraceSession.cache_dir``
    appends a path digest, so a healthy FIFO run wrote
    ``/tmp/bwave/sim/trace.fst`` and every reader reported
    ``/tmp/bwave/sim-<digest>/trace.fst: missing``. Driving the real binary
    through the real session is the only way this stays caught.
    """

    def test_streamed_store_is_found_by_its_own_session(self, tmp_path):
        _skip_if_no_bwave()
        from unittest.mock import patch

        from booley.sim.trace_session import TraceSession

        work = tmp_path / "sim"
        work.mkdir()

        with patch(
            "booley.sim.trace_session._bwave_cache_root",
            return_value=tmp_path / "bwave",
        ):
            session = TraceSession(work)
            bwave_proc, use_fifo, keepalive_fd = session.start_fifo()
            assert use_fifo and bwave_proc is not None

            wr_fd = os.open(str(session.fifo_path), os.O_WRONLY)
            os.write(wr_fd, MINIMAL_VCD.encode())
            os.close(wr_fd)

            session.cleanup_fifo(bwave_proc, keepalive_fd)

            assert session.bwave_path.exists(), "streamer wrote outside the session's bucket"
            found = session.find()

        assert found is not None, "session cannot find the store it just streamed"
        _assert_valid_fst_store(found)


class TestFifoEofCleanup:
    """Verify bwave exits cleanly (rc=0) and FIFO is removed."""

    def test_clean_exit_and_fifo_removed(self, tmp_path):
        _skip_if_no_bwave()
        from booley.sim.bwave_fifo import cleanup_bwave, start_bwave_fifo

        fifo_path = tmp_path / "trace.fifo"
        bwave_path = tmp_path / "trace.fst"
        os.mkfifo(str(fifo_path))

        bwave_proc, use_fifo, keepalive_fd = start_bwave_fifo(
            tmp_path,
            bwave_path,
            trace_scope=None,
        )
        assert use_fifo

        wr_fd = os.open(str(fifo_path), os.O_WRONLY)
        os.write(wr_fd, MINIMAL_VCD.encode())
        os.close(wr_fd)
        os.close(keepalive_fd)

        # Wait directly — should exit with rc=0 (no SIGKILL needed)
        rc = bwave_proc.wait(timeout=10)
        assert rc == 0, f"bwave exited with rc={rc}"

        cleanup_bwave(bwave_proc, bwave_path, fifo_path)

        assert not fifo_path.exists(), "FIFO should be deleted after cleanup"


@pytest.mark.slow
class TestFifoTimeoutKill:
    """Never close keepalive → cleanup must SIGKILL bwave."""

    def test_kill_on_timeout(self, tmp_path, monkeypatch):
        _skip_if_no_bwave()
        from booley.sim import bwave_fifo
        from booley.sim.bwave_fifo import cleanup_bwave, start_bwave_fifo

        # Exercise the real FIFO process and kill escalation without paying the
        # 15-second production grace in every suite run.
        monkeypatch.setattr(bwave_fifo, "_BWAVE_EXIT_TIMEOUT_SECONDS", 0.1)

        fifo_path = tmp_path / "trace.fifo"
        bwave_path = tmp_path / "trace.fst"
        os.mkfifo(str(fifo_path))

        bwave_proc, use_fifo, keepalive_fd = start_bwave_fifo(
            tmp_path,
            bwave_path,
            trace_scope=None,
        )
        assert use_fifo

        # Do NOT close keepalive_fd — bwave should hang
        cleanup_bwave(bwave_proc, bwave_path, fifo_path)

        # bwave should be dead now
        assert bwave_proc.poll() is not None, "bwave still alive after cleanup"
        assert not fifo_path.exists(), "FIFO should be deleted after cleanup"

        # Clean up keepalive fd
        os.close(keepalive_fd)


class TestFifoLargeVcd:
    """Write >64KB VCD through FIFO — regression test for pipe buffer deadlock.

    This is the exact scenario from commit 0e10deb: when bwave's stdout
    pipe buffer fills (64KB), the process blocks on write, the FIFO drains,
    and the simulator blocks trying to write more VCD → deadlock.

    The fix was redirecting stdout to DEVNULL; this test ensures it stays fixed.
    """

    def test_large_vcd_no_deadlock(self, tmp_path):
        _skip_if_no_bwave()
        from booley.sim.bwave_fifo import cleanup_bwave, start_bwave_fifo

        fifo_path = tmp_path / "trace.fifo"
        bwave_path = tmp_path / "trace.fst"
        os.mkfifo(str(fifo_path))

        bwave_proc, use_fifo, keepalive_fd = start_bwave_fifo(
            tmp_path,
            bwave_path,
            trace_scope=None,
        )
        assert use_fifo

        # Build a VCD >64KB: header + many transitions
        lines = [
            "$timescale 1ns $end",
            "$scope module top $end",
            "$var wire 8 A data [7:0] $end",
            "$upscope $end",
            "$enddefinitions $end",
            "#0",
            "b00000000 A",
        ]
        for t in range(1, 10_001):
            lines.append(f"#{t * 10}")
            lines.append(f"b{t & 0xFF:08b} A")

        vcd_data = "\n".join(lines).encode()
        assert len(vcd_data) > 64 * 1024, f"VCD too small: {len(vcd_data)} bytes"

        wr_fd = os.open(str(fifo_path), os.O_WRONLY)
        os.write(wr_fd, vcd_data)
        os.close(wr_fd)
        os.close(keepalive_fd)

        cleanup_bwave(bwave_proc, bwave_path, fifo_path)

        _assert_valid_fst_store(bwave_path)
