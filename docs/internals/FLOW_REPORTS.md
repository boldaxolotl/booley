# Built-in Flow reports

Where the built-in `sim`, `lint`, `synth`, `fpga`, and `mutation` Flows write
their reports, how a Simulation Record is laid out, and the few rules a reader
of those files must know. The JSON is self-describing: read a report to see its
fields. User docs ([FLOW_REFERENCE.md](../user/FLOW_REFERENCE.md) and the
per-Flow pages) say how to run each Flow and read its verdict.

The reports are evidence that Criteria, resume, and the Coverage
Analyst read later, often in another process. So every run gets its own
numbered directory, and files point at each other by relative path plus size and
SHA-256.

## Where reports go

Booley picks the report root in this order:

1. `--report-dir`, if given.
2. `$BOOLEY_RUNTIME_DIR/flow-reports`, when that variable is set. Every MCP
   endpoint subprocess sets it: an Interactive Mode call uses the server's
   session runtime
   (`.interactive_logs/<session>/.runtime` in the Booley Directory).
3. Otherwise, `flow-reports/` in the Booley Directory of the checkout.

Each run reserves the next number `N` under `<report-root>/<flow>/`. Every Flow
writes these files:

| Flow | Files |
|---|---|
| all five | `<flow>/<N>/report.json`, the [invocation report](#invocation-report), and `<flow>.json`, a copy of the newest one |
| `sim` | `sim/<N>/progress.json`, the per-Target [Record layout](#simulation-record-layout) under `sim/<N>/targets/`, and `sim/.invocation-<N>.lock` |
| `lint` | `lint/<N>/lint_report.json` and `lint_report.json`, a copy of the newest one |
| `synth`, `fpga` | `<flow>/<N>/progress.json`, `<flow>/<N>/targets/<slug>.json`, and `<flow>_<slug>.json`, the per-Target report |
| `mutation` | `mutation/<N>/report.json` and the [`campaign/` evidence directory](#mutation) |

`<slug>` is the Target selector with every character outside `A-Za-z0-9_.-`
replaced by `_`, plus a `-<8 hex of its SHA-256>` suffix when that changed the
name.

The unnumbered copies (`<flow>.json`, `lint_report.json`,
`<flow>_<slug>.json`) are overwritten by the next run, including a failed one
that leaves `detail` empty. Code that needs a stable pointer must use the
numbered paths.

## Invocation report

`<flow>/<N>/report.json` covers the whole invocation. Specialists write the
same report, with `mcp_tool` in place of `flow`. Its `$schema` is
`booley.simulation-report/v3` for `sim`, `booley.flow-report/v1` for the other
Flows, and `booley.specialist-report/v1` for Specialists.

- `passed` means exit `0`, whatever the Criteria say.
- `criterion_key` and `criterion_met` are optional keys. They appear only on
  runs with an acceptance identity and hold a value only
  when exactly one applicable Criterion maps to the run, it was evaluated, and
  one Target was selected. Otherwise the key is empty and the verdict `null`.
- `resources` (`cpu_user_s`, `cpu_sys_s`, `peak_rss_mb`) is a process-lifetime
  snapshot. Repeated calls inside one process include earlier work, and a
  resumed run counts only the resuming process. When the platform cannot
  measure, the field is missing: treat that as unknown, never as zero.

`synth` and `fpga` also carry a shared `implementation` envelope: the grade,
QoR metrics, baseline deltas, and artifact pointers.

## Progress

`sim`, `synth`, and `fpga` keep `<flow>/<N>/progress.json` current while they
run, so you can watch a run or see where an interrupted one stopped. `lint` and
`mutation` write none. Every update carries `run_id` and `timestamp`.

- `complete: true` means the run has stopped, not that it passed.
- `phase: complete`: every planned Target was processed.
- `phase: aborted`: the run stopped early; `pending_targets` lists the Targets
  that did not finish.
- `phase: superseded`: an interrupted coverage run that a later run resumed.
  `superseded_by` names that run; look there for the outcome. Marking is best
  effort, so an `aborted` run may also have been resumed.

## Dry-run plan

`--dry-run` prints a JSON `FlowPlan` and writes it to
`<report-root>/<flow>/flow_plan.json`, without a numbered directory, and runs no EDA tool. The plan's commands are templates (`template: true`): paths are relative to
`work_dir` and the environment is left out. For `sim`, only the Elaboration
Check prints this plan; an ordinary or coverage dry run prints its Record
preview instead.

## `sim`

### Simulation Record layout

Every `sim` run uses one layout, whatever the testbench (HDL or cocotb) and
whether it collects coverage. Each Target gets its own directory, named by its
percent-encoded selector (so `vendor:ip:core:1.0#sim_core` stays one path
component):

```text
<report-root>/sim/<N>/
  report.json
  progress.json
  targets/<target>/
    record/
      manifest.json                    what this run will execute (fixed at start)
      summary.json                     where it stands now
      build-variants/.../              shared simulator builds (launched read-only by every attempt) and their results
      work-items/.../attempts/.../     one directory per test or batch, with its attempts
    simulation.json                    the Target's results
    coverage.json                      coverage runs only: the Coverage Record reference
```

Three cases add to this layout:

- **Coverage** nests a Coverage Record inside one attempt (see
  [Coverage Record files](#coverage-record-files)).
- **Resume** takes a new run number for its own `report.json` and
  `progress.json`, but writes results into the original run's `record/`
  directory, so one Record stays in one place.
- **A Cycle Count baseline** gets its own Target directory beside the
  candidate's, for the selector `<target>@baseline-<first 12 characters of the
  baseline revision>`.

### Attempt files

An attempt directory (`work-items/<id>/attempts/<ordinal>-<attempt id>/`) keeps
each evidence file once:

```text
attempt.json
execution-evidence/    the run log, the trace and the adapter result, where the run wrote them
evidence/              files Booley derives, and copies of files written outside the attempt
private-build/         builds made per test only
runtime-inputs/        the attempt's runtime inputs
pre-sim/               pre-sim command evidence
coverage-record/       coverage runs only
```

`execution-evidence/run.log` is the file the live pointer in `progress.json`
names, so a reader tailing it during the run finds the final log there
afterwards. The work-item result references the log, the trace and the adapter
result at `execution-evidence/<name>`. `evidence/` holds the Cocotb transport
and infrastructure-failure documents Booley derives, plus a copy of any trace
or adapter file the simulator wrote outside the attempt (for example in a
configured run directory) whose original is removed with that directory.

Record files reference each other by size and SHA-256, in two forms. Durable
records (`summary.json`, work-item results) carry the canonical manifest
digest: the SHA-256 of `manifest.json` without its trailing newline. Typed
artifact references, such as `simulation.json`'s `simulation_record_manifest`,
carry the SHA-256 of the file's exact bytes. Compare each against its own form.
Resume checks the whole chain, plus the Target's current sources and test
suite, before it runs anything, and refuses when the suite or sources changed.

A typed reference stores a relative `path` and names its base in `path_base`:

- `report_invocation`: this run's directory;
- `reports_root`: the report root, when a resumed run points at the original
  run's Record;
- `external_origin_target`: an original run under a different report root; the
  caller supplies that Target directory;
- `origin_target`: the Target directory (used inside `simulation.json` and
  `coverage.json`).

Copying a whole run directory, or the whole report root, keeps every reference
valid. Copying only a resumed run does not: its report points back into the
original run. Existing files are never migrated to a newer schema, and a
directory or schema from Booley 0.4.0 or earlier (`campaign/`,
`booley.coverage-campaign/v5`) is refused, not read.

If writing the final report fails, the run exits `2` with
`detail.completion_error`; results already written stay.

### Coverage Record files

A coverage run adds a reference at Target level and the Coverage Record inside
the attempt it measured:

```text
<report-root>/sim/<N>/targets/<target>/
  coverage.json               booley.coverage-record-reference/v1
  simulation.json             the completed Target results
  record/work-items/<item>/attempts/<attempt>/coverage-record/
    coverage.json             booley.coverage-record/v1, the Coverage Record manifest
    coverage-points.jsonl.gz  booley.coverage-points/v2: every point and which Test Runs hit it
    native/raw/               one Verilator database per Test Run
    native/merged/            the merged database
    hooks/                    hook evidence, when collected
```

The reference's `coverage_record` object has `path`, `path_base`, `schema`,
`coverage_record_id`, `bytes`, and `sha256`. Its `path_base: origin_target`
resolves `path` against the Target directory to the nested manifest; size,
digest, and identities authenticate it. Native paths inside the manifest
resolve from the **Coverage Record directory**, not the Target directory.

Give the Coverage Analyst the numbered Target-level `coverage.json`: it
resolves the reference and checks the enclosing Simulation Record and the
completed `simulation.json`, so the nested path alone is not a valid input.
Booley code reads a Coverage Record through
`booley.flows.sim.coverage_record_store`, which checks the point store's size
and digest; never open the point store directly. Keep the reference, the
attempt, and the enclosing Simulation Record together.

In `report.json`, `detail.targets[<target>]` keeps each Target's results and a
`coverage_record` reference to the Target-level `coverage.json`.
`coverage_metrics` maps each native metric to `{percent, threshold, met}`. A
threshold miss on a run that executed cleanly still exits `0`; with one
evaluated
Criterion, `criterion_met` is `false`.

## `lint`

`lint_report.json` is the full record. `report.json` `detail` holds a bounded
findings preview: `findings` (up to 20), `findings_total`,
`findings_truncated`, and, with several Targets, `findings_by_target`.

- A warning reported by several Targets shows its first Target in the preview
  but counts toward each of them in `findings_by_target`, so per-Target counts
  can sum past `findings_total`. Errors count once per Target.
- `passed` in `lint_report.json` is false whenever there are findings, even when
  `warnings_as_errors = false` lets the CLI exit `0`.

## `synth`

`synth_<slug>.json`, also published as `synth/<N>/targets/<slug>.json`, is the
per-Target report. Its `artifacts.live_dirs` are mutable work directories that
the next run overwrites; read the numbered copies of the STA reports
(`artifacts/synth_<slug>/timing/`) instead.

Actionable warnings (dispositions `advisory` and `structural`) set
`implementation.status.grade` to `warn` but keep `passed: true`. Located
frontend warnings are counted too and never set `warn`.

## `fpga`

`fpga_<slug>.json`, also published as `fpga/<N>/targets/<slug>.json`, is the
per-Target report; the tool is always Vivado. On an implementation-cache hit
(`cached: true`), `run_evidence` describes the run that produced the cached
results, and `cache_consumer_run_id` is this run's ID.

## `mutation`

Each run takes the next number `N` under `<report-root>/mutation/`. The
invocation `report.json` carries the verdict, and `campaign/` holds the
durable evidence of the one campaign:

```text
<report-root>/mutation/<N>/
  report.json
  campaign/
    manifest.json            run identity, baseline and per-Mutant status
    mutation-specs.md        every Mutant: operator, file:line, original, mutated
    mutation-results.md      the outcome of each Mutant
    baseline.log             pristine baseline simulator output
    mutant-logs/             one simulator log per tested Mutant
    variants/mutant_<i>/...  the mutated source of each Mutant
```

A run without a report directory keeps the variants only under the Booley
Directory's `.runtime/mutation/` scratch tree. The `mutation_score` Criterion
is satisfied by `detected >= min_detected`.
