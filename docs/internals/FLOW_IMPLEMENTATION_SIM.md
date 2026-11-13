# Simulation Flow implementation

How the built-in Simulation Flow (`sim`) runs a Target's tests and turns the
result into evidence: the adapter boundary, verdicts, Cycle Count Goals, the
compiler cache, the Elaboration Check, and coverage collection, retention, and
analysis. It is for contributors and agents changing `booley.flows.sim`. What
all built-in Flows share (FuseSoC resolution, evidence contracts, the
invocation lifecycle, Goal baselines) is in
[FLOW_IMPLEMENTATION.md](FLOW_IMPLEMENTATION.md); report files and the
Simulation Campaign layout are in [FLOW_REPORTS.md](FLOW_REPORTS.md#sim); how
to run a simulation is in [FLOW-SIM.md](../user/FLOW-SIM.md) and
[COVERAGE.md](../user/COVERAGE.md).

Code, persisted formats, and messages still say "Simulation Campaign" and
"Coverage Campaign" for what the glossaries call a
[Simulation Record](../GLOSSARY.md#simulation-evidence) and a
[Coverage Record](../../src/booley/flows/sim/GLOSSARY.md)
([ADR 0090](../adr/0090-rename-simulation-and-coverage-campaigns-to-records.md));
this page uses the code's names.

## What a run produces

`sim` runs the resolved Target's selected Test Runs and produces a verdict per
test, a `sim_pass_{target}` Criterion, logs and reports, and optional trace
artifacts. The EDA tool (Verilator or Icarus, from the Target's
`flow_options.tool`) decides the build recipe. A
[Cocotb Target](../GLOSSARY.md#configuration) (a Python testbench) is an
independent property, read from the Target's `cocotb_module` flow option and
never from `tests.toml`; cocotb selects tests through `COCOTB_TEST_FILTER` on
cocotb 2.x and `TESTCASE` on 1.x, because 1.x ignores the filter and would
silently run the whole module.

Inputs come from three places: the Target holds build inputs and the
simulator; `tests.toml` holds the Tests, Test Variants, seeds, and how the
testbench selects a test ([TESTS.md](../user/TESTS.md)); `[flows.sim]` holds
execution and verdict policy
([FLOW-SIM.md](../user/FLOW-SIM.md#configuration-flowssim)). CLI selectors
such as `--test` (Test Run selectors like `test+variant@seed`), `--tests-file`,
and `--resume-from` resolve against those entries rather than becoming raw
command fragments.

`SimulateFlow` (`flows/sim/flow.py`) keeps Target and test selection, Cycle
Count baselines, Criteria, report rendering, and exit codes. The Simulation
Campaign (`flows/sim/campaign/`) owns the immutable workload plan, Simulator
Bundles, scheduling, per-work-item durability, and resume
([ADR 0064](../adr/0064-make-simulation-campaign-the-resume-authority.md)).
The manifest and terminal work-item results are the authority; the summary
and `simulation.json` are replaceable views of them.

## Adapter boundary

The simulator adapters sit behind one deliberately narrow seam
([ADR 0058](../adr/0058-deepen-the-simulation-adapter-seam.md)):

- `sim.build` prepares and authenticates the shared Edalize build.
- `sim.execution` owns caller invariants, Project-Root-scoped Pre-Sim and
  artifact-integrity policy, and the one place adapters are composed
  (`execution/composition.py`). It exposes `SimulationExecution` with
  `preview`, `plan_ordinary_group`, and `ordinary_group`: resolve each
  `TargetHandle` from its own Project Root, build one group, launch an
  executable snapshot through one adapter, and return immutable build, test,
  workload, log, trace, and infrastructure evidence.
- The three leaf modules under `sim.backends` run only their own simulator.
  No leaf imports a sibling adapter or `sim.flow`.

`TargetHandle.project_root` is authoritative: test registry fields and
`[flows.sim]` settings are loaded for that checkout on every current or Cycle
Count baseline run, never borrowed from a cached active Project, so a baseline
checkout is judged by its own configuration.

The parent trusts only the adapter's typed terminal result, a versioned JSON
document written atomically and bound to an unpredictable attempt token, the
adapter name, the durable Target identity, and the complete ordered set of
selected tests. The decoder rejects malformed, stale, reordered, duplicate, or
contradictory results, and any result file older than the invocation or
outside its build root. `[SIM_SUMMARY]`, `[SIM_RESULT]`, `[COCOTB_RESULTS]`,
`result.json`, and `results.xml` are inputs to an adapter's normalization and
human-readable logs, never the grading channel; an output line or exit code
alone could be stale or forged by the testbench.

The result also carries how the run ended: `completed`, `timeout`,
`disk_budget`, `fatal_init`, `sim_time_stall`, or `trace_stall`. Only
`completed` allows a verdict from sentinels and return codes. A guard
termination wins even when the child exits 0, and is recorded only for a child
that was still alive. A nonzero process or build outcome still outranks an
adapter pass.

For Cocotb, per-test verdicts come from this run's JUnit `results.xml`;
unexpected entries are diagnostics and cannot change a selected test's
verdict. If a batch aborts, finished tests keep their verdicts and the rest
carry the batch termination.

## Pre-Sim Commands

[Pre-Sim Commands](../GLOSSARY.md#configuration)
(`[flows.sim].pre_run_commands`) are the one Project-owned hook. They run in
the Sandbox per test for an HDL Target and once per Cocotb batch, under a
`BOOLEY_*` environment that names the Target, the Test Runs, and the run
directory (full list in
[FLOW-SIM.md](../user/FLOW-SIM.md#pre-sim-commands-flowssimpre_run_commands)).
This is how a per-test non-RTL step, such as cross-compiling the test's
firmware, joins the Flow.

`[flows.sim].pre_sim_build_access` decides when they run:

- `immutable` (default): after the build, with the Simulator Bundle path
  withheld. Booley checks that the hook did not change the build inputs, which
  is what allows one build to be shared across tests. A hook that cannot start
  or that changes build inputs is a Simulation Campaign integrity error (exit
  2).
- `legacy-per-test`: before a private per-test build, with
  `BOOLEY_BUILD_ROOT` set. Native coverage refuses this mode.

A hook that fails or times out becomes that test's `elab_error` with the tail
of its output. It can never produce a pass.

## Verdicts

Each test gets one verdict:

| Verdict | When |
|---|---|
| `pass` | The verdict sentinel reports passed and there are no SVA (SystemVerilog Assertion) errors. |
| `fail` | A failing sentinel, a nonzero exit with fresh evidence, or SVA errors. |
| `inconclusive` | The run ended cleanly but no verdict sentinel appeared, or a `--trace` run that otherwise passed could not prove its trace was written. |
| `elab_error` | Build, elaboration, or compile failed, or Pre-Sim Commands failed; the simulator never ran. |
| `timeout` | The per-test time budget ran out. |
| `crash` | The simulator was killed by a signal. |

Only a Target-level `pass` satisfies `sim_pass_{target}`. An `inconclusive`
Target skips the Criterion rather than failing it, because nothing was proven
either way; it still exits 1. Configuration errors (bad `--target`, disabled
Flow, unknown `--test`) and Flow crashes exit 2, outside this vocabulary.

## Per-test Cycle Count Goals

A Cycle Count Criterion binds one Target and Test Run to numeric thresholds
([ADR 0051](../adr/0051-per-test-cycle-count-criteria.md)). A Goal entry
argument looks like:

```json
{
  "family": "CYCLE_COUNT",
  "target": "sim_coremark",
  "test": "coremark",
  "thresholds": {
    "cycle_count_max": 100000,
    "cycle_count_reduce_at_least": "5%",
    "cycle_count_reduce_at_least_cycles": 2000
  }
}
```

The named test must pass and print exactly one
`[SIM_CYCLES] <test> <non-negative count>` line (cycle sentinels match the
longest configured prefix, so a short sentinel cannot swallow a longer one).
All thresholds must hold. Missing, duplicate, malformed, failed, or
inconclusive evidence leaves the Criterion unmet. A passing cocotb test can
meet its Criterion even when another test in the batch fails. Without a
Cycle Count Criterion, cycle counts are only observed.

`_max` and `_min` thresholds use the current run. Percentage and `_cycles`
thresholds also run the same Target and Test Run at the Goal's pinned base in
an ephemeral worktree; the baseline counts only when the Test Run's arguments
and environment match. A zero baseline cannot define a percentage and fails
closed. When either side was lost to infrastructure, relative Criteria are
left unchanged, since that says nothing about the count. Changes to declared
workload inputs (RTL, testbench, firmware, vectors, constraints) between base
and candidate are recorded with the comparison (`criteria/cycle_count.py`)
rather than rejected, so a Goal can measure intentional testbench or firmware
changes; they mean the change cannot be credited to the RTL alone.

## Logs and traces

`simulation.json`, `report.json`, and the Campaign layout are in
[FLOW_REPORTS.md](FLOW_REPORTS.md#sim). Each test's simulator output is kept
as live-log evidence in its Campaign work item: each launched attempt's
`execution-evidence/` directory, beside its snapshot, holds `run.log`
(tail-truncated to 10 MB), the adapter result JSON, and Cocotb's
`results.xml`. `booley.flows.sim.campaign.test_run_files` is the one lookup
from a Test Run to its accepted attempt's log and trace, used by both the
projection and the verdict card. A grouped run keeps each test's copy before
starting the next test, including failed, timed-out, and inconclusive ones. A
failed build never launches, so its `run.log` stays in the build directory.

Traces stay off the pass/fail path. `--trace` builds a separate trace-overlay
Target with its own build root, so it never replaces the fast verdict build,
and produces a queryable `trace.fst` next to the logs. A trace that fails to
appear turns a passing run into `inconclusive`: a missing waveform is a
tooling failure, never a silent pass.

## Verilator compiler cache ownership

Verilator builds can share compiled objects through ccache. Simulation build
preparation resolves the cache settings (`resolve_compiler_cache_settings` in
`flows/sim/config.py`) and the issued cache root into one immutable policy
(`flows/sim/compiler_cache.py`); `PreparedSimulationBuild.environment` is the
authority for compilation. `simulation_build_script` checks that the cache is
usable only when executing, and exports compiler settings only to the build
half. Booley's cache variables win over the Target's and the ambient
environment. Active builds also set `CCACHE_BASEDIR` and a
`-fdebug-prefix-map` for the generated build directory, so `-g` objects hit
the cache across build generations.

Ordinary, campaign, coverage, Cocotb, and elaboration-only builds use this
interface; Mutation Flow builds, which run Make directly, use
`simulation_build_environment`. Icarus builds do not resolve a cache policy.
The policy takes the environment as an argument (build preparation passes
`os.environ`). An unset root gives an inactive policy that compiles uncached
and warns to refresh the Sandbox; only invalid settings fail preparation. The
root is trusted as given and only created, never validated further.

Sandbox Issuance fixes `BOOLEY_COMPILER_CACHE_ROOT` to
`.runtime/compiler-cache/ccache` under the Booley Directory mount
(`booley.runtime.compiler_cache`), in both container and remote environments,
and Goal-bound and review subprocesses inherit it. Its presence and value
take part in specification validation, digest and drift detection, and
refresh, so a Sandbox issued before the cache existed gets a refresh hint.
There is no host Booley Directory fallback. The cache sits outside every
mutable build generation; setup cleanup removes only its own marker-guarded
`tmp/setup/<run-id>/` and never touches the cache.

## Simulation Elaboration Check

`sim --mode elab-only` compiles, elaborates, and links the normal untraced
simulator without running tests
([Elaboration Check](../GLOSSARY.md#flows-eda-tools-and-mcp-tools)). It skips
Pre-Sim Commands, Cocotb import and execution, test selection, run guards,
sentinels, and tracing. It uses the normal Simulation work root and build
policy, so a later full simulation can reuse the image. Only Simulation
Targets qualify, and both elaboration modes reject run-only arguments
(`--test`, `--tests-file`, `--trace`, `--result-verbosity full`, `--no-kill`).
It is the only producer of `elab_pass_{target}`; a full simulation does not
record it.

A compiler diagnostic proving the design was rejected is a design FAIL (exit
1). Setup failures, a missing tool, timeouts, OOM, signals, filesystem errors,
and ambiguous nonzero exits are infrastructure ERRORs (exit 2). Multi-Target
runs continue through every Target, and ERROR takes precedence over FAIL.
Reports use the Simulation namespace with mode `elab_only` or
`elab_only_standalone`. Complete build logs are archived under the invocation
report directory (or a unique log directory under the Simulation work root
for a bare CLI run), while the shared build cache stays mutable and reusable.

`--mode elab-only-standalone` adds a sweep that elaborates each module on its
own (`flows/sim/standalone.py`), to catch modules that only compile inside
their current parent. It covers the selected Targets' RTL filesets, skips
testbench-only sources, and includes package and interface files as shared
prerequisites. Each module is probed with the simulator of every selected
Target whose RTL contains it
([ADR 0089](../adr/0089-probe-standalone-modules-with-each-targets-simulator.md)):
`inspection.eda_tool` maps `icarus`/`iverilog` to `iverilog` and `verilator` to
`verilator`. A probe compiles only the shared files of the Targets that
contribute its frontend and dialect flags, and forwards only the Target's
language-dialect options (Icarus `-g*`; Verilator `--language`,
`--default-language`, `+<generation>ext+`) from `flow_options`; a module shared
by Targets with different dialect flags is probed once per set.

A Target with any other simulator would need a fallback probe (Verilator when
on PATH, as the stricter full-SystemVerilog frontend, else Icarus). `sim`
accepts only Icarus and Verilator Targets, so this path is unreachable until a
commercial simulator Target exists. A fallback probe whose compiler matches a
covering Target's runs once and is graded normally. A genuine module
rejection is exit 1. When the per-Target builds passed and the only problems
are fallback-probe parse errors, the result is "ungraded" (exit 2) and the
standalone Criterion keeps its previous state.

## Coverage Campaign orchestration

A Coverage Campaign is native Verilator coverage collected by one Simulation
invocation for one Target. Collection is off unless the caller asks:
`SimRequest.coverage=True`, the CLI `--coverage` (alias `--cov`), or the MCP
boolean `coverage`. A Coverage Criterion never turns collection on: a
Criterion says what evidence must show, not what a run collects.

`prepare_coverage_invocation` (`flows/sim/coverage_invocation.py`) resolves
every selected Target before any EDA work, build setup, report allocation, or
state change, so a bad selection fails as one aggregated error instead of
after the first Target ran. It rejects any Target whose EDA tool is not
Verilator and freezes each Target's exact test suite and source and build
fingerprints. The suite is the invocation's named tests when given
(configured skips do not apply), otherwise the registered suite minus
configured skips. The Coverage Criterion's `tests` list (or the full
registered suite for `tests: all`) is required evidence, not a selector: a
run whose suite differs, including by configured skips, is a suite mismatch
at evaluation. Targets run sorted, one at a time.

`run_coverage_target` (`flows/sim/coverage_transaction.py`) collects through
`SimulationExecutionPort`, assembles and validates the Campaign, loads the
Approved Waiver Set unless `--no-waivers` is given, evaluates, publishes the
Campaign under the attempt's own directory, and returns the outcome. Source
or Target drift during the run is rejected. Exit codes, first match wins:

| Exit | When |
|---|---|
| 1 | The design aborted the run. |
| 2 | Collection did not complete, or evaluation is `blocked` (for example, the Approved Waiver Set failed to load or match). |
| 1 | A simulation failed. |
| 0 | Otherwise. A threshold miss exits 0; the Criterion records it. |

Without a Coverage Criterion the evaluation is `not_requested`, even for
incompatible input. A collector failure on one Target lets later Targets run;
a shared execution or publication failure stops the invocation and keeps
earlier Targets' results and the pending list in the structured output.

Waiver configuration is `[coverage.waivers]` in the Booley Directory's
`booley.toml`, with an `anchor` (`rtl_repository` or
`project_data_repository`) and a relative `directory`
([COVERAGE.md](../user/COVERAGE.md#waiver-directory-coveragewaivers));
coverage window and hook settings stay on the Target under
`flow_options.booley.coverage`.

In Goal Mode, the Coverage Analyst records screened Waiver Candidates in the
Goal Record's `waiver-candidates.json` (`flows/sim/waiver_candidates.py`),
with strict and provisional verdicts. A Provisional Coverage Verdict only
informs the human and cannot meet a Goal. The Developer Agent proposes a
waiver through `goal_propose_change`; human approval revalidates the
candidate, Campaign, source, and policy identities, updates the Approved
Waiver Set, recomputes strict coverage, and records the change in the Goal
Change Log. A waiver-policy change made outside that path invalidates older
coverage evidence instead of counting as Goal approval
([ADR 0066](../adr/0066-record-waiver-candidates-for-approval-at-review.md)).

### Persistence and recovery

The file layout is in
[FLOW_REPORTS.md](FLOW_REPORTS.md#coverage-campaign-files). The rules that keep
it trustworthy:

- **The nested manifest is the commit marker.** `publish_coverage_campaign`
  (`flows/sim/coverage_campaign_store.py`) writes the point store first, then
  the nested `coverage.json` manifest, and removes the point store if the
  manifest never lands. Neither file is ever replaced, so a reader that finds
  a manifest finds a complete pair.
- **Readers re-derive what decides a verdict.** A deep read gunzips the point
  store once under a size ceiling, checks its header schema, and validates
  every point, then recomputes the overall rollups and re-scores a stored
  pass/fail evaluation from the points and stored thresholds; the stored
  rollups and verdict rows are discarded. A pass/fail verdict whose suite did
  not match, that carries diagnostics, or that cannot be re-scored is refused.
  Per-source rollups and `scoring` are writer-derived and passed through. The
  summary must never outvote the points it summarizes
  ([ADR 0062](../adr/0062-split-coverage-campaign-summary-from-point-storage.md)).
  Contract failures carry stable `COV_*` error codes.
- **Criteria come only from Campaign acceptance.** Simulation Campaign
  acceptance (`flows/sim/acceptance.py`) records Coverage Criterion evidence
  after the Campaign is published. Coverage observations go to
  transaction-named ledger directories, and the transaction ID is saved in
  `acceptance_transactions` in the same atomic state save as the updated
  Criteria. Readers ignore observations from transactions the saved state
  does not name, so an interrupted append or failed save keeps the previous
  verdict without deleting history.
- **What a late failure keeps.** Before the nested manifest is published, a
  persistence error keeps any observed simulation verdict but reports
  collection as `infrastructure_error` and blocks a requested evaluation.
  After it is published, later failures (Simulation Campaign, reference,
  summary, acceptance, report, or progress publication) keep the committed
  collection and evaluation results and the simulation verdict; they never
  turn a committed evaluation into `blocked`. The one exception: if the
  nested Campaign cannot be loaded back, the detail reports
  `infrastructure_error` and the prepared evaluation. If keeping a completed
  outcome fails while resolving its public reference, the result keeps the
  simulation verdict and tests but reports `collection: infrastructure_error`
  and, when evaluation was requested, `evaluation: blocked`, with no coverage
  reference. A report path is usable only when its own publication succeeded,
  and no stage runs after a publication failure except an error checkpoint.
- **Terminal progress follows publication.** If the normal terminal
  `progress.json` write fails, the Flow retries once as `phase: aborted` and
  returns 2 even when the retry succeeds; the committed Campaign stays valid.
  `phase: complete` is reserved for a successful progress write, so a
  per-Target progress failure ends as `aborted` even when every Target
  finished. A failed terminal write never replaces the result already built
  from durable outcomes with a generic error.

### Locks and resume

Simulation takes `.invocation-N.lock` beside `sim/N` before publishing the
invocation and holds it while producing. Pruning takes the same lock without
waiting, so an active invocation cannot be removed. Process exit releases the
lock; `progress.json` is never lock or resume authority. Every new invocation
takes a fresh number, and the Target transaction rejects native state left by
an earlier start.

Coverage resume finds its origin only from the validated manifest path, takes
the origin's invocation lock before recovery, and holds it to the end.
Successful recovery ends the new invocation as `complete` and marks a
`running` or `aborted` origin `superseded`. The origin's completed and
pending lists stay as they were. An origin already `complete` is left alone;
one already `superseded` keeps its first `superseded_by`. Missing, malformed,
or concurrently changed origin progress only skips the supersession mark,
because the manifest, not progress, decides recovery.

### Exact report retention

`booley.flows.sim.record_retention` removes the evidence of one exact
invocation; the command line is in
[FLOW_REPORTS.md](FLOW_REPORTS.md#coverage-retention). It takes the report
root explicitly (callers get Booley Directory roots from
`booley.runtime.booley_dir.resolve_booley_dir()`) and never picks a "latest"
Campaign or applies age or size rules: deleting evidence must be a deliberate
act on a named invocation.

- `prune_native_payload(reports_root, invocation, target)` needs an exact
  invocation number and one exact manifest Target selector. It validates the
  Campaign pair, Simulation identities, and link safety before changing
  anything; symlinks, unsafe paths, ambiguous or unknown Targets, a wrong
  invocation, and a recorded native payload already missing on the first try
  are errors. It writes `availability.json`
  (`booley.coverage-availability/v1`: Campaign ID, manifest and point-store
  digests, native artifact IDs, paths, and digests) in the nested Coverage
  Campaign directory with status `pruning`, deletes `native/` (unrecorded
  files included), then sets the status to `pruned`. A retry trusts the
  existing sidecar and finishes the removal. The manifest, point store,
  Simulation files, and hook evidence never change, so the Campaign stays
  analyzable.
- `prune_invocation(reports_root, invocation, *, project_data=None,
  include_dependents=False)` validates every Target and checks every file
  against the authenticated invocation, Campaign, and artifact inventory,
  then renames the invocation to `.pruned-N` and empties it. Unknown files are
  named and refused before any change. A terminal attempt owns its records,
  the files its result names, and its `private-build/`, `snapshot/`, and used
  shared-build trees; the unknown-file check inside it covers its records and
  `evidence/`, `runtime-inputs/`, and `pre-sim/`, not the build trees.
  Changed or missing native payloads are accepted, since everything goes.
  Pending or interrupted Campaigns, missing summaries, and missing projections
  count as abandoned, not invalid. A reservation abandoned before
  `progress.json` is removable when its directory is empty and its lock file
  is intact. The empty `.pruned-N` directory reserves the number for good.
  With `include_dependents`, authenticated resume dependents are removed
  first, journaled in `.prune-batch.json`.
- An interrupted full prune is finished when `.pruned-N` holds its
  `.prune.json` journal; without the journal it is refused.

Before removing an interrupted attempt's run directory, resume and full
pruning cancel that lane's execution and fail closed unless its process tree
has ended. Full pruning also removes owned templated run directories after a
marker check; literal run directories are untouched. That recovery needs the
Booley Directory only when such a resource survives. The Booley Directory is
inferred only when the report root is exactly
`<booley-directory>/.runtime/flow-reports`; any other root needs
`--project-data <booley-directory>` for `--full` in that case.

Full pruning holds the producer lock and every Campaign mutation lock through
a second inventory pass, recovery, journal publication, and the rename.
Producer-lock contention means the invocation is still being produced;
mutation-lock contention means a resume is running. Retention never removes
or rewrites either lock, and a Campaign lock never recreates a renamed
Campaign root. Selection, locking, validation, and filesystem failures exit 2.
Both operations are safe to retry with the same selection; never remove
journals, quarantines, invocation locks, or `.pruned-N` directories by hand.

### Coverage Analyst input

The Coverage Analyst takes the exact Target-level `coverage.json`, deep-loads
its point store, rejects collector-error or incompatible collection before
calling the model, and checks the matching completed `simulation.json`. It is
a separate advisory call: it never runs Simulation or records Criterion
evidence. Native pruning leaves its input usable; full pruning removes it. Its
output and evidence budget are in
[SPECIALIST_IMPLEMENTATION.md](SPECIALIST_IMPLEMENTATION.md#coverage-analyst) and
[ADR 0063](../adr/0063-bind-coverage-analysis-to-budgeted-evidence.md); the
release checks are in [coverage-release-gate.md](coverage-release-gate.md).

### RTL source advisories

A source file that declares a module but yields no coverage points usually
means the testbench never instantiated it, which percentages alone hide.
Preflight finds candidates with `rtl_module_candidates`
(`flows/sim/coverage_source_gaps.py`, using `booley.core.hdl.declares_module`):
the sorted module-declaring, non-include, non-`tb` Verilog inputs, stored on
`CoverageTargetPlan`. The scan does not preprocess and adds no dump switch to
coverage builds. The collector records which sources appear in any native
record of any type, through the same `_match_source` that assigns point
sources, before metric filtering; `source_gap_findings` then warns for each
candidate that never appears. `validate_coverage_sources`, run before and
after collection, is the only integrity check. Resume reuses the published
findings instead of rereading the current RTL. The Analyst's
`zero_point_sources` view pages through those paths under the normal byte
budgets with stable cursors.
