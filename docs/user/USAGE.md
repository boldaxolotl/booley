# Usage

How to drive Booley day to day. No previous experience with LLM agents is
assumed.

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

Booley's domain terms, including **Target**, **Booley Flow**, **Specialist**, and
**Session Runtime**, have precise definitions in the canonical controlled
vocabulary, [CONTEXT.md](../CONTEXT.md). Refer to it whenever a term is unfamiliar;
this guide does not repeat those definitions.

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
Flows, Specialists, Criteria, Targets, skills, artifacts, and runtime commands.
Print the whole sheet or use `booley cheat --list` and combine section flags,
such as `booley cheat --board` or `booley cheat --commands --project`.

Plain Doctor also setup-checks marked FPGA Targets and probes Vivado.
`booley doctor --deep` goes further and runs real smoke sims/lints/synthesis,
while reporting FPGA implementation as a target-specific manual check; it needs
the Session Runtime. Both it and the full command set are in the
[CLI reference](#cli-reference) below.

Credential-free release automation can use
`booley doctor --deep --skip-agent-checks`. Doctor reports the agent credential
inspection, Ticket Mode backend-health check, and live Developer authorization
probe as skipped; every non-agent project, runtime, Ticket Mode, and EDA check
still runs. This flag is for smoke tests, not the normal setup gate before an
agent session.

If `booley` is not found, return to the [installation instructions](../../README.md#installation).
Do not continue into the
container until plain `booley doctor` has no unresolved failures or warnings.

## Choose a mode

Booley has two ways to work. Both use the same project configuration, Booley Flows, Specialists, and
Session Runtime. For newcomers, they are a progression rather than an either-or
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

   Booley opens either [Claude Code](https://code.claude.com/docs/en/quickstart)
   or [Codex](https://developers.openai.com/codex/cli), matching the Project's
   `[agent].provider` setting. Bare `booley` is the short form of `booley chat`;
   both replace themselves with the selected CLI and leave the terminal session
   native. Use `booley --help` to see the command reference instead.

   If you use the Claude Code VS Code extension instead, open its chat panel in
   this reloaded window; there is no CLI command to run. For Codex, Booley
   recommends the CLI because you can run a separate Codex session in each
   container terminal and therefore keep multiple interactive sessions in
   flight. The Codex VS Code extension supports only one chat at a time, so it
   gives up that concurrency.

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

You do not need to know the exact Booley Flow command, flags, report locations, or MCP
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
> is push to `origin`, which is blocked by design, so a sandboxed agent can never
> corrupt your remote. So the loop is: let it commit, review the commits, then
> push them yourself from a terminal outside the Booley sandbox. (In Ticket Mode
> this isn't a choice: the agent always commits, since that's how a ticket's
> work is recorded and moved to review; the same push-to-`origin` block applies.)

## Booley Flows & Specialists

These are the built-in capabilities both modes share. **Booley Flows** are
predictable wrappers around EDA tools; **Specialists** are focused LLM agents.

You normally do not call either one manually. Say *"run the reset test on the
`sim_lite` Target"* or *"how much area did that cost?"*, and the agent picks the
capability, Target, and flags. The table is worth a skim because it shows the
complete set of built-in capabilities. Every Booley Flow and Specialist runs
inside the Session Runtime. **Sets** names the acceptance criteria that the
Booley Flow or Specialist can satisfy in a ticket.

Which EDA program runs underneath is determined by the Target. The currently
supported programs are tracked in [SUPPORTED-EDA-TOOLS.md](SUPPORTED-EDA-TOOLS.md).

The catalogs are generated from the MCP tool registry. `booley cheat --flows` and
`booley cheat --specialists` print them live as separate sections; the combined
reference below is also embedded in
[ARCHITECTURE.md](../internals/ARCHITECTURE.md#the-sandbox).

<!-- BEGIN GENERATED: flows -->
**Booley Flows**

Deterministic end-to-end orchestration; no LLM:

| Booley Flow | Purpose | Sets |
|--------|---------|------|
| `sim` | Run RTL simulation for one or more Targets | — |
| `lint` | Run lint for one or more Targets | `lint_clean` |
| `synth` | Run ASIC synthesis for one or more Targets with optional baseline comparison | `synthesis_ok` |
| `fpga` | Run FPGA implementation for one or more Targets with optional baseline comparison | `fpga_impl_ok` |

Common controls: `--target <name,...>` selects Target(s); `--dry-run` prints commands without executing them; `booley flow <name> --help` shows the full contract.

Key Flow-specific controls:

- `sim`: `--elab-only` (`--build-only`) compiles, elaborates, and links without running tests; add `--standalone` for the stronger module sweep. `--test <name>` selects a test, `--skip <name,...>` excludes tests, and `--trace` captures waveforms for the simulation run. Focused Cocotb output summarizes unselected skips; pass `--result-verbosity full` to print every XML testcase entry (the complete XML and JSON artifacts are always retained)
- `lint`: `--scope <file,...>` filters reported findings to selected files
- `synth`: `--baseline <ref>` compares metrics against a git revision; `--default-clock <ps>` explicitly supplies a clock only when the Target has no SDC
- `fpga`: `--baseline <ref>` compares metrics against a git revision; `--no-cache` forces a fresh implementation

**Specialists**

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
<!-- END GENERATED: flows -->

Booley validates each proposal as one exact replacement, compiles it in
isolation, and restores the pristine source. A completed run publishes one
atomic campaign manifest with a durable baseline log, every mutant log, each
source variant, and the first public test that killed each detected mutant.

The `Sets` column names the [acceptance criteria](#acceptance-criteria) each Booley Flow or Specialist can satisfy (per-target families expand per project Target, e.g. `sim_pass_{target}`). `coverage_analyst` and `tb_coder` also exist but are hidden until they mature (see [ROADMAP.md](../internals/ROADMAP.md)); the Developer Agent authors testbenches itself.

### Running a Booley Flow directly

Direct invocation is the diagnostic escape hatch for setup and reproduction.
Inside the Session Runtime:

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
bwave gui @dut --signals 'tb.dut.fifo.*' --time 1200c:1400c    # exactly this view
```

A scoped view arrives readable rather than as a wall of signals: the trace's
clock lands on row 1 (a waveform without its clock can't tell a cycle from a
glitch), and `--time START:END` drops the viewer's two markers on the ends of
the range, so the status bar reports the span as a delta instead of making you
subtract ruler numbers. The rest of the grammar (trace resolution, globs, time
tokens, `--append`, `--cursor`) matches `bwave` queries and is in `bwave gui
--help`. If a scoped view errors out instead of opening, see
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

The recommended path is the **`/booley-ticket-create`** skill in Claude Code or
Codex. Type it into the agent chat opened in
[Interactive Mode](#open-your-first-agent-session). Do not try to write a
perfect ticket before starting: describe everything you know, however
unstructured, and let the skill turn it into a precise contract:

1. **Brain-dump, then invoke the skill.** Half-formed is fine. The mess is the input.
2. **It asks how much detail the ticket should carry**: *Lightweight* (it infers the fields, no grilling) or *Detailed plan*. **Say Detailed plan** unless the change is genuinely trivial and you already know every file it touches.
3. **Grilling session.** The skill maps a dependency tree and asks one frontier at a time:
   every currently unblocked question arrives in the same round, each with a suggested
   answer, while downstream questions wait for their prerequisites. It investigates
   codebase facts instead of asking you for them. Once no branch remains silently assumed,
   it synthesises the `## Implementation Plan` and complete ticket directly.
4. **Review and approve the complete ticket.** This is the single draft-review artifact
   after grilling: there is no intermediate shared-understanding summary, short-form
   preview, or separate criteria menu. Read the criteria hardest. They are the entire
   contract: they are what the harness gates on, and prose in the ticket body gates
   nothing. Ask to edit criteria, fields, plan, or scope in place; `scope` is what keeps
   the agent out of unrelated files.
5. **Creation completes automatically.** After ticket approval, the skill authors and seals
   any required Target recipe, then enqueues the ticket. Target-contract worktrees, diffs,
   and seal metadata are internal mechanics rather than additional user approval gates.

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
authority is limited to `criteria` and `on_success`; the resulting Ticket remains the
structured artifact validated and sealed by Booley. Editing the guidance never changes an
existing Ticket. Projects initialized with the former `ticket_defaults.md` filename keep
working: the skill reads it as free-form guidance when `ticket_creation.md` is absent and
disregards the former scaffold's strict-format instructions.

Queuing a ticket doesn't start it. Tickets sit in `board/queue/` until you start Ticket Mode with `booley run` in a container terminal; that loop then pulls tickets off the queue one after another without further input. Use `/booley-ticket-triage` to work through blocked, failed, and finished ones.

**Writing a ticket by hand** is an advanced path because executable tickets
require the same preparation and validation that the skill automates. Follow
the complete CLI workflow in the packaged
`booley-ticket-create/SKILL.md` and its `TICKET_TEMPLATE.md`; moving a raw draft
straight to the queue cannot bypass those checks. `booley run --ticket <slug>
--dry-run` checks the resulting setup without executing it.

**Directory names and status names are not always the same word.** `booley board show` prints the ticket's *status*, while the file lives in a same-meaning but differently-named directory. Three of the eight differ:

| Directory | Status shown by `board show` |
|---|---|
| `board/drafts/` | `draft` |
| `board/queue/` | **`queued`** |
| `board/waiting/` | `waiting` |
| `board/active/` | **`running`** |
| `board/blocked/` | `blocked` |
| `board/review/` | `review` |
| `board/done/` | `done` |
| `board/archived/` | `archived` |

So a ticket reported as `running` is the one sitting in `board/active/` — nothing is out of sync.

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
  Resolving it returns the same Ticket to `queued`; the Runner later resumes its
  existing workspace and evidence.
- `running → queued` is an exceptional interruption-recovery move, not another
  development attempt. Do not requeue while the Ticket still has an active job.
- `running → review` is the default successful outcome. A Ticket configured with
  `on_success.destination: done` deliberately takes the `running → done`
  shortcut instead.

`review` is a human decision point, not a partial-rework loop. The reviewer has
three substantive choices:

1. Approve the Ticket as `done`. Small corrections may be made directly in the
   existing Ticket worktree, with the relevant Flows and Specialists invoked
   there, before approval; the Ticket remains in `review` throughout.
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

Ticket Mode seals that criterion set at intake. A Flow/Target call that cannot
bind one of the sealed criteria is rejected before job admission and shows the
copyable pending invocation; use `--diagnostic` to run it deliberately without
acceptance effects. Simulation acceptance compares the selected and passing
test names with the Target registry instead of trusting an aggregate count,
rejects explicitly model-only evidence for a DUT criterion, and requires a
recorded failing run before a `fail -> pass` criterion can become green.

Three related inputs have different jobs. `criteria.toml` defines the live
Criterion families available to Project-authored Flows and Specialists;
`ticket_creation.md` guides creation-time selection from that catalog; and each Ticket
stores the concrete immutable Criteria selected for that one run.

The supported criteria families are defined once in `criteria.toml` and listed below; `{target}` denotes a per-target expansion (one criterion per project Target). `booley cheat` renders this same table live, including any project-defined criteria. A bare `review_*` ticket key expands to `_clean`: every finding must be verified fixed or explicitly waived with user-visible justification. Use an explicit `_done` suffix for a terminal advisory review whose findings are reported but not fixed in that ticket run. Both modes become stale after relevant source changes.

<!-- BEGIN GENERATED: criteria -->
#### Build & Elaborate

| Criterion | Description | Set by | Workflow Region |
|-----------|-------------|--------|-------|
| `elab_pass_{target}` | RTL/TB compiles and elaborates cleanly (no simulation) | `sim --elab-only` | pre-sim |
| `elaborate_standalone` | Every module in the Targets' RTL source scope elaborates standalone from its declaring file (shared package/interface files auto-included, parameter defaults) | `sim --elab-only --standalone` | pre-sim |
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

#### Threshold parameters

<!-- BEGIN GENERATED: criteria-params -->
Per-target `synthesis_ok` / `fpga_impl_ok` criteria accept optional threshold **params**. Each takes a `targets:` list, the per-target scoping key naming which project Targets to check (the key is `targets`, never `configs`), plus one or more metric params. Four flavours per metric: two absolute, two relative to the ticket's `base_sha` baseline:

| Flavour param suffix | Baseline? | Meaning |
|----------------------|:---------:|---------|
| `_max` | no | metric must stay **≤** the given value |
| `_min` | no | metric must stay **≥** the given value |
| `_increase_at_most` | yes | metric may grow **at most N%** above baseline |
| `_reduce_at_least` | yes | metric must shrink **at least N%** below baseline |

Percentage threshold values must include the `%` suffix (for example, `cell_count_reduce_at_least: 8%`).

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

Syntax (ticket criteria): `cycle_count: [{target: sim_coremark, test: coremark, cycle_count_max: 100000, cycle_count_reduce_at_least: 5%}]`.

A named `[SIM_CYCLES] <test> <count>` observation is gated evidence only when that exact test passes. Missing, malformed, duplicate, legacy unnamed, failed, or inconclusive evidence fails closed. Without a `cycle_count` Criterion, existing Cycle Count records remain observational.

Relative comparisons report an **observed Cycle Count change**. When declared workload inputs differ, review reports disclose the changes and do not attribute the result to RTL alone.
<!-- END GENERATED: criteria-params -->

Ticket creation first opens an isolated Ticket Workspace. This is where the
ticket-creation agent adds any Target the Ticket will require; the Project's
destination branch stays fully functional and Doctor-clean until acceptance.
The Target Contract seals every participating repository ref, and final
acceptance rechecks that composite control surface before publishing it.

**Per-clock timing thresholds.** Timing is reported per clock, so the timing
metrics (`critical_path_ps`, `fmax_mhz`, `wns_ns`, `whs_ns`, `period_ns`) accept
**flat** or **clock-scoped** thresholds (area / cell / LUT / FF / BRAM / DSP /
utilization thresholds are **not** clock-scopable):

- Flat `fmax_mhz_min: 400` means "**every** clock's Fmax ≥ 400": it gates on
  the timing-worst clock.
- Clock-scoped `clk_i.fmax_mhz_min: 400` (or `clk_i.critical_path_ps_max: 9000`)
  gates only clock `clk_i`; the clock name is the one reported in `per_clock`.

The `critical_path_ps_max` ⊕ `fmax_mhz_min` mutual exclusion is enforced
**per-scope** (per clock), so `clk_i.fmax_mhz_min` and `clk_2x.critical_path_ps_max`
can coexist. Example: `synthesis_ok: {targets: [<target>], clk_i.fmax_mhz_min: 400,
clk_2x.critical_path_ps_max: 5000}`.

### Where the work lands (`on_success`)

Every ticket carries an `on_success` block that says what happens once the criteria are met:

```yaml
on_success:
  destination: review     # review (default) | done
  merge: true             # merge the ticket branch into its base
  cleanup: true           # remove the worktree and branch afterwards
  triage_report: true     # add an LLM-generated HTML explanation to the review package
  remove_targets: []      # criterion-bound Targets omitted from the accepted destination
```

`destination: review` parks the finished ticket in `board/review/` for you to look at, and **keeps its worktree and branch**. That preserved workspace is where a reviewer makes any small in-place correction and invokes Flows or Specialists again. `cleanup: true` is deferred until the review ends in `done`, `archived`, or an explicit full reset. Review never sends retained work back to the queue for partial rework. `destination: done` skips the pause and merges, cleans up, and closes in one step.

`remove_targets` handles Targets that must exist while the Ticket runs—for example, a
frozen comparison baseline—but must not remain in the accepted Project. It is fixed and
bound into the Target Contract during sealing, requires `merge: true`, and may name only
uniquely resolved Targets bound by that Ticket's Criteria. The Targets remain available
throughout development and review.
Acceptance prepares the normal merge candidate first, then removes only the declared
Target definitions and their unambiguously-owned `tests.toml` tables before publication;
shared filesets, sources, parameters, constraints, generators, and hooks remain.

Every review-bound run persists a versioned, machine-readable JSON package at
`logs/<slug>/.runtime/triage-prep/briefing.json`. Human Markdown and HTML views
are rendered from that same package, so a command-line client can inspect the
complete review input without scraping a presentation format. With
`triage_report: true`
(the default), Booley uses the configured model backend after criteria
acceptance to add a self-contained HTML explanation under the ticket log
directory. The triage skill presents its deterministic briefing directly in
chat instead of writing another summary report. Set `triage_report` to `false`
to skip the extra model call; Booley still writes the deterministic JSON
package, with a conservative deterministic assessment and no HTML explanation.
A generation failure is recorded but does not block an otherwise successful ticket;
`booley board prepare-review <slug> --force` retries it.
The same command supports tickets in `blocked/`: generating the full review
package for partial or blocked work is a normal way to inspect its diff,
criteria, scope deviations, and blockers before deciding whether to reset or
archive it. Use `booley board review-briefing <slug>` to render that package.
The triage briefing links directly to the HTML explanation using its
Session-Runtime path. Open that link, then select **Show Preview** in the HTML
editor (or run **Live Preview: Show Preview** from the Command Palette). The
workflow does not emit a `command:` link because VS Code intentionally
disables command URIs in untrusted chat-authored Markdown.

After a ticket enters review, `booley run` emits one stable JSON record even
when the full-screen Console was used:

```text
BOOLEY_RUN_RESULT {"disposition":"review","html_path":"/work/.../explanation.html","review_package_path":"/booley-project/tickets/logs/demo/.runtime/triage-prep/briefing.json","slug":"demo","version":1}
```

Normal progress output may surround this line. Command-line clients should scan
for the `BOOLEY_RUN_RESULT ` prefix; one record is emitted per review-bound
ticket. `html_path` is `null` when no HTML explanation was produced.

**Ticket worktrees live under `.booley_project/worktrees/<slug>`, but they are registered by their in-container path.** Booley is container-only, so the project is `/work` from git's point of view and the registrations record `/work/.booley_project/worktrees/...`. On the host those paths don't exist, so `git worktree list` shows every live ticket worktree as `prunable`:

```
/work/.booley_project/worktrees/axi-fix  0000000 [detached HEAD] prunable
```

That is cosmetic and expected — **do not "clean it up"**. A host-side `git worktree prune` deregisters a worktree an active ticket is still working in, and the run dies in confusing ways. Booley sets `gc.worktreePruneExpire=never` on the repo so background `git gc` can't do it by accident (`booley doctor` checks the setting), but an explicit `git worktree prune` you type yourself still wins. Let the ticket finish and let cleanup remove it, or run the prune from inside the Session Runtime where the paths resolve.

**`.booley_project/` is usually its own git repo, and the outer repo ignores it.** That is the intended layout — your RTL history stays clean of Booley bookkeeping — but it means outer-repo git commands cannot see anything inside it. Restoring an edited `booley.toml` from the project root fails with a pathspec error that never mentions why:

```
$ git checkout -- .booley_project/booley.toml
error: pathspec '.booley_project/booley.toml' did not match any file(s) known to git
```

Run it against the inner repo instead:

```bash
git -C .booley_project checkout -- booley.toml     # restore Booley config
git -C .booley_project status                      # what changed in Booley's own repo
git -C .booley_project log --oneline -5
```

Same rule for anything else under `.booley_project/` — `tests.toml`,
`ticket_creation.md`, the legacy `ticket_defaults.md`, `criteria.toml`, and the `.core`
files. If
`git -C .booley_project rev-parse --git-dir` errors, the directory isn't a repo on this
machine, so those files were never version-controlled: copy one aside before you edit it.

## Running Unattended

Ticket Mode is built for unsupervised, multi-hour runs. The minimum interaction is: create a ticket, then review the results. Everything in between runs on its own. It debugs failures across repeated simulate-fix cycles, resumes where it left off after an interruption (reboot, crash, subscription limit), and blocks a ticket for human triage when it gets stuck rather than guessing. When you triage a blocked ticket, you can retry it with **tagged feedback** to steer the next attempt without starting over.

### Entering the Session Runtime without VS Code

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

`session refresh` is transactional for the headless runtime. It keeps the old
container recoverable until the replacement is running on the reconciled
immutable image ID and an isolated in-container probe confirms the expected
Booley payload. It refuses to replace a runtime currently owned by VS Code;
use the editor's **Dev Containers: Rebuild Container** command in that case.
For a licensed headless runtime, run `booley session down` first so refresh does
not risk replacing the deterministic license-relay topology beneath a recoverable
old container.

These are **host** commands (they need Docker), and `booley init` must have run
first: it builds the image and creates the network, proxy, and reaper. The
container carries the same `booley.role=interactive` label as the VS Code one,
so the idle reaper owns its lifecycle either way. `booley session enter` is the
headless equivalent of a container terminal, so every container-only command
works through it.

An explicit command after `--` runs as one supervised Runtime Attachment
execution. `Ctrl-C`, `SIGTERM`, a lost Docker attachment, or an expired host
heartbeat requests scoped cancellation inside the runtime. Booley escalates
through a bounded grace period, reaps descendants even when they create a new
session, and returns only after the complete owned process tree is terminal. A
second interrupt requests immediate force cleanup. Normal exit codes and the
usual `128 + signal` shell convention are preserved; if the command handles an
interrupt and exits normally, its own exit code wins. If a pre-refresh Session
Runtime does not support the execution protocol, the command fails with exit
125 and tells you to run `booley session refresh`.

Each execution identity is inherited by its descendants and any Job leases they
hold. If the original supervisor disappears or leaves an incomplete record,
lease recovery signals only processes carrying that identity and releases the
slot after their durable identities are terminal. This fallback also covers an
interrupt arriving after the root command exits while descendants are still
being reaped; that interrupt retains the expected signal-derived host status.

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

Configure an [ntfy.sh](https://ntfy.sh) topic in `booley.toml` to get a push
notification when a ticket completes, blocks, or an automatic Doctor run finds
an issue. No need to watch the terminal. Install the ntfy.sh app on your phone
to receive them.

## When Booley itself misbehaves

A Booley Flow exits 2 with nothing useful on stderr, a doc describes a knob that isn't
there, a message reads like a crash when nothing crashed. Run the
**`/booley-feedback`** skill in the agent chat while the failure is still on
screen — it captures the reproduction, checks the claim against Booley's own
source before blaming it, scrubs your project's identifiers, and shows you the
exact text before anything is sent. Nothing leaves your machine unless you say
yes to that text.

Nothing broke but something was confusing? That is worth reporting too. Tell
the skill where it happened and what you expected instead; it will not ask for
a reproduction. Submission is host-only (the sandbox's egress proxy doesn't
allowlist github.com, and there is no mail client in there either), and
`[feedback] mode` in `booley.toml` decides where the offer points: a public
GitHub issue by default, a private mail to the maintainer with `"email"`, or
nothing at all — see [CONFIG.md](CONFIG.md#feedback-feedback).

## Telling Booley what you think

Nothing has to be broken. Tell `/booley-feedback` what you liked, what grated,
what you wish existed, or whether Booley earned its keep on your project.

One sentence is a complete report — there is no reproduction to give and none is
asked for. It lands in the same log, gets the same redaction, and is offered
upstream through the same preview-and-confirm path as a bug, so nothing leaves
your machine until you have read the exact text and agreed to it.

Why bother: bug reports say what is broken, never whether the thing is worth
using. Which parts earn their keep, which cost more than they give, what you
wanted and didn't find — that is what decides what gets built next, and it is the
one kind of report almost nobody sends unasked. A blunt "this wasn't worth the
setup cost on our project" is as useful as praise, and a lot rarer.

## CLI reference

`booley --help` labels every top-level command as `[host]`,
`[Session Runtime]`, `[either]`, or `[mixed]`. Session Runtime commands run after
VS Code accepts **Reopen in Container**, or through `booley session enter` in a
headless environment. Mixed commands enforce location at their nested
operation.

The host-owned Project Inventory records roots initialized by successful
`booley init` runs. Existing Projects can be imported with an explicit,
bounded discovery scan:

```bash
booley projects                         # roots, status, and grants
booley projects discover ~/workplace    # scan only this directory tree
booley projects --json                  # stable machine-readable listing
booley projects forget /old/project     # only after all grants are revoked
```

Missing and uninitialized roots remain visible so their host administration can
be cleaned up. Use the exact absolute path printed by `booley projects` to
revoke a grant even after its directory has been deleted.

```bash
# Execute a single ticket end-to-end
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
booley cheat --flows --runtime
booley cheat --board
booley cheat --commands --project

# Run diagnostics
booley doctor

# Run real smoke checks against marked sim/lint/synthesis Targets
# (marked FPGA Targets get explicit manual implementation commands)
booley doctor --deep

# Release smoke only: omit credentials and the live Developer probe
booley doctor --deep --skip-agent-checks
```

Every manual doctor run that ends with zero FAILs and zero active WARNs records
a **freshness stamp** into project runtime state. Automatic results are stored
separately so an in-container audit cannot overwrite evidence from host-only
checks. When the Session Runtime starts, Booley launches a one-shot, non-deep
Doctor audit if the previous automatic result is older than a week or its
configuration inputs changed. The start of `booley run` performs the same check
synchronously as a fallback before unattended work begins. Automatic runs never
repair guidance links or move orphaned tickets, and they never block work;
manual `booley doctor` retains those repairs.

The latest structured result and human-readable transcript live under
`.booley_project/runtime/doctor/last.json` and `last.log`. Changed findings are
reported by `booley session up`, `booley run`, `booley_status`, and the next
Interactive Mode Booley Flow result. An unresolved result is retried after one day
rather than on every container start; a clean result is checked weekly.
`--deep` is never automatic.

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
cap): interactive work has priority over ticket work, a running Job is never
preempted, and a queued Job can be cancelled with the `booley_cancel` MCP tool
(queued Jobs only). A submit is refused (`BLOCKED`) only when a class queue
itself is full (`queue_max`, default 8).

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

On a subscription, usage counts against that plan's limits: Booley detects a subscription/usage cap, waits, then requeues the ticket rather than failing. With an API key it's pay-per-token. With several credentials present, the agent CLI, not Booley, picks one, in its own order. For Claude that order is an exported `ANTHROPIC_API_KEY` first, then `CLAUDE_CODE_OAUTH_TOKEN` (the credential `booley auth` stores, below), then the subscription login; for Codex, an exported `OPENAI_API_KEY` outranks the `auth.json` login. Either way an exported API key outbids everything else, including a stored `booley auth` token. `booley init`, `booley doctor`, and `booley auth --status` report the credential that actually wins, and name anything it overrides. To *pin* the choice instead of leaving it to the environment, set `[agent] auth = "subscription"` (Booley then scrubs the API key from agent environments) or `"api_key"` (fails loud when the key is missing). See [CONFIG.md](CONFIG.md#pinning-what-bills-agent-auth).

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
