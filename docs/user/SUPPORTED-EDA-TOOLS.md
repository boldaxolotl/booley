# Supported EDA tools

This page lists the EDA tools each Booley Flow runs and the versions in the
Sandbox image. Check it before you onboard a design, or after an upgrade to
see which tool versions changed. To run the Flows, see
[Running Flows](FLOW_REFERENCE.md).

**Booley handles Verilog and SystemVerilog only.** No Flow accepts VHDL or
SystemC ([VHDL is on the roadmap](../internals/ROADMAP.md#vhdl-and-ghdl-support)).

## Built-in flows

Each [Target](TARGETS.md) names its EDA tool in `flow_options.tool`, and the
Flow runs that tool. FPGA implementation is the exception: it always runs
Vivado.

Every EDA tool runs inside the [Sandbox](SANDBOX.md). The tool's files come
from one of two places:

- **image**: the tool ships in the Booley Sandbox image. There is nothing to
  install.
- **host**: you install the tool on your machine and Booley mounts it
  read-only into the Sandbox. Only Vivado works this way; see
  [Commercial EDA provisioning](SANDBOX.md#commercial-eda-provisioning).

| Flow | EDA tool (`flow_options.tool`) | Comes from | What it gives you |
|---|---|---|---|
| `sim` | Verilator (`verilator`) | image | Tests, waveforms, cocotb, code coverage |
| `sim` | Icarus Verilog (`icarus`) | image | Tests, waveforms, cocotb; no coverage |
| `lint` | Verilator (`verilator`) | image | Structural and semantic checks |
| `lint` | Verible (`verible`) | image | Style and naming rules |
| `synth` | Yosys, plus OpenROAD for timing (`yosys`) | image | ASIC area and timing estimate on Nangate45 |
| `fpga` | AMD Vivado 2025.2 | host ([setup](SANDBOX.md#commercial-eda-provisioning)) | FPGA utilization and timing; Linux x86-64 hosts only |

**Simulation.** Waveform setup differs per simulator; see
[FLOW-SIM.md](FLOW-SIM.md#waveforms).
Coverage needs Verilator; see [COVERAGE.md](COVERAGE.md#collecting-coverage).
Other simulators, such as Xcelium, VCS, or Questa, are not supported yet; see
the [roadmap](../internals/ROADMAP.md#commercial-eda-tools).

<details markdown>
<summary>With a cocotb testbench</summary>

The image ships cocotb 2.x. A testbench pinned to cocotb 1.x
still runs once you [bake its own pins](SANDBOX.md#baking-the-cocotb-stack-into-the-image).
See [Cocotb Targets](TARGETS.md#cocotb-targets-python-testbenches).

</details>

**Other programs your tests need.** A test that compiles firmware or stages
vectors first runs that step through
[Pre-Sim Commands](FLOW-SIM.md#pre-sim-commands-flowssimpre_run_commands),
inside the Sandbox. The compiler has to be in the image: RISC-V projects can
use the [RISC-V image](SANDBOX.md#risc-v-toolchain-image-booley-sandbox-riscv),
and anything else goes into a
[custom Sandbox image](SANDBOX.md#custom-sandbox-image).

**`synth` is an estimate, not tape-out.** It gives fast area and timing
numbers to optimize RTL against: pre-layout, ideal clocks, setup checks only.
Sign-off (placement and routing, clock-tree synthesis, DRC/LVS, multi-corner
timing on a foundry PDK) is out of scope for Booley. `synth` maps only to
open-source cell libraries (Nangate45 today), so its absolute cell area
will not match your foundry's. For area, use the kGE metric (thousands of
NAND2-gate equivalents): it compares across libraries far better than raw
µm². See [FLOW-SYNTH.md](FLOW-SYNTH.md) for the options.

`booley bootstrap` downloads the Nangate45 cell library that `synth` maps to,
checks every file against a pinned SHA-256, and mounts it read-only into the
Sandbox. Booley does not redistribute it. Its license allows non-commercial
use only and restricts comparisons with other libraries.

## Versions in the sandbox image

These are the versions in a fresh `booley-sandbox` image. A
[custom image](SANDBOX.md#custom-sandbox-image) can add tools, and
[`[sandbox].pip_requirements`](SANDBOX.md#python-dependencies-sandboxpip_requirements)
can replace the Python packages.

| Component | Version |
|---|---|
| Base image | Ubuntu 26.04 |
| Python | 3.14 |
| Verilator | v5.054 (commit `bbb60a403932a9f695cb6ff001ceb73de6ad9234`) |
| Icarus Verilog | v13_0 |
| Yosys | v0.70, with its built-in `slang` frontend |
| sv2v | v0.0.13 |
| OpenROAD | 26Q4 (commit `c4d317e4fa2398b9880920a1411c20258a2175a9`; reports itself as `26Q3-2927-gc4d317e4fa`) |
| Verible | v0.0-4298-g085eef05 |
| FuseSoC / Edalize | 2.4.7 / 0.6.8 |
| cocotb | 2.1.0, with `cocotbext-axi` 0.1.28, `cocotbext-uart` 0.1.4, `numpy` 2.5.4 |
| Cell library | Nangate45 typical corner, downloaded by `booley bootstrap` |

The RISC-V image (`booley-sandbox-riscv`) adds the xPack RISC-V GCC
`15.2.0-1` and upstream Spike at commit
`4094d29094b690a7c4ae392c6d71260e5ec30b6b` (`1.1.1-dev`). This is upstream
Spike, not lowRISC's `ibex-cosim` fork that Ibex's UVM co-simulation needs.

To see what your image actually has, run these on the host:

```bash
booley doctor -v                              # host only: version lines from the Sandbox image for Verilator, Yosys, Icarus, sv2v
booley session enter -- verilator --version   # any EDA tool
booley session enter -- python -m pip list    # Python packages
```

Inside the Sandbox, `booley doctor -v` prints no version lines; run the tool
directly (`verilator --version`).

## Missing a tool?

Commercial simulators and synthesis tools are planned; the
[roadmap](../internals/ROADMAP.md#commercial-eda-tools) says which and why
they wait. To ask for a tool or report one that misbehaves, type
`/booley-feedback` in the Developer Agent chat; see
[When Booley itself misbehaves](USAGE.md#when-booley-itself-misbehaves).
