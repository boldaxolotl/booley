"""Producer scenarios for the checked-in Cocotb result compatibility sample."""

import cocotb
from cocotb.triggers import Timer


@cocotb.test()
async def test_reset(dut):
    await Timer(30, unit="ns")
    assert int(dut.value.value) == 0


@cocotb.test()
async def test_fail(dut):
    await Timer(30, unit="ns")
    observed = 1
    assert observed == 0, "deliberate failure: compatibility sample"


@cocotb.test(skip=True)
async def test_skipped(dut):
    raise AssertionError("skipped test must not execute")
