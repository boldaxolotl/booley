# Running Flows

A **Booley Flow** runs an EDA tool on one of your Targets the same way every
time and turns the output into a verdict: pass, fail, or "could not tell". No
LLM is involved, so you can trust a Flow's verdict as you would a script's.
Booley ships four:

- [Simulation (`sim`)](FLOW-SIM.md): build and run a Target's tests and report
  whether each one passed.
    - [Tests](TESTS.md): declare a sim Target's tests, Test Variants, and seeds.
    - [Coverage](COVERAGE.md): measure how much of the RTL your tests exercise,
      and find what they miss.
- [Lint (`lint`)](FLOW-LINT.md): check RTL for structural, semantic, and style
  problems.
- [ASIC synthesis (`synth`)](FLOW-SYNTH.md): estimate ASIC area and timing
  quickly while you iterate on RTL.
- [FPGA implementation (`fpga`)](FLOW-FPGA.md): implement the design for an
  FPGA and report resources and timing.

This page covers what they share: choosing Targets, common options, following
a run, and reading the result. It also covers **Specialists**, the LLM-powered
helpers that review code or test your testbench.

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
when the Goal started. If your Booley Directory is a Git repository of its own
(a Versioned Booley Directory, of which each worktree gets a Paired Checkout), the baseline run also uses the Target definitions from
where your worktree branched off, so both runs build the same design. The
progress output names the commit it chose.

### Live progress and logs

While a Flow runs in a terminal, a status line shows the Flow, Target, current
step, elapsed time, and how long ago the tool last printed anything, followed
by its latest six output lines. Elapsed time is not a percentage, and a quiet
tool isn't necessarily stuck: EDA tools often buffer their output.

When the output isn't a terminal, for example when redirected to a file or run
from the agent's shell, Booley prints plain timestamped `[booley-progress]`
lines instead: one per new step, plus one every five seconds with any new
output.

When Simulation runs several tests in parallel (`[jobs].max_heavy` above 1;
see [Jobs & concurrency](CONFIG.md#jobs--concurrency-jobs)), each running test
gets its own progress row and output lines, labelled `lane=<test>/<attempt>`.

Booley prints the log paths before the run starts and again after the verdict:

- `tool-output.log`: the complete transcript of the EDA tool's output.
- `run.log`: a capped snapshot of the latest output.

The paths are inside the Sandbox. To follow a run from a second terminal:

```bash
booley session enter -- tail -f <printed tool-output.log path>
```

Use `tail -F` for `run.log`, because Booley replaces it as it updates.

Transcripts collect under `.runtime/flow-console/` in the Booley Directory and
are never cleaned up automatically. Delete finished ones when you need the space,
but leave any directory that contains an `active` file: that run is still
going. Quiet runs, dry runs, and `--help` create no transcript.

## Running a Specialist

A **Specialist** is an LLM-powered helper the Developer Agent calls for one
focused task, with a fresh context each time:

- **Reviewer** reviews RTL or testbench code for one concern at a time, such as
  bugs, protocol, or spec compliance.
- **Mutation Tester** plants small bugs in the RTL and checks that your tests
  catch them.
- **Coverage Analyst** explains a coverage report and suggests what to test
  next.

A Specialist that judges one side of the RTL/testbench split can't see the
other side. A reviewer looking at the testbench never reads the RTL, so it
judges the tests against the spec rather than against what the RTL happens to
do.

Ask the agent, for example *"review the RTL for bugs"*. To run one yourself, in
a container terminal:

```bash
booley specialist                             # list the Specialists
booley specialist reviewer --help             # its options
booley specialist reviewer --category rtl --focus bugs --scope rtl
```

Specialists accept `-C/--project`, `--report-dir`, `--diagnostic`, and, where
it applies, `--target`. Three options exist only on the command line:

- `--model light|standard|heavy` picks the model tier. A tier below the
  Specialist's minimum is raised to that minimum.
- `--max-turns N` caps the conversation length.
- `--timeout DURATION` limits each model call, not the whole run.

Reports go to `mcp-tool-reports/` in the Booley Directory.
[CLI-REFERENCE.md](CLI-REFERENCE.md#booley-flows--specialists) lists each
Specialist's arguments and the Goals it can satisfy.

## Reading results

Every built-in Flow uses the same exit codes:

| Exit | Meaning | What to do |
|---:|---|---|
| `0` | The check passed. | Nothing. Advisory findings, such as lint warnings with `warnings_as_errors = false`, may still be listed. |
| `1` | The design failed the check, or a simulation was inconclusive. | Fix the RTL or testbench, then rerun. |
| `2` | The Flow couldn't reach a verdict: bad configuration, missing tool, crash, or a tool or build timeout. | Fix the setup. The result says nothing about the design. |

A simulation is **inconclusive** when it ends cleanly but without a pass or
fail verdict, for example because the testbench never printed its pass
message. A simulation that runs out of time exits `1`, but a build that runs
out of time exits `2`. [Simulation results](FLOW-SIM.md#reading-results)
has the details.

**Reports.** Each run writes a JSON report and full logs under `flow-reports/`
in the Booley Directory (Goal runs use the Goal's runtime directory), or under
`--report-dir`. The verdict and the report
both give the exact paths. [FLOW_REPORTS.md](../internals/FLOW_REPORTS.md)
describes the report files, for scripts that read them.

**Dry runs.** `--dry-run` prints a JSON plan of each unit of work: Target,
sources, constraints, parameters, and the exact commands. It exits `0` when
everything can be planned and `2` otherwise, still showing the parts that
could be planned. [Dry-run plan](../internals/FLOW_REPORTS.md#dry-run-plan)
lists the fields.

When a Flow fails for a reason you can't explain, see
[Troubleshooting](TROUBLESHOOTING.md). If Booley itself seems wrong, report it
as described in [When Booley itself misbehaves](USAGE.md#when-booley-itself-misbehaves).
