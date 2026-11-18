# Targets

A **Target** is one named build configuration of your design: which source
files, which top module, which parameters, and which EDA tool. Every Booley
Flow (sim, lint, synth, fpga, mutation) builds from a Target, and the Developer Agent
picks Targets by name instead of inventing its own tool arguments. So every
result traces back to a configuration you can read and rerun.

Targets live in [FuseSoC](https://github.com/olofk/fusesoc) `.core` files.
A Project needs at least one `.core` file. Booley finds them anywhere in the
repository, or in `.booley/cores/` in [Stealth Mode](STEALTH.md). The
`booley-setup` skill writes them during [setup](SETUP.md). Read this page when
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

Renaming a Target also means updating its `tests.toml` section and
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
    flow_options: {tool: yosys, arch: xilinx}  # Edalize requires arch; Booley ignores it
    filesets: [rtl]
    toplevel: dut
```

Points worth knowing in that example:

- **`tags: [tb]`** is how Booley tells testbench files from RTL; reviews and
  change tracking rely on it, so tag every testbench fileset.
- **`file_type: user` with `copyto:`** puts a data file (a `$readmemh` image,
  a vectors file) at the path the testbench opens. Booley places it in the
  simulation's working directory ([`run_cwd`](FLOW-SIM.md#sim-working-directory-flowssimrun_cwd))
  for the run and removes it afterwards. If a *different* file already sits at
  that path, the run fails rather than overwrite it, and Doctor warns ahead of
  time: rename the `copyto` destination or remove the checked-in copy.

### Fileset file types

Set `file_type` on a fileset as the default for its files, or on one file to
override it. Untyped files count as `user`. Spell the types exactly as shown:

| `file_type` | Use for |
| --- | --- |
| `verilogSource` | Verilog RTL and testbenches. |
| `systemVerilogSource` | SystemVerilog RTL and testbenches. |
| `vlt` | Verilator waiver files; see the [Verilator recipe](FLOW-LINT.md#verilator-lint-target). |
| `cSource`, `cppSource` | C/C++ sources of a Verilator simulation harness. |
| `veribleLintRules` | Verible rules file; at most one per Target. |
| `veribleLintWaiver` | Verible waiver files; see the [Verible recipe](FLOW-LINT.md#verible-lint-target). |
| `SDC` | ASIC timing constraints (uppercase); see the [Synthesis recipe](FLOW-SYNTH.md#synthesis-target). |
| `xdc` | Vivado constraints; see the [FPGA recipe](FLOW-FPGA.md#fpga-target). |
| `user` | Firmware, vectors, cocotb Python modules; `copyto:` sets where they land. |

- Give headers (`.vh`, `.svh`) the same type as their sources and mark them
  `is_include_file: true`, so they act as include paths.
- Keep `SDC` and `xdc` files out of `tags: [tb]` filesets.
- Use the unversioned `verilogSource` and `systemVerilogSource`. Coverage and
  build reuse don't recognize version-suffixed names.
- SystemC, UHDM, VHDL, and Tcl filesets have no supported path through the
  built-in Flows; see the [EDA-tool matrix](SUPPORTED-EDA-TOOLS.md).

### What Doctor rejects in a `.core`

Doctor checks the `.core` files your Targets use for anything that would let
the Developer Agent change how its own results are produced:

- **No `hooks:` on an fpga Target.** FPGA builds run a fixed, reviewed tool
  setup. Run an extra step from a `generators:` entry or outside the `.core`.
- **Parameters hold literal values.** A parameter's `datatype` is `bool`,
  `file`, `int`, `real`, or `str`. Booley can't pass an HDL expression such
  as `WIDTH*2` to the tool as a parameter, so write out its value.
- **Scripts live outside the source directories.** A file that a `scripts:`
  or `generators:` entry runs must sit outside the Project's RTL and
  testbench directories, for example in `scripts/`, so the Developer Agent
  does not edit it as part of its design work. While the agent's write Scope
  is unrestricted (`*`), every such script is flagged.

The first two fail Doctor; the script rule is a warning. Each finding names
the change to make.

### Target authoring

Name your own Targets `<axis>_<subject>`: the axis is the Flow that runs the
Target, and the subject says what it covers. Use lowercase letters, digits and
underscores.

| Axis | Flow | Example |
| --- | --- | --- |
| `sim_` | sim | `sim_smoke` |
| `lint_` | lint | `lint_style` |
| `synth_` | synth | `synth_timing` |
| `fpga_` | fpga | `fpga_board` |

- **The axis tells synth from fpga.** Both kinds of Target use FuseSoC's
  `generic` flow, so only the name separates them. A Target without an axis
  counts as fpga when its tool is `vivado`.
- **Always add a subject**, even to the only Target of its kind: `lint_soc`,
  not `lint`. A bare `lint` still runs as a lint Target.
- **Order the subject from coarse to fine**: `sim_smoke`, then
  `sim_smoke_irq`. `booley targets` sorts each core's Targets by name, so they
  appear grouped by Flow, and related Targets sit together.
- **Doctor flags names that break these rules** in the Targets it checks and
  in Stealth Mode cores, and suggests one: `lint_soc` for a bare `lint` in a
  core named `soc`.
- **Vendored upstream cores keep their own names.**

Add a `default:` Target only when another core depends on this one. Booley
doesn't list it as a Target you can run.

Each Flow's page shows a complete Target for that Flow and the
`flow_options` it reads: [Lint Targets](FLOW-LINT.md#lint-targets),
[Synthesis Target](FLOW-SYNTH.md#synthesis-target),
[FPGA Target](FLOW-FPGA.md#fpga-target), and, for Python testbenches,
[Cocotb Targets](FLOW-SIM.md#cocotb-targets-python-testbenches).

### Marking Targets for Doctor

[Doctor](DOCTOR.md) checks only the Targets you mark for it. The mark sits in the `.core`
file, beside the Target:

```yaml
targets:
  lint_core:
    flow: lint
    flow_options:
      tool: verilator
      booley: {doctor: [lint]}
```

The allowed names are `sim`, `lint`, `synth`, and `fpga`. Plain `booley doctor`
dry-runs each marked Target and `booley doctor --deep` runs it. An unmarked
Target still runs whenever you or the agent ask for it.
With an enabled `[flows.fpga]` table, Doctor fails until some Target is marked
`fpga`; set `enabled = false` there if the Project has no FPGA build yet.

## Vendored and multi-core repos

- **Unwanted vendored cores** (say, a bundled SoC's example cores): put an
  empty file named `FUSESOC_IGNORE` in their directory. Booley and FuseSoC skip
  every core under it.
- **Many cores in one repo** (ibex ships 208) need no extra configuration. Many
  cores may each have a Target called `lint` or `sim`; Booley asks you to
  pick only when a bare name is ambiguous, and you then name it as
  `vendor:library:name:version#target`.

<details markdown>
<summary>In stealth mode</summary>

To leave a vendored repository untouched, set `ignore_native_cores = true` and
keep your own cores in `.booley/cores/`: Booley then ignores every other
`.core` file. See [STEALTH.md](STEALTH.md#what-changes).

</details>

---

If a Target misbehaves, start with [Doctor](DOCTOR.md).
