"""Producer scenario: a batch killed while its third of four tests hangs."""

import cocotb
from cocotb.triggers import Timer


@cocotb.test()
async def test_done(dut):
    await Timer(30, unit="ns")


@cocotb.test()
async def test_bad(dut):
    await Timer(30, unit="ns")
    observed = 1
    assert observed == 0, "deliberate failure: timeout progress sample"


@cocotb.test()
async def test_hang(dut):
    # Never completes: the outer timeout must kill the batch here.
    while True:
        await Timer(1, unit="ms")


@cocotb.test()
async def test_later(dut):
    await Timer(30, unit="ns")
