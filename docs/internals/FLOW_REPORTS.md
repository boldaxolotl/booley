# Built-in Flow reports

This document defines the durable report files, JSON fields, and on-disk
layouts produced by the built-in `sim`, `lint`, `synth`, and `fpga` Flows. It is
for scripts and tools that consume Flow output and for Booley contributors.

| Document | Owns |
|---|---|
| [FLOW_REFERENCE.md](../user/FLOW_REFERENCE.md) | How to run each Flow and interpret its verdict |
| **This document** | Report locations, JSON schemas, and Campaign file layouts |
| [FLOW_IMPLEMENTATION.md](FLOW_IMPLEMENTATION.md) | How the Flows produce that evidence: planning, verdict rules, persistence and recovery transactions |
| [MCP-TOOLS.md](MCP-TOOLS.md#built-in-flow-calls) | How the same results reach an agent over MCP |

## Report locations

An explicit `--report-dir` is authoritative. Ticket and agent-driven runs use
their configured `<runtime>/flow-reports` root. Otherwise, every direct built-in
or Custom Flow writes beneath `<resolved-project-data>/flow-reports`. The selected
checkout determines Project data, including configured external and stealth
locations; Booley never falls back to writing `flow-reports/` in the RTL checkout.

Each Flow-specific durable report below includes `flow` and `timestamp` in
addition to its listed fields. The `synth` and `fpga` per-Target reports and
Criteria detail additionally carry the shared versioned `implementation`
envelope described in
[FLOW_IMPLEMENTATION.md](FLOW_IMPLEMENTATION.md#shared-implementation-report-envelope):
policy-resolved grade, identity, QoR metrics, recipe and provenance evidence,
baseline comparison, cache state, and immutable artifact pointers.

## Invocation report

Each invocation writes one per-invocation report (`report.json`), which MCP
also attaches to its result:

| Field | Contents |
|---|---|
| `flow`, `target`, `argv` | Flow identity, the requested Target selector, and parsed invocation arguments. |
| `exit_code`, `passed` | Overall graded result. |
| `criterion_key`, `criterion_met` | Criterion result when one invocation maps to one Criterion; these can be empty/false for aggregate runs. |
| `timestamp`, `elapsed_s`, `slug` | Run time, duration, and Ticket slug (empty outside a Ticket). |
| `detail` | Flow-specific aggregate data and artifact pointers. |
| `eda_tool`, `run_id`, `report_text` | Present when the run resolved an EDA tool, has a dispatched-job identity, or emitted a report card. |
| `usage` | Present for token-using endpoints, with `input_tokens`, `output_tokens`, `cached_tokens`, `cache_create_tokens`, and `cost_usd`. |

The direct CLI always publishes the final human-readable Flow verdict,
independently of whether Development State is configured. If a Flow already
printed the same complete verdict block during its run, Booley does not print a
second copy.

## Progress

Long-running Simulation, ASIC Synthesis, and FPGA Implementation invocations also
write a run-scoped `progress.json`. `complete: true` means the producer is
terminal, not necessarily successful. `phase: complete` means every planned
Target was processed; `phase: aborted` means the invocation stopped with the
listed `pending_targets`.

`phase: superseded` appears only on an interrupted coverage run that was later
resumed. The resume runs as a new invocation, and `superseded_by` names it
(`invocation` number and, when known, `run_id`). A superseded run's Target lists
still show its state when it was interrupted; check the resuming invocation's
`progress.json` for the outcome. Marking is best effort: if it fails, the
original run keeps its `running` or `aborted` phase.

Every new checkpoint includes `run_id` and `timestamp`.

The lifecycle, locking, and repair rules are in
[FLOW_IMPLEMENTATION.md](FLOW_IMPLEMENTATION.md#progress-lifecycle).

## Dry-run plan

Every built-in dry-run returns a JSON `FlowPlan` with `schema_version`, `flow`,
`mode`, `semantic_plan_fingerprint`, ordered `work_units`, `aggregate_errors`,
and `planning_disclosures`. Each work unit identifies its Target and revision
role, timeout, sources and constraints, resolved parameters and recipe, ordered
command argv, and expected artifacts. Paths are relative to `work_dir` where
possible; ambient environment values and secrets are excluded.

Dry-run never acquires a heavy execution slot, runs an EDA tool or Pre-Sim
Commands, changes timeline or Criteria state, records Criterion evidence,
populates implementation caches, or writes a normal verdict report. FuseSoC
setup and declared generators may run when authoritative resolution requires
them, using disposable scratch; this possibility is named in
`planning_disclosures` and the scratch is removed afterward.

The JSON plan is printed first on stdout. A successful dry run follows it with
its concise verdict summary on stdout; a failing one prints its planning-failure
reason on stderr. Dry-run atomically writes only the distinct
`<selected-report-root>/<flow>/flow_plan.json` artifact, using the same report
root precedence as a real invocation, and never reserves a numbered directory.
The `semantic_plan_fingerprint` excludes scratch and invocation-local paths so
the same prepared dry and real execution have the same semantic identity.

FPGA work units include the resolved part, top, XDC, source inputs, recipe, and
the explicitly marked Vivado Make command template. A later Target error blocks
execution but does not hide valid earlier work units from the plan.

## `sim`

### Simulation Campaign layout

Every `sim` run uses the same layout, whatever the testbench (HDL or cocotb) and
whether or not it collects coverage. Each run reserves the next number in the
`sim/` sequence under the report root (`<resolved-project-data>/flow-reports` by
default), and each Target gets its Simulation Campaign at
`<report-root>/sim/<N>/targets/<encoded-target>/campaign/manifest.json`, with
append-only attempts/results beneath it and an atomically regenerated
`summary.json`. Qualified Target selectors are percent-encoded into one
directory component.

Three cases add to this layout:

- **Coverage** adds `coverage.json`, the point store, and `native/` beside
  `campaign/` in the same Target directory (see
  [Coverage Campaign files](#coverage-campaign-files)).
- **Resume** reserves a new number for its own `report.json` and
  `progress.json`, but writes Campaign results beside the *original* manifest,
  so one Campaign stays in one place.
- **A Cycle Count baseline** gets its own Target directory next to the
  candidate's, named from `<target>@baseline-<first 12 hex of revision>` and
  encoded like any selector.

```text
targets/<encoded-target>/
  campaign/
    manifest.json
    summary.json
    build-variants/<digest>/attempts/<attempt>/
      build-attempt.json
      build-result.json
      evidence/bundle.json            authenticated shared Simulator Bundle
    work-items/.../attempts/...       append-only attempts and results
  simulation.json                    versioned Target-local projection
  coverage.json                      optional authenticated coverage reference
```

Manifests, bundles, attempts, results, summaries, and nested coverage references
bind one another by exact identity, byte count, and digest. Resume validates that
chain and the current Target revision/workload before launching an EDA tool.

Each Simulation Campaign freezes the Target's Required Simulation Suite in its
immutable manifest. A registered Target with no named suite instead requires its
one default-selection work item to pass. Changing the suite or its source
fingerprint makes an old manifest ineligible for resume rather than applying
historical results to the new suite.

### Schema versions and references

New `simulation.json` files use `booley.simulation-projection/v2`. Their manifest
and optional Coverage pointers are typed `origin_target` references with a
normalized relative path, byte count, digest, artifact kind, and Simulation
Campaign owner. Legacy absolute manifest and summary strings remain readable only
as hints after the supplied local Simulation Campaign has authenticated; readers
never follow them back to the producer path.

Versioned `report.json` files use `booley.simulation-report/v2`. Each Target has
one `artifacts` map whose references use `report_invocation` for local artifacts
or `reports_root` for an origin Simulation Campaign under the same reports root.
A cross-root resume uses `external_origin_target`; its caller supplies the origin
Target directory when resolving that external dependency. A resume report
declares `dependency: external_origin_campaign` and publishes no local
`simulation.json`. Copying a complete invocation preserves local references;
copying a reports root preserves same-root resume references. Copying only a
resume invocation leaves its immutable Simulation Campaign identity, digest, and
external relative path, but not the external artifact bytes.

Flat `sim_<target>.json` files and the earlier `targets/sim_<target>.json`
copies are no longer written. Use the exact `artifacts[target].report` path in
the numbered invocation's `report.json`. See the
[Simulation Campaign migration guide](../user/SIMULATION_CAMPAIGN_MIGRATION.md).

If completion reporting fails, the Flow returns exit code 2 with a structured
`detail.completion_error` while preserving existing Target, Campaign, and
Criterion results.

### `simulation.json`

`sim/<N>/targets/<encoded-target>/simulation.json`:

| Field | Contents |
|---|---|
| `target`, `target_identity`, `tb_top`, `eda_tool` | Callable Target selector, durable Target identity, and resolved simulation context. |
| `passed`, `complete`, `elapsed_s` | Target-level verdict, whether terminal publication completed, and execution duration. An interrupted publication leaves `complete: false` as an explicitly recoverable checkpoint. |
| `phase_timings_s` | Target aggregation of `setup` (including Target metadata resolution), `pre_sim`, `build`, `run`, and `result_processing`, plus `unattributed` overhead and `execution_total`. Persisted results also include `publication` and the resulting end-to-end `total`. Run-level structured detail separately exposes `resolution_s` for campaign selection and test-map resolution. |
| `tests[]` | Per-test `name`, `passed`, `verdict`, `timed_out`, `elapsed_s`, `build_s`, `cycles`, `cycle_observation`, `sva_errors`, `error_tail`, `test_validated`, `phase_timings_s`, and `resources`. `resources` contains `command_peak_rss_mb` and `command_oom_kill_delta`; supported platforms also add `simulation_user_cpu_s` and `simulation_system_cpu_s`. Trace runs add `trace_path`, `trace_bytes`, `trace_top_scope`, `trace_signal_count`, and `trace_total_ticks`. Optional fields include `artifacts.run_log`, `workload_fingerprint`, and `validation_note`. |
| `compile_command`, `fileset` | Best-effort generated command and resolved `rtl`/`tb` source lists. |
| `artifacts` | The report, fresh per-test run logs, result files, and trace artifacts that exist for this run. |

Elaboration Check output uses the same canonical `simulation.json` name and sets
`mode` to `elab_only` (or `elab_only_standalone` for the cumulative mode):

| Field | Contents |
|---|---|
| `target`, `target_identity`, `eda_tool`, `toplevel` | Resolved Simulation Target identity. |
| `passed`, `verdict`, `failure_class`, `reason`, `elapsed_s` | Target-level graded outcome and duration. |
| `compile_command`, `fileset` | Generated build command and resolved `rtl`/`tb` source lists when setup succeeded. |
| `log` | Complete archived build log. |

For `mode=elab_only_standalone` the invocation report also carries
`detail.standalone` with `modules_checked`, `shared_files`, `frontend`,
`failures`, optional `unparsed` modules, and the standalone log pointer.

### Coverage Campaign files

```text
<reports>/sim/<number>/
  report.json
  progress.json
  targets/<encoded-target>/
    coverage.json
    coverage-points.jsonl.gz
    simulation.json
    native/raw/
    native/merged/
    ... hook and queryability evidence
```

Coverage progress uses the shared terminal lifecycle. It carries
`coverage: true`, the invocation `run_id`, its latest `timestamp`, the exact
Target partition, and per-Target detail. An interrupted or failed invocation
preserves already durable Targets and leaves the failed or unstarted Targets
pending.

Structured `detail.targets[selector]` retains each Target's `simulation`,
`collection`, and `evaluation` truth even when a later publication failure or
another Target dominates the exit code. The canonical `coverage_campaign`
reference appears only after its public reference was successfully published
and authenticated; failures before that publication omit it, while a failure at
the subsequent `after:coverage_reference` checkpoint retains it.

New `coverage.json` manifests use `booley.coverage-campaign/v4`. They retain V3's
fingerprints, verdicts, capabilities, evaluation, and valid rollups, and add
required `scoring`: complete, compatible collection uses `valid` with a null
reason; every other collection status uses `invalid` with that status as its
reason and empty overall/source rollups. Compatible points and available
native/hook evidence remain diagnostic; incompatible native evidence has no
normalized points. Source rollups cover line, branch, expression, and toggle
metrics, use the same eligibility and waiver rules as overall rollups, and group
by source path rather than hierarchy. They are persisted only in
`coverage.json`; the Simulation response remains compact and points to that
file.

Required `coverage-points.jsonl.gz` stores lossless point identities and sparse
positive hit incidence; the manifest binds it by schema, exact relative path,
compressed and uncompressed byte counts, point count, and SHA-256. Valid V3
remains readable; score-bearing invalid V3 and all V1/V2 Campaigns require
recollection. Native artifact paths are relative to the Target directory. New
Simulation report references are relative to their containing report invocation
or reports root, never to the producing work directory.

### Coverage retention

`python -m booley.flows.sim.campaign_retention` prunes one exact invocation.
The algorithm and locking are in
[FLOW_IMPLEMENTATION.md](FLOW_IMPLEMENTATION.md#exact-report-retention); the
observable behavior is:

- **Native-only** (`--native-target`) removes that Target's raw and merged
  databases while retaining the immutable Campaign manifest and point store,
  Simulation, and hook evidence. Target-local `availability.json` records
  `pruning` or `pruned`; normalized evidence remains analyzable. It exits `2` for
  ambiguous, unsafe, changed, missing, or unrecognized payloads and never
  requires `--project-data`.
- **Full** (`--full`) removes the exact invocation's reports and native payloads;
  re-analysis is impossible. An empty `.pruned-N` tombstone reserves its number.
  It does not require recorded native payloads to remain unchanged or present,
  but exits `2` and names any file the invocation did not produce, leaving the
  invocation untouched. It also retires the Campaign's Project-local
  child-execution records: for abandoned Campaigns with surviving external
  resources it first performs bounded recovery/cancellation of authenticated
  orphan child processes and marker-checked cleanup of owned templated run
  directories. It does not require Project data when no external resource
  survives. Literal user-supplied run directories are never removed.
- **Project data.** When `REPORTS_ROOT` is `<project-data>/.runtime/flow-reports`
  (runtime-scoped execution) or `<project-data>/flow-reports` (direct execution),
  Booley infers the project-data root; the direct layout is accepted only when
  its parent is the currently resolved Project data directory. Otherwise, if the
  invocation contains Campaign child records, `--full` needs
  `--project-data "$PROJECT_DATA"` with the exact resolved project-data root.
- **Dependents.** Full pruning refuses before mutation when later resume
  invocations depend on the selected origin. `--include-dependents` removes those
  authenticated dependents and the origin in one retryable operation. If a
  dependent cannot be authenticated, prune it directly and retry the origin.
- **Active, abandoned, invalid.** If the producer still owns its invocation lock,
  or an exact resume owns a Campaign mutation lock, wait for it to finish; active
  selections exit `2`. An authenticated pending or interrupted Campaign, an
  unpublished summary, or an absent Simulation projection is abandoned and may be
  discarded with `--full`; native-only pruning instead points to `--full` or the
  exact `booley flow sim --resume-from <manifest>` command. An empty producer
  reservation abandoned before `progress.json` is likewise discardable with
  `--full` when its external invocation lock remains intact. Malformed,
  contradictory, linked, foreign, or unrecognized content remains undeletable.

Selection is validated before deletion. Retry an interrupted cleanup with the
same exact selection. No age, size, or latest heuristic deletes evidence
automatically.

## `lint`

`lint_report.json`:

| Field | Contents |
|---|---|
| `targets`, `eda_tools` | Requested Targets and the linter resolved for each. |
| `passed`, `elapsed_s`, `total_warnings` | Lint-clean status, duration, and deduplicated in-scope finding count. `passed` is false when warnings exist even if `warnings_as_errors = false` lets the direct CLI exit `0`. |
| `warnings[]` | Deduplicated in-scope `rule`, `file`, `line`, and `message` record for each finding. |
| `errors[]` | `target` and `message` for each Target that could not produce a lint verdict. |
| `target_results[]` | Per-Target `target`, `eda_tool`, raw `warnings` count before cross-Target deduplication and `--scope`, `files_linted`, `toplevel`, `toplevel_linted`, `duration_s`, `error`, and `log`. |
| `artifacts` | The durable report and per-Target run logs. |

## `synth`

`synth_<target>.json` (qualified selectors use a sanitized, hash-suffixed
filename):

| Field | Contents |
|---|---|
| `target`, `eda_tool`, `synth_mode` | Resolved synthesis identity and logical/physical methodology. |
| `passed`, `elapsed_s`, `returncode`, `timed_out`, `termination`, `infra_error`, `has_metrics` | Verdict, duration, and terminal classification. |
| `yosys_complete`, `timing_complete`, `structural_checks_complete`, `ppa_complete`, `peak_rss_mb` | Completion and resource evidence. |
| `area_um2`, `area_source`, `area_kge`, `cells` | Canonical area and cell metrics. |
| `per_clock`, `wns_ns`, `whs_ns`, `reg2reg_slack_ns`, `reg2reg_fmax_mhz` | Physical-mode timing. Each `per_clock` entry contains `period_ns`, `wns_ns`, `whs_ns`, `critical_path_ps`, and `fmax_mhz`; logical mode instead adds `estimated_fmax_mhz`. |
| `conditions` | `latches`, `expected_latches`, `unexpected_latches`, `comb_loops`, `multi_driven`, and the combined `has_critical` verdict. |
| `total_warnings`, `warning_summary` | Total warning-record occurrences plus unique and grouped counts by EDA tool, category, and disposition, with bounded representative diagnostics. Repeated warnings remain visible in the total; `unique_warnings` groups identical records. |
| `baseline`, `delta_pct`, `timing_delta_pct` | Optional baseline metrics and deltas; `baseline.ref` identifies the compared revision. |
| `baseline_target`, `candidate_target` | Callable selector compatibility fields for the baseline and candidate Targets. |
| `baseline_target_identity`, `candidate_target_identity` | Durable FuseSoC identities for the baseline and candidate Targets. |
| `run_evidence`, `baseline_run_evidence` | Current and optional baseline source/recipe provenance. |
| `failure_output`, `io_bound_critical` | Optional failure excerpt and I/O-bound timing indicator. |
| `artifacts` | The durable report, complete run log, build directory, and physical-mode timing directory. |

Actionable warnings produce `grade: "warn"` while keeping `passed: true`.
Explicitly benign warnings remain counted with a rationale and do not downgrade
the grade. Open `artifacts.log` for every raw diagnostic when the bounded
representatives are insufficient.

## `fpga`

`fpga_<target>.json`:

| Field | Contents |
|---|---|
| `target`, `eda_tool` | Resolved implementation identity; the EDA tool is Vivado. |
| `passed`, `returncode`, `timed_out`, `infra_error` | Verdict and terminal classification. |
| `cached`, `cache_fingerprint` | Whether implementation evidence was reused and the cache identity. |
| `metrics` | Current-run `lut_count`, `ff_count`, `bram_count`, `dsp_count`, `wns_ns`, `whs_ns`, `per_clock`, `latches`, `comb_loops`, `multi_driven`, `elapsed_s`, `cached`, `cache_fingerprint`, `failure_output`, `log_path`, and nested `artifacts`. Each clock contains `period_ns`, `wns_ns`, `whs_ns`, `critical_path_ps`, and `fmax_mhz`. |
| `baseline_ref`, `baseline_metrics` | Optional baseline revision and the same metrics from `--baseline`, without baseline artifact pointers. |
| `recipe_fingerprint`, `recipe_snapshot`, `run_evidence` | Normalized recipe and provenance for the current run. |
| `baseline_recipe_fingerprint`, `baseline_recipe_snapshot`, `baseline_run_evidence` | Optional baseline recipe and provenance. |
| `cache_consumer_run_id` | Present when this run consumes cached evidence produced by another run. |
| `baseline_target`, `candidate_target` | Callable selector compatibility fields for the baseline and candidate Targets. |
| `baseline_target_identity`, `candidate_target_identity` | Durable FuseSoC identities for the baseline and candidate Targets. |
| `artifacts` | The durable report, complete run log, and build, synthesis, and implementation directories. |

The PPA profile's Vivado strategy mapping is internal adapter evidence, not a
raw public knob. Baseline and candidate Targets may carry different persistent
profiles, but basis-bound comparisons reject differing measurement recipes.
