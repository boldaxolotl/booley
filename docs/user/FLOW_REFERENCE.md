# Booley Flow reference

Booley ships four built-in Flows: `sim`, `lint`, `synth`, and `fpga`. This page
explains how to run each one, what its result means, and where to look next.

Related references:

- [CONFIG.md](CONFIG.md): Targets, tests, constraints, Criteria, and Flow policy.
- [SUPPORTED-EDA-TOOLS.md](SUPPORTED-EDA-TOOLS.md): which EDA tools and versions
  each Flow supports.
- [USAGE.md](USAGE.md#booley-flows--specialists): the day-to-day agent workflow.
- [FLOW_REPORTS.md](../internals/FLOW_REPORTS.md): JSON report schemas and file
  layouts, for scripts that consume Flow output.

## Running a Flow

Normally you ask the Interactive or Developer Agent to run a Flow. Name the
Target, and for `sim` name the test (or ask for the Target's full suite). To
reproduce or debug a run yourself, use the CLI inside the Sandbox:

```bash
booley flow lint  --target lint_soc
booley flow sim   --target sim_soc --test reset
booley flow synth --target synth_soc
booley flow fpga  --target fpga_soc
booley flow <name> --help          # the authoritative option list
```

### Choosing Targets

Every run needs `--target`; there is no project-wide default. (The one exception
is `sim --resume-from`, which takes its Target from the Campaign it resumes.)

- `booley targets` lists all Targets; `booley targets --for <flow>` lists
  the ones a Flow can drive.
- Select several Targets by repeating the flag, using commas, or both:
  `--target a --target b,c` runs `a`, `b`, `c` in that order. Naming the same
  Target twice is an error.
- If two cores expose the same Target name, use the qualified selector printed
  by `booley targets`, such as `lowrisc:ibex:ibex_top#lint`.

### Common options

| Option | Effect |
|---|---|
| `--target <name,...>` | Targets to run (see above). |
| `--work-dir <path>` | Project or worktree root. Defaults to the current directory. |
| `--report-dir <path>` | Write reports here instead of the default report root. |
| `--diagnostic` | Run without recording Ticket Criteria. A strict Ticket requires it for Flow/Target pairs outside its baseline. |
| `--dry-run` | Resolve and validate the work, print the plan, and stop. No EDA tool runs and no state changes. |
| `--timeout-ms <ms>` | Active-time budget per work unit (queue time is not counted). Overrides `[flows.<name>].timeout_ms`. Simulation builds instead use `[flows.sim].build_timeout_ms`, and Pre-Sim Commands use an independent fixed budget; see [Simulation timeouts](CONFIG.md#simulation-build-pre-sim-and-run-timeouts). |

## Reading results

Every built-in Flow uses the same exit codes:

| Exit | Meaning | What to do |
|---:|---|---|
| `0` | The Flow ran and the check passed. | Nothing. Advisory findings (such as lint warnings with `warnings_as_errors = false`) may still be reported. |
| `1` | The Flow reached a design verdict and it failed. | Fix the RTL or testbench. |
| `2` | The Flow could not reach a verdict: bad configuration, missing tool, crash, timeout. | Fix the setup; the result says nothing about the design. |

The CLI prints a final verdict card: stdout on success, stderr on failure.

**Reports.** Each run writes a structured JSON report and complete logs. By
default they go under `flow-reports/` in the project-data directory (Ticket and
agent runs use their runtime directory). `--report-dir` overrides this. Booley
never writes reports into the RTL checkout. The verdict card and the report both
point at the exact log and artifact paths, so you rarely need to go looking.

**Dry runs.** `--dry-run` prints a JSON plan listing each work unit: Target,
sources, constraints, resolved parameters, and the exact commands it would run.
It exits `0` when everything can be planned and `2` otherwise, keeping the valid
parts of the plan for diagnosis. FuseSoC generators may run in a throwaway
scratch directory when resolution needs them; the plan says so.

For Simulation, ordinary candidate and baseline units expose the simulator
budget as `timeout_ms` and the invocation-carried build budget as recipe
`build_timeout_ms`. Elaboration Check units expose their build budget in both
fields because the Target build is their only subprocess. Standalone units
retain the standalone-sweep `timeout_ms` and have no build field.

## `sim`

`sim` builds and runs the tests registered for a simulation Target. The Target
chooses the simulator (Verilator or Icarus) and the testbench style (HDL or
cocotb).

### Running tests

```bash
booley flow sim --target sim_soc                          # full registered suite
booley flow sim --target sim_soc --test reset --test irq  # exact tests, in order
booley flow sim --target sim_soc --tests-file smoke.txt   # names from a file
booley flow sim --target sim_soc --test irq --trace       # capture a waveform
booley flow sim --target sim_soc --test irq --coverage    # collect coverage (Verilator only)
```

| Option | Effect |
|---|---|
| `--test <name>` | Run one registered test by exact name. Repeat for more. |
| `--tests-file <path>` | Read exact test names from a file, one per line (blank lines and `#` comments ignored). Cannot be combined with `--test`. |
| `--mode <simulate\|elab-only\|elab-only-standalone>` | Run tests (default), or only elaborate (see [Elaboration checks](#elaboration-checks)). |
| `--trace` | Capture a waveform. Use it to debug a failure, not for pass/fail checks. |
| `--coverage` / `--cov` | Collect coverage (see [Coverage](#coverage)). |
| `--resume-from <manifest.json>` | Resume an interrupted run (see [Resuming](#resuming-an-interrupted-run)). |
| `--result-verbosity <compact\|full>` | cocotb console detail. `full` prints every testcase; the complete XML/JSON is always kept. |
| `--no-kill` | Skip the pre-run cleanup of stale simulator processes. Diagnostic use only. |

Test selection rules:

- Every named test must exist in every selected Target; unknown, duplicate, or
  empty selections fail before anything runs.
- A plain run (no `--test`/`--tests-file`) honors the `skip` entries in
  `tests.toml`. Naming tests gives an exact explicit suite, which overrides those
  entries. A Target whose whole suite is skipped fails preflight instead of
  passing vacuously.

Multiple simulator processes can run in parallel up to `[jobs].max_heavy`
(default `1`, i.e. serial). Tests sharing a literal `run_cwd` always run one at a
time.

### Verdicts and Criteria

Each test gets one verdict:

- **HDL testbenches** pass or fail through the configured pass/fail sentinels;
  a fail sentinel always wins.
- **cocotb testbenches** use cocotb's result file; assertion output can still
  fail the test.
- A test that exits cleanly without a valid verdict is **inconclusive**, never a
  pass. So is a `--trace` run that produced no fresh waveform.
- A test stopped by a Booley guard stays **aborted** even if the simulator
  exits `0`. An infrastructure abort is exit `2`. A `$readmemh` input file that
  is missing and not declared in the Target is a design failure
  (`missing_input`, exit `1`). Either way the cause is kept, and assertions are
  reported as not observed.

Criteria are Ticket Mode acceptance conditions: a Ticket declares them, and
Flow runs made for that Ticket record whether they were met (see
[Acceptance Criteria](USAGE.md#acceptance-criteria)). An Interactive Mode run
only reports its verdict and exit code; it records no Criteria.

Within a Ticket, a `sim` run can satisfy these Criteria:

- `sim_pass_*`: declared under `SIM` for either the Target's complete Required
  Simulation Suite (`all: pass`) or individual named tests (`smoke: pass`).
  Passing a hand-picked subset does not satisfy an `all` Criterion.
- `cycle_count_<target>_<test>`: checks one named test. It passes when that test
  passes and its reported Cycle Count meets every threshold the Ticket declares
  (see [Threshold parameters](USAGE.md#threshold-parameters)).
- `elab_pass_<target>`: see [Elaboration checks](#elaboration-checks).

For example, a Ticket can require two tests independently:

```yaml
CRITERIA_MANDATORY:
  SIM:
    sim_core:
      smoke: pass
      regression: pass
```

Each named test has its own Criterion, evaluated from that test's results.
Run it with `booley flow sim --target sim_core --test smoke`, or include it in
a full-suite run. To additionally require the complete suite, add `all: pass`
under the same Target.

### Elaboration checks

`--mode elab-only` compiles, elaborates, and links the simulator without running
tests. It is a fast "does it build?" check, and a later full run reuses the
build. `--mode elab-only-standalone` adds a sweep that elaborates each reusable
RTL module on its own and can satisfy `elaborate_standalone`.

The Target build in either mode uses the shared
[`build_timeout_ms`](CONFIG.md#simulation-build-pre-sim-and-run-timeouts)
budget rather than `--timeout-ms`.

Within a Ticket, a successful build satisfies `elab_pass_<target>`, whether it
comes from `--mode elab-only` or from the build step of a normal run. It is
recorded as soon as the build succeeds, so a later test failure does not erase
it.

Both modes skip Pre-Sim Commands and reject run-only options (`--test`,
`--tests-file`, `--trace`, `--result-verbosity full`, `--no-kill`). A compiler
error in the RTL is exit `1`; a tool, setup, timeout, or out-of-memory problem is
exit `2` and leaves Criteria unchanged. With several Targets, every Target is
checked and exit `2` takes precedence over `1`.

### Resuming an interrupted run

Resume is for long, heavy runs, such as a multi-hour regression or a large
coverage collection, where re-running tests that already finished is expensive.
For a short run, just start it again.

Every simulation run records its plan and results in a durable Simulation
Campaign, one per Target. The CLI prints each Campaign's `manifest.json` path
before starting, and the verdict card repeats it. If a run is interrupted,
resume it:

```bash
booley flow sim --resume-from \
  "$PROJECT_DATA/flow-reports/sim/12/targets/sim_soc/campaign/manifest.json"
booley flow sim --resume-from <manifest.json> --dry-run   # show what is left
```

- Target, tests, mode, coverage, and trace come from the manifest and cannot be
  given again. Timeout and output options may change.
- One resume continues one Target. For a multi-Target run, resume each
  unfinished Target's manifest separately.
- Within a Target, the retry unit is a work item:

  | Testbench | Work item | On resume |
  |---|---|---|
  | HDL | one test | Only tests without a recorded result run again. |
  | cocotb | the whole batch | An interrupted batch re-runs all of its tests. |
  | `--coverage` | the whole collection | Rebuilds and re-runs every test into a distinct nested Coverage Campaign. |

- Resuming a coverage run saves time only when the collection already finished
  and the interruption hit while results were being published. Otherwise it
  costs the same as a new run.
- A test with a recorded result is finished, even if it failed. Resume never
  re-runs failures; start a new run for that.
- Resume refuses when the Target's sources or suite changed since the original
  run.
- Never edit, copy, or repair Campaign files by hand. They are bound together by
  digests, and resume validates the whole chain.

### Coverage

`sim` can collect Verilator coverage (line, branch, expression, toggle,
and cover properties) into a **Coverage Campaign**, check it against Coverage
Criteria, and hand it to the Coverage Analyst for waiver candidates and
testbench improvements.

#### How coverage is measured

Coverage is measured per Target, per run:

1. Each selected test runs in its own simulator process and writes its own
   Verilator coverage database.
2. Booley merges those databases into one result for the Target: a point is
   covered if any test hit it. Per-test hit counts are kept, so you can see
   which test covered what.
3. Each metric's percentage is covered RTL points divided by eligible RTL
   points. Testbench and generated code are reported but not scored. When
   approved waivers apply (see [Collecting vs. gating](#collecting-vs-gating)),
   waived points are left out of the count.

What counts as RTL:

- Every file in the Target's resolved fileset **without** the `tb` tag. Tag
  every testbench file `tb`; an untagged one is scored as RTL.
- Only code elaborated under the testbench top gets coverage points. An RTL
  module that is never instantiated produces no points, so it neither lowers
  the percentages nor shows up as a gap.
- Points are per instance: a module instantiated four times contributes four
  sets of points, and each instance must be exercised.

Each Target gets its own Coverage Campaign. Nothing is merged across Targets or
across runs.

#### Quick start

```bash
# 1. Collect coverage for the full suite
booley flow sim --target sim_soc --coverage

# 2. Ask the Coverage Analyst for waiver candidates and for how to improve the
#    testbench to raise coverage (advisory only)
booley flow coverage_analyst \
  --campaign <reports>/sim/12/targets/sim_soc/coverage.json
```

The verdict card prints the exact `coverage.json` path to use in step 2.

#### Requirements

- Every selected Target must use Verilator. Selecting any Icarus Target rejects
  the whole run before anything is built.
- Only `--mode simulate` (the default). `--trace` can be combined with
  `--coverage`.
- Tests run one at a time, one simulator process per test (including cocotb).
- Coverage builds are cached separately from normal and trace builds.
- Testbench properties such as excluding reset from coverage, or hooks for a
  custom C++ main, live in the Target's `.core`. See
  [Coverage configuration](CONFIG.md#native-coverage-configuration).

#### Collecting vs. gating

- **Ungated** (no Coverage Criterion): Booley collects and stores the Campaign
  with evaluation `not_requested`. Use it to explore.
- **Gated** (Ticket with a `coverage_<target>` Criterion): Booley also applies
  approved waivers and checks the thresholds. Only a persisted `pass` satisfies
  the Criterion.

Which tests run: tests named on the command line win, then the Criterion's test
list, then the full registered suite. If the tests you name differ from the
Criterion's list, coverage is still collected but gated evaluation is blocked.

#### Coverage Criteria and waivers

A Ticket declares thresholds per Target:

```yaml
CRITERIA_MANDATORY:
  COVERAGE:
    sim_counter:
      tests: all
      metrics: {line: {min_pct: 90}, branch: {min_pct: 80}}
```

Only points in the RTL count toward the percentages; testbench and generated
code are reported but not scored. Points that are legitimately unreachable or
out of scope can be waived by a human in a project-wide approval directory.
Waivers are checked per Target: one invalid approval blocks that Target's
evaluation. See [Coverage configuration](CONFIG.md#native-coverage-configuration)
and [Approved coverage waivers](CONFIG.md#approved-coverage-waivers) for the
full syntax.

#### Results

Simulation, collection, and threshold evaluation are reported independently: a
failing test can still yield valid, passing coverage, and a fully passing suite
can miss a threshold. The exit code combines them:

| Exit | When |
|---:|---|
| `2` | Coverage could not be trusted: preflight rejection, collection or infrastructure failure, incompatible data, or blocked evaluation (e.g. an invalid waiver or a suite mismatch). |
| `1` | A test failed, or valid coverage missed a threshold. |
| `0` | Everything passed, including ungated collection. |

With several Targets, each keeps its own simulation, collection, and evaluation
results in the report even when another Target decides the exit code.

#### Where the evidence lives

Each Target gets its own reference and nested Coverage Campaign under the run's
numbered report directory (qualified Target selectors are percent-encoded):

```text
<reports>/sim/<N>/targets/<target>/
  coverage.json              booley.coverage-campaign-reference/v1 pointer
  simulation.json            the matching Target simulation results
  campaign/
    manifest.json            the enclosing Simulation Campaign
    work-items/<item>/attempts/<attempt>/coverage-campaign/
      coverage.json          booley.coverage-campaign/v4 manifest
      coverage-points.jsonl.gz
      native/raw/            per-test Verilator databases
      native/merged/         merged Verilator database
      hooks/                 hook evidence, when collected
```

The Target reference's `coverage_campaign.path` resolves from the origin Target
directory, as declared by `coverage_campaign.path_base: origin_target`. The
nested manifest binds the point store and contains rollups and verdicts; its
native artifact paths are relative to the **Coverage Campaign directory**.
Current runs write V4; readers also accept valid retained V3 manifests at the
same nested location.

Pass the numbered Target-level `coverage.json` reference to the Coverage Analyst.
It authenticates the nested Campaign and matching completed Simulation evidence;
it does not accept the nested manifest directly. Manifest summary/deep readers
in `booley.flows.sim.coverage_campaign_store` take the resolved V3/V4 manifest,
not the reference. Never edit or pass the point store directly, and keep the
reference and enclosing Simulation Campaign together.

`<reports>/sim.json` is a mutable, last-writer-wins compatibility copy of the
newest invocation's `report.json`. It may contain Campaign pointers, but it is
not a stable Campaign selection: a later failed run can replace it with empty
`detail`. Use the numbered `<reports>/sim/<N>/...` paths for consumers. Booley
never infers a latest Campaign or merges coverage across Targets or runs.

#### Analyzing a Campaign

`coverage_analyst` explains one Campaign: what is uncovered, likely reasons,
which tests to add, and possible waiver candidates for human review.

```bash
booley flow coverage_analyst --campaign <exact coverage.json>
```

It never runs simulation, changes Criteria, or approves waivers. See
[USAGE.md](USAGE.md#coverage_analyst).

#### Cleaning up old Campaigns

Raw Verilator coverage databases are large. Booley never deletes evidence
automatically; prune an exact run explicitly:

```bash
# Drop one Target's raw/merged databases; the Campaign stays analyzable
python -m booley.flows.sim.campaign_retention \
  --reports-root "$REPORTS_ROOT" --invocation 12 --native-target sim_soc

# Remove the whole run (no re-analysis afterwards)
python -m booley.flows.sim.campaign_retention \
  --reports-root "$REPORTS_ROOT" --invocation 12 --full
```

- `--full` refuses when later resumed runs depend on this one; add
  `--include-dependents` to remove them together.
- If reports live outside the standard project-data locations, `--full` may ask
  for `--project-data "$PROJECT_DATA"`.
- Pruning refuses runs that are still active, and files it did not produce.
  An interrupted cleanup is safe to retry with the same arguments.

## `lint`

`lint` runs the linter chosen by each Target: Verilator for structural and
semantic checks, Verible for style and naming. To run both, declare two Targets
and select both.

```bash
booley flow lint --target lint_soc,style_soc --scope rtl/fifo.sv
```

- `--scope <file,...>` limits reported findings to the listed files.
- `[flows.lint].warnings_as_errors` decides whether warnings fail the exit code.
  Either way the report keeps the real counts, and `lint_clean_<target>` is only
  satisfied with zero findings.

The report lists deduplicated findings by file, line, rule, and message, and
points to the full log.

## `synth`

`synth` gives a fast ASIC quality-of-results estimate (area, timing/Fmax,
structural problems) to iterate RTL against. It is not tape-out synthesis or
sign-off. It uses Yosys and OpenROAD on the built-in Nangate45 technology.

```bash
booley flow synth --target synth_soc
booley flow synth --target synth_soc --baseline main   # compare with another revision
```

**Constraints.** Physical synthesis (the Target's `synth_mode`) needs a
`file_type: SDC` fileset in the Target that creates at least one clock; Booley
adds no timing constraints of its own. Logical synthesis skips STA and needs no
SDC. Synthesis mode is Target-owned: there is no per-call `--synth-mode` option.

Everyday options:

| Option | Effect |
|---|---|
| `--baseline <git-ref>` | Compare with the baseline Target at another revision (the two Targets may differ). |
| `--ppa-profile <compact\|balanced\|max_frequency>` | Use a clean built-in PPA profile for this call. It replaces the Target's advanced backend settings; expert flags below still apply on top. |
| `--flatten` / `--no-flatten` | Override the Target's hierarchy flattening. |
| `--frontend <sv2v\|slang>` | Override the Target's RTL frontend, mainly for diagnosis. |

Expert options, for tuning experiments:

- Yosys/ABC: `--abc-recipe <default|balanced|fast>` or `--abc-script <script>`,
  `--generic-abc-before-mapping` / `--no-generic-abc-before-mapping`,
  `--abc-delay-ps <ps>`.
- OpenROAD: `--utilization-pct <percent>`, `--placement-density <fraction>`,
  `--repair-setup` / `--no-repair-setup`, `--repair-hold` / `--no-repair-hold`,
  `--gate-cloning` / `--no-gate-cloning`, `--setup-margin-ns <ns>`,
  `--repair-tns-percent <percent>`.

**Verdict.** The report gives area, cells, per-clock timing and Fmax, latches,
and warning counts. Combinational loops and multiple drivers in the final netlist
fail the run. Other actionable warnings give a `warn` grade but still exit `0`;
known-benign warnings are counted with a rationale. `synthesis_ok_<target>` is
satisfied only when synthesis completes, the final structural check ran, and
every configured threshold passes.

## `fpga`

`fpga` runs FPGA implementation through host-provisioned AMD Vivado. The Target
owns the FPGA part, top module, parameters, and XDC constraints.

```bash
booley flow fpga --target fpga_soc
booley flow fpga --target fpga_soc --ppa-profile max_frequency --baseline main
```

| Option | Effect |
|---|---|
| `--baseline <git-ref>` | Compare implementation metrics with another revision. |
| `--ppa-profile <compact\|balanced\|max_frequency>` | Override the Target's optimization intent for this call (default: Target `flow_options.ppa_profile`, then `balanced`). Applies to baseline and candidate alike. |
| `--no-cache` | Force a fresh implementation instead of reusing a matching cached result. |

| Profile | Vivado strategies | Intent |
|---|---|---|
| `compact` | `Flow_AreaOptimized_high` / `Area_Explore` | Smaller resource use. |
| `balanced` | Vivado defaults | Default trade-off. |
| `max_frequency` | `Flow_PerfOptimized_high` / `Performance_ExplorePostRoutePhysOpt` | Higher Fmax. |

A profile states intent, not a guaranteed result. Target `synth` and `pnr`
fields select Edalize engines; they are not Vivado strategy overrides.

**Verdict.** The report gives LUT/FF/BRAM/DSP utilization, routed per-clock
timing and Fmax, and critical conditions (latches, combinational loops,
multi-driven nets). `fpga_impl_ok_<target>` is satisfied only when metrics are
complete, timing and configured thresholds pass, and no critical condition is
present. Power is not measured.
