## Booley: Quick Reference

### Commands

Every public top-level command; run `booley <command> --help` for its options
and nested actions.

#### Sandbox-only commands

On the host, bare `booley` prints help and a getting-started hint.
Inside the Sandbox, for Interactive Mode, we recommend `booley` / `booley chat`, which launches
`claude` or `codex` according to `[agent].provider`.

| Command | Purpose |
|---------|---------|
| `booley chat` | Explicit spelling of the Sandbox default `booley` command |
| `booley dashboard` | Show sessions, Goals, and Jobs |
| `booley flow` | List or directly run deterministic Booley Flows |
| `booley specialist` | List or directly run visible Specialists |
| `booley worktree` | Create a linked worktree with a Paired Checkout or Snapshot |

#### Either-location commands

These commands work on the host and inside the Sandbox. Full Target resolution
requires the Sandbox.

| Command | Location | Purpose |
|---------|----------|---------|
| `booley` | either | Host: help; Sandbox: open the configured Developer Agent CLI |
| `booley cheat` | either | List sections; use section flags or `--all` to print the reference |
| `booley doctor` | either | Check project, Sandbox, and toolchain health |
| `booley help` | either | Show top-level help, same as `booley --help` |
| `booley resources` | either | Show measured peak memory and run time per Target and Job phase |
| `booley targets` | either | List or filter Targets; full detail resolution requires the Sandbox |

#### Host-only commands

| Command | Purpose |
|---------|---------|
| `booley auth` | Configure or inspect Developer Agent and Specialist credentials |
| `booley bootstrap` | Check or reconcile reusable host resources |
| `booley eda` | Manage host commercial-EDA installations, grants, and licenses |
| `booley init` | Initialize, scaffold, or reseed project integration |
| `booley projects` | List remembered Project paths and their Grants |

Use the [Skills](#skills) for guided agent workflows such as Goal Mode, feedback,
version review, Project Setup cleanup, and Sandbox lifecycle management.

For Host Bootstrap, `--check-only` reports pending work without writing and
`--force` refreshes Booley-managed resources while preserving caches and
user-owned files. Force does not overwrite configuration or disable Docker's
layer cache.

### Goal Mode

Use `/booley-goal` in a Developer Agent chat. One session owns a clean linked
worktree and Goal Branch; every Goal is mandatory and judged by Flow or
Specialist evidence at the current code.

| Step/tool | Purpose |
|-----------|---------|
| `goal_enter` | Translate Goalsets into concrete Goals and create a Goal Branch |
| `goal_status` | Inspect evidence and freshness; `rules=true` restores working rules |
| `goal_propose_change` | Add, relax, retarget, or propose a coverage waiver; only the human approves |
| `goal_finish` | All Goals met at clean committed HEAD plus Session Summary → Review Package |
| `goal_finish(abandon=true)` | Explicit human instruction only; preserve branch and worktree |
| `booley goal status` | Inspect local records |
| `booley dashboard` | Sessions, Goals, and Jobs inside the Sandbox |

Pass the absolute worktree root as `work_dir` to Booley Flows, Specialists,
Goal tools, `booley_targets`, and `booley_report`. While a Goal Record
occupies the Project, calls without it are refused (only `booley_targets` may
omit it), and Project-defined endpoints must declare `work_dir` to run.
`booley_status`, `booley_poll`, `booley_cancel`, and `bwave` accept no `work_dir`.
With several Jobs running, call `booley_poll` with no `run_id` and handle whichever of your Jobs finishes first; pass a `run_id` only to wait for one specific Job. Never wait for a Job with `sleep` loops or log tails.
Resume saved proposal and Finish IDs rather than editing records. Protected Input changes block
Finish until reverted. Open `done` review findings remain visible; Finish does not merge.
Read Project-owned Goalsets, including `default.md`, from `goalsets/` in the
Main Booley Directory (`BOOLEY_DIR` in the Sandbox); a
Worktree Booley Directory inside `work_dir` is not used. `default.md` applies
unless the human explicitly skips it with a reason. Initialization seeds four
create-only Goalsets and preserves edits.

#### Test Runs

A Test Run is one Test Variant simulated with one seed, written
`test[+variant][@seed]`. Test Variants and the seed mechanism are declared per
Target in `tests.toml`.

| Form | Meaning |
|------|---------|
| `--test stress` | Default Test Variant at the Target's Default Seed (`1` unless `default_seed` is set) |
| `--test stress+soak` | Test Variant `soak` at the Default Seed |
| `--test stress+soak@42` | That Test Variant with seed 42 |
| `--test stress@x20` | 20 Test Runs with different drawn seeds (N is 1 to 1000) |
| `--seed 42` / `--seed random` | One seed for every selected Test Run / a drawn seed per Test Run; not with `@seed` or `@xN` |

A request may run at most 1000 Test Runs of one Target. An unseeded Target
refuses every seed: an HDL-testbench Target on Icarus without a `seed`
template.
Simulation Goals take the same selector in `test`, Cycle Count Goals in `test`
without `@xN`, and coverage Goals in `tests`. Goal Mode freezes the Test Runs
at entry, `@xN` seeds included; a simulation Goal is met when every frozen
Test Run has passed, and a failure stays until the code changes. A coverage
Goal is decided by its floors alone, whether or not its Test Runs passed.
`goal_status` lists the frozen Test Runs with a rerun command per failure. A
`--diagnostic` run records no Test Run outcome, frozen or not.

### Booley Flows

<!-- BEGIN GENERATED: flows -->
Deterministic end-to-end orchestration; no LLM:

| Booley Flow | Purpose | Sets |
|--------|---------|------|
| `sim` | Run RTL simulation for one or more Targets | `ELAB:{target}`, `SIM:{target}`, `CYCLE_COUNT:{target}`, `COVERAGE:{target}` |
| `lint` | Run lint for one or more Targets | `LINT:{target}` |
| `synth` | Run ASIC synthesis for one or more Targets with optional baseline comparison | `SYNTH:{target}` |
| `fpga` | Run FPGA implementation for one or more Targets with optional baseline comparison | `FPGA:{target}` |

Common controls: repeat `--target` or use comma-separated values; `--target a --target b,c` preserves the order `a`, `b`, `c`, and duplicate resolved Targets are rejected. MCP keeps one comma-separated `target` string. `--dry-run` returns a normalized plan without executing EDA; `booley flow <name> --help` shows the full contract.

Key Flow-specific controls:

- `sim`: `--mode elab-only` compiles, elaborates, and links without running tests; `--mode elab-only-standalone` adds the stronger module sweep. Repeat `--test <test-run>` for an exact ordered suite or use `--tests-file <path>`; a Test Run is `test`, `test+variant`, `test@seed`, `test+variant@seed`, or `test[+variant]@xN` for N drawn seeds, and `--seed <N>|random` sets or draws the seed of every selected Test Run; there is no CLI `--skip`, and configured skips apply only to unfiltered selection. MCP passes the same suite as a `test` array. Resume one exact durable Simulation Campaign with `--resume-from <exact-manifest.json>` (optionally with `--dry-run`): Cocotb retries the whole batch; a coverage Campaign resumes only after its collection finished (otherwise exit 2: start a new `--coverage` run). `--coverage` / `--cov` collects a native Coverage Campaign, and `--trace` captures waveforms for the simulation run. Focused Cocotb output summarizes unselected skips; pass `--result-verbosity full` to print every XML testcase entry (the complete XML and JSON artifacts are always retained)
- `lint`: `--scope <file,...>` filters reported findings to selected files
- `synth`: `--baseline <ref>` compares metrics against a git revision; physical Targets must own an SDC fileset that creates a clock
- `fpga`: `--baseline <ref>` compares metrics against a git revision; `--ppa-profile compact|balanced|max_frequency` selects portable optimization intent; `--no-cache` forces a fresh implementation
<!-- END GENERATED: flows -->

### Specialists

<!-- BEGIN GENERATED: specialists -->
LLM-powered Specialists running in scoped, isolated workspaces:

Ask your Developer Agent session to invoke a Specialist by name with the arguments below. Or run `booley specialist <name> [args...]` inside the Sandbox. `booley specialist` lists visible Specialists; `booley specialist <name> --help` shows their arguments. `--model`, `--max-turns`, and `--timeout` are CLI-only controls.

For example: `booley specialist reviewer --category rtl --focus bugs --scope rtl`. Common options are `-C/--project PATH`, `--report-dir`, `--diagnostic`, and `--target` where supported. `--timeout DURATION` accepts positive seconds (`90` or `90s`), minutes (`30m`), hours (`2h`), and combinations (`1h30m`). Model-call minimum budgets apply; seconds-only providers round up. CLI Flow and Specialist runs never record Goal evidence; use the Developer Agent's Goal-bound MCP calls with `work_dir`.

| Specialist | Purpose | Sets | Modifies code |
|------------|---------|------|:-------------:|
| `coverage_analyst` | Explain one exact coverage.json Campaign and propose advisory next steps | — | — |
| `mutation_tester` | Proposal-locked mutation testing: creator selects exact replacements, tester builds isolated variants | `MUTATION:{target}` | — |
| `reviewer` | Single-focus code review: reports issues by severity | `REVIEW:rtl_spec`, `REVIEW:rtl_bugs`, `REVIEW:rtl_protocol`, `REVIEW:tb_quality`, `REVIEW:rtl_security`, `REVIEW:rtl_optimization`, `REVIEW:rtl_code_style` | — |

#### `coverage_analyst`

Call the `coverage_analyst` Specialist from your Developer Agent session with `campaign="<exact-coverage.json>"` and optional `instruction="<question>"`. The Analyst explains retained native evidence and proposes advisory next steps; its model only reads evidence. It does not run Simulation, read waveforms, evaluate Criteria, or approve waivers. In Goal Mode, screened Waiver Candidates support a proposed Goal change, which needs human approval. Finish requires every Goal met; advisory reports do not mark strict coverage met. Verified Target sources are optional; stale sources give report-only analysis.

#### `reviewer`

Read-only, single-focus code review. It reports `CRITICAL`, `MAJOR`, and `MINOR` findings. In Interactive Mode, review the selected files using your specification or steering. In Goal Mode, `done` reports findings without requiring fixes; the review package includes open findings. `clean` requires every finding to be fixed or waived with a justification.

Interactive source review accepts Verilog/SystemVerilog (.v, .vh, .sv, .svh), plus Python (.py) and explicitly scoped C/C++ testbench sources and headers (.c, .cc, .cpp, .cxx, .h, .hh, .hpp, .hxx) for category=tb. Goal Mode also accepts other explicitly scoped support files. Sim C/C++ harness inputs count as TB for isolation and cannot be mutation scope. Declared opposite-category sources are refused in either mode; unregistered scoped sources retain the registration warning and receive only an exact per-file category grant.

The result links saved review evidence, including rejected proposals for inspection. Rejected proposals do not affect Criteria.

Call the `reviewer` Specialist from your Developer Agent session with `category="<category>"` and `focus="<focus>"`.

| Category | Focus | What it checks | Sets |
|----------|-------|----------------|------|
| `rtl` | `bugs` | Functional bug patterns, synthesis hazards, reset/width/signing mistakes, and ifdef/config consistency | `REVIEW:rtl_bugs` |
| `rtl` | `protocol` | Bus/protocol rule compliance, handshake behavior, ordering, and clock-domain crossings (CDC) | `REVIEW:rtl_protocol` |
| `rtl` | `spec` | Spec compliance: the RTL implements what the change request/spec requires, no more and no less | `REVIEW:rtl_spec` |
| `rtl` | `code_style` | Comments, naming, readability, maintainability, magic values, and assertion/cover-point quality | `REVIEW:rtl_code_style` |
| `rtl` | `optimization` | Unused/dead RTL and strict power/performance/area improvements with no functional or engineering trade-off | `REVIEW:rtl_optimization` |
| `rtl` | `security` | Fault-injection resistance, simple power/timing leakage, secret exposure, and unsafe failure behavior | `REVIEW:rtl_security` |
| `tb` | `quality` | False-pass paths in scoped testbench sources, missing checks and edge cases, coverage gaps, timing/sampling mistakes, and TB code quality | `REVIEW:tb_quality` |

**Interactive Mode:** also pass `scope="<file,...>"`. Add `spec="<path>"` (required for the `spec` focus) or `steer=["<context>"]` for context. `dry_run=true` previews the call. Each new review replaces the previous findings.

**Goal Mode:** the Review Goal fixes scope and spec, so `scope`, `spec` and `steer` are refused. Declare each Review Goal with exactly one `target` or `paths` (see the Goal catalog).

**Verify fixes (both modes):** after fixing findings, call `reviewer` with `verify=true` (CLI: `--verify`). It rechecks only the latest review's open findings, finds nothing new, and passes when none remain open. To argue a finding away, add `waive=[["<finding-id>", "<justification>"]]` (CLI: `--waive <finding-id> "<justification>"`, repeatable); the Reviewer accepts or rejects each. No other argument is accepted with verify. After repeated failed verifies, Goal Mode stops and asks you to change or abandon the Goal.

#### `mutation_tester`

Proposal-locked mutation testing. A read-only LLM creator returns exact source replacements; Booley runs a pristine baseline, then compiles and tests each replacement in isolation. It does not parse HDL or inject runtime selectors.

**Mutation campaign modes:**

| Campaign | Goal arguments | Interactive Mode arguments |
|----------|-----------------------------------------|------------------------|
| Default fixed | Target campaign with `target` + `scope` — generate 10 mutations and require all 10 detected | _(no goal arguments)_ — the same 10-of-10 campaign |
| Explicit fixed | add `total: N` and `min_detected: K` | `count="N"` requires all N; add `min_detected=K` to require K |
| Size-scaled | add `auto: true` — choose 3-25 mutations from language-neutral source size and the time budget | `count="auto"`; add `min_detected=K` for an explicit threshold |

`dry_run=true` validates Target metadata and prints the source-size breakdown and proposed auto count without invoking a Specialist or simulator.

Call the `mutation_tester` Specialist from your Developer Agent session: `scope="<rtl-file,...>"` chooses mutation sites; `target="<sim-target>"` chooses exactly one complete runnable Target suite; `steer=["<context>"]` biases mutation selection. A valid lock is reused on later runs, so new steering takes effect only with `regen_lock=true`. The Target supplies the testbench top and complete RTL closure; they are not separate caller inputs.
<!-- END GENERATED: specialists -->

Booley validates exact replacements and compiles each in isolation. It emits a
self-contained campaign manifest with durable baseline/per-mutant logs, source
variants, and the first public test that killed each detected mutant.

### Criteria

<!-- BEGIN GENERATED: criteria -->
| Goal | Requirement | Evidence producer |
|------|-------------|-------------------|
| `LINT:{target}` | The Target's linter passes with no unwaived findings | `lint` |
| `SIM:{target}` | RTL simulation passes all tests | `sim` |
| `ELAB:{target}` | RTL/TB compiles and elaborates cleanly (no simulation) | `sim` (`mode="elab-only"`) |
| `SYNTH:{target}` | ASIC synthesis completes within area/timing budgets | `synth` |
| `FPGA:{target}` | FPGA implementation completes within resource/timing budgets | `fpga` |
| `CYCLE_COUNT:{target}` | A named test passes and its observed Cycle Count meets every declared threshold | `sim` |
| `COVERAGE:{target}` | Native Coverage Campaign policy for one Simulation Target | `sim` (`coverage=true`) |
| `MUTATION:{target}` | Mutation testing achieves minimum kill rate | `mutation_tester` |
| `REVIEW:rtl_spec` | RTL review: spec compliance (RTL matches the Goal/spec, no more, no less) | `reviewer` (`category="rtl"`, `focus="spec"`) |
| `REVIEW:rtl_bugs` | RTL review: bug patterns, synthesis hazards, and ifdef/config consistency (the RTL as hardware, not against the spec) | `reviewer` (`category="rtl"`, `focus="bugs"`) |
| `REVIEW:rtl_protocol` | RTL review: bus/protocol compliance and clock-domain crossings (CDC) | `reviewer` (`category="rtl"`, `focus="protocol"`) |
| `REVIEW:rtl_security` | RTL review: hardware attack resistance to fault injection, simple power/timing analysis, and secret exposure (post-sim) | `reviewer` (`category="rtl"`, `focus="security"`) |
| `REVIEW:rtl_optimization` | RTL review: unused/dead code and missed power/performance/area wins, strict improvements only (post-sim) | `reviewer` (`category="rtl"`, `focus="optimization"`) |
| `REVIEW:rtl_code_style` | RTL review: comments, naming, readability, and assertion coverage (post-sim) | `reviewer` (`category="rtl"`, `focus="code_style"`) |
| `REVIEW:tb_quality` | Source-scoped TB review: false-pass detection, coverage gaps, and TB code quality | `reviewer` (`category="tb"`, `focus="quality"`) |

`{target}` is the bare Target name when unique, or its qualified selector otherwise. Details (thresholds, tests, baseline,
review verdict and spec) follow the name in parentheses. Review Goals require
`clean` or `done`; the name is identical for both requirements.
Each Review Goal also names exactly one scope: `target` (that Target's RTL or TB
files plus dependencies) or `paths` (files relative to the Goal worktree). Examples:
`{"family":"REVIEW","review":"rtl_bugs","verdict":"clean","target":"top"}`,
`{"family":"REVIEW","review":"tb_quality","verdict":"done","paths":["tb/top_tb.sv"]}`.

Goal argument `family` uses the uppercase capability before the colon.
Internal evidence keys and Project-defined evidence bindings are not Goal names.
<!-- END GENERATED: criteria -->

**Threshold parameters:**

<!-- BEGIN GENERATED: criteria-params -->
`synth` and `fpga` Goals name each Target directly and accept metric thresholds. Four flavours apply per metric: two absolute, two relative to the Goal base commit:

| Flavour param suffix | Baseline? | Meaning |
|----------------------|:---------:|---------|
| `_max` | no | metric must stay **≤** the given value |
| `_min` | no | metric must stay **≥** the given value |
| `_increase_at_most` | yes | metric may grow **at most N%** above baseline |
| `_reduce_at_least` | yes | metric must shrink **at least N%** below baseline |

Percentage threshold values must include the `%` suffix (for example, `"cell_count_reduce_at_least": "8%"` inside `thresholds`).

For a relative threshold on a new Target, set `baseline` to an existing Target. The baseline Target defaults to the candidate name and must exist at the Goal base commit. Booley runs it on base code and the candidate on current code. Missing or mismatched baseline evidence fails the check.

Goal argument examples:

```json
{"family": "SYNTH", "target": "asic_small", "baseline": "asic_base",
 "thresholds": {"cell_count_reduce_at_least": "8%"}}
```

```json
{"family": "FPGA", "target": "fpga_top", "thresholds": {"lut_count_max": 5000}}
```

**`SYNTH:{target}` (ASIC)**

| Metric | _max | _min | _increase_at_most | _reduce_at_least |
|--------|:---:|:---:|:---:|:---:|
| `area` | — | — | ✓ | ✓ |
| `area_kge` | ✓ | — | — | — |
| `area_um2` | ✓ | — | — | — |
| `cell_count` | ✓ | — | ✓ | ✓ |
| `critical_path_ps` | ✓ | — | ✓ | ✓ |
| `fmax_mhz` | — | ✓ | ✓ | ✓ |
| `wire_count` | ✓ | — | ✓ | ✓ |

> Absolute area caps pick a unit (`area_um2` / `area_kge`); the unit-agnostic `area` row carries the baseline-relative bounds only.

> Mutually exclusive: `area_um2_max` ⊕ `area_kge_max`.

> Mutually exclusive: `critical_path_ps_max` ⊕ `fmax_mhz_min`.

**`FPGA:{target}` (FPGA)**

| Metric | _max | _min | _increase_at_most | _reduce_at_least |
|--------|:---:|:---:|:---:|:---:|
| `bram_count` | ✓ | — | ✓ | ✓ |
| `critical_path_ps` | ✓ | — | ✓ | ✓ |
| `dsp_count` | ✓ | — | ✓ | ✓ |
| `ff_count` | ✓ | — | ✓ | ✓ |
| `fmax_mhz` | — | ✓ | — | — |
| `lut_count` | ✓ | — | ✓ | ✓ |

> Mutually exclusive: `critical_path_ps_max` ⊕ `fmax_mhz_min`.

**`CYCLE_COUNT` of one Test Run**

`CYCLE_COUNT:{target}` Goals name the Target with `target` and one Test Run with `test` (`test`, `test+variant`, or either with `@seed`); put bounds in `thresholds`. All thresholds for that Test Run must pass. Relative forms compare the same Test Run of the same Target at the Goal base commit.

```json
{"family": "CYCLE_COUNT", "target": "sim", "test": "smoke",
 "thresholds": {"cycle_count_max": 1000, "cycle_count_reduce_at_least": "8%"}}
```

| Parameter | Baseline? | Unit | Passing relation |
|-----------|:---------:|------|------------------|
| `cycle_count_max` | no | cycles | current ≤ threshold |
| `cycle_count_min` | no | cycles | current ≥ threshold |
| `cycle_count_increase_at_least` | yes | percent | signed change ≥ +N% |
| `cycle_count_increase_at_most` | yes | percent | signed change ≤ +N% |
| `cycle_count_reduce_at_least` | yes | percent | signed change ≤ -N% |
| `cycle_count_reduce_at_most` | yes | percent | signed change ≥ -N% |
| `cycle_count_increase_at_least_cycles` | yes | cycles | current - baseline ≥ N |
| `cycle_count_increase_at_most_cycles` | yes | cycles | current - baseline ≤ N |
| `cycle_count_reduce_at_least_cycles` | yes | cycles | baseline - current ≥ N |
| `cycle_count_reduce_at_most_cycles` | yes | cycles | baseline - current ≤ N |

A named `[SIM_CYCLES] <test> <count>` observation is gated evidence only when that exact test passes. Missing, malformed, duplicate, unnamed, failed, or inconclusive evidence fails closed. Without a `CYCLE_COUNT:{target}` Goal, existing Cycle Count records remain observational.

Relative comparisons report an **observed Cycle Count change**. When declared workload inputs differ, review reports disclose the changes and do not attribute the result to RTL alone.
<!-- END GENERATED: criteria-params -->

**Per-clock timing thresholds.** The timing metrics
(`critical_path_ps`, `fmax_mhz`, `wns_ns`, `whs_ns`, `period_ns`) are reported
per clock and their thresholds may be flat or clock-scoped (area/cell/LUT/FF/
BRAM/DSP/utilization are not clock-scopable):

- Flat `fmax_mhz_min: 400` → **every** clock's Fmax ≥ 400 (gates the worst clock).
- Clock-scoped `clk_i.fmax_mhz_min: 400` → only clock `clk_i`.

`critical_path_ps_max` ⊕ `fmax_mhz_min` is mutually exclusive **per clock**.
Example: `{"family": "SYNTH", "target": "synth_core", "thresholds": {"clk_i.fmax_mhz_min": 400, "clk_2x.critical_path_ps_max": 5000}}`.

### Targets

`--target` values are FuseSoC `.core` Target names; when a bare name is declared by more than one core, qualify it as `vlnv#name`. `booley targets` shows them all:

| Command | What it does |
|---------|-------------|
| `booley targets` | List every `.core` Target: drivable Booley Flows, EDA tool, toplevel, and `✓` Doctor selection |
| `booley targets --for sim` | Only Targets that Booley Flow could drive (any target-aware Booley Flow) |
| `booley targets 'sim_*'` | Glob filter over bare name or `vendor:lib:name#target` |
| `booley targets <name>` | Resolved detail: parameters, file counts, SDC/XDC (runs `fusesoc`, container-side) |

`--json` composes with all of the above; Developer Agents get the same listing via the `booley_targets` MCP tool.

Booley-authored Targets are named `<axis>_<subject>`: axis token for the driving Booley Flow (`sim_`, `lint_`, `synth_`, `fpga_`), then a subject that distinguishes the Target from others, coarse to fine: `sim_smoke`, `synth_timing`. The axis leads because nothing else distinguishes a synth Target from an FPGA Target (CAPI2 has no synth flow). `booley doctor` warns on names that don't, and on a `default:` Target in a core nothing `depend:`s on; vendored upstream cores are exempt.

For FPGA Targets the `fpga` axis, not `flow_options.tool`, determines drivability. The declared tool still controls FuseSoC resolution conditions; the FPGA Flow always rebuilds those resolved inputs into a Vivado EDAM. Unprefixed vendored Targets retain a `tool: vivado` fallback.

### Project Files

Recognized user-owned inputs under `.booley/` are listed below.
Generated runtime state, reports, logs, worktrees, and managed marker files are
omitted. FuseSoC `.core` design files also stay outside this directory, beside
the design sources they describe.

#### Basic project files

| File | Information | Affects |
|------|-------------|---------|
| `booley.toml` | Project, Flow, provider, Sandbox, and Job policy | All Flows and Specialists |
| `tests.toml` | Per-Target Tests, Test Variants, seeds, skips, environment | `sim`, `mutation_tester` |
| `goalsets/*.md` | Project-owned Goal bundles and optional default | `/booley-goal` |
| `doctor-waivers.toml` | Reviewed warning waivers and expiry | No endpoint; `doctor` only |
| `AGENTS.md` | Project instructions, ownership, and gotchas | Developer Agent and Specialists |
| `rtl_style_guide.md` | Project RTL style overrides | `reviewer` RTL code-style focus |
| `tb_style_guide.md` | Project testbench style overrides | `reviewer` TB quality focus |
| `docker/Dockerfile` | Project image build steps and dependencies | Flows/Specialists in the Sandbox |
| `<requirements>.txt` | Python dependency pins selected by `booley.toml` | Flows/Specialists in the Sandbox |
| `hooks/post-setup.*` | Per-worktree setup commands | Worktree setup |

#### Custom tool files

Only needed when adding custom Flows, Specialists, or host tools. Most projects
do not need these files.

| File | Information | Affects |
|------|-------------|---------|
| `criteria.toml` | Criteria defined for custom tools | Their producer Flows/Specialists |
| `mcp_tools/*.py` | Custom Flow, Specialist, or MCP definitions | The defined in-Sandbox endpoints |

### Skills

| Skill | Use it when | Result |
|-------|-------------|--------|
| `/booley-setup` | Set up an initialized Project; start on the host | `SETUP-PLAN.md`; configured Project; Doctor audit |
| `/booley-goal` | Work needs mandatory evidence-backed Goals | Goals, evidence, and review |
| `/booley-heal` | Doctor or Flow health has drifted | Repair safe findings; verify Doctor |
| `/booley-feedback` | Report bugs, friction, praise, or ideas | Redact evidence; export for manual sharing |

### Artifacts

`<GOAL>` means `<booley_dir>/goals/<goal-id>/`, the local Goal Record.

| Artifact | Path | Use |
|----------|------|-----|
| Goal state and base | `<GOAL>/record.json` | Worktree, branch, Goals, and lifecycle |
| Session Summary | `<GOAL>/SUMMARY.md` | What changed, evidence used, uncertainties |
| Review Package | `<GOAL>/review-package.json` | Diff, final evidence, Change Log, and review findings |
| Approved Goal changes | `<GOAL>/changes.jsonl` | Human decisions and reasons |
| Evidence logs | `<GOAL>/logs/` | Flow/Specialist evidence and retained reports |
| Summary on the Goal Branch | `.booley/goals/history/<goal-id>.md` | Committed outside Stealth when Git-trackable and owned by the RTL repository; Paired Checkout summaries stay local |

### Sandbox & Docker

The `booley-sandbox` image contains Booley's EDA toolchain, Developer Agent clients, and development dependencies. It backs the per-folder Sandbox (devcontainer) where all Booley work, Interactive Mode and Goal Mode alike, executes; a project image selected by `[sandbox].image` can extend it.

| Command | What it does |
|---------|-------------|
| `booley init` | Set up project + pull or build the image |
| `booley init --force` | Refresh Booley-managed resources; keep caches and user files |
| `booley doctor` | Verify image + container EDA tools |
| `booley session up` | Start the Sandbox headlessly (no VS Code) |
| `booley session enter [-- cmd]` | Shell into it, or run one command |
| `booley session down` | Stop and remove it |
| `booley` / `booley chat` | Launch the configured `claude` or `codex` command inside the Sandbox |
| `docker pull ghcr.io/boldaxolotl/booley-sandbox:<ver>` | Manual pull of pre-built image |

To extend the image, create `.booley/docker/Dockerfile` with `FROM booley-sandbox`, build it, then set `[sandbox].image` in `.booley/booley.toml`.

Inside the Sandbox, `booley` / `booley chat` is a convenience alias for the selected Developer Agent command:
it replaces itself with the native CLI. Arguments after `booley chat` are passed to it unchanged, for example `booley chat --resume <id>` (Claude) or `booley chat resume --last` (Codex); `booley chat -- --help` forwards `--help`.

**`booley chat`, `booley goal`, and `booley dashboard` run in the Sandbox.**
Bare `booley` prints help on the host and starts the configured Developer Agent in the
Sandbox. For parallel Goal work, use one linked worktree per session. The
Dashboard automatic opening defaults on; set `[sandbox].dashboard=false` to opt out.
`booley init` and `booley session` run on the host; Doctor works on either side.
