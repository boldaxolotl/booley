# Usage

This guide shows how to use Booley day to day. You don't need any experience
with AI coding agents to follow it.

**Contents**

1. [Read this first](#read-this-first)
2. [Check your setup](#first-verify-your-setup)
3. [Choose how to work](#choose-how-to-work)
4. [Interactive Mode](#interactive-mode): [first session](#open-your-first-developer-agent-session), [good prompts](#write-a-useful-prompt), [reviewing changes](#what-the-developer-agent-is-allowed-to-do), [editing instructions](#editing-project-instructions), [waveforms](#viewing-waveforms)
5. [Entering the Sandbox without VS Code](#entering-the-sandbox-without-vs-code)
6. [Auth & billing](#auth--billing)
7. [Reporting problems and feedback](#when-booley-itself-misbehaves)

Goal Mode has its own guide, [GOAL-MODE.md](GOAL-MODE.md). Every command,
Flow, and Specialist is listed in [CLI-REFERENCE.md](CLI-REFERENCE.md).

## Read this first

This guide picks up after installation and project setup. It assumes that
`booley init` and the `booley-setup` skill have both finished. If they haven't,
install Booley using [INSTALL.md](INSTALL.md), then follow
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

Both run inside the **Sandbox**, a Docker container that holds the EDA tools
and keeps the Developer Agent away from the rest of your computer.

Sandbox limits and network egress for both execution modes are host-owned
`[sandbox]` settings; see [Host configuration](SANDBOX.md#host-configuration-configtoml).

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

`-C/--project PATH` runs a command against another checkout or worktree (any
directory inside it works), e.g. `booley -C ../other targets` or
`booley flow lint --target lint --project ../other`. Details:
[CLI-REFERENCE](CLI-REFERENCE.md#selecting-a-project).

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
  [installation instructions](INSTALL.md).
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
work, with one linked worktree and Goal Branch per session; see
[GOAL-MODE.md](GOAL-MODE.md).

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

    From then on you are using that CLI's own chat. To run several sessions,
    open more container terminals.

    Arguments after `booley chat` go to that CLI unchanged. To resume an
    earlier conversation, run `booley chat --resume <id>` (Claude) or
    `booley chat resume --last` (Codex).

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

## Entering the Sandbox without VS Code

**Reopen in Container** needs VS Code. Without it (e.g. over SSH),
`booley session` starts the same container. Run these on
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
the new one is ready. If your EDA tools need a license server, run
`booley session down` before refreshing.

`up` and `enter` reuse a running Sandbox, including one VS Code opened.
`refresh`, `down`, and `up --rebuild` leave a VS Code-owned container alone:
close the window, or use **Dev Containers: Rebuild Container**.

## Auth & billing

The Developer Agent and Specialists use the provider set in `[agent] provider` in `booley.toml`:
`claude` (the default) or `codex`. Each one signs in the same way its own app
does, with either a **subscription** or an **API key**:

| Provider | Subscription | API key |
|---|---|---|
| `claude` | Claude Pro/Max/Team/Enterprise (your login in `~/.claude/.credentials.json`) | `ANTHROPIC_API_KEY` |
| `codex` | your Codex login (`codex login`, saved in `~/.codex/auth.json`) | `OPENAI_API_KEY` |

Both work in Interactive Mode and Goal Mode.

- **Subscription:** usage counts against your plan. When you hit a plan
  limit, Claude calls wait and retry; Codex calls stop until the limit resets.
  Resume an interrupted Goal session with `goal_status(rules=true)`.

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

Use the report path the skill prints.
