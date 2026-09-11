"""Two finite, separately attributable coverage tests in the Session Runtime."""

import json
from pathlib import Path

import cocotb
from cocotb.triggers import Timer


async def exercise(dut, choices):
    dut.clk.value = 0
    dut.reset.value = 1
    dut.choice.value = 0
    await Timer(1, unit="ns")
    dut.clk.value = 1
    await Timer(1, unit="ns")
    dut.reset.value = 0
    for choice in choices:
        dut.clk.value = 0
        dut.choice.value = choice
        await Timer(1, unit="ns")
        dut.clk.value = 1
        await Timer(1, unit="ns")
        assert int(dut.decoded.value) == 1 << choice


@cocotb.test()
async def gap(dut):
    await exercise(dut, staged("gap", [0, 1] * 9))


@cocotb.test()
async def full(dut):
    await exercise(dut, staged("full", [0, 1, 2] * 6))


def staged(test, expected):
    vectors = json.loads(Path("qa-cocotb-vectors.json").read_text())
    assert vectors[test] == expected, "Wrong staged per-test vector"
    return vectors[test]
