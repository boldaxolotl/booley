# Booley Flow reference

This reference describes the public behavior of Booley's built-in deterministic
Flows: `sim`, `lint`, `synth`, and `fpga`. It explains what to invoke,
what each result means, and which reports and artifacts to inspect. For exact
project configuration, see [CONFIG.md](CONFIG.md); for compatible EDA programs
and versions, see [SUPPORTED-EDA-TOOLS.md](SUPPORTED-EDA-TOOLS.md).

## Using a Flow

In normal work, ask the Interactive or Developer Agent to run the appropriate
Flow and always name the Target. For `sim`, also name the test or explicitly ask
for the Target's full registered test suite. To reproduce or diagnose a run
yourself inside the Sandbox, use the direct CLI:

```bash
booley flow lint --target lint_soc
booley flow sim --target sim_core --target sim_peripheral,sim_soc --test reset
booley flow synth --target synth_soc
booley flow <name> --help
```

Every new Target-aware call requires `--target`; Booley has no project-wide default.
The one exception is `sim --resume-from`, which reconstructs its Target and
workload from the named immutable Simulation Campaign manifest and rejects a
simultaneous Target.
Use `booley targets` to list available Targets and
`booley targets --for-flow <flow>` to narrow the list. If two cores expose the
same Target name, use the qualified selector printed by `booley targets`, such as
`lowrisc:ibex:ibex_top#lint`.

Common controls:

- `--target <name,...>` selects one or more configured Targets. Repeat the flag,
  use comma-separated values, or mix both forms; for example,
  `--target a --target b,c` selects `a`, then `b`, then `c`. Caller order is
  preserved. Selecting the same resolved Target twice, including through two
  different selector spellings, is an error. MCP keeps one comma-separated
  `target` string rather than an array.
- `--work-dir <path>` selects the project/worktree root; it defaults to the
  current directory.
- `--report-dir <path>` persists the invocation report and Flow-specific reports
  under that directory.
- `--diagnostic` runs without satisfying Ticket Criteria. A strict Ticket
  requires it when the Flow/Target pair is outside the Ticket baseline.
- `--dry-run` resolves and validates the requested work and prints the same
  normalized plan shape for every built-in Flow. It does not run EDA or
  Pre-Sim Commands and does not update Booley-managed durable state.
- `--timeout-ms <positive-integer>` sets the active-time budget for each Flow
  work unit. It overrides `[flows.<name>].timeout_ms`, which overrides the
  workload-specific default. Queue time is not charged. The old `--timeout`
  spelling remains a deprecated CLI-only alias for one compatibility window.
- `booley flow <name> --help` is the authoritative argument list.

## Shared result contract

All built-in Flows use the same exit-code grades:

| Exit | Meaning |
|---:|---|
| `0` | The Flow ran and its requested condition passed. |
| `1` | The Flow reached a design verdict and the requested check failed. |
| `2` | The Flow could not reach a design verdict because configuration, infrastructure, or execution failed. |

Exit `1` is evidence about the RTL or testbench. Advisory findings can still
exit `0`, for example lint warnings with `warnings_as_errors = false` or synthesis
timing violations when timing is not configured to gate the result. Exit `2`
means the Flow did not produce a trustworthy complete design result.
Agent-facing MCP calls carry the same grade in `EXIT_CODE:` and structured
output; MCP `isError` is not the design verdict.

The direct CLI always publishes the final human-readable Flow verdict,
independently of whether Development State is configured. Successful verdicts
use stdout; failed and rejected diagnoses use stderr. If a Flow already printed the same complete verdict block during its
run, Booley does not print a second copy.

An agent-facing MCP call attaches its per-invocation report as
`structuredContent.reports[0]`. The report contains:

| Field | Contents |
|---|---|
| `flow`, `target`, `argv` | Flow identity, the requested Target selector, and parsed invocation arguments. |
| `exit_code`, `passed` | Overall graded result. |
| `criterion_key`, `criterion_met` | Criterion result when one invocation maps to one Criterion; these can be empty/false for aggregate runs. |
| `timestamp`, `elapsed_s`, `slug` | Run time, duration, and Ticket slug (empty outside a Ticket). |
| `detail` | Flow-specific aggregate data and artifact pointers. |
| `eda_tool`, `run_id`, `report_text` | Present when the run resolved an EDA tool, has a dispatched-job identity, or emitted a report card. |
| `usage` | Present for token-using endpoints, with `input_tokens`, `output_tokens`, `cached_tokens`, `cache_create_tokens`, and `cost_usd`. |

`structuredContent.passed` repeats the overall boolean verdict. If the report is
too large for the MCP result, `reports` is empty, `truncated` is `true`, and the
result retains the Flow, Target, exit code, and artifact pointers needed to open
the durable report.

Long-running Simulation, ASIC Synthesis, and FPGA Implementation invocations also
write a run-scoped `progress.json`. `complete: true` means the producer is
terminal, not necessarily successful. `phase: complete` means every planned
Target was processed; `phase: aborted` means the invocation stopped with the
listed `pending_targets`; and coverage resume can mark the authenticated origin
`phase: superseded` with a bounded `superseded_by` identity. A superseded
origin's Target lists are historical and are deliberately not rewritten from the
recovered Campaign. Every new checkpoint includes `run_id` and `timestamp`.

For MCP fallback evidence, `partial` is true whenever the phase is not `complete`
or pending Targets remain. Thus `aborted` and `superseded` are terminal but
partial, and neither appears as a running checkpoint. After timeout or
cancellation, the MCP supervisor attempts an idempotent `aborted` repair only
after it has reaped the child process; a missing or unwritable checkpoint does
not override the Job's exit or cancellation result.

### Dry-run plan

Every built-in dry-run returns a JSON `FlowPlan` with `schema_version`, `flow`,
`mode`, `semantic_plan_fingerprint`, ordered `work_units`, `aggregate_errors`,
and `planning_disclosures`. Each work unit identifies its Target and revision
role, timeout, sources and constraints, resolved parameters and recipe, ordered
command argv, and expected artifacts. Paths are relative to `work_dir` where
possible; ambient environment values and secrets are excluded.

Dry-run exits `0` only when the aggregate plan is valid. It exits `2` when any
selected Target or baseline cannot be planned, while retaining successfully
planned work units for diagnosis. It never acquires a heavy execution slot,
runs an EDA tool or Pre-Sim Commands, changes timeline or Criteria state, records
Criterion evidence, populates implementation caches, or writes a normal
verdict report. FuseSoC setup and declared generators may run when authoritative
resolution requires them, using disposable scratch; this possibility is named
in `planning_disclosures` and the scratch is removed afterward.

The JSON plan is printed first on stdout. A successful dry run follows it with
its concise verdict summary on stdout. A failing dry run instead prints its
concise planning-failure reason on stderr, while still writing no normal
verdict report.

Dry-run atomically writes only the distinct
`<selected-report-root>/<flow>/flow_plan.json` artifact. It uses the same report
root precedence as a real invocation and never reserves a numbered directory. The
`semantic_plan_fingerprint` excludes scratch and invocation-local paths so the
same prepared dry and real execution have the same semantic identity.

An explicit `--report-dir` is authoritative. Ticket and agent-driven runs use
their configured `<runtime>/flow-reports` root. Otherwise, every direct built-in
or Custom Flow writes beneath `<resolved-project-data>/flow-reports`. The selected
checkout determines Project data, including configured external and stealth
locations; Booley never falls back to writing `flow-reports/` in the RTL checkout.
The per-Flow fields below describe the Flow-specific durable reports. Each includes
`flow` and `timestamp` in addition to the fields listed below.

The `synth` and `fpga` per-Target reports and Criteria detail additionally carry
the shared versioned `implementation` envelope. It contains the policy-resolved
grade, identity, QoR metrics, recipe and provenance evidence, baseline
comparison, cache state, and immutable artifact pointers described in the
implementation reference.

## `sim`

`sim` builds and runs the tests registered for a simulation Target. The Target
selects Verilator or Icarus and whether the testbench is HDL or cocotb.

Useful controls:

- `--mode simulate` builds and runs selected tests (the default).
- `--mode elab-only` compiles, elaborates, and links the ordinary untraced
  simulator image without running tests.
- `--mode elab-only-standalone` performs ordinary Target elaboration first,
  then adds the stronger reusable-module sweep.
- `--elab-only`, `--build-only`, and their combination with `--standalone`
  remain deprecated CLI-only aliases for one compatibility window.
- `--test <name>` selects one registered test by exact name and may be repeated.
- `--tests-file <path>` selects exact names from a UTF-8 file, one per line;
  blanks and `#` comments are ignored. It cannot be combined with `--test`.
- Duplicate, unknown, empty, or catalog-less named selections fail before
  simulation. Every explicit name must exist in every selected Target.
- A plain unfiltered run applies configured `skip` entries. Repeatable `--test`
  or `--tests-file` is an exact explicit suite and therefore overrides those
  entries. A plain Target whose complete registered suite is configured skipped
  fails preflight instead of passing vacuously.
- There is no CLI `--skip` option. Put long-lived exclusions in `tests.toml`,
  or name the exact suite to run with repeated `--test` options or
  `--tests-file`.
- `--trace` captures a waveform artifact.
- `--coverage` (permanent alias `--cov`) explicitly collects a native Verilator
  Coverage Campaign. MCP uses boolean `coverage: true`; the default is false.
- `--result-verbosity <compact|full>` selects cocotb console detail and defaults
  to `compact`; `full` prints every XML testcase entry. Complete XML and JSON
  artifacts are retained in either mode.
- `--no-kill` skips the pre-run zombie-process cleanup; this is a diagnostic
  escape hatch, not a normal simulation control.
- `--resume-from <manifest.json>` validates and resumes that exact durable
  Simulation Campaign. It cannot be combined with Target, test, mode, coverage,
  or trace selection: those values are reconstructed from the immutable
  manifest. Timeout and presentation controls may change. `--dry-run` reports
  completed, interrupted, and pending work without admission or mutation.

Ordinary HDL, Cocotb-batch, and native-coverage-aggregate executions publish
their resume authority at
`<report-root>/sim/<N>/targets/<encoded-target>/campaign/manifest.json`, with
append-only attempts/results beneath it and an atomically regenerated
`summary.json`. Direct Simulation, native coverage, and resume all use
`<resolved-project-data>/flow-reports` by default and reserve numbers from the
same `sim/` sequence. A resume creates a new compatibility invocation but
keeps authoritative Simulation Campaign writes beside the original manifest.
Cocotb interruption retries its whole batch as one new Simulation Attempt while
retaining independent XML-derived observations. Native coverage interruption
retries its whole serial collection/merge aggregate as one new Simulation
Attempt and creates a distinct nested Coverage Campaign; it never continues or
overwrites an interrupted native database.

The CLI prints each exact manifest path before admitting simulator work. Its
final card gives the strict grade and keeps the manifest path needed to resume:

```bash
booley flow sim --target sim_soc --test reset --test interrupts
booley flow sim --resume-from \
  "$PROJECT_DATA/flow-reports/sim/12/targets/sim_soc/campaign/manifest.json"
```

The MCP `sim` input deliberately uses an array, not the former scalar shape:

```json
{"target":"sim_soc","test":["reset","interrupts"]}
```

MCP has no `tests_file` or `skip` property. It accepts the same exact ordered
test names directly in `test`; `resume_from` names one manifest and conflicts
with `target`, `test`, explicit `mode`, `coverage`, and `trace`.

The authoritative files remain beside the original manifest when resume creates
a later report-only invocation:

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

Do not edit, copy into place, or repair Campaign JSON manually. Manifests,
bundles, attempts, results, summaries, and nested coverage references bind one
another by exact identity, byte count, and digest. Resume validates that chain
and the current Target revision/workload before launching an EDA tool.

New `simulation.json` files use `booley.simulation-projection/v2`. Their manifest
and optional Coverage pointers are typed `origin_target` references with a
normalized relative path, byte count, digest, artifact kind, and Simulation
Campaign owner.
Legacy absolute manifest and summary strings remain readable only as hints after
the supplied local Simulation Campaign has authenticated; readers never follow
them back to the producer path.

Versioned `report.json` files use `booley.simulation-report/v2`. Each Target has
one `artifacts` map whose references use `report_invocation` for local artifacts
or `reports_root` for an origin Simulation Campaign under the same reports root.
A cross-root resume uses `external_origin_target`; its caller supplies the origin
Target directory when resolving that external dependency. A resume report declares
`dependency: external_origin_campaign` and publishes no local `simulation.json`.
Copying a complete invocation preserves local references; copying a reports root
preserves same-root resume references. Copying only a resume invocation leaves its
immutable Simulation Campaign identity, digest, and external relative path, but
not the external artifact bytes.

Structured campaign output reports `grade`, `complete`,
aggregate `observation_counts`, and a maximum-32 `observations` preview. Every
preview entry retains `test`, `execution`, `functional`, `assertions`,
`assertion_count`, and bounded `detail`; `observation_total` and
`observations_truncated` disclose whether the preview is complete.
The independent observation axes mean:

- `execution`: whether the simulator process completed, timed out, or failed
  before producing trustworthy test evidence;
- `functional`: the pass/fail/inconclusive test verdict;
- `assertions`: assertion evidence independently observed for that test.

Resolve the `manifest` artifact reference, then inspect its authenticated terminal
results for the complete durable record; the MCP preview is intentionally not a
replacement for those files.

Simulation Campaign scheduling uses the admitted Simulation Job as one heavy
lane. With `[jobs].max_heavy = 1` execution is serial. Higher caps allow at most
`max_heavy` simulator processes across that Project, including the borrowed
outer lane; work sharing a literal `run_cwd` still serializes to prevent
cross-talk. Templated attempt directories can overlap safely.

Existing CLI, MCP, and report consumers should follow the concise
[Simulation Campaign migration guide](SIMULATION_CAMPAIGN_MIGRATION.md).

Each Simulation Campaign freezes the Target's Required Simulation Suite in its immutable
manifest. A target-level `sim_pass_<target>` Criterion is eligible to pass only
when every member of that frozen suite has a durable passing result; selecting
and passing a subset does not satisfy the target-level Criterion. A registered
Target with no named suite instead requires its one default-selection work item
to pass. Changing the suite or its source fingerprint makes an old manifest
ineligible for resume rather than applying historical results to the new suite.

HDL testbenches report their outcome through configured pass/fail sentinels;
cocotb Targets use cocotb's result file, with assertion output still able to
fail the run. Fail sentinels take priority. A clean process that produces no
valid verdict is `inconclusive`, never a pass. A traced run is likewise
inconclusive when it cannot confirm a fresh trace artifact.

The Flow records per-test verdicts and can satisfy `sim_pass_<target>` and
configured per-test Cycle Count Criteria. It also records
`elab_pass_<target>` from an authenticated successful build before simulation
starts, so a later runtime failure cannot erase successful elaboration evidence.
Infrastructure failure before or during the build leaves that Criterion
unchanged.

Elaboration Check mode skips Pre-Sim Commands, test selection, Cocotb Python,
run guards, sentinels, and tracing. Run-only arguments such as `--test`,
`--tests-file`, `--trace`, `--result-verbosity full`, and `--no-kill` are rejected in
this mode. Only Simulation Targets are eligible. A compiler diagnostic that
proves the RTL was rejected is exit `1`; setup, missing-tool, timeout, OOM,
signal/crash, filesystem, and ambiguous nonzero failures are exit `2` and do
not change Criteria. Multi-Target checks continue through every Target, with
an infrastructure error taking precedence over a design failure.


Simulation report migration: flat `sim_<target>.json` files and the earlier
`targets/sim_<target>.json` copies are no longer written. Use the exact
`artifacts[target].report` path in the numbered invocation's `report.json`.
Qualified Target selectors are percent-encoded into one directory component.
Old scripts should consume that pointer instead of guessing a filename.

Structured output (`sim/<N>/targets/<encoded-target>/simulation.json`):

| Field | Contents |
|---|---|
| `target`, `target_identity`, `tb_top`, `eda_tool` | Callable Target selector, durable Target identity, and resolved simulation context. |
| `passed`, `complete`, `elapsed_s` | Target-level verdict, whether terminal publication completed, and execution duration. An interrupted publication leaves `complete: false` as an explicitly recoverable checkpoint. |
| `phase_timings_s` | Target aggregation of `setup` (including Target metadata resolution), `pre_sim`, `build`, `run`, and `result_processing`, plus `unattributed` overhead and `execution_total`. Persisted results also include `publication` and the resulting end-to-end `total`. Run-level structured detail separately exposes `resolution_s` for campaign selection and test-map resolution. |
| `tests[]` | Per-test `name`, `passed`, `verdict`, `timed_out`, `elapsed_s`, `build_s`, `cycles`, `cycle_observation`, `sva_errors`, `error_tail`, `test_validated`, `phase_timings_s`, and `resources`. `resources` contains `command_peak_rss_mb` and `command_oom_kill_delta`; supported platforms also add `simulation_user_cpu_s` and `simulation_system_cpu_s`. Trace runs add `trace_path`, `trace_bytes`, `trace_top_scope`, `trace_signal_count`, and `trace_total_ticks`. Optional fields include `artifacts.run_log`, `workload_fingerprint`, and `validation_note`. |
| `compile_command`, `fileset` | Best-effort generated command and resolved `rtl`/`tb` source lists. |
| `artifacts` | The report, fresh per-test run logs, result files, and trace artifacts that exist for this run. |

Elaboration Check structured output uses the same canonical `simulation.json` name and
sets `mode` to `elab_only` (or `elab_only_standalone` for the cumulative mode):

| Field | Contents |
|---|---|
| `target`, `target_identity`, `eda_tool`, `toplevel` | Resolved Simulation Target identity. |
| `passed`, `verdict`, `failure_class`, `reason`, `elapsed_s` | Target-level graded outcome and duration. |
| `compile_command`, `fileset` | Generated build command and resolved `rtl`/`tb` source lists when setup succeeded. |
| `log` | Complete archived build log. |

When `mode=elab_only_standalone` is requested, the invocation report also carries
`detail.standalone` with `modules_checked`, `shared_files`, `frontend`,
`failures`, optional `unparsed` modules, and the standalone log pointer.
The sweep can satisfy `elaborate_standalone`; an unavailable or untrustworthy
probe is exit `2` and leaves its prior Criterion state unchanged.

### Native Coverage Campaigns

```bash
booley flow sim --target sim_soc --coverage
booley flow sim --target sim_soc --cov --trace
booley flow coverage_analyst --campaign <reports>/sim/12/targets/sim_soc/coverage.json
```

Collection requires simulation mode and Verilator for every selected Target.
Selecting Icarus, even alongside a Verilator Target, rejects the whole invocation
before build or report paths are created. Targets and tests run sequentially in
stable order, with one simulator process per test, including Cocotb. Normal,
trace, coverage, and trace+coverage builds have separate cache identities.

A Criterion never activates collection. Without one, the full runnable selected
suite produces the same durable Campaign with evaluation `not_requested`, without
loading waivers or updating Coverage Criteria. Explicit invocation test selection
wins over the Criterion's exact suite, which wins over the full registered suite.
A different explicit suite still collects evidence but blocks gated evaluation.

Gated evaluation matches Approved Waivers transactionally per Target. For each
collected Target, only approvals naming that Target are checked against its
Campaign; one invalid point approval blocks that Target's evaluation and prevents
all approvals for that Target from applying. Approvals naming a known Target that
is not in the invocation are not checked against points by that run. Unknown
Target identities are still rejected when the Approved Waiver Set is loaded.

Only a durably persisted `pass` satisfies `coverage_<target>`. Simulation failure,
collection completeness, and policy evaluation remain independent: a failing
simulation can produce valid passing coverage, and passing simulation can miss a
threshold. Exit precedence is `2` for Coverage Preflight, collection, infrastructure,
persistence, incompatible-format, or blocked-evaluation errors; then `1` for a
simulation failure or valid threshold miss; otherwise `0`, including ungated
collection. Structured `detail.targets[selector]` retains each Target's
`simulation`, `collection`, `evaluation`, and canonical `coverage_campaign`
reference even when another Target dominates the exit code.

The default report root is `flow-reports` under the resolved project-data
directory; `--report-dir` selects an explicit root. Each invocation owns:

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

Coverage progress uses the same terminal lifecycle. It carries `coverage: true`,
the invocation `run_id`, its latest `timestamp`, the exact Target partition, and
per-Target detail. An interrupted or failed invocation preserves already durable
Targets and leaves the failed or unstarted Targets pending.

New `coverage.json` manifests use `booley.coverage-campaign/v3`. They keep exact
source/build/tool and suite fingerprints, independent per-run verdicts,
capabilities, overall rollups, deterministic per-source-file rollups, percentages,
and stored evaluation. Source rollups cover line, branch, expression, and toggle
metrics, use the same eligibility and waiver rules as overall rollups, and group by
source path rather than hierarchy. They are persisted only in `coverage.json`; the
Simulation response remains compact and points to that file.
Required `coverage-points.jsonl.gz` stores lossless point identities and sparse
positive hit incidence; the manifest binds it by schema, exact relative path,
compressed and uncompressed byte counts, point count, and SHA-256. V1 and V2
Campaigns are rejected at a hard schema cutoff; recollect coverage to produce V3.
Pass consumers the exact
`coverage.json` path; never pass or edit the point store directly.
Native artifact paths are relative to the Target directory. New Simulation
report references are relative to their containing report invocation or reports
root, never to the producing work directory. There is no project-wide latest
Campaign and no cross-Target merge. Missing legacy flat reports require consumers
to follow the canonical report pointers instead.

### Exact coverage retention

Use the report root and exact invocation number from the produced report:

```bash
python -m booley.flows.sim.campaign_retention --reports-root "$REPORTS_ROOT" --invocation 12 --native-target sim_soc
python -m booley.flows.sim.campaign_retention --reports-root "$REPORTS_ROOT" --invocation 12 --full
```

Full pruning also retires the Campaign's Project-local child-execution records.
When `REPORTS_ROOT` has either standard shape,
`<project-data>/.runtime/flow-reports` for runtime-scoped execution or
`<project-data>/flow-reports` for direct execution, Booley infers that project-data
root. The direct layout is accepted only when its parent is the currently resolved
Project data directory. If reports live elsewhere and the invocation contains Campaign child
records, add `--project-data "$PROJECT_DATA"` to `--full`, where the value is
the exact resolved project-data root. Native-only pruning never requires
`--project-data`.

Native pruning removes that Target's raw and merged databases while retaining
the immutable Campaign manifest and point store, Simulation, and hook evidence. Target-local
`availability.json` records `pruning` or `pruned`; normalized evidence remains
analyzable. Full pruning removes the exact invocation's reports and native
payloads; re-analysis is impossible. An empty `.pruned-N` tombstone reserves its
number. Selection is validated before deletion. Native-only pruning exits `2`
for ambiguous, unsafe, changed, missing, or unrecognized payloads. Full pruning
does not require recorded native payloads to remain unchanged or present, but it
exits `2` and names any file the invocation did not produce; the invocation is
left untouched. Active selections also exit `2`. Retry an interrupted cleanup
with the same exact selection. No age, size, or latest heuristic deletes evidence
automatically.

## `lint`

`lint` runs the linter selected by each Target. Verilator provides structural
and semantic diagnostics; Verible provides style and naming diagnostics. To run
both, declare and invoke two Targets.

`--scope <file,...>` filters the reported findings to selected files. Project
configuration decides whether warnings make the direct Flow exit nonzero, but
the report and `lint_clean_<target>` evidence retain the actual finding counts.

The normalized report records a flat list of findings by file, line, rule, and
message, deduplicates repeated diagnostics, and points to the complete run log.

Structured output (`lint_report.json`):

| Field | Contents |
|---|---|
| `targets`, `eda_tools` | Requested Targets and the linter resolved for each. |
| `passed`, `elapsed_s`, `total_warnings` | Lint-clean status, duration, and deduplicated in-scope finding count. `passed` is false when warnings exist even if `warnings_as_errors = false` lets the direct CLI exit `0`. |
| `warnings[]` | Deduplicated in-scope `rule`, `file`, `line`, and `message` record for each finding. |
| `errors[]` | `target` and `message` for each Target that could not produce a lint verdict. |
| `target_results[]` | Per-Target `target`, `eda_tool`, raw `warnings` count before cross-Target deduplication and `--scope`, `files_linted`, `toplevel`, `toplevel_linted`, `duration_s`, `error`, and `log`. |
| `artifacts` | The durable report and per-Target run logs. |

## `synth`

`synth` produces a fast ASIC quality-of-results estimate for RTL iteration. It
is not tape-out synthesis or sign-off. The Target supplies the top, frontend,
recipe, and SDC constraints for physical mode; the built-in backend supplies its
Nangate45 technology inputs.

Useful controls:

- `--baseline <git-ref>` compares the candidate with its recorded baseline Target
  at another revision. Distinct baseline and candidate Targets are supported.
- `--frontend <sv2v|slang>` overrides the Target's RTL frontend for diagnosis.
- `--ppa-profile <compact|balanced|max_frequency>` selects a clean built-in PPA
  profile for this invocation.
- `--flatten` / `--no-flatten` overrides the Target's hierarchy-flattening
  choice. Synthesis mode (`physical` or `logical`) remains Target-owned; there
  is no per-call `--synth-mode` option.

Physical synthesis requires the selected Target to carry a `file_type: SDC`
fileset that creates at least one clock. Booley loads those files in Target
order and adds no generated timing constraints. Logical synthesis does not run
STA and therefore does not require SDC.

Expert Yosys controls:

- `--abc-recipe <default|balanced|fast>` or `--abc-script <script>` overrides
  ABC mapping.
- `--generic-abc-before-mapping` / `--no-generic-abc-before-mapping` toggles the
  generic pre-mapping ABC pass.
- `--abc-delay-ps <picoseconds>` overrides the ABC delay target.

Expert OpenROAD controls:

- `--utilization-pct <percent>` and `--placement-density <fraction>` override
  floorplan/global-placement density.
- `--repair-setup` / `--no-repair-setup`, `--repair-hold` /
  `--no-repair-hold`, and `--gate-cloning` / `--no-gate-cloning` toggle repair
  behavior.
- `--setup-margin-ns <nanoseconds>` and `--repair-tns-percent <percent>` tune
  setup repair.

An explicit per-call PPA profile starts from that clean built-in profile rather
than inheriting the Target's backend-specific advanced settings. Expert
per-call flags then apply on top.

The Flow reports area, timing/Fmax, inferred latches, final-netlist structural
conditions, EDA warning counts, and the measurement basis. It satisfies
`synthesis_ok_<target>` only when synthesis completes, the dedicated final
Yosys structural check is present, and every configured threshold and
structural policy passes.

Structured output (`synth_<target>.json`; qualified selectors use a sanitized,
hash-suffixed filename):

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

Final combinational loops and multiple drivers are separate fatal structural
conditions. Other actionable warnings produce `grade: "warn"` while keeping
`passed: true` and exit zero. Explicitly benign warnings remain counted with a
rationale and do not downgrade the grade. Open `artifacts.log` for every raw
diagnostic when the bounded representatives are insufficient.

## `fpga`

`fpga` runs FPGA implementation for a Target, currently through host-provisioned
AMD Vivado. The Target owns the FPGA part, toplevel, compile-time parameters,
and XDC constraints.

Useful controls:

- `--baseline <git-ref>` compares implementation metrics with another revision.
- `--ppa-profile <compact|balanced|max_frequency>` overrides the Target's
  portable optimization intent for this invocation. Resolution is call,
  Target `flow_options.ppa_profile`, then `balanced`.
- `--no-cache` forces fresh implementation instead of reusing a matching result.
- `--dry-run` performs the shared aggregate planning contract described above.
  Its FPGA work units include resolved part, top, XDC, source inputs, recipe,
  and the explicitly marked Vivado Make command template. A later Target error
  blocks execution but does not hide valid earlier work units from the plan.

| Profile | Vivado synthesis | Vivado implementation | Intent |
|---|---|---|---|
| `compact` | `Flow_AreaOptimized_high` | `Area_Explore` | Prefer resource/area reduction. |
| `balanced` | unchanged Vivado default | unchanged Vivado default | Preserve the existing default trade-off. |
| `max_frequency` | `Flow_PerfOptimized_high` | `Performance_ExplorePostRoutePhysOpt` | Prefer timing/Fmax. |

These mappings are internal adapter evidence, not raw public knobs. A per-call
profile applies to both baseline and candidate. Baseline and candidate Targets may carry
different persistent profiles, but basis-bound comparisons reject differing
measurement recipes. Target `synth` and `pnr` values are Edalize engine-selector
fields; the built-in FPGA Flow neither forwards them nor treats them as Vivado
strategy overrides.

The Flow normalizes utilization, routed timing/Fmax, fixed critical-condition
counts (latches, combinational loops, and multi-driven nets), constraint/recipe
identity, and cache identity. It satisfies `fpga_impl_ok_<target>` only when
implementation evidence and primary metrics are complete, timing and configured
thresholds pass, and no critical condition is present.

Structured output (`fpga_<target>.json`):

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

The profile name describes optimization intent, not a promised QoR result.
FPGA area is represented by LUT/FF/BRAM/DSP utilization. Power is not currently
normalized, so this Flow does not claim a measured power result.

## Related references

- [USAGE.md](USAGE.md#booley-flows--specialists) explains the day-to-day agent
  workflow and direct CLI.
- [CONFIG.md](CONFIG.md) defines Flow, Target, test, constraint, and policy
  configuration.
- [SUPPORTED-EDA-TOOLS.md](SUPPORTED-EDA-TOOLS.md) defines supported EDA tools,
  provisioning, trace capability, and versions.
- [FLOW_IMPLEMENTATION.md](../internals/FLOW_IMPLEMENTATION.md) documents the
  built-in Flow implementation for Booley contributors.
