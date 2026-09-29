# Usage

How to drive Booley day to day. No previous experience with LLM agents is
assumed.

**Contents**

- [Read this first](#read-this-first)
- [First, verify your setup](#first-verify-your-setup)
- [Choose a mode](#choose-a-mode)
- [Interactive Mode](#interactive-mode): [first session](#open-your-first-agent-session), [prompts](#write-a-useful-prompt), [permissions](#what-the-agent-is-allowed-to-do)
- [Booley Flows & Specialists](#booley-flows--specialists): [running a Flow directly](#running-a-booley-flow-directly), [waveforms](#viewing-waveforms)
- [Ticket-Driven Workflow](#ticket-driven-workflow): [creating Tickets](#creating-tickets), [Board lifecycle](#ticket-board-lifecycle), [Acceptance Criteria](#acceptance-criteria), [`on_success`](#where-the-work-lands-on_success)
- [Running Unattended](#running-unattended)
- [Scope](#scope)
- [Push Notifications](#push-notifications)
- [Feedback and bug reports](#when-booley-itself-misbehaves)
- [CLI reference](#cli-reference): [concurrent tickets](#concurrent-tickets)
- [Auth & billing](#auth--billing)

## Read this first

This guide starts after installation and project setup. It assumes `booley
init` and the `booley-setup` skill have finished successfully; if they have not,
install Booley from the [README](../../README.md#installation), then follow
[SETUP.md](SETUP.md).

Booley gives an LLM coding agent access to your project's configured EDA flows.
Unlike a plain chatbot, a coding agent can inspect files, edit them, run shell
commands, and call Booley Flows and Specialists while it works. In practice, you open an agent
such as Claude Code or Codex, describe the result you want in ordinary language,
and let the agent choose and run the appropriate Booley capability. You remain
responsible for reviewing its reasoning, code, and hardware results.

There are three places where you may type during this guide:

| Place | What goes there | Example |
| --- | --- | --- |
| **Host terminal** | Commands on your normal computer, outside Docker | `booley doctor`, `code .` |
| **Container terminal** | Shell commands after VS Code has reopened the project in its devcontainer | `booley run`, `git diff` |
| **Agent chat** | Natural-language requests and slash-prefixed skills | `Run lint and explain every finding` |

If a block begins with a command such as `booley` or `git`, type it in the
terminal named by the surrounding text. Italicized sentences such as *"Run lint
and explain every finding"* are prompts to type in the agent chat. A skill
invocation such as `/booley-ticket-create` is also typed in the **agent chat**,
not in a shell.

Booley's domain terms have precise definitions in the canonical controlled
vocabularies indexed by the [context map](../../CONTEXT-MAP.md). Refer to the
owning glossary whenever a term is unfamiliar; this guide does not repeat those
definitions.

## First, verify your setup

Before either mode, a newcomer's literal first commands. These run on the
**host** (no container needed) and confirm Booley is wired up and shows you what
it can see:

```bash
booley doctor          # static health check: config, image, toolchain
booley targets         # every .core Target Booley can see, grouped by core
booley cheat --list    # the cheatsheet's sections, each printable on its own
```

If Booley reports that its version changed, invoke `/booley-heal` in your agent
chat.

For the fastest orientation, start with `booley cheat`. It gives a compact
overview of every public CLI command, the editable `.booley_project` files,
Flows, Specialists, Criteria, Targets, skills, artifacts, and Sandbox commands.
Print the whole sheet or use `booley cheat --list` and combine section flags,
such as `booley cheat --board` or `booley cheat --commands --project`.

`booley doctor --deep` goes further and runs real smoke sims/lints/synthesis
inside the Sandbox; other Doctor flags are in the [CLI reference](#cli-reference).

If `booley` is not found, return to the [installation instructions](../../README.md#installation).
Do not continue into the
container until plain `booley doctor` has no unresolved failures or warnings.

## Choose a mode

Booley has two ways to work. Both use the same project configuration, Booley Flows, Specialists, and
Sandbox. For newcomers, they are a progression rather than an either-or
choice:

1. **Start with Interactive Mode.** Work through the first session below even
   if you already use coding agents. The live conversation lets you see how a
   Booley session selects Targets, invokes Booley Flows and Specialists, reports
   evidence, and responds to your direction. Continue interactively until those
   mechanics and their artifacts are familiar.
2. **Then move to Ticket Mode.** Once you understand what Booley does during a
   session, use a written Ticket to give the same machinery clear Scope and
   Criteria and let `booley run` drive the work autonomously. This becomes the
   recommended path for well-defined development work.

Interactive Mode remains useful for investigations, ad-hoc changes, and
individual Booley Flow or Specialist runs. Ticket Mode does not require you to keep
Claude Code or Codex open: `booley run` launches the configured agents itself.

## Interactive Mode

Interactive Mode is the onboarding workflow and the place to explore Booley
directly. Once setup has finished, Booley's Flows (`sim`, `lint`, and so on)
and Specialists (`reviewer`, `mutation_tester`) are available to Claude Code or
Codex.

### Open your first agent session

These steps begin on your normal computer:

1. Open a terminal, change to the RTL repository, and launch VS Code:

   ```bash
   cd path/to/your-rtl-project
   code .
   ```

   If your shell says `code` is not found, open VS Code normally and select
   **File → Open Folder** instead.

2. Accept VS Code's **Reopen in Container** notification. If it does not
   appear, open the Command Palette (`Ctrl+Shift+P`) and select **Dev
   Containers: Reopen in Container**. Wait for the window to reload. The first
   start may take several minutes. The remote indicator in the lower-left
   corner should then identify a Dev Container.

3. In the reloaded VS Code window, select **Terminal → New Terminal**. This is
   now a **container terminal**. Booley's sandbox image already contains both
   agent CLIs. Start the provider selected in
   `.booley_project/booley.toml`:

   ```bash
   booley
   ```

   **We recommend the CLI for Interactive Mode.** Bare `booley` is the short
   form of `booley chat`: both are convenience launchers for the Project's
   `[agent].provider` setting in `.booley_project/booley.toml`:

   | Provider | Equivalent command in the container terminal |
   | --- | --- |
   | `claude` | `claude` ([Claude Code](https://code.claude.com/docs/en/quickstart)) |
   | `codex` | `codex` ([Codex CLI](https://developers.openai.com/codex/cli)) |

   Think of `booley` / `booley chat` as an alias for the selected command.
   Booley replaces itself with that CLI; you use the agent's native chat,
   commands, and controls. To pass agent-specific options, invoke `claude` or
   `codex` directly. `booley --help` shows Booley's own command reference.
   For concurrent Interactive sessions, open a separate container terminal
   and launch the CLI in each one.

   If you prefer a chat panel, you can use the Claude Code or Codex VS Code
   extension instead. Booley's devcontainer configuration installs the
   extension for the selected provider inside the container. Open its chat
   panel in this reloaded VS Code window; there is no CLI command to run.

   If the CLI shows a login screen instead of a chat, open a separate **host
   terminal** and run `booley auth --status`. Follow its guidance (usually
   `booley auth`), then use **Dev Containers: Rebuild Container** from the VS
   Code Command Palette before trying again. See
   [Auth & billing](#auth--billing).

4. The agent opens an interactive chat in the terminal or side panel. Type this
   safe first request into that chat:

   > Check whether Booley Interactive Mode is ready. List the available
   > simulation targets and explain what each one is for. Do not change files.

   The agent should call Booley's `booley_status` and target-listing MCP tools and
   summarize the result. You do not type those MCP tool calls yourself.

5. If the previous response listed a simulation Target, try one real EDA run:

   > Run the tests on the most appropriate simulation target. Do not edit any
   > files. Tell me what ran, whether it passed, and where the detailed report
   > was written.

   A useful agent response states which Target and Booley Flow it chose, reports a
   pass, design failure, or infrastructure failure, and explains the next
   action. If the project has no simulation Target, ask it to run lint instead.
   Ask follow-up questions exactly as you would ask another engineer.

You have now completed an Interactive Mode session. The rest of this document
explains the available capabilities and the autonomous ticket workflow; you do
not need to learn all of it before continuing to use natural-language prompts.

For example:

- *"Debug the backpressure test failure on the sim_heavy target."*
- *"Compare synth area between the following commits ..."*
- *"Run a security review on the control unit module."*

You do not need to know the exact Booley Flow command, flags, report locations, or
MCP tool names before asking. Give the agent the engineering goal and any important
constraints; it can inspect the configured Targets and choose the mechanics.

### Write a useful prompt

Talk to the agent as you would brief an engineer joining the task. Include what
you want to learn or change, the relevant Target or module if you know it, any
constraints, and what evidence you expect. If you only want investigation, say
**do not edit files** explicitly.

For example:

> The `ready` signal sometimes remains low after reset on the `sim_full`
> Target. Reproduce the failure, inspect the waveform, and explain the likely
> cause. Do not edit files yet. Show me the evidence and propose a fix.

You can refine the request after the agent responds. You do not need to restart
the session when it chooses the wrong direction; tell it what assumption was
wrong or what evidence you want next.

### What the agent is allowed to do

Claude Code and Codex sessions inside the container start with **no approval
prompts** and no inner CLI sandbox. That's the point of the container — it is
cap-dropped, mounts only your project, and reaches nothing but the LLM API
through the Booley proxy, so the prompts would be guarding a box that is already
the guard. The in-container registrar pins this on every container start
(`bypassPermissions` for Claude; `approval_policy = "never"` plus
`sandbox_mode = "danger-full-access"` for Codex). Claude users can press
`shift+tab` to step a session back down to auto/plan/default. Nothing is written
to your **host** Claude Code or Codex settings.

Transcripts land in `.booley_project/.interactive_logs/<session-id>/`
(gitignored). How registration works and the session
lifecycle mechanics are in
[ARCHITECTURE.md](../internals/ARCHITECTURE.md#interactive-mode). If `booley` doesn't show
up in `/mcp`, see
[TROUBLESHOOTING.md](TROUBLESHOOTING.md#booley-is-missing-from-mcp-in-claude-code-or-codex). `/mcp` is
the client screen that lists MCP (Model Context Protocol) connections—the
connections through which an agent sees external MCP tools such as Booley.

Before accepting code changes, inspect them in the container terminal with
`git diff` and run the relevant checks. You can also ask the agent to explain
the diff, but that is not a substitute for engineering review.

> **Tip: let the agent commit, you push.** In Interactive Mode, let the agent
> commit its own work. It saves you the time of writing proper commit messages,
> and there's little to gain from doing it by hand. What the agent **can't** do
> is push to a Git server outside the Sandbox: default-deny egress blocks
> that network access. Container-local repositories and local-path remotes,
> including one named `origin`, remain writable sandbox state. So the loop is:
> let the agent commit, review the commits, then push them yourself from a
> terminal outside the Booley sandbox. (In Ticket Mode this isn't a choice: the
> agent always commits, since that's how a ticket's work is recorded and moved
> to review; the same external-server boundary applies.)

## Booley Flows & Specialists

These are the built-in capabilities both modes share. **Booley Flows** are
predictable wrappers around EDA tools; **Specialists** are focused LLM agents.

You normally do not call either one manually. Say *"run the reset test on the
`sim_lite` Target"* or *"how much area did that cost?"*, and the agent picks the
capability, Target, and flags. The table is worth a skim because it shows the
complete set of built-in capabilities. Every Booley Flow and Specialist runs
inside the Sandbox. **Sets** names the acceptance criteria that the
Booley Flow or Specialist can satisfy in a ticket.

Which EDA program runs underneath is determined by the Target. The currently
supported programs are tracked in [SUPPORTED-EDA-TOOLS.md](SUPPORTED-EDA-TOOLS.md).

The catalogs are generated from the MCP tool registry. `booley cheat --flows` and
`booley cheat --specialists` print them live as separate sections.

<!-- BEGIN GENERATED: flows -->
**Booley Flows**

Deterministic end-to-end orchestration; no LLM:

| Booley Flow | Purpose | Sets |
|--------|---------|------|
| `sim` | Run RTL simulation for one or more Targets | — |
| `lint` | Run lint for one or more Targets | `lint_clean` |
| `synth` | Run ASIC synthesis for one or more Targets with optional baseline comparison | `synthesis_ok` |
| `fpga` | Run FPGA implementation for one or more Targets with optional baseline comparison | `fpga_impl_ok` |

Common controls: repeat `--target` or use comma-separated values; `--target a --target b,c` preserves the order `a`, `b`, `c`, and duplicate resolved Targets are rejected. MCP keeps one comma-separated `target` string. `--dry-run` returns a normalized plan without executing EDA; `booley flow <name> --help` shows the full contract.

Key Flow-specific controls:

- `sim`: `--mode elab-only` compiles, elaborates, and links without running tests; `--mode elab-only-standalone` adds the stronger module sweep. Repeat `--test <name>` for an exact ordered suite or use `--tests-file <path>`; there is no CLI `--skip`, and configured skips apply only to unfiltered selection. MCP passes the same suite as a `test` array. Resume one exact durable Simulation Campaign with `--resume-from <exact-manifest.json>` (optionally with `--dry-run`): Cocotb retries the whole batch and coverage retries the whole aggregate into a distinct nested Coverage Campaign. `--coverage` / `--cov` collects a native Coverage Campaign, and `--trace` captures waveforms for the simulation run. Focused Cocotb output summarizes unselected skips; pass `--result-verbosity full` to print every XML testcase entry (the complete XML and JSON artifacts are always retained)
- `lint`: `--scope <file,...>` filters reported findings to selected files
- `synth`: `--baseline <ref>` compares metrics against a git revision; physical Targets must own an SDC fileset that creates a clock
- `fpga`: `--baseline <ref>` compares metrics against a git revision; `--ppa-profile compact|balanced|max_frequency` selects portable optimization intent; `--no-cache` forces a fresh implementation

**Specialists**

LLM-backed sub-agents running in scoped, isolated workspaces:

| Specialist | Purpose | Sets | Modifies code |
|------------|---------|------|:-------------:|
| `coverage_analyst` | Explain one exact coverage.json Campaign and propose advisory next steps | — | — |
| `mutation_tester` | Proposal-locked mutation testing: creator selects exact replacements, tester builds isolated variants | `mutation_score` | — |
| `reviewer` | Single-focus code review: reports issues by severity | `review_*` | — |

#### `coverage_analyst`

Call `coverage_analyst --campaign <exact-coverage.json> [--instruction <question>]`. The read-only Analyst explains retained native evidence and proposes advisory next steps. It does not run Simulation, read waveforms, evaluate Criteria, or approve waivers. Verified Target sources are optional; stale sources give report-only analysis.

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
| `tb` | `quality` | False-pass paths in scoped testbench sources, missing checks and edge cases, coverage gaps, timing/sampling mistakes, and TB code quality | `review_tb_quality` |

Controls: required `--scope <file,...>` selects files; repeatable `--steer` adds review context; `--dry-run` validates and previews without invoking an agent. The `spec` focus needs specification text: Ticket Mode resolves its mounted ticket or linked spec automatically, while standalone mode uses `--spec <path>`.

#### `mutation_tester`

Proposal-locked mutation testing. A read-only LLM creator returns exact source replacements; Booley runs a pristine baseline, then compiles and tests each replacement in isolation. It does not parse HDL or inject runtime selectors.

**Mutation campaign modes:**

| Campaign | Ticket Mode (`mandatory` or `optional`) | Standalone CLI options |
|----------|-----------------------------------------|------------------------|
| Default fixed | Target campaign with `target` + `scope` — generate 10 mutations and require all 10 detected | _(no goal options)_ — the same 10-of-10 campaign |
| Explicit fixed | add `total: N` and `min_detected: K` | `--count N` requires all N; add `--min-detected K` to require K |
| Size-scaled | add `auto: true` — choose 3-25 mutations from language-neutral source size and the time budget | `--count auto`; add `--min-detected K` for an explicit threshold |

`--dry-run` validates Target metadata and prints the source-size breakdown and proposed auto count without invoking an agent or simulator.

Targeting and reuse: `--scope <rtl-file,...>` chooses mutation sites; `--target <sim-target>` chooses exactly one complete runnable Target suite; `--steer <context>` biases mutation selection. A valid lock is reused on later runs, so new steering takes effect only with `--regen-lock`. The Target supplies the testbench top and complete RTL closure; they are not separate caller inputs.
<!-- END GENERATED: flows -->

Booley validates each proposal as one exact replacement, compiles it in
isolation, and restores the pristine source. A completed run publishes one
atomic campaign manifest with a durable baseline log, every mutant log, each
source variant, and the first public test that killed each detected mutant.

The `Sets` column names the [acceptance criteria](#acceptance-criteria) each Booley Flow or Specialist can satisfy (per-target families expand per project Target, e.g. `sim_pass_{target}`). `tb_coder` also exists but is hidden until it matures (see [ROADMAP.md](../internals/ROADMAP.md)); the Developer Agent authors testbenches itself.

#### Coverage collection workflows

In Interactive Mode, explicitly request collection, then pass its exact Campaign
path to the Analyst:

```bash
booley flow sim --target sim_counter --coverage
booley flow coverage_analyst --campaign <reports>/sim/12/targets/sim_counter/coverage.json
```

MCP uses `sim` with `target: "sim_counter", coverage: true`, followed by
`coverage_analyst` with the returned exact `campaign` path. Ungated collection
runs the full runnable suite, stores `not_requested`, and does not load waivers.

In Ticket Mode, author the [Coverage Criterion](CONFIG.md#native-coverage-configuration)
for each required Target and explicitly invoke `sim --coverage`. Only a durable
Campaign `pass` satisfies the Criterion. `fail` and `blocked` do not satisfy it;
Simulation Criteria retain their independent measured truth. The Analyst only
advises and never changes Criteria or approves Waiver Candidates.

### Running a Booley Flow directly

Direct invocation is the diagnostic escape hatch for setup and reproduction.
Inside the Sandbox:

```bash
booley flow sim --target sim_soc --test reset
```

Use `booley flow` to list discovered Flows, `booley targets --for-flow <flow>` to
list compatible Targets, and `booley flow <name> --help` for the live argument
schema. [FLOW_REFERENCE.md](FLOW_REFERENCE.md) is the canonical reference for
Target selectors, controls, exit codes, verdicts, Criteria, reports, and
artifacts.

### Viewing waveforms

When a traced simulation fails, the agent reads the trace with `bwave` queries,
and that part you never touch. What you do get is the picture: `bwave gui` puts a
trace on your screen as a VaporView tab
([`lramseyer.vaporview`](https://marketplace.visualstudio.com/items?itemName=lramseyer.vaporview),
auto-installed by the generated devcontainer spec) in the attached VS Code
window. Normally you just ask (*"show me the FIFO handshake around the
failure"*) and the agent scopes the view for you:

```bash
bwave gui                                                      # latest session trace
bwave gui @dut --signals 'tb.clk%b@red' \
  --group 'FIFO handshake=tb.dut.fifo.*%h@green' \
  --group 'Control=tb.dut.ctrl.state%h@blue' --time 1200c:1400c
```

A scoped view arrives readable rather than as a wall of signals: the trace's
clock lands on row 1 (a waveform without its clock can't tell a cycle from a
glitch), each `--group 'NAME=GLOB'` becomes a native named/collapsible section,
and `--time START:END` drops the viewer's two markers on the ends of the range,
so the status bar reports the span as a delta instead of making you subtract
ruler numbers. Repeating a group name adds another pattern to that group;
`--append` extends a same-name group or adds a new one. Signal selectors also
accept `%b`/`%h`/`%d` radix and `@red`/`@blue`/`@green` color suffixes; these
presentation properties are read back from the viewer before success is
reported. The rest of the grammar
(trace resolution, globs, time tokens, `--signals`, `--cursor`) matches `bwave`
queries and is in `bwave gui --help`. If a scoped view errors out instead of
opening, see
[TROUBLESHOOTING.md](TROUBLESHOOTING.md#bwave-gui-fails-on-a-scoped-view).

## Ticket-Driven Workflow

In this mode you interact with Booley the way a project manager interacts with
an engineer: through tickets. A ticket is a local Markdown file that records the
task, the files likely to change, and the checks that define success. It is not
a Jira ticket or GitHub issue, and creating one does not send anything outside
your machine.

The beginner path is:

1. In **agent chat**, type `/booley-ticket-create`, then describe the change in
   your own words. The skill asks questions and shows you the complete draft
   before writing it.
2. Approve the draft. The skill creates a short identifier called a **slug**
   (for example, `fix-fifo-backpressure`) and places the ticket in the queue, or
   in waiting if another ticket must finish first. Its **acceptance criteria**
   are the Booley Flow- and Specialist-backed checks that must pass before the work can finish.
3. In a **container terminal**, confirm that it is queued and start it:

   ```bash
   booley board show
   booley run --ticket <slug>
   ```

4. Leave that terminal running. Booley prints progress as the agent edits,
   calls Booley Flows and Specialists, and checks the acceptance criteria. The run stops at review,
   completion, or a blocked state that needs a human decision.
5. Back in **agent chat**, type `/booley-ticket-triage` to review a completed or
   blocked ticket and decide what happens next.

The sections below explain each part of that loop in detail.

### Creating Tickets

Do not try to write a perfect ticket before invoking
**`/booley-ticket-create`**: brain-dump everything you know, however
unstructured, and let the skill turn it into a precise contract.

- **Choose *Detailed plan*** when it asks how much detail to carry, unless the
  change is genuinely trivial and you already know every file it touches.
  *Lightweight* infers the fields without questions.
- **Answer the grilling.** Questions arrive in rounds, each with a suggested
  answer; the skill looks up codebase facts itself instead of asking you.
- **Read the criteria hardest** when it shows the complete draft. They are the
  entire contract: the harness gates on them, and prose in the ticket body gates
  nothing. `scope` is what keeps the agent out of unrelated files. Ask for edits
  in place.
- **Approval finishes the job.** The skill authors any new Targets the ticket
  needs and enqueues it; there are no further approval gates.

#### Project Ticket Creation Guidance

`booley init` creates `.booley_project/ticket_creation.md`. Write ordinary Markdown there
when a Project repeatedly wants the same Criteria or successful-run disposition. For
example:

```markdown
- Include a corrective security review in every feature Ticket.
- Every Ticket uses the `sim_smoke` and `sim_regression` Targets.
- Feature and refactor Tickets must prove that area does not regress on `synth_area`.
```

There are no required headings, YAML blocks, or complete per-type mappings. Lightweight,
Detailed-plan, and agent-driven creation start with Booley's shipped inference and apply
every relevant statement. The skill consults the live Criterion catalog, Targets, and
registered tests to turn the prose into concrete Ticket frontmatter. Explicit instructions
for one Ticket win over Project guidance; ambiguous or unavailable requirements are
surfaced rather than ignored or invented.

Only `/booley-ticket-create` reads this file, and only while creating a Ticket. Its
authority is limited to the `CRITERIA_MANDATORY` and `CRITERIA_OPTIONAL` blocks,
Target annotations, and `on_success`; the resulting Ticket remains the
structured artifact validated by Booley. Editing the guidance never changes an
existing Ticket. Projects initialized with the former `ticket_defaults.md` filename keep
working: the skill reads it as free-form guidance when `ticket_creation.md` is absent and
disregards the former scaffold's strict-format instructions.

Queuing a ticket doesn't start it. Tickets sit in `board/queue/` until you start Ticket Mode with `booley run` in a container terminal; that loop then pulls tickets off the queue one after another without further input. Use `/booley-ticket-triage` to work through blocked, failed, and finished ones.

**Amending a blocked Ticket.** During triage, the agent may propose relaxing an
existing acceptance Criterion or expanding file Scope when the recorded blocker
supports that change. It shows the exact before-and-after proposal for Human
approval. An approved amendment keeps the Ticket's implementation, publishes a
new Ticket baseline, and queues the Ticket to resume. Retry with feedback,
requested review, reset, and fresh authoring remain separate choices; the
triage agent handles the amendment commands.

**Writing a ticket by hand** is an advanced path because executable tickets
require the same preparation and validation that the skill automates. Follow
the complete CLI workflow in the packaged
`booley-ticket-create/SKILL.md` and its `TICKET_TEMPLATE.md`; moving a raw draft
straight to the queue cannot bypass those checks. `booley run --ticket <slug>
--dry-run` checks the resulting setup without executing it.

`booley board show` prints each ticket's *status*, which matches its `board/`
directory name except for three: `drafts/` is `draft`, `queue/` is `queued`, and
`active/` is `running`.

### Ticket Board lifecycle

A Ticket is one body of work with one branch, worktree, and evidence history. Its
normal path is:

```text
draft ──► queued ──► running ──► review ──► done
  │          ▲          │           └──────► archived
  └─► waiting┘          └─► blocked ──► queued
```

- `waiting → queued` happens when dependency Tickets finish.
- `running → blocked` records a question or failure that needs human input.
  Resolving it returns the same Ticket to `queued`; an active `booley run`
  invocation later resumes its existing workspace and evidence.
- `running → queued` is an exceptional interruption-recovery move, not another
  development attempt. Do not requeue while the Ticket still has an active job.
- `running → review` happens when `on_success` includes `review`. Omitting it
  takes the `running → done` shortcut.

`review` is a human decision point, not a partial-rework loop. Once a Ticket is
accepted, any new commit in its worktree makes that acceptance stale; Booley
will not approve it until the Ticket heads match the accepted ones again. The
reviewer has three substantive choices:

1. Approve the Ticket as `done` when its live participant heads still match the
   accepted heads. If acceptance is stale, first preserve post-acceptance commits
   on a separate safety branch and restore every named Ticket ref and worktree to
   its exact frozen commit before approval.
2. Reset it completely. This retires the Ticket worktree and branch, archives
   the current runtime artifacts as prior-run history, clears the active state,
   and returns the Ticket to `queued` as a clean run. It does not resume or
   selectively retain the reviewed work.
3. Archive it. If the remaining work needs a different contract, create a new
   Ticket rather than sending this one back for rework.

The ordinary `review → queued` move is therefore invalid. Only the explicit,
destructive reset operation may put a reviewed Ticket back in the queue. Use
`/booley-ticket-triage` for blocked and review decisions, `booley board show` to
inspect state, and `booley cheat --board` for the compact transition reference.

### Acceptance Criteria

A ticket doesn't describe *steps*: it declares **acceptance criteria** (split into `mandatory` and `optional`), and the harness, not the agent, decides when they're met. A criterion is satisfied only by a valid verdict from the Booley Flow or Specialist that owns it (e.g. a simulation criterion needs `sim` to return `pass`; a `review_*` criterion needs a `reviewer` run), never by the Developer Agent asserting success, and it is re-checked whenever the underlying code changes. **A ticket cannot reach review with an unmet mandatory criterion.** Optional criteria do not block review, but the Developer Agent must justify every optional criterion it could not complete; `submit_run_report` rejects the report until that explanation is supplied, and final acceptance rejects a stale report that does not cover the currently unmet set. This applies even when routine run reports are disabled. See [ARCHITECTURE.md](../internals/ARCHITECTURE.md#ticket-mode) for the criteria mechanics.

Three related inputs have different jobs. `criteria.toml` defines the live
Criterion families available to Project-authored Flows and Specialists;
`ticket_creation.md` guides creation-time selection from that catalog; and each Ticket
stores the concrete immutable Criteria selected for that one run.

The supported criteria families are defined once in `criteria.toml` and listed below; `{target}` denotes a per-target expansion (one criterion per project Target). `booley cheat` renders this same table live, including any project-defined criteria. A bare `review_*` ticket key expands to `_clean`: every finding must be verified fixed or explicitly waived with user-visible justification. Use an explicit `_done` suffix for a terminal advisory review whose findings are reported but not fixed in that ticket run. Both modes become stale after relevant source changes.

A TB-quality review is source-scoped and does not require a simulation Target.
Ticket intake records the TB files from the Ticket scope directly on the
review criterion so its suggested invocation is immediately callable.

<!-- BEGIN GENERATED: criteria -->
#### Build & Elaborate

| Criterion | Description | Set by | Workflow Region |
|-----------|-------------|--------|-------|
| `elab_pass_{target}` | RTL/TB compiles and elaborates cleanly (no simulation) | `sim --mode elab-only` | pre-sim |
| `elaborate_standalone` | Every module in the Targets' RTL source scope elaborates standalone from its declaring file (shared package/interface files auto-included, parameter defaults) | `sim --mode elab-only-standalone` | pre-sim |
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
| `review_tb_quality` | Source-scoped TB review: false-pass detection, coverage gaps, and TB code quality | `reviewer --category tb --focus quality` | pre-sim |

#### Simulation

| Criterion | Description | Set by | Workflow Region |
|-----------|-------------|--------|-------|
| `cycle_count_{target,test}` | A named test passes and its observed Cycle Count meets every declared threshold | `sim` | sim loop |
| `sim_pass_{target}` | RTL simulation passes all tests | `sim` | sim loop |

#### Coverage

| Criterion | Description | Set by | Workflow Region |
|-----------|-------------|--------|-------|
| `coverage_{target}` | Native Coverage Campaign policy for one Simulation Target | `sim --coverage` | post-sim |

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

#### Threshold parameters

`SYNTH`, `FPGA`, and `CYCLE_COUNT` Criteria accept metric thresholds, either
absolute (`_max`, `_min`) or relative to the Ticket baseline
(`_increase_at_most`, `_reduce_at_least`; percentages need the `%` suffix).
Timing thresholds can be scoped to one clock with a `clk_i.` prefix:

```yaml
SYNTH: {synth_core: {cell_count_reduce_at_least: 8%, clk_i.fmax_mhz_min: 400}}
CYCLE_COUNT: {sim_coremark: {coremark: {cycle_count_max: 100000}}}
```

`booley cheat --criteria` prints the full per-metric table, mutual exclusions,
and every `cycle_count` variant.

### Where the work lands (`on_success`)

Every Ticket carries a list of completion actions. Omitted actions are false:

```yaml
on_success: [triage_report, review, merge, cleanup]
```

`review` parks the finished Ticket in `board/review/` for a human decision and
keeps its worktree and branch until that decision. The reviewer may make a small
in-place correction and run the relevant Flows again. `cleanup` waits until
review ends. Omitting `review` finishes directly in `board/done/`.

`cleanup` works without `merge` for disposable test Tickets. Booley first pins
the accepted commits under internal refs so evidence remains reachable, then
removes the Ticket branches and worktrees. To keep those workspaces, omit
`cleanup`. To leave the destination branch untouched, omit `merge`.

Most Tickets use existing Targets. To author one, annotate every structured
mention with `(new)`, `(temp)`, or `(replaces old_target)` and include `merge`:

```yaml
CRITERIA_MANDATORY:
  LINT: {lint_style (new): clean}
  SIM:
    sim_core_v2 (replaces sim_core): {all: pass}
    ticket_probe (temp): {smoke: pass}
```

`new` Targets stay after the Ticket merges, `replaces` swaps out the named
predecessor, and `temp` Targets exist only as Ticket evidence and are removed on
acceptance. A Ticket may not edit or delete existing Targets. The details are in
[ADR 0060](../adr/0060-model-target-changes-with-ticket-target-plans.md).

Every review-bound run writes a review package under
`logs/<slug>/.runtime/triage-prep/`. With `triage_report` in `on_success`,
Booley also makes one extra model call to write a self-contained HTML
explanation, which the triage briefing links to; open it and select **Show
Preview**. `booley board review <slug> --force` regenerates the package, also
for a `blocked` Ticket when you want to inspect partial work before deciding
whether to reset or archive it. `booley board show <slug>` renders it.

`booley run` ends each Ticket with one machine-readable `BOOLEY_RUN_RESULT`
JSON line for scripts; `booley cheat --board` documents its fields.

Two Git gotchas live in Troubleshooting:
[Ticket worktrees show as `prunable`](TROUBLESHOOTING.md#ticket-worktrees-show-as-prunable-on-the-host)
and [`git` cannot see files under `.booley_project/`](TROUBLESHOOTING.md#git-cannot-see-files-under-booley_project).

## Running Unattended

Ticket Mode is built for unsupervised, multi-hour runs. The minimum interaction is: create a ticket, then review the results. Everything in between runs on its own. It debugs failures across repeated simulate-fix cycles, resumes where it left off after an interruption (reboot, crash, subscription limit), and blocks a ticket for human triage when it gets stuck rather than guessing. When you triage a blocked ticket, you can retry it with **tagged feedback** to steer the next attempt without starting over.

### Entering the Sandbox without VS Code

"Reopen in Container" needs the VS Code UI. When there isn't one (a CI job, an
agent driving the CLI, or a host with no `devcontainer` CLI), `booley session`
opens the same container from the same generated `.devcontainer/devcontainer.json`:

```bash
booley session up                       # create or start it; runs the same lifecycle hooks
booley session enter                    # interactive shell inside it
booley session enter -- booley doctor   # or run one command and exit
booley session status                   # running | stopped | absent
booley session refresh                  # rebuild configured image, recreate session
booley session down                     # stop and remove
```

`session refresh` is safe to interrupt: the old Sandbox stays recoverable until
the replacement is verified, and the next lifecycle command finishes or rolls
back an interrupted refresh. It refuses to replace a Sandbox owned by VS Code;
use **Dev Containers: Rebuild Container** there. For a licensed headless
Sandbox, run `booley session down` first.

These are **host** commands (they need Docker), and `booley init` must have run
first: it builds the image and creates the network, proxy, and reaper. The
container carries the same `booley.role=interactive` label as the VS Code one,
so the idle reaper owns its lifecycle either way. `booley session enter` is the
headless equivalent of a container terminal, so every container-only command
works through it.

A command after `--` is supervised: `Ctrl-C` cleans up its whole process tree
inside the Sandbox, keeps the Sandbox from being reaped as idle while it runs,
and returns the command's exit code (or the usual `128 + signal`).

## Scope

Each ticket declares the files it's expected to touch. That's a plan, not a
fence: if finishing the job genuinely needs a file the ticket didn't name — a
shared package or neighbouring module — the agent edits it and the change lands
on the branch like any other. Booley records every such file in
`.runtime/scope_deviations.json` for ticket triage.

The hard lines are Booley bookkeeping and the Target/control inputs prepared by
the ticket-creation agent. Developer commits touching either are rejected; if a
Target recipe is wrong, the ticket blocks so creation or triage can revise it.

If the same file keeps showing up as a deviation across tickets, that's a hint
your ticket scopes are drawn too narrowly, not that the agent is misbehaving.

## Push Notifications

Configure an [ntfy.sh](https://ntfy.sh) topic in your Project's `booley.toml`,
then subscribe to that topic in the ntfy app:

```toml
[notifications]
ntfy_topic = "your-private-topic"
# Optional: omit events to enable all, or use [] to disable all.
events = ["blocked", "review", "done", "doctor", "rate_limit"]
```

`blocked` asks for input, `review` announces work ready for review, and `done`
announces the accepted transition to `done`, even if cleanup still needs recovery.
`doctor` announces changed automatic
Doctor issues; `rate_limit` announces Claude rate-limit waits.

The Sandbox's default network policy blocks ntfy.sh. To permit delivery,
add `"ntfy.sh"` to `egress_allowlist` in the existing `[interactive]` table of
[your host configuration](CONFIG.md#host-configuration-configtoml), preserving
any other entries:

```toml
[interactive]
egress_allowlist = ["ntfy.sh"]
```

After changing the policy, shut down active Sandboxes for every Project,
run `booley bootstrap` on the host to update the shared proxy, then restart the
Sessions. Recreating a Session alone does not update the proxy. This permission
applies to all Projects on the host. Delivery uses HTTPS and is best-effort:
notifications do not change Ticket outcomes, wait for delivery, or retry failed
requests. Each delivery attempt has a 5-second connection timeout and a
15-second total timeout. Set `NTFY_DISABLE=1` to suppress delivery in tests.

## When Booley itself misbehaves

A Booley Flow exits 2 with nothing useful on stderr, a doc describes a knob that isn't
there, a message reads like a crash when nothing crashed. Run the
**`/booley-feedback`** skill in the agent chat while the failure is still on
screen — it captures the reproduction, checks the claim against Booley's own
source before blaming it, scrubs your project's identifiers, and writes a
redacted Markdown file for you to inspect and share manually. Booley does not
transmit it.

Nothing has to be broken. Confusing behavior, praise, gripes, wishes, and a
blunt "this wasn't worth the setup cost" are all worth one sentence to
`/booley-feedback`; it will not ask for a reproduction. Export is explicit and
local; see [CONFIG.md](CONFIG.md#feedback-feedback).

## CLI reference

`booley --help` labels every top-level command as `[host]`,
`[Sandbox]`, `[either]`, or `[mixed]`. Sandbox commands run after
VS Code accepts **Reopen in Container**, or through `booley session enter` in a
headless environment. Mixed commands enforce location at their nested
operation.

`booley projects` lists the Projects this host knows about;
`booley projects discover <dir>` imports existing ones.

`--dry-run` / `--check-ready` validate without opening the full-screen Console.

```bash
# Execute a single ticket end-to-end in the full-screen Console
booley run --ticket <slug>

# Validate the setup without executing anything (one-shot, no TUI)
booley run --dry-run

# Keep the loop alive as a daemon instead of exiting on an idle queue
booley run --idle-timeout 0

# Print the current ticket board
booley board

# Show cheatsheet (whole sheet)
booley cheat

# Show one section of it (`--list` names them all)
booley cheat --criteria
booley cheat --flows --sandbox
booley cheat --board
booley cheat --commands --project

# Run diagnostics; --concise hides successful rows
booley doctor
booley doctor --concise

# Run real smoke checks against marked sim/lint/synthesis Targets
# (marked FPGA Targets get explicit manual implementation commands)
booley doctor --deep

# Credential-free release smoke: skips the agent credential and Developer probes
booley doctor --deep --skip-agent-checks
```

Booley also runs a non-deep Doctor audit automatically when the Sandbox starts
and before `booley run`, at most weekly (daily while findings are unresolved)
or when configuration changed. It never blocks work; changed findings surface in
`booley session up`, `booley run`, and the next Flow result. The latest result
lives in `.booley_project/runtime/doctor/last.log`.

`booley run` ends by itself once the queue has stayed fully drained — nothing
executable, active, or waiting — for `--idle-timeout` seconds (default 300).
Pass `--idle-timeout 0` to keep polling forever, which is what you want when
the loop runs as a daemon and tickets are queued from another terminal.

The CLI is headless: no interactive agent runtime (the Claude Code or Codex app) is required to drive it. It drives the agent through the SDK (Claude Agent SDK / Codex SDK) instead. It picks up tickets from the queue, runs them, and moves completed tickets to review.

> **`booley run` is container-only.** Launched on a host terminal it fails fast
> and points you to **Reopen in Container** (or
> `booley session enter -- booley run`). `booley init` is the host-side
> counterpart and refuses inside the container, where Docker is deliberately
> unavailable.

### Concurrent tickets

Two terms this section leans on (both in the [glossary](../CONTEXT.md#execution)): a
**Job** is a single background run a ticket dispatches — one sim, one synth, one
Specialist; each kind is a **Job Class** with its own concurrency cap.

Concurrency is one `booley run` per container terminal: open another terminal
in the same devcontainer, start another run, and watch each ticket's Console in
its own terminal. Each run claims a Developer Agent slot, capped by
`[jobs] max_tickets` (default 2, see [CONFIG.md](CONFIG.md#jobs--concurrency-jobs));
runs beyond the cap wait in FIFO order and the Console narrates the wait
("waiting for slot (position N)"). The same admission applies to the Jobs the
tickets dispatch (sim/synth runs, Specialists; each Job Class has its own
cap): interactive work has priority over ticket work, but the scheduler never
preempts a running Job. A submit is refused (`BLOCKED`) only when a class queue
itself is full (`queue_max`, default 8).

The agent can cancel a queued or running Job with the `booley_cancel` MCP
tool; ask it to.

> **Tip: scale out once Booley feels familiar.** The whole system is built to
> be driven many-at-once: run several Claude Code tabs or parallel Codex CLI
> agents alongside multiple terminals, and keep multiple tickets in flight.
> Tickets get their own git worktree automatically, but interactive sessions
> don't, so parallel interactive agents can collide in the shared tree. The fix
> is in
> [TROUBLESHOOTING.md](TROUBLESHOOTING.md#two-interactive-agents-keep-clobbering-each-others-edits).

## Auth & billing

This section is about what pays for the tokens, and about one failure mode that bites unattended runs specifically.

Booley's LLM agents (the Ticket Mode Developer Agent and the Specialists `reviewer` and `mutation_tester`) run through the **agent provider** recorded by `booley init` as `[agent] provider` in `booley.toml`: `claude` (the default, using the Claude Agent SDK) or `codex` (the Codex CLI). Init does not infer this choice from installed CLIs; flags, existing configuration, or a terminal answer can override the default. Each provider authenticates exactly as its own app does, with the same two options either way: a **subscription** or an **API key**:

| provider | subscription | API key |
|---|---|---|
| `claude` | Claude Pro/Max/Team/Enterprise, via the OAuth login `booley init` detects at `~/.claude/.credentials.json` | `ANTHROPIC_API_KEY` |
| `codex` | the Codex login (`codex login`, stored at `~/.codex/auth.json`) | `OPENAI_API_KEY` |

Under either provider, both options work for Ticket Mode *and* for Specialists in Interactive Mode: there is no API-key-only restriction.

`booley init --skip-credentials` is available for CI and other setup-only
environments that intentionally have no provider secret. It skips credential
inspection only: init still resolves, validates, and records the provider/auth
policy. Normal user setup should omit the flag so init can report whether the
selected credential is ready.

On a subscription, usage counts against the plan's limits; Booley waits out a
usage cap and requeues the ticket rather than failing. An API key bills per
token. With several credentials present, the agent CLI picks one in its own
order:

- **Claude:** exported `ANTHROPIC_API_KEY`, then `CLAUDE_CODE_OAUTH_TOKEN` (what
  `booley auth` stores, below), then the subscription login.
- **Codex:** exported `OPENAI_API_KEY`, then the `auth.json` login.

An exported API key therefore beats everything, including a stored
`booley auth` token. `booley init`, `booley doctor`, and `booley auth --status`
report which credential wins. To pin the choice, set `[agent] auth =
"subscription"` (Booley scrubs the API key from agent environments) or
`"api_key"` (fails loudly when the key is missing); see
[CONFIG.md](CONFIG.md#pinning-what-bills-agent-auth).

For long unattended runs, run **`booley auth`**. It stores the app's *rotation-free* credential at `~/.config/booley/` (mode 0600, deliberately outside every repo and bind mount so it cannot be committed) and re-seeds the devcontainer spec. Booley then injects it into containers itself, with no `export` needed. `booley auth --status` reports which credential each agent would use, and `booley doctor` warns when a run is about to rely on a refreshing one.

Why this matters for long runs (the default credential rotates and can log
every in-flight agent out mid-run) is in
[TROUBLESHOOTING.md](TROUBLESHOOTING.md#agents-turn-into-not-logged-in-partway-through-an-unattended-run).
The rotation-free alternative differs per app:

| app | rotation-free credential | how |
|---|---|---|
| Claude | one-year OAuth token (never refreshes) | `booley auth`, which runs `claude setup-token` for you |
| Codex | API key (`OPENAI_API_KEY`) | `booley auth --app codex`, then paste the key |

Codex has no `setup-token` equivalent: `codex login` writes the *refreshing* credential we are trying not to depend on, so its API key is the only rotation-free option, and it bills per token rather than against your subscription. That's a real trade-off, not a free win.

The stored credential reaches VS Code's "Reopen in Container" too: the re-seeded spec mounts it read-only and the in-container registrar applies it on every container start (Claude: `settings.json` `env`; Codex: `auth.json`). Rebuild an existing container once so the mount exists. Exporting `CLAUDE_CODE_OAUTH_TOKEN` / `OPENAI_API_KEY` yourself still works and a non-empty export takes precedence over the stored value. The credential is never baked into the spec.
