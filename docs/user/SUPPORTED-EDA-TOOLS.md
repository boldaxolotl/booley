# Supported EDA tools

## Read this first

This doc assumes a few Booley terms: **Booley Flow** (Booley's deterministic
end-to-end orchestration), **Target** (a named FuseSoC `.core` build target),
and **EDA provisioning** (where an approved tool installation comes from). If any are new, consult the
glossary in [CONTEXT.md](../CONTEXT.md); the [README](../../README.md) and
[SETUP.md](SETUP.md) give the wider picture. Everything below is the detail
layer on top of that vocabulary.

The single source of truth for which EDA tools Booley drives, with which
provisioning source, trace support, and requirements. When README,
SETUP, or ROADMAP need to state "what's supported," they link here rather than
keeping their own drifting lists.

**Source language: SystemVerilog/Verilog only.** VHDL is not supported in any
flow — there is no GHDL/NVC Booley Flow, no VHDL lint or synthesis path, and a mixed
repo's VHDL files simply have no Booley Target to point at. If your design is
VHDL-first, Booley has nothing to offer it today; a repo that ships both an SV
and a VHDL implementation (a common pattern) onboards fine — you wire the SV
half and leave the VHDL half alone.

Two axes govern every flow:

- **EDA tool**: the concrete external program, declared in the resolved FuseSoC Target's
  `.core` file (its `flow_options.tool`, or the legacy `default_tool` mirror),
  *not* set by config knobs. Every command is built by Booley's one builder,
  the FuseSoC/Edalize path (Edalize generates EDA commands).
- **Provisioning** decides where the installation files originate, never where
  the command runs. `image` means the runtime image supplies the tool. `host`
  means a built-in policy mounts one administrator-registered installation
  read-only into the Session Runtime under an exact Project Grant. Every EDA
  process still executes inside that runtime; Project configuration cannot
  select a host command, path, arbitrary mount, license destination, or
  execution location.

For every host-provisioned EDA tool, the built-in policy owns one canonical
container path. The administrator may register different host installation
paths, but every approved installation of a given tool is mounted at that same
read-only destination inside the Session Runtime. Neither the Project nor the
Installation Registration can configure the destination. This gives wrappers,
Flows, Doctor checks, and future image changes one stable tool layout instead
of making container paths part of Project configuration.

## Built-in flows

| Booley Flow | EDA tool | Provisioning | Trace | Requirements |
|---|---|---|---|---|
| `sim` (including `--elab-only`) | Verilator | image | waveform (enabling/disabling trace forces a Verilator recompile) | ships in the standard image; cocotb testbenches supported |
| `sim` (including `--elab-only`) | Icarus Verilog | image | waveform | ships in the standard image; cocotb testbenches supported |
| `lint` | Verilator | image | none | ships in the standard image |
| `lint` | Verible (`verible-verilog-lint`, style/naming rules) | image | none | ships in the standard image |
| `synth` | Yosys (+ OpenROAD in physical mode) | image + setup cache | none | tools ship in the standard image; `booley init` fetches the pinned Nangate45 liberty/PDK into a host cache mounted read-only at `/opt/pdk` |
| `fpga` | AMD Vivado 2025.2 | host | none | supported on Linux x86-64 under an exact registration and Project Grant; floating FlexNet relay is experimental |

### Vivado host-provisioning policy

Vivado 2025.2 on Linux x86-64 is the first supported host-provisioned EDA
policy. An administrator registers its release root, and Booley mounts that
root read-only at the fixed target `/opt/booley-eda/vivado` inside the Session
Runtime. The wrapper,
compatibility libraries, locale, image identity, mount, and environment are
host-issued policy rather than Project settings. Vivado itself executes inside
the runtime.

The Project requests host provisioning under `[eda.vivado]`; the administrator
selects the exact Installation Registration in the Grant for one canonical
Project root with `booley eda`. Host-provisioned Vivado is unavailable on
Windows, macOS, and non-x86-64 Linux. Windows remains supported for Booley and
EDA tools already provisioned in its Linux Docker image; a native Windows EDA
installation cannot be mounted into a Linux container and executed there.

The optional fixed-destination FlexNet relay is experimental. Its synthetic
forwarding, isolation, failure, and cleanup tests pass, but an
administrator-approved paid-seat checkout, accounting, concurrency, and return
matrix was not available. Do not treat floating-license behavior or its SLA as
validated until that site evidence exists.

The base sandbox image pins **cocotb 2.x**, but the cocotb run-half — the run
stage of the simulate flow that Booley drives after FuseSoC builds the sim —
also speaks the **cocotb 1.x** selection dialect (`TESTCASE` instead of
`COCOTB_TEST_FILTER`, auto-detected via `cocotb-config --version`). So a project
that pins a 1.x stack still routes `sim` through the built-in flow.

Dialect compatibility does not guarantee that every historical cocotb release
works with the image's current simulator. If simulation stalls at time zero,
see [TROUBLESHOOTING.md](TROUBLESHOOTING.md#simulation-stalls-at-time-zero-without-results).

A test that needs a non-RTL build step before it can run (a per-case
firmware compile, vector staging) declares it as
`[flows.sim].pre_run_commands`
([CONFIG.md](CONFIG.md#pre-run-commands-flowssimpre_run_commands)) —
the project's own Makefile runs inside the Session Runtime, and Booley keeps the
same Flow contract above it. A simulator outside this matrix is out of scope
for Ticket Mode; widening the matrix is the sanctioned extension axis.

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

Everything below is pinned in `src/booley/data/docker/Dockerfile` (the `ARG`
lines are the source of truth). This table is what a fresh `booley-sandbox`
build gives you; a project image may add to it, and `[sandbox].pip_requirements`
may override the Python rows (see
[CONFIG.md](CONFIG.md#python-dependencies-sandboxpip_requirements)).

| Component | Version |
|---|---|
| Base image | `ubuntu:24.04` |
| Python | 3.13 (deadsnakes PPA) |
| Verilator | v5.046 (built from source) |
| Icarus Verilog | v13_0 |
| Yosys | v0.68, built with its bundled `read_slang` frontend (povik/sv-elab on MikePopoloski/slang — a Yosys submodule, so it has no version of its own) |
| sv2v | v0.0.13 |
| OpenROAD | 2.0-17598-ga008522d8 (Precision-Innovations release 2024-12-14) |
| Verible | v0.0-4148-g1ea007ec |
| FuseSoC / Edalize | 2.4.6 / 0.6.8 |
| cocotb | 2.0.1, with `cocotbext-axi` 0.1.28, `cocotbext-uart` 0.1.4, `numpy` 2.5.2 |
| Liberty / PDK | NangateOpenCellLibrary (typical CCS), fetched and SHA-256 verified by `booley init`, mounted read-only at `/opt/pdk/cell/lib` |

Check what your image actually has rather than trusting the table after an
upgrade:

```bash
booley doctor -v                                  # prints each EDA tool's version line
booley session enter -- verilator --version       # or any other EDA tool
booley session enter -- python -m pip list        # the Python side
```

The RISC-V variant (`booley-sandbox-riscv`) adds a cross toolchain and Spike on
top of these; see
[CONFIG.md](CONFIG.md#risc-v-toolchain-image-booley-sandbox-riscv).

Future commercial EDA integrations require their own built-in provisioning,
licensing, Doctor, security, and full-Flow evidence before they can join this
matrix. Planned integrations live in
[ROADMAP.md](../internals/ROADMAP.md#commercial-eda-tools).
