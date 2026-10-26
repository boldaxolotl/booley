# Targets

How Booley learns your design. Every Booley Flow builds from a FuseSoC `.core`
Target, so a `.core` file is the second mandatory piece of a Project next to
[`booley.toml`](CONFIG.md). The `booley-setup` skill writes both; this page
explains what they mean.

## Design description (`.core`)

The design itself is described outside `booley.toml`: *how it builds* lives in
one or more FuseSoC CAPI2 `.core` files ([CAPI2 reference](https://fusesoc.readthedocs.io/en/stable/user/build_system/core_file.html)),
and *what to verify* in `.booley/tests.toml` (see [Simulation tests](TESTS.md)).

> **Is FuseSoC mandatory?** Yes: every Booley Flow builds from a resolvable `.core`
> Target. A `.core` is usually a mechanical restatement of the build
> you already have (a filelist plus a toplevel); for the one thing it can't
> express, a per-test non-RTL build step like compiling a test's firmware,
> declare [Pre-sim commands](FLOW-SIM.md#pre-sim-commands-flowssimpre_run_commands)
> instead of abandoning the flow.

A `.core` describes *how the design builds*: source files (`filesets`), top
modules (`toplevel`), typed parameters/defines, and build **targets**:

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
  sim:                                # target name == the --target value
    flow: sim
    flow_options: {tool: verilator}   # verilator | icarus
    filesets: [rtl, tb, fw]
    toplevel: tb_dut                  # a sim target's toplevel is its TB top
    parameters: [WIDTH]
  lint:
    flow: lint
    flow_options: {tool: verilator}
    filesets: [rtl]
    toplevel: dut
  synth:
    flow: generic
    flow_options: {tool: yosys, arch: xilinx}  # arch is edalize plumbing Booley
    filesets: [rtl]                            # ignores; the OpenROAD engine
    toplevel: dut                              # drives its own PDK/target
```

A few conventions worth calling out in that example:

- **Tracing needs no project-owned helper source.** The Icarus trace overlay
  supplies the packaged dump module when the Target does not already provide one,
  then roots it for the trace run. Existing project-supplied modules remain
  supported and are reused. Verilator relies on the Target's trace-capable C++
  simulation harness and does not need the SystemVerilog dump module.
- **A `file_type: user` file with `copyto:`** stages a non-RTL data file (a
  `$readmemh` image, a vectors file) into the build tree at the name the TB opens.
  Before the simulator starts, Booley exposes that declared relative path in the
  configured `run_cwd`; the temporary entry is removed after the run. An identical
  file already present there is preserved, while a different file at the same path
  is an input-setup error rather than being overwritten.
  Doctor warns about differing declared inputs at the Project Root or a literal
  `run_cwd`, naming the Target, source and destination. Rename the `copyto`
  destination, remove the checked-in copy, or choose a literal `run_cwd` without
  the collision. Identical files and templated `run_cwd` produce no collision
  warning.
  A missing declared `$readmemh` destination is a staging/infrastructure error;
  an undeclared path is a `missing_input` design failure with a fileset hint.
- **`flow_options.arch`** (and any other Edalize-only knob) is plumbing Booley
  passes through to the toolchain. The built-in synth path drives its own
  PDK/target via the OpenROAD engine and ignores `arch`.

### Fileset file types

Set `file_type` on a fileset as the default for its files, or on a file entry
to override it. Booley treats untyped files as `user`.

The built-in Flows consume these types. Spell them exactly as shown:

| `file_type` | Use for |
| --- | --- |
| `verilogSource` | Verilog RTL and testbenches for lint and sim; non-testbench RTL for synth and fpga. |
| `systemVerilogSource` | SystemVerilog RTL and testbenches for the same Flows. |
| `vlt` | Verilator control (waiver) files for lint and Target-based Verilator simulation builds. See the [Verilator recipe](#verilator-lint-target). |
| `cSource`, `cppSource` | C/C++ sources of a Verilator simulation harness. |
| `veribleLintRules` | Verible rules file; select at most one. |
| `veribleLintWaiver` | Verible waiver files; select any number. See the [Verible recipe](#verible-lint-target). |
| `SDC` | ASIC timing constraints for physical synth; the type is uppercase. See [ASIC synthesis](FLOW-SYNTH.md#configuration-flowssynth). |
| `xdc` | Vivado FPGA constraints; name the file `*.xdc`. See [FPGA implementation](FLOW-FPGA.md#configuration-flowsfpga). |
| `user` | Non-RTL inputs such as firmware, vectors, and cocotb Python modules. `copyto:` sets the staged path; see the example above and [Cocotb Targets](#cocotb-targets-python-testbenches). |

- Give headers the same type as their sources and mark them
  `is_include_file: true`. HDL headers (`.vh`/`.svh`) then supply include
  paths instead of compiling separately.
- Keep `SDC` and `xdc` files outside testbench-tagged filesets.
- Use the unversioned names `verilogSource` and `systemVerilogSource`. Some
  consumers accept version-suffixed names, but simulation reuse and coverage
  helpers match the unversioned names exactly.

Every Flow except fpga keeps declared types; for lint and simulation, FuseSoC
passes them to the selected EDA tool. The fpga Flow rebuilds file types from
filenames and selects RTL by suffix alone, so give FPGA RTL a lowercase `.v`
or `.sv` suffix. The `xdc` type is matched case-insensitively, but a file
keeps it only if its name also has an XDC suffix.

Edalize support alone does not make a type an input to every Booley Flow.
These types are recognized only in narrow places:

- `systemCSource`, `uhdm`: the pinned Edalize Verilator node accepts them, but
  that alone does not make SystemC or UHDM execution supported; see the
  [EDA-tool matrix](SUPPORTED-EDA-TOOLS.md).
- `tclSource`: mapped from the `.tcl` suffix, but the built-in synth and FPGA
  recipes have no general Tcl fileset hook.
- `vhdlSource`, `vhdlSource-2008`: classified as sources by simulation
  coverage, but Booley has no supported VHDL simulation path.

### Target authoring

Name project-owned Targets `<axis>_<subject>` using the Booley Flow axis:

| Axis | Driven by | Example |
| --- | --- | --- |
| `sim_` | `sim` | `sim_smoke` |
| `lint_` | `lint` | `lint_style` |
| `synth_` | `synth` | `synth_timing` |
| `fpga_` | `fpga` | `fpga_board` |

The axis is needed because CAPI2 has no synthesis flow: synth and FPGA Targets
can both resolve as `generic`. A bare axis such as `lint` is fine when there is
only one Target for it; the Elaboration Check reuses a sim Target. Targets with an `asic_` prefix run; `booley doctor` recommends `synth_`.

Use `default:` only when another core depends on this one; Booley does not show
it as a selectable Target. Vendored upstream cores keep their original names.
Renaming a Target also requires updating its `tests.toml` section, Doctor
metadata references, explicit callers, and Goals. For Python testbenches, see
[Cocotb Targets](#cocotb-targets-python-testbenches).

Explicit lint Targets require `verilator`, `verible`, or `veriblelint` in
`flow_options.tool`; unsupported or missing EDA tools are excluded from
`booley targets --for lint`, and explicit Doctor lint selections report incompatibility.

#### Verilator lint Target

To waive Verilator warnings, declare a `.vlt` control file with
`file_type: vlt` and select its fileset on the lint Target. The type must be
exactly `vlt`: an untyped `.vlt` file, or a guessed type such as
`verilatorConfig`, does not apply the waiver.

This core sits beside the `rtl/` and `lint/` directories; file paths are
relative to the `.core` file:

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
      verilator_options: [-Wall]
    filesets: [rtl, lint_config]
    toplevel: top
```

`-Wall` is optional: it adds lint warnings to Verilator's default set and is
independent of the waiver.

`lint/waivers.vlt` holds the required control-file directive, then the warning
to waive:

```text
`verilator_config
lint_off -rule WIDTHTRUNC -file "*top.sv"
```

This waives `WIDTHTRUNC` in source paths ending in `top.sv`, including the
FuseSoC build-tree copy; other warning classes stay enabled. Make the pattern
specific to your sources; `-lines` narrows a waiver further (see Verilator's
[control-file reference](https://verilator.org/guide/latest/control.html)).

Run inside the Sandbox:

```bash
booley flow lint --target lint_structural
```

- Edalize places `vlt` files before HDL inputs, so file-specific rules apply
  when those inputs are read.
- Add `lint_config` to other Verilator Targets only where the same waiver is
  intended.
- `--mode elab-only-standalone` probes (see
  [Elaboration Check](FLOW-SIM.md#elaboration-check-flowssim)) use their own HDL-only
  inputs and ignore Target waiver filesets.

#### Verible lint Target

Use canonical `verible` for a style Target that selects RTL, its rules, and optional
waivers:

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

These are existing Project source files at paths relative to the `.core` file,
selected through its filesets. Supply at most one rules file; waiver files are optional.

## Cocotb Targets (Python testbenches)

A **Cocotb Target** is an ordinary sim Target whose testbench is a cocotb Python
module instead of HDL. It needs nothing beyond the built-in flow: declare `cocotb_module` in the
Target's flow options and Booley takes it from there.

```yaml
filesets:
  rtl:
    files:
      - rtl/counter.sv: {file_type: systemVerilogSource}
  tb:
    files:
      - tb/test_counter.py: {file_type: user, copyto: test_counter.py}
      # multi-file TBs: copyto preserves the package layout in the build root
      - tb/helpers/util.py: {file_type: user, copyto: helpers/util.py}
    tags: [tb]                       # required, same as an SV testbench fileset

targets:
  sim_cocotb:
    flow: sim
    flow_options:
      tool: icarus                   # icarus | verilator
      cocotb_module: test_counter    # THIS is what makes it a Cocotb Target
      iverilog_options: [-g2012]     # SystemVerilog sources need -g2012
      timescale: 1ns/1ps
    filesets: [rtl, tb]
    toplevel: counter                # what the Python TB attaches to
```

```toml
# .booley/tests.toml
[sim_cocotb]
tests = ["test_reset", "test_basic"]   # the @cocotb.test() FUNCTION names
# no `select` — see below
```

What differs from an SV Target:

- **`toplevel` is whatever the Python testbench attaches to.** For a simple
  design that is the **DUT itself**: cocotb's `dut` handle *is* the toplevel,
  and there is no HDL wrapper. But a design whose ports are **SystemVerilog
  interfaces** needs one: cocotb's BFMs bind to interface *instances*
  (`AxiStreamBus.from_entity(dut.s_axis_tx)`), and an instance has to be
  instantiated somewhere. Such projects ship a thin HDL wrapper that declares
  the interfaces and instantiates the DUT, and **the wrapper is the
  `toplevel`** (commonly named after the test module, e.g.
  `toplevel: test_taxi_eth_mac_10g` alongside
  `cocotb_module: test_taxi_eth_mac_10g`). Both shapes work as-is; point
  `toplevel` at the module the testbench actually drives.
- **`tests.toml` lists cocotb test-function names**, and must **not** declare a
  `select` template, since selection is an env-var filter Booley builds
  (`COCOTB_TEST_FILTER`, or `TESTCASE` on cocotb 1.x, auto-detected). A `select`
  on a Cocotb Target is a setup-time error. `skip` works unchanged.
- **Seeds and Test Variants.** Booley seeds a Cocotb Target through cocotb's
  own seed variable, so `tests.toml` must not declare a `seed` template for
  it, and its Test Variants carry `env` only. Tests that differ in seed or in
  Variant environment run in separate simulator processes; see
  [Seeds](TESTS.md#seeds-seed-default_seed).
- **No sentinels** (see [pass/fail sentinels](FLOW-SIM.md#simulation--passfail-sentinels-flowssim)):
  a missing or truncated `results.xml` is *inconclusive*, never a pass. RTL
  assertion output is still scanned.
- **One test module per Target.** A cocotb Target names exactly one
  `cocotb_module`, so a second test module is a second Target — this is a
  mechanical consequence of the `.core`, not a style preference, and it is the
  rule that wins over any "one Target per intent" advice you may read
  elsewhere. That advice applies to **classic HDL testbenches**, where one
  sentinel-scored TB toplevel usually covers a whole intent and splitting it
  buys nothing.
  A project with eight cocotb modules therefore has eight sim Targets. If it
  also has build variants (a `-D` set per hardware flavour), the Targets
  multiply: variants × modules. That is fine up to a point — the Targets are a
  handful of `.core` lines each and `booley targets` stays readable — but it is
  worth deciding **which** variants earn a full module sweep rather than
  generating the whole matrix. A common shape is: every module on the default
  variant, plus the one or two modules that actually exercise the varying
  behaviour on the other variants. Goals name Targets explicitly, so
  an unenumerated combination is simply one nobody gates on.
- **Tracing goes through a VCD.** Booley converts it to FST; `--trace-fst` is
  rejected. See [Tracing a Cocotb Target](#tracing-a-cocotb-target).
- **Image-provisioned only**, Icarus or Verilator. Commercial simulators are
  out of scope for Cocotb Targets.
- Tests generated by **`TestFactory` / `generate_tests`** or
  **`@cocotb.parametrize`** are fine: list the rendered IDs in `tests.toml`
  (e.g. `run_test_tx/ifg=12`), not the generating function's name.
  `booley doctor` defers checking these names to `results.xml` at run time.

### Tracing a Cocotb Target

`booley flow sim --target <target> --trace` works on a Cocotb Target with no
trace setup in the `.core`. The cocotb run owns a VCD dump: the simulator writes
`dump.vcd` in the run directory, and after the run Booley converts it to the FST
that B-Wave reads, then removes the raw VCD. A failed conversion keeps the VCD,
as `trace.vcd`, with its diagnostics in the Target's simulation work directory.
As on any Target, a header-only or otherwise unqueryable waveform is rejected.

Booley drives the dump itself, so `[flows.sim].trace_args` has no effect here:

- **Icarus** gets `+trace`, which fires Booley's dump module, the same one the
  [trace overlay](FLOW-SIM.md#how-booley-asks-for-a-waveform-flowssimtrace_args) supplies
  to an SV Target.
- **Verilator** gets `--trace --trace-file dump.vcd`, the flags of the C++
  harness cocotb supplies.

What `verilator_options` may carry on a Verilator Cocotb Target, and what a
traced run then does:

| Trace flag | Result |
|---|---|
| none | Accepted. Booley adds `--trace` to the traced build. |
| `--trace` or `--trace-vcd` | Accepted. This is a VCD trace recipe; Booley keeps the options as written. |
| `--trace-fst` | Rejected: this is a native FST recipe, and cocotb's harness is driven for a VCD. |
| `--trace-saif` | Rejected, as on every Verilator Target. |

Booley refuses a traced run of a `--trace-fst` Cocotb Target with:

```text
Cocotb Target '<target>' requests native FST tracing, but the Cocotb run-half currently owns a VCD dump; use a VCD trace recipe for this Target
```

Remove `--trace-fst`, or replace it with `--trace`, to fix it. A
`-DVM_TRACE_FMT_FST` define must go with it, since Booley rejects that define
without `--trace-fst`.

## Flat and vendored repos

- **Flat single-file repos** (one `picorv32.v` at the root) need no `rtl/`+`tb/`
  restructure. Classification comes from the project's mandatory `.core`
  ([as always](TESTS.md#per-target-environment-env)): a fileset listing the root-level
  file (e.g. `files: [picorv32.v]`) is classified by its **exact path** rather
  than a parent directory, so the single file is treated as RTL with no
  directory layout. TB files use the same fileset mechanism carrying
  `tags: [tb]`.
- **Vendored cores** you don't want Booley/FuseSoC to discover (a bundled SoC's
  example cores) are quarantined with a `FUSESOC_IGNORE` marker file in the
  directory. Booley's `.core` scanner skips any directory carrying one, exactly
  as FuseSoC's own scanner does.
- **Recursive symlinks** are directory links back to a containing ancestor
  (for example `src/a/lib/self -> ../../..`). FuseSoC discovery follows directory
  links, so following these would repeatedly scan the same tree. Doctor reports
  one failure per resolved ancestor, with a link count and a few paths; run
  `booley doctor --verbose` for every affected path. Place `FUSESOC_IGNORE` in
  each link's containing directory, or an enclosing subtree you can exclude:
  it hides **all cores in that subtree**, so avoid excluding wanted cores at
  the link's target. Local untracked markers leave vendored source files
  unchanged, but **nested untracked markers are not copied into Goal or baseline
  worktrees**; commit them if repository changes are acceptable. To keep the
  vendored repository unchanged across worktrees, use
  [Stealth isolation](STEALTH.md#stealth-mode) (`enabled = true` and
  `ignore_native_cores = true`) with authored cores in `.booley/cores/`;
  this excludes native discovery entirely while reading the original RTL.
- **Git submodules** work, but Goal Mode does not clone them. See
  [Submodules](#submodules) below.
- **Multi-core repos** (a FuseSoC-native project shipping tens or hundreds of
  `.core` files: ibex has 208, with a `lint` target in 54 of them) work with
  **no `[fusesoc]` config at all**. Sharing a Target name across cores (many
  `lint`s, many `sim`s) is normal FuseSoC, not an error; Booley identifies a
  Target by its `(VLNV, name)` pair — VLNV being FuseSoC's
  Vendor:Library:Name:Version coordinate for a core — and only asks you to
  disambiguate when a bare name is genuinely contested.

### Submodules

Goal Mode and baseline-relative Flows run in isolated git worktrees. Booley
does not run `git submodule update` there: doing so may re-clone from a private
SSH URL that the Sandbox cannot reach.

After the worktree's branch or the Flow's baseline ref is selected, Booley
reads that revision's exact gitlinks and reconstructs each submodule from the
initialized repository at the **same path** in the main Project. It transfers
the pinned commit's local Git objects into a standalone detached repository;
it does not copy a working tree, share a `.git` pointer, configure a remote, or
consult the URL in `.gitmodules`. Recursive submodules use the same rule at
each level. Consequently, SSH-only URLs are harmless once the source hierarchy
is initialized locally.

What this asks of you, once, on the host:

```bash
git submodule update --init --recursive
# If .booley is a standalone Git repository:
git -C .booley submodule update --init --recursive
```

Every selected submodule must be **present, clean, and non-shallow** in its
owning RTL repository or Paired Checkout before Goal entry or a baseline Flow
starts. Its local object database must also contain the complete object closure
for the destination's pinned commit. Worktree setup hard-errors when one of
these preconditions is not met, for example:

- `submodule <path> not found in source Project; initialize it first`
- `submodule <path> is dirty`
- `submodule <path> is shallow; fetch its complete history first`
- `local objects for submodule <path> are incomplete`

The destination gitlink is authoritative: if the main Project currently has a
newer submodule checkout, Booley still materializes the historical pin. A
matching clean destination is accepted on resume. If a later recursive step
fails, repositories created by that attempt are rolled back; pre-existing
content is never removed.

To copy only some of the submodules (a big docs or FPGA-board submodule nothing
builds against), list the ones you want:

```toml
[submodules]
paths = ["deps/bus_pkg", "deps/axi_verif"]
```

When `paths` is absent, all top-level gitlinks in the selected destination
revision are materialized. An explicit empty list selects none. A non-empty
list is an allowlist intersected with that revision's top-level gitlinks;
anything omitted remains an empty gitlink directory in the worktree. Nested
gitlinks below a selected entry are always reconstructed recursively.

For a separately paired `.booley` repository, the allowlist above
selects gitlinks in the outer repository. Booley also materializes every
top-level gitlink recorded by the Paired Checkout, recursively and
offline, because that repository can own acceptance controls and build inputs.
