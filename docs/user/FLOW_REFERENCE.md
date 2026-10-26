# Running Flows

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
| `--timeout DURATION` | Active-time budget per work unit (queue time is not counted). Overrides `[flows.<name>].timeout_ms`. Simulation builds instead use `[flows.sim].build_timeout_ms`, and Pre-Sim Commands use an independent fixed budget; see [Simulation timeouts](FLOW-SIM.md#simulation-build-pre-sim-and-run-timeouts). |

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
