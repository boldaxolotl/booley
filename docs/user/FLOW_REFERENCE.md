# Running Flows

> **Do you need this page?** For everyday work, [Usage](USAGE.md) is enough:
> the Developer Agent runs Flows for you. Read on if you want to understand
> Flows in depth, or run them yourself.

Booley ships four built-in Flows. Each has its own page:

- [Simulation (`sim`)](FLOW-SIM.md): build and run a Target's tests and report
  whether each one passed.
    - [Simulation tests](TESTS.md): declare a sim Target's tests, Test Variants,
      and seeds in `tests.toml`.
    - [Simulation coverage](COVERAGE.md): measure how much of the RTL your
      tests exercise, and find what they miss.
- [Lint (`lint`)](FLOW-LINT.md): check RTL for structural, semantic, and style
  problems.
- [ASIC synthesis (`synth`)](FLOW-SYNTH.md): estimate ASIC area and timing
  quickly while you iterate on RTL.
- [FPGA implementation (`fpga`)](FLOW-FPGA.md): implement the design for an
  FPGA and report resources and timing.

This page covers what they share: running a Flow or a Specialist, following
its progress, choosing Targets, and reading the result.

Related references:

- [TARGETS.md](TARGETS.md): Targets and the `.core` files Flows build from.
- [CONFIG.md](CONFIG.md): Project-wide `booley.toml` settings.
- [SUPPORTED-EDA-TOOLS.md](SUPPORTED-EDA-TOOLS.md): which EDA tools and versions
  each Flow supports.
- [CLI-REFERENCE.md](CLI-REFERENCE.md#booley-flows--specialists): every Flow and
  Specialist, and the Goals each can satisfy.
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

### Live progress and logs

Direct CLI runs show progress on **stderr**: a status line (Flow, Target,
current step, elapsed time, age of the last output) plus the latest six output
lines. Elapsed time is not a percentage or a health signal, and EDA tools may
buffer their output.

Redirected/non-TTY stderr, `TERM=dumb`, and short terminals use flushed newline
records without terminal escapes or carriage-return rewrites. For example:

```text
[booley-progress] 2026-10-05T13:00:00Z synth Target=synth_soc stage=candidate: synthesis elapsed=0s transcript=/work/.booley/.runtime/flow-console/invocation-abc/tool-output.log
[booley-progress] 2026-10-05T13:00:05Z synth Target=synth_soc stage=yosys elapsed=5s heartbeat (elapsed time; process health unknown)
[booley-progress] 2026-10-05T13:00:05Z synth Target=synth_soc stage=yosys elapsed=5s output: Executing synthesis passes.
```

Each record has a UTC timestamp. Records announce each new step and add a
heartbeat every five seconds with any new output lines. Current and baseline
work are labeled separately.

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
For sim, `--verbose` (resume mismatch detail) and `--result-verbosity` (result
detail) control result output, not live output.

Log paths are printed before the run starts and repeated in a footer after the
verdict. `tool-output.log` is the full append-only transcript; `run.log` is a
capped snapshot.

Paths are **inside the Sandbox**. Follow the exact printed transcript path from
a second terminal attached to that existing Sandbox:

```bash
booley session enter -- tail -f /work/.booley/.runtime/flow-console/invocation-abc/tool-output.log
```

`run.log` is replaced as it updates; follow it with `tail -F`.

Each invocation gets its own directory under the Project runtime's
`flow-console/`. There is no automatic pruning: delete completed invocation
directories you don't need, but leave any directory containing an
`active` marker alone.
Help, validation-only, dry-run and quiet calls create no transcript.

Shell calls, including the Developer Agent's, print this progress to stderr
even without a terminal. Pass `--quiet` for clean captured output.

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
| `--timeout DURATION` | Active-time budget per work unit (queue time is not counted). Overrides `[flows.<name>].timeout_ms`. Simulation builds instead use `[flows.sim].build_timeout_ms`, and Pre-Sim Commands use an independent fixed budget; see [Simulation timeouts](FLOW-SIM.md#simulation-build-pre-sim-and-run-timeouts). |

`--timeout-ms` (milliseconds) and `--work-dir` (a literal path, without checkout
discovery) are hidden aliases of `--timeout` and `-C/--project`; each prints a
notice on stderr. Don't combine an alias with its canonical option.

`--baseline <git-ref>` (synth, fpga) reruns the Flow at that commit for
comparison. When the Project has a Paired Checkout, the baseline also needs
the Booley Directory as it was then. Outside Goal Mode, Booley uses the
merge-base of the Paired Checkout branch's recorded creation base with its
HEAD, or its upstream if no base was recorded; it refuses when it has neither
or the recorded base is invalid. A detached Paired Checkout uses its
checked-out commit. Goal Mode uses the commit pinned at Goal entry. Progress
output names the commit it chose.

## Running a Specialist

Inside the Sandbox:

```bash
booley specialist
booley specialist reviewer --help
booley specialist reviewer --category rtl --focus bugs --scope rtl --timeout 30m
booley specialist coverage_analyst --campaign reports/sim/12/targets/sim_soc/coverage.json
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
MCP parameters and `booley.toml` use milliseconds.

Project selection works before or after the endpoint name for built-ins and
visible Specialists: `booley flow -C ../other lint --target lint`, or
`booley flow lint --target lint -C ../other`. A leading `--` separates the
main CLI from the endpoint options; a subsequent `--` starts an opaque payload.

## Reading results

Every built-in Flow uses the same exit codes:

| Exit | Meaning | What to do |
|---:|---|---|
| `0` | The Flow ran and the check passed. | Nothing. Advisory findings (such as lint warnings with `warnings_as_errors = false`) may still be reported. |
| `1` | The Flow reached a design verdict and it failed. | Fix the RTL or testbench. |
| `1` | Simulation was `inconclusive` (for example, no pass/fail sentinel after a clean run). The Criterion is skipped. | Repair the testbench verdict or missing evidence, then rerun. |
| `2` | The Flow could not reach a verdict: bad configuration, missing tool, crash, tool or build timeout. | Fix the setup; the result says nothing about the design. |

A simulation run timeout exits `1` and fails `SIM:{target}`; build, Elaboration
Check and Pre-Sim Command timeouts exit `2`. See
[Verdicts and Criteria](FLOW-SIM.md#verdicts-and-criteria).

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
scratch directory when resolution needs them; the plan says so. Field
details: [FLOW_REPORTS](../internals/FLOW_REPORTS.md#dry-run-plan).
