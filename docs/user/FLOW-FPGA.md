# FPGA implementation (`fpga`)

`fpga` synthesizes, places, and routes a Target for a specific FPGA part with
AMD Vivado, then reports resource use (LUTs, flip-flops, block RAMs, DSPs) and
routed timing for each clock. Use it to see whether the design fits the device
and meets its clocks, and to measure how an RTL change moves those numbers. A
build can take minutes to hours, so run [Lint](FLOW-LINT.md) and
[Simulation](FLOW-SIM.md) first; for a fast area and timing estimate without
an FPGA part, use [ASIC synthesis](FLOW-SYNTH.md). Power is not measured. For
what all Flows share (choosing Targets, options, logs, exit codes), see
[Running Flows](FLOW_REFERENCE.md).

## Running an FPGA build

1. **Get Vivado into the Sandbox.** Vivado 2025.2 is not in the standard
   Sandbox Image: a host administrator registers an installation and grants it
   to the Project, and Booley mounts it read-only into the Sandbox, where
   Vivado runs. This works only on a Linux x86-64 host. If your part needs a
   paid licence, the grant also names a licence profile; floating (FlexNet)
   licences are experimental. Steps:
   [Commercial EDA provisioning](SANDBOX.md#commercial-eda-provisioning).
2. **Give the agent an FPGA Target.** It needs the FPGA part, the top module,
   and at least one XDC constraints file that defines your clocks
   (`create_clock`) and, for a full build, pin locations. Name the Target `fpga`
   or `fpga_<subject>`. The Developer Agent can write it for you if you tell it
   the part and the clocks; see [Configuration](#configuration-flowsfpga).
3. **Ask the agent**, naming the Target:

    > *"Run the FPGA build for `fpga_soc` and tell me LUTs and Fmax."*
    >
    > *"Compare `fpga_soc` resources and timing against `main`."*
    >
    > *"Try `fpga_soc` with the `max_frequency` profile and see if Fmax improves."*

4. **Read the result.** The agent reports utilization, timing, and the verdict
   for each Target, plus percentage changes against the baseline when you asked
   for a comparison.

To reproduce a run yourself inside the Sandbox:

```bash
booley flow fpga --target fpga_soc
booley flow fpga --target fpga_soc --ppa-profile max_frequency --baseline main
```

| Option | Effect |
|---|---|
| `--baseline <git-ref>` | Also build the Target at that revision and show LUT/FF/BRAM/DSP changes in percent. |
| `--ppa-profile <compact\|balanced\|max_frequency>` | Use this [optimization profile](#optimization-profiles) for this run, for the baseline too. Default: the Target's `ppa_profile`, else `balanced`. |
| `--no-cache` | Build again even when an identical earlier build can be reused. |

## Reading results

Each Target gets one row: LUT, FF, BRAM, and DSP counts, worst setup slack
(WNS), worst hold slack (WHS), Fmax, build time (or `cached`), and a verdict.
Fmax is that of the slowest clock, derived from the clock period in your XDC
and its setup slack; with several clocks, the row names that clock. The JSON
report (`fpga_<target>.json`) has the per-clock breakdown (period, slacks,
critical path, Fmax), and the verdict card prints its path and the build log's.

| Verdict | Meaning | Exit |
|---|---|---:|
| `PASS` | Routing completed, LUT and FF counts were found, setup and hold slack are both ≥ 0 on every clock, and there are no latches, combinational loops, or multi-driven nets. | `0` |
| `FAIL` | Vivado rejected the RTL (a syntax or port error), or one of the above is not true; the failure line says which: `timing not met`, `critical conditions`, or `missing LUT/FF metrics`. | `1` |
| `ERROR` | No verdict: Vivado missing, crashed, or timed out; a Target error such as a missing part or XDC; or a baseline that could not be compared. | `2` |

An XDC without `create_clock` gives Vivado nothing to time against, so no slack
is reported and the run fails `timing not met`.

An `FPGA:{target}` Goal in [Goal Mode](GOAL-MODE.md) is met only by a `PASS`;
the Goal can add limits on resources and Fmax, absolute or relative to the Goal
base ([threshold parameters](GOAL-MODE.md#threshold-parameters)).

## Optimization profiles

A profile states what to optimize for; it is not a guaranteed result.

| Profile | Optimizes for |
|---|---|
| `compact` | Fewer resources. |
| `balanced` (default) | Vivado's default trade-off. |
| `max_frequency` | Higher Fmax. |

Set it on the Target (`ppa_profile`) to make it stick, or with `--ppa-profile`
for one run.

## Out-of-context builds

A block that is not a whole chip, such as an IP core with more ports than the
package has pins, cannot be placed with I/O buffers on real pins. Set
`out_of_context: true` on its Target: Vivado then synthesizes the block without
I/O buffers, so it places and routes for resource and timing numbers without a
pinout. Such a build produces no bitstream.

## Reusing earlier builds

When every input matches an earlier build (sources, constraints, part, profile,
parameters, Target options, and tool versions), Booley reuses its results
instead of running Vivado again; the row shows `cached`. Pass `--no-cache`, or
ask the agent for a fresh build, to force a new one.

## Configuration (`[flows.fpga]`)

Besides the shared `enabled` and `timeout_ms` (default `7200000`, two hours
per Target build), FPGA implementation has no settings in `booley.toml`:

```toml
[flows.fpga]
timeout_ms = 7200000
```

Everything about how the design is built lives on the Target in the `.core`
file, not in `booley.toml`:

```yaml
# <design>.core
filesets:
  fpga_constraints:
    files:
      - constraints/top.xdc: {file_type: xdc}
targets:
  fpga_soc:
    flow_options:
      tool: vivado
      part: xc7a100tcsg324-1   # required
      ppa_profile: balanced    # compact | balanced | max_frequency
      out_of_context: false    # default false
    filesets: [rtl, fpga_constraints]
    toplevel: top
```

The `file_type: xdc` fileset is required and is the only source of
constraints. Keep `tool: vivado`. Other `flow_options`, such as `synth` or
`pnr`, do not change the build. A vendored Target whose name has no `fpga`
prefix can still be built if its `tool` is `vivado`. How the Flow picks RTL
files: [Fileset file types](TARGETS.md#fileset-file-types).
