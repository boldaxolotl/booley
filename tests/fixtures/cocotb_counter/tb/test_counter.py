"""E2E fixture cocotb module (plan-cocotb-support Part G).

The three "real" tests (reset/count/overflow) are the happy-path suite
(G8); the deliberately-failing variants back the failure/crash-shape cases
(G9/G11) via their own Targets' tests.toml lists.
"""

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, RisingEdge, Timer
from helpers.util import expected_after


async def _init(dut):
    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())
    dut.en.value = 0
    dut.rst_n.value = 0
    await ClockCycles(dut.clk, 3)
    dut.rst_n.value = 1
    await RisingEdge(dut.clk)


@cocotb.test()
async def test_reset(dut):
    await _init(dut)
    assert int(dut.count.value) == 0, "count must clear on reset"


@cocotb.test()
async def test_count(dut):
    await _init(dut)
    start = int(dut.count.value)
    dut.en.value = 1
    await ClockCycles(dut.clk, 10)
    dut.en.value = 0
    await RisingEdge(dut.clk)
    got = int(dut.count.value)
    want = expected_after(start, 10)
    assert got == want, f"count={got}, expected {want} after 10 enabled cycles"


@cocotb.test()
async def test_overflow(dut):
    """Wraps at 2^WIDTH — stops short of the FD/FE trap values."""
    await _init(dut)
    dut.en.value = 1
    await ClockCycles(dut.clk, 200)
    dut.en.value = 0
    await RisingEdge(dut.clk)
    got = int(dut.count.value)
    want = expected_after(0, 200)
    assert got == want, f"count={got}, expected {want} after 200 cycles"


@cocotb.test()
async def test_fail_assert(dut):
    """Deliberate failure (G9): the failure text must surface per-test."""
    await _init(dut)
    got = int(dut.count.value)
    assert got == 42, f"deliberate failure: count={got}, expected the answer"


@cocotb.test()
async def test_py_exception(dut):
    """Plain Python exception mid-test (G11 crash shape)."""
    await _init(dut)
    raise RuntimeError("fixture: deliberate python exception")


@cocotb.test()
async def test_rtl_fatal(dut):
    """Drives count to 8'hFE so the RTL $fatal fires (G11 crash shape)."""
    await _init(dut)
    dut.en.value = 1
    await ClockCycles(dut.clk, 300)


@cocotb.test()
async def test_hang(dut):
    """Never finishes — the timeout-kill shape (G11)."""
    await _init(dut)
    await Timer(1, unit="sec")
