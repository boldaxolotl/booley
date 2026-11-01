# Supported EDA tools

Terms such as **Booley Flow**, **Target**, and **EDA provisioning** are
defined in the [glossary](../GLOSSARY.md).

**Source language: SystemVerilog/Verilog only.** VHDL is not supported in any
flow — there is no GHDL/NVC Booley Flow, no VHDL lint or synthesis path, and a mixed
repo's VHDL files simply have no Booley Target to point at. If your design is
VHDL-first, Booley has nothing to offer it today; a repo that ships both an SV
and a VHDL implementation (a common pattern) onboards fine — you wire the SV
half and leave the VHDL half alone.

Two axes govern every flow:

- **EDA tool**: the concrete external program a Flow executes. The Target's
  `flow_options.tool` (or `default_tool`) normally selects it and always
  participates in FuseSoC resolution. FPGA implementation is the fixed-backend
  exception: its Target-name axis declares intent, and Booley always executes
  Vivado after rebuilding the resolved inputs into a Vivado EDAM.
- **Provisioning** decides where the installation files originate, never where
  the command runs. `image` means the Sandbox Image supplies the tool. `host`
  means a built-in policy mounts one administrator-registered installation
  read-only into the Sandbox under an exact Project Grant. Every EDA
  process still executes inside that Sandbox; Project configuration cannot
  select a host command, path, arbitrary mount, license destination, or
  execution location.

Every approved installation of a tool mounts at the same fixed read-only path
in the Sandbox (Vivado: `/opt/booley-eda/vivado`); neither the Project nor the
registration can change it.

## Built-in flows

| Booley Flow | EDA tool | Provisioning | Trace | Requirements |
|---|---|---|---|---|
| `sim` (including `--mode elab-only`) | Verilator | image | waveform (enabling/disabling trace forces a Verilator recompile) | ships in the standard image; cocotb testbenches supported |
| `sim` (including `--mode elab-only`) | Icarus Verilog | image | waveform | ships in the standard image; cocotb testbenches supported |
| `lint` | Verilator | image | none | ships in the standard image |
| `lint` | Verible (`verible-verilog-lint`, style/naming rules) | image | none | ships in the standard image |
| `synth` | Yosys (+ OpenROAD in physical mode) | image + setup cache | none | tools ship in the standard image; `booley bootstrap` fetches the pinned Nangate45 liberty/PDK into a host cache mounted read-only at `/opt/pdk` |
| `fpga` | AMD Vivado 2025.2 | host | none | supported on Linux x86-64 under an exact registration and Project Grant; floating FlexNet relay is experimental |

### Vivado host-provisioning policy

Vivado 2025.2 on Linux x86-64 is the first supported host-provisioned EDA
policy. An administrator registers its release root, and Booley mounts that
root read-only at the fixed target `/opt/booley-eda/vivado` inside the Sandbox. The wrapper,
compatibility libraries, locale, image identity, mount, and environment are
host-issued policy rather than Project settings. Vivado itself executes inside
the Sandbox.

The FPGA Flow's portable `compact`, `balanced`, and `max_frequency` profiles
were characterized on this exact lane. They map to supported Vivado run
strategies; `balanced` preserves the vendor defaults without patching generated
Tcl. These names express optimization intent, not guaranteed QoR, and FPGA power is
not currently a normalized metric.

The Project requests host provisioning under `[eda.vivado]`; the administrator
selects the exact Installation Registration in the Grant for one canonical
Project Root with `booley eda`. `booley projects` lists each remembered root and
its Grants, including roots that have since been deleted. Host-provisioned
Vivado is unavailable on Windows, macOS, and non-x86-64 Linux. Windows remains
supported for Booley and EDA tools already provisioned in its Linux Docker
image; a native Windows EDA installation cannot be mounted into a Linux
container and executed there.

The FlexNet relay is experimental: it has not been validated against real
paid-seat checkouts. Don't rely on floating-license behavior yet.

The image ships **cocotb 2.x**; a Project that pins **cocotb 1.x** still works
(Booley detects it and uses `TESTCASE`). If simulation stalls at time zero, see
[TROUBLESHOOTING.md](TROUBLESHOOTING.md#simulation-stalls-at-time-zero-without-results).

A test that needs a non-RTL build step before it can run (a per-case
firmware compile, vector staging) declares it as
`[flows.sim].pre_run_commands`
([FLOW-SIM.md](FLOW-SIM.md#pre-sim-commands-flowssimpre_run_commands)) —
the project's own Makefile runs inside the Sandbox, and Booley keeps the
same Flow contract above it. Simulators outside this matrix are not supported.

> **`synth` is a PPA estimate, not tape-out.** It's a fast
> power/performance/area estimate to optimize RTL against, whatever
> engine backs it (today Yosys + OpenROAD, deliberately pre-layout, ideal-clock,
> setup-only; Genus / Design Compiler may follow). Real tape-out sign-off
> (physical impl, CTS, routing, DRC/LVS, multi-corner STA on a foundry PDK) is
> out of scope for Booley.

### `synth` RTL frontend (sv2v vs slang)

Yosys reads the design through one of two frontends, selected by the synthesis
Target's `flow_options.frontend` (or the `--frontend` override):

| frontend | how RTL enters Yosys | requires |
|---|---|---|
| `sv2v` (default) | sv2v transpiles SystemVerilog → Verilog, then `read_verilog` | sv2v + any Yosys |
| `slang` | Yosys reads SystemVerilog natively via `read_slang` (no transpile); params pass as `-G NAME=VALUE`, defines as `-D`, include dirs as `-I` | **Yosys ≥ 0.67** sandbox image (vendored slang/sv-elab frontend) |

`sv2v` is the default and works on every sandbox image. `slang` skips the
transpile step and handles SystemVerilog constructs sv2v can choke on (complex
interfaces/modports, some casts), but needs a Yosys-0.67-or-newer image; on an
older image `synth` fails fast with a message telling you to switch
frontend or upgrade the image. Both frontends feed the same tech-mapping tail
(dfflibmap → ABC → `stat`) and the same optional OpenROAD physical path, so the
choice affects only RTL frontend processing, not the PPA methodology.

**Which to pick.** Stay on `sv2v` unless it fails. Reach for `slang` when the
design puts **parameterized interfaces on module port lists**, or reads their
parameters hierarchically (`localparam KEEP_W = s_axis_tx.KEEP_W`). sv2v
cannot transpile either, and for such a design `slang` is not a preference but
a requirement.

Neither frontend is strictly more capable, so "try the other one" is a real
move in both directions. Before concluding that a design cannot be synthesized,
check the known [`slang`](TROUBLESHOOTING.md#rtl-simulates-cleanly-but-synth-rejects-it-under-slang)
and [`sv2v`](TROUBLESHOOTING.md#yosys-rejects-transpiled-verilog-after-sv2v-succeeds)
failure signatures and workarounds.

**Assertion behavior differs by frontend.** `sv2v` drops SVA, so a design full
of `assert property` reaches Yosys assertion-free. `slang` instead lowers SVA
into `$check` cells; the flow strips those cells before tech mapping, so they
neither reach ABC nor corrupt the netlist handed to OpenROAD. If your design
guards its assertions behind a define (`NO_ASSERTIONS` and friends), setting it
on the ASIC Target is still the cleanest option because it keeps RTL frontend
processing cheap.

## Versions in the sandbox image

Versions in a fresh `booley-sandbox` image. A project image may add to it, and
`[sandbox].pip_requirements` may override the Python rows (see
[SANDBOX.md](SANDBOX.md#python-dependencies-sandboxpip_requirements)).

| Component | Version |
|---|---|
| Base image | `ubuntu:26.04` |
| Python | 3.14 (Ubuntu) |
| Verilator | v5.052, source commit `ea338be98e1e838d3518809ce8899f85a009963c` |
| Icarus Verilog | v13_0 |
| Yosys | v0.69, built with its bundled `read_slang` frontend (povik/sv-elab on MikePopoloski/slang — a Yosys submodule, so it has no version of its own) |
| sv2v | v0.0.13 |
| OpenROAD | 26Q4 source commit `c4d317e4fa2398b9880920a1411c20258a2175a9` (the exact upstream OCI binary reports `26Q3-2927-gc4d317e4fa`) |
| Verible | v0.0-4296-g0f262651 |
| FuseSoC / Edalize | 2.4.7 / 0.6.8 |
| cocotb | 2.1.0, with `cocotbext-axi` 0.1.28, `cocotbext-uart` 0.1.4, `numpy` 2.5.3 |
| Liberty / PDK | NangateOpenCellLibrary (typical CCS), fetched and SHA-256 verified by `booley bootstrap`, mounted read-only at `/opt/pdk/cell/lib` |

Check what your image actually has rather than trusting the table after an
upgrade:

```bash
booley doctor -v                                  # prints each EDA tool's version line
booley session enter -- verilator --version       # or any other EDA tool
booley session enter -- python -m pip list        # the Python side
```

The RISC-V variant (`booley-sandbox-riscv`) adds xPack RISC-V GCC `15.2.0-1`
and upstream Spike at master snapshot
`609dbe0b9994154833039209fa37151e7c05e9d4` (`1.1.1-dev`) on top of these.
This is upstream Spike, not lowRISC's `ibex-cosim` fork that Ibex's UVM co-simulation
needs. See also [SANDBOX.md](SANDBOX.md#risc-v-toolchain-image-booley-sandbox-riscv).

Planned commercial tools: see
[ROADMAP.md](../internals/ROADMAP.md#commercial-eda-tools).

### Native coverage capabilities

Coverage requires stable Verilator `v5.052`, full commit
`ea338be98e1e838d3518809ce8899f85a009963c`; 5.050 is excluded. Explicit collection
selecting any Icarus Target is an atomic all-Target Preflight error.

| Native record | V1 treatment |
|---|---|
| Line/basic block, branch, expression | Scored for points in the Target RTL closure. |
| Toggle | Directional per-bit points, scored independently. |
| User-authored cover property | Scored native evidence. |
| FSM and covergroup | Retained as unscored evidence when parsed; no V1 threshold. |
| Unknown record class | Retained capability/findings evidence; never silently scored. |
| Incompatible native format | No normalized points; collection error and gated evaluation blocked. |

Capabilities distinguish `reported` (parsed and retained), `absent` (no records),
and `unsupported` (cannot produce or safely interpret). Unscored does not mean
unsupported. Generated-main, declared custom-main, HDL-testbench, and Cocotb
harnesses are supported; every selected test owns its native database and process.
