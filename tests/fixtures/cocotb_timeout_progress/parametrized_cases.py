"""Producer scenario: generated test names that contain a dot of their own."""

import cocotb
from cocotb.triggers import Timer


@cocotb.test()
@cocotb.parametrize(gain=[0.5, 1.5])
async def test_gain(dut, gain):
    await Timer(30, unit="ns")
    # Only the second variant never completes, so the batch is killed there.
    while gain > 1:
        await Timer(1, unit="ms")
