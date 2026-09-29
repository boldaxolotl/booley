# Built-in Flow reports

This is the file-level reference for what the built-in `sim`, `lint`, `synth`,
and `fpga` Flows write to disk: where the reports go, which JSON fields they
contain, and how simulation Campaigns are laid out. Read it if you write a
script or tool that consumes Flow output.

| Document | Covers |
|---|---|
| [FLOW_REFERENCE.md](../user/FLOW_REFERENCE.md) | How to run each Flow and read its verdict |
| **This document** | Report files, JSON fields, and on-disk layout |
| [FLOW_IMPLEMENTATION.md](FLOW_IMPLEMENTATION.md) | How the Flows produce these files: planning, verdict rules, recovery |
| [MCP-TOOLS.md](MCP-TOOLS.md#built-in-flow-calls) | How the same results reach an agent over MCP |

## Where reports go

Booley picks the report root in this order:

1. `--report-dir`, if given.
2. For Ticket and agent-driven runs, `<runtime>/flow-reports`.
3. Otherwise, `flow-reports/` in the Project data directory.

Booley never writes `flow-reports/` into the RTL checkout itself.

Every Flow report below also carries `flow` and `timestamp`. The `synth` and
`fpga` reports additionally carry an `implementation` block with the grade,
metrics, recipe, provenance, and baseline comparison (see
[FLOW_IMPLEMENTATION.md](FLOW_IMPLEMENTATION.md#shared-implementation-report-envelope)).

## Invocation report

Every run writes one `report.json` for the whole invocation. MCP attaches the
same file to its result.

| Field | Contents |
|---|---|
| `flow`, `target`, `argv` | Which Flow ran, the requested Target selector, and the parsed arguments. |
| `exit_code`, `passed` | The overall result. |
| `criterion_key`, `criterion_met` | The Criterion result, when the run maps to exactly one Criterion. Empty or false for multi-Target runs. |
| `timestamp`, `elapsed_s`, `slug` | Start time, duration, and the Ticket slug (empty outside a Ticket). |
| `detail` | Flow-specific results and pointers to per-Target files. |
| `eda_tool`, `run_id`, `report_text` | The EDA tool used, the job ID, and the printed verdict card, when present. |
| `usage` | For Specialists that call a model: `input_tokens`, `output_tokens`, `cached_tokens`, `cache_create_tokens`, and `cost_usd`. |

## Progress

Long `sim`, `synth`, and `fpga` runs also write `progress.json` while they
work, so you can watch a run or tell what state an interrupted one was left in.
Every update includes `run_id` and `timestamp`.

- `complete: true` means the run has stopped. It does not mean it succeeded.
- `phase: complete`: every planned Target was processed.
- `phase: aborted`: the run stopped early; `pending_targets` lists the Targets
  that did not finish.
- `phase: superseded`: this was an interrupted coverage run that was later
  resumed. `superseded_by` names the resuming run (`invocation` number and,
  when known, `run_id`); look there for the outcome. This run's Target lists
  still show where it stopped. Marking is best effort, so an `aborted` run may
  still have been resumed.

Coverage runs add `coverage: true` and per-Target detail to their progress file.

## Dry-run plan

`--dry-run` prints a JSON `FlowPlan` and writes it to
`<report-root>/<flow>/flow_plan.json`. It does not take a numbered run
directory.

The plan has `schema_version`, `flow`, `mode`, `semantic_plan_fingerprint`,
the ordered `work_units`, `aggregate_errors`, and `planning_disclosures`. Each
work unit lists its Target, timeout, sources and constraints, resolved
parameters, the exact command it would run, and the files it would produce.
Paths are relative to `work_dir` where possible, and no environment values or
secrets are included. The fingerprint ignores scratch paths, so a dry run and
the real run of the same work have the same fingerprint.

A dry run runs no EDA tool, touches no Criteria, and fills no caches. It may
run FuseSoC setup or generators in a throwaway directory when resolving the
Target requires it; `planning_disclosures` says so when that happens.

## `sim`

### Simulation Campaign layout

Every `sim` run uses the same layout, whatever the testbench (HDL or cocotb) and
whether or not it collects coverage. Each run takes the next number `N` under
`<report-root>/sim/`, and each Target gets its own directory:

```text
<report-root>/sim/<N>/
  report.json
  progress.json
  targets/<target>/
    campaign/
      manifest.json                    what this run will execute (fixed at start)
      summary.json                     where it stands now
      build-variants/.../              simulator builds and their results
      work-items/.../                  one directory per test or batch, with its attempts
    simulation.json                    the Target's results (see below)
    coverage.json                      coverage runs only
```

Qualified Target selectors (such as `vendor:ip:core:1.0#sim`) are
percent-encoded into one directory name.

Three cases add to this layout:

- **Coverage** adds `coverage.json`, the point store, and `native/` to the same
  Target directory (see [Coverage Campaign files](#coverage-campaign-files)).
- **Resume** takes a new run number for its own `report.json` and
  `progress.json`, but writes Campaign results into the *original* run's
  `campaign/` directory, so one Campaign stays in one place.
- **A Cycle Count baseline** gets its own Target directory next to the
  candidate's, named from `<target>@baseline-<first 12 hex of revision>`.

The Campaign files reference each other by size and SHA-256 digest. Resume
checks that whole chain, plus the Target's current sources and test suite,
before it runs anything. If the suite or sources changed, the old Campaign can
no longer be resumed.

### Report schemas and references

- `simulation.json` uses `booley.simulation-projection/v2`.
- `report.json` uses `booley.simulation-report/v2`.
- `coverage.json` uses `booley.coverage-campaign/v4`.

To find a Target's results, follow `artifacts[target].report` in the run's
`report.json`. Every path inside these files is relative, and each reference
says what it is relative to: `report_invocation` (this run's directory),
`reports_root` (the report root, used when a resumed run points at the original
run's Campaign), or `external_origin_target` (an original run under a different
report root; the caller supplies that directory).
Copying a whole run directory, or the whole report root, keeps every reference
valid. A resumed run's report points back to the original run's Campaign, so
copying only the resumed run leaves those references dangling.

If writing the final report fails, the run exits `2` with
`detail.completion_error`; results already written are kept.

### `simulation.json`

`sim/<N>/targets/<target>/simulation.json`:

| Field | Contents |
|---|---|
| `target`, `target_identity`, `tb_top`, `eda_tool` | Target selector, its full FuseSoC identity, the testbench top, and the simulator. |
| `passed`, `complete`, `elapsed_s` | Target verdict, whether the run finished writing its results, and duration. `complete: false` means the run was interrupted while publishing and can be resumed. |
| `phase_timings_s` | Seconds spent in `setup`, `pre_sim`, `build`, `run`, `result_processing`, `publication`, and `unattributed`, with `execution_total` and `total`. |
| `tests[]` | One entry per test (see below). |
| `compile_command`, `fileset` | The build command and the resolved `rtl` and `tb` source lists. |
| `artifacts` | The report, per-test run logs, result files, and waveforms from this run. |

Each `tests[]` entry has `name`, `passed`, `verdict`, `timed_out`,
`elapsed_s`, `build_s`, `cycles`, `cycle_observation`, `sva_errors`,
`error_tail`, `test_validated`, `phase_timings_s`, and `resources`
(`command_peak_rss_mb`, `command_oom_kill_delta`, and on supported platforms
`simulation_user_cpu_s` and `simulation_system_cpu_s`). Traced tests add
`trace_path`, `trace_bytes`, `trace_top_scope`, `trace_signal_count`, and
`trace_total_ticks`. Optional fields: `artifacts.run_log`,
`workload_fingerprint`, and `validation_note`.

Elaboration checks write the same file with `mode` set to `elab_only` or
`elab_only_standalone`, and these fields:

| Field | Contents |
|---|---|
| `target`, `target_identity`, `eda_tool`, `toplevel` | The Target and its top module. |
| `passed`, `verdict`, `failure_class`, `reason`, `elapsed_s` | The outcome, why it failed, and duration. |
| `compile_command`, `fileset` | The build command and source lists, when setup succeeded. |
| `log` | The full build log. |

For `elab_only_standalone`, `report.json` also has `detail.standalone` with
`modules_checked`, `shared_files`, `frontend`, `failures`, any `unparsed`
modules, and the log path.

### Coverage Campaign files

A coverage run adds these files to each Target directory:

```text
<report-root>/sim/<N>/targets/<target>/
  coverage.json              the Campaign: rollups, per-file rollups, evaluation
  coverage-points.jsonl.gz   every coverage point and which runs hit it
  native/raw/                one Verilator database per test
  native/merged/             the merged database
```

`coverage.json` is the entry point; always read points through it rather than
opening the point store directly. It holds fingerprints, capabilities, the
evaluation against any Coverage Criterion, and rollups per metric and per
source file (line, branch, expression, toggle). Its `scoring` field says
whether the numbers can be trusted: `valid` when collection completed
normally, otherwise `invalid` with the reason, and empty rollups.

In `report.json`, `detail.targets[<target>]` keeps each Target's `simulation`,
`collection`, and `evaluation` results even when another Target decided the
exit code. `coverage_campaign` points to `coverage.json` once it has been
written.

### Coverage retention

`python -m booley.flows.sim.campaign_retention` deletes the evidence of one
exact run. Nothing is ever deleted automatically. Usage is in
[FLOW_REFERENCE.md](../user/FLOW_REFERENCE.md#cleaning-up-old-campaigns); the
algorithm is in
[FLOW_IMPLEMENTATION.md](FLOW_IMPLEMENTATION.md#exact-report-retention).

- **`--native-target <target>`** removes only that Target's raw and merged
  Verilator databases. `coverage.json` and the point store stay, so the
  Campaign can still be analyzed. `availability.json` in the Target directory
  records the pruning.
- **`--full`** removes the whole run. It leaves an empty `.pruned-N` marker so
  the run number is not reused. It refuses when later resumed runs depend on
  this one, unless `--include-dependents` is given.

Both refuse to touch a run that is still active, or any file the run did not
produce. An interrupted cleanup is safe to retry with the same arguments.

## `lint`

`lint_report.json`:

| Field | Contents |
|---|---|
| `targets`, `eda_tools` | The Targets and the linter used for each. |
| `passed`, `elapsed_s`, `total_warnings` | Whether lint is clean, duration, and the number of distinct findings. `passed` is false whenever there are findings, even when `warnings_as_errors = false` lets the CLI exit `0`. |
| `warnings[]` | One `rule`, `file`, `line`, and `message` per distinct finding. |
| `errors[]` | `target` and `message` for each Target that could not be linted. |
| `target_results[]` | Per Target: `target`, `eda_tool`, raw `warnings` count (before deduplication and `--scope`), `files_linted`, `toplevel`, `toplevel_linted`, `duration_s`, `error`, and `log`. |
| `artifacts` | The report and per-Target logs. |

## `synth`

`synth_<target>.json` (a qualified selector becomes a sanitized file name with
a hash suffix):

| Field | Contents |
|---|---|
| `target`, `eda_tool`, `synth_mode` | The Target, the tool, and logical or physical mode. |
| `passed`, `elapsed_s`, `returncode`, `timed_out`, `termination`, `infra_error`, `has_metrics` | The verdict and how the run ended. |
| `yosys_complete`, `timing_complete`, `structural_checks_complete`, `ppa_complete`, `peak_rss_mb` | Which stages finished, and peak memory. |
| `area_um2`, `area_source`, `area_kge`, `cells` | Area and cell count. |
| `per_clock`, `wns_ns`, `whs_ns`, `reg2reg_slack_ns`, `reg2reg_fmax_mhz` | Physical-mode timing. Each `per_clock` entry has `period_ns`, `wns_ns`, `whs_ns`, `critical_path_ps`, and `fmax_mhz`. Logical mode reports `estimated_fmax_mhz` instead. |
| `conditions` | `latches`, `expected_latches`, `unexpected_latches`, `comb_loops`, `multi_driven`, and the combined `has_critical`. |
| `total_warnings`, `warning_summary` | Warning count, plus `unique_warnings` and counts grouped by tool, category, and disposition, with a few sample messages. |
| `baseline`, `delta_pct`, `timing_delta_pct` | With `--baseline`: the baseline's metrics and the deltas. `baseline.ref` is the compared revision. |
| `baseline_target`, `candidate_target` | Selectors of the compared Targets. |
| `baseline_target_identity`, `candidate_target_identity` | Their full FuseSoC identities. |
| `run_evidence`, `baseline_run_evidence` | Source and recipe provenance for each side. |
| `failure_output`, `io_bound_critical` | A failure excerpt, and whether the critical path runs through I/O. |
| `artifacts` | The report, run log, build directory, and timing directory. |

Actionable warnings set `grade: "warn"` but keep `passed: true`. Known-benign
warnings are still counted, with a rationale. `artifacts.log` has every raw
message.

## `fpga`

`fpga_<target>.json`:

| Field | Contents |
|---|---|
| `target`, `eda_tool` | The Target; the tool is always Vivado. |
| `passed`, `returncode`, `timed_out`, `infra_error` | The verdict and how the run ended. |
| `cached`, `cache_fingerprint` | Whether results came from the implementation cache, and the cache key. |
| `metrics` | `lut_count`, `ff_count`, `bram_count`, `dsp_count`, `wns_ns`, `whs_ns`, `per_clock`, `latches`, `comb_loops`, `multi_driven`, `elapsed_s`, `cached`, `cache_fingerprint`, `failure_output`, `log_path`, and `artifacts`. Each clock has `period_ns`, `wns_ns`, `whs_ns`, `critical_path_ps`, and `fmax_mhz`. |
| `baseline_ref`, `baseline_metrics` | With `--baseline`: the baseline revision and the same metrics, without artifact paths. |
| `recipe_fingerprint`, `recipe_snapshot`, `run_evidence` | The implementation recipe and provenance for this run. |
| `baseline_recipe_fingerprint`, `baseline_recipe_snapshot`, `baseline_run_evidence` | The same for the baseline. |
| `cache_consumer_run_id` | Set when this run reused another run's cached results. |
| `baseline_target`, `candidate_target` | Selectors of the compared Targets. |
| `baseline_target_identity`, `candidate_target_identity` | Their full FuseSoC identities. |
| `artifacts` | The report, run log, and build, synthesis, and implementation directories. |

The baseline and candidate may use different PPA profiles, but a comparison is
refused when their measurement recipes differ.
