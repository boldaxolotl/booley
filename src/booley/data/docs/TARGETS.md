# Targets

A **Target** is one named build configuration of your design: which source
files, which top module, which parameters, and which EDA tool. Every Booley
Flow (sim, lint, synth, fpga) builds from a Target, and the Developer Agent
picks Targets by name instead of inventing its own tool arguments. So every
result traces back to a configuration you can read and rerun.

Targets live in [FuseSoC](https://github.com/olofk/fusesoc) `.core` files.
A Project needs at least one `.core` next to [`booley.toml`](CONFIG.md); the
`booley-setup` skill writes both during setup (host doc, not shipped). Read this page when
you review what setup wrote, add a Target, or fix one that Doctor flags. What
each Target should *verify* (its tests) lives in [`tests.toml`](TESTS.md).

FuseSoC is mandatory. A `.core` is usually a plain restatement of the build
you already have: a file list plus a top module. The one thing it can't
express, a per-test build step such as compiling a test's firmware, goes in
[Pre-Sim Commands](FLOW-SIM.md#pre-sim-commands-flowssimpre_run_commands).

## Adding or changing a Target

1. Ask the agent, for example: "add a Verible style-lint Target for the RTL"
   or "add a sim Target with `WIDTH=32`".
2. The agent edits the `.core` (and `tests.toml` for a sim Target) and runs
   Doctor.
3. Check the result with `booley targets` (the list) or
   `booley targets <name>` (one Target in detail). `--for sim|lint|synth|fpga`
   shows only the Targets that Flow can run.

Renaming a Target also means updating its `tests.toml` section, any Goals, and
anything else that calls it by name; ask the agent to do all of it in one
change.

## Design description (`.core`)

A `.core` file ([FuseSoC CAPI2 format](https://fusesoc.readthedocs.io/en/stable/user/build_system/core_files.html))
groups source files into **filesets**, declares typed **parameters**, and
names the **targets** that combine them:

```yaml
CAPI=2:
name: ::my_project:0
filesets:
  rtl:
    files:
      - rtl/pkg.sv: {file_type: systemVerilogSource}
      - rtl/dut.sv: {file_type: systemVerilogSource}
  tb:
    files:
      - tb/tb_dut.sv: {file_type: systemVerilogSource}
    tags: [tb]                        # required on the testbench fileset
  fw:                                 # non-RTL data the TB reads ($readmemh etc.)
    files:
      - fw/firmware.hex: {file_type: user, copyto: firmware.hex}
parameters:
  WIDTH: {datatype: int, paramtype: vlogparam, default: 16}
targets:
  sim_dut:                            # target name == the --target value
    flow: sim
    flow_options: {tool: verilator}   # verilator | icarus
    filesets: [rtl, tb, fw]
    toplevel: tb_dut                  # a sim target's toplevel is its TB top
    parameters: [WIDTH]
  lint_dut:
    flow: lint
    flow_options: {tool: verilator}
    filesets: [rtl]
    toplevel: dut
  synth_dut:
    flow: generic
    flow_options: {tool: yosys, arch: xilinx}  # arch is ignored by Booley
    filesets: [rtl]
    toplevel: dut
```

Points worth knowing in that example:

- **`tags: [tb]`** is how Booley tells testbench files from RTL; reviews and
  change tracking rely on it, so tag every testbench fileset.
- **Waveform tracing needs no extra source in the `.core`.** The Icarus trace
  overlay supplies the packaged dump module when the Target does not already
  provide one. Existing project-supplied modules remain supported and are
  reused. Verilator relies on the Target's trace-capable C++ simulation
  harness and does not need the dump module.
- **`file_type: user` with `copyto:`** puts a data file (a `$readmemh` image,
  a vectors file) at the path the testbench opens. Booley places it in the
  simulation's working directory ([`run_cwd`](FLOW-SIM.md#sim-working-directory-flowssimrun_cwd))
  for the run and removes it afterwards. If a *different* file already sits at
  that path, the run fails rather than overwrite it, and Doctor warns ahead of
  time: rename the `copyto` destination or remove the checked-in copy.
- **`flow_options.arch`** and other Edalize-only options are ignored by
  Booley's synth Flow, which picks its own PDK; see
  [ASIC synthesis](FLOW-SYNTH.md).

### Fileset file types

Set `file_type` on a fileset as the default for its files, or on one file to
override it. Untyped files count as `user`. Spell the types exactly as shown:

| `file_type` | Use for |
| --- | --- |
| `verilogSource` | Verilog RTL and testbenches. |
| `systemVerilogSource` | SystemVerilog RTL and testbenches. |
| `vlt` | Verilator waiver files; see the [Verilator recipe](#verilator-lint-target). |
| `cSource`, `cppSource` | C/C++ sources of a Verilator simulation harness. |
| `veribleLintRules` | Verible rules file; at most one per Target. |
| `veribleLintWaiver` | Verible waiver files; see the [Verible recipe](#verible-lint-target). |
| `SDC` | ASIC timing constraints (uppercase); see the [Synthesis recipe](#synthesis-target). |
| `xdc` | Vivado constraints; see the [FPGA recipe](#fpga-target). |
| `user` | Firmware, vectors, cocotb Python modules; `copyto:` sets where they land. |

- Give headers (`.vh`, `.svh`) the same type as their sources and mark them
  `is_include_file: true`, so they act as include paths.
- Keep `SDC` and `xdc` files out of `tags: [tb]` filesets.
- Use the unversioned `verilogSource` and `systemVerilogSource`. Coverage and
  build reuse don't recognize version-suffixed names.
- The fpga Flow picks RTL by filename, not declared type: give FPGA RTL a
  lowercase `.v` or `.sv` suffix. Its constraints come only from
  `file_type: xdc` files.
- SystemC, UHDM, VHDL, and Tcl filesets have no supported path through the
  built-in Flows; see the [EDA-tool matrix](SUPPORTED-EDA-TOOLS.md).

### Target authoring

Name your own Targets `<axis>_<subject>`, where the axis is the Flow that
runs them:

| Axis | Flow | Example |
| --- | --- | --- |
| `sim_` | sim | `sim_smoke` |
| `lint_` | lint | `lint_style` |
| `synth_` | synth | `synth_timing` |
| `fpga_` | fpga | `fpga_board` |

The prefix matters for synth and fpga: both use FuseSoC's `generic` flow, so
the `synth_`/`fpga_` prefix decides which Flow runs the Target. Without a
prefix, a Target whose tool is `vivado` counts as fpga. Every Target needs a
subject after the axis, even when it is the only one. A bare `lint` still runs
and keeps its axis, but breaks the convention. For the Targets Doctor runs (and
any in a stealth core), it notes names that break the rule and suggests
`<axis>_<core name>` for a bare axis (`lint_soc` for a core named `soc`).
Vendored upstream cores keep their own names.

Add a `default:` Target only when another core depends on this one. Booley
doesn't list it as a Target you can run.

A lint Target needs `flow: lint` and `flow_options.tool` set to `verilator` or
`verible` (`veriblelint` also works). Other tools don't show up in `booley targets --for lint`.
For Python testbenches, see [Cocotb Targets](#cocotb-targets-python-testbenches).
A synth Target sets `flow: generic` and `tool: yosys`, an fpga Target
`flow: generic` and `tool: vivado`; see the recipes below.

#### Verilator lint Target

To waive Verilator warnings, add a `.vlt` control file with `file_type: vlt`
and include its fileset in the lint Target. The type must be exactly `vlt`:
an untyped `.vlt` file, or a guessed type such as `verilatorConfig`, is
silently ignored. Paths are relative to the `.core` file:

```yaml
CAPI=2:
name: acme:ip:top:1.0
filesets:
  rtl:
    files: [rtl/top.sv]
    file_type: systemVerilogSource
  lint_config:
    files:
      - lint/waivers.vlt: {file_type: vlt}
targets:
  lint_structural:
    flow: lint
    flow_options:
      tool: verilator
      verilator_options: [-Wall]   # optional: more warnings, unrelated to the waiver
    filesets: [rtl, lint_config]
    toplevel: top
```

`lint/waivers.vlt` starts with the control-file directive, then lists what to
waive:

```text
`verilator_config
lint_off -rule WIDTHTRUNC -file "*top.sv"
```

This waives `WIDTHTRUNC` in files ending in `top.sv` (the pattern also matches
FuseSoC's build-tree copy); every other warning stays on. Keep patterns
specific, and narrow further with `-lines` (see Verilator's
[control-file reference](https://verilator.org/guide/latest/control.html)).
Add `lint_config` to other Verilator Targets only where the same waiver
should apply.

#### Verible lint Target

A style Target selects the RTL plus a rules file and, optionally, waivers:

```yaml
CAPI=2:
name: acme:ip:top:1.0
filesets:
  rtl:
    files: [rtl/top.sv]
    file_type: systemVerilogSource
  lint_config:
    files:
      - lint/rules.cfg: {file_type: veribleLintRules}
      - lint/waivers.txt: {file_type: veribleLintWaiver}
targets:
  lint_style:
    flow: lint
    flow_options: {tool: verible}
    filesets: [rtl, lint_config]
    toplevel: top
```

Use at most one rules file; waiver files are optional. For what lint reports,
see [Lint](FLOW-LINT.md).

#### Synthesis Target

A synthesis Target selects the RTL plus an SDC timing-constraints fileset. The
`flow_options` after `arch` are optional and shown with their defaults:

```yaml
CAPI=2:
name: acme:ip:core:1.0
filesets:
  rtl:
    files: [rtl/core.sv]
    file_type: systemVerilogSource
  timing_constraints:
    files:
      - constraints/core.sdc: {file_type: SDC}
targets:
  synth_core:
    flow: generic
    flow_options:
      tool: yosys
      arch: xilinx               # required by Edalize; Booley ignores it
      synth_mode: physical       # physical (default) | logical
      ppa_profile: balanced      # compact | balanced (default) | max_frequency
      flatten: true              # default true
      frontend: sv2v             # sv2v (default) | slang
      # slang_options: [--single-unit]   # raw options for the slang frontend
      # advanced_settings_yosys: {abc_recipe: balanced}   # expert settings nest here
      # advanced_settings_openroad: {utilization_pct: 50}  # physical mode only
    filesets: [rtl, timing_constraints]
    toplevel: core
```

- The SDC fileset (`file_type: SDC`, uppercase) must create a clock and is
  required in physical mode; a `synth_mode: logical` Target needs none. See
  [Timing constraints](FLOW-SYNTH.md#timing-constraints) and
  [Fast logical estimates](FLOW-SYNTH.md#fast-logical-estimates).
- `slang_options` is a non-empty list passed to the `slang` frontend
  unchanged.
- Expert `advanced_settings_yosys` and `advanced_settings_openroad` settings
  are described under
  [Optimization profiles](FLOW-SYNTH.md#optimization-profiles).
- To keep a large RAM from distorting results, swap in a Memory Timing
  Surrogate with a define and a synthesis-only fileset, not a Target option;
  see [Large memories](FLOW-SYNTH.md#large-memories).

#### FPGA Target

An FPGA Target selects the RTL plus an XDC constraints fileset for one part:

```yaml
CAPI=2:
name: acme:soc:top:1.0
filesets:
  rtl:
    files: [rtl/top.sv]
    file_type: systemVerilogSource
  fpga_constraints:
    files:
      - constraints/top.xdc: {file_type: xdc}
targets:
  fpga_soc:
    flow: generic
    flow_options:
      tool: vivado
      part: xc7a100tcsg324-1     # required
      ppa_profile: balanced      # compact | balanced (default) | max_frequency
      out_of_context: false      # default false
    filesets: [rtl, fpga_constraints]
    toplevel: top
```

- The `xdc` fileset is required and is the only constraint source. The XDC
  must `create_clock`; without one the run ends in `ERROR`
  ([Reading results](FLOW-FPGA.md#reading-results)).
- `part` is required. `out_of_context: true` builds a block without I/O
  buffers ([Out-of-context builds](FLOW-FPGA.md#out-of-context-builds)). For
  `ppa_profile`, see [Optimization profiles](FLOW-FPGA.md#optimization-profiles).
- Keep `tool: vivado`; other `flow_options`, such as `synth` or `pnr`, do not
  change the build. An unprefixed vendored Target still counts as fpga when
  its tool is `vivado`.

## Cocotb Targets (Python testbenches)

A **Cocotb Target** is a sim Target whose testbench is a
[cocotb](https://www.cocotb.org/) Python module instead of HDL. Setting
`cocotb_module` in the Target's flow options is all it takes:

```yaml
filesets:
  rtl:
    files:
      - rtl/counter.sv: {file_type: systemVerilogSource}
  tb:
    files:
      - tb/test_counter.py: {file_type: user, copyto: test_counter.py}
      # multi-file TBs: copyto keeps the package layout
      - tb/helpers/util.py: {file_type: user, copyto: helpers/util.py}
    tags: [tb]                       # required, as for an SV testbench

targets:
  sim_cocotb:
    flow: sim
    flow_options:
      tool: icarus                   # icarus | verilator
      cocotb_module: test_counter    # this makes it a Cocotb Target
      iverilog_options: [-g2012]     # SystemVerilog sources need -g2012
      timescale: 1ns/1ps
    filesets: [rtl, tb]
    toplevel: counter                # what the Python TB attaches to
```

```toml
# .booley/tests.toml
[sim_cocotb]
tests = ["test_reset", "test_basic"]   # the @cocotb.test() function names
```

How it differs from an SV Target:

- **`toplevel` is the module the Python testbench drives.** Usually that is
  the DUT itself. A DUT with SystemVerilog interface ports needs a thin HDL
  wrapper that instantiates the interfaces and the DUT, because cocotb's bus
  models bind to interface instances; then the wrapper is the `toplevel`.
- **`tests.toml` lists cocotb test-function names.** Tests generated by
  `TestFactory`, `generate_tests`, or `@cocotb.parametrize` are listed by their
  rendered names (for example `run_test_tx/ifg=12`). Doctor can't check these
  names ahead of time; a wrong one shows up when the test runs.
- **No `select` or `seed` template in `tests.toml`.** Booley picks tests and
  seeds through cocotb's own environment variables; a `select` is a setup
  error. Test Variants carry `env` only. See [Seeds](TESTS.md#seeds-seed-default_seed).
- **Pass/fail comes from cocotb's `results.xml`**, not from
  [sentinels](FLOW-SIM.md#passfail-sentinels-and-cycle-counts). A missing
  or truncated `results.xml` is *inconclusive*, never a pass. RTL assertion
  output is still checked.
- **One test module per Target.** A second module is a second Target, and
  build variants (a `-D` set per hardware flavour) multiply that. Rather than
  the full matrix, a common choice is every module on the default variant,
  plus only the modules that exercise the difference on the other variants.
- **Icarus or Verilator only.** Commercial simulators can't run Cocotb Targets.
- **Tracing needs no `.core` setup.** See
  [Tracing a Cocotb Target](FLOW-SIM.md#tracing-a-cocotb-target).

## Flat and vendored repos

- **Flat repos** (one `picorv32.v` at the root) need no `rtl/` and `tb/`
  restructure. A fileset can list root-level files directly
  (`files: [picorv32.v]`); testbench files go in a fileset tagged
  `tags: [tb]`, as usual.
- **Unwanted vendored cores** (say, a bundled SoC's example cores): put an
  empty file named `FUSESOC_IGNORE` in their directory. Booley and FuseSoC skip
  every core under it.
- **Directory symlinks that loop back** to a containing directory (for
  example `src/a/lib/self -> ../../..`) would make core discovery scan the same
  tree over and over, so Doctor fails on them; `booley doctor --verbose` lists
  every affected path. Put a `FUSESOC_IGNORE` in the directory holding the
  link. It hides all cores below it, so don't place it where wanted cores
  live. Commit the marker if you can: an uncommitted one in a subdirectory
  doesn't reach Goal or baseline worktrees.
- **Many cores in one repo** (ibex ships 208) need no extra configuration. Many
  cores may each have a Target called `lint` or `sim`; Booley asks you to
  pick only when a bare name is ambiguous, and you then name it as
  `vendor:library:name:version#target`.
- **Git submodules** work, with one setup step; see [Submodules](#submodules).

<details markdown>
<summary>In stealth mode</summary>

To leave a vendored repository untouched, set `ignore_native_cores = true` and
keep your own cores in `.booley/cores/`: Booley then ignores every other
`.core` file. See STEALTH.md (host doc, not shipped).

</details>

### Submodules

Goal Mode and baseline comparisons run in separate git worktrees. Booley
fills each worktree's submodules from your local copy at the same path, at the
commit the branch pins, without cloning anything, so private SSH URLs are
fine. Recursive submodules work the same way.

Run this once on the host:

```bash
git submodule update --init --recursive
```

<details markdown>
<summary>In stealth mode</summary>

`.booley` is its own Git repository, so initialize its submodules too:

```bash
git -C .booley submodule update --init --recursive
```

</details>

Each submodule must be present, clean, and have its full history (not a
shallow clone) before a Goal or baseline run starts. Otherwise the run stops
with one of:

- `submodule <path> not found in source Project; initialize it first`
- `submodule <path> is dirty`
- `submodule <path> is shallow; fetch its complete history first`
- `local objects for submodule <path> are incomplete`

The last one means your local copy lacks objects for the commit the branch
pins; fetch in that submodule. Booley always uses the pinned commit, even when your checkout
has a newer one.

To copy only some submodules (skip a large docs or board submodule nothing
builds from), list the ones you want in `booley.toml`:

```toml
[submodules]
paths = ["deps/bus_pkg", "deps/axi_verif"]
```

Without `paths`, every top-level submodule is copied; `paths = []` copies
none. Unlisted submodules appear as empty directories in the worktree.
Submodules nested inside a listed one are always copied.

<details markdown>
<summary>In stealth mode</summary>

The list applies to the RTL repository only: every submodule of the `.booley`
repository is always copied.

</details>

---

If a Target misbehaves, start with [Troubleshooting](TROUBLESHOOTING.md) and
Doctor (host doc, not shipped).
