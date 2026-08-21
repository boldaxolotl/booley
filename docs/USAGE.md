# Usage

How to drive Booley day to day. No previous experience with LLM agents is
assumed.

## Read this first

This guide starts after installation and project setup. It assumes `booley
init` and the `booley-setup` skill have finished successfully; if they have not,
install Booley from the [README](../README.md#installation), then follow
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
vocabulary, [CONTEXT.md](CONTEXT.md). Refer to it whenever a term is unfamiliar;
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

For the fastest orientation, start with `booley cheat`. It gives a compact
overview of every public CLI command, the editable `.booley_project` files,
Flows, Specialists, Criteria, Targets, skills, artifacts, and runtime commands.
Print the whole sheet or use `booley cheat --list` and combine section flags,
such as `booley cheat --commands --project`.

`booley doctor --deep` goes further and runs real smoke sims/lints/synthesis, but
that one needs the Session Runtime; both it and the full command set are in the
[CLI reference](#cli-reference) below.

Credential-free release automation can use
`booley doctor --deep --skip-agent-checks`. Doctor reports the agent credential
inspection, Ticket Mode backend-health check, and live Developer authorization
probe as skipped; every non-agent project, runtime, Ticket Mode, and EDA check
still runs. This flag is for smoke tests, not the normal setup gate before an
agent session.

If `booley` is not found, return to the [installation instructions](../README.md#installation).
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
   `.booley_project/booley.toml`—either
   [Claude Code](https://code.claude.com/docs/en/quickstart):

   ```bash
   claude
   ```

   or [Codex](https://developers.openai.com/codex/cli):

   ```bash
   codex
   ```

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
[ARCHITECTURE.md](ARCHITECTURE.md#interactive-mode). If `booley` doesn't show
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
complete set of built-in capabilities. **Execution** says whether a Booley Flow or Specialist can
run in the container (`sandbox`), on your computer (`host`), or both; the agent
normally uses the default. **Sets** names the acceptance criteria that the Booley Flow or Specialist
can satisfy in a ticket.

Which EDA program runs underneath is determined by the Target. The currently
supported programs are tracked in [SUPPORTED-EDA-TOOLS.md](SUPPORTED-EDA-TOOLS.md).

The catalogs are generated from the MCP tool registry. `booley cheat --flows` and
`booley cheat --specialists` print them live as separate sections; the combined
reference below is also embedded in
[ARCHITECTURE.md](ARCHITECTURE.md#the-sandbox).

<!-- BEGIN GENERATED: flows -->
**Booley Flows**

Deterministic end-to-end orchestration; no LLM:

| Booley Flow | Purpose | Sets |
|--------|---------|------|
| `elab` | Compile + elaborate RTL/TB for one or more Targets (no simulation) | `elab*` |
| `sim` | Run RTL simulation for one or more Targets | `sim_pass` |
| `lint` | Run lint for one or more Targets | `lint_clean` |
| `synth` | Run ASIC synthesis for one or more Targets with optional baseline comparison | `synthesis_ok` |
| `fpga` | Run FPGA implementation for one or more Targets with optional baseline comparison | `fpga_impl_ok` |

Common controls: `--target <name,...>` selects Target(s); `--dry-run` prints commands without executing them; `booley flow <name> --help` shows the full contract.

Key Flow-specific controls:

- `elab`: `--standalone` also proves every RTL module elaborates from its declaring file
- `sim`: `--test <name>` selects a test, `--skip <name,...>` excludes tests, and `--trace` captures waveforms for the simulation run
- `lint`: `--scope <file,...>` filters reported findings to selected files
- `synth`: `--baseline <ref>` compares metrics against a git revision; `--default-clock <ps>` explicitly supplies a clock only when the Target has no SDC
- `fpga`: `--baseline <ref>` compares metrics against a git revision; `--no-cache` forces a fresh implementation

**Specialists**

LLM-backed sub-agents running in scoped, isolated workspaces:

| Specialist | Purpose | Sets | Modifies code |
|------------|---------|------|:-------------:|
| `mutation_tester` | Lock-based mutation testing: creator designs muxed RTL once, tester runs deterministic sim loop | `mutation_score` | — |
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
| `rtl` | `optimization` | Strict power/performance/area improvements with no functional or engineering trade-off | `review_rtl_optimization` |
| `rtl` | `security` | Fault-injection resistance, simple power/timing leakage, secret exposure, and unsafe failure behavior | `review_rtl_security` |
| `tb` | `quality` | False-pass paths, missing checks and edge cases, coverage gaps, timing/sampling mistakes, and TB code quality | `review_tb_quality` |

Controls: `--scope <file,...>` selects files; `--diff-ref <git-ref>` reviews only the diff; repeatable `--steer` adds review context. The `spec` focus needs the ticket/spec text: Ticket Mode resolves it automatically, while Interactive Mode uses `--ticket <path>`.

#### `mutation_tester`

Read-only, lock-based mutation testing. An LLM creator inserts output-observable single-point RTL mutations once; deterministic baseline and mutant simulations then measure how many the Target's complete test suite detects. The creator can target operator/comparison/polarity/bit-select changes, reset values, FSM next-state logic, and LHS/signal swaps.

**Mutation campaign modes:**

| Campaign | Ticket Mode (`mandatory` or `optional`) | Standalone CLI options |
|----------|-----------------------------------------|------------------------|
| Default fixed | Target campaign with `target` + `scope` — generate 10 mutations and require all 10 detected | _(no goal options)_ — the same 10-of-10 campaign |
| Explicit fixed | add `total: N` and `min_detected: K` | `--count N` requires all N; add `--min-detected K` to require K |
| Complexity-scaled | add `auto: true` — choose 3-25 mutations from RTL complexity and the time budget | `--count auto`; add `--min-detected K` for an explicit threshold |

Standalone `--dry-run` prints the complexity breakdown and proposed auto count without running mutations.

Targeting and reuse: `--scope <rtl-file,...>` chooses mutation sites; `--target <sim-target>` chooses the complete runnable Target suite; `--steer <context>` biases mutation selection. A valid lock is reused on later runs, so new steering takes effect only with `--regen-lock`. Standalone calls can override module discovery with `--dut-top`, `--dut-files`, and `--tb-top`.
<!-- END GENERATED: flows -->

The `Sets` column names the [acceptance criteria](#acceptance-criteria) each Booley Flow or Specialist can satisfy (per-target families expand per project Target, e.g. `sim_pass_{target}`). `coverage_analyst` and `tb_coder` also exist but are hidden until they mature (see [ROADMAP.md](ROADMAP.md)); the Developer Agent authors testbenches itself.

### Running a Booley Flow directly

The escape hatch, not the front door: **`booley flow`** runs one by hand. Reach for it when there is no agent in the loop: validating a flow during setup, or reproducing a failure yourself.

```bash
booley flow lint --target lint_soc
booley flow sim --target sim_soc --test reset
booley flow synth --target synth_soc
booley flow                       # list the available Booley Flows
booley flow lint --help           # the Flow's own help
```

Everything after the Flow name is passed to the Booley Flow verbatim, and its exit code comes back verbatim too. Booley Flows use exit codes meaningfully, and the same three grades mean the same thing across every Flow:

| Exit | Meaning | Example |
| --- | --- | --- |
| **0** | The Booley Flow ran and the design is clean | no lint warnings; elaboration succeeded |
| **1** | The Booley Flow ran and **the design failed** | lint warnings remain; the compiler rejected the RTL |
| **2** | **The Booley Flow could not reach a verdict** | linter binary missing, the generated build description (EDAM) or configure blew up, timeout |

The distinction that matters is exit 1 vs 2: exit 1 is a *result about your RTL*, not a crash, and exit 2 means nothing was learned about the RTL at all. An undeclared identifier is exit 1 from both `lint` and `elab`: a design defect, reported the same way by whichever Booley Flow happens to catch it first.

**Over MCP, the verdict is the `EXIT_CODE:` line — never `isError`.** An agent calling these Booley Flows through MCP gets `isError: false` on essentially every call, including a design that failed hard. That is not a bug: MCP's `isError` reports whether the *MCP tool ran*, and a lint run that found 40 violations ran perfectly. The verdict travels in the result body, whose first line is `EXIT_CODE: <n>` with exactly the three grades above (and the same number in `structuredContent`, as `reports[0].exit_code`, alongside a `passed` boolean). An agent — or a custom wrapper — that keys off `isError` will read every failing design as a pass. Key off `EXIT_CODE`.

Custom MCP tools in `.booley_project/mcp_tools/` are discovered alongside the built-ins. Every valid implementation is agent-enabled by default; `[flows.<name>].enabled = false` is the explicit opt-out for a Flow, while `[mcp_tools.<name>].enabled = false` opts out a Specialist or other non-Flow endpoint. The old `[tools].builtin` and `[tools].custom` lists are migration errors.

`booley flow` deliberately discovers without the project `enabled` filter. It is the human/porter escape hatch that must find implementations while `booley.toml` is incomplete, although an individual Booley Flow may still report that its configured flow is disabled when invoked. Direct discovery also does not reproduce MCP mode filters: Interactive Mode hides autonomous-only `submit_run_report`, while a direct diagnostic run is not an interactive MCP surface. If an agent cannot see an MCP tool that `booley flow` lists, check its `enabled` setting, custom MCP tool syntax and literal metadata, the current mode, and whether the Session Runtime needs restarting (see [MCP-TOOLS.md](MCP-TOOLS.md#default-discovery-and-explicit-opt-out)).

To see what `--target` values exist in the first place, use **`booley targets`**:

```bash
booley targets                    # every .core Target, grouped by core
booley targets --for sim          # only Targets that the Booley Flow could drive
booley targets 'sim_*'            # glob filter (bare name or vendor:lib:name#target)
booley targets sim_soc          # resolved detail view: parameters, files, SDC/XDC
booley targets --json             # machine-readable (composes with all of the above)
```

The listing is a cheap `.core`-YAML read (works host-side too) and marks each Target wired via `[flows.*].default_target` with `←`; only the single-Target detail view runs `fusesoc run --setup`, so run that one inside the Session Runtime. Agents get the same listing as the `booley_targets` MCP tool.

**Qualifying an ambiguous name.** When two cores declare the same Target name (normal in a multi-core repo), pass `vlnv#target` — the core's FuseSoC coordinate, a `#`, then the Target: `--target 'lowrisc:ibex:ibex_top#lint'`. The VLNV part can be shortened to any unambiguous suffix (`ibex_top#lint` works), and `booley targets` prints the shortest form that resolves. Quote it: `#` starts a comment in most shells.

That selector is **Booley's**, not FuseSoC's. Running `fusesoc` by hand — reproducing a build outside Booley, say — the same Target is two separate arguments, and passing the joined form fails with `Illegal character in core name`:

```bash
booley flow lint --target 'lowrisc:ibex:ibex_top#lint'     # Booley
fusesoc run --target lint lowrisc:ibex:ibex_top            # raw fusesoc
```

Booley does that split for you: it strips the qualifier and hands FuseSoC `--target <name> <vlnv>`.

To create or rename a Target, see
[Target authoring](CONFIG.md#target-authoring). Those configuration rules are
not needed to select and run an existing Target.

> **`synth` is a PPA estimate, not tape-out synthesis.** Power/performance/area numbers fast enough to iterate the RTL against; real tape-out sign-off is **out of scope for Booley**. See [SUPPORTED-EDA-TOOLS.md](SUPPORTED-EDA-TOOLS.md#built-in-flows).

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
   it summarizes the shared understanding for confirmation and synthesises an
   `## Implementation Plan` into the ticket body. That plan is what the Developer Agent
   builds against, so the back-and-forth is the point, not a formality.
4. **Review the draft, and read the criteria hardest.** It shows the inferred fields (flagging anything missing or uncertain), then a numbered **Mandatory / Optional** criteria menu. Criteria are the entire contract: they are what the harness gates on, and prose in the ticket body gates nothing. Toggle by number, adjust thresholds, add your own. Give `scope` the same scrutiny; it's what keeps the agent out of unrelated files.
5. **Approve.** It shows the complete ticket and asks. Nothing is written until you say yes.
6. **It writes and queues it.** The draft lands in `board/drafts/`, gets validated, and then `booley board move <slug> queue` stamps it and moves it to `board/queue/` — or `board/waiting/` if it declares dependencies on other tickets.

Queuing a ticket doesn't start it. Tickets sit in `board/queue/` until you start Ticket Mode with `booley run` in a container terminal; that loop then pulls tickets off the queue one after another without further input. Use `/booley-ticket-triage` to work through blocked, failed, and finished ones.

**Writing a ticket by hand** works too. The template ships inside the installed
package at `booley/data/skills/booley-ticket-create/TICKET_TEMPLATE.md`; print
its path with

```bash
python -c "import booley, pathlib; print(pathlib.Path(booley.__file__).parent / 'data/skills/booley-ticket-create/TICKET_TEMPLATE.md')"
```

Copy that file to `board/drafts/<slug>.md` and fill it in (or run `booley board create <slug>`, which writes a stub there for you). Then validate and queue it:

```bash
python -m booley.ticket_board validate-ticket <path>   # a path, not a slug
booley board move <slug> queue                         # draft -> queue, stamps created/last_update
booley board show                                      # where everything stands
```

`booley run --ticket <slug> --dry-run` additionally checks the setup without executing anything. Note that a draft is never picked up: `booley board move` is what puts it in front of the run loop, and it is the only verb that changes a ticket's state by hand (targets: `queue` or `done` — the rest of the transitions belong to the run loop).

**Directory names and status names are not the same word.** `booley board show` prints the ticket's *status*, while the file lives in a same-meaning but differently-named directory. Two of the eight differ:

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

### Acceptance Criteria

A ticket doesn't describe *steps*: it declares **acceptance criteria** (split into `mandatory` and `optional`), and the harness, not the agent, decides when they're met. A criterion is satisfied only by a valid verdict from the Booley Flow or Specialist that owns it (e.g. a simulation criterion needs `sim` to return `pass`; a `review_*` criterion needs a `reviewer` run), never by the Developer Agent asserting success, and it is re-checked whenever the underlying code changes. **A ticket cannot reach review with an unmet mandatory criterion.** Optional criteria do not block review, but the Developer Agent must justify every optional criterion it could not complete; `submit_run_report` rejects the report until that explanation is supplied, and final acceptance rejects a stale report that does not cover the currently unmet set. This applies even when routine run reports are disabled. See [ARCHITECTURE.md](ARCHITECTURE.md#ticket-mode) for the criteria mechanics.

The supported criteria families are defined once in `criteria.toml` and listed below; `{target}` denotes a per-target expansion (one criterion per project Target). `booley cheat` renders this same table live, including any project-defined criteria. A bare `review_*` ticket key expands to `_clean`: every finding must be verified fixed or explicitly waived with user-visible justification. Use an explicit `_done` suffix for a terminal advisory review whose findings are reported but not fixed in that ticket run. Both modes become stale after relevant source changes.

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
| `review_rtl_optimization` | RTL review: missed power/performance/area wins, strict improvements only, no trade-offs (post-sim) | `reviewer --category rtl --focus optimization` | post-sim |
| `review_rtl_security` | RTL review: hardware attack resistance to fault injection, simple power/timing analysis, and secret exposure (post-sim) | `reviewer --category rtl --focus security` | post-sim |

#### Testbench Review

| Criterion | Description | Set by | Workflow Region |
|-----------|-------------|--------|-------|
| `review_tb_quality` | TB review: false-pass detection, coverage gaps, and TB code quality | `reviewer --category tb --focus quality` | pre-sim |

#### Simulation

| Criterion | Description | Set by | Workflow Region |
|-----------|-------------|--------|-------|
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

#### Synthesis / FPGA threshold flavours

<!-- BEGIN GENERATED: criteria-params -->
Per-target `synthesis_ok` / `fpga_impl_ok` criteria accept optional threshold **params**. Each takes a `targets:` list, the per-target scoping key naming which project Targets to check (the key is `targets`, never `configs`), plus one or more metric params. Four flavours per metric: two absolute, two relative to the ticket's `base_sha` baseline:

| Flavour param suffix | Baseline? | Meaning |
|----------------------|:---------:|---------|
| `_max` | no | metric must stay **≤** the given value |
| `_min` | no | metric must stay **≥** the given value |
| `_increase_at_most` | yes | metric may grow **at most N%** above baseline |
| `_reduce_at_least` | yes | metric must shrink **at least N%** below baseline |

Syntax (ticket criteria): `synthesis_ok: {targets: [<target>], cell_count_max: 500, fmax_mhz_min: 400}`.

In Ticket Mode, ticket creation seals an immutable Target contract before enqueue. A baseline-relative `synthesis_ok` or `fpga_impl_ok` criterion runs `base_sha` and the ticket head with the same normalized Target recipe. Developer execution cannot change contract controls; a missing or incorrect recipe blocks as `target-contract-change-required` for revision and resealing. Missing or mismatched baseline evidence never skips a relative check.

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
<!-- END GENERATED: criteria-params -->

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
  triage_report: true     # prepare rich HTML explanation before review
```

`destination: review` parks the finished ticket in `board/review/` for you to look at, and **keeps its worktree and branch** — `cleanup: true` is not ignored, it's deferred: it runs when you close the ticket with `booley board move <slug> done`. `destination: done` skips the pause and merges, cleans up, and closes in one step.

With `triage_report: true` (the default), Booley uses the configured agent
backend after criteria acceptance to prepare a self-contained HTML explanation
under the ticket log directory before moving the ticket to review. The triage
skill presents its deterministic briefing directly in chat instead of writing
another summary report. Set `triage_report` to `false` to skip the extra agent
call. A generation failure is recorded but does not block an otherwise
successful ticket; `booley board prepare-review <slug> --force` retries it.
The same command supports tickets in `blocked/`: generating the full review
package for partial or blocked work is a normal way to inspect its diff,
criteria, scope deviations, and blockers before deciding whether to reset or
archive it. Use `booley board review-briefing <slug>` to render that package.
The triage briefing links directly to the HTML explanation using its
Session-Runtime path. Open that link, then select **Show Preview** in the HTML
editor (or run **Live Preview: Show Preview** from the Command Palette). The
workflow does not emit a `command:` link because VS Code intentionally
disables command URIs in untrusted chat-authored Markdown.

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

Same rule for anything else under `.booley_project/` — `tests.toml`, `criteria.toml`, the `.core` files. If `git -C .booley_project rev-parse --git-dir` errors, the directory isn't a repo on this machine, so those files were never version-controlled: copy one aside before you edit it.

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

These are **host** commands (they need Docker), and `booley init` must have run
first: it builds the image and creates the network, proxy, and reaper. The
container carries the same `booley.role=interactive` label as the VS Code one,
so the idle reaper owns its lifecycle either way. `booley session enter` is the
headless equivalent of a container terminal, so every container-only command
works through it.

## Scope

Each ticket declares the files it's expected to touch. That's a plan, not a
fence: if finishing the job genuinely needs a file the ticket didn't name — a
shared package, a `.core` fileset, a neighbouring module — the agent edits it
and the change lands on the branch like any other. Booley records every such
file in `.runtime/scope_deviations.json`, and ticket triage shows you the list
so you decide whether each one was justified. Nothing is silently thrown away,
and nothing is blocked on your behalf.

The one hard line is Booley's own bookkeeping — development state, criteria,
ticket files, `booley.toml`. Those are the record your run is graded against,
so a commit touching them is rejected outright.

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

The whole `booley` command set. A few are general (`cheat`, `doctor`, `targets`
from the [orientation block](#first-verify-your-setup) above); the rest drive
Ticket Mode. Run these from a terminal **inside the Session Runtime**: open the repo in VS
Code and accept **Reopen in Container** first, or, with no VS Code, enter it
headlessly with `booley session enter` (see
[Entering the Session Runtime without VS Code](#entering-the-session-runtime-without-vs-code)). (`booley
cheat` works anywhere; `booley doctor` works on either side; `booley init` and
`booley session` are host-side.)

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
booley cheat --commands --project

# Run diagnostics
booley doctor

# Run real smoke checks against the first applicable sim/lint/synthesis Targets
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
> and points you to **Reopen in Container** (or `docker exec` into the running
> session). `booley init` is the host-side counterpart and refuses inside the
> container, where Docker is deliberately unavailable.

### Concurrent tickets

Two terms this section leans on (both in the [glossary](CONTEXT.md#execution)): a
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

Booley's LLM agents (the Ticket Mode Developer Agent and the Specialists `reviewer` and `mutation_tester`) run through whichever **agent provider** you select with `[agent] provider` in `booley.toml`: `claude` (the Claude Agent SDK, the default) or `codex` (the Codex CLI). Each provider authenticates exactly as its own app does, with the same two options either way: a **subscription** or an **API key**:

| provider | subscription | API key |
|---|---|---|
| `claude` | Claude Pro/Max/Team/Enterprise, via the OAuth login `booley init` detects at `~/.claude/.credentials.json` | `ANTHROPIC_API_KEY` |
| `codex` | the Codex login (`codex login`, stored at `~/.codex/auth.json`) | `OPENAI_API_KEY` |

Under either provider, both options work for Ticket Mode *and* for Specialists in Interactive Mode: there is no API-key-only restriction.

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
