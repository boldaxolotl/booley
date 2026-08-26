# Cocotb Testbench Style Guide

Canonical style guide for cocotb (Python) testbenches used by **Cocotb
Targets**: a sim Target whose `.core` flow options declare a
`cocotb_module`. The Target's `toplevel` is whatever the testbench attaches
to — the DUT itself for a simple design, with no HDL wrapper; or a thin HDL
wrapper that instantiates the DUT when its ports are SystemVerilog interfaces,
since cocotb's bus interfaces bind to interface *instances* and something has
to instantiate them. Either way the verdict comes from cocotb's `results.xml`,
never from printed sentinels.

Project-specific overlays may be supplied by the caller or ticket context. The
SystemVerilog `tb_style_guide.md` does not apply to Python testbenches.

---

## 1. Module Layout

One test module per Target (`flow_options.cocotb_module`), staged into the
build root via `copyto`. One Booley test = one named `@cocotb.test()` function;
`tests.toml` lists exactly those function names.

```python
# tb/test_counter.py
import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, RisingEdge

from helpers.model import expected_after  # multi-file helpers: a package dir
# staged beside the module (copyto)


async def init(dut):
    """Shared bring-up: clock, reset, defaults."""
    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())
    dut.en.value = 0
    dut.rst_n.value = 0
    await ClockCycles(dut.clk, 3)
    dut.rst_n.value = 1
    await RisingEdge(dut.clk)


@cocotb.test()
async def test_reset(dut):
    """One behavior per test: reset clears the counter."""
    await init(dut)
    assert int(dut.count.value) == 0
```

- **One test per behavior.** Small, named `@cocotb.test()` coroutines — the
  test name is the report unit, the selection unit (`--test`), and the
  criterion evidence. No mega-test that checks everything.
- **`async def` + `@cocotb.test()` only.** No `TestFactory` in v1 — generated
  test names drift from `tests.toml` and break selection.
- **The `dut` handle IS the toplevel.** Signals are `dut.<port>`; internal
  hierarchy is `dut.<instance>.<signal>`. Keep hierarchy reaches shallow —
  deep pokes couple the TB to implementation detail.

## 2. Verdicts — assert, never print

`results.xml` is the verdict source. A test passes when its coroutine returns,
fails when it raises (an `assert` is the idiomatic raise).

- **No sentinel prints.** `[SIM_RESULT] PASSED` / `ALL TESTS PASSED` strings do
  nothing for a Cocotb Target (sentinels are bypassed) and mislead readers.
- **Assert with context**: `assert got == want, f"count={got}, expected {want}"`
  — the message lands verbatim in the per-test failure report.
- RTL-side `$error`/`$fatal`/SVA failures are still counted from the sim
  output and fail the batch even when every Python test passed — don't
  duplicate RTL assertions in Python.

## 3. Clock, Reset, and Timing

- Start clocks with `cocotb.start_soon(Clock(...).start())` in a shared
  `init()`; never hand-toggle in a loop.
- Reset synchronously and wait a settle edge before driving stimulus (see the
  layout example) so every test starts from a known state — tests share one
  sim process (batched execution) and MUST NOT depend on running order or
  residual state.
- **Await sim-time triggers, never wall-clock sleeps.** `time.sleep()` /
  blocking I/O stalls the whole simulator; use `Timer`, `ClockCycles`,
  `RisingEdge`, `with_timeout`.
- **Bound every wait.** An unbounded `await` on a condition that never comes
  burns the whole wall-clock budget; wrap handshakes in
  `cocotb.triggers.with_timeout(trigger, n, "ns")`.

## 4. Bus Functional Models — use `cocotbext-*`, don't hand-roll

The sandbox image pins a curated set: `cocotb` 2.x, `numpy`, `cocotbext-axi`,
`cocotbext-uart`. Use them instead of hand-rolled drivers/monitors:

```python
from cocotbext.axi import AxiLiteBus, AxiLiteMaster

axil = AxiLiteMaster(AxiLiteBus.from_prefix(dut, "s_axil"), dut.clk, dut.rst)
await axil.write_dword(0x0000, 0x1234_5678)
```

- Booley Flows have no network: a testbench can only import what the image ships or
  the project vendors. **`cocotbext-spi` is not in the image** (no
  cocotb-2.x-compatible release) — vendor an SPI BFM in the project tree when
  needed.
- **Vendored-model layout:** put vendored Python (BFMs, golden models) in the
  TB fileset as `file_type: user` entries with `copyto:` paths that preserve
  the package layout (e.g. `copyto: spi_bfm/__init__.py`); Booley pins the
  build root on `PYTHONPATH`, so the module and its packages import from
  there. Tag the fileset `tags: [tb]`.

## 5. Golden References and Comparison Discipline

Same bar as the SV guide: every stimulus needs a checked expectation.

- Derive expected values from an *independent* model (a Python function, a
  numpy computation, a vendored golden model) — never from the DUT signal
  being checked.
- Compare on every transaction/sample, not only at end-of-test; accumulate and
  assert per item so the failure names the first mismatch.
- Convert handle values explicitly (`int(dut.count.value)`) before comparing —
  `LogicArray` equality with Python ints has resolution pitfalls around
  `X`/`Z`; deciding how `X` should compare is part of the test's contract.

## 6. Tracing

Nothing to add in the testbench: `--trace` works out of the box (the trace
overlay supplies the dump mechanism on Icarus; Verilator uses cocotb's built-in
`--trace` main flags). **Never call `$dumpvars` equivalents or write VCDs from
Python** — the harness owns the trace lifecycle.

## 7. tests.toml Registration

Every `@cocotb.test()` meant to gate a criterion must be listed by exact
function name; a listed name with no matching function comes back
**inconclusive** ("no matching @cocotb.test") rather than passing.

```toml
[sim_cocotb]                 # the .core Target name
tests = ["test_reset", "test_count", "test_overflow"]
# no `select` — cocotb selection is an env-var filter Booley builds
skip = ["test_known_hang"]   # known-hangs, pruned like any Target
```
