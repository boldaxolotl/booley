# Built-in Flow reports

This is the file reference for what the built-in `sim`, `lint`, `synth`, and
`fpga` Flows write to disk: where reports go, which JSON fields they hold, and
how simulation Campaigns are laid out. Read it when you change what a Flow
writes or write code that reads it. How the Flows produce these files, and
their verdict and exit-code rules, are in the implementation docs below. User
docs show only the few files a user opens; keep them consistent with this page.

| Document | Covers |
|---|---|
| [FLOW_REFERENCE.md](../user/FLOW_REFERENCE.md) and the per-Flow pages ([FLOW-SIM.md](../user/FLOW-SIM.md), [FLOW-LINT.md](../user/FLOW-LINT.md), [FLOW-SYNTH.md](../user/FLOW-SYNTH.md), [FLOW-FPGA.md](../user/FLOW-FPGA.md)) | How to run each Flow and read its verdict |
| **This document** | Report files, JSON fields, and on-disk layout |
| [FLOW_IMPLEMENTATION.md](FLOW_IMPLEMENTATION.md) | How the Flows produce these files: planning, verdict rules, recovery |
| [FLOW_IMPLEMENTATION_SIM.md](FLOW_IMPLEMENTATION_SIM.md) | The same for `sim`: Campaigns, coverage collection, retention |
| [MCP-TOOLS.md](MCP-TOOLS.md#built-in-flow-calls) | How the same results reach an agent over MCP |

The reports are evidence: Goals, Criteria, resume, and the Coverage Analyst all
read them later, often in another process. So every run gets its own numbered
directory, files point at each other by relative path plus size and SHA-256,
and a reader can tell a stale or swapped file from the one the run wrote.

File names, fields, and this page say Simulation Campaign and Coverage
Campaign. The glossary (`docs/GLOSSARY.md`) calls them Simulation Record and
Coverage Record; [#2711](https://github.com/boldaxolotl/booley/issues/2711)
moves the code and formats to those names.

## Where reports go

Booley picks the report root in this order:

1. `--report-dir`, if given.
2. `$BOOLEY_RUNTIME_DIR/flow-reports`, when that variable is set. Every MCP
   endpoint subprocess sets it: a Goal-bound call uses the Goal Record's runtime
   directory, an ordinary Interactive Mode call the server's session runtime
   (`.interactive_logs/<session>/.runtime` in the Booley Directory).
3. Otherwise, `flow-reports/` in the Booley Directory of the checkout.

Each run reserves the next number `N` under `<report-root>/<flow>/`. Every Flow
writes these files:

| Flow | Files |
|---|---|
| all four | `<flow>/<N>/report.json`, the [invocation report](#invocation-report), and `<flow>.json`, a copy of the newest one |
| `sim` | `sim/<N>/progress.json`, the per-Target [Campaign layout](#simulation-campaign-layout) under `sim/<N>/targets/`, and `sim/.invocation-<N>.lock` |
| `lint` | `lint/<N>/lint_report.json` and `lint_report.json`, a copy of the newest one |
| `synth`, `fpga` | `<flow>/<N>/progress.json`, `<flow>/<N>/targets/<slug>.json`, and `<flow>_<slug>.json`, the per-Target report |

`<slug>` is the Target selector with every character outside `A-Za-z0-9_.-`
replaced by `_`, plus a `-<8 hex of its SHA-256>` suffix when that changed the
name.

The unnumbered copies (`<flow>.json`, `lint_report.json`,
`<flow>_<slug>.json`) are overwritten by the next run, including a failed one
that leaves `detail` empty. Code that needs a stable pointer must use the
numbered paths.

## Invocation report

`<flow>/<N>/report.json` covers the whole invocation. Specialists write the
same report, with `mcp_tool` in place of `flow`.

| Field | Contents |
|---|---|
| `$schema` | `booley.simulation-report/v3` for `sim`, `booley.flow-report/v1` for the other Flows, `booley.specialist-report/v1` for Specialists. |
| `flow`, `target`, `argv`, `mode` | Which Flow ran, the requested Target selector, the raw command-line arguments, and `detail.mode` when the Flow set one. |
| `exit_code`, `passed` | `passed` means exit `0`, whatever the Criteria say. |
| `criterion_key`, `criterion_met` | The Criterion this run decided and its verdict, when exactly one applicable Criterion maps to the run, it was evaluated, and one Target was selected. Otherwise an empty key and `null`. Present only on runs with an acceptance identity (normally Goal-bound), so read them as optional keys. |
| `timestamp`, `elapsed_s`, `slug` | When the report was written (at the end of the run), wall-clock duration, and the Goal Record ID for a Goal-bound run (otherwise empty). |
| `detail` | Flow-specific results and pointers to per-Target files. |
| `eda_tool`, `eda_tools` | The EDA tool that ran: scalar `eda_tool` for one Target, or `eda_tools` mapping each selected Target to its tool (`null` if unresolved) whenever several Targets were selected. A standalone sweep that used one frontend fills the scalar. |
| `resources` | `cpu_user_s` and `cpu_sys_s` in seconds, `peak_rss_mb` in MiB. Optional. |
| `run_id`, `report_text` | The Job ID and the printed verdict card, when present. |
| `usage` | Specialists that called a model: `input_tokens`, `output_tokens`, `cached_tokens`, `cache_create_tokens`, and `cost_usd`. |

`resources` is a process-lifetime snapshot: CPU adds the endpoint process and
the children it waited for, and `peak_rss_mb` is the largest single process,
not a sum over the process tree. Repeated calls inside one process include
earlier work, and a resumed Campaign counts only the resuming process. When the
platform cannot measure, the field is missing; treat that as unknown, never as
zero.

`synth` and `fpga` also carry the shared `implementation` envelope: top-level in
each per-Target report, and as `detail.implementation` in `report.json`, with
`schema_version`, `targets`, the worst `grade`, `passed`, `baseline_ref`, and
`results.<target>` (each Target's envelope without recipe snapshots or the
diagnostic excerpt). The envelope's sections are described in
[FLOW_IMPLEMENTATION.md](FLOW_IMPLEMENTATION.md#shared-implementation-report-envelope).

## Progress

`sim`, `synth`, and `fpga` keep `<flow>/<N>/progress.json` current while they
run, so you can watch a run or see where an interrupted one stopped. `lint`
writes none. Every update carries `run_id` and `timestamp`.

- `complete: true` means the run has stopped, not that it passed.
- `phase: complete`: every planned Target was processed.
- `phase: aborted`: the run stopped early; `pending_targets` lists the Targets
  that did not finish.
- `phase: superseded`: an interrupted coverage run that a later run resumed.
  `superseded_by` names that run (`invocation` and, when known, `run_id`); look
  there for the outcome. Marking is best effort, so an `aborted` run may also
  have been resumed.

`sim` progress also has per-Target detail and `mode`; coverage runs add
`coverage: true`. The guarantees behind these phases are in
[FLOW_IMPLEMENTATION.md](FLOW_IMPLEMENTATION.md#progress-lifecycle).

## Dry-run plan

`--dry-run` prints a JSON `FlowPlan` and writes it to
`<report-root>/<flow>/flow_plan.json`, without a numbered directory. What a dry
run does and skips is in
[FLOW_IMPLEMENTATION.md](FLOW_IMPLEMENTATION.md#shared-planning-and-dry-run-lifecycle).

The plan has `schema_version`, `flow`, `mode`, `semantic_plan_fingerprint`, the
ordered `work_units`, `aggregate_errors`, and `planning_disclosures`. Each work
unit names its Target and role, timeout, sources and constraints, parameters,
recipe, the commands it would run (marked `template: true`: paths are relative
to `work_dir` and the environment is left out), and the files it would produce.
The fingerprint ignores scratch paths, so a dry run and the real run of the
same work match. `planning_disclosures` says when `synth` or `fpga` planning ran
FuseSoC setup or generators in a throwaway directory.

For `sim`, only the Elaboration Check prints this plan: its units expose their
build budget as `timeout_ms` and recipe `build_timeout_ms`, because the Target
build is their only subprocess. Standalone units keep the standalone-sweep
`timeout_ms` and have no build field. An ordinary or coverage dry run prints
its Campaign preview instead.

## `sim`

### Simulation Campaign layout

Every `sim` run uses one layout, whatever the testbench (HDL or cocotb) and
whether it collects coverage. Each Target gets its own directory, named by its
percent-encoded selector (so `vendor:ip:core:1.0#sim_core` stays one path
component):

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
    simulation.json                    the Target's results
    coverage.json                      coverage runs only: the Coverage Campaign reference
```

Each work item holds `result.json` (its terminal result) and one directory per
attempt under `attempts/`. An attempt holds `attempt.json`, `evidence/` (the
copies the Campaign keeps, such as `0001-run.log` and the trace, which
`simulation.json` and the verdict card point at), `execution-evidence/` (the
live `run.log`, tail-truncated to 10 MB, the adapter result, and cocotb's
`results.xml`), `snapshot/`, `private-build/`, `runtime-inputs/`, and
`pre-sim/` when it applies.

Three cases add to this layout:

- **Coverage** nests a Coverage Campaign inside one attempt (see
  [Coverage Campaign files](#coverage-campaign-files)).
- **Resume** takes a new run number for its own `report.json` and
  `progress.json`, but writes results into the original run's `campaign/`
  directory, so one Campaign stays in one place.
- **A Cycle Count baseline** gets its own Target directory beside the
  candidate's, for the selector `<target>@baseline-<first 12 characters of the
  baseline revision>`.

Campaign files reference each other by size and SHA-256, in two forms. Durable
records (`summary.json`, work-item results) carry the canonical manifest
digest: the SHA-256 of `manifest.json` without its trailing newline. Typed
artifact references, such as `simulation.json`'s `campaign_manifest`, carry the
SHA-256 of the file's exact bytes. Compare each against its own form. Resume
checks the whole chain, plus the Target's current sources and test suite,
before it runs anything, and refuses when the suite or sources changed.

### Report schemas and references

- `simulation.json`: `booley.simulation-projection/v3`.
- Target-level `coverage.json`: `booley.coverage-campaign-reference/v1`.
- Nested Coverage Campaign `coverage.json`: `booley.coverage-campaign/v5`.
- Existing files are never migrated to a newer schema.

In `report.json`, `detail.campaigns.<selector>` summarizes each Target. Its
`artifacts` hold references to `manifest`, `simulation`, and, for coverage,
`coverage`; follow `artifacts.simulation` to a Target's results. A reference
stores a relative `path` and names its base in `path_base`:

- `report_invocation`: this run's directory;
- `reports_root`: the report root, when a resumed run points at the original
  run's Campaign;
- `external_origin_target`: an original run under a different report root; the
  caller supplies that Target directory;
- `origin_target`: the Target directory (used inside `simulation.json` and
  `coverage.json`).

Copying a whole run directory, or the whole report root, keeps every reference
valid. Copying only a resumed run does not: its report points back into the
original run.

If writing the final report fails, the run exits `2` with
`detail.completion_error`; results already written stay.

`report.json` also keeps every observed cycle count in top-level
`cycle_counts.<selector>[]` rows `{test, cycle_count}`, both nullable
(`test: null` is the unnamed test). The `detail.campaigns.<selector>.observations`
preview keeps only the first 32, and inline MCP output drops `cycle_counts`. If
the counts can't be computed, the report has `cycle_counts_error: unavailable`
instead; the verdict and Criteria are unaffected.

### `simulation.json`

`sim/<N>/targets/<target>/simulation.json`:

| Field | Contents |
|---|---|
| `$schema`, `flow`, `mode` | Schema ID, `sim`, and `simulate`. |
| `target`, `target_identity` | Target selector and its full FuseSoC identity. |
| `passed`, `inconclusive` | The Target verdict. |
| `complete` | `true` once the results are also recorded against the Target's Criteria (or it has none); `false` means the run stopped in between. |
| `tests[]` | One entry per test (see below). |
| `test_runs[]` | One row per Test Run: `name`, `test`, `variant`, `seed`, `args`, `env`, `fingerprint`, and `grade` (`pass`, `fail`, `error`, or `inconclusive`). `test` is null for the unnamed test; `variant` and `seed` are null when the run has none. |
| `campaign_id`, `campaign_manifest` | The Campaign and a reference to its `manifest.json`: `path_base`, `path`, `bytes`, `sha256`, `kind`, and `owner` (the Campaign ID). |
| `coverage_campaign`, `collection`, `evaluation` | Coverage runs only: a reference of the same shape to the Target's `coverage.json`, and the collection and evaluation statuses. |

Each `tests[]` entry has `name` (`default` for the unnamed test), `passed`,
nullable `cycles`, `sva_errors`, `timed_out`, `execution`, `failure_kind`, and
`error_tail`. It names its Test Run with `test_run` (for example
`smoke+fast@7`), `variant`, `seed`, and `fingerprint`, and points at that Test
Run's files: `log` (the simulator log) and `trace` (the waveform of a `--trace`
run). Both are POSIX paths relative to the directory holding this
`simulation.json`, so a copied run keeps them valid, or `null` when the file
does not exist. They come from the attempt the Campaign accepted for that Test
Run; the Test Runs of one cocotb batch share files. `test_runs[]` rows carry no
paths, because rerun matching reads those rows.

### Elaboration Check reports

An Elaboration Check writes the same file with `mode` set to `elab_only`, or
`elab_only_standalone` when the standalone module sweep also ran:

| Field | Contents |
|---|---|
| `target`, `target_identity`, `eda_tool`, `toplevel` | The Target and its top module. |
| `passed`, `verdict`, `failure_class`, `reason`, `elapsed_s` | The outcome, why it failed, and duration. |
| `compile_command`, `fileset` | The build command and source lists, when setup succeeded. |
| `log` | The full build log. |

`report.json` then has `detail.targets[]` with the same per-Target fields. For
`elab_only_standalone` it also has `detail.standalone`: `modules_checked`,
`shared_files`, `frontend`, `probes`, `failures`, `unparsed` (only when some
modules could not be parsed), and `log`. `frontend` joins the probe frontends
with `+` (`iverilog+verilator` for a mixed sweep). `probes` groups the
`{module, file}` pairs by `{frontend, dialect_flags, fallback}`;
`dialect_flags` lists the language-dialect flags the probe used (empty for the
compiler's default). Each `failures` and `unparsed` entry also carries
`frontend`, `dialect_flags`, and `fallback`.

### Coverage Campaign files

A coverage run adds a reference at Target level and the Coverage Campaign
inside the attempt it measured:

```text
<report-root>/sim/<N>/targets/<target>/
  coverage.json               booley.coverage-campaign-reference/v1
  simulation.json             the completed Target results
  campaign/work-items/<item>/attempts/<attempt>/coverage-campaign/
    coverage.json             the Coverage Campaign manifest
    coverage-points.jsonl.gz  every coverage point and which Test Runs hit it
    native/raw/               one Verilator database per Test Run
    native/merged/            the merged database
    hooks/                    hook evidence, when collected
```

The reference's `coverage_campaign` object has `path`, `path_base`, `schema`,
`campaign_id`, `bytes`, and `sha256`. Its `path_base: origin_target` resolves
`path` against the Target directory to the nested manifest; size, digest, and
identities authenticate it. Native paths inside the manifest resolve from the
**Coverage Campaign directory**, not the Target directory.

Give the Coverage Analyst the numbered Target-level `coverage.json`: it
resolves the reference and checks the enclosing Simulation Campaign and the
completed `simulation.json`, so the nested path alone is not a valid input.
Booley code reads a Campaign through `booley.flows.sim.coverage_campaign_store`,
which takes the resolved manifest (not the reference) and checks the point
store's size and digest; never open the point store directly. Keep the
reference, the attempt, and the enclosing Simulation Campaign together.

The manifest holds fingerprints, capabilities, the evaluation against any
Coverage Criterion, and rollups per metric (`rollups`) and per source file
(`source_rollups`, each `{source, rollups}`, for line, branch, expression, and
toggle). `scoring` says whether the numbers can be trusted: `status: valid`
(with `reason: null`) when collection completed, otherwise `status: invalid`
with the collection status as `reason`, and empty rollups.

Each rollup has `metric`, `semantics` (one sentence on what one point means),
`total_points`, `eligible_points`, `covered_points`, `waived_points`, and
`percent` (covered / eligible, rounded to 2 decimals, `null` when nothing is
eligible). A metric with no points has no rollup. For example:

```json
{"metric": "branch", "total_points": 810, "eligible_points": 732,
 "covered_points": 565, "waived_points": 0, "percent": 77.19, "semantics": "…"}
```

The manifest's `findings` list holds a `COV_RTL_SOURCE_WITHOUT_POINTS` warning
for each RTL source of the Target that declares a module but produced no
coverage points, addressed as `/source_closure/rtl/<index>/path`. A module the
testbench never instantiates has no points, so without this warning it would
drop out of the percentages silently. These warnings change no collection,
evaluation, Criterion, percentage, or exit code. The verdict card previews
three; the Coverage Analyst's `zero_point_sources` view pages through the full
list (see [RTL source
advisories](FLOW_IMPLEMENTATION_SIM.md#rtl-source-advisories)).

#### Point store

`coverage-points.jsonl.gz` is gzipped JSON Lines. The first line is a header,
`{"$schema": "booley.coverage-points/v1", "campaign_id": …, "target_identity": …}`;
every other line is one coverage point:

```json
{
  "id": "cp1:…",
  "identity": {
    "metric": "toggle",
    "location": {"source": "rtl/fifo.sv",
                 "start": {"line": 42, "column": 17}, "end": {"line": 42, "column": 17}},
    "hierarchy": "TOP.tb.dut.u_fifo",
    "subject": {"signal_bit_direction": "wr_ptr[2]:1->0"},
    "collector": {"record_type": "toggle", "native_key": "…"}
  },
  "hits_by_run": {"run:002:stress@1": 3},
  "disposition": {"kind": "eligible"}
}
```

| Field | Contents |
|---|---|
| `id` | Stable point ID: `cp1:` plus the unpadded base64url of the canonical `identity` JSON. Waivers and Waiver Candidates name points by it. |
| `identity.metric` | `line`, `branch`, `expression`, `toggle`, `cover_property`, or the unscored `fsm` and `covergroup`. |
| `identity.location` | Project-relative `source` and the start and end line and column Verilator reported. |
| `identity.hierarchy` | Instance path; points are per instance. |
| `identity.subject` | One key per metric: `basic_block` (line), `outcome` (branch: `if`, `else`, a case item; expression: the term combination, such as `(rst_ni==0) => 1`), `signal_bit_direction` (toggle), `cover_property`, `native_fsm`, `native_covergroup`. |
| `identity.collector` | Verilator's `record_type` and raw record key, for tracing a point back to the native database. |
| `hits_by_run` | Hit count per Test Run ID (`run:<NNN>:<Test Run name>`); zero counts are left out, so an uncovered point has `{}`. |
| `disposition` | `kind` is `eligible` (scored RTL), `unscored` (a `testbench`, `generated`, or `foreign` source, or an unscored metric), or `waived`; a waived point also has `reason`, `waiver_id`, `waiver_file`, `waiver_fingerprint`, and `provenance`. |

#### Coverage in `report.json`

`detail.targets[<target>]` keeps each Target's `passed`, `simulation`,
`collection`, and `evaluation` results, even when another Target decided the
exit code, and `coverage_campaign`, a reference to the Target-level
`coverage.json` once it was written. `coverage_metrics` maps each native metric
to `{percent, threshold, met}`, from the same Coverage Campaign as
`coverage.json`:

- `met` compares that metric with its floor, whatever the aggregate Criterion
  verdict (`criterion_key` / `criterion_met`) says.
- With no floor, `threshold` and `met` are null.
- With no eligible points, `percent` is null and a configured floor gets
  `met: false`.
- An unscored Campaign gives an empty map.

A threshold miss on a run that executed cleanly still exits `0`; for a
Goal-bound run with one evaluated Criterion, `criterion_met` is `false`.

### Coverage retention

Nothing runs this cleanup on its own. User docs tell people to delete finished
run folders directly ([Cleaning up reports](../user/FLOW_REFERENCE.md#cleaning-up-reports));
`python -m booley.flows.sim.campaign_retention` is the guarded version, for
tooling and for a run that others depend on. It removes the evidence of one
exact run:

```bash
python -m booley.flows.sim.campaign_retention --reports-root <root> --invocation <N> --native-target <target>
python -m booley.flows.sim.campaign_retention --reports-root <root> --invocation <N> --full
```

- **`--native-target`** removes the `native/` directory (raw and merged
  Verilator databases) of the Coverage Campaign that the Target's
  `coverage.json` references. The manifest and point store stay, so the
  Campaign can still be analyzed; `availability.json` (schema
  `booley.coverage-availability/v1`) in that directory records the pruning.
- **`--full`** removes the whole run and leaves an empty `.pruned-<N>` marker
  so the number is never reused. It refuses while later resumed runs depend on
  this one, unless `--include-dependents` is given, and refuses any file the
  run did not produce.

Both refuse a run that is still active and are safe to retry with the same
arguments. `--project-data`, the selection rules, and the recovery algorithm
are in [FLOW_IMPLEMENTATION_SIM.md](FLOW_IMPLEMENTATION_SIM.md#exact-report-retention).

## `lint`

`report.json` `detail` holds `targets`, `total_warnings`, `elapsed_s`,
`eda_tools` (when a linter ran), the log `artifacts`, and a bounded findings
preview:

| Field | Type and contents |
|---|---|
| `findings` | Up to 20 findings: distinct in-scope warnings in order, then Target errors. |
| `findings_total` | Integer: distinct in-scope warnings plus Target errors, before truncation. |
| `findings_truncated` | Boolean: `findings_total > 20`. |
| `findings_by_target` | Multiple Targets only: every selected Target → `{warnings: int, errors: int}`, zeros included. |

Every finding has string `severity`, `message`, and `target`, plus `rule`,
`file`, and `line`. Warnings have `severity: "warning"`, string `rule` and
`file`, and integer `line`; errors have `severity: "error"` and null `rule`,
`file`, and `line`. A warning reported by several Targets shows its first
Target in the preview but counts toward each of them in `findings_by_target`,
so per-Target counts can sum past `findings_total`. Errors count once per
Target.

`lint_report.json` is the full record:

| Field | Contents |
|---|---|
| `targets`, `eda_tools` | The Targets and the linter that ran for each. |
| `passed`, `elapsed_s`, `total_warnings` | Whether lint is clean, duration, and the number of distinct findings. `passed` is false whenever there are findings, even when `warnings_as_errors = false` lets the CLI exit `0`. |
| `warnings[]` | One `rule`, `file`, `line`, `message`, and sorted `targets` (the Targets that reported it) per distinct finding. When the Targets that reported it used different linters, `eda_tools` maps each of them to its linter. |
| `errors[]` | `target` and `message` for each Target that could not be linted. |
| `target_results[]` | Per Target: `target`, `eda_tool`, the raw `warnings` count (before deduplication and `--scope`), `files_linted`, `toplevel`, `toplevel_linted`, `duration_s`, `error`, and `log`. |
| `artifacts` | This report and each Target's log. |

## `synth`

`synth_<slug>.json`, also published as `synth/<N>/targets/<slug>.json`:

| Field | Contents |
|---|---|
| `target`, `eda_tool`, `synth_mode` | The Target, the tool, and logical or physical mode. |
| `passed`, `elapsed_s`, `returncode`, `timed_out`, `termination`, `infra_error`, `has_metrics` | The verdict and how the run ended. |
| `yosys_complete`, `timing_complete`, `structural_checks_complete`, `ppa_complete`, `peak_rss_mb` | Which stages finished, and peak memory. |
| `area_um2`, `area_source`, `area_kge`, `cells` | Area and cell count. |
| `per_clock`, `wns_ns`, `whs_ns`, `reg2reg_slack_ns`, `reg2reg_fmax_mhz` | Physical-mode timing. Each `per_clock` entry has `period_ns`, `wns_ns`, `whs_ns`, `critical_path_ps`, and `fmax_mhz`. Logical mode reports `estimated_fmax_mhz` instead. |
| `conditions` | `latches`, `expected_latches`, `unexpected_latches`, `comb_loops`, `multi_driven`, and the combined `has_critical`. |
| `total_warnings`, `warning_summary` | Warning count, plus `unique_warnings` and counts grouped by tool, category, and disposition, with a few sample messages. |
| `baseline`, `delta_pct`, `timing_delta_pct` | With `--baseline`: the baseline's metrics (`baseline.ref` is the compared revision) and the area and timing deltas. `baseline.timing_unavailable` says why no baseline timing reports were kept. |
| `baseline_target`, `candidate_target` | Selectors of the compared Targets. |
| `baseline_target_identity`, `candidate_target_identity` | Their full FuseSoC identities. |
| `run_evidence`, `baseline_run_evidence` | Source and recipe provenance for each side. |
| `failure_output`, `io_bound_critical` | A failure excerpt, and `true` when the critical path runs through I/O; each present only when it applies. |
| `openroad_reuse` | Only when OpenROAD results were reused: `producer_run_id` and `producer_started_at` of the run that produced them. `elapsed_s` and `peak_rss_mb` then cover only this run's own work (Yosys). |
| `artifacts` | `report`, `log`, `live_dirs` (the mutable `build` and `timing` work directories), `timing` (this run's copy of the STA reports, at `artifacts/synth_<slug>/timing/` in the numbered run directory), and, with `--baseline`, `baseline_timing` (the baseline's STA reports, at `artifacts/synth_<slug>/baseline_timing/`). |

The STA reports exist in physical mode only: `overall.rpt` and
`overall.csv.rpt`, `pre_repair.rpt` and `pre_repair.csv.rpt` (only when timing
repair runs), and `reg2reg.rpt` (only when the design has registers).
`[flows.synth].timing_report_paths` sets how many paths each lists. The next
run overwrites the `live_dirs` work directories, so read the numbered copies.

Actionable warnings set `implementation.status.grade` to `warn` but keep
`passed: true`. Known-benign warnings are still counted, with a rationale.
`artifacts.log` has every raw message.

## `fpga`

`fpga_<slug>.json`, also published as `fpga/<N>/targets/<slug>.json`:

| Field | Contents |
|---|---|
| `target`, `eda_tool` | The Target; the tool is always Vivado. |
| `passed`, `returncode`, `timed_out`, `infra_error` | The verdict and how the run ended. |
| `cached`, `cache_fingerprint` | Whether results came from the implementation cache, and the cache key. |
| `metrics` | `lut_count`, `ff_count`, `bram_count`, `dsp_count`, `wns_ns`, `whs_ns`, `per_clock`, `latches`, `comb_loops`, `multi_driven`, `elapsed_s`, `cached`, `cache_fingerprint`, `failure_output`, `log_path`, and `artifacts`. Each clock has `period_ns`, `wns_ns`, `whs_ns`, `critical_path_ps`, and `fmax_mhz`. |
| `baseline_ref`, `baseline_metrics` | With `--baseline`: the baseline revision and the same metrics, without file paths. |
| `recipe_fingerprint`, `recipe_snapshot`, `run_evidence` | The implementation recipe and provenance. On a cache hit, `run_evidence` describes the run that produced the cached results. |
| `baseline_recipe_fingerprint`, `baseline_recipe_snapshot`, `baseline_run_evidence` | The same for the baseline. |
| `cache_consumer_run_id` | On a cache hit, this run's ID. |
| `baseline_target`, `candidate_target` | Selectors of the compared Targets. |
| `baseline_target_identity`, `candidate_target_identity` | Their full FuseSoC identities. |
| `artifacts` | `report`, `log`, and `live_dirs` (the build, synthesis, and implementation directories). |
