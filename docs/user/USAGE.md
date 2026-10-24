# Usage

This guide shows how to use Booley day to day. You don't need any experience
with AI coding agents to follow it.

**Contents**

1. [Read this first](#read-this-first)
2. [Check your setup](#first-verify-your-setup)
3. [Choose how to work](#choose-how-to-work)
4. [Interactive Mode](#interactive-mode): [first session](#open-your-first-developer-agent-session), [good prompts](#write-a-useful-prompt), [reviewing changes](#what-the-developer-agent-is-allowed-to-do), [editing instructions](#editing-project-instructions), [waveforms](#viewing-waveforms)
5. [Goal Mode](#goal-mode): [Goalsets](#goalsets-and-entry), [Test Runs](#goals-that-name-test-runs), [evidence](#working-with-evidence), [human decisions](#changes-and-human-decisions), [Finish](#finish-and-review)
6. [Recovery and parallel work](#recovery-unattended-work-and-parallel-sessions), [without VS Code](#entering-the-sandbox-without-vs-code)

7. [Auth & billing](#auth--billing)
8. [Reporting problems and feedback](#when-booley-itself-misbehaves)
9. Reference: [Flows & Specialists](#booley-flows--specialists), [Goal catalog](#goal-catalog), [CLI](#cli-reference)

## Read this first

This guide picks up after installation and project setup. It assumes that
`booley init` and the `booley-setup` skill have both finished. If they haven't,
install Booley using the [README](../../README.md#installation), then follow
[SETUP.md](SETUP.md).

**What Booley does.** Booley lets a Developer Agent, such as Claude Code or
Codex, run your project's EDA tools. The Developer Agent is more than a chatbot: it
can read and edit files, run commands, and call Booley while it works. You
describe what you want in plain language, and the Developer Agent picks the right Booley
Flow or Specialist and runs it. You are still responsible for reviewing its reasoning, its
code, and the hardware results.

Booley offers two kinds of capability:

- **Flows** (`sim`, `lint`, `synth`, `fpga`) run EDA tools in a fixed,
  predictable way. No AI is involved.
- **Specialists** (`reviewer`, `mutation_tester`, `coverage_analyst`) are small
  LLM-powered capabilities that each do one focused job.

For direct CLI reproduction, see [live Flow progress and logs](FLOW_REFERENCE.md#live-progress-and-logs), including quiet and verbose output.

Both run inside the **Sandbox**, a Docker container that holds the EDA tools
and keeps the Developer Agent away from the rest of your computer.

Sandbox limits and network egress for both execution modes are host-owned
`[sandbox]` settings; see [Host configuration](CONFIG.md#host-configuration-configtoml).

**Where you type.** This guide uses three places:

| Place | What goes there | Example |
| --- | --- | --- |
| **Host terminal** | Commands on your own computer, outside Docker | `booley doctor`, `code .` |
| **Container terminal** | Commands inside the Sandbox, after VS Code has reopened the project in its container | `booley dashboard`, `git diff` |
| **Developer Agent chat** | Plain-language requests and skills that start with `/` | *"Run lint and explain every finding"* |

Command blocks go in the terminal named in the text around them. Sentences in
*italics* are prompts for the Developer Agent chat. Skills such as
`/booley-goal` also go in the Developer Agent chat, not in a terminal.

Booley uses some terms with exact meanings. When one is unfamiliar, look it up
in the glossary linked from the [context map](../../GLOSSARY-MAP.md).

### Selecting a Project

Use `-C/--project PATH` on Project-bound commands to select a checkout or worktree,
including a nested directory in it. Paths are relative to the caller; Booley does
not change the process directory. With no selector, cwd discovery stays unchanged.
An explicit canonical selector overrides `RTL_PROJECT_ROOT`.

```bash
booley -C ../other targets
booley session -C ../other status
booley session status --project ../other
booley flow lint --target lint --project ../other
```

Use the selector once, at the root, command, or nested operation. It also works on
`chat`, `doctor`, `init`, `upgrade`, `cleanup`, and `feedback`, and on bare
`booley` inside the Sandbox to select the Project for chat. On the host, bare
`booley` prints help without resolving the selector.
`auth --status` optionally uses the selected Project and its auth policy; outside
a Project it reports both supported AI agent clients (Claude Code and Codex),
unless `--app` selects one. Bare `cheat` and `cheat --list` accept and ignore the
selector without discovering a Project.
`bootstrap`, `projects`, and `eda` retain their grammar
and do not accept the new selector. Full option names are stable; ambiguous
abbreviations produce an error. Arguments after a command's payload `--` stay opaque.

Selection chooses the checkout, while `BOOLEY_DIR` chooses the Booley Directory;
an external data override remains supported. Checkout-local data configuration and
initialization's destination scope keep their existing precedence. A selector
requires an existing directory; `init` accepts an uninitialized directory.

`cheat --project-files` selects the Project Files section.

Flows and Specialists accept `--timeout DURATION`: positive integer seconds (`90`
or `90s`), minutes (`30m`), hours (`2h`), or descending combinations (`1h30m`).
MCP parameters and config keys use milliseconds.

## First, verify your setup

Run these in a **host terminal**. They check that Booley works and show what it
can see:

```bash
booley doctor          # health check: configuration, Sandbox Image, EDA tools
booley targets         # every Target Booley can see, grouped by core
booley cheat           # available quick-reference sections
```

Don't continue until `booley doctor` shows no failures or warnings.

- If `booley` is not found, go back to the
  [installation instructions](../../README.md#installation).
- If Booley says its version changed, type `/booley-heal` in the Developer Agent chat.
- For a quick overview of Booley, start with `booley cheat`. It lists the
  available sections; print the ones you need, for example `booley cheat --goals`
  or `booley cheat --commands --project-files`. Use `booley cheat --all` for the
  whole cheatsheet; `booley cheat --list` also shows the section list.
- `booley doctor --deep` goes further and runs short real simulations, lints,
  and syntheses.

## Upgrade review

When Doctor reports `upgrade.review-pending`, run `booley upgrade status --json`
and invoke `/booley-heal` to review the release changes. Follow the
[Doctor upgrade-review workflow](DOCTOR.md#upgrade-review) for plain, deep, and
host verification, acknowledgment, and the final plain check.

## Choose how to work

Start with Interactive Mode to explore and run individual Flows. Use
`/booley-goal` in the same Developer Agent chat when a task needs mandatory
completion conditions. Goal Mode supports both human guidance and unattended
work, with one linked worktree and Goal Branch per session.

## Interactive Mode

### Open your first Developer Agent session

1. In a **host terminal**, go to your RTL project and open VS Code:

   ```bash
   cd path/to/your-rtl-project
   code .
   ```

   If `code` is not found, open VS Code yourself and use **File → Open Folder**.

2. When VS Code offers **Reopen in Container**, accept it. If the offer doesn't
   appear, open the Command Palette (`Ctrl+Shift+P`) and run **Dev Containers:
   Reopen in Container**. The first start can take several minutes. When it's
   done, the lower-left corner of the window says you are in a Dev Container.

3. Open **Terminal → New Terminal**. This is a **container terminal**. Start
   the Developer Agent:

   ```bash
   booley
   ```

   `booley` (or `booley chat`) just starts the Developer Agent chosen in
   `[agent].provider` in `.booley/booley.toml`:

   | Provider | Same as running |
   | --- | --- |
   | `claude` | `claude` ([Claude Code](https://code.claude.com/docs/en/quickstart)) |
   | `codex` | `codex` ([Codex CLI](https://developers.openai.com/codex/cli)) |

   From then on you are using that CLI's own chat. To pass it extra options,
   run `claude` or `codex` directly. To run several sessions, open more
   container terminals.

   Prefer a chat panel? The Claude Code or Codex VS Code extension is already
   installed in the container. Open its panel instead of using the terminal.

   If you see a login screen instead of a chat, run `booley auth --status` in a
   **host terminal** and follow its advice (usually `booley auth`). Then run
   **Dev Containers: Rebuild Container** and try again. See
   [Auth & billing](#auth--billing).

4. Type this safe first request into the chat:

   > Check whether Booley Interactive Mode is ready. List the available
   > simulation targets and explain what each one is for. Do not change files.

   The agent asks Booley for this and summarizes what it found. You never call
   Booley's MCP tools yourself.

5. If the agent listed a simulation Target, try a real run:

   > Run the tests on the most appropriate simulation target. Do not edit any
   > files. Tell me what ran, whether it passed, and where the detailed report
   > was written.

   A good answer names the Target and Flow it used, says whether the result was
   a pass, a design failure, or an infrastructure failure, and suggests what to do next.
   If there is no simulation Target, ask for lint instead.

That's a complete session. You don't need to read the rest of this guide
before you keep going. Just ask for what you want:

- *"Debug the backpressure test failure on the sim_heavy target."*
- *"Compare synth area between the following commits ..."*
- *"Run a security review on the control unit module."*

You don't need to know Flow names, flags, or where reports go. Give the agent
the goal and any limits, and it works out the details.

### Write a useful prompt

Brief the Developer Agent the way you would brief an engineer joining the task:

- what you want to learn or change,
- the Target or module, if you know it,
- any limits,
- what evidence you want to see.

If you only want an investigation, say **do not edit files**. For example:

> The `ready` signal sometimes remains low after reset on the `sim_full`
> Target. Reproduce the failure, inspect the waveform, and explain the likely
> cause. Do not edit files yet. Show me the evidence and propose a fix.

If the agent heads the wrong way, don't start over. Tell it which assumption
was wrong or what evidence you want next.

### What the Developer Agent is allowed to do

Inside the container, the Developer Agent **never asks for permission** before acting.
That is deliberate: the container itself is the safety boundary. It can only
see your project, and its only network access is to the AI provider. Booley
does not change your Claude Code or Codex settings outside the container.

**Always review changes yourself.** Use `git diff` in a container terminal and
run the relevant checks. Asking the agent to explain its diff helps, but it
doesn't replace your own review.

> **Tip: let the Developer Agent commit; you push.** Let the Developer Agent commit its own work;
> it writes good commit messages. It **can't** push to GitHub or any other
> server, because the container blocks that network access. So: let it commit,
> review the commits, then push them yourself from a terminal outside the
> container.

Per-session logs of Flow and Specialist calls through Booley’s tool server
are saved in `.booley/.interactive_logs/`. Chat transcripts live in
the Developer Agent CLI’s own session storage. If the
Developer Agent can't reach Booley (`booley` is missing from its `/mcp` list), see
[TROUBLESHOOTING.md](TROUBLESHOOTING.md#booley-is-missing-from-mcp-in-claude-code-or-codex).
How the connection works is in
[ARCHITECTURE.md](../internals/ARCHITECTURE.md#interactive-mode).

### Editing Project instructions

Root `AGENTS.md` and `CLAUDE.md` normally link to the canonical Booley Directory
guide. Editing either link edits that guide. In an embedded non-Stealth layout,
stage the canonical file with `git add .booley/AGENTS.md`, substituting your
configured Booley Directory if different. The root links remain ignored.
For a separate Booley repository, including Stealth, stage and commit `AGENTS.md`
inside that repository. An explicitly selected hybrid footprint instead uses a
matching tracked root `AGENTS.md` copy; keep it identical to the canonical guide
and stage changes to both copies.

### Viewing waveforms

When a simulation with tracing fails, the Developer Agent reads the waveform on its own
with `bwave`. When *you* want to see it, ask: *"show me the FIFO handshake
around the failure"*. The agent opens it in a VaporView tab in VS Code, showing
just the signals and time range that matter. VS Code installs the extension
automatically when you attach to the container. If it is missing, see
[TROUBLESHOOTING.md](TROUBLESHOOTING.md#bwave-gui-fails-on-a-scoped-view).

You can also open a view yourself:

```bash
bwave gui                                                      # the latest trace
bwave gui @dut --signals 'tb.clk%b@red' \
  --group 'FIFO handshake=tb.dut.fifo.*%h@green' \
  --group 'Control=tb.dut.ctrl.state%h@blue' --time 1200c:1400c
```

The clock always goes on the first row. Each `--group 'NAME=PATTERN'` becomes
a named, collapsible section. `--time START:END` puts markers at both ends so
the status bar shows the span. Add `%b`/`%h`/`%d` to a signal for binary, hex,
or decimal, and `@red`/`@blue`/`@green` for a color. `bwave gui --help` has
the rest. If a view fails to open, see
[TROUBLESHOOTING.md](TROUBLESHOOTING.md#bwave-gui-fails-on-a-scoped-view).

## Goal Mode

Goal Mode gives one Developer Agent session mandatory, evidence-backed completion
conditions. Open a Developer Agent session and type `/booley-goal` with the work
you want done. The skill helps you choose a clean linked worktree, Goalsets,
Targets, and ad-hoc Goals, then calls `goal_enter`. Booley creates a Goal Branch
from the worktree's current HEAD and records that base for comparisons.

### Goalsets and entry

Goalsets are Project-owned Markdown files under `.booley/goalsets/`.
`booley init` creates missing `feature.md`, `bugfix.md`, `refactor.md`, and
`verification.md` without rewriting existing files. Customize their prose to
express your project's rules. The Developer Agent translates them into concrete Goals;
Booley validates the arguments, not the prose. Each Goal records its origin.

An optional `default.md` applies to every entry. Skipping it requires your
explicit instruction and reason. Setup can create this file; initialization
does not seed it. You can also add Goals for this particular change. Duplicate
Goals merge to the stricter condition, and entry reports every merge.

Goalsets, including `default.md`, come from the **Main Booley Directory**. In the Sandbox that is `BOOLEY_DIR`, even when the
directory lives outside the main checkout. The Worktree Booley Directory inside
the Goal worktree is not used: a `default.md` that exists only there does not
apply. `work_dir` selects the worktree, not the Goalsets.

A session started inside a worktree without `BOOLEY_DIR` can resolve
that worktree's copy instead. Start the session with `BOOLEY_DIR` set
to the Main Booley Directory; entry refuses a Booley Directory
inside the Goal worktree.

A Goal has one name, `CAPABILITY:target`, for example `SIM:sim_fifo`.
Review Goals use `REVIEW:rtl_bugs`. Details such as tests, thresholds and the
review requirement follow the name in parentheses. Goal arguments use the
uppercase capability as `family`. Every Goal is mandatory. For example, a Goalset excerpt can ask for:

```json
[
  {"family": "LINT", "target": "lint_fifo"},
  {"family": "SIM", "target": "sim_fifo"},
  {"family": "REVIEW", "review": "rtl_bugs", "verdict": "clean"},
  {"family": "SYNTH", "target": "synth_fifo", "thresholds": {"area_increase_at_most": "10%"}}
]
```

Use the [Goal catalog](#goal-catalog) and `booley cheat --criteria` for
the public Goal names and threshold parameters. A new Target can be
named at entry but stays unmet until it exists. Spec reviews name a spec file
relative to the worktree.

### Threshold parameters

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

### Goals that name Test Runs

A **Test Run** is one Test Variant simulated with one seed, written
`test[+variant][@seed]`
([reference](FLOW_REFERENCE.md#test-runs-test-variants-and-seeds)). Simulation,
Cycle Count and coverage Goals require exact Test Runs, and each can name them
with a selector:

```json
[
  {"family": "SIM", "target": "sim_fifo"},
  {"family": "SIM", "target": "sim_fifo", "test": "stress+soak"},
  {"family": "SIM", "target": "sim_fifo", "test": "stress+soak@42"},
  {"family": "SIM", "target": "sim_fifo", "test": "stress+soak@x20"},
  {"family": "CYCLE_COUNT", "target": "sim_fifo", "test": "burst@7",
   "thresholds": {"cycle_count_max": 1000}},
  {"family": "COVERAGE", "target": "sim_fifo", "tests": ["reset", "stress@x5"],
   "metrics": {"line": 90}}
]
```

| `family` | `test` / `tests` | Requires |
|---|---|---|
| `SIM` without `test` | none | The default Test Variant of every Test of the Target that is not skipped, each at the Default Seed. |
| `SIM` with `test` | `test`, `test+variant`, either with `@seed`, or either with `@xN` | That Test Run. `@xN` requires N Test Runs of the Test Variant with different seeds, N from 1 to 1000. |
| `CYCLE_COUNT` | `test`, `test+variant`, either with `@seed` | One Test Run. `@xN` is refused: a threshold applies to the Cycle Count of one Test Run. |
| `COVERAGE` | `"all"`, or a list of selectors, `@xN` included | Coverage floors over the merged coverage of the listed Test Runs. |

A selector without a Test Variant means the Test's default Variant. A selector
without a seed means the Target's Default Seed.

**Freezing.** At entry Booley resolves every such Goal to exact Test Runs and
stores them in the Goal Record. From then on the Goal requires those Test
Runs. Editing a Test's `default` Variant or the Target's `default_seed` in
`tests.toml` afterwards does not change what the Goal requires. For `@xN`
Booley draws N distinct seeds at random at entry, so each Goal Mode samples
new seeds, and reruns inside one Goal Mode use the same ones. Goals of one
Target that name the same Test Variant with the same N share one set of
seeds.

Goals that resolve to the same Test Runs merge at entry, like other duplicate
Goals. Of two `@xN` Goals on the same Test Variant, the larger N remains. A
Goal with a pinned seed and an `@xN` Goal stay separate Goals. A Goal that
names a seed on an unseeded Target is refused at entry. So is a Goal that names
a Test its Target skips in `tests.toml`, and so is a Goal change that would
freeze one: remove the Test from `skip` or drop the Goal.

**Targets and Tests that do not exist yet.** A Goal can name a Target, Test or
Test Variant that the work will add. That part of the Goal is *pending* and
the Goal is unmet. Booley freezes it when it first exists, at the next Flow
run or status read, with the defaults `tests.toml` declares at that moment. A
Goal over a whole Target also freezes each Test that is added to the Target
later. The Review Package lists every such Test Run under the heading
`Test Runs resolved after entry`.

**Evidence.** A Test Run counts for a Goal only when its name and its declared
arguments and environment equal one of the Goal's frozen Test Runs. Each
frozen Test Run is `passed`, `failed` or `missing`, computed from applicable
evidence history at the same Goal revision and dependent file contents.
Observations retain their own Test Run rows; older stored outcome copies are
ignored. Evidence without Test Run rows supplies no outcome:

- It becomes `passed` when it passes at the current contents of the files the
  Goal depends on.
- It becomes `failed` when it fails at those contents, and a later pass at the
  same contents does not change that. A flaky seed therefore stays failed
  until the code changes.
- It is `missing` until it has run. An `error` or `inconclusive` Test Run
  leaves the outcome as it was.

A diagnostic MCP run changes none of these outcomes, even when it runs a
frozen Test Run. Its report names the Goals whose frozen Test Runs ran and
the ones that failed; use a Goal-bound Developer Agent MCP call with `work_dir`
and without `diagnostic` to record them. CLI runs never record Goal evidence.

A simulation Goal is met when every frozen Test Run is `passed`. They do not
have to pass in one invocation: 20 seeds can pass over several runs and
Simulation Campaigns, including after an interruption, and a run of a few
named Test Runs adds their outcomes without disturbing the others. A Cycle
Count Goal takes its measurement from a run of its frozen Test Run, and that
Test Run must be `passed`.

A coverage Goal is decided by its coverage floors alone. It takes its
measurement from one `--coverage` run that ran exactly its frozen Test Runs,
because coverage is not merged across runs. Whether those Test Runs passed
does not matter to it: when the floors are met the Goal is met, even if a
Test Run failed in that run or in any other. `goal_status` shows the outcomes
of the measuring run under the Goal, with a rerun command for each failure.
Add a simulation Goal for the same Test Runs when they must also pass. A run
that does not measure the Goal, such as a plain simulation, leaves a
measurement at the same file contents as it was. A failing Test Run that ends
before the simulator writes its coverage data makes collection incomplete, so
that run measures nothing either. With no earlier measurement at those
contents, the Goal stays unmet as not measured.

If a frozen Test Run's Test or Test Variant is removed, or its declared
arguments or environment change, the Goal stays unmet until you approve a
retarget. If its Test is skipped in `tests.toml`, the Goal stays unmet until
the Test leaves `skip` or you approve a Goal change that drops it; a retarget of
a Goal that names the skipped Test is refused, while a retarget of a Goal on the
whole Target freezes its suite again without that Test. See
[A Goal that names Test Runs stays unmet](TROUBLESHOOTING.md#a-goal-that-names-test-runs-stays-unmet)
for each reason `goal_status` can show.

**Seeing the frozen Test Runs.** `goal_status`, `booley goal status` and the
Dashboard list each Goal's frozen Test Runs with their outcomes, the parts
still pending, the Test Runs resolved after entry, and a rerun command for every
failed Test Run. The Review Package computes the same outcomes from its frozen
evidence and lists each frozen Test Run with its rerun command. It retains the
contributing observations, so later runs do not change a saved package.

### Working with evidence

The Developer Agent passes the absolute worktree root as `work_dir` to every tool that
accepts it.

Built-in MCP worktree routing:

| Accepts `work_dir` | Does not accept `work_dir` |
| --- | --- |
| `sim`, `lint`, `synth`, `fpga` | `booley_status` |
| `reviewer`, `mutation_tester`, `coverage_analyst` | `booley_poll`, `booley_cancel` |
| `goal_enter`, `goal_status`, `goal_propose_change`, `goal_finish` | `booley_sleep`, `coverage_evidence` |
| `booley_targets`, `booley_report` | `bwave` |

`booley_sleep` appears only when diagnostic tools are enabled, and
`coverage_evidence` only in the Coverage Analyst's nested server.

While any Goal Record occupies the Project, Booley Flows, Specialists, Goal
tools, and `booley_report` refuse a call without `work_dir`, even a diagnostic
call outside that worktree; `booley_targets` may omit it. Project-defined
endpoints must declare `work_dir` to run.

Only Booley Flows and Specialists meet Goals. Shell output or the Developer Agent saying
"done" does not. `goal_status` shows each Goal's current evidence and freshness;
code changes can require another run. For a bugfix, reproduce the reported bug
with a failing test before editing the design.

Protected Inputs decide how evidence is produced: `booley.toml`,
`FUSESOC_IGNORE`, and the Project's `hooks`, `.managed`, `generators`, and
`mcp_tools` directories. Editing one blocks Finish until reverted and discards
affected evidence. Never weaken tests or Targets to make a Goal pass. The
Review Package flags `.core`, `tests.toml`, and `.sdc`/`.xdc` edits.

### Simulation baseline reports

Entering Goal Mode starts a background Job for each SIM Goal's frozen Test Runs
at the Goal Branch's base contents. Adding a SIM Goal through an approved Goal
Change schedules its baseline too. Entry returns while the Job runs; status
shows the baseline as pending. Booley reuses a result for the same Target,
resolved Test Run and dependent file contents across sessions and worktrees.
Protected execution inputs are part of the cache identity too.

`goal_status`, `booley goal status`, the Dashboard's Goal details and the Review
Package show a summary for each SIM Goal. Every Test Run other than
`pass -> pass` is named, with its Variant and seed:

| Transition | Meaning |
|---|---|
| `pass -> pass` | Passed at baseline and passes at current contents. |
| `fail -> pass` | Failed at baseline and passes now. |
| `pass -> fail` | Passed at baseline and fails now: a regression. |
| `fail -> fail` | Failed at baseline and still fails. |
| `new (seen failing) -> pass` | Absent at baseline; a matching functional failure was recorded earlier in this session at different dependent contents. |
| `new (never seen failing) -> pass` | Absent at baseline and no qualifying earlier failure was recorded. |
| `new -> fail` | Absent at baseline and fails now. |

Baseline and current evidence must name the same resolved Test Run, including
Variant, seed and declared arguments. An existing Test with incompatible inputs
is **not comparable**, with a reason. A new Test never runs against the baseline design
using a newly authored testbench. A stale or missing current result is shown as unavailable
rather than a transition. The Review Package calls attention to regressions and
new Test Runs that passed without being seen failing.

These reports do not change any Goal's met, unmet or stale state and never block
Finish. A build error, timeout, interrupted Job, unavailable dependency or
ungraded result is **unavailable**, with its reason, rather than a failing Test.
Only a recorded functional Test failure counts as seen failing; a timeout or
infrastructure error does not. Earlier evidence without that classification
cannot prove a functional failure. Finish freezes the report as it is, so its
Review Package may still show a pending baseline.

Booley verifies versioned baseline dependencies against the base commit and
uses captured entry configuration for an unversioned Booley Directory. Dependencies
outside those pinned or captured inputs make the report unavailable. The Project
must ignore Booley's baseline scratch directory, as Project Initialization
configures it; otherwise Booley reports an unavailable baseline without leaving
scratch files that would affect entry or Finish. The advice to reproduce a bug
before editing remains unconditional.

### Changes and human decisions

When a Goal needs to be added, relaxed, retargeted, or changed through a coverage
waiver, the Developer Agent uses `goal_propose_change`. You see the exact proposal and
approve or reject it with a reason. The client form is used when available;
chat fallback records your quoted instruction against the saved proposal ID.
Silence cannot approve a change. Approved changes appear in the Change Log.
Coverage Waiver Candidates need your decision; the Analyst cannot approve them.

Changing which Test Runs a Goal requires is a Goal change like any other:

| Change | Kind | Frozen Test Runs |
|---|---|---|
| Add a Goal that names a Test Run | `add` | Frozen when the change is applied. |
| Pin a seed, name another Test Variant, or raise N | `retarget` | Raising N keeps the frozen seeds and draws only the difference. |
| Lower N | `relax` | The Goal keeps its first N frozen seeds. |
| Retarget a Goal without changing how it is written | `retarget` | Frozen again from the Target as it is now. Use this when a frozen Test Run is gone or its declaration changed. |

A change that would freeze a Test Run of a Test the Target skips in `tests.toml`
is refused.

After any of these changes the Goal's Test Run outcomes start again.

Goal Mode clients must expose MCP `structuredContent` to the Developer Agent.
Entry and status return the internal `goal_key` change addresses there, separately
from human Goal names. A client that forwards only text can read status but cannot
address an existing Goal for a change.

### Finish and review

Finish requires all Goals met with fresh evidence at a clean, committed HEAD
and a Session Summary. The Developer Agent calls `goal_finish` with the record ID, a
stable operation ID, and that summary. The Review Package contains the diff
against the base, final Goals and evidence, the Change Log, open review
findings, Target changes, constraint edits, and the Session Summary.

A `clean` review requires no open findings. A `done` review is advisory:
findings remain in the package and do not require a second approval to Finish.
Read the package before deciding what to merge. Completion keeps the Goal
Branch and worktree; merge, publication, and cleanup need separate instructions.

Local Goal Records live under `.booley/goals/<goal-id>/`. Outside
Stealth, Finish commits the Session Summary under
`.booley/goals/history/<goal-id>.md` in the Goal worktree when that
path is Git-trackable and owned by the RTL repository. Stealth, excluded, or
Paired Checkout summaries stay local.

Booley deletes a finished, abandoned, or failed Goal Record, with its Session
Summary and Review Package, once both its worktree and its Goal Branch are gone.
A worktree is gone when it is absent from Git’s worktree registry: after
`git worktree remove`, or
after `git worktree prune` for a worktree directory you deleted by hand.
Booley starts a background cleanup after every Goal entry, Finish, and abandonment;
the command returns without waiting for Git checks or deletion. If the worker
cannot start or is interrupted, the next lifecycle command retries cleanup. A record is kept
whenever Git cannot confirm that the worktree or branch is gone, the record is
unreadable, or another command is using it. Committed summaries under `goals/history/`
are never deleted. Copy anything you want to keep before you remove both the
worktree and the branch.

### Recovery, unattended work, and parallel sessions

The same Goal session can work with your guidance or unattended. If intent is
missing or a Goal cannot be met, the Developer Agent explains what decision it needs.
After a crash or reconnection, ask it to call `goal_status(rules=true)` in the
same worktree before continuing. Saved proposal IDs and Finish operation IDs
allow recovery without duplicating a decision or completion. A Finish result
of `revalidation_required` needs fresh evidence and a new summary and operation ID.

Abandonment is available only on your explicit instruction, through
`goal_finish(abandon=true)` with your quoted words. Quiet presence or a Developer Agent
being stuck never abandons a Goal automatically. The branch and worktree remain.

For parallel work, use one linked worktree and Goal Branch per session. Start
another Developer Agent in another container terminal and invoke `/booley-goal` there.
Jobs share the Project's [admission caps](CONFIG.md#jobs--concurrency-jobs).
`booley dashboard` shows sessions, Goals, and Jobs inside the Sandbox; it opens
on VS Code folder attachment by default. Opt out with `[sandbox].dashboard=false`.
Each session shows what its agent client last reported: working, waiting for
tool call, or waiting for user input. Click a report, artifact, or evidence
path in a detail view to open it in the editor.
Press `s` for the session list, `j` for running Jobs, `r` for recent results,
or `a` for all Jobs. From a session detail, Job shortcuts show that session's
Jobs; press `u` to include all sessions and standalone Jobs. `Esc` returns to
the previous view, and `?` shows help.

Sessions have short, persistent Project handles such as `S17`. Jobs include
their kind: `J-SIM25`, `J-SYN34`, `J-LINT45`, or `J-FPGA33`; simulation baseline
Jobs use `SIM`, and other endpoints use `J-JOB<n>`. One Job counter covers every
kind, so numbers may skip within a kind. Handles survive Dashboard restarts
and session pruning. The `1`–`9` keys remain navigation shortcuts, independent
of handles. Full session keys and Job run IDs are secondary text in details;
MCP polling continues to use the full run ID.

Sessions receive handles when registered; Jobs receive them when created.
Historical records without allocations keep `S?` or `J-SIM?` (and the equivalent
for other kinds). Handle-storage failures show a diagnostic. The Dashboard
itself only reads handles and never allocates them.

For scripted inspection, `booley goal status` shows this worktree's Goal Mode,
or every active Goal Mode in the Project when this worktree hosts none. In a
worktree without an active Goal Mode, `booley goal status` and `goal_status`
also show its most recently ended Goal Record: Goal id, state, end time, the
failure reason of a failed record, and the Session Summary and Review Package
paths when those files exist.

### Entering the Sandbox without VS Code

**Reopen in Container** needs VS Code. Without it (in CI or for unattended
work), `booley session` starts the same container. Run these on
the **host**, after `booley init`:

```bash
booley session up                       # create or start the container
booley session enter                    # open a shell inside it
booley session enter -- booley doctor   # or run one command and exit
booley session status                   # running (with VS Code owner/name) | stopped | absent
booley session refresh                  # rebuild the image and recreate the container
booley session down                     # stop and remove a headless Sandbox
```

`booley session enter` works like a container terminal, so every
container-only command works through it. `Ctrl-C` on a command run with `--`
stops everything it started inside the container.

`session refresh` is safe to interrupt; the prior container stays usable until
the new one is ready. It won't replace a container that VS Code opened; use
**Dev Containers: Rebuild Container** for that. If your EDA tools need a
license server, run `booley session down` before refreshing.

`up` and `enter` reuse the current Sandbox, including one owned by VS Code.
`status` identifies a running VS Code Sandbox by owner and name. CLI `down`
and `up --rebuild` refuse editor ownership: close the window or stop it through Dev Containers,
and use **Dev Containers: Rebuild Container** to rebuild it.

## Auth & billing

The Developer Agent and Specialists use the provider set in `[agent] provider` in `booley.toml`:
`claude` (the default) or `codex`. Each one signs in the same way its own app
does, with either a **subscription** or an **API key**:

| Provider | Subscription | API key |
|---|---|---|
| `claude` | Claude Pro/Max/Team/Enterprise (your login in `~/.claude/.credentials.json`) | `ANTHROPIC_API_KEY` |
| `codex` | your Codex login (`codex login`, saved in `~/.codex/auth.json`) | `OPENAI_API_KEY` |

Both work in Interactive Mode and Goal Mode.

- **Subscription:** usage counts against your plan. Claude rate-limit events
  make Booley wait and retry a Specialist call within its retry and timeout
  budgets. Codex usage caps stop the call; it must be invoked again after the
  limit resets. Resume an interrupted Goal session with `goal_status(rules=true)`.

- **API key:** you pay per token.

If an API key is exported, it is used even when you also have a subscription.
To choose explicitly, set `[agent] auth = "subscription"` or `"api_key"`; see
[CONFIG.md](CONFIG.md#pinning-what-bills-agent-auth). `booley auth --status`
shows which login is in use.

**For long runs, run `booley auth`.** The normal login renews itself
periodically, and a renewal can sign out every running Developer Agent or Specialist in the middle of
a run ([details](TROUBLESHOOTING.md#developer-agents-and-specialists-turn-into-not-logged-in-partway-through-an-unattended-run)).
`booley auth` saves a login that doesn't need renewing:

| App | Login that doesn't expire | How |
|---|---|---|
| Claude | a one-year token | `booley auth` (it runs `claude setup-token` for you) |
| Codex | an API key | `booley auth --app codex`, then paste the key |

Codex has no long-lived subscription token, so for Codex this means paying per
token instead of using your subscription.

The saved login lives in `~/.config/booley/`, outside every project, so it can't
be committed by accident. Booley passes it to every container on its own,
including VS Code's; rebuild an existing container once to pick it up.

`booley init --skip-credentials` skips the login check, for CI machines that
have no login on purpose. Don't use it for normal setup.

## When Booley itself misbehaves

Maybe a Flow failed with no useful message, a doc describes a setting that
doesn't exist, or a message looks like a crash when nothing crashed. Type
**`/booley-feedback`** in the Developer Agent chat while the problem is still on screen.
It records how to reproduce it, checks Booley's own code before blaming it,
removes your project's names, and saves a report file for you to read and share
yourself. Booley never sends it anywhere.

It doesn't have to be a bug. Confusing behavior, praise, complaints, wishes, or
"this wasn't worth the setup cost" are all worth one sentence to
`/booley-feedback`. See [CONFIG.md](CONFIG.md#feedback-feedback).

In the Sandbox, each Project has one Findings Log shared by all of its worktrees.
The log and default local report stay in the Main Booley Directory, even
when a worktree has its own Paired Checkout. When the skill uses the
feedback CLI, its `-C` / `--project` option selects the checkout used for context;
it does not move that shared log or the default report. For Projects, the
`BOOLEY_DIR` environment variable selects shared storage. Outside the
Sandbox, without that override, storage is discovered from the selected checkout
and may use a separate log. Use the report paths provided by the skill.

## Booley Flows & Specialists

This section is reference. You rarely call Flows or Specialists yourself: ask
the Developer Agent (*"run the reset test on `sim_lite`"*, *"how much area did that
cost?"*) and it picks the Flow or Specialist, Target, and options. The **Sets** column shows
which [acceptance criteria](#working-with-evidence) each one can satisfy. Which
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

Every Flow's options and results are in [FLOW_REFERENCE.md](FLOW_REFERENCE.md), and its report files in [FLOW_REPORTS.md](../internals/FLOW_REPORTS.md); `booley flow <name> --help` prints the options too.

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

### Running a Booley Flow directly

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

In Goal Mode, add a [coverage Goal](CONFIG.md#native-coverage-configuration)
for each Target that needs one. Only valid coverage evidence satisfies it.
The Analyst may screen Waiver Candidates; approving one requires your decision
through `goal_propose_change`. Rerun coverage after approval to obtain fresh
strict evidence. See [coverage waivers at review](FLOW_REFERENCE.md#coverage-waivers-at-review).

## Goal catalog

Every supported Goal has the same name in status, proposals, the Dashboard and
Review Package. `{target}` is its bare name when unique, or its qualified selector otherwise.

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

## CLI reference

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
