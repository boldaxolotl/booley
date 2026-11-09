# Simulation (`sim`)

The simulation Flow, `sim`, simulates your design with one of the supported
simulator EDA tools: Verilator or Icarus (see
[Supported EDA tools](SUPPORTED-EDA-TOOLS.md)). Each simulation Target picks its
simulator and kind of testbench (HDL or cocotb) and declares the Tests to run;
`sim` reports which ones passed. Use it to check that a change still passes its
tests, to reproduce a failure with a waveform, or, with an
[elaboration check](#elaboration-checks), to see quickly whether the design
still compiles. It does not catch style problems or estimate area and timing:
use [Lint](FLOW-LINT.md) and [ASIC synthesis](FLOW-SYNTH.md) for those.

What all Flows share (choosing Targets, common options, live progress, exit
codes) is in [Running Flows](FLOW_REFERENCE.md). How to declare a Target's
Tests, Test Variants and seeds is in [`tests.toml`](TESTS.md); measuring how much RTL
the tests exercise is in [Coverage](COVERAGE.md); checking that the tests
catch planted bugs is the Mutation Tester's job in
[Specialists](SPECIALISTS.md).

## Running a simulation

1. Check that the Project has a simulation Target (`booley targets --for sim`
   lists them) and that its Tests are declared. Project Setup normally does
   both; see [Adding a Test](#adding-a-test) to add one yourself.
2. Ask the Developer Agent to run the tests, naming the Target and, if you
   want, the tests:

    > *"Run simulation: all tests on the `sim_soc` Target."*
    >
    > *"Run the `irq` and `reset` tests on the `sim_soc` Target."*
    >
    > *"Run the `stress` test with 20 random seeds on the `sim_soc` Target."*

3. The agent reports each Test Run as passed or failed. When a test fails, it
   reads that Test Run's log, and it can rerun that one Test Run with the same
   seed and a waveform to find the cause:

    > *"Rerun the failing seed with a waveform and find out why it fails."*

4. To look at the waveform yourself, see
   [Viewing waveforms](USAGE.md#viewing-waveforms).

To reproduce a run yourself inside the Sandbox:

```bash
booley flow sim --target sim_soc                        # all tests for this Target
booley flow sim --target sim_soc --test reset --test irq
booley flow sim --target sim_soc --test stress@x20      # 20 random seeds
booley flow sim --target sim_soc --test irq --trace     # with a waveform
booley flow sim --target sim_soc --mode elab-only       # compile only
```

| Option | Effect |
|---|---|
| `--test <test run>` | Run only these Test Runs (see below). Repeat for more. |
| `--tests-file <path>` | Read the Test Runs from a file, one per line; blank lines and `#` comments are ignored. Not with `--test`. |
| `--seed <N\|random>` | Run every selected Test Run with seed `N`, or with a separate random seed each. |
| `--trace` | Capture a [waveform](#waveforms). Slows the simulation; use it for debugging, not for pass/fail runs. |
| `--coverage` / `--cov` | Collect [coverage](COVERAGE.md) (Verilator only). |
| `--no-waivers` | With `--coverage`, report coverage without applying approved waivers. See [Coverage](COVERAGE.md). |
| `--mode <simulate\|elab-only\|elab-only-standalone>` | Run the tests (default), or only [compile](#elaboration-checks). |
| `--resume-from <manifest.json>` | [Resume](#resuming-an-interrupted-run) an interrupted run. |
| `--verbose` | With `--resume-from`, show every detail of why the manifest doesn't match the current Project. |
| `--result-verbosity <compact\|full>` | For a Cocotb Target, `full` prints every test function's result. |
| `--no-kill` | Don't kill leftover simulator processes before the run. For diagnosis only. |

### Adding a Test

1. Ask the agent to add the test, for example *"Add a test for FIFO overflow
   to the `sim_soc` Target."* The agent writes the testbench code and registers
   the Test name in [`tests.toml`](TESTS.md).
2. The agent runs `booley doctor`, which checks `tests.toml` and names the
   entry and the rule for any mistake.
3. Ask the agent to run it: *"Run `fifo_overflow` on `sim_soc`."* A run of the
   whole Target includes it from now on.

### Test Runs, Test Variants and seeds

`--test` selects [Test Runs](TESTS.md#seeds-seed-default_seed): one
[Test Variant](TESTS.md#test-variants-variants) at one seed, named
`test+variant@seed`. It accepts:

| You write | Booley runs |
|---|---|
| `stress` | The Test's default Variant at the Target's Default Seed (`1` unless [`default_seed`](TESTS.md#seeds-seed-default_seed) says otherwise). |
| `stress+soak` | The Variant `soak` at the Default Seed. |
| `stress@42`, `stress+soak@42` | That Variant with seed 42 (1 to 2147483647). |
| `stress@x20`, `stress+soak@x20` | 20 Test Runs of that Variant, each with a different random seed. N is 1 to 1000. |

`--seed 42` gives every selected Test Run seed 42; `--seed random` draws a
separate seed for each. Booley picks every seed before the run starts, so a
report always names the seed that ran.

Without `--test`, Booley runs the default Variant of every Test at the Default
Seed, minus the Tests in [`skip`](TESTS.md#skipping-known-bad-tests-skip).
Naming Tests runs exactly those, skipped or not.

Booley refuses a request before anything builds when a named Test or Variant
doesn't exist, two selectors mean the same Test Run, it asks for more than 1000
Test Runs of one Target, or it asks for a seed on an
[unseeded](TESTS.md#seeds-seed-default_seed) Target. `--seed` can't be combined
with a `--test` that already names a seed, with an elaboration check, or with
`--resume-from`.

Tests run in the order you name them; with `--coverage`, in order of Test name.
They run one at a time unless you raise `max_heavy` in
[`[jobs]`](CONFIG.md#jobs--concurrency-jobs).

## Reading results

The run ends with a block per Target: its verdict (`PASS`, `FAIL`, `ERROR` or
`INCONCLUSIVE`, see below), the paths to its Campaign manifest and the report,
and for a failure the end of the simulator output. The Test Runs are listed
when there is more to say than "everything passed at the Default Seed" (a
failure, another seed or Variant, or a Test that ran more than once). Each
failed Test Run comes with a command that reruns exactly it:

```text
sim_soc: FAIL (Simulation Campaign 3f2a9c1e-…)
  manifest: /work/.booley/flow-reports/sim/12/targets/sim_soc/campaign/manifest.json
  report: /work/.booley/flow-reports/sim.json
  <end of the failed test's simulator output>
  Test Runs: 3 (2 passed, 1 failed)
    PASS         stress+soak@1804289383
    FAIL         stress+soak@846930886
    PASS         stress+soak@1681692777
  Rerun a failed Test Run with the same seed:
    booley flow sim --target sim_soc --test=stress+soak@846930886
```

The two paths:

- **`manifest`** is the run's plan, written before anything runs: the Target,
  the Test Runs with their seeds, the builds and the source files. You need
  it only to [resume an interrupted run](#resuming-an-interrupted-run).
- **`report`** is a copy of the newest run's `report.json`, and the next run
  overwrites it. The run's own copy is `sim/12/report.json`.

Each Test Run gets one verdict:

| Verdict | Meaning | Exit |
|---|---|---:|
| Pass / fail | An HDL testbench passes or fails by the [message it prints](#passfail-sentinels-and-cycle-counts). A Cocotb Target uses cocotb's result file. Either way, [error and assertion messages](#passfail-sentinels-and-cycle-counts) in the output fail a test even when it printed its pass message, and so does a nonzero simulator exit code. A test also fails when it reads a `$readmemh` file that is missing and not declared in the Target (reported as `missing_input`; [declare it with `copyto`](TARGETS.md#design-description-core)), or when a [Pre-Sim Command](#pre-sim-commands-flowssimpre_run_commands) preparing it fails or times out. | `0` / `1` |
| Inconclusive | The simulator exited with code `0` without printing a pass or fail message, or a `--trace` run produced no usable waveform. It never counts as a pass. | `1` |
| Timeout | The simulation ran past its time limit (`timeout_ms`, ten minutes by default). It counts as a failure: look for a deadlock in the RTL or testbench, or raise the limit if the test really needs longer. | `1` |
| Error | An infrastructure problem stopped the test, such as a build timeout, a build failure with no recognised design error, a missing tool, or Pre-Sim Commands that can't start; the result says nothing about the design. An RTL compile error or a simulator crash is a failure instead. | `2` |

The card doesn't print log or waveform paths. Each run has its own numbered
directory (`sim/12/` above; see [Running Flows](FLOW_REFERENCE.md#reading-results)).
These are the files to open, relative to
`sim/<N>/targets/<target>/`:

| File | What it holds |
|---|---|
| `simulation.json` | The Target's results: each Test Run with its seed, verdict and cycle count. |
| `campaign/work-items/<NNNN>-<id>/attempts/<NNNN>-<id>/evidence/*-run.log` | The full simulator output of one Test Run, or of one batch for a Cocotb Target. |
| `…/evidence/*-trace.fst` | The [waveform](#waveforms) of a `--trace` run, next to its log. |

Work items are numbered in the order the run planned them, and each one's
`result.json` names its Test Run. To find the log of the failed run above:

```bash
grep -l '"test_run":"stress+soak@846930886"' \
  /work/.booley/flow-reports/sim/12/targets/sim_soc/campaign/work-items/*/result.json
```

A work item normally has one attempt. One that was interrupted and then
[resumed](#resuming-an-interrupted-run) gets another, in the original run's
directory.

**Goals.** In [Goal Mode](GOAL-MODE.md), a `sim` run that the agent makes in
the Goal's worktree records evidence for `SIM`, `ELAB`, `CYCLE_COUNT` and
`COVERAGE` Goals. Runs you start from the CLI never do. Which Test Runs a
Goal needs, and how seeds are frozen, is in
[Simulation, Cycle Count, and coverage Goals](GOAL-MODE.md#simulation-cycle-count-and-coverage-goals).

## Pass/fail sentinels and cycle counts

A simulation that exits with code `0` hasn't necessarily passed: a testbench
can call `$finish` early or never check anything. So for an HDL testbench
Booley looks for a pass or fail message, a **sentinel**, in the simulator
output. By default these are `[SIM_RESULT] PASSED` and `[SIM_RESULT] FAILED`:

```systemverilog
$display("[SIM_RESULT] PASSED");
```

If your testbench already prints its own messages, declare them instead of
editing a testbench you don't own:

```toml
[flows.sim]
pass_sentinels = ["ALL TESTS PASSED."]
fail_sentinels = ["ERROR!", "TIMEOUT"]
```

Your lists replace the defaults. When both a pass and a fail sentinel appear,
the test fails. `booley doctor` warns when a sim Target's testbench doesn't
contain the default pass sentinel; a custom one it can't find is only a note,
because the testbench may build the message at run time. A Cocotb Target takes
its verdicts from cocotb, so sentinels don't apply to it.

**Error and assertion messages.** A pass sentinel isn't enough on its own.
A test also fails, on any simulator and testbench kind, when its output
contains `$error`, `$fatal`, `] Error:` or `Fatal:`, a line with `Assertion`
and `FAILED` or `ERROR`, or a line starting with `ERROR: `. This catches SVA
assertions that fire while the testbench still reports success. If your
testbench prints such words as ordinary text, reword those messages.

**Cycle counts.** Cycle count is the performance number for RTL work: a
faster divider or a better branch predictor shows up as fewer cycles for the
same test. The simulator can't tell Booley that number, because only the
testbench knows which cycles count (say, from the benchmark's start to its
end, not the reset sequence). So the testbench reports it by printing
`[SIM_CYCLES] <test> <count>`, for example `[SIM_CYCLES] coremark 12345`.
Without it, Booley has no cycle count for the test, and a `CYCLE_COUNT` Goal
on it stays unmet.
`cycle_sentinels` replaces that prefix with one your testbench already prints,
such as `["CoreMark completed in:"]` for `CoreMark completed in: coremark 12345`.
Counts appear as `cycles=N` on the verdict card and in the report, also for
Cocotb Targets. They are informational unless a `CYCLE_COUNT` Goal sets
[thresholds](GOAL-MODE.md#thresholds) on them, such as "at most 12,000
cycles" or "cycle count drops at least 8%" against the code the Goal started
from.

## Waveforms

Ask the agent to rerun a failing test with a waveform, or pass `--trace`.
Then ask the agent what happened in that run, for example why a test failed
or when a signal changed, and it answers by reading the waveform with B-Wave.
It can also open the part you care about in VS Code
([Viewing waveforms](USAGE.md#viewing-waveforms)). A traced run
that produces no fresh, readable waveform is inconclusive rather than passing
silently. The waveform is saved next to the Test Run's log; see
[Reading results](#reading-results).

Booley stores every waveform as FST, a compact format that B-Wave and common
viewers (GTKWave, VaporView) read. The simulator can write either format:

- **FST**: Booley uses the file as it is.
- **VCD**: Booley converts it to FST for you. With Verilator the conversion
  runs while the test runs, so the full VCD never lands on disk. Icarus and
  Cocotb Targets write the VCD first and Booley converts it after the run
  (Verilator does the same on Windows).

How tracing is switched on depends on the Target:

- **Icarus**: Booley adds its own waveform dump module, so the testbench needs
  no changes.
- **Verilator**: Booley builds with tracing and passes `+trace` and
  `+tracefile=<file>` to the simulation, which the Target's C++ `main()` must
  handle. A Target built with `--trace-fst` writes FST directly; otherwise
  the dump is VCD.
- **Cocotb Target**: Booley drives the dump itself; see
  [Tracing a Cocotb Target](#tracing-a-cocotb-target).

If your Verilator `main()` takes other options, declare them in `trace_args`.
`{file}` becomes the waveform path, and the list replaces the default pair.
Without it, a binary that doesn't know `+trace` ignores it and writes an empty
waveform:

```toml
[flows.sim]
trace_args = ["--trace={file}"]
```

If an HDL testbench writes its dump to a fixed file name of its own, tell
Booley where to find it:

```toml
[flows.sim]
trace_files = ["fpu.vcd"]   # .fst also works; globs are allowed
```

Booley looks for each entry in the [run directory](#sim-working-directory-flowssimrun_cwd),
then the trace directory, then the build directory, and only when its own
waveform file is missing. The file must be written by the current run. An FST
file is used as it is. A VCD file of any size is converted after the run,
within the `trace_convert_timeout_ms` budget. If the conversion fails or runs
out of time, the run reports a trace incident and keeps the VCD for diagnosis.
A failing simulation stays failed and a passing one is reported inconclusive
(no waveform to query); neither becomes a timeout. Dump FST instead, or raise
`trace_convert_timeout_ms`, when conversion is too slow.

On Linux and macOS with native B-Wave, a literal relative `.vcd` entry inside
the run directory skips the wait: during the run that path is a named pipe,
and B-Wave converts what the testbench writes while the test runs. The VCD
never lands on disk, the pipe is removed afterwards, and the waveform is
ready when the run ends. Globs, absolute or outside-the-run-directory
entries, Windows, a host without native B-Wave, and a file already at that
path (Booley does not turn it into a pipe) fall back to the post-run conversion above,
including keeping the VCD when that conversion fails. If the testbench
deletes or replaces the file, the file left there is converted after the run.
A dump that is never written, is reopened (a second VCD header), or that
B-Wave rejects gives a trace incident naming B-Wave's error, with no VCD kept;
the verdict follows the same rule as a failed conversion above. A testbench must not seek in, read, or
reopen a streamed dump. If Booley is killed hard mid-run a pipe can remain; the
next `--trace` run removes it, or delete it yourself, because a run without
`--trace` would block writing to it.

### Tracing a Cocotb Target

`booley flow sim --target <target> --trace` works on a Cocotb Target with no
trace setup in the `.core`. The simulator writes a VCD, and Booley converts it
to FST after the run. If the conversion fails, the VCD is kept as `trace.vcd`
in the Target's simulation work directory, next to the error output.
[`[flows.sim].trace_args`](#waveforms)
has no effect here: Booley drives the dump itself.

On a Verilator Cocotb Target, `verilator_options` may contain `--trace` or
`--trace-vcd`, or no trace flag at all. `--trace-fst` makes a traced run fail
with:

```text
Cocotb Target '<target>' requests native FST tracing, but the Cocotb run-half currently owns a VCD dump; use a VCD trace recipe for this Target
```

Replace `--trace-fst` with `--trace`, and remove any `-DVM_TRACE_FMT_FST`
define along with it.

## Elaboration checks

`--mode elab-only` compiles, elaborates and links the simulator without running
any tests: a fast "does it still build?" check. A later full run can reuse the
build. A successful build meets an `ELAB` Goal, and so does the build step of a
normal run, even if a test then fails.

`--mode elab-only-standalone` also elaborates each reusable RTL module on its
own. This catches a module that compiles only thanks to a define or include
that the Target happens to supply. `standalone_frontend` chooses the compiler
for that sweep: `auto` (default) uses Verilator when it is installed, otherwise
Icarus. When that compiler differs from the Target's and can't parse something
the Target build accepted, the module is reported as ungraded rather than as a
design failure; if no other module fails, the run exits `2`.

Both modes skip Pre-Sim Commands, and refuse options that only make sense when
tests run, such as `--test`, `--trace` and `--no-kill`. A compile error in the
RTL exits `1`; a tool, setup or timeout problem exits `2`.

## Resuming an interrupted run

For a long run, such as a multi-hour regression, resuming saves re-running the
tests that already finished. For a short run, just start it again.

Every run records its plan and results in a Simulation Campaign, one per
Target. The CLI prints each Campaign's `manifest.json` path before it starts,
and the verdict card repeats it. Ask the agent to resume the run, or:

```bash
booley flow sim --resume-from <manifest.json> --dry-run   # show what is left
booley flow sim --resume-from <manifest.json>
```

- The manifest fixes the Target, the Test Runs with their seeds, the mode,
  coverage and tracing, so you can't give those options again. A resumed
  `--seed random` or `@xN` run keeps the seeds it drew at the start. Timeout
  and output options may change.
- One resume continues one Target. For a run of several Targets, resume each
  unfinished Target's manifest.
- What runs again depends on the testbench:

    | Testbench | Unit of work | On resume |
    |---|---|---|
    | HDL | one Test Run | Only Test Runs without a result run again. |
    | cocotb | the whole batch | Finished batches are skipped; an interrupted batch reruns all its tests. |

    A Cocotb batch is every test that shares a seed and Test Variant, run in
    one simulator process. Resuming a Cocotb Target doesn't start it from
    scratch: only the interrupted batch runs again, and the batches that
    finished are kept. A run with several seeds (`@x20`) or Test Variants has
    several batches.

- A `--coverage` run resumes only if it was interrupted while publishing its
  results. Otherwise Booley refuses (exit `2`). Start a new `booley flow sim --coverage` run instead:
  the refusal prints the command, with the same Test Runs and seeds.
- A test with a recorded result is finished, even if it failed. Resume never
  re-runs failures; start a new run for that.
- Resume refuses when the Target's sources, Tests or simulation settings have
  changed since the original run. Don't edit Campaign files by hand: resume
  checks that they are intact.

## Pre-Sim Commands (`[flows.sim].pre_run_commands`)

Some tests need a non-RTL build step before the simulation starts, typically a
CPU core whose testbench loads firmware compiled for each test. Declare that
step as **Pre-Sim Commands**: shell lines that Booley runs inside the Sandbox
immediately before each simulator process. That is once per Test Run for an
HDL testbench, and once per batch for a Cocotb Target.

```toml
[flows.sim]
pre_run_commands = ["make -C tests build_case CASE=$BOOLEY_TEST_NAME"]
```

The commands run in the Project Root, not in `run_cwd`: write paths relative to
the Project Root, or `cd "$BOOLEY_RUN_CWD"` first. Nothing creates output
directories for you, so `mkdir -p` any you write into. Booley exports:

| Variable | Value |
|---|---|
| `BOOLEY_TARGET` | The Target being run. |
| `BOOLEY_TEST_NAME` | The Test, when exactly one Test is being prepared. |
| `BOOLEY_TEST_NAMES` | The Tests being prepared, space-separated. |
| `BOOLEY_TEST_RUN_NAMES` | The Test Runs being prepared (`stress+soak@42`), space-separated. |
| `BOOLEY_TEST_RUN_NAME`, `BOOLEY_TEST_VARIANT`, `BOOLEY_TEST_SEED` | The Test Run, its Variant and seed, when exactly one Test Run is prepared and it has them. |
| `BOOLEY_RUN_CWD` | The directory the simulation runs in: `run_cwd`, or the Project Root. |
| `BOOLEY_PROJECT_ROOT`, `BOOLEY_DIR` | The Project Root of the checkout being simulated and that checkout's own Booley Directory (`/work/.booley` for the main checkout inside the Sandbox). |
| `BOOLEY_SIM_EDA_TOOL` | The simulator, such as `verilator`. |

For example, to build different firmware per Test Variant:

```toml
[flows.sim]
pre_run_commands = [
  "make -C tests build_case CASE=$BOOLEY_TEST_NAME FLAVOUR=${BOOLEY_TEST_VARIANT:-plain}",
]
```

The commands get their own fixed ten-minute limit, separate from the
simulation's. If they fail or time out, the tests they were preparing fail
with the command's output, and the run goes on with the next test. Each firing
is listed in the report with its exit status and duration, `--dry-run` shows
where the commands will run, and `booley doctor` checks their format.

By default the commands can't change the simulator build: they run after it is
built and see only run-time inputs, so the build can be reused across tests.
If a command must write into the build tree, set
`pre_sim_build_access = "legacy-per-test"`. Each test then gets its own build,
exposed as `BOOLEY_BUILD_ROOT`, which is slower.

For a setup step that runs once per worktree rather than before every test, use
the [worktree setup hook](CONFIG.md#worktree-setup-hooks) instead.

## Sim working directory (`[flows.sim].run_cwd`)

Booley runs the simulation from the Project Root. A testbench that loads
vectors or firmware with relative paths (`data/…`, `../fw/…`) then can't find
them. Point `run_cwd` at the directory those paths are relative to, usually the
testbench directory:

```toml
[flows.sim]
run_cwd = "tests/work"   # relative to the Project Root
```

`run_cwd` itself must not contain a `..` component: Booley refuses it before
running. Paths the testbench opens from there, such as `../fw/…`, are fine.

The directory must already exist in every checkout: Booley doesn't create it.
Commit a placeholder file such as `.gitkeep` so it does. Tests that share it run
one at a time. If the directory only receives per-run output, add the
`{attempt}` placeholder instead (`run_cwd = "runs/{attempt}"`): Booley then
creates a private directory for each attempt and removes it afterwards, and
tests can run in parallel. `{target}`, `{test}` and `{campaign}` are also
available.

## Build reuse

A repeat run skips the build when nothing that goes into it has changed:
sources, cores, toolchain, flags and environment. Cocotb Targets always
rebuild. Each Target's result says why a build was or wasn't reused.

## Verilator builds

Verilator turns the design into C++ that has to be compiled, which is often
the slowest part of a run. Booley speeds that up in two ways.

**Parallel compilation.** Booley compiles the model with several jobs at once.
It splits the Sandbox's CPUs between the `max_heavy` builds that may run at
the same time, and allows about one compile job per GiB of `heavy_memory`
(both in [`[jobs]`](CONFIG.md#jobs--concurrency-jobs)). A `-j` in the
Target's `make_options` overrides this.

**Compiler cache.** The C++ compilation uses ccache, so a rebuild after a
small change is quicker. The cache is kept in the Project's Booley Directory,
so it survives Sandbox rebuilds and is shared by every checkout of the Project
in the Sandbox, including Goal worktrees. To inspect or empty it, run inside
the Sandbox:

```sh
CCACHE_DIR=/booley-project/.runtime/compiler-cache/ccache ccache --show-stats
CCACHE_DIR=/booley-project/.runtime/compiler-cache/ccache ccache --clear
```

If ccache isn't available, the build prints a warning and compiles without it.

## Configuration (`[flows.sim]`)

Simulation settings live in `.booley/booley.toml`. All are optional; the values
shown are the defaults. Coverage settings are on the
[Coverage](COVERAGE.md) page, and the Tests themselves are in
[`tests.toml`](TESTS.md).

```toml
[flows.sim]
# enabled = true                     # see CONFIG.md
# timeout_ms = 600000                # time limit per simulation (10 minutes)
# build_timeout_ms = 3600000         # time limit per simulator build (1 hour)
# trace_convert_timeout_ms = 1800000 # time limit for converting a run's VCD to FST (30 minutes)
# pass_sentinels = ["[SIM_RESULT] PASSED"]    # see Pass/fail sentinels
# fail_sentinels = ["[SIM_RESULT] FAILED"]
# cycle_sentinels = ["[SIM_CYCLES]"]
# run_cwd = "."                      # see Sim working directory
# pre_run_commands = []              # see Pre-Sim Commands
# pre_sim_build_access = "immutable" # or a private build per test
# trace_args = ["+trace", "+tracefile={file}"]   # Verilator; see Waveforms
# trace_files = []                   # see Waveforms
# max_rundir_bytes = 5368709120      # stop a run that writes more than 5 GiB; 0 = off
# sim_time_grace_s = 180             # cocotb: stop a run whose clock never starts; 0 = off
# standalone_frontend = "auto"       # auto | verilator | iverilog; see Elaboration checks

[flows.sim.compiler_cache]
# enabled = true
# max_size = "5G"                    # M, G, Mi or Gi; trimmed after each build, not a hard limit
```

### Timeouts

`timeout_ms` (or `--timeout` for one run) limits each simulation, and in
`elab-only-standalone` mode the module sweep. A simulation that runs out of
time is a test failure (exit `1`); a sweep that runs out of time exits `2`. `build_timeout_ms` limits each simulator build, for normal runs,
coverage and elaboration checks alike; it defaults to `3600000` (one hour), and
running out of time is an error (exit `2`). `timeout_ms` limits only the
simulation: under `--trace`, converting the VCD to FST has its own
`trace_convert_timeout_ms` budget (default `1800000`, 30 minutes) for the whole
attempt, and running out of it is a trace incident, not a timeout. Pre-Sim Commands have an
independent fixed 600-second budget that neither setting changes.

**Two safety limits.** `max_rundir_bytes` stops a run whose directory grows
by more than the limit, for example a testbench that dumps every signal to
disk; the message names the largest files. `sim_time_grace_s` stops a cocotb
run whose simulation time is still zero that many seconds after cocotb starts,
which usually means the cocotb version doesn't match the simulator; see
[Baking the cocotb stack into the image](SANDBOX.md#baking-the-cocotb-stack-into-the-image).
Raise it if your testbench spends minutes in Python setup before the first
clock edge.
