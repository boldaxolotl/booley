"""Producer scenario for the checked-in Cocotb skip result samples."""

import cocotb
import pytest
from cocotb.triggers import Timer


@cocotb.test()
async def test_runtime_skip(dut):
    await Timer(10, unit="ns")
    pytest.skip("feature not present in this configuration")


@cocotb.test()
async def test_runtime_skip_bare(dut):
    await Timer(10, unit="ns")
    pytest.skip()


@cocotb.test()
async def test_runtime_skip_multiline(dut):
    await Timer(10, unit="ns")
    pytest.skip("\nrequires WIDTH=32\nrebuild with WIDTH=32")


@cocotb.test(skip=True)
async def test_decorator_skip(dut):
    raise AssertionError("a skipped test never runs")
