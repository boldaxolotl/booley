# FPGA implementation (`fpga`)

`fpga` synthesizes, places, and routes a Target for a specific FPGA part, then
reports resource use (LUTs, flip-flops, block RAMs, DSPs) and routed timing for
each clock. AMD Vivado runs the build. Use it to see whether the design fits the device
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
   paid license, the grant also names a license profile; floating (FlexNet)
   licenses are experimental. Steps:
   [Commercial EDA provisioning](SANDBOX.md#commercial-eda-provisioning).
2. **Check for an FPGA Target.** `booley targets --for fpga` lists them. Each
   needs the FPGA part, the top module, and at least one XDC constraints file
   that defines your clocks and, for a full build, pin locations. The agent
   can write one if you tell it the part and the clocks; to add it yourself,
   name the Target `fpga_<subject>` and see
   [Timing constraints](#timing-constraints) and the
   [FPGA Target recipe](TARGETS.md#fpga-target).
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

The Target's `flow_options` set how it is built. Some also have an option
that overrides the Target for one run; with `--baseline`, the override applies
to the baseline build too:

| Target setting | Option for one run | Effect |
|---|---|---|
| `part` | none | The FPGA part. Required. |
| `ppa_profile` | `--ppa-profile <compact\|balanced\|max_frequency>` | What to optimize for. Default: `balanced`. See [Optimization profiles](#optimization-profiles). |
| `out_of_context` | none | `true` builds the block without I/O buffers. Default: `false`. See [Out-of-context builds](#out-of-context-builds). |
| none | `--baseline <git-ref>` | Also build the Target at that revision and add the change in percent. Shared rules: [Running Flows](FLOW_REFERENCE.md#common-options). |
| none | `--no-cache` | Build again even when [an earlier build could be reused](#reusing-earlier-runs). |

## Reading results

A run you start yourself prints one row per Target, then the path of its log.
Example (Vivado):

```text
  Target     LUT      FF      BRAM   DSP   WNS        WHS        Fmax                  Time      Status
  ────────   ──────   ─────   ────   ───   ────────   ────────   ───────────────────   ───────   ──────
  fpga_soc   12,034   8,100   4      2     0.312 ns   0.045 ns   114.1 MHz [clk_sys]   1843.2s   PASS

  fpga_soc: log: .booley/flow-reports/fpga/3/artifacts/fpga_fpga_soc/run.log

RESULT: PASS
```

WNS and WHS are the worst setup and hold slack over all clocks. Fmax is that
of the slowest clock, derived from the clock period in your XDC and its setup
slack; with several clocks, the row names that clock. Time reads `cached` when
the build was [reused](#reusing-earlier-runs); a reused build prints no log.
With `--baseline`, a `baseline: <commit>` line comes first and a second row
under each Target shows the LUT, FF, BRAM and DSP changes, such as `Δ -3.7%`.
A failing Target also gets notes under the table: the latch, combinational-loop
and multi-driven counts, or the end of Vivado's output.

The run's files are under `flow-reports/` in the Booley Directory (or
`--report-dir`), in the numbered run directory (`fpga/3/` above):

| File | What it holds |
|---|---|
| `fpga/<N>/targets/<target>.json` | Per Target: `passed`, and under `metrics` the `lut_count`, `ff_count`, `bram_count` and `dsp_count`; `per_clock`, each clock's `period_ns`, `wns_ns`, `whs_ns`, `critical_path_ps` and `fmax_mhz`; and `latches`, `comb_loops` and `multi_driven`. On an `ERROR`, `infra_error` says why and `metrics.failure_output` holds Vivado's last output. With `--baseline`, `baseline_metrics` has the baseline's numbers. A copy of the newest run's file is kept as `fpga_<target>.json` at the top of `flow-reports/`. |
| `fpga/<N>/artifacts/fpga_<target>/run.log` | The build's output, followed by Vivado's placed utilization report and its routed timing-summary and DRC reports. |

For the rest of Vivado's output, such as the full timing paths or
checkpoints to open in the Vivado GUI, go to the build directory in your
checkout, `.booley/.runtime/edalize/fpga/<target>/`. It holds the Vivado
project, with each stage's reports, logs and checkpoints under
`fpga_<target>.runs/synth_1/` and `fpga_<target>.runs/impl_1/`; the JSON
report lists these directories under `metrics.artifacts.live_dirs`. The next
build of the same Target overwrites them, and a `--baseline` build's are
deleted when the run ends.

| Verdict | Meaning | Exit |
|---|---|---:|
| `PASS` | Routing completed, LUT and FF counts were found, setup and hold slack are both ≥ 0 on every clock, and there are no latches, combinational loops, or multi-driven nets. | `0` |
| `FAIL` | Vivado rejected the RTL (a syntax or port error), setup or hold slack is negative (`timing not met`), or the design has latches, combinational loops, or multi-driven nets (`critical conditions`). | `1` |
| `ERROR` | No verdict: Vivado missing, crashed, or timed out; routing did not complete or reported no resource or timing numbers; a Target error such as a missing part or XDC; or a baseline that could not be compared. | `2` |

A timing violation is `FAIL`, unlike [ASIC synthesis](FLOW-SYNTH.md#reading-results),
where it is `WARN` by default: routed timing on a real part is the result
itself, not an estimate. FPGA builds allow no latches at all; there is no
`expected_latches` setting.

## Timing constraints

An FPGA Target reads its constraints from XDC files in a `file_type: xdc`
fileset. At least one file must create a clock; Booley adds no constraints of
its own, so the clock periods, I/O delays and pin locations you write are what
the build is timed and placed against. To declare the fileset, see the
[FPGA Target recipe](TARGETS.md#fpga-target).

```tcl
# constraints/soc.xdc
create_clock -name clk_sys -period 8.0 [get_ports clk_i]
set_property PACKAGE_PIN E3 [get_ports clk_i]
set_property IOSTANDARD LVCMOS33 [get_ports clk_i]
```

A Target without XDC stops with an `ERROR` before Vivado starts. An XDC
without `create_clock` gives Vivado nothing to time against, so no slack is
reported and the run ends in `ERROR` (missing timing evidence). An
[out-of-context build](#out-of-context-builds) needs no pin locations.

## Optimization profiles

A profile states what to optimize for; it is not a guaranteed result.

| Profile | Optimizes for |
|---|---|
| `compact` | Fewer resources. |
| `balanced` (default) | Vivado's default trade-off. |
| `max_frequency` | Higher Fmax. |

Set it on the Target (`ppa_profile`) to make it stick, or with `--ppa-profile`
for one run.

## Reusing earlier runs

When every input matches an earlier passing build of this Target in this
checkout (sources, constraints, part, profile, parameters, Target options, and
tool versions), Booley reuses its results instead of running Vivado again; the
row shows `cached`. Pass `--no-cache`, or ask the agent for a fresh build, to
force a new one.

## Out-of-context builds

A block that is not a whole chip, such as an IP core with more ports than the
package has pins, cannot be placed with I/O buffers on real pins. Set
`out_of_context: true` on its Target: Vivado then synthesizes the block without
I/O buffers, so it places and routes for resource and timing numbers without a
pinout. Such a build produces no bitstream.

## Configuration (`[flows.fpga]`)

Besides the shared `enabled` and `timeout_ms` (default `7200000`, two hours
per Target build), FPGA implementation has no settings in `booley.toml`:

```toml
[flows.fpga]
timeout_ms = 7200000
```

Everything about how the design is built lives on the Target in the `.core`
file, not in `booley.toml`: see the [Target settings](#running-an-fpga-build)
and the full [FPGA Target recipe](TARGETS.md#fpga-target), with its XDC
fileset. How the Flow picks RTL and constraint files:
[Fileset file types](TARGETS.md#fileset-file-types).
