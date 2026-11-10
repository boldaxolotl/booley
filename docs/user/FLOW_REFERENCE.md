# Running Flows

A **Booley Flow** runs an EDA tool on one of your Targets the same way every
time and turns the output into a verdict: pass, fail, or "could not tell". No
LLM is involved, so you can trust a Flow's verdict as you would a script's.
Booley ships four:

- [Simulation (`sim`)](FLOW-SIM.md): build and run a Target's tests and report
  whether each one passed. A Target's Tests, Test Variants and seeds are
  declared in [`tests.toml`](TESTS.md). A companion guide covers coverage:
    - [Coverage](COVERAGE.md): guide to measuring how much of the RTL your
      tests exercise, and finding what they miss.
- [Lint (`lint`)](FLOW-LINT.md): check RTL for structural, semantic, and style
  problems.
- [ASIC synthesis (`synth`)](FLOW-SYNTH.md): estimate ASIC area and timing
  quickly while you iterate on RTL.
- [FPGA implementation (`fpga`)](FLOW-FPGA.md): implement the design for an
  FPGA and report resources and timing.

This page covers what they share: choosing Targets, common options, following
a run, and reading the result. **Specialists**, the LLM-powered helpers that
review code or test your testbench, have their own page:
[Specialists](SPECIALISTS.md).

For everyday work [Usage](USAGE.md) is enough: the Developer Agent picks the
Flow, Target, and options for you. Read on when you want to understand a
result, or run a Flow yourself to check your setup or reproduce a problem.

## Running a Flow

Ask the Developer Agent. Name the Target, and for simulation the test, or ask
for the whole suite:

> Lint `lint_soc`.

> Run the `reset` test on `sim_soc`.

> Synthesize `synth_soc` and compare the area with `main`.

The agent replies with the verdict and the paths to the report and logs.

To run a Flow yourself, use a container terminal (VS Code's terminal after
**Reopen in Container**, or `booley session enter`):

```bash
booley flow                                   # list the Flows
booley flow lint  --target lint_soc
booley flow sim   --target sim_soc --test reset
booley flow synth --target synth_soc
booley flow fpga  --target fpga_soc
booley flow <name> --help                     # every option of one Flow
```

Your own runs never count as Goal evidence; only the agent's runs in Goal Mode
do.

### Background Jobs

When a Flow or Specialist started through an MCP tool outlives the inline wait,
it keeps running as a background Job and the call returns a `run_id`. With
several Jobs running, call `booley_poll` with no `run_id`: it returns the
result of whichever Job you started finishes first and says which others are
still running, or that there is nothing to wait for. Pass a `run_id` to wait for
one specific Job. Jobs started by another agent session or by a nested
Specialist are not included. Every Booley MCP tool response ends with one line
for each finished Job you started but have not polled yet (up to ten, then a
count of the rest; input prompts carry none), and the Dashboard's
Job details show how long each result waited before it was read.

### Resource use

Booley measures every Job's peak memory and time. `booley resources` shows the
recent range for each Target, which tells you how many heavy Jobs fit side by
side (`max_heavy`) and how much memory to reserve for each (`heavy_memory`).
See [Job resource history](CONFIG.md#job-resource-history).

### Choosing Targets

`booley targets` lists every Target; `booley targets --for <flow>` lists the
ones a Flow can run. [TARGETS.md](TARGETS.md) explains what a Target is.

If you leave out `--target` and the Project has exactly one eligible Target,
the Flow runs it and the choice is announced. With zero or multiple eligible
Targets, the run
exits `2` and lists the eligible ones (`(none)` when there are none). A
simulation resume (`sim --resume-from`) always uses the Target of the run it
resumes.

To run several Targets in one go, repeat the flag, use commas, or both:
`--target a --target b,c` runs `a`, `b`, then `c`. Naming the same Target
twice is an error.

If two `.core` files declare the same Target name, use the longer name
`booley targets` prints, such as `lowrisc:ibex:ibex_top#lint`.

### Common options

Every built-in Flow accepts these. Options specific to one Flow are on its own
page.

| Option | Effect |
|---|---|
| `--target <name,...>` | Targets to run (see [Choosing Targets](#choosing-targets)). |
| `-C/--project PATH` | Run in another Project or worktree; see [Selecting a Project](CLI-REFERENCE.md#selecting-a-project). |
| `--timeout DURATION` | Time limit per unit of work, for example `90s`, `30m`, or `1h30m`. Time spent waiting in the queue doesn't count. |
| `--dry-run` | Print what would run and stop. No EDA tool runs. |
| `--report-dir <path>` | Write reports here instead of the default location. |
| `--diagnostic` | Mark the run as diagnostic so it never counts as Goal evidence. |
| `-q/--quiet` | Hide live progress and log paths; show only the verdict and errors. |

Without `--timeout`, a Flow uses `[flows.<name>].timeout_ms` from
`booley.toml`, or its built-in default. Simulation builds have their own limit,
`[flows.sim].build_timeout_ms`; see
[Simulation timeouts](FLOW-SIM.md#timeouts).

`synth` and `fpga` also take `--baseline <git-ref>`: they run the Flow a
second time at that commit and report the difference, so you can see what a
change cost in area or timing. In Goal Mode the baseline is the commit pinned
when the Goal started.

<details markdown>
<summary>In stealth mode</summary>

Your Booley Directory is a Git repository of its own, so the baseline run also
uses the Target definitions from where your worktree branched off, and both
runs build the same design. The progress output names the commit it chose. See
[STEALTH.md](STEALTH.md).

</details>

### Live progress and logs

When you run a Flow yourself with `booley flow`, Booley keeps a small live
panel at the bottom of your terminal and redraws it in place. Its first line
names the Flow, Target, and current step, then the elapsed time and how long ago
the EDA tool last printed anything:

```text
synth · synth_soc · repair_timing   4m 10s   last output 12s ago
```

Below it are the tool's latest six output lines. Elapsed time is not a
percentage, and a quiet tool isn't necessarily stuck: EDA tools often buffer
their output. `-q/--quiet` hides the panel. When the agent runs a Flow, you see
its verdict instead, not this panel.

When the output isn't a terminal, for example when redirected to a file or run
from the agent's shell, Booley prints plain timestamped `[booley-progress]`
lines instead: one per new step, plus one every five seconds with any new
output.

When Simulation runs several tests in parallel (`[jobs].max_heavy` above 1;
see [Jobs & concurrency](CONFIG.md#jobs--concurrency-jobs)), each running test
gets its own progress row and output lines, labelled `lane=<test>/<attempt>`.

On a terminal, Booley prints the log paths once, on one line, after the verdict.
In redirected output it prints them before the run starts and again after the
verdict:

- `tool-output.log`: the complete transcript of the EDA tool's output.
- `run.log`: a capped snapshot of the latest output.

The paths are inside the Sandbox. To follow a run from a second terminal:

```bash
booley session enter -- tail -f <printed tool-output.log path>
```

A terminal run prints the path only at the end, so while it runs, follow the
`tool-output.log` in the `.runtime/flow-console/invocation-*` directory that
holds an `active` file.

Use `tail -F` for `run.log`, because Booley replaces it as it updates.

Transcripts collect under `.runtime/flow-console/` in the Booley Directory and
are never cleaned up automatically. Delete finished ones when you need the space,
but leave any directory that contains an `active` file: that run is still
going. Quiet runs, dry runs, and `--help` create no transcript.

## Reading results

Every built-in Flow uses the same exit codes:

| Exit | Meaning | What to do |
|---:|---|---|
| `0` | The check passed. | Nothing. Advisory findings, such as lint warnings with `warnings_as_errors = false`, may still be listed. |
| `1` | The design failed the check, or a simulation was inconclusive. | Fix the RTL or testbench, then rerun. |
| `2` | The Flow couldn't reach a verdict: bad configuration, missing tool, crash, or a tool or build timeout. | Fix the setup. The result says nothing about the design. |

A simulation is **inconclusive** when the simulator exits with code `0` but
without a pass or fail verdict, for example because the testbench never
printed its pass message. A simulation that runs out of time exits `1`, but a build that runs
out of time exits `2`. [Simulation results](FLOW-SIM.md#reading-results)
has the details.

**Reports.** Each run writes a JSON report and full logs under `flow-reports/`
in the Booley Directory (Goal runs use the Goal's runtime directory), or under
`--report-dir`. Each run gets the next number under its Flow's directory, such
as `flow-reports/sim/12/`, with the run's `report.json` at the top. Each
Flow's page says which files in it to open.

**Dry runs.** `--dry-run` prints a JSON plan of each unit of work: Target,
sources, constraints, parameters, and the exact commands. It exits `0` when
everything can be planned and `2` otherwise, still showing the parts that
could be planned.

When a Flow fails for a reason you can't explain, see
[Troubleshooting](TROUBLESHOOTING.md). If Booley itself seems wrong, report it
as described in [When Booley itself misbehaves](USAGE.md#when-booley-itself-misbehaves).

### Cleaning up reports

Booley keeps reports until they are deleted. Where they go decides who
deletes them:

| Run started by | Reports | Cleaned up |
|---|---|---|
| A Goal | The Goal Record, in the main checkout's Booley Directory | Automatically, once the Goal has ended and its worktree and Goal Branch are gone. |
| The agent in Interactive Mode | `.interactive_logs/<session>/` in the main checkout's Booley Directory, from any checkout | Not automatically; see below. |
| You, from the CLI | `flow-reports/` in the Booley Directory of the checkout you run in, or `--report-dir` | With the worktree, for a run in a linked worktree. |

In the main checkout nothing removes them, and coverage runs in particular
grow to gigabytes. Delete the run folders under `flow-reports/` and the
sessions under `.interactive_logs/` you don't need, when no run is active. Keep a run you may
still [resume](FLOW-SIM.md#resuming-an-interrupted-run), and the run a
resumed run continued from.
