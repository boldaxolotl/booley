# Usage

This guide shows how to use Booley day to day. You don't need any experience
with AI coding agents to follow it.

**Contents**

1. [Read this first](#read-this-first)
2. [Check your setup](#first-verify-your-setup)
3. [Choose how to work](#choose-how-to-work)
4. [Interactive Mode](#interactive-mode): [first session](#open-your-first-agent-session), [good prompts](#write-a-useful-prompt), [reviewing changes](#what-the-agent-is-allowed-to-do), [waveforms](#viewing-waveforms)
5. [Goal Mode](#goal-mode): [Goalsets](#goalsets-and-entry), [evidence](#working-with-evidence), [human decisions](#changes-and-human-decisions), [Finish](#finish-and-review)
6. [Recovery and parallel work](#recovery-unattended-work-and-parallel-sessions), [without VS Code](#entering-the-sandbox-without-vs-code)

7. [Auth & billing](#auth--billing)
8. [Reporting problems and feedback](#when-booley-itself-misbehaves)
9. Reference: [Flows & Specialists](#booley-flows--specialists), [Criteria catalog](#criteria-catalog), [CLI](#cli-reference)

## Read this first

This guide picks up after installation and project setup. It assumes that
`booley init` and the `booley-setup` skill have both finished. If they haven't,
install Booley using the [README](../../README.md#installation), then follow
[SETUP.md](SETUP.md).

**What Booley does.** Booley lets an AI coding agent, such as Claude Code or
Codex, run your project's EDA tools. A coding agent is more than a chatbot: it
can read and edit files, run commands, and call Booley while it works. You
describe what you want in plain language, and the agent picks the right Booley
Flow or Specialist and runs it. You are still responsible for reviewing its reasoning, its
code, and the hardware results.

Booley offers two kinds of capability:

- **Flows** (`sim`, `lint`, `synth`, `fpga`) run EDA tools in a fixed,
  predictable way. No AI is involved.
- **Specialists** (`reviewer`, `mutation_tester`, `coverage_analyst`) are small
  AI agents that each do one focused job.

For direct CLI reproduction, see [live Flow progress and logs](FLOW_REFERENCE.md#live-progress-and-logs), including quiet and verbose output.

Both run inside the **Sandbox**, a Docker container that holds the EDA tools
and keeps the agent away from the rest of your computer.

Sandbox limits and network egress for both execution modes are host-owned
`[sandbox]` settings; see [Host configuration](CONFIG.md#host-configuration-configtoml).

**Where you type.** This guide uses three places:

| Place | What goes there | Example |
| --- | --- | --- |
| **Host terminal** | Commands on your own computer, outside Docker | `booley doctor`, `code .` |
| **Container terminal** | Commands inside the Sandbox, after VS Code has reopened the project in its container | `booley dashboard`, `git diff` |
| **Agent chat** | Plain-language requests and skills that start with `/` | *"Run lint and explain every finding"* |

Command blocks go in the terminal named in the text around them. Sentences in
*italics* are prompts for the agent chat. Skills such as
`/booley-goal` also go in the agent chat, not in a terminal.

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
`auth --status` and `cheat --list` accept and ignore it without discovering a Project.
`bootstrap`, `projects`, and `eda` retain their grammar
and do not accept the new selector. Full option names are stable; ambiguous
abbreviations produce an error. Arguments after a command's payload `--` stay opaque.

Selection chooses the checkout, while `BOOLEY_PROJECT_DIR` chooses Project data;
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
booley cheat           # a one-page summary of Booley's commands and files
```

Don't continue until `booley doctor` shows no failures or warnings.

- If `booley` is not found, go back to the
  [installation instructions](../../README.md#installation).
- If Booley says its version changed, type `/booley-heal` in the agent chat.
- For a quick overview of Booley, start with `booley cheat`. It's long, so
  `booley cheat --list` shows its sections and you can print just the ones you
  need, for example `booley cheat --goals` or `booley cheat --commands --project-files`.
- `booley doctor --deep` goes further and runs short real simulations, lints,
  and syntheses.

## Choose how to work

Start with Interactive Mode to explore and run individual Flows. Use
`/booley-goal` in the same Sandbox agent chat when a task needs mandatory
completion conditions. Goal Mode supports both human guidance and unattended
work, with one linked worktree and Goal Branch per session.

## Interactive Mode

### Open your first agent session

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
   the agent:

   ```bash
   booley
   ```

   `booley` (or `booley chat`) just starts the agent chosen in
   `[agent].provider` in `.booley_project/booley.toml`:

   | Provider | Same as running |
   | --- | --- |
   | `claude` | `claude` ([Claude Code](https://code.claude.com/docs/en/quickstart)) |
   | `codex` | `codex` ([Codex CLI](https://developers.openai.com/codex/cli)) |

   From then on you are using that agent's own chat. To pass it extra options,
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

Brief the agent the way you would brief an engineer joining the task:

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

### What the agent is allowed to do

Inside the container, the agent **never asks for permission** before acting.
That is deliberate: the container itself is the safety boundary. It can only
see your project, and its only network access is to the AI provider. Booley
does not change your agent settings outside the container.

**Always review changes yourself.** Use `git diff` in a container terminal and
run the relevant checks. Asking the agent to explain its diff helps, but it
doesn't replace your own review.

> **Tip: let the agent commit; you push.** Let the agent commit its own work;
> it writes good commit messages. It **can't** push to GitHub or any other
> server, because the container blocks that network access. So: let it commit,
> review the commits, then push them yourself from a terminal outside the
> container.

Per-session logs of Flow and Specialist calls through Booley’s tool server
are saved in `.booley_project/.interactive_logs/`. Chat transcripts live in
the agent CLI’s own session storage. If the
agent can't reach Booley (`booley` is missing from its `/mcp` list), see
[TROUBLESHOOTING.md](TROUBLESHOOTING.md#booley-is-missing-from-mcp-in-claude-code-or-codex).
How the connection works is in
[ARCHITECTURE.md](../internals/ARCHITECTURE.md#interactive-mode).

### Viewing waveforms

When a simulation with tracing fails, the agent reads the waveform on its own
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

Goal Mode gives one agent session mandatory, evidence-backed completion
conditions. Open a Sandbox agent session and type `/booley-goal` with the work
you want done. The skill helps you choose a clean linked worktree, Goalsets,
Targets, and ad-hoc Goals, then calls `goal_enter`. Booley creates a Goal Branch
from the worktree's current HEAD and records that base for comparisons.

### Goalsets and entry

Goalsets are Project-owned Markdown files under `.booley_project/goalsets/`.
`booley init` creates missing `feature.md`, `bugfix.md`, `refactor.md`, and
`verification.md` without rewriting existing files. Customize their prose to
express your project's rules. The agent translates them into concrete Goals;
Booley validates the arguments, not the prose. Each Goal records its origin.

An optional `default.md` applies to every entry. Skipping it requires your
explicit instruction and reason. Setup can create this file; initialization
does not seed it. You can also add Goals for this particular change. Duplicate
Goals merge to the stricter condition, and entry reports every merge.

A Goal names a Flow or review family and the applicable Target, tests, and
thresholds. Every Goal is mandatory. For example, a Goalset excerpt can ask for:

```json
[
  {"family": "lint", "target": "lint_fifo"},
  {"family": "sim", "target": "sim_fifo"},
  {"family": "review", "review": "rtl_bugs", "verdict": "clean"},
  {"family": "synth", "target": "synth_fifo", "thresholds": {"area_increase_at_most": "10%"}}
]
```

Use the [Criteria catalog](#criteria-catalog) and `booley cheat --criteria` for
the evidence keys and threshold parameters used by Goals. A new Target can be
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
{"family": "synth", "target": "asic_small", "baseline": "asic_base",
 "thresholds": {"cell_count_reduce_at_least": "8%"}}
```

```json
{"family": "fpga", "target": "fpga_top", "thresholds": {"lut_count_max": 5000}}
```

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

**Per-test `CYCLE_COUNT`**

`cycle_count` Goals name the Target with `target` and the registered test with `test`; put bounds in `thresholds`. All thresholds for that test must pass. Relative forms compare the same Target/test at the Goal base commit.

```json
{"family": "cycle_count", "target": "sim", "test": "smoke",
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

A named `[SIM_CYCLES] <test> <count>` observation is gated evidence only when that exact test passes. Missing, malformed, duplicate, unnamed, failed, or inconclusive evidence fails closed. Without a `cycle_count` Criterion, existing Cycle Count records remain observational.

Relative comparisons report an **observed Cycle Count change**. When declared workload inputs differ, review reports disclose the changes and do not attribute the result to RTL alone.
<!-- END GENERATED: criteria-params -->

### Working with evidence

The agent passes the absolute worktree root as `work_dir` on every Booley call.
Only Booley Flows and Specialists meet Goals. Shell output or the agent saying
"done" does not. `goal_status` shows each Goal's current evidence and freshness;
code changes can require another run. For a bugfix, reproduce the reported bug
with a failing test before editing the design.

Protected Inputs decide how evidence is produced: `booley.toml`,
`FUSESOC_IGNORE`, and the Project's `hooks`, `.managed`, `generators`, and
`mcp_tools` directories. Editing one blocks Finish until reverted and discards
affected evidence. Never weaken tests or Targets to make a Goal pass. The
Review Package flags `.core`, `tests.toml`, and `.sdc`/`.xdc` edits.

### Changes and human decisions

When a Goal needs to be added, relaxed, retargeted, or changed through a coverage
waiver, the agent uses `goal_propose_change`. You see the exact proposal and
approve or reject it with a reason. The client form is used when available;
chat fallback records your quoted instruction against the saved proposal ID.
Silence cannot approve a change. Approved changes appear in the Change Log.
Coverage Waiver Candidates need your decision; the Analyst cannot approve them.

### Finish and review

Finish requires all Goals met with fresh evidence at a clean, committed HEAD
and a Session Summary. The agent calls `goal_finish` with the record ID, a
stable operation ID, and that summary. The Review Package contains the diff
against the base, final Goals and evidence, the Change Log, open review
findings, Target changes, constraint edits, and the Session Summary.

A `clean` review requires no open findings. A `done` review is advisory:
findings remain in the package and do not require a second approval to Finish.
Read the package before deciding what to merge. Completion keeps the Goal
Branch and worktree; merge, publication, and cleanup need separate instructions.

Local Goal Records live under `.booley_project/goals/<goal-id>/`. Outside
Stealth, Finish commits the Session Summary under
`.booley_project/goals/history/<goal-id>.md` in the Goal worktree when that
path is Git-trackable. Stealth or excluded summaries stay local.

### Recovery, unattended work, and parallel sessions

The same Goal session can work with your guidance or unattended. If intent is
missing or a Goal cannot be met, the agent explains what decision it needs.
After a crash or reconnection, ask it to call `goal_status(rules=true)` in the
same worktree before continuing. Saved proposal IDs and Finish operation IDs
allow recovery without duplicating a decision or completion. A Finish result
of `revalidation_required` needs fresh evidence and a new summary and operation ID.

Abandonment is available only on your explicit instruction, through
`goal_finish(abandon=true)` with your quoted words. Quiet presence or an agent
being stuck never abandons a Goal automatically. The branch and worktree remain.

For parallel work, use one linked worktree and Goal Branch per session. Start
another agent in another container terminal and invoke `/booley-goal` there.
Jobs share the Project's [admission caps](CONFIG.md#jobs--concurrency-jobs).
`booley dashboard` shows sessions, Goals, and Jobs inside the Sandbox; it opens
on VS Code folder attachment by default. Opt out with `[sandbox].dashboard=false`.
Inspect records with `booley goal status`.

### Entering the Sandbox without VS Code

**Reopen in Container** needs VS Code. Without it (in CI, or when a script or
agent drives Booley), `booley session` starts the same container. Run these on
the **host**, after `booley init`:

```bash
booley session up                       # create or start the container
booley session enter                    # open a shell inside it
booley session enter -- booley doctor   # or run one command and exit
booley session status                   # running | stopped | absent
booley session refresh                  # rebuild the image and recreate the container
booley session down                     # stop and remove it
```

`booley session enter` works like a container terminal, so every
container-only command works through it. `Ctrl-C` on a command run with `--`
stops everything it started inside the container.

`session refresh` is safe to interrupt; the prior container stays usable until
the new one is ready. It won't replace a container that VS Code opened; use
**Dev Containers: Rebuild Container** for that. If your EDA tools need a
license server, run `booley session down` before refreshing.

## Auth & billing

Booley's AI agents use the provider set in `[agent] provider` in `booley.toml`:
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
periodically, and a renewal can sign out every running agent in the middle of
a run ([details](TROUBLESHOOTING.md#agents-turn-into-not-logged-in-partway-through-an-unattended-run)).
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
**`/booley-feedback`** in the agent chat while the problem is still on screen.
It records how to reproduce it, checks Booley's own code before blaming it,
removes your project's names, and saves a report file for you to read and share
yourself. Booley never sends it anywhere.

It doesn't have to be a bug. Confusing behavior, praise, complaints, wishes, or
"this wasn't worth the setup cost" are all worth one sentence to
`/booley-feedback`. See [CONFIG.md](CONFIG.md#feedback-feedback).

## Booley Flows & Specialists

This section is reference. You rarely call Flows or Specialists yourself: ask
the agent (*"run the reset test on `sim_lite`"*, *"how much area did that
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
| `sim` | Run RTL simulation for one or more Targets | — |
| `lint` | Run lint for one or more Targets | `lint_clean` |
| `synth` | Run ASIC synthesis for one or more Targets with optional baseline comparison | `synthesis_ok` |
| `fpga` | Run FPGA implementation for one or more Targets with optional baseline comparison | `fpga_impl_ok` |

Every Flow's options and results are in [FLOW_REFERENCE.md](FLOW_REFERENCE.md), and its report files in [FLOW_REPORTS.md](../internals/FLOW_REPORTS.md); `booley flow <name> --help` prints the options too.

**Specialists**

LLM-backed sub-agents running in scoped, isolated workspaces:

Ask your connected agent session to invoke a Specialist by name with the arguments below. Or run `booley specialist <name> [args...]` inside the Sandbox. `booley specialist` lists visible Specialists; `booley specialist <name> --help` shows their arguments. `--model`, `--max-turns`, and `--timeout` are CLI-only controls.

For example: `booley specialist reviewer --category rtl --focus bugs --scope rtl`. The supported module alternative is `python -m booley.specialists.reviewer` with the same flags. Common options are `-C/--project PATH`, `--report-dir`, `--diagnostic`, and `--target` where supported. `--timeout DURATION` accepts positive seconds (`90` or `90s`), minutes (`30m`), hours (`2h`), and combinations (`1h30m`). Model-call minimum budgets apply; seconds-only providers round up.

| Specialist | Purpose | Sets | Modifies code |
|------------|---------|------|:-------------:|
| `coverage_analyst` | Explain one exact coverage.json Campaign and propose advisory next steps | — | — |
| `mutation_tester` | Proposal-locked mutation testing: creator selects exact replacements, tester builds isolated variants | `mutation_score` | — |
| `reviewer` | Single-focus code review: reports issues by severity | `review_*` | — |

#### `coverage_analyst`

Call the `coverage_analyst` Specialist from your connected agent session with `campaign="<exact-coverage.json>"` and optional `instruction="<question>"`. The Analyst explains retained native evidence and proposes advisory next steps; its model only reads evidence. It does not run Simulation, read waveforms, evaluate Criteria, or approve waivers. In Goal Mode, screened Waiver Candidates support a proposed Goal change, which needs human approval. Finish requires every Goal met; advisory reports do not mark strict coverage met. Verified Target sources are optional; stale sources give report-only analysis.

#### `reviewer`

Read-only, single-focus code review. It reports `CRITICAL`, `MAJOR`, and `MINOR` findings. In Interactive Mode, review the selected files using your specification or steering. In Goal Mode, `done` reports findings without requiring fixes; the review package includes open findings. `clean` requires every finding to be fixed or waived with a justification.

The result links saved review evidence, including rejected proposals for inspection. Rejected proposals do not affect Criteria.

Call the `reviewer` Specialist from your connected agent session with `scope="<file,...>"`, `category="<category>"`, and `focus="<focus>"`.

| Category | Focus | What it checks | Sets |
|----------|-------|----------------|------|
| `rtl` | `bugs` | Functional bug patterns, synthesis hazards, reset/width/signing mistakes, and ifdef/config consistency | `review_rtl_bugs` |
| `rtl` | `protocol` | Bus/protocol rule compliance, handshake behavior, ordering, and clock-domain crossings (CDC) | `review_rtl_protocol` |
| `rtl` | `spec` | Spec compliance: the RTL implements what the change request/spec requires, no more and no less | `review_rtl_spec` |
| `rtl` | `code_style` | Comments, naming, readability, maintainability, magic values, and assertion/cover-point quality | `review_rtl_code_style` |
| `rtl` | `optimization` | Unused/dead RTL and strict power/performance/area improvements with no functional or engineering trade-off | `review_rtl_optimization` |
| `rtl` | `security` | Fault-injection resistance, simple power/timing leakage, secret exposure, and unsafe failure behavior | `review_rtl_security` |
| `tb` | `quality` | False-pass paths in scoped testbench sources, missing checks and edge cases, coverage gaps, timing/sampling mistakes, and TB code quality | `review_tb_quality` |

Arguments: required `scope="<file,...>"` selects files; `steer=["<context>"]` adds review context; `dry_run=true` validates and previews without invoking an agent. The `spec` focus needs specification text: use `spec="<path>"` or the spec file named by the Review Goal.

#### `mutation_tester`

Proposal-locked mutation testing. A read-only LLM creator returns exact source replacements; Booley runs a pristine baseline, then compiles and tests each replacement in isolation. It does not parse HDL or inject runtime selectors.

**Mutation campaign modes:**

| Campaign | Goal arguments | Interactive Mode arguments |
|----------|-----------------------------------------|------------------------|
| Default fixed | Target campaign with `target` + `scope` — generate 10 mutations and require all 10 detected | _(no goal arguments)_ — the same 10-of-10 campaign |
| Explicit fixed | add `total: N` and `min_detected: K` | `count="N"` requires all N; add `min_detected=K` to require K |
| Size-scaled | add `auto: true` — choose 3-25 mutations from language-neutral source size and the time budget | `count="auto"`; add `min_detected=K` for an explicit threshold |

`dry_run=true` validates Target metadata and prints the source-size breakdown and proposed auto count without invoking an agent or simulator.

Call the `mutation_tester` Specialist from your connected agent session: `scope="<rtl-file,...>"` chooses mutation sites; `target="<sim-target>"` chooses exactly one complete runnable Target suite; `steer=["<context>"]` biases mutation selection. A valid lock is reused on later runs, so new steering takes effect only with `regen_lock=true`. The Target supplies the testbench top and complete RTL closure; they are not separate caller inputs.
<!-- END GENERATED: flows -->

`tb_coder` also exists but is hidden until it is ready (see
[ROADMAP.md](../internals/ROADMAP.md)); for now, the agent writes testbenches
itself.

### Running a Booley Flow directly

Running a Flow yourself is useful when checking your setup or reproducing a
problem. In a container terminal:

```bash
booley specialist                            # list visible Specialists
booley specialist reviewer --help            # shared and Specialist arguments
booley flow                                  # list the Flows
booley targets --for sim                     # Targets a Flow can use
booley flow sim --target sim_soc --test reset
booley flow sim --help                       # every option
```

[FLOW_REFERENCE.md](FLOW_REFERENCE.md) has the full details: options, exit
codes, and results. [FLOW_REPORTS.md](../internals/FLOW_REPORTS.md) defines the
report files.

**Coverage.** Coverage is collected only when you ask for it. Then pass the
result to the Coverage Analyst for an explanation:

```bash
booley flow sim --target sim_counter --coverage
```

Then call the `coverage_analyst` Specialist from your connected agent session with
`campaign="<reports>/sim/12/targets/sim_counter/coverage.json"`.

In Goal Mode, add a [coverage Goal](CONFIG.md#native-coverage-configuration)
for each Target that needs one. Only valid coverage evidence satisfies it.
The Analyst may screen Waiver Candidates; approving one requires your decision
through `goal_propose_change`. Rerun coverage after approval to obtain fresh
strict evidence. See [coverage waivers at review](FLOW_REFERENCE.md#coverage-waivers-at-review).

## Criteria catalog

Every built-in Criterion under the name Booley uses in evidence reports.
Goals use these keys after entry translates their family arguments.
`{target}` means one Criterion per Target, for example `sim_pass_sim_core`.
Criteria are defined in `criteria.toml`, where custom tools can add their own.

<!-- BEGIN GENERATED: criteria -->
#### Build & Elaborate

| Criterion | Description | Set by | Workflow Region |
|-----------|-------------|--------|-------|
| `elab_pass_{target}` | RTL/TB compiles and elaborates cleanly (no simulation) | `sim` (`mode="elab-only"`) | pre-sim |
| `elaborate_standalone` | Every module in the Targets' RTL source scope elaborates standalone from its declaring file (shared package/interface files auto-included, parameter defaults) | `sim` (`mode="elab-only-standalone"`) | pre-sim |
| `lint_clean_{target}` | The Target's linter passes with no unwaived findings | `lint` | pre-sim |

#### RTL Code Review

| Criterion | Description | Set by | Workflow Region |
|-----------|-------------|--------|-------|
| `review_rtl_bugs` | RTL review: bug patterns, synthesis hazards, and ifdef/config consistency (the RTL as hardware, not against the spec) | `reviewer` (`category="rtl"`, `focus="bugs"`) | pre-sim |
| `review_rtl_protocol` | RTL review: bus/protocol compliance and clock-domain crossings (CDC) | `reviewer` (`category="rtl"`, `focus="protocol"`) | pre-sim |
| `review_rtl_spec` | RTL review: spec compliance (RTL matches the Goal/spec, no more, no less) | `reviewer` (`category="rtl"`, `focus="spec"`) | pre-sim |
| `review_rtl_code_style` | RTL review: comments, naming, readability, and assertion coverage (post-sim) | `reviewer` (`category="rtl"`, `focus="code_style"`) | post-sim |
| `review_rtl_optimization` | RTL review: unused/dead code and missed power/performance/area wins, strict improvements only (post-sim) | `reviewer` (`category="rtl"`, `focus="optimization"`) | post-sim |
| `review_rtl_security` | RTL review: hardware attack resistance to fault injection, simple power/timing analysis, and secret exposure (post-sim) | `reviewer` (`category="rtl"`, `focus="security"`) | post-sim |

#### Testbench Review

| Criterion | Description | Set by | Workflow Region |
|-----------|-------------|--------|-------|
| `review_tb_quality` | Source-scoped TB review: false-pass detection, coverage gaps, and TB code quality | `reviewer` (`category="tb"`, `focus="quality"`) | pre-sim |

#### Simulation

| Criterion | Description | Set by | Workflow Region |
|-----------|-------------|--------|-------|
| `cycle_count_{target,test}` | A named test passes and its observed Cycle Count meets every declared threshold | `sim` | sim loop |
| `sim_pass_{target}` | RTL simulation passes all tests | `sim` | sim loop |

#### Coverage

| Criterion | Description | Set by | Workflow Region |
|-----------|-------------|--------|-------|
| `coverage_{target}` | Native Coverage Campaign policy for one Simulation Target | `sim` (`coverage=true`) | post-sim |

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

## CLI reference

`booley --help` marks every command as `[host]`, `[Sandbox]`, `[either]`, or
`[mixed]`. Sandbox commands run in a container terminal or through
`booley session enter`.

```bash
# Goal inspection and Dashboard (Sandbox)
booley goal status                # this worktree's Goal Mode, or every active Goal Mode in the Project when this worktree hosts none
booley dashboard                  # sessions, Goals, and Jobs

# Worktrees (container)
booley worktree new <name>        # .booley_project/worktrees/<name>, paired Project or clean snapshot

# Quick reference
booley cheat                      # the whole cheatsheet
booley cheat --list               # its section names
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

`booley worktree new <name>` gives a versioned `.booley_project` repository a
paired checkout on `booley-worktree/<name>` at its current HEAD. Commit tracked
changes and untracked files in the Project repository first; an unborn
repository and an existing Project branch are refused. Ignored state stays
behind. A non-versioned Project gets a clean snapshot. Run this command from
the primary workspace; creating from a paired Project checkout is refused.
The outer checkout starts detached unless `<branch>--<description>` selects an
existing branch. To remove a paired workspace, remove the Project checkout
first with `git -C <absolute-project-dir> worktree remove <absolute-worktree>/.booley_project`,
then run `git -C <absolute-workspace-root> worktree remove <absolute-worktree>`.
To reuse the name, preserve any Project commits you need, then delete its branch
with `git -C <absolute-project-dir> branch -D booley-worktree/<name>`.
The command prints these steps with absolute paths.

`booley projects discover` stops descending at each initialized Project to
avoid scanning its RTL, vendor, and build trees. Nested Projects are not imported
by that scan; run `booley projects discover <nested path>` on a nested Project
directly to import it.

Booley also runs a quick health check on its own when the container starts and
on supported command and Flow paths: about once a week, daily while problems remain, and
whenever the configuration changes. It never blocks work. New problems show up
in `booley session up` and the next Flow result. The last result
is in `.booley_project/runtime/doctor/last.log`.

Plain Doctor and `booley session up` also report whether prior deep validation is
current or due. Deep evidence has no expiry; automatic change triggers are only
the Booley version and active immutable Sandbox Image. Project/Target/RTL edits
leave that evidence intact. Deep due is advisory and never launches deep checks.
See [Doctor health and deep validation](DOCTOR.md) for qualification, failed and
cancelled attempts, and image identity handling.

Use `booley cheat --goals` for the Goal lifecycle and MCP tools.
