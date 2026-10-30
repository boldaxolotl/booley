# Simulation (`sim`)

`sim` builds and runs the tests registered for a simulation Target. The Target
chooses the simulator (Verilator or Icarus) and the testbench style (HDL or
cocotb).

## Running tests

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
| `--coverage` / `--cov` | Collect coverage (see [Coverage](COVERAGE.md)). |
| `--no-waivers` | With `--coverage`, report raw coverage without applying approved waivers. With a Coverage Criterion it also needs `--diagnostic` (see [Collecting vs. gating](COVERAGE.md#collecting-vs-gating)). |
| `--resume-from <manifest.json>` | Resume an interrupted run (see [Resuming](#resuming-an-interrupted-run)). |
| `--verbose` | With `--resume-from`, include full Simulation Campaign mismatch pointers and values (also with `--dry-run`). |
| `--result-verbosity <compact\|full>` | cocotb console detail. `full` prints every testcase; the complete XML/JSON is always kept. |
| `--no-kill` | Skip the pre-run cleanup of stale simulator processes. Diagnostic use only. |

Test selection rules:

- Coverage runs sort tests by name, whatever order you gave them:
  `--test gap --test full --coverage` assigns `run:001:full@1`, then
  `run:002:gap@1`. A run id ends in its Test Run name.
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

## Test Runs, Test Variants and seeds

A **Test Variant** is a Test with a fixed set of extra run-time arguments and
environment, declared in
[`tests.toml`](TESTS.md#test-variants-variants). A **Test Run** is one Test
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
[`default_seed`](TESTS.md#seeds-seed-default_seed). A run of the whole Target
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
`simulation.json`; see [FLOW_REPORTS.md](../internals/FLOW_REPORTS.md#simulationjson).

**Directories and file names.** Per-run evidence is named by Test Run. An
HDL-testbench Target writes each run log to
`sim_<target>/tests/<test run>/run.log`, for example
`tests/stress+soak@42/run.log`. A Cocotb Target writes one directory per
batch: `tests/batch/` for the default Test Runs, and `tests/batch-<hex>/` for
a batch with another seed, Variant or environment. Coverage run ids and native
database names end in the Test Run name (`run:001:full@1`).

**Pre-Sim Commands** receive the Test Run in
[`BOOLEY_TEST_RUN_*` variables](#pre-sim-commands-flowssimpre_run_commands).

## Verdicts and Criteria

Each test gets one verdict:

- **HDL testbenches** pass or fail through the configured pass/fail sentinels;
  a fail sentinel always wins.
- **cocotb testbenches** use cocotb's result file; assertion output can still
  fail the test.
- A test that exits cleanly without a valid verdict is **inconclusive**, never a
  pass. So is a `--trace` run that produced no fresh waveform. An inconclusive
  run exits `1` and skips the `SIM:{target}` Criterion.
- A simulation run that exceeds its run budget gets a **timeout** verdict,
  exits `1`, and fails `SIM:{target}`. Look for an RTL or testbench deadlock,
  or raise `--timeout` or `[flows.sim].timeout_ms` if the test needs longer.
  Build, Elaboration Check and Pre-Sim Command timeouts are infrastructure
  errors and exit `2`.
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
[worktree routing](../internals/MCP-TOOLS.md#work_dir-routing) for the full inventory and
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
Runs with the same declared arguments and environment, run by a Goal-bound
`sim` MCP call (one that passes the Goal worktree as `work_dir`). CLI runs
(`booley flow sim`) never record Goal evidence. In a Goal-bound call:

- `sim` with `target="sim_core"` takes its Test Runs from the Target's
  Goal that covers every Test (a simulation Goal without `test`, or a
  coverage Goal with `tests = "all"`): its frozen Test Runs, and the default
  Test Run of any Test added to the Target since. With no such Goal it runs
  the default Test Run of each Test. A Goal that names a Test Run gets
  evidence from this run only when that Test Run is among them.
- `test` set to the name of a frozen Test Run, as `goal_status` lists it,
  runs that Test Run.
- `test="stress+soak@x20"` runs the 20 frozen seeds of the Goal with the same
  Target, Test Variant and N. With no such Goal, Booley draws 20 new seeds and
  the report says that these Test Runs are diagnostic.
- Every other Test Run is **diagnostic**: it is reported, it records no Goal
  evidence, and its failure blocks no Goal. The report lists diagnostic
  failures under their own heading.
- `diagnostic=true` records nothing, even when the run executes a Goal's frozen
  Test Runs: a pass does not count for the Goal and a failure does not make
  the frozen Test Run `failed`. The report names each Goal whose frozen Test
  Runs ran and the ones that failed. Call again without `diagnostic` to
  record them.

When a simulation Goal is met: see
[Goals that name Test Runs](GOAL-MODE.md#goals-that-name-test-runs).

## Elaboration checks

`--mode elab-only` compiles, elaborates, and links the simulator without running
tests. It is a fast "does it build?" check, and a later full run reuses the
build. `--mode elab-only-standalone` adds a sweep that elaborates each reusable
RTL module on its own and can satisfy `elaborate_standalone`.

The Target build in either mode uses the shared
[`build_timeout_ms`](#simulation-build-pre-sim-and-run-timeouts)
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

## Resuming an interrupted run

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

## Configuration (`[flows.sim]`)

Simulation settings live under `[flows.sim]` in `.booley/booley.toml`.
Coverage settings are on the [Coverage](COVERAGE.md#configuration) page, and
the tests themselves are declared in [`tests.toml`](TESTS.md).

### Simulation build, Pre-Sim, and run timeouts

`[flows.sim].build_timeout_ms` is a positive-integer budget for each
simulator-image build. It defaults to `3600000` (one hour) and applies to
ordinary Simulation, native Coverage, and Elaboration Check builds.
`[flows.sim].timeout_ms` and the per-call `--timeout` override instead bound
simulator execution, or the standalone module sweep in
`elab-only-standalone`. Pre-Sim Commands have an independent fixed 600-second
budget.

```toml
[flows.sim]
build_timeout_ms = 3600000
timeout_ms = 600000
```

### Compiler cache (`[flows.sim.compiler_cache]`)

Verilator C++ compilation uses a persistent, Project-scoped ccache by default:

```toml
[flows.sim.compiler_cache]
enabled = true
max_size = "5G"
```

`enabled` must be a boolean. `max_size` accepts a positive integer followed by
`M`, `G` (decimal megabytes/gigabytes), or `Mi`, `Gi` (binary units). Zero,
fractional values, and suffixes outside this grammar are configuration errors.
`max_size` is a soft eviction target applied after each build, not a disk quota.

The cache lives in the Booley Directory runtime, survives Sandbox recreation,
and is shared by Goal worktrees and Mutation Tester builds. To inspect or empty
it, run ccache inside a Sandbox:

```sh
CCACHE_DIR=/booley-project/.runtime/compiler-cache/ccache ccache --show-stats
CCACHE_DIR=/booley-project/.runtime/compiler-cache/ccache ccache --clear
```

Booley sets `OBJCACHE`, `CCACHE_DIR`, `CCACHE_MAXSIZE`, `CCACHE_COMPILERCHECK`
and `CCACHE_BASEDIR` for the build; they override ambient and `tests.toml`
values. Make overrides of those names bypass Booley's settings and cost cache
hits. Add compiler flags through the Target environment or Verilator `-CFLAGS`,
not a Make assignment to `USER_CPPFLAGS`.

If ccache is missing or the cache location is unavailable, the build prints one
warning and compiles uncached.

**Build reuse.** Repeat runs skip the build when a retained simulator image of
the same Target variant (plain, traced, or coverage) was built from identical
inputs: sources, cores, toolchain, flags and environment. Each Simulation
result's build entry records the `cache_decision` (`hit`, `changed source,
recipe, or tool`, `reuse unsupported`, and so on), which says why a rebuild
happened. A Pre-Sim Command that only writes run-time inputs, such as firmware
in `$BOOLEY_RUN_CWD`, keeps reuse; anything it writes into the build tree forces
a rebuild. Cocotb Targets always rebuild.

### Simulation & pass/fail sentinels (`[flows.sim]`)

Booley decides sim pass/fail by scanning the testbench's stdout for a **sentinel
substring**, so a plain `$finish` (exit 0) is never mistaken for a pass. By
default it looks for `[SIM_RESULT] PASSED` / `[SIM_RESULT] FAILED`. You have two
options, and you never have to rewrite a testbench you don't own:

- Emit the built-in markers from your TB (`$display("[SIM_RESULT] PASSED")`);
  see `booley.data.refs/sim_result_sentinel.sv`.
- Keep your TB's existing wording and declare it in `booley.toml`:

    ```toml
    [flows.sim]
    pass_sentinels = ["ALL TESTS PASSED."]
    fail_sentinels = ["ERROR!", "TIMEOUT"]
    cycle_sentinels = ["CoreMark completed in:"]
    ```

    These are honored end-to-end in the built-in Icarus/Verilator flow (a fail
    sentinel wins ties). `booley doctor` greps your sim Target's TB for the
    default sentinel and warns at setup time if none is present. For a custom
    configured phrase, a missing literal is only a note because the TB may build
    it dynamically; the simulation smoke remains authoritative. Otherwise a
    passing run reads as INCONCLUSIVE only when you finally run it.

Cycle counts use the same literal-prefix model. By default Booley recognizes
`[SIM_CYCLES] smoke 12345`; `cycle_sentinels` lets an existing HDL testbench
keep its own prefix instead. The prefix must be followed by a
test name and a decimal integer as the line's final field, such as
`CoreMark completed in: coremark 12345`. Booley selects the record matching the
test it invoked. Counts show up as `cycles=N` on the verdict card (up to 32
tests, space permitting), in `report.json` under `cycle_counts`, and in each
Target's `simulation.json` as `tests[].cycles`; see
[FLOW_REPORTS.md](../internals/FLOW_REPORTS.md#report-schemas-and-references)
for the field layout.
Native Coverage runs report null counts. Configuring `cycle_sentinels` replaces the
built-in cycle prefix; it does not affect the pass/fail verdict. A count-only record is readable when it is the only
cycle record in the log. That count-only form and all named records remain observational
unless Goal Mode names a `CYCLE_COUNT:{target}` Goal. Gated evidence on the
non-Campaign path requires exactly one named record; the Simulation Campaign path
accepts any valid count, including a count-only record. Missing or invalid
records yield a null count.

Cocotb Targets are the exception: they score from cocotb's `results.xml`, so
pass/fail sentinel knobs don't apply. Named cycle records are still collected
from their batched output and attributed to the matching tests; see
[Cocotb Targets](TARGETS.md#cocotb-targets-python-testbenches).

### Frozen-clock watchdog (`[flows.sim].sim_time_grace_s`)

A cocotb generation whose compiled run loop doesn't match the simulator's
builds, loads and logs perfectly — and then no timed callback ever fires. The
symptom is nasty because everything *looks* alive: the heartbeat ticks, wall
clock advances, and **simulation** time sits at `0.00 ns` until the full
`timeout_ms` budget burns down. Booley watches the simulator's own clock in the
cocotb log and aborts early with the diagnosis instead:

```toml
[flows.sim]
sim_time_grace_s = 180   # default; 0 disables the watchdog
```

The watchdog only arms once the run has printed a cocotb log line (proof the
simulator started), and only fires while the highest simulation time seen is
*exactly* zero — a sim that advanced even one timestep is never touched again,
so a slow-but-live simulation can't be killed by it. Raise the value if your
testbench legitimately spends minutes in Python setup before the first clock
edge. The usual real fix is re-pinning the cocotb ↔ simulator pair; see
[Baking the cocotb stack into the image](SANDBOX.md#baking-the-cocotb-stack-into-the-image).

### Sim working directory (`[flows.sim].run_cwd`)

Some testbenches `$readmemh` vectors or firmware via paths relative to the
process working directory (`data/…`, `../fw/…`). Booley runs the sim from the
Project Root by default, so those reads miss. Point the run at the directory
those relative paths are authored against — usually the testbench dir:

```toml
[flows.sim]
run_cwd = "tests/work"   # relative to the Project Root; unset = run from Project Root
```

**A literal directory must already exist.** Booley does not create a literal
`run_cwd`; a missing one fails before the simulator starts. Runs that share a
literal `run_cwd` run one at a time.

For HDL-testbench Targets, `run_cwd` may also use the `{campaign}`, `{target}`,
`{test}` and `{attempt}` placeholders. Booley then creates a private directory
for each attempt and removes it, along with the empty parents it created, after
the run.

The resolved path is exported to Pre-Sim Commands as `BOOLEY_RUN_CWD`. They may
create files inside it but must not replace the directory.

### Pre-Sim Commands (`[flows.sim].pre_run_commands`)

Some tests need a non-RTL build step before the sim can run. The classic case
is a CPU core whose testbench loads a firmware image cross-compiled *per test
case*. Declare that step as **Pre-Sim Commands**:

```toml
[flows.sim]
pre_run_commands = ["make -C tests build_case CASE=$BOOLEY_TEST_NAME"]
```

Each entry is one non-empty shell line; Doctor and the Simulation Flow
refuse an empty entry.

The lines run **inside the Sandbox** immediately before each simulator
process: once per Test Run for an HDL-testbench Target, once before each batch
for a Cocotb Target.

Their working directory is the **Project Root** — *not* `run_cwd`, and not the
build tree. Write paths relative to the Project Root, or `cd "$BOOLEY_RUN_CWD"`
first. Nothing in the chain creates directories for you: neither `run_cwd` nor
any output dir your script writes into is auto-created, so a staging script
that targets a fresh directory must `mkdir -p` it itself.

Booley exports the run's identity and its authoritative directories, so
artifact staging never has to guess the sim's working directory:

| Variable | When set | Value |
|---|---|---|
| `BOOLEY_TARGET` | always | the Target being run |
| `BOOLEY_TEST_NAME` | single-test runs only | the selected Test, as its bare name |
| `BOOLEY_TEST_NAMES` | always | the bare names of the run's Tests, space-joined |
| `BOOLEY_TEST_RUN_NAMES` | firings that prepare a Test Run | the names of the [Test Runs](TESTS.md#seeds-seed-default_seed) this firing prepares (`test[+variant][@seed]`), space-joined |
| `BOOLEY_TEST_RUN_NAME` | one Test Run only | that Test Run's name |
| `BOOLEY_TEST_VARIANT` | one Test Run whose Test declares Test Variants | the Test Variant that runs |
| `BOOLEY_TEST_SEED` | one Test Run on a seeded Target | the seed, in decimal |
| `BOOLEY_RUN_CWD` | always | the directory the Simulation Flow runs in ([`run_cwd`](#sim-working-directory-flowssimrun_cwd) when set; otherwise the Project Root) |
| `BOOLEY_BUILD_ROOT` | `pre_sim_build_access = "legacy-per-test"` only | the fresh private per-test Edalize build tree; deliberately absent in immutable mode |
| `BOOLEY_PROJECT_ROOT` / `BOOLEY_DIR` | always | same meaning as in the [post-setup hook](CONFIG.md#worktree-setup-hooks) |
| `BOOLEY_SIM_EDA_TOOL` | after Target resolution | concrete EDA tool driven by this Simulation Flow run |

A hook that stages one input per Test keys it on `BOOLEY_TEST_NAME`. A hook
that stages a different input per Test Variant, or that needs the seed, reads
the `BOOLEY_TEST_RUN_*` variables:

```toml
[flows.sim]
pre_run_commands = [
  "make -C tests build_case CASE=$BOOLEY_TEST_NAME FLAVOUR=${BOOLEY_TEST_VARIANT:-plain}",
]
```

Inside the Sandbox, `BOOLEY_DIR` is **`/booley-project`**, not
`/work/.booley`. Use the variable rather than hardcoding a path.

Failure semantics: a nonzero exit records that test as a **failed** run with an
attributed tail (`pre-sim commands failed (rc=N): …`) and the loop continues
with the next test, never a Flow crash. The commands have an independent
600-second budget; they do not consume `timeout_ms`, `--timeout`, or
`build_timeout_ms`. `--dry-run` previews them in their real position, and
`booley doctor` validates the shape and notes when they're configured.

Every firing is recorded in the run report — one line per invocation naming the
Target/test, the number of command lines, the exit status and the duration
(`pre_run_commands (2 line(s)) for div_test: rc=0 in 4.7s`) — so a hook doing
the wrong thing quietly is visible without breaking it on purpose.

`[flows.sim].pre_sim_build_access` selects the Pre-Sim Commands contract:
`"immutable"` (default) runs hooks after authenticating the bundle and exposes
no build path, while `"legacy-per-test"` preserves pre-compile hooks against a
fresh private generation. The selected value is part of Simulation Campaign workload
identity and cannot change on resume.

In the default `immutable` mode the shared Simulator Bundle is authenticated
before the hook runs, but the hook receives no authoritative build path and
cannot mutate the image used for execution. Choose `legacy-per-test` only for a hook that truly must
write into the compile surface: it disables shared-build reuse for that work
item and records a private build in the Campaign. Changing either build-access
mode, hook commands, runtime inputs, or `run_cwd` changes workload identity and
prevents a prior manifest from resuming.

Pre-Sim Commands run only for `sim`. For a *once-per-worktree* setup step (not per-run), use the [post-setup
hook](CONFIG.md#worktree-setup-hooks) (defined under [Advanced setups](CONFIG.md#advanced-setups))
instead.

### How Booley asks for a waveform (`[flows.sim].trace_args`)

By default `booley flow sim --trace` passes `+trace` and `+tracefile=<file>`.
For Icarus, the trace overlay supplies Booley's packaged `booley_vcd_dump.sv`
when the Target does not already provide one, then roots that uninstantiated
module so it dumps a VCD with no testbench edits. Existing project-supplied
copies remain supported and are rooted instead of adding a duplicate. Verilator
does not need this module: its trace overlay enables or preserves build-time
tracing, and the Target's trace-capable C++ simulation harness owns the waveform
lifecycle.

A project that owns its C++ `main()` defines its own trace CLI instead (Ibex's
`VerilatorSimCtrl` enables capture only for `-t` / `--trace[=FILE]`). A mismatch
false-passes silently: the binary ignores the plusarg it doesn't know and emits
a header-only FST. Declare the contract:

```toml
[flows.sim]
trace_args = ["--trace={file}"]
```

`{file}` interpolates the destination selected from the Target's trace recipe.
For a Verilator Target with a project-owned C++ simulation harness and an
authored `--trace-fst` setup it is a regular `trace.fst` file; Booley leaves the
Target's FST build options intact and B-Wave reads that file directly. For VCD tracing it
is the live FIFO that B-Wave converts to FST. Args without `{file}` are passed
as-is (`["-t"]`). This **replaces** the default pair, so include every argument
the binary needs. Booley rejects header-only or otherwise unqueryable waveforms.

A Cocotb Target differs on two points: it runs cocotb's harness instead of a
project-owned one, so `trace_args` has no effect, and it rejects `--trace-fst`.
See [Tracing a Cocotb Target](TARGETS.md#tracing-a-cocotb-target).

### Where the testbench drops its dump (`[flows.sim].trace_files`)

`trace_args` covers "how do I turn tracing on". The other half is "where does
the file land". Booley looks for the artifacts *it* asked for — `trace.fst` /
`trace.fifo` / `trace.vcd` in the build dir, plus the bwave cache. A testbench
that owns its C++ `main()` typically ignores all that and writes a hardcoded
name into whatever its working directory is, so a perfectly good waveform is
reported as inconclusive with `trace requested but no fresh .fst store or
convertible .vcd was produced`. Declare it:

```toml
[flows.sim]
trace_files = ["fpu.vcd"]        # .fst is also accepted; globs are allowed
```

Each entry is resolved against `run_cwd`, then the trace/work dir, then the
build dir (absolute paths are used as given), and is consulted **only when
Booley's own artifacts are absent** — it is a fallback, not an override. Only a
file created or changed by the current run is accepted. A matched `.fst` is read
directly; a matched `.vcd` under the finalize-time size cap is converted to FST.
An oversized VCD is retained for incident diagnostics, but never reported as
`TRACE_OK`; convert it separately with `bwave build` or reduce the dump scope.

### Per-run disk budget (`[flows.sim].max_rundir_bytes`)

A testbench with a default-on tracer, `$dumpfile` or `$fwrite` sink can fill the
disk. A watchdog
polls the run directory and kills the run when it crosses the budget:

```toml
[flows.sim]
max_rundir_bytes = 5368709120   # 5 GiB, the default; 0 disables the guard
```

The budget counts **growth since the simulator started**, so inputs already
staged in `run_cwd` don't count. The kill message names the largest files
written during the run.

### Elaboration Check (`[flows.sim]`)

This setting tunes the `--mode elab-only-standalone` sweep described in
[Elaboration checks](#elaboration-checks).

```toml
[flows.sim]
# standalone_frontend = "auto"   # auto | iverilog | verilator
```

**`standalone_frontend`** picks which frontend proves the
`elaborate_standalone` criterion. `auto` (default) uses Verilator when
installed — normally the same frontend the Target build drives, so the probe can
never reject SystemVerilog the design demonstrably compiles — and falls back to
`iverilog -g2012`. Pin `iverilog` or `verilator` to choose by hand.

A probe that cannot parse a construct the per-Target build accepted is
reported as a frontend capability gap (exit 2, no verdict) rather than a design
FAIL — but **only when the probe frontend differs** from the one that elaborated
the Target. With the same EDA tool on both sides, a parse error means the standalone
build is genuinely missing a `+define+`/include the Target supplies, which is the
defect this criterion exists to catch. Modules that did grade still report their
verdict (a gap on one module never erases a real failure on another; those are
listed as "ungraded").
