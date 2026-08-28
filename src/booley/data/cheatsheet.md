## Booley: Quick Reference

### Commands

Every public top-level command; run `booley <command> --help` for its options
and nested actions.

| Command | Purpose |
|---------|---------|
| `booley init` | Initialize, scaffold, or reseed project integration |
| `booley doctor` | Check project, runtime, and toolchain health |
| `booley auth` | Configure or inspect agent credentials |
| `booley eda` | Manage host commercial-EDA installations, grants, and licenses |
| `booley session` | Start, enter, inspect, refresh, or stop the Session Runtime |
| `booley targets` | List or filter Targets and show resolved details |
| `booley flow` | List or directly run deterministic Booley Flows |
| `booley run` | Execute queued or named tickets |
| `booley board` | Create, inspect, move, reset, or archive tickets |
| `booley cheat` | Show this reference, whole or by section |

### Ticket Board

One Ticket keeps one branch, worktree, and evidence history.

| Path | Meaning |
|------|---------|
| `draft → queued → running → review → done` | Normal lifecycle |
| `draft → waiting → queued` | Wait for dependency Tickets |
| `running → blocked → queued` | Human input, then resume the same Ticket |
| `running → queued` | Exceptional interruption recovery; wait for active jobs first |
| `running → done` | Explicit `on_success.destination: done` shortcut |
| `review → archived` | Close this Ticket; use a new Ticket for separate follow-up |
| `review ──full reset──► queued` | Retire worktree/branch; archive artifacts; clear active state |

Review is a decision point, not a partial-rework loop. Fix small findings
directly in the existing Ticket worktree and finish as `done`; archive it and
create a new Ticket; or reset the entire run. Ordinary `review → queued` is
invalid—the explicit reset is a clean start, never a resume of reviewed work.

Inspect with `booley board show`; handle blocked and review decisions with
`/booley-ticket-triage`.

When `booley run` moves a Ticket to review, it prints a `BOOLEY_RUN_RESULT `
JSON record containing the machine-readable review-package path and optional
HTML path. One record is emitted per review-bound Ticket.

### Booley Flows

<!-- BEGIN GENERATED: flows -->
Deterministic end-to-end orchestration; no LLM:

| Booley Flow | Purpose | Sets |
|--------|---------|------|
| `elab` | Compile + elaborate RTL/TB for one or more Targets (no simulation) | `elab*` |
| `sim` | Run RTL simulation for one or more Targets | — |
| `lint` | Run lint for one or more Targets | `lint_clean` |
| `synth` | Run ASIC synthesis for one or more Targets with optional baseline comparison | `synthesis_ok` |
| `fpga` | Run FPGA implementation for one or more Targets with optional baseline comparison | `fpga_impl_ok` |

Common controls: `--target <name,...>` selects Target(s); `--dry-run` prints commands without executing them; `booley flow <name> --help` shows the full contract.

Key Flow-specific controls:

- `elab`: `--standalone` also proves every RTL module elaborates from its declaring file
- `sim`: `--test <name>` selects a test, `--skip <name,...>` excludes tests, and `--trace` captures waveforms for the simulation run. Focused Cocotb output summarizes unselected skips; pass `--result-verbosity full` to print every XML testcase entry (the complete XML and JSON artifacts are always retained)
- `lint`: `--scope <file,...>` filters reported findings to selected files
- `synth`: `--baseline <ref>` compares metrics against a git revision; `--default-clock <ps>` explicitly supplies a clock only when the Target has no SDC
- `fpga`: `--baseline <ref>` compares metrics against a git revision; `--no-cache` forces a fresh implementation
<!-- END GENERATED: flows -->

### Specialists

<!-- BEGIN GENERATED: specialists -->
LLM-backed sub-agents running in scoped, isolated workspaces:

| Specialist | Purpose | Sets | Modifies code |
|------------|---------|------|:-------------:|
| `mutation_tester` | Proposal-locked mutation testing: creator selects exact replacements, tester builds isolated variants | `mutation_score` | — |
| `reviewer` | Single-focus code review: reports issues by severity | `review_*` | — |

#### `reviewer`

Read-only, single-focus code review. It reports `CRITICAL`, `MAJOR`, and `MINOR` findings. A terminal `_done` review reports findings without triggering fixes; `_clean` requires every finding to be verified fixed or explicitly waived with user-visible justification.
Call `reviewer --scope <file,...> --category <category> --focus <focus>`.

| Category | Focus | What it checks | Sets |
|----------|-------|----------------|------|
| `rtl` | `bugs` | Functional bug patterns, synthesis hazards, reset/width/signing mistakes, and ifdef/config consistency | `review_rtl_bugs` |
| `rtl` | `protocol` | Bus/protocol rule compliance, handshake behavior, ordering, and clock-domain crossings (CDC) | `review_rtl_protocol` |
| `rtl` | `spec` | Spec compliance: the RTL implements what the ticket/spec requires, no more and no less | `review_rtl_spec` |
| `rtl` | `code_style` | Comments, naming, readability, maintainability, magic values, and assertion/cover-point quality | `review_rtl_code_style` |
| `rtl` | `optimization` | Unused/dead RTL and strict power/performance/area improvements with no functional or engineering trade-off | `review_rtl_optimization` |
| `rtl` | `security` | Fault-injection resistance, simple power/timing leakage, secret exposure, and unsafe failure behavior | `review_rtl_security` |
| `tb` | `quality` | False-pass paths, missing checks and edge cases, coverage gaps, timing/sampling mistakes, and TB code quality | `review_tb_quality` |

Controls: `--scope <file,...>` selects files; `--diff-ref <git-ref>` reviews only the diff; repeatable `--steer` adds review context. The `spec` focus needs the ticket/spec text: Ticket Mode resolves it automatically, while Interactive Mode uses `--ticket <path>`.

#### `mutation_tester`

Proposal-locked mutation testing. A read-only LLM creator returns exact source replacements; Booley runs a pristine baseline, then compiles and tests each replacement in isolation. It does not parse HDL or inject runtime selectors.

**Mutation campaign modes:**

| Campaign | Ticket Mode (`mandatory` or `optional`) | Standalone CLI options |
|----------|-----------------------------------------|------------------------|
| Default fixed | Target campaign with `target` + `scope` — generate 10 mutations and require all 10 detected | _(no goal options)_ — the same 10-of-10 campaign |
| Explicit fixed | add `total: N` and `min_detected: K` | `--count N` requires all N; add `--min-detected K` to require K |
| Size-scaled | add `auto: true` — choose 3-25 mutations from language-neutral source size and the time budget | `--count auto`; add `--min-detected K` for an explicit threshold |

Standalone `--dry-run` prints the source-size breakdown and proposed auto count without running mutations.

Targeting and reuse: `--scope <rtl-file,...>` chooses mutation sites; `--target <sim-target>` chooses the complete runnable Target suite; `--steer <context>` biases mutation selection. A valid lock is reused on later runs, so new steering takes effect only with `--regen-lock`. Standalone calls can supply `--dut-files`, `--dut-top` as a prompt hint, and `--tb-top` for classic simulator Targets.
<!-- END GENERATED: specialists -->

Booley validates exact replacements and compiles each in isolation. It emits a
self-contained campaign manifest with durable baseline/per-mutant logs, source
variants, and the first public test that killed each detected mutant.

### Criteria

<!-- BEGIN GENERATED: criteria -->
#### Build & Elaborate

| Criterion | Description | Set by | Workflow Region |
|-----------|-------------|--------|-------|
| `elab_pass_{target}` | RTL/TB compiles and elaborates cleanly (no simulation) | `elab` | pre-sim |
| `elaborate_standalone` | Every module in the Targets' RTL source scope elaborates standalone from its declaring file (shared package/interface files auto-included, parameter defaults) | `elab --standalone` | pre-sim |
| `lint_clean_{target}` | The Target's linter passes with no unwaived findings | `lint` | pre-sim |

#### RTL Code Review

| Criterion | Description | Set by | Workflow Region |
|-----------|-------------|--------|-------|
| `review_rtl_bugs` | RTL review: bug patterns, synthesis hazards, and ifdef/config consistency (the RTL as hardware, not against the spec) | `reviewer --category rtl --focus bugs` | pre-sim |
| `review_rtl_protocol` | RTL review: bus/protocol compliance and clock-domain crossings (CDC) | `reviewer --category rtl --focus protocol` | pre-sim |
| `review_rtl_spec` | RTL review: spec compliance (RTL matches the ticket/spec, no more, no less) | `reviewer --category rtl --focus spec` | pre-sim |
| `review_rtl_code_style` | RTL review: comments, naming, readability, and assertion coverage (post-sim) | `reviewer --category rtl --focus code_style` | post-sim |
| `review_rtl_optimization` | RTL review: unused/dead code and missed power/performance/area wins, strict improvements only (post-sim) | `reviewer --category rtl --focus optimization` | post-sim |
| `review_rtl_security` | RTL review: hardware attack resistance to fault injection, simple power/timing analysis, and secret exposure (post-sim) | `reviewer --category rtl --focus security` | post-sim |

#### Testbench Review

| Criterion | Description | Set by | Workflow Region |
|-----------|-------------|--------|-------|
| `review_tb_quality` | TB review: false-pass detection, coverage gaps, and TB code quality | `reviewer --category tb --focus quality` | pre-sim |

#### Simulation

| Criterion | Description | Set by | Workflow Region |
|-----------|-------------|--------|-------|
| `cycle_count_{target,test}` | A named test passes and its observed Cycle Count meets every declared threshold | `sim` | sim loop |
| `sim_pass_{target}` | RTL simulation passes all tests | `sim` | sim loop |

#### Verification Quality

| Criterion | Description | Set by | Workflow Region |
|-----------|-------------|--------|-------|
| `mutation_score_{target}` | Mutation testing achieves minimum kill rate | `mutation_tester` | post-sim |

#### Implementation & PPA

| Criterion | Description | Set by | Workflow Region |
|-----------|-------------|--------|-------|
| `fpga_impl_ok_{target}` | FPGA implementation completes within resource/timing budgets | `fpga` | post-sim |
| `synthesis_ok_{target}` | ASIC synthesis completes within area/timing budgets | `synth` | post-sim |
<!-- END GENERATED: criteria -->

**Threshold parameters:**

<!-- BEGIN GENERATED: criteria-params -->
Per-target `synthesis_ok` / `fpga_impl_ok` criteria accept optional threshold **params**. Each takes a `targets:` list, the per-target scoping key naming which project Targets to check (the key is `targets`, never `configs`), plus one or more metric params. Four flavours per metric: two absolute, two relative to the ticket's `base_sha` baseline:

| Flavour param suffix | Baseline? | Meaning |
|----------------------|:---------:|---------|
| `_max` | no | metric must stay **≤** the given value |
| `_min` | no | metric must stay **≥** the given value |
| `_increase_at_most` | yes | metric may grow **at most N%** above baseline |
| `_reduce_at_least` | yes | metric must shrink **at least N%** below baseline |

Syntax (ticket criteria): `synthesis_ok: {targets: [<target>], cell_count_max: 500, fmax_mhz_min: 400}`.

For a relative threshold, a Target entry may instead be a directed frozen pair: `{baseline: <baseline-target>, candidate: <candidate-target>}`. A plain Target name is backward-compatible shorthand for using that Target on both sides.

In Ticket Mode, ticket creation seals an immutable Target contract before enqueue. A baseline-relative `synthesis_ok` or `fpga_impl_ok` criterion runs the pair's baseline Target at `base_sha` and its candidate Target at the ticket head. Both Targets and their directed binding are sealed. Developer execution cannot change contract controls; a missing or incorrect Target blocks as `target-contract-change-required` for revision and resealing. Missing or mismatched baseline evidence never skips a relative check.

**`synthesis_ok` (ASIC)**

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

**`fpga_impl_ok` (FPGA)**

| Metric | _max | _min | _increase_at_most | _reduce_at_least |
|--------|:---:|:---:|:---:|:---:|
| `bram_count` | ✓ | — | ✓ | ✓ |
| `critical_path_ps` | ✓ | — | ✓ | ✓ |
| `dsp_count` | ✓ | — | ✓ | ✓ |
| `ff_count` | ✓ | — | ✓ | ✓ |
| `fmax_mhz` | — | ✓ | — | — |
| `lut_count` | ✓ | — | ✓ | ✓ |

> Mutually exclusive: `critical_path_ps_max` ⊕ `fmax_mhz_min`.

**Per-test `cycle_count`**

Use a list of mappings. Every item names one `target` and registered `test`, plus one or more thresholds; all thresholds on the item must pass. Relative forms automatically compare the same Target/test at the ticket's pinned `base_sha`.

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

Syntax (ticket criteria): `cycle_count: [{target: sim_coremark, test: coremark, cycle_count_max: 100000, cycle_count_reduce_at_least: 5}]`.

A named `[SIM_CYCLES] <test> <count>` observation is gated evidence only when that exact test passes. Missing, malformed, duplicate, legacy unnamed, failed, or inconclusive evidence fails closed. Without a `cycle_count` Criterion, existing Cycle Count records remain observational.

Relative comparisons report an **observed Cycle Count change**. When declared workload inputs differ, review reports disclose the changes and do not attribute the result to RTL alone.
<!-- END GENERATED: criteria-params -->

**Per-clock timing thresholds.** The timing metrics
(`critical_path_ps`, `fmax_mhz`, `wns_ns`, `whs_ns`, `period_ns`) are reported
per clock and their thresholds may be flat or clock-scoped (area/cell/LUT/FF/
BRAM/DSP/utilization are not clock-scopable):

- Flat `fmax_mhz_min: 400` → **every** clock's Fmax ≥ 400 (gates the worst clock).
- Clock-scoped `clk_i.fmax_mhz_min: 400` → only clock `clk_i`.

`critical_path_ps_max` ⊕ `fmax_mhz_min` is mutually exclusive **per clock**.
Example: `synthesis_ok: {targets: [<target>], clk_i.fmax_mhz_min: 400, clk_2x.critical_path_ps_max: 5000}`.

### Targets

`--target` values are FuseSoC `.core` Target names; when a bare name is declared by more than one core, qualify it as `vlnv#name`. `booley targets` shows them all:

| Command | What it does |
|---------|-------------|
| `booley targets` | List every `.core` Target: flow, EDA tool, toplevel, and `Dr` Doctor selection |
| `booley targets --for sim` | Only Targets that Booley Flow could drive (any target-aware Booley Flow) |
| `booley targets 'sim_*'` | Glob filter over bare name or `vendor:lib:name#target` |
| `booley targets <name>` | Resolved detail: parameters, file counts, SDC/XDC (runs `fusesoc`, container-side) |

`--json` composes with all of the above; agents get the same listing via the `booley_targets` MCP tool.

Booley-authored Targets are named `<axis>_<subject>`: axis token for the driving Booley Flow (`sim_` for `sim`/`elab`, `lint_`, `synth_`, `fpga_`), then a subject that distinguishes the Target from others, coarse to fine: `sim_smoke`, `synth_timing`. The axis leads because nothing else distinguishes a synth Target from an FPGA Target (CAPI2 has no synth flow). `booley doctor` warns on names that don't, and on a `default:` Target in a core nothing `depend:`s on; vendored upstream cores are exempt.

### Project Files

Recognized user-owned inputs under `.booley_project/` are listed below.
Generated runtime state, reports, logs, worktrees, and managed marker files are
omitted. FuseSoC `.core` design files also stay outside this directory, beside
the design sources they describe.

#### Basic project files

| File | Information | Affects |
|------|-------------|---------|
| `booley.toml` | Project, Flow, agent, sandbox, and job policy | All Flows and Specialists |
| `tests.toml` | Per-Target tests, selectors, skips, and environment | `sim`, `mutation_tester` |
| `ticket_creation.md` | Free-form Ticket Creation Guidance | `/booley-ticket-create` only |
| `doctor-waivers.toml` | Reviewed warning waivers and expiry | No endpoint; `doctor` only |
| `AGENTS.md` | Project instructions, ownership, and gotchas | Developer Agent and Specialists |
| `rtl_style_guide.md` | Project RTL style overrides | `reviewer` RTL code-style focus |
| `tb_style_guide.md` | Project testbench style overrides | `reviewer` TB quality focus |
| `docker/Dockerfile` | Project image build steps and dependencies | All runtime Flows/Specialists |
| `<requirements>.txt` | Python dependency pins selected by `booley.toml` | All runtime Flows/Specialists |
| `hooks/post-setup.*` | Per-worktree setup commands | All Ticket Mode endpoints |

#### Custom tool files

Only needed when adding custom Flows, Specialists, or host tools. Most projects
do not need these files.

| File | Information | Affects |
|------|-------------|---------|
| `criteria.toml` | Criteria defined for custom tools | Their producer Flows/Specialists |
| `mcp_tools/*.py` | Custom Flow, Specialist, or MCP definitions | The defined in-runtime endpoints |

### Skills

| Skill | Use it when | Result |
|-------|-------------|--------|
| `/booley-ticket-create <desc>` | You want to create a ticket | Apply guidance; preview and enqueue |
| `/booley-ticket-triage` | Tickets are blocked or awaiting review | Unblock/reset or approve/reject |
| `/booley-heal` | Doctor or Flow health has drifted | Repair safe findings; verify Doctor |
| `/booley-feedback` | Report bugs, friction, praise, or ideas | Redact evidence; submit after approval |

### Artifacts

`<LOGS>/<slug>/` is one ticket's log directory under the project's ticket
logs root.

| Artifact | Path | Use |
|----------|------|-----|
| Human summary and ticket snapshot | `<LOGS>/<slug>/` (`REPORT.md`, `ticket.md`, plans, summaries) | Start here when reviewing what the run did |
| Prepared HTML explanation | `<LOGS>/<slug>/*-explanation-<slug>.html` and `.runtime/triage-prep/` | Rich change walkthrough linked from approve/reject triage |
| Human-readable logs and prompts | `<LOGS>/<slug>/human-logs/` | Follow the run, errors, prompts, and rendered transcripts |
| Machine state and checkpoints | `<LOGS>/<slug>/.runtime/` | Resume/debug state; implementation detail rather than the first review stop |
| Per-invocation Flow reports | `<LOGS>/<slug>/.runtime/flow-reports/<flow>/<N>/report.json` | Structured verdict, metrics, and evidence (`N` is the invocation number) |
| Per-invocation Specialist reports | `<LOGS>/<slug>/.runtime/mcp-tool-reports/<mcp-tool>/<N>/report.json` | Structured Specialist verdict and evidence |
| Raw agent transcripts | `<LOGS>/<slug>/.runtime/transcripts/` | Provider-level debugging when the rendered transcript is insufficient |

### Runtime & Docker

The `booley-sandbox` image contains Booley's EDA toolchain, agent runtimes, and development dependencies. It backs the per-folder Session Runtime (devcontainer) where all Booley work, Interactive and Ticket Mode alike, executes; a project image selected by `[sandbox].image` can extend it.

| Command | What it does |
|---------|-------------|
| `booley init` | Set up project + pull or build the image |
| `booley init --force` | Rebuild from scratch (no cache) |
| `booley doctor` | Verify image + container EDA tools |
| `booley session up` | Start the Session Runtime headlessly (no VS Code) |
| `booley session enter [-- cmd]` | Shell into it, or run one command |
| `booley session down` | Stop and remove it |
| `docker pull ghcr.io/boldaxolotl/booley-sandbox:<ver>` | Manual pull of pre-built image |

To extend the image, create `.booley_project/docker/Dockerfile` with `FROM booley-sandbox`, build it, then set `[sandbox].image` in `.booley_project/booley.toml`.

**`booley run` is container-only.** Run it from a terminal **inside** the devcontainer (Reopen in Container, or `booley session enter`), one terminal per concurrent ticket, up to `[jobs] max_tickets` (default 2); extra runs queue with "waiting for slot (position N)". Launched on the host it fails fast and names the fix. `booley init` and `booley session` stay host-side; `booley doctor` works on either side.
