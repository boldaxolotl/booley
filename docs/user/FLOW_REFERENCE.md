# Booley Flow reference

Booley ships four built-in Flows: `sim`, `lint`, `synth`, and `fpga`. This page
explains how to run each one, what its result means, and where to look next.

Related references:

- [CONFIG.md](CONFIG.md): Targets, tests, constraints, Criteria, and Flow policy.
- [SUPPORTED-EDA-TOOLS.md](SUPPORTED-EDA-TOOLS.md): which EDA tools and versions
  each Flow supports.
- [USAGE.md](USAGE.md#booley-flows--specialists): the day-to-day Developer Agent workflow.
- [FLOW_REPORTS.md](../internals/FLOW_REPORTS.md): JSON report schemas and file
  layouts, for scripts that consume Flow output.

## Running a Flow

Normally you ask the Developer Agent to run a Flow. Name the
Target, and for `sim` name the test (or ask for the Target's full suite). To
reproduce or debug a run yourself, use the CLI inside the Sandbox:

```bash
booley flow lint  --target lint_soc
booley flow sim   --target sim_soc --test reset
booley flow synth --target synth_soc
booley flow fpga  --target fpga_soc
booley flow <name> --help          # the authoritative option list
```

Help groups shared controls under **Common**, reporting under **Output**, then
Flow-specific options. Synth's **Expert: ABC** and **Expert: OpenROAD** sections
come last and remain visible in ordinary `--help`. Boolean override pairs share
one help entry; leaving them unset preserves Target/profile/backend resolution.
Each option explains omission behavior. Omitted Target selection follows the
eligible Target cardinality rule below; Simulation resume restores manifest selection.

### Live progress and logs

Direct built-in CLI calls (including `python3 -m booley.flows.<name>`) show
progress on **stderr**. A capable terminal displays one status line and up to
six recent output lines, refreshed at most four times per second. Status gives
the Flow, Target, current work unit, invocation elapsed time, command elapsed
time, and age of the last observed output. It fits the terminal when resized.
Elapsed time advances during silent work; it is neither a percentage nor proof
that the EDA process is healthy. EDA tools may buffer their output.

TTY status and independent completion messages use adaptive durations: `59s`,
`1m 00s`, `1h 00m`, `1d 00h`. Every positive subsecond value uses one decimal
(`0.0s`, `0.4s`, or rounded `1.0s`); nonpositive durations display `0s`.
Last-output ages use `just now` below five seconds, then `5s ago`, `1m ago`,
`1h ago`, or `1d ago`. Redirected newline records keep their existing elapsed
seconds tokens and UTC prefixes. JSON, report document bytes, and printed
report excerpts keep their existing representation.

Redirected/non-TTY stderr, `TERM=dumb`, and short terminals use flushed newline
records without terminal escapes or carriage-return rewrites. For example:

```text
[booley-progress] 2026-10-05T13:00:00Z synth Target=synth_soc stage=candidate: synthesis elapsed=0s transcript=/work/.booley/.runtime/flow-console/invocation-abc/tool-output.log
[booley-progress] 2026-10-05T13:00:05Z synth Target=synth_soc stage=yosys elapsed=5s heartbeat (elapsed time; process health unknown)
[booley-progress] 2026-10-05T13:00:05Z synth Target=synth_soc stage=yosys elapsed=5s output: Executing synthesis passes.
```

Records include UTC timestamps and announce work-unit changes and a five-second heartbeat with up to six
new output lines. Synth observes its sv2v/Yosys/OpenROAD files; FPGA observes
Vivado's main and child-run logs. Unknown stage markers leave the current work
unit in place. Simulation identifies builds, reuse, Pre-Sim Commands and test
attempts; lint identifies configuration and checking. Current and baseline
work are labeled separately. Queue waiting never consumes the EDA timeout.

When Simulation runs several tests at once (`[jobs].max_heavy` above 1 with more
than one test), each test is a lane. Its records and transcript labels also carry
`lane=<tests>/<attempt>`, for example `lane=alpha/1a2b3c4d`, and each lane keeps its
own six recent output lines, printed on the heartbeat, when that lane moves to
another work unit, and at the end of the run.
The heartbeat then shows `stage=lanes=<count>`, and a terminal shows one row per
running lane. One test at a time keeps the single-stage format.

Place presentation flags **after the Flow name**:

```bash
booley flow synth --target synth_soc --quiet
```

`-q`/`--quiet` suppresses new progress, live output and log footers, and creates
no observation transcript. Verdicts, errors and admission notices still appear.
Sim's existing `--verbose` controls detailed resume mismatch diagnostics;
`--result-verbosity` continues to control result detail independently. Neither
option changes live output. Follow the printed transcript path for full output.

The existing verdict and return code are unchanged. Exact log paths are printed
before dispatch as they become known and repeated in a separate stderr footer
following the verdict. The append-only `tool-output.log` records observed pipe
and stage-file chunks with source/work-unit labels. It preserves original bytes,
including internal result records filtered from the display. Cross-stream order
is observational, not authoritative. Original stage logs remain the tool's
source files. `run.log` remains a capped, atomically replaced evidence snapshot;
it is not a full append-only transcript.

Paths are **inside the Sandbox**. Follow the exact printed transcript path from
a second terminal attached to that existing Sandbox:

```bash
booley session enter -- tail -f /work/.booley/.runtime/flow-console/invocation-abc/tool-output.log
```

For a replaceable `run.log`, use `tail -F` and expect bounded snapshots rather
than complete output. Temporary baseline paths are labeled; their observed
content survives in the invocation transcript after baseline cleanup, and they
are not offered as retained logs in the footer. A transcript write failure or
blocked writer reports that the transcript is incomplete/unavailable and
omits a full-log path claim; the Flow continues with its normal evidence.

Each invocation reserves its own directory under the Project runtime's
`flow-console/`. Output consumes disk in proportion to observed bytes. Delete
completed invocation directories manually when not needed; leave any
directory containing an `active` marker untouched. There is no automatic pruning.
Help, validation-only, dry-run and quiet calls create no transcript.

Typed/MCP calls and runtime-composed calls retain their existing event
transport. Ordinary direct shell calls gain stderr progress even without a TTY,
including Developer Agent shell calls. Developer Agents or scripts requiring the previous
captured transcript should pass `--quiet`.

### Choosing Targets

For sim, lint, synth, and fpga on CLI and MCP, omitting `--target` selects
exactly one eligible Target, and that selection is announced. With zero or multiple
eligible Targets, the run exits 2 and lists eligible selectors (`(none)` for zero).
`sim --resume-from` takes its Target from the Campaign it resumes, independent
of the current catalog.

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
| `-C/--project PATH` | Select the Project Root or worktree; nested paths discover their owning checkout. Omission preserves cwd discovery. |
| `--report-dir <path>` | Write reports here instead of the default report root. |
| `--diagnostic` | Run diagnostically without recording Goal evidence. |
| `--dry-run` | Resolve and validate the work, print the plan, and stop. No EDA tool runs and no state changes. |
| `--timeout DURATION` | Active-time budget per work unit (queue time is not counted). Overrides `[flows.<name>].timeout_ms`. Simulation builds instead use `[flows.sim].build_timeout_ms`, and Pre-Sim Commands use an independent fixed budget; see [Simulation timeouts](CONFIG.md#simulation-build-pre-sim-and-run-timeouts). |

## Running a Specialist

Inside the Sandbox, use the public route or the supported Python module entry:

```bash
booley specialist
booley specialist reviewer --help
booley specialist reviewer --category rtl --focus bugs --scope rtl --timeout 30m
booley specialist coverage_analyst --campaign reports/sim/12/targets/sim_soc/coverage.json
python -m booley.specialists.reviewer --category rtl --focus bugs --scope rtl
```

The optional separator in `booley specialist reviewer -- --scope "rtl,ip" ...`
forwards arguments unchanged. Listings include enabled Project Specialists and
exclude hidden endpoints. Host listing and help work; execution requires the
Sandbox, for example `booley session enter -- booley specialist reviewer ...`.

Specialists share `-C/--project PATH`, `--report-dir`, `--diagnostic`, and `--target`
where supported. Reports default to `mcp-tool-reports/` under the resolved Booley Directory, or the runtime directory when supplied; an explicit flag takes precedence.
`--model` selects a tier subject to the Specialist floor and configured role pin;
`--max-turns` accepts a positive integer. These and `--timeout` are CLI-only:
MCP rejects `model`, `max_turns`, `timeout`, and `timeout_ms` for Specialists.

`--timeout DURATION` is a model-call budget, not a whole-invocation deadline.
Use positive integer seconds (`90` or `90s`), minutes (`30m`), hours (`2h`),
or descending combinations (`1h30m`, `1m30s`). Fractional values, signs,
whitespace, millisecond suffixes, repeated units and zero totals are rejected.
Existing defaults and minimums remain unchanged. Seconds-only providers round
up with `(timeout_ms + 999) // 1000`. Flow budgets retain their work-unit scope.
Hidden `--timeout-ms` retains positive integer milliseconds, including 1 ms,
for one compatibility release; it prints a deprecation notice on stderr.
MCP parameters and `booley.toml` milliseconds remain unchanged.

Project selection works before or after the endpoint name for built-ins and
visible Specialists: `booley flow -C ../other lint --target lint`, or
`booley flow lint --target lint -C ../other`. A leading `--` separates the
main CLI from the endpoint options; a subsequent `--` starts an opaque payload.
Unknown endpoint arguments stop selector extraction. Custom endpoints use outer
selection (`booley flow -C ../other custom`); inherited common parsers support
it. Overrides that bypass the common entrypoint reject explicit outer selection.
Plugin-owned options, including `--timeout short|long`, keep their contracts.

Hidden `--work-dir` remains a literal path alias for one compatibility release,
with a stderr notice. Canonical selection discovers nested checkout paths and
ignores `RTL_PROJECT_ROOT`; the alias preserves literal resolution. Never mix
selectors or timeout flags, even if their values agree. Module entrypoints expose
the same canonical options for Booley-owned endpoints. Saved reproduction commands,
internal Simulation backend protocols and historical evidence keep their contracts.

## Reading results

Every built-in Flow uses the same exit codes:

| Exit | Meaning | What to do |
|---:|---|---|
| `0` | The Flow ran and the check passed. | Nothing. Advisory findings (such as lint warnings with `warnings_as_errors = false`) may still be reported. |
| `1` | The Flow reached a design verdict and it failed. | Fix the RTL or testbench. |
| `1` | Simulation was `inconclusive` (for example, no pass/fail sentinel after a clean run). The Criterion is skipped. | Repair the testbench verdict or missing evidence, then rerun. |
| `2` | The Flow could not reach a verdict: bad configuration, missing tool, crash, tool or build timeout. | Fix the setup; the result says nothing about the design. |

A simulation run that exceeds its run budget gets a `timeout` verdict and exits
`1`; it fails `SIM:{target}`. Investigate a possible RTL/testbench deadlock, or
raise `--timeout` or `[flows.sim].timeout_ms` if the test legitimately needs
longer. Build, Elaboration Check, and Pre-Sim Command timeouts instead exit `2`.

The CLI prints a final verdict card: stdout on success, stderr on failure.

**Reports.** Each run writes a structured JSON report and complete logs. By
default they go under `flow-reports/` in the Booley Directory (Goal and
Specialist runs use their runtime directory). `--report-dir` overrides this. Booley
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
booley flow sim --target sim_soc --test reset --test irq  # exact tests (see ordering below)
booley flow sim --target sim_soc --tests-file smoke.txt   # names from a file
booley flow sim --target sim_soc --test irq --trace       # capture a waveform
booley flow sim --target sim_soc --test irq --coverage    # collect coverage (Verilator only)
booley flow sim --target sim_soc --test stress+soak       # a Test Variant
booley flow sim --target sim_soc --test stress@42         # one seed
booley flow sim --target sim_soc --test stress+soak@x20   # 20 drawn seeds
booley flow sim --target sim_soc --seed random            # a drawn seed for every Test Run
```

| Option | Effect |
|---|---|
| `--test <test run>` | Run the named Test Runs: `test`, `test+variant`, `test@seed`, `test+variant@seed`, or `test[+variant]@xN` for N drawn seeds. Repeat for more. See [Test Runs, Test Variants and seeds](#test-runs-test-variants-and-seeds). |
| `--seed <N\|random>` | Use seed `N` (1..2147483647) for every selected Test Run, or `random` to draw one seed per Test Run. Cannot be combined with a `--test` that names a seed (`test@seed`, `test@xN`), with an elaboration mode, or with `--resume-from`. |
| `--tests-file <path>` | Read Test Run selectors from a file, one per line (blank lines and `#` comments ignored). Cannot be combined with `--test`. |
| `--mode <simulate\|elab-only\|elab-only-standalone>` | Run tests (default), or only elaborate (see [Elaboration checks](#elaboration-checks)). |
| `--trace` | Capture a waveform. Use it to debug a failure, not for pass/fail checks. |
| `--coverage` / `--cov` | Collect coverage (see [Coverage](#coverage)). |
| `--no-waivers` | With `--coverage`, report raw coverage without applying approved waivers. With a Coverage Criterion it also needs `--diagnostic` (see [Collecting vs. gating](#collecting-vs-gating)). |
| `--resume-from <manifest.json>` | Resume an interrupted run (see [Resuming](#resuming-an-interrupted-run)). |
| `--verbose` | With `--resume-from`, include full Simulation Campaign mismatch pointers and values (also with `--dry-run`). |
| `--result-verbosity <compact\|full>` | cocotb console detail. `full` prints every testcase; the complete XML/JSON is always kept. |
| `--no-kill` | Skip the pre-run cleanup of stale simulator processes. Diagnostic use only. |

Test selection rules:

- Coverage selections use deterministic sorted test-name order, regardless of
  the `--test` or `--tests-file` input order. This stabilizes Campaign identity
  and run numbering: `--test gap --test full --coverage` assigns
  `run:001:full@1`, then `run:002:gap@1`. A run id ends in its Test Run name.
  Several Test Runs of one Test (two Test Variants, or `test@xN`) stay
  together in the order you named them, each in its own simulator process;
  the merged coverage is the union over all of them.
- Plain selections preserve explicit input order; an unfiltered suite follows
  the registry order in `tests.toml`. HDL tests are scheduled in that order.
  cocotb batches filter the selected set within one simulator process; cocotb
  controls the order in which test functions execute.
- Every named test must exist in every selected Target; unknown, duplicate, or
  empty selections fail before anything runs.
- A plain run (no `--test`/`--tests-file`) honors the `skip` entries in
  `tests.toml`. Naming tests gives an exact explicit suite, which overrides those
  entries. A Target whose whole suite is skipped fails preflight instead of
  passing vacuously.

Multiple simulator processes can run in parallel up to `[jobs].max_heavy`
(default `1`, i.e. serial). Tests sharing a literal `run_cwd` always run one at a
time.

### Test Runs, Test Variants and seeds

A **Test Variant** is a Test with a fixed set of extra run-time arguments and
environment, declared in
[`tests.toml`](CONFIG.md#test-variants-variants). A **Test Run** is one Test
Variant simulated with one seed. Every result, report row and Goal names a
Test Run, written `test[+variant][@seed]`: `reset@1`, `stress+soak@42`.

`--test` takes a selector that names one or more Test Runs:

| Selector | Test Runs |
|---|---|
| `stress` | The default Test Variant of `stress` at the Target's Default Seed. |
| `stress+soak` | The Test Variant `soak` at the Default Seed. |
| `stress@42`, `stress+soak@42` | That Test Variant with seed 42. |
| `stress@x20`, `stress+soak@x20` | 20 Test Runs of that Test Variant, each with a different seed that Booley draws at random. N is 1 to 1000. |

A seed is a decimal number from 1 to 2147483647, written without a sign or
leading zeros. The **Default Seed** is `1` unless the Target sets
[`default_seed`](CONFIG.md#seeds-seed-default_seed). A run of the whole Target
(no `--test`) runs the default Test Variant of every Test that is not skipped,
each at the Default Seed.

`--seed` sets the seed of every Test Run the request selects, including a run
of the whole Target. `--seed 42` uses seed 42 for all of them. `--seed random`
draws a separate seed for each Test Run. Booley draws every seed before the
run starts and records it, so the report always names the seed that ran.

Booley refuses these requests before anything builds:

- `--seed` together with a `--test` that names a seed (`test@seed` or
  `test@xN`). Name the seed in one place.
- `--seed` together with `--mode elab-only`, `--mode elab-only-standalone` or
  `--resume-from`.
- A selector that names a Test Variant the Test does not declare, or a
  Variant of a Test that declares none.
- Two selectors that resolve to the same Test Run, such as `stress` and
  `stress+short` when `short` is the default Variant.
- More than 1000 Test Runs of one Target in one request. Split the request.
- Any seed for an **unseeded** Target: `--seed`, `test@seed` or `test@xN`. An
  Icarus Target with an HDL testbench is unseeded until its `tests.toml`
  section declares a `seed` template, and the refusal says how to add one. An
  HDL-testbench Target of any simulator other than Verilator and Icarus is
  unseeded with or without a template. Test Runs of an unseeded Target are
  written without `@seed`.

How the seed reaches the simulation:

| Target | Seed mechanism |
|---|---|
| Verilator, HDL testbench | `+verilator+seed+<seed>`, plus the Target's `seed` template when it declares one |
| Icarus, HDL testbench | the Target's `seed` template; unseeded without one |
| Cocotb Target | cocotb's seed variable (`COCOTB_RANDOM_SEED` on cocotb 2.x, `RANDOM_SEED` on 1.x) |

Test Runs that differ in arguments, environment or seed never share a
simulator process. An HDL-testbench Target starts one process per Test Run. A
Cocotb Target batches the Test Runs that have the same environment and seed,
so a request with two seeds runs at least two batches. Test Variants and seeds
are run-time inputs: they reuse the Target's simulator build.

**Report rows.** A Target's report lists its Test Runs, up to 32 rows, when
any of them did not pass, ran with a seed other than the Default Seed, or
belongs to a Test that declares Test Variants, or when a Test ran more than
once:

```text
  Test Runs: 3 (2 passed, 1 failed)
    PASS         stress+soak@1804289383
    FAIL         stress+soak@846930886
    PASS         stress+soak@1681692777
  Rerun a failed Test Run with the same seed:
    booley flow sim --target sim_soc --test=stress+soak@846930886
```

A row is `PASS`, `FAIL`, `ERROR` (an infrastructure problem stopped the run)
or `INCONCLUSIVE`. Each failed Test Run gets a rerun command that selects
exactly it. The machine-readable rows are `test_runs[]` in the Target's
`simulation.json`, each with `name`, `test`, `variant`, `seed`, `args`, `env`,
`fingerprint` and `grade`.

**Directories and file names.** Per-run evidence is named by Test Run. An
HDL-testbench Target writes each run log to
`sim_<target>/tests/<test run>/run.log`, for example
`tests/stress+soak@42/run.log`. A Cocotb Target writes one directory per
batch: `tests/batch/` for the default Test Runs, and `tests/batch-<hex>/` for
a batch with another seed, Variant or environment. Coverage run ids and native
database names end in the Test Run name (`run:001:full@1`).

**Pre-Sim Commands** receive the Test Run in
[`BOOLEY_TEST_RUN_*` variables](CONFIG.md#pre-sim-commands-flowssimpre_run_commands).

### Verdicts and Criteria

Each test gets one verdict:

- **HDL testbenches** pass or fail through the configured pass/fail sentinels;
  a fail sentinel always wins.
- **cocotb testbenches** use cocotb's result file; assertion output can still
  fail the test.
- A test that exits cleanly without a valid verdict is **inconclusive**, never a
  pass. So is a `--trace` run that produced no fresh waveform. An inconclusive
  run exits `1` and skips the `SIM:{target}` Criterion.
- A simulation run that exceeds its run budget gets a **timeout** verdict,
  exits `1`, and fails `SIM:{target}`.
- A test stopped by a Booley guard stays **aborted** even if the simulator
  exits `0`. An infrastructure abort is exit `2`. A `$readmemh` input file that
  is missing and not declared in the Target is a design failure
  (`missing_input`, exit `1`). Either way the cause is kept, and assertions are
  reported as not observed.

Goal Mode translates Goals into evidence keys. Ordinary Interactive Mode runs
report verdicts without a Goal Record. In an active Goal worktree, pass its
absolute root as `work_dir` to Booley Flows, Specialists, Goal tools,
`booley_targets`, and `booley_report` so calls select that worktree.
`booley_status`, `booley_poll`, `booley_cancel`, and `bwave` accept no `work_dir`. See
[worktree routing](USAGE.md#working-with-evidence) for the full inventory and
when `work_dir` is required.

- `SIM:{target}` requires the default Test Variant of every Test of the
  Target that is not skipped, each at the Default Seed.
- A simulation Goal with a `test` selector requires the Test Runs that the
  selector names: one Test Run, or N Test Runs with different seeds for
  `test@xN`.
- `CYCLE_COUNT:{target}` requires one passing Test Run and all its
  Cycle Count thresholds. A relative threshold runs the same Test Run, with
  the same Test Variant and seed, at the Goal base commit. If the base commit
  cannot run it (the Test is not registered there, or the Target cannot apply
  the seed or the Variant's arguments), the comparison is inconclusive and
  the report names the reason.
- `ELAB:{target}` requires a successful build; see below.

For example, entry can include:

```json
[{"family": "SIM", "target": "sim_core"},
 {"family": "SIM", "target": "sim_core", "test": "stress+soak@x20"},
 {"family": "CYCLE_COUNT", "target": "sim_core", "test": "smoke",
  "thresholds": {"cycle_count_max": 100}}]
```

Goal Mode resolves each of these Goals to exact Test Runs at entry and
**freezes** them, including the 20 seeds it draws for `stress+soak@x20`. A
Test Run is evidence for a Goal only when it is one of the Goal's frozen Test
Runs with the same declared arguments and environment. Inside the Goal
worktree:

- `booley flow sim --target sim_core` takes its Test Runs from the Target's
  Goal that covers every Test (a simulation Goal without `test`, or a
  coverage Goal with `tests = "all"`): its frozen Test Runs, and the default
  Test Run of any Test added to the Target since. With no such Goal it runs
  the default Test Run of each Test. A Goal that names a Test Run gets
  evidence from this run only when that Test Run is among them.
- `--test` with the name of a frozen Test Run, as `goal_status` lists it,
  runs that Test Run.
- `--test stress+soak@x20` runs the 20 frozen seeds of the Goal with the same
  Target, Test Variant and N. With no such Goal, Booley draws 20 new seeds and
  the report says that these Test Runs are diagnostic.
- Every other Test Run is **diagnostic**: it is reported, it records no Goal
  evidence, and its failure blocks no Goal. The report lists diagnostic
  failures under their own heading.
- `--diagnostic` records nothing, even when the run executes a Goal's frozen
  Test Runs: a pass does not count for the Goal and a failure does not make
  the frozen Test Run `failed`. The report names each Goal whose frozen Test
  Runs ran and the ones that failed. Run again without `--diagnostic` to
  record them.

A simulation Goal is met when every one of its frozen Test Runs has passed at
the current file contents. The Test Runs can pass in separate invocations. See
[Goals that name Test Runs](USAGE.md#goals-that-name-test-runs).

### Elaboration checks

`--mode elab-only` compiles, elaborates, and links the simulator without running
tests. It is a fast "does it build?" check, and a later full run reuses the
build. `--mode elab-only-standalone` adds a sweep that elaborates each reusable
RTL module on its own and can satisfy `elaborate_standalone`.

The Target build in either mode uses the shared
[`build_timeout_ms`](CONFIG.md#simulation-build-pre-sim-and-run-timeouts)
budget rather than `--timeout`.

In Goal Mode, a successful build satisfies `ELAB:{target}`, whether it
comes from `--mode elab-only` or from the build step of a normal run. It is
recorded as soon as the build succeeds, so a later test failure does not erase
it.

Both modes skip Pre-Sim Commands and reject run-only options (`--test`,
`--tests-file`, `--trace`, `--result-verbosity full`, `--no-kill`). A compiler
error in the RTL is exit `1`; a tool, setup, timeout, or out-of-memory problem is
exit `2` and leaves Criteria unchanged. With several Targets, every Target is
checked and exit `2` takes precedence over `1`.

### Resuming an interrupted run

Resume is for long, heavy runs, such as a multi-hour regression, where
re-running tests that already finished is expensive.
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

- Target, Test Runs, mode, coverage, trace, and `--no-waivers` come from the
  manifest and cannot be given again. The manifest records every Test Run with
  its Test Variant and seed, so a resumed run of `--seed random` or `test@xN`
  continues with the seeds it drew at the start and never draws new ones.
  Resuming a `--no-waivers` run for a Target with a Coverage Criterion also
  needs `--diagnostic`. Timeout and output options may change.
- One resume continues one Target. For a multi-Target run, resume each
  unfinished Target's manifest separately.
- Within a Target, the retry unit is a work item:

  | Testbench | Work item | On resume |
  |---|---|---|
  | HDL | one Test Run | Only Test Runs without a recorded result run again. |
  | cocotb | the whole batch | An interrupted batch re-runs all of its tests. |
  | `--coverage` | the whole collection | Refused (exit `2`) until the collection has a recorded result; then only publication is finished. |

- Resuming a coverage run whose collection never finished is refused with exit
  `2`, before anything builds or runs, and `--dry-run` reports the same
  refusal. Start a new `booley flow sim --coverage` run instead: the refusal
  prints the command, which names the interrupted run's Test Runs so that the
  new run repeats their Test Variants and seeds. Resume finishes a coverage
  run only when the interruption hit while results were being published.
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
  module that is never instantiated can produce no points. After complete
  collection, Booley reports each module-bearing RTL source with no native
  coverage points as an advisory warning. Percentages remain unchanged.
- This is a file-level check: a measured module can conceal an unmeasured sibling
  in the same file. TB-tagged and FuseSoC include files are excluded, as are
  package-only, interface-only, parameter-only, and defines-only sources.
- Zero points means no native measurement records, including unsupported record
  classes. Zero hits, waivers, and point eligibility do not create this warning.
  It does not establish deadness or lack of instantiation.
- Candidates come from a lexical scan at Preflight: `verilogSource` and
  `systemVerilogSource` inputs that are neither include files nor `tb`-tagged
  and whose text, with comments removed, has a line starting with `module` or
  `macromodule`. The scan does no preprocessing, so a module inside an `ifdef`
  counts at face value. A file whose records Verilator remaps elsewhere
  (`` `line `` directives) may still be reported.
- Points are per instance: a module instantiated four times contributes four
  sets of points, and each instance must be exercised.

Each Target gets its own Coverage Campaign. Nothing is merged across Targets or
across runs.

#### Quick start

```bash
# 1. Collect coverage for the full suite
booley flow sim --target sim_soc --coverage
```

Then call the `coverage_analyst` Specialist from your Developer Agent session with
`campaign="<reports>/sim/12/targets/sim_soc/coverage.json"` for Waiver Candidates
and testbench improvements. The verdict card prints the exact
`coverage.json` path to pass as `campaign`.

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

- Every coverage run applies the approved waivers: waived points are reported
  `waived` and counted in `waived_points`, and they are left out of the eligible
  points and percentages. An invalid approval file, or an approval that does not
  match the collected points, blocks that Target's evaluation and exits 2, with
  or without a Criterion. Fix the approvals or use `--no-waivers`. When
  collection is incomplete (already exit 2), approvals are not matched against
  the partial points and nothing is waived.
- **Ungated** (no Coverage Criterion): Booley collects and stores the Campaign
  with evaluation `not_requested`. Use it to explore.
- **Gated** (Goal Mode with a `COVERAGE:{target}` Goal): Booley also checks
  the thresholds. Only a persisted `pass` satisfies
  the Criterion. A Goal remains unmet until strict evidence passes; see
  [Coverage waivers at review](#coverage-waivers-at-review). The thresholds
  alone decide the Goal: a Test Run that fails its checks does not make it
  unmet, as long as the run still wrote its coverage data.
- **Raw numbers** (`--no-waivers`): applies no approved waivers. With a
  Coverage Criterion it needs `--diagnostic`; otherwise Booley exits 2 before
  anything builds. The thresholds are then evaluated on raw numbers, and no
  Criteria are recorded.

Which tests run: the Test Runs you name with `--test`/`--tests-file`, otherwise the
Target's registered suite minus `tests.toml` skips, the same as any `sim` run.
A coverage run accepts every selector form, including several Test Runs of one
Test (`--test stress+short --test stress+soak`, or `--test stress@x5`). Each
Test Run is one simulator process with its own native database, and the
merged coverage is the union over all of them. A
Criterion's `tests` list never picks tests; it says which tests the evidence
must come from. If what ran differs from that list, coverage is still collected
but gated evaluation is blocked (suite mismatch), so pass the listed tests
explicitly.

#### Coverage Criteria and waivers

A coverage Goal declares thresholds per Target:

```json
{"family": "COVERAGE", "target": "sim_counter", "tests": "all",
 "metrics": {"line": 90, "branch": 80}}
```

Only points in the RTL count toward the percentages; testbench and generated
code are reported but not scored. Points that are legitimately unreachable or
out of scope can be waived by a human in a project-wide approval directory.
Waivers are checked per Target: one invalid approval blocks that Target's
evaluation. See [Coverage configuration](CONFIG.md#native-coverage-configuration)
and [Approved coverage waivers](CONFIG.md#approved-coverage-waivers) for the
full syntax.

#### Coverage waivers at review

In Goal Mode, the Coverage Analyst's code screens Waiver Candidates and saves
those ready for human review in the local Goal Record's `waiver-candidates.json`.
An `unreachable` candidate needs a zero-hit point. A candidate carries no
approval authority; only strict coverage evidence meets a Goal.

Ask the Developer Agent to propose each needed candidate with
`goal_propose_change(operation="create", kind="waiver", goal_key=...,
candidate_id=..., rationale=...)`. Inspect the point, source, Campaign,
coverage delta, and justification before approving or rejecting the exact
proposal. The client form records your decision and reason; chat fallback
records your quoted instruction. No candidate is accepted by default.

Approval writes the bound waiver and any review proof into the configured
[approval directory](CONFIG.md#approved-coverage-waivers) and records the
change in the Change Log. Rerun coverage to obtain fresh strict evidence;
approval itself does not meet a Goal. A stale or invalid candidate needs a new
Campaign and Analyst screening. A rejection is retained for that exact point
and source content. Finish includes the approved changes and evidence in its
Review Package, leaving merge to your separate instruction.

#### Results

Simulation, collection, and threshold evaluation are reported independently: a
failing test can still yield valid, passing coverage, and a fully passing suite
can miss a threshold. The exit code reflects simulation and collection:

| Exit | When |
|---:|---|
| `2` | Coverage could not be trusted: preflight rejection, collection or infrastructure failure, incompatible data, or blocked evaluation (e.g. an invalid waiver or a suite mismatch). |
| `1` | A test failed. |
| `0` | No test failed and coverage can be trusted. A missed threshold also exits `0`: the Coverage Criterion is recorded as not met and the verdict card names the missed metric. |

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
      coverage.json          current Coverage Campaign manifest
      coverage-points.jsonl.gz
      native/raw/            one Verilator database per Test Run
      native/merged/         merged Verilator database
      hooks/                 hook evidence, when collected
```

Cycle counts: the numbered `report.json` lists every test's count under
`cycle_counts.<Target selector>[]` as `{test, cycle_count}` (`test: null` for
an unnamed test, or `cycle_counts_error: unavailable` if counts couldn't be
collected). Each Target's `simulation.json` has them as `tests[].cycles`; find
it via `detail.campaigns.<selector>.artifacts.simulation`, whose `path_base`
may point at the original run after a resume.

The Target reference's `coverage_campaign.path` resolves from the origin Target
directory, as declared by `coverage_campaign.path_base: origin_target`. The
nested manifest binds the point store and contains rollups and verdicts; its
native artifact paths are relative to the **Coverage Campaign directory**.

Pass the numbered Target-level `coverage.json` reference to the Coverage Analyst.
It authenticates the nested Coverage Campaign and reads the matching completed Simulation result;
it does not accept the nested manifest directly. Manifest readers
in `booley.flows.sim.coverage_campaign_store` take the resolved Campaign manifest,
not the reference. Never edit or pass the point store directly, and keep the
reference and enclosing Simulation Campaign together.

`<reports>/sim.json` is a mutable, last-writer-wins compatibility copy of the
newest invocation's `report.json`. It may contain Campaign pointers, but it is
not a stable Campaign selection: a later failed run can replace it with empty
`detail`. Use the numbered `<reports>/sim/<N>/...` paths for consumers. Booley
never infers a latest Campaign or merges coverage across Targets or runs.

#### Analyzing a Campaign

`coverage_analyst` explains one Campaign: what is uncovered, likely reasons,
which tests to add, and Waiver Candidates for human review.

Call the `coverage_analyst` Specialist from your Developer Agent session with `campaign="<exact coverage.json>"`.

It never runs simulation, changes Criteria, or approves waivers; in Goal
Mode Booley records its candidates for
[approval at review](#coverage-waivers-at-review). See
[USAGE.md](USAGE.md#coverage_analyst).

#### Cleaning up completed Campaigns

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
- If reports live outside the standard Booley Directory locations, `--full` may ask
  for `--project-data "$PROJECT_DATA"`.
- Pruning refuses runs that are still active. `--full` also refuses files the run
  did not produce; `--native-target` removes the Target's whole `native/` directory.
  An interrupted cleanup is safe to retry with the same arguments.

## `lint`

`lint` runs the linter chosen by each Target: Verilator for structural and
semantic checks, Verible for style and naming. To run both, declare two Targets
and select both.

```bash
booley flow lint --target lint_soc,lint_style --scope rtl/fifo.sv
```

For Target recipes, see [Verilator lint Target](CONFIG.md#verilator-lint-target)
(`.vlt` waivers with `file_type: vlt`) and
[Verible lint Target](CONFIG.md#verible-lint-target) (style rules and waivers).
[Target authoring](CONFIG.md#target-authoring) covers EDA-tool compatibility.

- `--scope <file,...>` limits reported findings to the listed files.
- `[flows.lint].warnings_as_errors` decides whether warnings fail the exit code.
  Either way the report keeps the real counts, and `LINT:{target}` is only
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
SDC. Physical fanout limits come from OpenROAD's defaults, the library, or the
Project's SDC. OpenROAD uses an implicit maximum fanout of 50 when neither the
library nor the SDC sets a limit. For very large nets, set a suitable limit in
the Target's SDC: `set_max_fanout <n> [current_design]`.

Synthesis mode is Target-owned: there is no per-call `--synth-mode` option.

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
known-benign warnings are counted with a rationale. `SYNTH:{target}` is
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
multi-driven nets). `FPGA:{target}` is satisfied only when metrics are
complete, timing and configured thresholds pass, and no critical condition is
present. Power is not measured.
