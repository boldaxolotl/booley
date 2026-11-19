import cocotb
import qa_no_such_module_xyz  # noqa: F401


@cocotb.test()
async def test_never_runs(dut):
    pass
