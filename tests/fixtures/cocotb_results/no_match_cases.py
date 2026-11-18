"""Producer scenario for the checked-in zero-match Cocotb result sample."""

import cocotb
from cocotb.triggers import Timer


@cocotb.test()
@cocotb.parametrize(width=[1, 2])
async def test_param(dut, width):
    await Timer(1, unit="ns")
