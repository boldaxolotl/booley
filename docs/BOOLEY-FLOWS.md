# Booley Flows: Build and Evidence Contracts

This document is the implementation reference for Booley's built-in
deterministic EDA flows. It follows a FuseSoC Target through command generation,
execution, verdict interpretation, and durable evidence.

## Document boundary

The documentation is split by responsibility, not by reader type:

| Document | Owns |
|---|---|
| **This document** | The implementation and evidence contracts of the built-in `sim`, `elab`, `lint`, `synth`, and `fpga` Booley Flows |
| [MCP-TOOLS.md](MCP-TOOLS.md) | The generic MCP tool framework: discovery, lifecycle, base classes, result routing, and Custom Flows |
| [CONFIG.md](CONFIG.md) | The project configuration surface: exact keys, defaults, examples, `.core` design description, and `tests.toml` |
| [SUPPORTED-EDA-TOOLS.md](SUPPORTED-EDA-TOOLS.md) | The source-of-truth matrix of supported EDA engines, provisioning, trace support, and installation requirements |

Configuration is mentioned here only where its ownership or effect is part of a
build contract. Use `CONFIG.md`, not this document, to configure a project. Use
`MCP-TOOLS.md` when implementing or extending the MCP tool abstraction itself.

## Overview

Booley owns a unified build system built on **FuseSoC** rather than adapting each
project's Makefiles and TCL scripts. The design is described once in a `.core`
file, and every Flow run is generated from that description. Existing projects
therefore port their build description when adopting Booley; [SETUP.md](SETUP.md)
covers that process.

Owning the build system is what buys the rest: the agent reaches every EDA tool through one interface instead of guessing at per-project conventions, and **Criteria** (the named pass/fail conditions a Ticket must satisfy; see the [CONTEXT.md](CONTEXT.md) glossary) are tracked automatically from the results, which is also what makes the whole thing usable in CI.

A deterministic **Booley Flow** has to do two things: turn the caller's request into a real EDA tool invocation, and turn the result back into facts Booley can reason about. This doc covers both halves for the built-ins: the **invocation** half (FuseSoC and Edalize generate the command) and the **interpretation** half (the per-Flow evidence contract). See [ARCHITECTURE.md](ARCHITECTURE.md) for where this layer sits in the whole system.

Read [From `--target` to a command](#from---target-to-a-command-fusesoc-and-edalize)
and [Booley Flow evidence contracts](#booley-flow-evidence-contracts) in order: they
explain how a Target becomes a command and what evidence each built-in Booley Flow
must return. The remaining sections are per-Flow references for `sim`, `elab`,
`lint`, `synth`, and `fpga`.

## From `--target` to a command: FuseSoC and Edalize

Rather than hand-build commands per EDA tool, Booley builds command generation on two upstream libraries: **FuseSoC** resolves the design description and **Edalize** emits the backend command. These are internal building blocks, not external services. The deliberate line Booley draws is in *what it delegates to them*: it uses them to generate the command, but interpreting the result back into facts stays in Booley's own code.

The canonical design description is a FuseSoC **`.core` file** (CAPI2, FuseSoC's YAML schema). Each `.core` declares one or more **Targets**, and a Target fixes everything needed to *build* the design: the fileset (with `file_type` and `tags: [tb]` testbench markers), typed parameters, the toplevel module, and the EDA tool to use (`flow_options.tool`: Verilator, Icarus, Yosys, or Vivado; [SUPPORTED-EDA-TOOLS.md](SUPPORTED-EDA-TOOLS.md) is the source-of-truth matrix). The `--target` argument to `sim`, `lint`, `elab`, `synth`, and `fpga` names one of these Targets. Each is both a `booley flow` CLI selection (`booley flow sim --target sim_dut`) and an MCP tool the agent calls during Ticket execution; the Flow contract is the same either way.

Resolution happens in two phases (`src/booley/fusesoc/fusesoc_registry.py`): a cheap,
side-effect-free parse of the `.core` YAML (to validate `--target` names and
expand per-target Criteria), and a subprocess pass that runs FuseSoC beside the
Flow's Python orchestration—inside the Session Runtime for agent-facing calls—to
resolve filesets, parameters, and the `depends` graph into an **EDAM** (EDA
Metadata) that Booley reads rather than hand-assembles.

**Command generation is delegated; result interpretation is not.** This split is what makes the evidence contracts below possible. The EDAM feeds Edalize, whose flows (`Sim`, `Lint`, `Vivado`, `Generic`) emit an EDA-tool-specific Makefile or TCL script: the *command*, never the verdict. Booley then runs that command and does all interpretation itself: the Simulation Flow appends a `[SIM_SUMMARY]` verdict sentinel (a machine-readable verdict line), the Lint Flow dedupes Verilator warnings, and the FPGA Implementation Flow extracts utilization/timing from Vivado reports. Edalize's flow options are whitelisted per flow (`src/booley/flows/edam.py`) and file paths must resolve under the workspace, so a `.core` Target cannot smuggle arbitrary command structure across the sandbox boundary.

`synth` is the one exception: Edalize ships no Yosys ASIC-synthesis flow, only
the FPGA-oriented `icestorm`/`trellis`, so it resolves the Target through FuseSoC
for the filelist and toplevel, then invokes Booley's own `run_yosys_syn` wrapper
directly.

### Where configuration lives

Configuration is split across three files by **owner and concern**:

| File | Owner | Scope |
|------|-------|-------|
| `<name>.core` | FuseSoC | Design description: filesets, typed parameters, toplevel, per-flow Targets, and the EDA tool |
| `.booley_project/tests.toml` | Booley | Verification intent: per-Target test lists and the run-time test selector (e.g. a `+test_id=` plusarg template; a plusarg is a `+name=value` simulator argument) |
| `.booley_project/booley.toml` | Booley | Project metadata, source dirs, per-Flow policy, and approved EDA provisioning requests |

[CONFIG.md](CONFIG.md) owns the exact schemas, defaults, and configuration
examples for all three. The table is repeated here only because ownership of an
input determines which layer may interpret it.

Every Flow command executes inside the Session Runtime. Most EDA binaries ship
in the runtime image. A supported commercial tool may instead come from an
administrator-registered host installation mounted read-only under a built-in
policy. Project configuration requests host provisioning, while the exact host
Grant selects the Installation Registration; Project data cannot select a host
path, command, execution location, or license server.
The support matrix lives in
[SUPPORTED-EDA-TOOLS.md](SUPPORTED-EDA-TOOLS.md#built-in-flows). The per-Flow sections
below describe only how a built-in Flow uses its selected EDA tool.

Normalized reports produced in the Session Runtime live under the project
runtime tree, referred to below as `<runtime>` = `.booley_project/.runtime`.
The per-Flow report paths that follow (`<runtime>/flow-reports/...`) all resolve
there.

## Booley Flow evidence contracts

Booley Flows are **evidence contracts** between the caller and project-specific EDA flows, not simple wrappers around project commands. Their job is to turn messy EDA tool behavior (logs, exit codes, generated files, project build conventions) into facts Booley can safely reason about.

`sim` is the clearest example. Running the simulator is the easy part; normalizing the result into a verdict, a resolved Target, a resolved test identity, logs, reports, and optional trace artifacts is the real job.

Every Booley Flow contract provides the same kind of shared reality:

- **Verdict normalization.** The Flow produces a small, explicit outcome vocabulary rather than forcing the caller to interpret raw logs. For simulation, only a `pass` verdict satisfies `sim_pass_*`; `fail`, `elab_error`, and `timeout` fail the Criterion, `inconclusive` skips it entirely, and each points toward a different next action. Flow crashes and configuration errors sit outside this vocabulary: they surface as an exit-2 Flow error (Booley Flows exit 0 on a clean pass, 1 on findings or a design failure, 2 when the Flow itself could not run), not a per-test verdict.
- **Verdict integrity: never trust a stale artifact.** When a verdict comes from a file the run writes (a report, a JUnit XML, a log sentinel), anything present before the run must not survive into this run's evidence, and the exit code outranks the artifact. Otherwise a build that fails to compile leaves the *prior* run's artifact in place, and reading it reports a false pass on RTL that does not even build. A nonzero exit with no fresh evidence maps to `elab_error` or an infra verdict: never `pass`, never a functional `fail`.
- **Artifact normalization.** Logs, structured reports, and generated artifacts land in predictable locations. Waveform and debug tooling should not hunt through simulator-specific build directories; synthesis consumers should not scrape arbitrary output paths for reports.
- **Identity normalization.** Arguments such as `target` and `test` name entries in the project's FuseSoC/Booley configuration, not ad-hoc command-line fragments. A request to run one test resolves to exactly the test that ran, and the report carries that resolved identity.
- **Flow boundary.** Projects may use Verilator, Icarus, Vivado, per-test firmware builds (Pre-Run Commands, below), or DPI (C code linked into the simulation). Past the Booley Flow boundary, Booley sees the same contract regardless of the EDA tool or how its installation was provisioned.
- **Actionability.** The structured result tells the caller what to do next: debug a behavioral failure, fix elaboration, investigate a hang, repair an inconclusive testbench verdict, or mark a Criterion satisfied.

This applies to every built-in. `elab` converts compile-only and standalone-module
checks into `elab_pass_*` / `elaborate_standalone` evidence. Synthesis converts
backend-specific timing, area, and failure modes into comparable reports and
`synthesis_ok` Criteria. FPGA implementation converts Vivado utilization,
timing, and DRC evidence into stable resource metrics and `fpga_impl_ok`
Criteria. The stricter the evidence contract, the less the caller has to infer
from unstructured output.

### Ticket Target contracts

Ticket Mode treats the Target recipe as acceptance input, not implementation
work. Before enqueue, ticket creation records schema 1 with exact outer and
optional project-data commits, the criterion Targets, and a normalized SHA-256
digest; compatibility `base_sha` equals the outer commit.

The digest covers every `.core`, the test registry, Target-selecting Flow
configuration, selected SDC/XDC, and referenced generators or hooks. Paths are
part of the identity. RTL and testbench contents remain editable.

Contract metadata is published only after every repository validates and
commits. Execution starts from those commits, and intake, each Flow, the commit
guard, and review handoff reject drift as `target-contract-change-required`.
Relative synth/FPGA Targets must fully resolve at the seal so baseline and final
use one recipe; a future non-relative Target may omit only sources declared
Scope `[new]`.

Revision archives the old identity, clears execution evidence, and restarts
from the destination baseline without transplanting implementation commits.
Legacy running/review tickets may finish, but a new or reset execution requires
a valid seal.

### Shared run logs and artifacts

Every built-in Flow that keeps a `run.log` (`sim`, `elab`, `lint`, `synth`,
`fpga`) truncates it at the *start* of a run and stamps a one-line header —
`[BOOLEY RUN_LOG] run=… flow=… target=… started=…` — above the output. The body
only lands when the run finishes, so a log still showing `(run in progress …)`
under its header means this run has produced no output yet. Tailing during an
async-job wait therefore cannot surface a previous run's verdict as live, and
the same header lets a Flow decide whether a `run.log` pointer in its report is
honest.

The common `artifacts` shape, work-dir-relative path rules, multi-Target nesting,
and structured-output guarantees belong to the generic MCP tool/report contract
in [MCP-TOOLS.md](MCP-TOOLS.md#common-artifact-contract). Built-in sections below
name only their EDA-tool-specific directory roles, files, and freshness rules.

## `sim`

`sim` runs the resolved Target's tests and normalizes the outcome into a
per-test verdict, a per-target `sim_pass_{target}` Criterion, durable logs and
reports, and optional trace artifacts. The **EDA tool** (Verilator or Icarus,
read from the Target's `flow_options.tool`) picks the build-recipe shape.
Whether the Target is a **cocotb** one (a Python testbench driving the DUT) is
an independent property, read from the
Target's `cocotb_module` flow option (never from `tests.toml`); cocotb Targets
are sandbox-only in v1 and drive test selection through `COCOTB_TEST_FILTER`
rather than a plusarg.

### Configuration boundary

`[flows.sim]` holds execution and verdict policy; `tests.toml` holds the
per-Target test list and run-time selector; the FuseSoC Target holds the build
inputs and simulator choice. [CONFIG.md](CONFIG.md#simulation--passfail-sentinels-flowssim)
owns the exact simulation keys and defaults, while its
[design-description section](CONFIG.md#design-description-core-and-tests-teststoml)
owns the `tests.toml` schema.

The CLI selectors `--test` (substring include-filter), `--skip`, `--trace`
(debug-only; never a pass/fail source), `--timeout`, and `--dry-run` resolve
against those config entries rather than acting as raw command fragments.

**Pre-Run Commands** (`[flows.sim].pre_run_commands`) are the one
project-owned hook, and they do not loosen the contract. Shell lines run at the
Session Runtime immediately before each run (per test for an HDL Target, once per
Cocotb batch), under a `BOOLEY_*` env contract that names the run
(`BOOLEY_TEST_NAME` / `BOOLEY_TEST_NAMES`, `BOOLEY_TARGET`) and its authoritative
directories (`BOOLEY_RUN_CWD`, `BOOLEY_BUILD_ROOT`). This is how a per-test
non-RTL build step (e.g. cross-compiling the selected test's firmware) joins the
Simulation Flow: a failing pre-run is recorded as that test's failed result with
an attributed tail. It can never manufacture a pass, and it never crashes the
Flow.

### Verdict semantics

Each test resolves to exactly one of five verdicts:

- `pass`: the run's `[SIM_SUMMARY]` sentinel reports `passed` with zero SVA (SystemVerilog Assertion) errors.
- `fail`: a failing sentinel, a nonzero exit with fresh evidence, or SVA errors.
- `inconclusive`: the sim ran cleanly (exit 0, no SVA errors) but produced *no* verdict sentinel, so nothing affirmatively passed; also where a `--trace` run lands if it otherwise passed but could not confirm the trace was written.
- `elab_error`: build/elaboration/compile failure, or a failed Pre-Run Command (the sim never ran).
- `timeout`: the per-test budget was exceeded.

The authority is the `[SIM_SUMMARY]` JSON line, not the exit code alone. Raw
Verilator/Icarus runs emit no summary, so Booley re-derives one from
`[SIM_RESULT]` sentinels (plus project sentinels) and the exit code, with a
**fail sentinel winning over a pass sentinel**. cocotb is the exception: its
verdict comes from the JUnit `results.xml`, since the process exit code is
untrustworthy in both directions; even so, an all-pass XML after a nonzero
exit still folds down to FAIL. Stale artifacts never leak in: cocotb runs unlink
`results.xml` before each run, and HDL runs ignore result files older than the
current invocation.

Only a true target-level `pass` satisfies `sim_pass_{target}`; an
`inconclusive` target skips the Criterion rather than failing it. Flow crashes
and configuration errors (bad `--target`, disabled Flow, unknown `--test`) are
exit-2 Flow errors, outside the verdict vocabulary.

### Reports and artifacts

Every run writes a per-Target JSON report at
`<runtime>/flow-reports/sim_{target}.json` carrying the resolved identity (`target`,
`tb_top`, `eda_tool`), timing, the target `passed` flag, and a `tests`
list: one entry per test with its `name`, `verdict`, `sva_errors`, and an
`error_tail`. For native HDL Targets, every entry also carries
`artifacts.run_log`, a work-directory-relative pointer to an atomic,
unabridged copy of that test's simulator output. A grouped run preserves this
copy before starting the next test, including for failed, timed-out, and
inconclusive tests that produced output. The Target build directory retains its
compatibility `run.log` (the latest test's output, tail-truncated to 10 MB) and
`result.json`; cocotb adds its `results.xml`.

`sim` adds one stricter invariant: it omits the entire block unless the current
run's header proves the pointers are fresh. A build that dies before the
simulator starts must not cite a previous run's `result.json` or trace as current
evidence.

Trace artifacts stay off the pass/fail path by design: `--trace` builds a
separate trace-overlay Target (its own build root) so it never clobbers the fast
verdict build, and produces a queryable `trace.fst` waveform store next to the
logs. A trace that fails to materialize downgrades a passing run to
`inconclusive`: a missing waveform is a tooling failure, never a silent pass.

## `elab`

`elab` compiles and elaborates one or more Targets without running their
testbenches. For simulation Targets it uses the same FuseSoC/Edalize build path
as `sim`; for ASIC Targets it uses the same `sv2v` or `slang` frontend that
`synth` will use. It records `elab_pass_{target}` for each Target and can also
evaluate the project-wide `elaborate_standalone` Criterion.

### Configuration boundary

The selected Target owns sources, toplevel, parameters, and frontend choice.
`[flows.elab]` owns `enabled`, the default Target, build-tree retention, and the
standalone probe frontend. Edalize-backed builds and ASIC frontend checks both
run inside the Session Runtime.
[CONFIG.md](CONFIG.md#elaboration-flowselab) owns the exact keys and defaults.

The public selectors are `--target`, `--dry-run`, `--timeout`, and
`--standalone`. A Ticket that declares `elaborate_standalone` requests the
standalone sweep automatically, so `--standalone` is mainly the Interactive and
direct-CLI opt-in.

### Primary and standalone checks

The primary check builds each Target to the compile/link boundary and never
starts the simulator. A compiler that ran and rejected the source is a design
FAIL; Target resolution, setup, and missing-tool failures are Flow ERRORs. Each
Target is isolated, so one setup failure does not prevent the remaining Targets
from producing evidence.

The standalone sweep covers the union of the selected Targets' RTL filesets. It
lexically discovers every module and probes each from its declaring file, with
package/interface files included as shared prerequisites.
`standalone_frontend = "auto"` uses Verilator when available and otherwise
`iverilog -g2012`; the probes always run inside the Session Runtime. A genuine standalone compile failure
leaves `elaborate_standalone` unmet. If a different probe frontend cannot parse a construct
that the primary build accepted, the affected module is ungraded; a sweep that
reaches no verdict is a Flow ERROR rather than a design failure.

### Reports and artifacts

Each Target writes `<runtime>/flow-reports/elab_{target}.json` with its resolved
identity, EDA tool, compile command, RTL/TB fileset, elapsed time, verdict,
error tail, and artifact pointers when available. Its complete compiler output
is retained in a per-Target `run.log` on both pass and fail.

Passing compiler build trees are removed by default because they are large and
the durable evidence is already normalized. A failing tree is retained; set
`[flows.elab].keep_build_dir = true` to retain passing trees for incremental
rebuilds. The standalone sweep uses its own work directory and `run.log`, so it
cannot overwrite a Target's evidence.

## `lint`

`lint` runs the resolved Target's linter and normalizes its diagnostics into a
WARN/FAIL/ERROR verdict, a per-target `lint_clean_{target}` Criterion, and
durable logs and reports (`run.log` plus `lint_report.json`) — deduplicated
across Targets and scope-filtered along the way. There is one `lint` Booley Flow and
the **Target** picks the linter: Verilator `--lint-only` for structural/semantic findings,
Verible `verible-verilog-lint` for style/naming, selected by the Target's
`flow_options.tool`. Wanting both on the same RTL is two lint Targets (say
`lint_dut` and `lint_style`), and therefore two Criteria:
`lint_clean_{target}` means "clean under whatever linter that Target names",
never "Verilator-clean".

### Configuration boundary

The Target selects the linter and owns its rules and waivers. `[flows.lint]`
holds execution policy, including whether warnings affect the process exit.
[CONFIG.md](CONFIG.md#lint-flowslint) owns the exact keys and defaults.

Lint *policy*, which rules fire and what is waived, is design description,
not Booley config, so it lives on the Target: a `.vlt` file for Verilator,
`veribleLintRules` / `veribleLintWaiver` filesets (and the `ruleset` / `rules`
flow options) for Verible. Booley adds no severity tiers and no waiver
machinery of its own: every finding counts against the Criterion, and a waiver
edit lands in the diff like any other change, where ticket Scope and the
Reviewer agent ([CONTEXT.md](CONTEXT.md)) are the control.

The CLI adds `--scope` (comma-separated path fragments, which filter the findings
*and* the Criteria counts with them), `--dry-run`, and `--timeout` (ms, default
120000).

### Verdict semantics

| Outcome | Exit | When |
|---------|------|------|
| PASS | 0 | the linter ran and left zero in-scope findings after dedup |
| WARN / FAIL | 1 | findings remain, or the linter ran and rejected the RTL |
| ERROR | 2 | no verdict was reached: missing binary, setup failure, timeout |

Two rules carry the weight here.

**A non-zero linter exit is never a clean lint.** Findings are counted from
parsed `%Warning` / Verible finding lines, so a run that dies before emitting
any would otherwise score zero findings and pass. A recorded failure always
outranks the tally. The one deliberate exception is Verilator's
location-less `Exiting due to N warning(s)` epilogue on a warnings-only run.
Grading that a hard failure would make `warnings_as_errors = false` inert, so
the verdict flows through the warning tally and the knob instead.

**FAIL versus ERROR splits on *who* failed.** A linter that ran and rejected
the design is the linter working, so that is a design FAIL, the same grade
`elab` gives the identical source. A linter that could not run at all is
an ERROR that names the installation fix (normally rebuilding the runtime image).
Verible's EDA tool node is invoked
`--parse_fatal --lint_fatal=false` precisely to preserve that split: a parse
failure makes Verible itself exit non-zero → ERROR, while findings leave
Verible at exit 0 with parseable lines → a WARN from Booley. One unusable linter decides the whole run, because the other Targets'
verdicts are then not trustworthy evidence of a clean design.

`warnings_as_errors = false` moves the exit code only. The WARN text stays and
`lint_clean_{target}` still records the finding count truthfully, so a CI gate
can pass on warnings-only while the Criteria keep the honest number.

### Findings and reports

Findings are parsed with the shared regexes in the private parser module
(`booley.flows._eda_parsers`, one source of truth for the Verilator and
Verible dialects), then deduplicated across Targets on `(rule, file, line)`
and scope-filtered.
The console echoes the first five; the full list always goes to the report:

```text
<runtime>/flow-reports/lint_report.json           # stable "latest run" path
<runtime>/flow-reports/lint/<N>/lint_report.json  # per-invocation copy
```

The numbered copy exists because consecutive runs would otherwise clobber each
other: a Verilator pass followed by a Verible pass is two runs of one Flow.
The report carries `passed`, `total_warnings`, per-finding rule/file:line/
message, any `errors`, and a `target_results` entry per Target: the EDA tool
that actually linted, finding count, `files_linted`, `toplevel`,
`toplevel_linted`, and duration.

**Coverage guard (`toplevel_linted`).** A Target can lint a fileset that
excludes its own toplevel: a style fileset trimmed of macro-heavy files, for
instance. Nothing in the linter's output says so, and the findings then cover
only part of the design while reading as a clean bill for all of it. Booley
scans the resolved sources for a `module <toplevel>` declaration and surfaces
the hole as a warning line. It is best-effort by design: any read failure
counts as declared, so an I/O hiccup never fabricates a coverage warning.

## `synth`

`synth` is a fast ASIC quality-of-results (QoR) estimate. It maps RTL through
Yosys in both modes; physical mode continues through OpenROAD, while logical
mode stops after mapping. The Flow normalizes area, physical timing or a logical
frequency estimate, and
latch/loop conditions into a per-target `synthesis_ok_{target}` Criterion.

> **PPA estimate, not sign-off.** In default `physical` mode, the numbers come
> after quick floorplan / global placement / setup repair but are still
> pre-layout estimates, not a tape-out flow. `logical` is a faster mapped-area
> flow with only a rough logic-delay frequency estimate. See
> [SUPPORTED-EDA-TOOLS.md](SUPPORTED-EDA-TOOLS.md#built-in-flows).

### Configuration boundary

The `.core` Target owns the persistent recipe and timing inputs: frontend,
profile, flattening, synthesis mode, backend overrides, source files, SDC, and
toplevel. `[flows.synth]` owns execution and verdict policy such as the default
Target, timeout, and intentional-latch allowance.
[CONFIG.md](CONFIG.md#asic-synthesis-flowssynth) owns the exact keys, defaults,
and examples.

`ppa_profile` and `synth_mode` are the main synthesis controls. The built-in
backend translates the profile to Yosys and OpenROAD settings; backend-specific
knobs live under `advanced_settings_yosys` and `advanced_settings_openroad`.
Per-call profile and expert overrides are resolved before command generation;
their precedence and profile contents are part of the
[configuration reference](CONFIG.md#asic-synthesis-flowssynth).

Timing intent lives in the Target's SDC fileset (below), not in config scalars.

### Constraints (SDC)

Timing constraints are **design intent** (clock period, I/O delays,
false/multicycle paths), so they live on the FuseSoC Target as a `file_type:
SDC` fileset, source-controlled and per-target like the RTL, symmetric with how
FPGA XDC is a Target fileset. The configuration shape and example live in
[CONFIG.md](CONFIG.md#asic-synthesis-flowssynth).

A physical Target with **no** SDC fileset **and** no explicit clock is a **hard
error**, not a silent default: the run fails loudly, naming the Target and the
fix, rather than fabricating a clock the author never chose. Logical mode does
not run STA, so it neither requires nor consumes SDC. The only way to a canned
clock in physical mode without SDC is the explicit per-run `--default-clock
<ps>` opt-in.
When the Target's SDC declares its own `create_clock` / `set_input_delay` /
`set_output_delay`, that fully owns the timing intent and the Fmax readout
recovers the effective period from the SDC's `create_clock`, not a config scalar.

**Constrain the clock near the design's realistic target.** Too aggressive a
clock, say a 4 ns (250 MHz) constraint on a design whose real speed is tens of
MHz, makes `repair_timing` buffer/upsize thousands of instances chasing an
impossible constraint, running for minutes before STA. Near the achievable
period it converges quickly, with Fmax falling out of the slack.

### Synthesis modes and timing evidence

- **`physical`** (default) runs OpenROAD floorplanning, global placement,
  optimization, placement-based parasitic estimation, and OpenROAD's embedded
  OpenSTA. Timing is reported per clock, including `critical_path_ps`,
  `fmax_mhz`, `wns_ns`, and `whs_ns`. `area_um2` is the OpenROAD
  post-optimization area and `area_source` is
  `openroad_post_optimization`. Missing OpenROAD/PDK inputs or incomplete
  physical results fail the run; there is no standalone OpenSTA fallback.
- **`logical`** stops after Yosys technology mapping. It is the fast option for
  area iteration. `estimated_fmax_mhz` is derived from ABC's slowest
  liberty-mapped combinational partition, but it is not STA and is not
  per-clock. It excludes placement, wire delay, clock-to-Q, and setup time, so
  Booley warns that the estimate is probably inaccurate and never uses it for
  timing thresholds. `area_um2` is Yosys's liberty-mapped area and
  `area_source` is `yosys_mapped`.

Both modes expose the same canonical area fields: `area_um2` and
`area_source`. There are no parallel mapped/post-optimization area fields.

Beyond the parsed metrics, [`artifacts.dirs`](#shared-run-logs-and-artifacts) names the two
directories holding everything the run wrote:

- **`timing`** — physical-mode STA reports: `overall.rpt` and its
  machine-readable `overall.csv.rpt` twin plus `reg2reg.rpt`. List it for the
  per-path slack breakdown behind the summarized `per_clock` numbers. Absent
  in logical mode.
- **`build`** — everything else: `stat_<design>.txt` (the `stat -liberty` output
  `area_um2`/`cells` are parsed from, with the per-cell-type breakdown), both
  netlists, the per-stage `yosys.log` / `openroad.log` / `sv2v.log`,
  the rendered `synth.ys`, and the SDC actually fed to STA.

Worth reaching for on a large design, where `run.log` inlines every stage's full
text and runs to megabytes.

A clock's `critical_path_ps` / `fmax_mhz` is its single most-negative-slack
path. With a non-zero I/O-delay budget from the SDC, an I/O path can dominate
that number and hide the true internal logic speed, so every run *also* reports
the worst **register-to-register** path (`reg2reg_fmax_mhz` /
`reg2reg_slack_ns` in the JSON report and the QoR line), unaffected by the I/O
budget. Compare reg→reg numbers for A/B logic changes; author
`set_input/output_delay 0` in the Target SDC only if you also want the
*overall* path to reflect reg→reg.

### Intentional latches

An inferred latch is normally a design bug, so any latch fails the run. But not
every latch is an accident: a standard-cell **integrated clock-gating** cell is
built from a deliberate `always_latch`, and lowRISC's generic `prim_clock_gating`
contains exactly one. Declare how many the design contains on purpose with
`[flows.synth].expected_latches`. It is an **allowance, not a mute**:
the raw inferred count is always reported, and one latch more than declared still
fails, so the check keeps catching the accidental latch it exists for. Set it
only for latches you can point at in the RTL, and prefer mapping the cell to your
library's real ICG primitive when the technology provides one.

### RTL frontend

`frontend` picks how the RTL enters Yosys (`--frontend` overrides the Target
default). `sv2v` (default) transpiles SystemVerilog → Verilog, then
`read_verilog`, and works on every sandbox image. `slang` reads SystemVerilog
natively (`read_slang`, no transpile) and needs a **Yosys ≥ 0.67** image; on an
older one the run fails fast and tells you to switch frontend or rebuild. Reach
for `slang` when the design puts **parameterized interfaces on module port
lists** or reads their parameters hierarchically
(`localparam KEEP_W = s_axis.KEEP_W`). sv2v transpiles neither, so there it is a
requirement, not a preference. Both frontends feed the same tech-mapping and
timing tail, so the choice affects elaboration only, not the PPA methodology.
For the full comparison and known `slang` limitations, see
[SUPPORTED-EDA-TOOLS.md → RTL frontend](SUPPORTED-EDA-TOOLS.md#synth-rtl-frontend-sv2v-vs-slang).

### Verdict semantics

`synthesis_ok_{target}` is satisfied only when:

- the flow exits 0 without hitting the per-config timeout;
- usable metrics are present (a cell count or an area figure); a clean exit
  that produced no metrics never passes;
- no critical condition remains: zero latches beyond `expected_latches`, zero
  combinational loops, zero multi-driven nets, and zero unmapped processes.

Timeouts, infrastructure errors, and metric-less nonzero exits are Flow
failures; critical conditions on an otherwise clean run are design failures.
Either way the failing stage's own output (a missing liberty file, a
Yosys/sv2v error) is carried into the report, so the reason is named instead
of a bare "no metrics".

#### Ticket baselines and sealed recipes

Ticket Mode's shared baseline and recipe invariants are defined in
[Ticket Target contracts](#ticket-target-contracts).

### Reports and Criteria detail

For each Target, the Flow writes:

```text
<runtime>/flow-reports/synth_<target>.json    # per-target metrics (+ baseline)
<runtime>/flow-reports/synth/<N>/report.json  # per-invocation structured report
<runtime>/flow-reports/synth.json             # flat compatibility copy of the latest report
```

The full synthesis output is persisted as `run.log` in the per-target Edalize
work dir, on pass and fail alike.

The Criteria detail includes:

- `area_um2`, `area_kge`, `cells`, `wire_count`
- `estimated_fmax_mhz` in logical mode; `per_clock` (per-clock
  `critical_path_ps` / `fmax_mhz` / `wns_ns` /
  `whs_ns`), aggregate `wns_ns` / `whs_ns`, `reg2reg_fmax_mhz`, `reg2reg_slack_ns`
- `latches`, `expected_latches`, `unexpected_latches`, `comb_loops`,
  `multi_driven`, `process_count`
- `baseline_metrics`, when `--baseline` is used
- normalized current/baseline recipe fingerprints and snapshots, with their
  semantic differences summarized in the Review package
- `_metric_map` and `_min_allowed` for threshold/acceptance display

## `fpga`

`fpga` is an FPGA quality-of-results check. It runs implementation,
normalizes utilization and timing metrics, writes Booley reports, and satisfies
`fpga_impl_ok_{target}` only when implementation evidence is complete and
timing is met.

### Configuration boundary

The selected Target owns FPGA build intent: device part, out-of-context choice,
sources, XDC, toplevel, and compile-time defines. `[flows.fpga]` owns execution
policy and default Target selection. [CONFIG.md](CONFIG.md#fpga-implementation-flowsfpga)
owns the exact keys, defaults, and examples.

The device `part`, `out_of_context` choice, and other build-recipe inputs live
under the selected Target's `flow_options`. XDC constraints are a Target
`file_type: xdc` fileset, and compile-time defines are typed `vlogdefine`
parameters. Doctor rejects those build inputs under `[flows.fpga]`.

The Booley Flow generates an Edalize `vivado` project whose `make` target invokes
Vivado inside the Session Runtime. With `provisioning = "image"`, the runtime
image must satisfy the built-in Vivado wrapper contract. With
`provisioning = "host"`, an administrator registers an exact supported Vivado
release and grants the Project access; Booley mounts that release read-only at
the fixed path expected by the image-owned wrapper. There is no Project-owned
host path, `vivado_path`, arbitrary mount, or execution-location knob.

License Profiles are also host-owned. When one is authorized, the runtime
receives only a fixed pointer to its session-owned relay, never a Project-chosen
license environment or destination. See
[SUPPORTED-EDA-TOOLS.md](SUPPORTED-EDA-TOOLS.md) for the currently validated
Vivado version, platform, and licensing status.

### Constraints (XDC)

XDC constraints are **design intent**: they carry `create_clock`, false paths,
and pin placement (`set_property PACKAGE_PIN`), so they live on the FuseSoC
Target, not in `booley.toml`, symmetric with how ASIC SDC is a Target
fileset.

The source is a `file_type: xdc` fileset in the `.core`:
source-controlled and per-target. Multiple XDC files are supported; the
configuration shape and example live in
[CONFIG.md](CONFIG.md#fpga-implementation-flowsfpga).

The `file_type: xdc` fileset is the **sole** source: a Target with no such
fileset is a hard error naming the Target and the fix. XDC is mandatory, and
`booley.toml` has no `xdc` key — constraints belong with the design, not the
Flow configuration.

The FPGA `part` (e.g. `xc7a200tfbg484-1`) is Target-specific build intent and
therefore lives at `flow_options.part`. Keeping it beside the Target prevents a
single global Flow section from silently applying the wrong device to another
Target.

### Public CLI

```bash
booley flow fpga --target <target>[,<target>...] [--baseline <ref>] [--no-cache] [--dry-run] \
  [--timeout <ms>]
```

`fpga` selects a FuseSoC Target via `--target`. Per-call build-time flags are
limited to the controls shown above; design inputs come from the Target and
Flow configuration.

Field rules:

- `--target`: one or more FuseSoC Target names; required on every invocation.
- `--baseline`: compare against a git ref. The baseline is built in an
  ephemeral `git worktree`, so it works in Interactive Mode as well as Ticket
  Mode (the two execution modes; see [CONTEXT.md](CONTEXT.md)). A Ticket Mode
  criterion with a relative threshold supplies its immutable `base_sha`
  automatically.
- `--no-cache`: force a fresh implementation even when a matching reusable
  result exists. The fresh result replaces the cache after it completes.
- `--dry-run`: validate inputs and print the planned Vivado build
  (part/top/XDC and resolved source counts) without running Vivado.
- `--timeout`: per-target timeout in milliseconds (default 7200000).

All build-time inputs come from the Target, not the Flow policy section: the
part from `flow_options.part`, XDC from the Target's `file_type: xdc`
fileset (see [Constraints (XDC)](#constraints-xdc) above; clock definitions
live there too, so there is no clock-name argument), the top module from the
Target's `toplevel` (its absence is a hard error), and defines from the Target's
declared `vlogdefine` parameters (`-d/--define` was removed).

Reporting and timing thresholds are nonetheless **per-clock**: the clock names
come from the XDC and surface in the `per_clock` metric map below.

### Build execution

Both provisioning sources run the **same** Edalize `vivado` project inside the
Session Runtime. The Booley Flow materializes it (sources, XDC, part, defines,
generated Tcl), then invokes its `make` target:

```
make -C .booley_project/.runtime/edalize/fpga/<target>
```

The `make` process and every Vivado subprocess remain in the container whether
the executable files came from the image or an approved read-only host mount.
A host-issued immutable specification fixes the image, wrapper, mount target,
environment, labels, and optional relay topology before Docker creates or
resumes the container. Provisioning failures stop startup; they cannot fall
back to executing the command on the host.

### Normalized metrics

The build returns only the raw run log, stderr, and exit code; interpretation
stays in Booley. Booley parses the Vivado-written route reports (`*_utilization_placed.rpt`,
`*_timing_summary_routed.rpt`, `*_drc_routed.rpt`, plus the impl run log)
under the materialized project (age-gated so only reports written by this
run count) into the normalized metric dict. Everything else Vivado wrote —
power, route status, clock utilization, the synth-stage utilization, the design
checkpoints, `vivado.log` — is reachable by listing the `impl` / `synth` /
`build` directories that [`artifacts.dirs`](#shared-run-logs-and-artifacts) names:

```json
{
  "status": "pass",
  "lut_count": 1200,
  "ff_count": 700,
  "bram_count": 2,
  "dsp_count": 1,
  "wns_ns": 0.25,
  "whs_ns": 0.10,
  "per_clock": {
    "clk_i":  {"period_ns": 10.0, "wns_ns": 0.25,  "whs_ns": 0.10, "critical_path_ps": 9750.0, "fmax_mhz": 102.56},
    "clk_2x": {"period_ns": 5.0,  "wns_ns": -0.10, "whs_ns": 0.05, "critical_path_ps": 5100.0, "fmax_mhz": 196.08}
  },
  "latch_count": 0,
  "comb_loop_count": 0,
  "multi_driven_count": 0
}
```

| Metric | Role |
|--------|------|
| `lut_count`, `ff_count` | Required: absence leaves the Criterion unsatisfied |
| `wns_ns`, `whs_ns` | Required: worst negative setup / worst hold slack in ns, the honest worst case across all clocks |
| `bram_count`, `dsp_count` | Reported when the FPGA family provides them; not verdict-gating |
| `latch_count`, `comb_loop_count`, `multi_driven_count` | Critical-condition counters; non-zero fails the run (see Verdict semantics) |

Per-clock timing (`per_clock`): Fmax and critical-path delay are inherently
**per-clock**, so they come back as a map keyed by clock name, one entry per
`create_clock` in Vivado's Clock Summary (a single-clock design has exactly
one), never as top-level scalars (the old `critical_path_ps` / `fmax_mhz`
scalars are gone, with no back-compat alias). Each entry carries the clock's
constrained `period_ns`, its `wns_ns` / `whs_ns`, and two derived fields:
`critical_path_ps = (period_ns − wns_ns) × 1000` and
`fmax_mhz = 1e6 / critical_path_ps` (`null` when the derived delay is
non-positive). Any sub-field may be `null` when the clock has no path or its
period is unknown.

Timing thresholds (`fpga_impl_ok`) gate on these per-clock values. A **flat**
`critical_path_ps_max` / `fmax_mhz_min` gates the timing-worst clock, so every
clock must pass, while a **clock-scoped** `clk_i.fmax_mhz_min` gates just
clock `clk_i` (see [USAGE.md](USAGE.md#synthesis--fpga-threshold-flavours)).

### Verdict semantics

`fpga_impl_ok_{target}` is satisfied only when:

- the build completes without infrastructure error;
- `route_design` completed (the run log's success marker). Route completion
  (not the `make` exit code) defines success: a boardless QoR target's
  `write_bitstream` fails by design (no pinout) without failing the run. Only
  when route did not complete is the `make` exit code surfaced as the failure;
- LUT and FF metrics are present;
- `wns_ns >= 0` and `whs_ns >= 0` (both present);
- latch, combinational-loop, and multi-driven counters are zero.

Provisioning and setup failures are Flow errors. Missing metrics, timing
violations, and critical design conditions are design failures.

#### Ticket baselines and sealed recipes

Ticket Mode's shared baseline and recipe invariants are defined in
[Ticket Target contracts](#ticket-target-contracts).

### Reports and Criteria detail

For each Target, the Flow writes:

```text
<runtime>/flow-reports/fpga_<target>.json    # per-target metrics (+ baseline)
<runtime>/flow-reports/fpga/<N>/report.json  # per-invocation structured report
<runtime>/flow-reports/fpga.json             # flat compatibility copy of the latest report
```

The Criteria detail includes:

- `lut_count`, `ff_count`, `bram_count`, `dsp_count`
- `wns_ns`, `whs_ns`, `per_clock` (per-clock `period_ns` / `wns_ns` / `whs_ns` /
  `critical_path_ps` / `fmax_mhz`)
- `has_primary_metrics`, `timing_met`, `has_critical`
- `latches`, `comb_loops`, `multi_driven`
- `baseline_metrics`, when `--baseline` is used
- normalized current/baseline recipe fingerprints and snapshots, with their
  semantic differences summarized in the Review package
- `_metric_map` and `_min_allowed` for threshold/acceptance display
