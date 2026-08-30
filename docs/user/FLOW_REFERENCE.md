# Booley Flow reference

This reference describes the public behavior of Booley's built-in deterministic
Flows: `sim`, `elab`, `lint`, `synth`, and `fpga`. It explains what to invoke,
what each result means, and which reports and artifacts to inspect. For exact
project configuration, see [CONFIG.md](CONFIG.md); for compatible EDA programs
and versions, see [SUPPORTED-EDA-TOOLS.md](SUPPORTED-EDA-TOOLS.md).

## Using a Flow

In normal work, ask the Interactive or Developer Agent to run the appropriate
Flow and always name the Target. For `sim`, also name the test or explicitly ask
for the Target's full registered test suite. To reproduce or diagnose a run
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

An agent-facing MCP call attaches its per-invocation report as
`structuredContent.reports[0]`. That report carries `flow`, `target`,
`exit_code`, `passed`, `elapsed_s`, `timestamp`, and Flow-specific `detail`; it
also carries `eda_tool` when Target resolution reached an EDA tool.
`structuredContent.passed` repeats the overall boolean verdict. If the report is
too large for the MCP result, `reports` is empty, `truncated` is `true`, and the
result retains the Flow, Target, exit code, and artifact pointers needed to open
the durable report.

Every completed run identifies the resolved Target and publishes normalized
reports under `.booley_project/.runtime/flow-reports/`. Reports point to their
run logs and retained artifacts. The per-Flow fields below describe those
durable structured reports.

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
configured per-test Cycle Count Criteria.

Structured output (`sim_<target>.json`):

| Field | Contents |
|---|---|
| `target`, `tb_top`, `eda_tool` | Resolved simulation identity. |
| `passed`, `elapsed_s` | Target-level verdict and duration. |
| `tests[]` | Per-test `name`, `passed`, `verdict`, `timed_out`, `elapsed_s`, `build_s`, `cycles`, `cycle_observation`, `sva_errors`, `error_tail`, and `test_validated`. Trace runs add trace path, size, scope, signal-count, and tick-count fields. |
| `compile_command`, `fileset` | Best-effort generated command and resolved RTL/TB source lists. |
| `artifacts` | The report, fresh per-test run logs, result files, and trace artifacts that exist for this run. |

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

Structured output (`elab_<target>.json`):

| Field | Contents |
|---|---|
| `target`, `eda_tool` | Resolved elaboration identity. |
| `passed`, `elapsed_s` | Target-level verdict and duration. |
| `error_output` | Bounded compiler diagnostic tail; empty on a clean run. |
| `compile_command`, `fileset` | Best-effort generated command and resolved `rtl`/`tb` source lists. |
| `artifacts` | The report and fresh `run.log` pointers. |

When `--standalone` is requested, the invocation report also carries
`detail.standalone` with `modules_checked`, `shared_files`, `frontend`,
`failures`, optional `unparsed` modules, and the standalone log pointer.

## `lint`

`lint` runs the linter selected by each Target. Verilator provides structural
and semantic diagnostics; Verible provides style and naming diagnostics. To run
both, declare and invoke two Targets.

`--scope <file,...>` filters the reported findings to selected files. Project
configuration decides whether warnings make the direct Flow exit nonzero, but
the report and `lint_clean_<target>` evidence retain the actual finding counts.

The normalized report groups findings by file, line, rule, and message,
deduplicates repeated diagnostics, and points to the complete run log.

Structured output (`lint_report.json`):

| Field | Contents |
|---|---|
| `targets`, `eda_tools` | Requested Targets and the linter resolved for each. |
| `passed`, `elapsed_s`, `total_warnings` | Run verdict, duration, and deduplicated in-scope finding count. |
| `warnings[]` | Full `rule`, `file`, `line`, and `message` record for each finding. |
| `errors[]` | Target and message for each Target that could not produce a lint verdict. |
| `target_results[]` | Per-Target linter, finding count, files linted, toplevel coverage, duration, error, and log pointer. |
| `artifacts` | The durable report and per-Target run logs. |

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

Structured output (`synth_<target>.json`):

| Field | Contents |
|---|---|
| `target`, `eda_tool`, `synth_mode` | Resolved synthesis identity and logical/physical methodology. |
| `passed`, `returncode`, `timed_out`, `termination`, `infra_error` | Verdict and terminal classification. Completion flags distinguish Yosys, timing, structural-check, and complete-PPA evidence. |
| `area_um2`, `area_source`, `area_kge`, `cells` | Canonical area and cell metrics. |
| `per_clock`, `wns_ns`, `whs_ns`, `reg2reg_slack_ns`, `reg2reg_fmax_mhz` | Physical-mode timing; logical mode instead adds `estimated_fmax_mhz`. |
| `conditions` | `latches`, `expected_latches`, `unexpected_latches`, `comb_loops`, `multi_driven`, and the combined `has_critical` verdict. |
| `baseline`, `delta_pct`, `timing_delta_pct` | Optional baseline metrics and deltas. |
| `artifacts` | The durable report, complete run log, build directory, and physical-mode timing directory. |

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

Structured output (`fpga_<target>.json`):

| Field | Contents |
|---|---|
| `target`, `eda_tool` | Resolved implementation identity; the EDA tool is Vivado. |
| `passed`, `returncode`, `timed_out`, `infra_error` | Verdict and terminal classification. |
| `cached`, `cache_fingerprint` | Whether implementation evidence was reused and the cache identity. |
| `metrics` | LUT, FF, BRAM, DSP, aggregate setup/hold slack, per-clock timing/Fmax, critical-condition counts, duration, and failure output. |
| `baseline_metrics` | Optional metrics from `--baseline`. |
| `recipe_fingerprint`, `recipe_snapshot`, `run_evidence` | Normalized recipe and provenance for the current run, with baseline counterparts when present. |
| `baseline_target`, `candidate_target` | The compared Target pair. |
| `artifacts` | The durable report, complete run log, and build, synthesis, and implementation directories. |

## Related references

- [USAGE.md](USAGE.md#booley-flows--specialists) explains the day-to-day agent
  workflow and direct CLI.
- [CONFIG.md](CONFIG.md) defines Flow, Target, test, constraint, and policy
  configuration.
- [SUPPORTED-EDA-TOOLS.md](SUPPORTED-EDA-TOOLS.md) defines supported EDA tools,
  provisioning, trace capability, and versions.
- [FLOW_IMPLEMENTATION.md](../internals/FLOW_IMPLEMENTATION.md) documents the
  built-in Flow implementation for Booley contributors.
