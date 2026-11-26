## Booley: Quick Reference

### Commands

Every public top-level command; run `booley <command> --help` for its options
and nested actions.

#### Sandbox-only commands

| Command | Purpose |
|---------|---------|
| `booley dashboard` | Show sessions, Jobs, and Targets |
| `booley flow` | List or directly run deterministic Booley Flows |
| `booley measure` | Measure a commit against `metrics.toml`; `--validate` checks the file |
| `booley specialist` | List or directly run visible Specialists |
| `booley worktree` | Prepare a linked worktree made with Git or T3 Code (Paired Checkout or Snapshot) |

#### Either-location commands

These commands work on the host and inside the Sandbox. Full Target resolution
requires the Sandbox.

| Command | Location | Purpose |
|---------|----------|---------|
| `booley` | either | Show top-level help; on the host, add a getting-started hint |
| `booley cheat` | either | List sections; use section flags or `--all` to print the reference |
| `booley coverage` | either | List uncovered coverage points of a finished coverage run |
| `booley doctor` | either | Check project, Sandbox, and toolchain health |
| `booley help` | either | Show top-level help, same as `booley --help` |
| `booley init` | either | Initialize the Project; scaffold, reseed, and image work are host-only |
| `booley resources` | either | Show measured peak memory and run time per Target and Job phase |
| `booley targets` | either | List or filter Targets; full detail resolution requires the Sandbox |

#### Host-only commands

| Command | Purpose |
|---------|---------|
| `booley auth` | Configure or inspect Developer Agent and Specialist credentials |
| `booley bootstrap` | Check or reconcile reusable host resources |
| `booley eda` | Manage host commercial-EDA installations, grants, and licenses |
| `booley projects` | List remembered Project paths and their Grants |

The [Skills](#skills) are guided workflows you start in a Developer Agent chat,
such as feedback,
version review, Project Setup cleanup, and Sandbox lifecycle management.

For Host Bootstrap, `--check-only` reports pending work without writing and
`--force` refreshes Booley-managed resources while preserving caches and
user-owned files. Force does not overwrite configuration or disable Docker's
layer cache.

### Metric-Driven Development

Metric-Driven Development accepts work by measuring a commit against the Target Metrics in `.booley/metrics.toml`. Commit the work, then run `booley measure`: it measures only committed work, prints a report, and exits 0 when every enforced bound is met, 1 when one is unmet, and 2 on an error or when it cannot run. A Measured Commit is one whose full `booley measure` run met every enforced bound.

These rules apply to you whether you type the commands or a Developer Agent runs them for you:

- Measure only when a Measured Commit is asked for.
- Iterate with plain `booley flow`, not with `booley measure`.
- Measure the base at the start of a piece of work (`booley measure --all <base>`) only when the work has a relative target or a comparison is wanted.
- Exit 1: fix, commit and measure again, up to 3 attempts unless the request states another number.
- Exit 2: stop and report the error.
- Never edit `metrics.toml` or `tests.toml` to get a met verdict, and never add tables, without approval.
- The work is complete when the commit is Measured with a green header.

`metrics.toml` holds one `[<target>.<flow>]` table per Target and Flow. A `<metric>_max` or `<metric>_min` key is an enforced bound, `track` lists metrics that are only reported, and `off` removes a bound that `[defaults.<flow>]` would add. `[defaults.<flow>]` adds bounds to every table of that Flow.

The listing `booley cheat --metrics` prints below is `metrics.toml` text: add the blocks you keep to `.booley/metrics.toml`, give each bound you keep a number, delete the rest, and check the file with `booley measure --validate`.

<!-- BEGIN GENERATED: metrics -->
Run `booley cheat --metrics` in a Project to list its Targets' metrics.
<!-- END GENERATED: metrics -->

### Booley Flows

<!-- BEGIN GENERATED: flows -->
Deterministic end-to-end orchestration; no LLM:

| Booley Flow | Purpose |
|--------|---------|
| `sim` | Run RTL simulation for one or more Targets |
| `lint` | Run lint for one or more Targets |
| `synth` | Run ASIC synthesis for one or more Targets with optional baseline comparison |
| `fpga` | Run FPGA implementation for one or more Targets with optional baseline comparison |
| `mutation` | Deterministic mutation testing: seeded Mutants from fixed Mutation Operators, each built and tested in isolation |

Common controls: repeat `--target` or use comma-separated values; `--target a --target b,c` preserves the order `a`, `b`, `c`, and duplicate resolved Targets are rejected. A Developer Agent's MCP tool call takes one comma-separated `target` string instead. `--dry-run` returns a normalized plan without executing EDA; `booley flow <name> --help` shows the full contract.

Key Flow-specific controls:

- `sim`: `--mode elab-only` compiles, elaborates, and links without running tests; `--mode elab-only-standalone` adds the stronger module sweep. Repeat `--test <test-run>` for an exact ordered suite or use `--tests-file <path>`; a Test Run is `test`, `test+variant`, `test@seed`, `test+variant@seed`, or `test[+variant]@xN` for N drawn seeds, and `--seed <N>|random` sets or draws the seed of every selected Test Run; there is no CLI `--skip`, and configured skips apply only to unfiltered selection. A Developer Agent's MCP tool call passes the same suite as a `test` array. Resume one exact durable Simulation Record with `--resume-from <exact-manifest.json>` (optionally with `--dry-run`): Cocotb retries the whole batch; a coverage Simulation Record resumes only after its collection finished (otherwise exit 2: start a new `--coverage` run). `--coverage` / `--cov` collects a native Coverage Record, and `--trace` captures waveforms for the simulation run. Focused Cocotb output summarizes unselected skips; the complete XML and JSON results are always retained
- `lint`: `--scope <file,...>` filters reported findings to selected files
- `synth`: `--baseline <ref>` compares metrics against a git revision; physical Targets must own an SDC fileset that creates a clock
- `fpga`: `--baseline <ref>` compares metrics against a git revision; `--ppa-profile compact|balanced|max_frequency` selects portable optimization intent; `--no-cache` forces a fresh implementation
- `mutation`: `--target` names exactly one Simulation Target whose complete suite judges each Mutant; `--scope <rtl-file,...>` chooses mutation sites (default: the Target's own RTL, minus testbench, vendored, and generated files); `--count N|auto` sets the Mutant count (default 10; `auto` scales 3-25 from source size and the time budget) and `--min-detected K` the pass threshold (default ceil(0.8 x count), of the Mutants scored when fewer run); `--seed <N>` (default 0) picks another reproducible Mutant set; `--dry-run` previews the count without running a simulator. A Mutant that fails to build is dropped and the next candidate takes its place; a timed-out Mutant counts as `detected (timeout)`; the default is 10 Mutants with 8 detected
<!-- END GENERATED: flows -->

With several Jobs running, the Developer Agent calls `booley_poll` with no
`run_id` and handles whichever of its Jobs finishes first, passing a `run_id`
only to wait for one specific Job; it never waits with `sleep` loops or log
tails.

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

### Specialists

<!-- BEGIN GENERATED: specialists -->
Specialists running in scoped, isolated workspaces:

Ask your Developer Agent session to invoke a Specialist by name with the arguments below (written as it passes them, `name="value"`; the CLI spells each as a flag, `--name value`). Or run `booley specialist <name> [args...]` inside the Sandbox. `booley specialist` lists visible Specialists; `booley specialist <name> --help` shows their arguments. `--max-turns` and `--timeout` are CLI-only controls.

For example: `booley specialist reviewer --category rtl --focus bugs --scope rtl`. Common options are `-C/--project PATH`, `--report-dir`, and `--target` where supported. `--timeout DURATION` accepts positive seconds (`90` or `90s`), minutes (`30m`), hours (`2h`), and combinations (`1h30m`). Model-call minimum budgets apply; seconds-only providers round up. A CLI run is a Standalone Job: it shows in the Dashboard and copies its output to `job_logs/standalone/<run_id>/job.log` in the Project's common Booley Directory (the main checkout's `.booley` from a linked worktree).

| Specialist | Purpose | Modifies code |
|------------|---------|:-------------:|
| `coverage_analyst` | Explain one exact coverage.json Coverage Record and propose advisory next steps | — |
| `reviewer` | Single-focus code review: reports issues by severity | — |

#### `coverage_analyst`

Ask your Developer Agent to call the `coverage_analyst` Specialist with `record="<exact-coverage.json>"` and optional `instruction="<question>"`. The Analyst explains retained native evidence and proposes advisory next steps; its model only reads evidence. It does not run Simulation, read waveforms, evaluate Criteria, or approve waivers. Screened Waiver Candidates are advisory and need human approval; advisory reports do not mark strict coverage met. Verified Target sources are optional; stale sources give report-only analysis.

#### `reviewer`

Read-only, single-focus code review. It reports `CRITICAL`, `MAJOR`, and `MINOR` findings. It reviews the selected files using your specification or steering.

Interactive source review accepts Verilog/SystemVerilog (.v, .vh, .sv, .svh), plus Python (.py) and explicitly scoped C/C++ testbench sources and headers (.c, .cc, .cpp, .cxx, .h, .hh, .hpp, .hxx) for category=tb. Sim C/C++ harness inputs count as TB for isolation and cannot be mutation scope. Declared opposite-category sources are refused; unregistered scoped sources retain the registration warning and receive only an exact per-file category grant. Each Reviewer has the opposite category's sources and HDL files no .core fileset declares hidden; all other project files, such as firmware, are readable context, and findings outside the scope are filtered.

The result links saved review evidence, including rejected proposals for inspection. Rejected proposals do not affect Criteria.

Ask your Developer Agent to call the `reviewer` Specialist with `category="<category>"` and `focus="<focus>"`.

| Category | Focus | What it checks |
|----------|-------|----------------|
| `rtl` | `bugs` | Functional bug patterns, synthesis hazards, reset/overflow/signing mistakes, and ifdef consistency across built Targets |
| `rtl` | `protocol` | Bus/protocol rule compliance, handshake behavior, ordering, and clock-domain crossings (CDC) |
| `rtl` | `spec` | Spec compliance: the RTL implements what the change request/spec requires, no more and no less |
| `rtl` | `code_style` | Comments, naming, readability, maintainability, magic values, and assertion/cover-point quality |
| `rtl` | `optimization` | Functionally dead RTL and strict power/performance/area improvements with no functional or engineering trade-off |
| `rtl` | `security` | Fault-injection resistance, simple power/timing leakage, secret exposure, and unsafe failure behavior |
| `tb` | `quality` | False-pass paths in scoped testbench sources, missing checks and edge cases, coverage gaps, timing/sampling mistakes, and TB code quality |

Also pass `scope="<file,...>"`. Add `spec="<path>"` (required for the `spec` focus) or `spec_text="<text>"` (CLI: `--spec-text`; not with `spec`), or `steer=["<context>"]` for context. A completed review exits 0; pass `fail_on="critical|major|minor"` (CLI: `--fail-on`) to exit 1 when an open finding is at or above that severity. `dry_run=true` previews the call. Each new review replaces the previous findings.

**Changes only:** add `base="<commit>"` (CLI: `--base <commit>`) to review only the `scope` files changed since the merge-base of that commit and HEAD, uncommitted changes included. Unchanged `scope` files that use them are context, the diff and commit messages are given to the Reviewer, and no change in scope exits 0 without a review. Pass every file of the category as the `scope` to give the Reviewer the files that use the changed ones. Findings already present before the change are listed separately as pre-existing and are never open.

**Re-check:** after fixing findings, pass `recheck="<evidence file>"` (CLI: `--recheck <evidence file>`), the `Reviewer evidence:` path of the earlier review or re-check, instead of running a new review. Run one full review, then re-check until its findings are fixed; run another full review only on purpose. Each open finding becomes `fixed`, `still open`, or `can't tell`, and no new finding is reported. `fixed` needs current code that shows it. Category, focus, and scope come from the file, so `scope`, `base`, `category`, `focus`, `spec`, `spec_text`, and `steer` are refused. With `fail_on`, only `still open` and `can't tell` findings count. To leave a finding as it is, pass `waive=[["<finding-id>", "<reason>"]]` (CLI: `--waive <finding-id> "<reason>"`) with `recheck`; the Reviewer accepts or refuses each proposal, and an accepted `waived` finding never counts for `fail_on`.
<!-- END GENERATED: specialists -->

Booley validates exact replacements and compiles each in isolation. It emits a
self-contained campaign manifest with durable baseline/per-mutant logs, source
variants, and the first public test that killed each detected mutant.

### Targets

`--target` values are FuseSoC `.core` Target names; when a bare name is declared by more than one core, qualify it as `vlnv#name`. `booley targets` shows them all:

| Command | What it does |
|---------|-------------|
| `booley targets` | List every `.core` Target: drivable Booley Flows, the EDA tools it runs (e.g. `sv2v+yosys+openroad` for a physical synth Target, `yosys` for a logical one with the `slang` frontend), toplevel, and `✓` Doctor selection |
| `booley targets --for sim` | Only Targets that Booley Flow could drive (any target-aware Booley Flow) |
| `booley targets 'sim_*'` | Glob filter over bare name or `vendor:lib:name#target` |
| `booley targets <name>` | Detail: synth mode and frontend (synth Targets), parameters, file counts, SDC/XDC (the last three run `fusesoc`, container-side) |

`--json` composes with all of the above and reports each Target's `synth_mode`,
`frontend`, and `eda_tools`. Developer Agents get the same listing via the
`booley_targets` MCP tool.

Booley-authored Targets are named `<axis>_<subject>`: axis token for the driving
Booley Flow (`sim_`, `lint_`, `synth_`, `fpga_`), then a subject that
distinguishes the Target from others, coarse to fine: `sim_smoke`,
`synth_timing`. The axis leads because nothing else distinguishes a synth Target
from an FPGA Target (CAPI2 has no synth flow). Vendored upstream cores keep
their own names. Add a `default:` Target only to a core another core `depend:`s
on.

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
| `tests.toml` | Per-Target Tests, Test Variants, seeds, skips, environment | `sim`, `mutation` |
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
| `mcp_tools/*.py` | Custom Flow, Specialist, or MCP definitions | The defined in-Sandbox endpoints |

### Skills

| Skill | Use it when | Result |
|-------|-------------|--------|
| `/booley-setup` | Set up an initialized Project; start on the host | `SETUP-PLAN.md`; configured Project; Doctor audit |
| `/booley-heal` | Doctor or Flow health has drifted | Repair safe findings; verify Doctor |
| `/booley-feedback` | Report bugs, friction, praise, or ideas | Redact evidence; export for manual sharing |

### Sandbox & Docker

The `booley-sandbox` image contains Booley's EDA toolchain, Developer Agent
clients, and development dependencies. It backs the per-folder Sandbox
(devcontainer) where all Booley work executes; a project image selected by
`[sandbox].image` can extend it.

| Command | What it does |
|---------|-------------|
| `booley init` | Set up project; on the host also pull or build the image |
| `booley init --force` | Refresh Booley-managed resources; keep caches and user files |
| `booley doctor` | Verify image + container EDA tools |
| `booley session up` | Start the Sandbox headlessly (no VS Code) |
| `booley session enter [-- cmd]` | Shell into it, or run one command |
| `booley session down` | Stop and remove it |
| `docker pull ghcr.io/boldaxolotl/booley-sandbox:<ver>` | Manual pull of pre-built image |

Offline copies of the user docs that skills cite (config, Flows, troubleshooting, Sandbox) are in `/booley-docs/<FILE>.md`.

To extend the image, create `.booley/docker/Dockerfile` with `FROM booley-sandbox`, build it, then set `[sandbox].image` in `.booley/booley.toml`.

**`booley dashboard` runs in the Sandbox.**
For parallel work, use one linked worktree per session.
`booley session` runs on the host. `booley init` runs on the host or in a hand-written Sandbox (Project Initialization only); a Booley-issued Sandbox refuses it. Doctor works on either side.
