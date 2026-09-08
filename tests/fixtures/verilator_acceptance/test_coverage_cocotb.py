"""Timed Cocotb driver used by the standalone image acceptance runner."""

import random

import cocotb
from cocotb.triggers import Timer


@cocotb.test()
async def coverage_and_seed(dut):
    random_values = [random.randrange(256) for _ in range(8)]
    dut._log.info("RANDOM=%s", random_values)
    dut.clk.value = 0
    for value in range(8):
        dut.a.value = value % 4
        dut.b.value = 0
        dut.c.value = value
        await Timer(1, unit="ns")
        dut.clk.value = 1
        await Timer(1, unit="ns")
        assert int(dut.hit.value) == (5 if value % 4 == 3 else 0)
        dut.clk.value = 0
