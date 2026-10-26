# CLI reference

Every public command, Booley Flow, and Specialist. You rarely need this day
to day: ask the Developer Agent and it picks the command. [USAGE.md](USAGE.md)
covers the workflow.

## Commands

`booley help` and `booley --help` show the same public commands, grouped as
`[Sandbox]` first, `[either]` next, and `[host]` last. Sandbox commands
run in a container terminal. `[either]` commands work on the host and inside the
Sandbox. Target listing works in either location, but full detail resolution
requires the Sandbox. Flow and Specialist listing and
execution are Sandbox-only; their `-h` / `--help` pages also work on
the host inside an initialized Project.

| Location | Public commands, in order |
|---|---|
| Sandbox | `chat`, `dashboard`, `flow`, `specialist`, `worktree` |
| Either | `cheat`, `doctor`, `help`, `targets` |
| Host-only | `auth`, `bootstrap`, `eda`, `init`, `projects` |

Commands used by skills remain available but are omitted from top-level help.
Use `booley cheat --skills` for guided agent workflows.

```bash
# Dashboard (Sandbox)
booley dashboard                  # sessions, Goals, and Jobs

# Worktrees (container)
booley worktree new <name>        # .booley/worktrees/<name>, Paired Checkout or Snapshot

# Quick reference
booley cheat                      # available sections (same as --list)
booley cheat --all                # the whole cheatsheet
booley cheat --goals              # Goal Mode workflow
booley cheat --criteria           # evidence catalog (combine sections)

# Health checks
booley doctor                     # check setup
booley doctor --concise           # show only problems
booley doctor --deep              # also run short real sims, lints, and syntheses
booley doctor --deep --skip-agent-checks   # CI: skip the login checks

# Projects on this machine (host)
booley projects                   # list known projects
booley projects discover <dir>    # find existing projects under <dir>
```

`booley worktree new <name>` gives a Versioned Booley Directory a
Paired Checkout on `booley-worktree/<name>` at its current HEAD. Commit tracked
changes and untracked files in the Booley Directory's repository first; an unborn
repository and an existing Booley Directory branch are refused. Ignored state stays
behind. A non-versioned Booley Directory supplies a Snapshot. Run this command from
the primary workspace; creating from a Paired Checkout is refused.
The outer checkout starts detached unless `<branch>--<description>` selects an
existing branch.

Removing a worktree deletes everything in it, including ignored files. First
preserve the commits you need, then copy needed untracked and ignored files into
a new archive outside the worktree, overwriting nothing. Check the whole worktree,
including its Booley Directory: `reviewer-evidence/`, the Findings Log
(`findings.jsonl`), `handoffs/`, reports and runtime diagnostics. Also copy every
needed file that Findings attach, wherever it lives: the log does not contain
them, and its absolute attachment paths may not resolve from the archive. Keep
the archive separate from the main Findings Log; do not merge the two.

Then, in the same Sandbox and from outside the worktree:

1. Clean dirty and untracked files, including a Snapshot's copied Booley
   Directory files when Git lists them as untracked.
2. Paired Checkout only: remove the inner checkout with
   `git -C <absolute-booley-directory> worktree remove <absolute-worktree>/.booley`.
3. Remove the outer worktree with
   `git -C <absolute-project-root> worktree remove <absolute-worktree>`.
4. Optional, Paired Checkout only: to reuse the name, delete the Project branch
   with `git -C <absolute-booley-directory> branch -D booley-worktree/<name>`.

Inspect locks before unlocking a worktree or forcing removal. `booley worktree new`
prints the worktree path to stdout and these steps to stderr, with a warning: Booley
cannot map paths to the host, so the commands use quoted absolute **Sandbox
paths**. Do not paste them into a host terminal.

Outside Goal Mode, `--baseline` uses the Paired Checkout branch's recorded
creation base ahead of its upstream. Pushing, merging or rebasing newer Project
changes does not update that recorded commit: the Project baseline is its
merge-base with the current HEAD. Rewriting history can therefore select an
older common ancestor. A branch without a recorded base falls back to its
upstream's merge-base with HEAD; a branch with neither refuses. An invalid
recorded base refuses even when an upstream exists. A detached paired HEAD
uses its checked-out commit, including during a rebase or bisect. Baseline
preparation progress names the rule and commit. Goal Mode keeps the exact
Paired Checkout commit pinned at entry.

`booley projects discover` stops descending at each initialized Project to
avoid scanning its RTL, vendor, and build trees. Nested Projects are not imported
by that scan; run `booley projects discover <nested path>` on a nested Project
directly to import it.

Booley also runs a quick health check on its own when the container starts and
on supported command and Flow paths: about once a week, daily while problems remain, and
whenever the configuration changes. It never blocks work. New problems show up
in `booley session up` and the next Flow result. The last result
is in `.booley/runtime/doctor/last.log`.

Plain Doctor and `booley session up` also report whether prior deep validation is
current or due. Deep evidence has no expiry; automatic change triggers are only
the Booley version and active immutable Sandbox Image. Project/Target/RTL edits
leave that evidence intact. Deep due is advisory and never launches deep checks.
See [Doctor health and deep validation](DOCTOR.md) for qualification, failed and
cancelled attempts, and image identity handling.

Use `booley cheat --goals` for the Goal lifecycle and MCP tools.

## Booley Flows & Specialists

You rarely call Flows or Specialists yourself: ask
the Developer Agent (*"run the reset test on `sim_lite`"*, *"how much area did that
cost?"*) and it picks the Flow or Specialist, Target, and options. The **Sets** column shows
which [acceptance criteria](GOAL-MODE.md#working-with-evidence) each one can satisfy. Which
EDA program runs underneath depends on the Target; see
[SUPPORTED-EDA-TOOLS.md](SUPPORTED-EDA-TOOLS.md). `booley cheat --flows` and
`booley cheat --specialists` print the same lists.

<!-- BEGIN GENERATED: flows -->
**Booley Flows**

Deterministic end-to-end orchestration; no LLM:

| Booley Flow | Purpose | Sets |
|--------|---------|------|
| `sim` | Run RTL simulation for one or more Targets | `ELAB:{target}`, `SIM:{target}`, `CYCLE_COUNT:{target}`, `COVERAGE:{target}` |
| `lint` | Run lint for one or more Targets | `LINT:{target}` |
| `synth` | Run ASIC synthesis for one or more Targets with optional baseline comparison | `SYNTH:{target}` |
| `fpga` | Run FPGA implementation for one or more Targets with optional baseline comparison | `FPGA:{target}` |

Shared Flow options and results are in [FLOW_REFERENCE.md](FLOW_REFERENCE.md), each Flow's own in [FLOW-SIM.md](FLOW-SIM.md), [FLOW-LINT.md](FLOW-LINT.md), [FLOW-SYNTH.md](FLOW-SYNTH.md), and [FLOW-FPGA.md](FLOW-FPGA.md), and report files in [FLOW_REPORTS.md](../internals/FLOW_REPORTS.md); `booley flow <name> --help` prints the options too.

**Specialists**

LLM-powered Specialists running in scoped, isolated workspaces:

Ask your Developer Agent session to invoke a Specialist by name with the arguments below. Or run `booley specialist <name> [args...]` inside the Sandbox. `booley specialist` lists visible Specialists; `booley specialist <name> --help` shows their arguments. `--model`, `--max-turns`, and `--timeout` are CLI-only controls.

For example: `booley specialist reviewer --category rtl --focus bugs --scope rtl`. The supported module alternative is `python -m booley.specialists.reviewer` with the same flags. Common options are `-C/--project PATH`, `--report-dir`, `--diagnostic`, and `--target` where supported. `--timeout DURATION` accepts positive seconds (`90` or `90s`), minutes (`30m`), hours (`2h`), and combinations (`1h30m`). Model-call minimum budgets apply; seconds-only providers round up. CLI Flow and Specialist runs never record Goal evidence; use the Developer Agent's Goal-bound MCP calls with `work_dir`.

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
<!-- END GENERATED: flows -->

The Developer Agent writes testbenches directly.

## Running a Booley Flow directly

Running a Flow yourself is useful when checking your setup or reproducing a
problem. In a container terminal:

```bash
booley specialist                            # list visible Specialists
booley specialist reviewer --help            # shared and Specialist arguments
booley flow                                  # list the Flows
booley targets --for sim                     # Targets a Flow can use
booley flow sim --target sim_soc --test reset
booley flow sim --target sim_soc --test stress+soak@42   # a Test Variant with one seed
booley flow sim --help                       # every option
```

For sim, lint, synth, and fpga on CLI and MCP, omitting `--target` selects
exactly one eligible Target, and that selection is announced. With zero or multiple
eligible Targets, the run exits 2 and lists eligible selectors (`(none)` for zero).
Simulation resume (`--resume-from`) retains the manifest Target regardless of
the current catalog.

[FLOW_REFERENCE.md](FLOW_REFERENCE.md) has the full details: options, exit
codes, and results. [FLOW_REPORTS.md](../internals/FLOW_REPORTS.md) defines the
report files.

**Coverage.** Coverage is collected only when you ask for it. Then pass the
result to the Coverage Analyst for an explanation:

```bash
booley flow sim --target sim_counter --coverage
```

Then call the `coverage_analyst` Specialist from your Developer Agent session with
`campaign="<reports>/sim/12/targets/sim_counter/coverage.json"`.

In Goal Mode, add a [coverage Goal](COVERAGE.md#native-coverage-configuration)
for each Target that needs one. Only valid coverage evidence satisfies it.
The Analyst may screen Waiver Candidates; approving one requires your decision
through `goal_propose_change`. Rerun coverage after approval to obtain fresh
strict evidence. See [coverage waivers at review](COVERAGE.md#coverage-waivers-at-review).
