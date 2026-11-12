"""cocotb tests for the mutation QA fixture: `full`, `smoke` and `fail`.

Same behavior as `mutation_tb.sv`. Every test first waits for `ack`. The clock
runs freely and there is no watchdog, so a design that never answers hangs
until Booley's time limit. `fail` is registered only by
`faults/tests-baseline-fail.toml`.
"""

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import FallingEdge, RisingEdge, Timer


async def start(dut):
    """Start the free-running clock, hold reset and run the handshake."""
    cocotb.start_soon(Clock(dut.clk, 2, unit="ns").start())
    dut.rst.value = 1
    for name in ("a", "b", "op", "sh_d", "sh_n", "en", "v", "wide", "req"):
        getattr(dut, name).value = 0
    dut.req.value = 1
    while str(dut.ack.value) != "1":
        await RisingEdge(dut.clk)
    dut.req.value = 0
    while str(dut.ack.value) != "0":
        await RisingEdge(dut.clk)


@cocotb.test()
async def full(dut):
    await start(dut)
    for i in range(1024):
        op, a, b = i >> 8, (i >> 4) & 15, i & 15
        dut.op.value, dut.a.value, dut.b.value = op, a, b
        await Timer(1, unit="ns")
        assert int(dut.y.value) == ((a & b) if op & 1 else (a + b) & 15), "alu y"
        assert int(dut.lt.value) == int(a < b), "alu lt"
        assert int(dut.eq.value) == int(a == b), "alu eq"
    for i in range(64):
        n, d = i >> 4, i & 15
        dut.sh_n.value, dut.sh_d.value = n, d
        await Timer(1, unit="ns")
        assert int(dut.sh_y.value) == (d << n) & 15, "alu_shift y"
    # Counter: reset value, hold while disabled, count through the wrap.
    for _ in range(2):
        await FallingEdge(dut.clk)
    dut.rst.value = 0
    assert int(dut.q.value) == 2, "counter reset value"
    for _ in range(3):
        await FallingEdge(dut.clk)
    assert int(dut.q.value) == 2, "counter holds while disabled"
    dut.en.value = 1
    for i in range(1, 19):
        await FallingEdge(dut.clk)
        assert int(dut.q.value) == (2 + i) & 15, "counter counts"
    dut.en.value = 0
    for _ in range(2):
        await FallingEdge(dut.clk)
    assert int(dut.q.value) == 4, "counter holds after counting"
    for i in range(256):
        dut.wide.value = i
        await FallingEdge(dut.clk)
        assert int(dut.hi.value) == (i >> 7) & 1, "decoys hi"
        assert int(dut.low.value) == i & 15, "decoys low"
        assert int(dut.r.value) == (i >> 7) & 1, "decoys r"


@cocotb.test()
async def smoke(dut):
    await start(dut)
    # Drive a few inputs and read `busy`, which nothing checks.
    dut.op.value, dut.a.value, dut.b.value, dut.v.value = 2, 10, 5, 9
    for _ in range(4):
        await FallingEdge(dut.clk)
    dut.rst.value = 0
    dut.en.value = 1
    for _ in range(4):
        await FallingEdge(dut.clk)
    _ = dut.busy.value


@cocotb.test()
async def fail(dut):
    await start(dut)
    raise AssertionError("the fail test always fails")
