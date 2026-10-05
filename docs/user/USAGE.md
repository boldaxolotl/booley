# Usage

This guide shows how to use Booley day to day. You don't need any experience
with AI coding agents to follow it.

**Contents**

1. [Read this first](#read-this-first)
2. [Check your setup](#first-verify-your-setup)
3. [Two ways to work](#choose-a-mode)
4. [Interactive Mode](#interactive-mode): [first session](#open-your-first-agent-session), [good prompts](#write-a-useful-prompt), [reviewing changes](#what-the-agent-is-allowed-to-do), [waveforms](#viewing-waveforms)
5. [Ticket Mode](#ticket-driven-workflow): [creating Tickets](#creating-tickets), [acceptance criteria](#acceptance-criteria), [Scope](#scope), [when a Ticket finishes](#where-the-work-lands-on_success), [reviewing results](#ticket-board-lifecycle)
6. [Running unattended](#running-unattended): [several Tickets at once](#concurrent-tickets), [without VS Code](#entering-the-sandbox-without-vs-code)
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
| **Container terminal** | Commands inside the Sandbox, after VS Code has reopened the project in its container | `booley run`, `git diff` |
| **Agent chat** | Plain-language requests and skills that start with `/` | *"Run lint and explain every finding"* |

Command blocks go in the terminal named in the text around them. Sentences in
*italics* are prompts for the agent chat. Skills such as
`/booley-ticket-create` also go in the agent chat, not in a terminal.

Booley uses some terms with exact meanings. When one is unfamiliar, look it up
in the glossary linked from the [context map](../../CONTEXT-MAP.md).

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
`bootstrap`, `projects`, `eda`, and Ticket Mode `run`/`board` retain their grammar
and do not accept the new selector. Full option names are stable; ambiguous
abbreviations produce an error. Arguments after a command's payload `--` stay opaque.

Selection chooses the checkout, while `BOOLEY_PROJECT_DIR` chooses Project data;
an external data override remains supported. Checkout-local data configuration and
initialization's destination scope keep their existing precedence. A selector
requires an existing directory; `init` accepts an uninitialized directory.

Hidden `--project-root`/`-p` aliases remain only where previously supported, for one
compatibility release, with one-line stderr notices. They retain literal path
resolution, while the canonical selector discovers nested directories. Persisted
Sandbox initialization specifications retain internal compatibility with the old
`session prepare` form. `cheat --project-files` selects the Project Files section;
legacy bare `cheat --project` still selects that section with a deprecation notice.

Flows and Specialists accept `--timeout DURATION`: positive integer seconds (`90`
or `90s`), minutes (`30m`), hours (`2h`), or descending combinations (`1h30m`).
The hidden `--timeout-ms` alias keeps millisecond precision and prints a notice for
one compatibility release. MCP parameters and config keys stay in milliseconds.

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
  need, for example `booley cheat --board` or `booley cheat --commands --project-files`.
- `booley doctor --deep` goes further and runs short real simulations, lints,
  and syntheses.

## Choose a mode

Booley has two ways to work. Both use the same configuration, Flows,
Specialists, and Sandbox.

1. **Interactive Mode: start here.** You chat with the agent and watch it work.
   Do the first session below even if you have used coding agents before. It
   shows how Booley picks Targets, runs Flows, and reports results.
2. **Ticket Mode: move on once that feels familiar.** You write down a task as
   a Ticket, and `booley run` does the work on its own, with no chat window
   needed. This is the best way to do well-defined development work.

Interactive Mode stays useful after that for investigations, quick changes,
and one-off Flow runs.

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
> container. (Ticket Mode always works this way.)

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

## Ticket-Driven Workflow

In Ticket Mode you work with Booley like a project lead with an engineer: you
hand over written tasks and review the results. A **Ticket** is a Markdown file
on your machine. It records the task, the files likely to change, and the
checks that must pass. It is not a Jira or GitHub issue, and nothing is sent
anywhere.

The whole loop:

1. **Create.** In the agent chat, type `/booley-ticket-create` and describe the
   change in your own words. The skill asks questions, then shows you the
   finished Ticket. When you approve it, the Ticket gets a short name, its
   **slug** (for example `fix-fifo-backpressure`), and joins the queue.
2. **Run.** In a container terminal:

   ```bash
   booley board show          # confirm the Ticket is queued
   booley run --ticket <slug>
   ```

   Leave it running. The screen shows progress while the agent edits code,
   runs Flows, and checks the Ticket's criteria. It stops when the work is
   ready for review, done, or stuck and waiting for you.
3. **Review.** In the agent chat, type `/booley-ticket-triage` to look at a
   finished or stuck Ticket and decide what happens next.

`booley run` without `--ticket` works through the whole queue, one Ticket after
another.

### Creating Tickets

You don't need a perfect description. Tell `/booley-ticket-create` everything
you know, however messy, and it turns that into a precise Ticket.

- **Pick *Detailed plan*** when it asks, unless the change is truly trivial and
  you already know every file it touches. *Lightweight* skips the questions
  and fills in the Ticket on its own.
- **Answer its questions.** They come in rounds, each with a suggested answer.
  It looks up facts in the code itself instead of asking you.
- **Check the criteria most carefully** when it shows the draft. The criteria
  are the whole contract: Booley only checks those, and the prose in the Ticket
  checks nothing. The `scope` field keeps the agent out of unrelated files. Ask
  for any changes before you approve.
- **Approving finishes the job.** The skill adds any new Targets the Ticket
  needs and queues it. There are no further approval steps.

**Project-wide rules.** If every Ticket in your project should get the same
checks, write them in `.booley_project/ticket_creation.md` in plain Markdown:

```markdown
- Include a corrective security review in every feature Ticket.
- Every Ticket uses the `sim_smoke` and `sim_regression` Targets.
- Feature and refactor Tickets must prove that area does not regress on `synth_area`.
```

No special format is needed. The skill reads this file each time it creates a
Ticket and turns the rules into real criteria. Instructions you give for one
Ticket win over these rules, and the skill tells you when a rule is unclear or
can't be met. Editing the file doesn't change existing Tickets. (Older projects
may call this file `ticket_defaults.md`; it still works.)

**Writing a Ticket by hand** is possible but advanced: a hand-written Ticket
must pass the same checks the skill does for you. Follow
`booley-ticket-create/SKILL.md` and its `TICKET_TEMPLATE.md`, then check the
result with `booley run --ticket <slug> --dry-run`.

### Acceptance Criteria

A Ticket doesn't list steps. It lists **acceptance criteria**: checks that must
pass before the work counts as finished. They are grouped by the Flow or
Specialist that checks them:

```yaml
CRITERIA_MANDATORY:
  LINT:
    lint_core: clean
  SIM:
    sim_core: {all: pass}
  REVIEW:
    rtl: {bugs: clean}
    tb: {quality: clean}
CRITERIA_OPTIONAL:
  SYNTH:
    synth_core: {area_um2_max: 10000, fmax_mhz_min: 400}
```

Booley decides when they pass, not the agent:

- A criterion passes only when its Flow or Specialist says so. For example, a
  `SIM` criterion needs `sim` to report a pass, and a `REVIEW` criterion needs a
  `reviewer` run. The agent saying "it works" never counts.
- When the code changes, criteria that depend on it must pass again.
- **Mandatory** criteria must all pass before the Ticket can reach review.
  **Optional** ones don't block, but the agent must explain each one it
  couldn't meet.

Each `REVIEW` entry needs an outcome. `clean` means every finding must be
fixed, or waived with a written reason. `done` means the review only has to
run: its findings are reported, not fixed.

You rarely write this by hand; `/booley-ticket-create` does it for you. The
[Criteria catalog](#criteria-catalog) lists every criterion under the name
Booley reports it by (for example, `REVIEW: rtl: {bugs: clean}` shows up as
`review_rtl_bugs`). `booley cheat --criteria` prints the same list, including
any your project added.
How criteria are checked is in
[ARCHITECTURE.md](../internals/ARCHITECTURE.md#ticket-mode).

#### Threshold parameters

Synthesis, FPGA, and cycle-count criteria can set limits. A limit is either a
fixed number (`_max`, `_min`) or a change compared with the code before the
Ticket started (`_increase_at_most`, `_reduce_at_least`; percentages need a
`%`). Timing limits can apply to a single clock, such as `clk_i.`:

```yaml
SYNTH: {synth_core: {cell_count_reduce_at_least: 8%, clk_i.fmax_mhz_min: 400}}
CYCLE_COUNT: {sim_coremark: {coremark: {cycle_count_max: 100000}}}
```

`booley cheat --criteria` lists every metric and which limits it accepts.

### Scope

A Ticket names the files it expects to change. That's a plan, not a fence. If
the job really needs another file, such as a shared package or a neighboring
module, the agent edits it, and Booley lists it in
`.runtime/scope_deviations.json` so you see it during review.

Two things are off limits: Booley's own bookkeeping files, and the Targets and
settings prepared when the Ticket was created. If a Target turns out to be
wrong, the Ticket stops and waits for you rather than letting the agent change
it.

If the same file keeps showing up outside the scope, your Tickets' scopes are
probably too narrow.

### Where the work lands (`on_success`)

Each Ticket lists what should happen once its criteria pass:

```yaml
on_success: [triage_report, review, merge, cleanup]
```

**This full list is the default**, and most Tickets keep it:
`/booley-ticket-create` fills in all four, and you only remove the ones you
don't want. (Every Ticket must have the field, so a hand-written Ticket has to
list it too.)

The actions always run in this order, whatever order you write them in, and
each step waits for the one before it. Leave an action out and it's skipped:

1. **`triage_report`**: writes an HTML explanation of the work for the reviewer
   (one extra AI call). It only applies together with `review`.
2. **`review`**: parks the Ticket until you approve it. Its branch and working
   copy stay in place so you can inspect them. Nothing below happens until you
   approve. Without `review`, the Ticket goes straight on to the next step.
3. **`merge`**: merges the work into the destination branch. Leave it
   out to keep that branch untouched.
4. **`cleanup`**: deletes the Ticket's branch and working copy, after the merge
   succeeds if there is one. The accepted commits stay reachable. Leave it out
   to keep them.

So with `review` in the list, nothing reaches your branch until you've
approved it. Without `review`, `merge` and `cleanup` run as soon as the
criteria pass, unless a `_done` review left findings open: then the Ticket
waits for your approval anyway.

**Adding Targets in a Ticket.** Most Tickets use the Targets you already have.
To add one, mark it where the Ticket mentions it, and include `merge`:

```yaml
CRITERIA_MANDATORY:
  LINT: {lint_style (new): clean}
  SIM:
    sim_core_v2 (replaces sim_core): {all: pass}
    ticket_probe (temp): {smoke: pass}
```

`(new)` Targets stay after the merge. `(replaces sim_core)` swaps out the old
Target. `(temp)` Targets exist only to prove this Ticket and are removed
afterwards. A Ticket can't edit or delete existing Targets. The full rules are
in [ADR 0060](../adr/0060-model-target-changes-with-ticket-target-plans.md).

### Ticket Board lifecycle

`booley board show` lists every live Ticket and its status; add `--all` to
include done and archived Tickets from Ticket History. `booley board show
<slug>` finds a Ticket either way. A Ticket usually moves like this:

```text
draft ──► queued ──► running ──► review ──► done
  │          ▲          │           └──────► archived
  └─► waiting┘          └─► blocked ──► queued
```

- **waiting**: it depends on another Ticket and is queued once that one is
  done.
- **blocked**: the agent is stuck and needs you. Answer it with
  `/booley-ticket-triage`; the Ticket goes back to the queue and picks up where
  it left off.
- **review**: the criteria passed and the Ticket waits for your decision.

Each live Ticket is one file, `.booley_project/tickets/board/<slug>.md`, and
its status is kept beside it in `tickets/state/<slug>.json`. Both are ignored
by Git. When a Ticket is done or archived, its document moves to
`tickets/history/<slug>.md`, which Booley commits. Boards made before this
layout need a one-time manual migration; see
[Troubleshooting](TROUBLESHOOTING.md#booley-board-refuses-to-start-the-ticket-board-needs-migrating).

**Reviewing a finished Ticket.** `/booley-ticket-triage` walks you through it.
It shows the diff, the criteria results, any files outside the scope, and, with
`triage_report`, a link to the HTML explanation (open it and choose **Show
Preview**). You then choose one of three things:

1. **Approve** it. It moves to done.
2. **Reset** it. This throws the work away and runs the Ticket again from
   scratch.
3. **Archive** it. If the remaining work needs different criteria, write a new
   Ticket instead.

There is no "send it back for a bit more work". Booley approves exactly the
commits whose criteria passed. If you commit anything during review, triage
helps you move those commits to a separate branch before approving.

**Fixing a blocked Ticket.** Triage can also propose loosening a criterion or
widening the scope when that's what the Ticket needs. You see the exact change
before approving it, and the Ticket then continues with its work kept.

`booley board review <slug> --force` rebuilds the review summary, which is
also handy for looking at a blocked Ticket's partial work. `booley board show
<slug>` displays it.

Two Git surprises are covered in Troubleshooting:
[Ticket worktrees show as `prunable`](TROUBLESHOOTING.md#ticket-worktrees-show-as-prunable-on-the-host)
and [`git` cannot see files under `.booley_project/`](TROUBLESHOOTING.md#git-cannot-see-files-under-booley_project).

## Running Unattended

Ticket Mode is built to run for hours without you. At a minimum, you create a
Ticket and later review the result. In between, Booley:

- keeps simulating and fixing until the criteria pass,
- picks up where it left off after a reboot, crash, or usage limit,
- stops and asks you (**blocked**) instead of guessing when it's stuck.

When you answer a blocked Ticket, you can add feedback to steer the next
attempt without starting over.

`booley run` stops by itself once the queue has been empty for 5 minutes
(`--idle-timeout`, in seconds). Use `--idle-timeout 0` to keep it waiting for
new Tickets forever. It only runs inside the container. On a host terminal it
tells you to reopen in the container, or to use
`booley session enter -- booley run`.

For long runs, set up a login that won't expire mid-run; see
[Auth & billing](#auth--billing).

### Concurrent tickets

To run several Tickets at once, start one `booley run` in each container
terminal. By default two can run together (`[jobs] max_tickets`, see
[CONFIG.md](CONFIG.md#jobs--concurrency-jobs)). Extra runs wait their turn, and
the screen shows their place in line.

The same goes for the individual **Jobs** a Ticket starts, such as one
simulation, one synthesis, or one Specialist run. Each kind has its own limit.
Your interactive requests go ahead of Ticket work, but a running Job is never
interrupted. The agent can cancel a Job if you ask it to.

> **Tip: work in parallel once Booley feels familiar.** Run several Tickets and
> several agent chats at the same time. Each Ticket gets its own working copy
> of the code automatically, but chat sessions share one. To keep two chat
> agents from overwriting each other, see
> [TROUBLESHOOTING.md](TROUBLESHOOTING.md#two-interactive-agents-keep-clobbering-each-others-edits).

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

`session refresh` is safe to interrupt; the old container stays usable until
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

Both work everywhere, in Ticket Mode and in Interactive Mode.

- **Subscription:** usage counts against your plan. Claude rate-limit events
  make Booley wait and retry the agent call, within its retry and timeout
  budgets. Codex usage caps stop the agent call; Booley does not automatically
  resume it at the reset time. A Developer Agent failure leaves the Ticket
  blocked. The Ticket runner may pause for a detected limit, but that pause
  does not requeue an already-blocked Ticket. After the limit resets or you
  resolve a spending or credit cap, use
  `booley board move <slug> queue` to retry it. A standalone Specialist must be
  invoked again.
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
which [acceptance criteria](#acceptance-criteria) each one can satisfy. Which
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

For example: `booley specialist reviewer --category rtl --focus bugs --scope rtl`. The supported module alternative is `python -m booley.specialists.reviewer` with the same flags. Common options are `-C/--project PATH`, `--report-dir`, `--diagnostic`, and `--target` where supported. `--timeout DURATION` accepts positive seconds (`90` or `90s`), minutes (`30m`), hours (`2h`), and combinations (`1h30m`). Existing model-call budgets and minimums remain unchanged; seconds-only providers round up. Hidden `--work-dir` and `--timeout-ms` aliases retain their old units for one compatibility release and print deprecation notices on stderr. Connected agent arguments and configuration stay unchanged.

| Specialist | Purpose | Sets | Modifies code |
|------------|---------|------|:-------------:|
| `coverage_analyst` | Explain one exact coverage.json Campaign and propose advisory next steps | — | — |
| `mutation_tester` | Proposal-locked mutation testing: creator selects exact replacements, tester builds isolated variants | `mutation_score` | — |
| `reviewer` | Single-focus code review: reports issues by severity | `review_*` | — |

#### `coverage_analyst`

Call the `coverage_analyst` Specialist from your connected agent session with `campaign="<exact-coverage.json>"` and optional `instruction="<question>"`. The Analyst explains retained native evidence and proposes advisory next steps; its model only reads evidence. It does not run Simulation, read waveforms, evaluate Criteria, or approve waivers. In Ticket Mode, Booley records its screened Waiver Candidates for a human to accept or reject at Ticket review. When every mandatory Criterion is met strictly or by a verified Provisional Coverage Verdict, submit your run report and finish for human review without blocking or marking strict coverage met. Verified Target sources are optional; stale sources give report-only analysis.

#### `reviewer`

Read-only, single-focus code review. It reports `CRITICAL`, `MAJOR`, and `MINOR` findings. In Interactive Mode, review the selected files using your specification or steering. In Ticket Mode, `_done` reports findings without requiring fixes, but open findings make the Ticket wait for your approval even without `review` in `on_success`. `_clean` requires every finding to be fixed or waived with a justification.

The result links saved review evidence, including rejected proposals for inspection. Rejected proposals do not affect Criteria.

Call the `reviewer` Specialist from your connected agent session with `scope="<file,...>"`, `category="<category>"`, and `focus="<focus>"`.

| Category | Focus | What it checks | Sets |
|----------|-------|----------------|------|
| `rtl` | `bugs` | Functional bug patterns, synthesis hazards, reset/width/signing mistakes, and ifdef/config consistency | `review_rtl_bugs` |
| `rtl` | `protocol` | Bus/protocol rule compliance, handshake behavior, ordering, and clock-domain crossings (CDC) | `review_rtl_protocol` |
| `rtl` | `spec` | Spec compliance: the RTL implements what the ticket/spec requires, no more and no less | `review_rtl_spec` |
| `rtl` | `code_style` | Comments, naming, readability, maintainability, magic values, and assertion/cover-point quality | `review_rtl_code_style` |
| `rtl` | `optimization` | Unused/dead RTL and strict power/performance/area improvements with no functional or engineering trade-off | `review_rtl_optimization` |
| `rtl` | `security` | Fault-injection resistance, simple power/timing leakage, secret exposure, and unsafe failure behavior | `review_rtl_security` |
| `tb` | `quality` | False-pass paths in scoped testbench sources, missing checks and edge cases, coverage gaps, timing/sampling mistakes, and TB code quality | `review_tb_quality` |

Arguments: required `scope="<file,...>"` selects files; `steer=["<context>"]` adds review context; `dry_run=true` validates and previews without invoking an agent. The `spec` focus needs specification text: Ticket Mode resolves its mounted ticket or linked spec automatically, while Interactive Mode uses `spec="<path>"`.

#### `mutation_tester`

Proposal-locked mutation testing. A read-only LLM creator returns exact source replacements; Booley runs a pristine baseline, then compiles and tests each replacement in isolation. It does not parse HDL or inject runtime selectors.

**Mutation campaign modes:**

| Campaign | Ticket Mode (`mandatory` or `optional`) | Interactive Mode arguments |
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

In a Ticket, add a [coverage criterion](CONFIG.md#native-coverage-configuration)
for each Target that needs one. Only a passing coverage run satisfies it. The
Analyst never changes criteria. In a Ticket it may record Waiver Candidates;
if counting them would meet the criterion, the Ticket goes to `review` and you
accept or reject each one with `booley board approve <slug> --accept-waivers
ID,... --reject-waivers ID,...`. See
[FLOW_REFERENCE.md](FLOW_REFERENCE.md#coverage-waivers-at-review).

## Criteria catalog

Every built-in criterion, under the name Booley uses in reports and
`booley board show`. In a Ticket you write them grouped by Flow or Specialist
instead; see [Acceptance Criteria](#acceptance-criteria). `{target}` means one
criterion per Target (for example `sim_pass_sim_core`). Criteria are defined in `criteria.toml`, which
is also where a project can add its own.

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
| `review_rtl_spec` | RTL review: spec compliance (RTL matches the ticket/spec, no more, no less) | `reviewer` (`category="rtl"`, `focus="spec"`) | pre-sim |
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
# Tickets (container)
booley run --ticket <slug>        # run one Ticket
booley run                        # work through the whole queue
booley run --dry-run              # check the setup without running anything
booley run --idle-timeout 0       # keep waiting for new Tickets forever
booley board                      # show the Ticket board
booley board --all                # ...including done and archived Tickets

# Quick reference
booley cheat                      # the whole cheatsheet
booley cheat --list               # its section names
booley cheat --criteria           # one section (combine as many as you like)

# Health checks
booley doctor                     # check setup
booley doctor --concise           # show only problems
booley doctor --deep              # also run short real sims, lints, and syntheses
booley doctor --deep --skip-agent-checks   # CI: skip the login checks

# Projects on this machine (host)
booley projects                   # list known projects
booley projects discover <dir>    # find existing projects under <dir>
```

`booley projects discover` stops descending at each initialized Project to
avoid scanning its RTL, vendor, and build trees. Nested Projects are not imported
by that scan; run `booley projects discover <nested path>` on a nested Project
directly to import it.

Booley also runs a quick health check on its own when the container starts and
before `booley run`: about once a week, daily while problems remain, and
whenever the configuration changes. It never blocks work. New problems show up
in `booley session up`, `booley run`, and the next Flow result. The last result
is in `.booley_project/runtime/doctor/last.log`.

Plain Doctor and `booley session up` also report whether prior deep validation is
current or due. Deep evidence has no expiry; automatic change triggers are only
the Booley version and active immutable Sandbox Image. Project/Target/RTL edits
leave that evidence intact. Deep due is advisory and never launches deep checks.
See [Doctor health and deep validation](DOCTOR.md) for qualification, failed and
cancelled attempts, and image identity handling.

Each Ticket run ends with one `BOOLEY_RUN_RESULT` line of JSON for scripts;
`booley cheat --board` describes it.
