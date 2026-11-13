# Built-in Booley Flow implementation

How Booley's built-in Booley Flows (`sim`, `lint`, `synth`, `fpga`, `mutation`) turn
a FuseSoC Target into an EDA command, and the EDA output back into evidence,
and why they are built that way. It is for contributors and agents changing
Flow code. Simulation has its own page,
[FLOW_IMPLEMENTATION_SIM.md](FLOW_IMPLEMENTATION_SIM.md), because it is the
largest Flow; this page covers what the Flows share plus `lint`, `synth`,
`fpga`, and `mutation`. To run a Flow, read [FLOW_REFERENCE.md](../user/FLOW_REFERENCE.md) and
its per-Flow pages instead.

| Document | Covers |
|---|---|
| [FLOW_REFERENCE.md](../user/FLOW_REFERENCE.md), [FLOW-SIM.md](../user/FLOW-SIM.md), [FLOW-LINT.md](../user/FLOW-LINT.md), [FLOW-SYNTH.md](../user/FLOW-SYNTH.md), [FLOW-FPGA.md](../user/FLOW-FPGA.md) | Running each Flow, its options, `[flows.*]` keys, and verdicts |
| [FLOW_REPORTS.md](FLOW_REPORTS.md) | Report locations, JSON fields, and Simulation Campaign file layout |
| [MCP-TOOLS.md](MCP-TOOLS.md) | The MCP surface, Custom Flows, and Specialists |
| [SPECIALIST_IMPLEMENTATION.md](SPECIALIST_IMPLEMENTATION.md) | Reviewer and Coverage Analyst internals |
| [JOB_RECORDS.md](JOB_RECORDS.md) | How a Flow run is tracked as a Job |
| [TARGETS.md](../user/TARGETS.md), [TESTS.md](../user/TESTS.md), [CONFIG.md](../user/CONFIG.md) | The `.core` Targets, `tests.toml`, and `booley.toml` |
| [SUPPORTED-EDA-TOOLS.md](../user/SUPPORTED-EDA-TOOLS.md) | Which EDA tools each Flow supports and how they are provisioned |

## From a Target to a command: FuseSoC and Edalize

Booley does not adapt each project's Makefiles and Tcl scripts. The design is
described once in a FuseSoC `.core` file, and every Flow run is generated from
it, so the agent reaches every EDA tool through one interface instead of
guessing at per-project conventions. That is why porting a project to Booley
means porting its build description ([SETUP.md](../user/SETUP.md)).

A `.core` file (CAPI2, FuseSoC's YAML schema) declares Targets. A
[Target](../GLOSSARY.md#configuration) fixes everything needed to build the
design: filesets (with `file_type` and `tags: [tb]` testbench markers), typed
parameters, the toplevel, and `flow_options.tool`, the EDA tool FuseSoC resolves
for (Verilator, Icarus, Verible, Yosys, or Vivado). `--target` names one or more
Targets; when it is omitted, the one eligible Target is selected, but only for
built-in Flows: a Custom Flow requires an explicit Target by default. A Target's
identity is its declaring core's VLNV (the `.core` file's
Vendor:Library:Name:Version identifier) plus its Target name; the Flow receives
the shortest selector that is unambiguous in the Project, the bare name when
unique, otherwise a VLNV suffix and `#name`. Goals and evidence compare
identity, while commands render selectors. The CLI (`booley flow sim --target
sim_core`) and the MCP call run the same Flow code.

`booley.fusesoc.fusesoc_registry` resolves a Target in two steps. A cheap,
side-effect-free YAML read lists Target names, to validate `--target` and
expand per-Target Criteria. Then `fusesoc run --setup`, run as a subprocess
inside the Sandbox, resolves filesets, parameters, and the `depends` graph
into an EDAM (EDA Metadata) file that Booley reads rather than builds. The
module's rule is "list names from data you trust; resolve through the FuseSoC
program": Booley never reimplements FuseSoC's resolution.

**Command generation is delegated; result interpretation is not.** Edalize,
FuseSoC's backend library, emits the EDA tool's Makefile or Tcl script, never
the verdict. Booley runs that command and does all interpretation itself: the
Simulation Flow reads its verdict sentinels, the Lint Flow parses and
deduplicates findings, the FPGA Flow parses Vivado reports. That split is what
makes the evidence contracts below possible. How each Flow gets its command:

| Flow | Command source |
|---|---|
| `sim`, `lint` | The build directory `fusesoc run --setup` leaves after Edalize's `configure()`. |
| `synth` | A Yosys/OpenROAD Makefile Booley generates, because Edalize has no Yosys ASIC-synthesis flow (only the FPGA-oriented `icestorm` and `trellis`). |
| `fpga` | A Vivado EDAM Booley rebuilds from the resolved Target (`flows/fpga/backends/vivado/edam.py`), so it can apply PPA profiles and its own checks; Edalize's `vivado` flow emits the project. |

Only the FPGA EDAM passes through `flows/edam.py`, which allows only listed
`flow_options` keys and requires file paths under the workspace. Sim and lint
`*_options` reach the EDA tool as FuseSoC resolved them. The security boundary
is the Sandbox, not `.core` validation
([ADR 0079](../adr/0079-trust-the-project-inside-the-sandbox.md)); every Flow
command runs inside it.

### Who owns which input

| File | Owner | Holds |
|------|-------|-------|
| `<name>.core` | FuseSoC | Filesets, typed parameters, toplevel, Targets, and the EDA tool |
| `.booley/tests.toml` | Booley | Per-Target Tests and how the testbench selects one (for example a `+test_id=` plusarg, a `+name=value` simulator argument) |
| `.booley/booley.toml` | Booley | Project metadata, per-Flow execution policy, and EDA provisioning requests |

Ownership decides which layer may interpret an input: anything that changes
what is built belongs on the Target, so one global setting can never apply
silently to the wrong Target. Doctor fails Target recipe keys (`part`,
`ppa_profile`, `synth_mode`, `sdc`, and others) placed under `[flows.*]`
(`booley/audit/flow_schema.py`).

Most EDA binaries ship in the Sandbox Image. A supported commercial tool can
instead come from an administrator-registered host installation mounted
read-only. `booley.toml` only requests host provisioning; the host's Project
Grant selects the installation, and Project configuration cannot choose a host
path, command, execution location, or license server
([SANDBOX.md](../user/SANDBOX.md#commercial-eda-provisioning)).

## Evidence contracts

A Booley Flow is an evidence contract between the caller and the EDA tools,
not a wrapper around project commands. Running the simulator is easy; turning
its logs, exit codes, and generated files into facts Booley can reason about
is the job. Every built-in Flow provides:

- **Verdict normalization.** A small, explicit outcome vocabulary instead of
  raw logs, where each outcome points to a different next action (debug a
  failure, fix elaboration, investigate a hang, repair a testbench that never
  reported a verdict).
- **Verdicts never trust a stale artifact.** When a verdict comes from a file
  the run writes (a report, JUnit XML, a log line), anything present before the
  run must not count, and the exit code outranks the file. Otherwise a build
  that fails to compile leaves the previous run's report in place and reads as
  a pass. A nonzero exit with no fresh evidence is never a pass.
- **Predictable artifacts.** Logs and reports land in known places, so
  waveform tools and report readers never search EDA-tool-specific build
  directories.
- **Exact identity.** `target` and `test` name entries in the `.core` and
  `tests.toml`, never command-line fragments, and the result records exactly
  what ran.
- **One contract past the boundary.** Verilator or Icarus, image- or
  host-provisioned, with or without DPI or per-test firmware builds: callers
  see the same result shape.

All Flows share one exit-code convention (`booley/core/exit_codes.py`): 0 for
a clean pass, 1 for findings or a design failure, 2 when the Flow itself could
not reach a verdict (infrastructure, configuration, or publication failure),
and 130 when cancelled. Each Flow maps its own grades onto these codes; the
per-Flow sections say how.

## Code structure

Built-in Flows take typed requests and return
`booley.runtime.endpoint_execution.ExecutionResult`, whose `outcome` is an
`EndpointOutcome`. They do not subclass `McpTool` or generate MCP schemas; the
CLI and MCP layers adapt to them.

```python
from pathlib import Path

from booley.flows.lint.flow import LintFlow
from booley.flows.lint.request import LintRequest

result = LintFlow().execute(LintRequest(target="lint_soc", work_dir=Path.cwd(), diagnostic=True))
print(result.exit_code, result.outcome.detail)
```

This call runs inside the Sandbox and does the same validation, admission,
and persistence as the CLI. Each Flow's request type (`SimRequest`,
`LintRequest`, `SynthRequest`, `FpgaRequest`) lives in its package beside its
implementation. `slug` (the Goal Record ID) and `state_file` are not
constructor arguments: they come from the environment or the Goal binding, so
a caller cannot point a run at another Goal's state. A request is copied for
execution because preparation and baseline work may change it.

Which execution adapter runs decides whether Goal evidence is recorded:

- `StandaloneFlowExecution` (direct calls and `booley flow`) records no Goal
  evidence and skips final persistence.
- `GoalFlowExecution` (`booley/goals/flow_execution.py`), which MCP composition
  supplies with the call's Run Binding, records Goal evidence. A direct caller
  must compose it explicitly to get the same path. It imports no Flow or MCP
  code, and MCP builds it lazily, so loading MCP never loads Goal Mode.

| Class or module | Owns |
|---|---|
| `FlowMechanics` (`flows/base.py`) | Subprocess execution, timeout and cancellation cleanup, artifact freshness checks, the Flow pre-state gate |
| `BuiltinFlow` (`flows/base.py`) | Per-invocation state for the built-ins. Every entry point (construction, `execute`, `execute_cli`, `parse_args`) starts a fresh invocation, so no state leaks between calls. |
| `EndpointState` (`flows/endpoint_state.py`) | Per-invocation state, delegating to the acceptance, admission, reporting, and invocation modules |
| `builtin_cli` | The CLI parser, built from common definitions plus each Flow's argument adapter, producing the same typed request |
| `booley.mcp.flow_adapter` | MCP schema extraction and MCP-only adjustments (canonical timeout and mode fields, hidden CLI aliases); the MCP server keeps wire validation, dispatch, budgeting, and result conversion |
| `booley.runtime.endpoint_execution` | The one execution coordinator |

Flow- and Criteria-aware services live beside their Flow policy, not in
`booley.runtime`, because Runtime must not import Flows. Custom Flows and
Specialists use a different base: `BooleyFlow` combines `FlowMechanics` with
the `EndpointContext` facade (eager `_add_args`, CLI parsing, and the hooks
Project-local Flows rely on), and Specialists subclass `McpTool`. MCP loading
accepts both and honors Project-local schema overrides; see
[MCP-TOOLS.md](MCP-TOOLS.md). Built-ins never inherit `EndpointContext`.

Import rule D15 in [SOURCE-DEPENDENCY-CONTRACT.md](SOURCE-DEPENDENCY-CONTRACT.md)
forbids every `booley.flows -> booley.mcp` import, including deferred and
type-only ones. `tests/flows/test_transport_contract.py` covers direct typed
and CLI calls, acceptance failures, a real lint child through MCP with a fake
EDA executable, transport schemas, and Project-local `BooleyFlow` and
`McpTool` loading.

## Invocation lifecycle

### Ordering

Preparation validates the runtime, Flow enablement, the bound Goal worktree,
and the Target-to-Goal binding before Job admission. An omitted `--target` is
resolved before the binding check; an explicit one is checked after it. The
Run Binding captures the Goal Record and specification revisions plus the
Protected Inputs; publication checks it again before appending evidence or
merging state, and a binding that has changed discards the evidence
rather than assigning it to another Goal Record. The admission claim is held
through the run, final acceptance, state persistence, and reporting, then
released.

Acceptance is recorded at two points, and both must stay:

1. Each `set_criterion` call computes the change, records immutable evidence,
   then saves mutable state and emits its update, so a long run shows progress
   and a crash keeps what was already proven.
2. At completion the coordinator records final acceptance or invalidation
   before saving the timeline and final report.

Do not defer all Criteria writes to completion.

### Failures

A failure while recording final acceptance becomes exit 2 with
`detail.completion_error`. It keeps the Flow's verdict and known Target facts,
skips final state persistence, and still tries to publish `report.json`.
Per-Target acceptance already committed before a later failure stays visible
in `detail.acceptance`, and user-facing output has no traceback.

A failure appending acceptance inside the run (`_run`) is different: that
update does not save, the invocation adapter turns the exception into an error
outcome, and a recording adapter then persists the error as usual. Cancellation
propagates, and subprocess trees, stdout redirection, and admission claims are
cleaned up.

### Shared planning and dry-run lifecycle

`lint`, `synth`, `fpga`, and the Simulation Elaboration Check project their
preparation into `FlowPlan` / `WorkUnitPlan` records (`flows/plan.py`). A work
unit keeps everything that identifies one timed execution together: Target and
revision role, selected tests or modules, EDA tool, sources, constraints,
parameters, recipe, ordered argv, and expected artifacts. All requested units
are validated before the first real command runs, so a bad later Target fails
the call before any EDA time is spent. `synth` and `fpga` execute their
planned recipes for candidate and baseline units; Lint's plan shows a command
template and the real run resolves through `fusesoc run --setup`. Ordinary and
coverage Simulation runs print a Simulation Campaign preview instead. `mutation`
also inherits `BuiltinFlow` but keeps its own dry-run summary instead of a
`FlowPlan`.

`semantic_plan_fingerprint` hashes what the work means and leaves out dry-run
status, cwd and scratch prefixes, report destinations, and invocation IDs,
which differ per invocation. Tests compare dry and real preparation with it,
separately from the recipe and cache fingerprints that decide implementation
reuse.

A dry run validates the boundary and Targets but skips Job admission, EDA
execution, acceptance, `_post_run()` persistence, normal reports, and cache
filling; its display events carry `dry_run=true`. It writes only
`<report-root>/<flow>/flow_plan.json` ([FLOW_REPORTS.md](FLOW_REPORTS.md#dry-run-plan)).
`synth` and `fpga` may need FuseSoC setup or generators to produce an EDAM;
they confine that to an invocation-owned temporary directory in the checkout
(`.booley-synth-plan-*`, `.booley-fpga-plan-*`), remove it on exit, and
disclose it in the plan. EDA tools and Pre-Sim Commands never run.

### Progress lifecycle

`progress.json` (written by `sim`, `synth`, and `fpga`) is an observation of
one run, never liveness or resume authority; its fields are in
[FLOW_REPORTS.md](FLOW_REPORTS.md#progress). `flows/progress_lifecycle.py`
enforces:

- Every write carries the dispatched `run_id` and a UTC `timestamp`, and
  `completed_targets` plus `pending_targets` is an exact, disjoint split of
  `targets`. A Target becomes completed only after its durable publication.
- Every catchable exit publishes a terminal document. `phase: complete` means
  the planned work was exhausted, whatever the design verdict; `aborted` means
  orchestration stopped early or terminal publication failed.
- One failed terminal write is retried: a failed `complete` is retried as
  `aborted`, and a failed `aborted` is retried without hiding the original
  failure. A successful retry does not erase the exit-2 publication error.
- Writers and repairs share one file lock (`.progress.lock`), so a supervisor
  repair cannot overwrite a concurrent coverage-resume supersession.
- When an MCP supervisor terminates or finds a dead child, it repairs a
  matching, still-live checkpoint only after reaping the process group, on a
  best-effort basis. A missing, unsafe, malformed, changed, or unwritable
  checkpoint never changes the Job outcome already decided.

### Run logs

Every built-in Flow truncates its `run.log` when a run starts and writes a
header line, `[BOOLEY RUN_LOG] run=… flow=… target=… started=…`
(`flows/run_log.py`). While the run is going the body is a
`(run in progress …)` status line, followed in Simulation by a live tail of up
to 200 KB; the full output (tail-truncated to 10 MB) replaces it at the end.
Tailing a log during a wait therefore never shows the previous run's verdict
as if it were live, and a Flow can use the header to check that a `run.log` its
report points to really belongs to this run. The generic artifact shape is in
[MCP-TOOLS.md](MCP-TOOLS.md#common-artifact-contract).

## Goal baselines and editable design inputs

Ordinary CLI calls are diagnostic; Goal verification goes through the
MCP-bound execution adapter, which binds evidence to the Goal Record. Goal
entry pins the clean linked worktree's HEAD as the base and creates a Goal
Branch. A baseline-relative Goal runs its baseline Target at that base. The
candidate Target is an editable design input: renaming or removing a bound
Target leaves the Goal unmet until it is restored or a human approves a
retargeting Goal Change Proposal. Target and constraint (`.sdc`, `.xdc`)
changes appear in the Review Package rather than being forbidden, because the
agent may legitimately need to edit them
([ADR 0067](../adr/0067-replace-ticket-mode-with-goal-mode.md)).

[Protected Inputs](../GLOSSARY.md#execution) are files that decide how
evidence is produced, so changing them mid-Goal would let the agent move the
goalposts. They are: Project configuration and checkout-root `booley.toml`,
the `pipeline.toml` beside every `booley.toml` candidate (the configuration
reader still falls back to it), worktree-root `FUSESOC_IGNORE`, and `hooks`,
`.managed`, `generators`, and `mcp_tools` at their consumer roots, plus files
the Project lists in `[project].protected_inputs`
([CONFIG.md](../user/CONFIG.md#additional-protected-inputs-projectprotected_inputs)).
`booley/goals/protected_inputs.py` digests every candidate location a reader
might use (the Goal worktree, the Main Booley Directory, the primary
checkout's `.booley/`, and absolute configured paths), not only the one
currently selected, because emulating each reader's choice would let a
selection mismatch hide a change. Goal entry captures working-tree and
committed identities; for declared files it also stores a per-file capture
(path, contents, type, executable bit) so a violation can name the file.
Drift warns at once, discards run evidence at publication, refuses Goal Change
Proposals, and blocks Finish until reverted.

A Goal without a bound Target is checked against authored sources only.
Generated build artifacts, the Git-ignored files a Target consumes other than
HDL, `.core` files, and declared programs, are
left out of its fingerprints and of the committed-input check at Finish, so
regenerating one does not stale the Goal unless the Reviewer's scope names it.
HDL, `.core` files, declared programs, symlinks, paths outside the repository,
and files in a nested Git repository are never left out, so a regenerated
ignored file cannot hide authored logic. A Goal bound to a Target still needs
a Simulation producer for each uncommitted generated input.

## Shared implementation-report envelope

`synth` and `fpga` publish the same versioned `implementation` envelope
(`flows/implementation_report.py`) in their per-Target JSON, Criteria detail,
aggregate MCP detail, and live progress, so consumers read one shape for both
Flows. Schema version 1 has:

- `identity`: Flow, Target, EDA tool, invocation ID, timestamp;
- `status`: the policy-resolved grade (`pass`, `warn`, `fail`, or `error`),
  tool return code, timeout and infrastructure state, completion facts, and
  bounded diagnostics;
- `metrics` and `conditions`: common timing fields plus Flow-specific QoR
  (ASIC area and cells, or FPGA LUT/FF/BRAM/DSP);
- `recipe` and `provenance`: the normalized recipe, the producing run, and a
  separate identity for a run that consumed a cached result;
- `comparison`: the resolved baseline, mirrored baseline evidence,
  comparison-basis errors, and a current/baseline/delta record per comparable
  metric;
- `cache` and `artifacts`: cache evidence and immutable numbered report and
  log paths; mutable build directories are labeled `live_dirs`.

The Flow-specific fields stay beside the envelope. `status.passed` is true for
`pass` and `warn`; `fail` exits 1 and `error` exits 2. One resolved status
feeds the report, the Criterion, the aggregate result, and the MCP result, so
they cannot disagree about fatal timing or an invalid baseline. A missing
optional metric makes only that metric's delta unavailable.

## `lint`

`lint` runs the resolved Target's linter and turns its diagnostics into a
PASS/WARN/FAIL/ERROR verdict and a `lint_clean_{target}` Criterion. There is
one Lint Flow; the Target's `flow_options.tool` picks the linter: Verilator
`--lint-only` for structural and semantic findings, Verible
`verible-verilog-lint` for style and naming. Running both on the same RTL takes
two lint Targets (say `lint_dut` and `lint_style`) and so two Criteria:
`lint_clean_{target}` means "clean under that Target's linter", never
"Verilator-clean".

### Configuration boundary

The Target owns the linter and its rules and waivers: a `.vlt` file
(`file_type: vlt`) for Verilator, `veribleLintRules` / `veribleLintWaiver`
filesets and the `ruleset` / `rules` flow options for Verible. Rules and
waivers are design description, not Booley configuration, so Booley adds no
severity tiers or waiver mechanism of its own. Every finding counts against the
Criterion, and a waiver edit shows up in the diff like any other design change,
where the Review Package and the Reviewer can see it. `[flows.lint]` holds
execution policy only, including whether warnings change the exit code; the
keys are in [FLOW-LINT.md](../user/FLOW-LINT.md#configuration-flowslint).

### Verdict semantics

| Outcome | Exit | When |
|---------|------|------|
| PASS | 0 | The linter ran and left no in-scope findings after deduplication. |
| WARN / FAIL | 1 | Findings remain, or the linter ran and rejected the RTL. |
| ERROR | 2 | No verdict was reached: missing binary, setup or planning failure, timeout, an unmatched `--scope` entry, or a Target whose toplevel no linted file declares. |

**A nonzero linter exit is never a clean lint.** Findings are counted from
parsed `%Warning` and Verible finding lines, so a run that dies before printing
any would otherwise score zero and pass. A recorded failure always outranks
the tally. The one exception is Verilator's location-less
`Exiting due to N warning(s)` epilogue on a warnings-only run: grading it as a
hard failure would make `warnings_as_errors = false` useless, so that run is
graded by its warning count and the setting instead.

**FAIL versus ERROR depends on who failed.** A linter that ran and rejected the
design is working, so that is a design FAIL. A linter that could not run is an
ERROR naming the installation fix (normally rebuilding the Sandbox Image).
Verible is invoked with `--parse_fatal --lint_fatal=false` to keep that split:
both flags default to true, so without `--lint_fatal=false` ordinary findings
would exit 1 and look like a parse failure. With it, a parse failure exits
nonzero (ERROR) and findings exit 0 with parseable lines (WARN). One unusable
linter decides the whole run, because the other Targets' verdicts are then not
trustworthy evidence of a clean design.

**A Target that lints nothing is an ERROR.** Before running, Booley checks that
some linted HDL file declares `module <toplevel>`. A style fileset trimmed of
the top module, or a stale `toplevel`, would otherwise lint against nothing
and pass. The check is an ERROR rather than a warning, and the linter is not
run, because its verdict would be meaningless. A file that cannot be read
counts as declaring the module, so an I/O error never invents a failure.

`warnings_as_errors = false` moves the exit code only: the WARN text stays and
`lint_clean_{target}` still records the true finding count, so a CI job can
pass on warnings while the Criteria keep the honest number.

### Findings

Findings are parsed with the shared regular expressions in
`booley.flows.eda_parsers` (one place for the Verilator and Verible formats),
then deduplicated across Targets on `(rule, file, line)` and filtered by
`--scope`. Deduplication keeps the first finding's fields and order and
collects every contributing Target. The console prints the first five. The
numbered `lint/<N>/lint_report.json` exists because a Verilator pass followed
by a Verible pass is two runs of one Flow and would otherwise overwrite each
other; its fields are in [FLOW_REPORTS.md](FLOW_REPORTS.md#lint). The
per-Target `warnings` count in that report is the raw count before
deduplication and `--scope`; the Criterion and `total_warnings` use the
deduplicated, scoped count.

## `synth`

`synth` is a fast ASIC quality-of-results (QoR) estimate, not sign-off. Both
modes map the RTL through Yosys. `physical` (the default) continues through
OpenROAD floorplanning, global placement, setup repair, and STA (static timing
analysis); its numbers are still pre-layout estimates. `logical` stops after
technology mapping. The Flow turns area, timing, and structural conditions
into a `synthesis_ok_{target}` Criterion.

### Configuration boundary

The Target's `.core` owns the recipe and timing inputs: frontend, PPA profile,
flattening, synthesis mode, backend overrides, sources, SDC, and toplevel
([Synthesis Target](../user/TARGETS.md#synthesis-target)). `[flows.synth]` owns
execution and verdict policy (`timeout_ms`, `expected_latches`,
`expected_comb_loops`, `fail_on_timing_violation`, `timing_report_paths`); see
[FLOW-SYNTH.md](../user/FLOW-SYNTH.md#configuration-flowssynth). Doctor fails
recipe keys such as `ppa_profile`, `synth_mode`, or `sdc` placed under a
`[flows.*]` table (`booley/audit/flow_schema.py`), because a recipe value on
the Flow table would apply silently to every Target. `ppa_profile` is translated
to Yosys and OpenROAD settings; `advanced_settings_yosys` and
`advanced_settings_openroad` override individual knobs, and per-call overrides
are resolved before command generation
([optimization profiles](../user/FLOW-SYNTH.md#optimization-profiles)).

### Constraints (SDC)

Timing constraints are design intent, so they live on the Target as a
`file_type: SDC` fileset, like the RTL and like FPGA XDC
([ADR 0061](../adr/0061-make-target-sdc-the-only-physical-synthesis-timing-authority.md)).
A physical-mode Target without one is a hard error before any EDA tool runs,
rather than a clock Booley invents: a default clock produced PPA numbers that
measured no one's intent. OpenROAD loads the SDC files in fileset order and
adds no clocks, I/O delays, or other defaults; an SDC that defines no clock is
an input error. Logical mode runs no STA and ignores SDC.

A clock far tighter than the design can meet (4 ns on a design good for tens
of MHz) makes `repair_timing` buffer and upsize thousands of instances for
minutes before STA. Near the achievable period it converges quickly, and Fmax
falls out of the slack.

### Modes and timing evidence

- **`physical`** reports per-clock `critical_path_ps`, `fmax_mhz`, `wns_ns`,
  and `whs_ns` from OpenROAD's OpenSTA. `area_um2` is OpenROAD's
  post-optimization area (`area_source = "openroad_post_optimization"`).
  Missing OpenROAD or PDK inputs, or incomplete physical results, fail the run;
  there is no standalone OpenSTA fallback.
- **`logical`** reports `estimated_fmax_mhz` from ABC's slowest liberty-mapped
  combinational partition. That is not STA: it ignores placement, wires,
  clock-to-Q, and setup, so Booley labels it probably inaccurate and never
  uses it for timing thresholds. `area_um2` is Yosys's mapped area
  (`area_source = "yosys_mapped"`).

Both modes use the same two area fields, so consumers never pick between
parallel mapped and post-optimization fields.

A clock's `critical_path_ps` / `fmax_mhz` is its single worst-slack path. With
a nonzero I/O-delay budget an I/O path can dominate it and hide the internal
logic speed, so physical mode also reports the worst register-to-register path
(`reg2reg_fmax_mhz`, `reg2reg_slack_ns`). These are null when STA finds no
reg-to-reg path, as in a purely combinational design. Compare reg-to-reg
numbers for A/B logic changes.

The report lists artifact directories rather than every file, because a file
inventory was most of the report's size and a hard-coded file name silently
goes stale. `artifacts.live_dirs.timing` (physical mode) holds the STA reports
(`overall.rpt`, `overall.csv.rpt`, `reg2reg.rpt` when the design has registers,
and `pre_repair*.rpt` when repair ran). `artifacts.live_dirs.build` holds the
rest: the `stat` output, the netlists, the two check outputs, the per-stage
logs, the rendered `synth.ys`, and the SDC fed to STA. On a large design these
are easier to search than `run.log`, which holds the tail (up to 10 MB) of all
stages' output.

### Intentional latches and loops

An inferred latch or combinational loop is normally a bug, so any one fails the run unless it is declared. An integrated
clock-gating cell is the exception: it is built from a deliberate
`always_latch` (lowRISC's generic `prim_clock_gating` has one), and failing on
it made correct designs unsynthesizable. `[flows.synth].expected_latches`
declares how many latches are intended, and `expected_comb_loops` does the same
for combinational loops. `[flows.fpga]` has both keys too. They are allowances,
not mutes: the raw count is always reported, and one more than declared still
fails. All four share one reader (`expected_count` in
`booley/flows/flow_config.py`): a missing, negative, boolean, or non-integer
value is 0. Multi-driven nets have no allowance.

### RTL frontend

`frontend` (Target default, `--frontend` per call) chooses how RTL enters
Yosys. `sv2v` (default) transpiles SystemVerilog to Verilog and works on every
Sandbox Image. `slang` reads SystemVerilog directly and needs Yosys 0.67 or
later; a preflight (`yosys -p 'help read_slang'`) fails fast on an older image.
`slang` is required when the design puts parameterized interfaces on module
ports or reads interface parameters hierarchically, which sv2v cannot
transpile. Both feed the same mapping and timing steps. See
[FLOW-SYNTH.md](../user/FLOW-SYNTH.md#rtl-frontend-sv2v-vs-slang).

### Verdict semantics

`synthesis_ok_{target}` is satisfied only when:

- the run exits 0 within its timeout;
- the mode's results are complete: an area figure with the mode's
  `area_source`, and in physical mode OpenROAD timing evidence; a clean exit
  without them never passes;
- no critical condition remains: no latches beyond `expected_latches`, no
  combinational loops beyond `expected_comb_loops`, no multi-driven nets, no unmapped processes.

The loop count comes from a Yosys `check` that Booley runs right after `synth`
and before mapping (`premap_check_<design>.txt`). Mapped library cells are black
boxes to `check`, so a check after mapping cannot trace a loop through them.
`check` works per module, so without `flatten` a loop that crosses a module
boundary is not counted. The multi-driver count comes from a final `check` run
after mapping and optimization (`check_<design>.txt`). The reported loop count
is the larger of the two, so a loop that either check reports counts toward
`expected_comb_loops`. Yosys also runs `check` inside `synth`; loops seen only there (an
optimization removes them, as in
[#1417](https://github.com/boldaxolotl/booley/issues/1417)) are advisory when
neither check finds a loop, and are kept with that rationale. Missing, stale,
or incomplete output from either check sets `structural_checks_complete` to
false and cannot pass.

Negative setup or hold slack grades `warn` and exits 0 unless
`[flows.synth].fail_on_timing_violation = true`, which makes it a failure
(exit 1). The default is lenient because early SDC files are often
placeholders; the cost is that a consumer reading only the exit code misses
negative slack.

Timeouts, infrastructure errors, and nonzero exits without metrics are Flow
errors (exit 2); critical conditions on an otherwise clean run are design
failures (exit 1). Either way the failing stage's own output (a missing
liberty file, a Yosys or sv2v error) is carried into the report.

### Warnings and Criteria detail

Each current and baseline result carries a bounded `warning_summary`: total
and unique counts, counts by EDA tool, category, and disposition, and one
representative message per group; `artifacts.log` keeps every raw message.
Structural warnings become fatal when the check that owns their count
confirms them and the confirmed count exceeds its declared allowance. A loop
within `expected_comb_loops` still prints its Yosys warning, so that run grades
`warn`. Other actionable warnings grade `warn` but keep `passed: true` and exit 0. A warning
known to be benign is counted with its rationale and does not downgrade the
result. A located Yosys warning (`file:line[:col]: Warning:`, in practice from
the `read_verilog` and `slang` frontends, slang on Yosys 0.69 and 0.70) is counted under category `frontend`
with disposition `informational`: it never grades `warn`, because it reports
the source rather than the synthesis. Its message drops the location and the
echoed source line so the same warning groups across lines, and a trailing
slang `[-W...]` is kept as the group's `code`. A located loop or multi-driver
warning keeps its structural classification. Informational groups sort after
all others (the first-listed loop group aside) in the representatives. Other unknown warning forms default to
actionable, so a new message is never silently ignored.

The Criterion detail carries the area, cell, and wire counts, the
`warning_summary`, the timing fields of the mode, the latch, loop,
multi-driver, and process counts, `baseline_metrics` whenever a baseline ran
(an explicit `--baseline` or the one pinned at Goal entry), the current and
baseline recipe fingerprints and snapshots, and `_metric_map` / `_min_allowed`
for threshold display. Report files are in
[FLOW_REPORTS.md](FLOW_REPORTS.md#synth).

## `fpga`

`fpga` is an FPGA quality-of-results check. It runs Vivado implementation,
normalizes utilization and timing, and satisfies `fpga_impl_ok_{target}` only
when the implementation evidence is complete and timing is met.

### Configuration boundary

The Target owns the build intent under `flow_options`: device `part`,
`ppa_profile`, and `out_of_context`, plus its sources, `toplevel`, XDC
fileset, and typed `vlogdefine` parameters
([FPGA Target](../user/TARGETS.md#fpga-target)). `[flows.fpga]` owns execution
and verdict policy (`timeout_ms`, `expected_latches`, `expected_comb_loops`) ([FLOW-FPGA.md](../user/FLOW-FPGA.md#configuration-flowsfpga)), and
Doctor fails recipe keys such as `part`, `ppa_profile`, or `out_of_context`
placed there. Keeping the part on the Target stops one global setting from
silently applying the wrong device to another Target. There is no per-call
define or clock-name argument: defines come from the Target's parameters and
clocks from its XDC.

The Target name's `fpga` axis decides whether the FPGA Flow can drive a Target
(`booley.targets.target_naming.fpga_intent`). `flow_options.tool` stays a
FuseSoC resolution input, mainly for `tool_<name>` conditional files, but does
not choose the backend: Booley always rebuilds the resolved inputs into a
Vivado EDAM. A Target with no axis prefix counts as FPGA when its tool is
`vivado`, so vendored upstream cores, which cannot be renamed, stay usable.

XDC is mandatory: a Target with no `file_type: xdc` fileset is a hard error
naming the Target and the fix, and `booley.toml` has no XDC setting. XDC
carries clocks, false paths, and pin placement, which are design intent, the
same reasoning as SDC for `synth`. Reporting and timing thresholds are per
clock, with clock names taken from the XDC.

Dry-run and real execution share one Target-recipe preflight: FuseSoC setup,
validation of part, top, XDC, and parameters, source partitioning, and
resolution of the portable profile to a Vivado mapping. Only a real run then
materializes the Vivado project. Multi-Target planning is all-or-nothing for
real runs: a setup failure on a later Target prevents every unit from running,
while a dry run keeps the valid earlier units next to Target-attributed
`aggregate_errors` for diagnosis.

Plain Doctor covers FPGA when `[flows.fpga]` exists or a Target is marked
`booley: {doctor: [fpga]}`, and the Flow is enabled. It runs the dry-run and a
bounded Vivado batch startup. Deep Doctor never runs a full implementation,
which takes too long for a health check; it prints one SKIP per Target with
the command to run by hand.

### Build execution

The Flow generates an Edalize `vivado` project (sources, XDC, part, defines,
generated Tcl) and runs its `make` target inside the Sandbox:

```
make -C .booley/.runtime/edalize/fpga/<target>
```

Before that, it writes the non-default `synth_1` / `impl_1` strategy for the
selected profile into the project Tcl. `balanced` writes nothing, so its Tcl
matches plain Edalize byte for byte. The strategy goes in before the
out-of-context patch, because Vivado resets synthesis step properties when the
strategy changes. `max_frequency` adds a post-route `phys_opt_design` step and
a Tcl hook that prints a completion marker; a run without that marker is not
accepted or cached, even if routing succeeded, because its numbers would not
come from the profile's final step.

`make` and every Vivado process run in the container whether Vivado comes
from the Sandbox Image (`provisioning = "image"`) or from an administrator's
registered host installation mounted read-only (`provisioning = "host"`). A
host-issued specification fixes the image, wrapper, mount, environment, and
license relay before the container starts, and a provisioning failure stops
startup rather than running anything on the host. The Project cannot choose a
host path, mount, execution location, or license environment. See
[SANDBOX.md](../user/SANDBOX.md#commercial-eda-provisioning).

### Normalized metrics

The build returns only the run log, stderr, and exit code; interpretation
stays in Booley. `parse_fpga_reports` (`flows/fpga/backends/vivado/edam.py`)
reads the routed reports (`*_utilization_placed.rpt`,
`*_timing_summary_routed.rpt`, `*_drc_routed.rpt`) and the implementation run
log, accepting only files written by this run. Everything else Vivado writes
(power, route status, checkpoints, `vivado.log`) is in the `impl`, `synth`, and
`build` directories that `artifacts.live_dirs` names.

| Metric | Role |
|--------|------|
| `lut_count`, `ff_count` | Required |
| `wns_ns`, `whs_ns` | Required: worst setup and hold slack across all clocks, in ns |
| `bram_count`, `dsp_count` | Reported when the device family provides them; never gate the verdict |
| `latch_count`, `comb_loop_count`, `multi_driven_count` | Critical conditions; a latch or loop count above `expected_latches` or `expected_comb_loops` (default 0) fails the run, and any multi-driven net does |
| `per_clock` | Per-clock `period_ns`, `wns_ns`, `whs_ns`, `critical_path_ps`, `fmax_mhz` |

Fmax and critical path only make sense per clock, so they exist only inside
`per_clock`, one entry per `create_clock` in Vivado's clock summary. Each entry
derives `critical_path_ps = (period_ns - wns_ns) * 1000` and
`fmax_mhz = 1e6 / critical_path_ps` (null when the delay is not positive); any
field is null when the clock has no path or no known period. A flat threshold
such as `fmax_mhz_min` applies to every clock; a clock-scoped one such as
`clk_i.fmax_mhz_min` applies to that clock only
([GOAL-MODE.md](../user/GOAL-MODE.md#thresholds)).

### Verdict semantics

`fpga_impl_ok_{target}` is satisfied only when:

- route completed (the implementation run log's success line). Route
  completion, not the `make` exit code, defines success, because a boardless
  QoR Target's `write_bitstream` fails by design for lack of a pinout. Only
  when route did not complete does the `make` exit code become the failure;
- the profile's completion marker is present (`max_frequency` only);
- fresh, nonempty utilization, timing, and DRC reports give LUT, FF, `wns_ns`,
  and `whs_ns`;
- `wns_ns >= 0` and `whs_ns >= 0`;
- the latch and loop counts do not exceed `expected_latches` and `expected_comb_loops`, and the multi-driver count is zero.

Provisioning and setup failures, a missing completion marker, and a routed run
whose reports lack the required metrics are Flow errors (exit 2). Timing
violations and critical design conditions are design failures (exit 1).

The Criterion detail carries the utilization and timing metrics,
`has_primary_metrics`, `timing_met`, `has_critical`, `baseline_metrics` when a
baseline ran, the current and baseline recipe fingerprints and snapshots, and
`_metric_map` / `_min_allowed` for threshold display. Report files are in
[FLOW_REPORTS.md](FLOW_REPORTS.md#fpga).
