from os import qa_no_such_name  # noqa: F401

import cocotb


@cocotb.test()
async def test_never_runs(dut):
    pass
