# Booley Flow reference

This reference describes the public behavior of Booley's built-in deterministic
Flows: `sim`, `elab`, `lint`, `synth`, and `fpga`. It explains what to invoke,
what each result means, and which reports and artifacts to inspect. For exact
project configuration, see [CONFIG.md](CONFIG.md); for compatible EDA programs
and versions, see [SUPPORTED-EDA-TOOLS.md](SUPPORTED-EDA-TOOLS.md).

## Using a Flow

In normal work, ask the Interactive or Developer Agent to run the appropriate
Flow and name the Target or test when it matters. To reproduce or diagnose a run
yourself inside the Session Runtime, use the direct CLI:

```bash
booley flow lint --target lint_soc
booley flow sim --target sim_soc --test reset
booley flow synth --target synth_soc
booley flow <name> --help
```

Every Target-aware call requires `--target`; Booley has no project-wide default.
Use `booley targets` to list available Targets and `booley targets --for <flow>`
to narrow the list. If two cores expose the same Target name, use the qualified
selector printed by `booley targets`, such as
`lowrisc:ibex:ibex_top#lint`.

Common controls:

- `--target <name,...>` selects one or more configured Targets.
- `--dry-run` validates inputs and prints the planned command without running it.
- `--timeout <milliseconds>` bounds execution when the Flow exposes a timeout.
- `booley flow <name> --help` is the authoritative argument list.

## Shared result contract

All built-in Flows use the same exit-code grades:

| Exit | Meaning |
|---:|---|
| `0` | The Flow ran and its requested condition passed. |
| `1` | The Flow ran and found a design failure or finding. |
| `2` | The Flow could not reach a design verdict because configuration, infrastructure, or execution failed. |

Exit `1` is evidence about the RTL or testbench. Exit `2` means the Flow did not
produce a trustworthy design result. Agent-facing MCP calls carry the same grade
in `EXIT_CODE:` and structured output; MCP `isError` is not the design verdict.

Every completed run identifies the resolved Target and publishes normalized
reports under `.booley_project/.runtime/flow-reports/`. Reports point to their
run logs and retained artifacts. Ticket Mode also applies a valid result to the
Criterion family owned by that Flow; a Developer Agent's assertion alone never
satisfies a Criterion.

## `sim`

`sim` builds and runs the tests registered for a simulation Target. The Target
selects Verilator or Icarus and whether the testbench is HDL or cocotb.

Useful controls:

- `--test <name>` runs one registered test.
- `--skip <name,...>` excludes registered tests.
- `--trace` captures a waveform artifact.
- `--result-verbosity full` prints every cocotb XML testcase entry; complete
  XML and JSON artifacts are retained regardless of console verbosity.

HDL testbenches report their outcome through configured pass/fail sentinels;
cocotb Targets use cocotb's result file, with assertion output still able to
fail the run. Fail sentinels take priority. A clean process that produces no
valid verdict is `inconclusive`, never a pass. A traced run is likewise
inconclusive when it cannot confirm a fresh trace artifact.

The Flow records per-test verdicts and can satisfy `sim_pass_<target>` and
configured per-test Cycle Count Criteria. Its report includes resolved test
identity, duration, verdict details, optional Cycle Count observations, logs,
and trace artifacts.

## `elab`

`elab` compiles and elaborates a Target without running simulation. It is a fast
structural diagnostic, not a substitute for a passing simulation.

`--standalone` additionally checks whether RTL modules elaborate from their
declaring files. This is useful for unusual reusable-module requirements; normal
RTL completion still uses Simulation Criteria because simulation already
includes elaboration.

The Flow can satisfy `elab_pass_<target>` and, when requested,
`elaborate_standalone`. Reports distinguish primary Target elaboration from
standalone-module findings and point to retained build logs or trees.

## `lint`

`lint` runs the linter selected by each Target. Verilator provides structural
and semantic diagnostics; Verible provides style and naming diagnostics. To run
both, declare and invoke two Targets.

`--scope <file,...>` filters the reported findings to selected files. Project
configuration decides whether warnings make the direct Flow exit nonzero, but
the report and `lint_clean_<target>` evidence retain the actual finding counts.

The normalized report groups findings by file, line, severity, rule, and
message, deduplicates repeated diagnostics, and points to the complete run log.

## `synth`

`synth` produces a fast ASIC quality-of-results estimate for RTL iteration. It
is not tape-out synthesis or sign-off. The Target supplies the top, frontend,
technology inputs, and optional SDC constraints.

Useful controls:

- `--baseline <git-ref>` compares the candidate with the same sealed Target at
  another revision.
- `--default-clock <picoseconds>` supplies a clock only when the Target has no
  SDC.
- `--frontend <sv2v|slang>` overrides the Target's RTL frontend for diagnosis.

The Flow reports area, timing/Fmax, inferred latches, frontend identity, and
the measurement basis. It satisfies `synthesis_ok_<target>` only when synthesis
completes and every configured threshold and latch policy passes.

## `fpga`

`fpga` runs FPGA implementation for a Target, currently through host-provisioned
AMD Vivado. The Target owns the FPGA part, toplevel, compile-time parameters,
and XDC constraints.

Useful controls:

- `--baseline <git-ref>` compares implementation metrics with another revision.
- `--no-cache` forces fresh implementation instead of reusing a matching result.
- `--dry-run` validates the Target and prints the planned Vivado build.

The Flow normalizes utilization, routed timing/Fmax, DRC status, constraints,
and cache identity. It satisfies `fpga_impl_ok_<target>` only when implementation
evidence is complete and every configured timing, utilization, and DRC rule
passes.

## Related references

- [USAGE.md](USAGE.md#booley-flows--specialists) explains the day-to-day agent
  workflow and direct CLI.
- [CONFIG.md](CONFIG.md) defines Flow, Target, test, constraint, and policy
  configuration.
- [SUPPORTED-EDA-TOOLS.md](SUPPORTED-EDA-TOOLS.md) defines supported EDA tools,
  provisioning, trace capability, and versions.
- [FLOW_IMPLEMENTATION.md](../internals/FLOW_IMPLEMENTATION.md) documents the
  built-in Flow implementation for Booley contributors.
