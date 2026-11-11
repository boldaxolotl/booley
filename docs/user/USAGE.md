# Usage

This guide shows how to use Booley day to day. You don't need any experience
with AI coding agents to follow it.

**Contents**

1. [Read this first](#read-this-first)
2. [First, verify your setup](#first-verify-your-setup)
3. [Interactive Mode](#interactive-mode): [first session](#open-your-first-developer-agent-session), [what to ask for](#what-you-can-ask-for), [skills](#booley-skills), [good prompts](#write-a-useful-prompt), [long runs and results](#long-runs-and-results), [dashboard](#the-dashboard), [parallel work](#several-agents-at-once), [reviewing changes](#what-the-developer-agent-is-allowed-to-do), [editing instructions](#editing-project-instructions), [waveforms](#viewing-waveforms)
4. [Reporting problems and feedback](#when-booley-itself-misbehaves)

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
  LLM-powered capabilities that each do one focused job. See
  [Specialists](SPECIALISTS.md).

Both run inside the **Sandbox**, a Docker container that holds the EDA tools
and keeps the Developer Agent away from the rest of your computer.

**Where you type.** This guide uses three places:

| Place | What goes there | Example |
| --- | --- | --- |
| **Host terminal** | Commands on your own computer, outside Docker | `booley doctor`, `booley auth` |
| **Container terminal** | Commands inside the Sandbox, after VS Code has reopened the project in its container | `booley dashboard`, `git diff` |
| **Developer Agent chat** | Plain-language requests and skills that start with `/` | *"Run lint and explain every finding"* |

Command blocks go in the terminal named in the text around them. Sentences in
*italics* are prompts for the Developer Agent chat. Skills such as
`/booley-goal` also go in the Developer Agent chat, not in a terminal.

Booley uses some terms with exact meanings. When one is unfamiliar, look it up
in the glossary linked from the [context map](../../GLOSSARY-MAP.md).

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
- If Doctor reports a failure or warning, or says Booley's version changed,
  type `/booley-heal` in a Claude Code or Codex chat. It fixes what it safely
  can and tells you exactly what you need to do yourself. After a version
  change it also walks you through the
  [upgrade review](DOCTOR.md#upgrade-review).
- For a quick overview of Booley, start with `booley cheat`. It lists the
  available sections; print the ones you need, for example `booley cheat --goals`
  or `booley cheat --commands --project-files`. Use `booley cheat --all` for the
  whole cheatsheet; `booley cheat --list` also shows the section list.
- `booley doctor --deep` goes further and runs short real simulations, lints,
  and syntheses. Run it in a container terminal.
- Booley also runs plain Doctor by itself: when the container starts, about once
  a week, and after configuration changes. It never blocks work; new problems
  show up in the next Flow result. When it says a deep check is **due** (after a
  Booley or Sandbox Image update), run `booley doctor --deep`. See
  [DOCTOR.md](DOCTOR.md#automatic-checks).

## Interactive Mode

### Open your first Developer Agent session

1. Open your RTL project folder in VS Code (**File → Open Folder**).

2. When VS Code offers **Reopen in Container**, accept it. If the offer doesn't
   appear, open the Command Palette (`Ctrl+Shift+P`) and run **Dev Containers:
   Reopen in Container**. The first start can take several minutes. When it's
   done, the lower-left corner of the window says you are in a Dev Container.
   No VS Code (for example, over SSH)? See
   [Entering the Sandbox without VS Code](SANDBOX.md#entering-the-sandbox-without-vs-code).

3. Open **Terminal → New Terminal**. This is a **container terminal**. Start
   the Developer Agent:

    ```bash
    booley
    ```

    `booley` (or `booley chat`) just starts the Developer Agent chosen by
    `[agent] default` (or its older name `provider`) in `.booley/booley.toml`:

    | Provider | Same as running |
    | --- | --- |
    | `claude` | `claude` ([Claude Code](https://code.claude.com/docs/en/quickstart)) |
    | `codex` | `codex` ([Codex CLI](https://developers.openai.com/codex/cli)) |

    From then on you are using that CLI's own chat. To run several sessions,
    open more container terminals and run `booley` in each. While one session
    is working in the main checkout, the next one starts in its own worktree;
    see [Several agents at once](#several-agents-at-once).

    `booley chat --cli codex` opens another Agent CLI instead, as long as it is
    listed in `[agent] clis`; otherwise it stops and lists the allowed ones. In
    the Sandbox, Booley currently sets up (sign-in, Booley tools, skills) only
    the default CLI, so another one may ask you to sign in and lack Booley's tools.

    Arguments after `booley chat` and its own options (`--cli`, `-C`,
    `--new-worktree`, `--here`) go to the
    CLI unchanged. To resume an earlier conversation, run
    `booley chat --resume <id>` (Claude) or `booley chat resume <id>` /
    `booley chat resume --last` (Codex); a conversation that ran in a worktree
    reopens there (see [Several agents at once](#several-agents-at-once)).

    If you see a login screen instead of a chat, run `booley auth --status` in a
    **host terminal** and follow its advice (usually `booley auth`). Then run
    **Dev Containers: Rebuild Container** and try again. See
    [Sign-in and billing](SETUP.md#sign-in-and-billing).

4. Type this first request into the chat:

    > Check whether Booley Interactive Mode is ready. List the available
    > Targets and explain what each one is for.

    The agent asks Booley for this and summarizes what it found.

5. If the agent listed a simulation Target, pick one and run it:

    > Run the `<target>` simulation.

    Replace `<target>` with a name from the agent's list.

That's a complete session. You don't need to read the rest of this guide
before you keep going. Just ask for what you want.

### What you can ask for

Simulation is only one of Booley's Flows. Your project has a Target for each
kind of run that Project Setup configured, so ask the agent which ones you have.

| Flow or Specialist | What it does | Try asking | Guide |
| --- | --- | --- | --- |
| `sim` | Builds and runs your tests in Verilator or Icarus, optionally with coverage | *"Debug the backpressure test failure on the `sim_heavy` target."* | [FLOW-SIM.md](FLOW-SIM.md), [TESTS.md](TESTS.md), [COVERAGE.md](COVERAGE.md) |
| `lint` | Lints with Verilator (structure and semantics) or Verible (style) | *"Run lint and explain every finding."* | [FLOW-LINT.md](FLOW-LINT.md) |
| `synth` | Quick ASIC area and timing estimate with Yosys and OpenROAD | *"Compare synth area between these two commits: ..."* | [FLOW-SYNTH.md](FLOW-SYNTH.md) |
| `fpga` | FPGA implementation with AMD Vivado installed on a Linux x86-64 host | *"Run FPGA implementation and tell me whether timing is met."* | [FLOW-FPGA.md](FLOW-FPGA.md) |
| `reviewer` | AI code review with one focus: bugs, protocol, spec, security, style, optimization, or testbench quality | *"Run a security review on the control unit module."* | [CLI-REFERENCE.md](CLI-REFERENCE.md#reviewer) |
| `mutation_tester` | Plants small bugs in your RTL and checks that your tests catch them | *"Check whether the FIFO tests catch injected bugs."* | [CLI-REFERENCE.md](CLI-REFERENCE.md#mutation_tester) |
| `coverage_analyst` | Explains a coverage report and suggests what to test next | *"Explain the coverage gaps from the last run."* | [COVERAGE.md](COVERAGE.md), [CLI-REFERENCE.md](CLI-REFERENCE.md#coverage_analyst) |

You don't need to know Flow names, flags, or where reports go. Give the agent
the goal and any limits, and it works out the details. Options and results
shared by all Flows are in [FLOW_REFERENCE.md](FLOW_REFERENCE.md); which EDA
tools each Flow supports is in [SUPPORTED-EDA-TOOLS.md](SUPPORTED-EDA-TOOLS.md).

**New module or test?** Ask the agent to add it, for example *"Add a test for
the new `fifo_ptr` module to the `sim_full` Target."* It updates the `.core`
file and `.booley/tests.toml` for you. The background is in
[TARGETS.md](TARGETS.md) and [TESTS.md](TESTS.md).

**Work that must be finished properly?** When a task has conditions that must
hold before it counts as done (all tests pass, a clean review, area under a
limit), type `/booley-goal` and describe the work, confirm the Goals, clear the
chat, and type `/booley-goal start`. Booley then won't let the agent declare it
done until each condition is met with evidence. See
[GOAL-MODE.md](GOAL-MODE.md#how-to-use-goal-mode).

### Booley skills

Skills are guided workflows you start by typing their name in the Developer
Agent chat:

| Skill | Use it when |
| --- | --- |
| `/booley-setup` | Setting up a Project (you started this on the host during setup) |
| `/booley-goal` | Work needs conditions that must be met, with evidence |
| `/booley-heal` | Doctor reports problems, or a Flow stopped working |
| `/booley-feedback` | Reporting a Booley bug, confusion, praise, or an idea |

`booley cheat --skills` prints the same list.

### Write a useful prompt

Brief the Developer Agent the way you would brief an engineer joining the task:

- what you want to learn or change,
- the Target or module, if you know it,
- any limits,
- what evidence you want to see.

For example:

> The `ready` signal sometimes remains low after reset on the `sim_full`
> Target. Reproduce the failure, inspect the waveform, and explain the likely
> cause. Show me the evidence and propose a fix.

If the agent heads the wrong way, don't start over. Tell it which assumption
was wrong or what evidence you want next.

### Long runs and results

Every Flow or Specialist run is a **Job**. The agent starts it and waits for the
result, which can take seconds for lint and much longer for big simulations or
synthesis. By default only one simulation or synthesis runs at a time; others
wait in a queue, so a run that seems stuck may just be waiting its turn. To
allow more, see [Jobs & concurrency](CONFIG.md#jobs--concurrency-jobs).

The agent tells you the verdict and the paths to the report and logs. Reports
from normal runs are kept under `.booley/flow-reports/`, never in your RTL. `booley resources`
shows peak memory and run time per Target. How to read a result:
[FLOW_REFERENCE.md](FLOW_REFERENCE.md#reading-results).

### The dashboard

The Dashboard opens in a VS Code terminal when you attach to the container (or
run `booley dashboard` in a container terminal); turn the automatic start off
with `[sandbox].dashboard=false`. It comes back by itself after a window
reload (this can take up to about half a minute). If you closed it, **Booley:
Open Dashboard** in the Command Palette reopens it or reveals the open one.
`booley dashboard` replaces a Dashboard that no terminal shows any more.
The Dashboard has three windows; press `←` or `→` to switch:

- **Sessions:** every Developer Agent session and what its agent last
  reported: working, waiting for a tool call, or waiting for your input.
  Sessions waiting for your input or a Goal change decision are grouped under
  **NEEDS YOU**, and the tab reads `Sessions N◆` while N sessions need you.
- **Jobs:** Running, Finished (the newest 50), or All Jobs, filterable by
  session.
  A `booley flow` or `booley specialist` run typed in a container terminal
  also appears here as a Standalone Job with no session. Its output is copied
  to `job.log`, shown as a link.
- **Targets:** the Targets of one worktree, with their core and the Flows that
  can drive them. Press `Enter` on a Target for its filesets, tests, Test
  Variants, and constraints. The **Worktree** item in the top bar picks the
  worktree: those with a connected session come first, the main checkout last.

Click a report, artifact, or log path to open it in the editor. Press `↑` on
the first row to reach the top bar with the tabs, filters, and Help; the footer
shows the keys you can use.

Sessions have short handles such as `S17`; Jobs carry their kind, as in
`J-SIM25` or `J-SYN34`. A session resumed after a long pause may get a new
handle, except a conversation resumed in its worktree, which keeps its handle.

### Several agents at once

Agents that share a checkout overwrite each other's edits, so `booley chat`
keeps them apart. The first session runs in the main checkout. While it is
running, the next `booley chat` creates a worktree `s<N>` on branch
`booley/s<N>`, named after the session handle (`S3` gets `s3`), and starts
there. It prints one line first:

```text
Booley session S3 works in worktree <path> on branch booley/s3 (<reason>).
```

A session that runs in the main checkout prints no such line. A worktree is
made only when the main checkout is taken, so a lone session behaves as before.

- `booley chat --new-worktree` always starts in a new worktree.
- `booley chat --here` always starts in the main checkout, for example to
  read code alongside another session.
- `[chat] worktree = "auto" | "always" | "never"` in `.booley/booley.toml` sets
  the default; see [Interactive sessions](CONFIG.md#interactive-sessions-chat).
- Run from inside an existing linked worktree, `booley chat` starts there and
  creates nothing.

When the agent of a worktree session exits and you are still at the terminal,
`booley chat` asks whether to keep or discard the worktree. The question lists
the work that is not in the main checkout yet: uncommitted changes in the
worktree and in its Paired Checkout, and the number of commits on each of its
branches that main lacks (Git-ignored files are not counted):

```text
Session S3 has ended. Worktree s3: <path>
Work not in main yet:
  - 2 uncommitted change(s) in the worktree
  - 3 commit(s) on booley/s3 not in main
Discard deletes the uncommitted changes and keeps the branches. Booley never
merges: merge what you need first.
Keep or discard worktree s3? [K/d]
```

Enter keeps it. Discard removes the worktree, the same as
`booley worktree rm s3`, and keeps its branches, so committed work stays
reachable. Booley never merges anything for you. If the removal is refused, for
example because the agent is still running somewhere or a Goal is active,
Booley shows why and asks a second time before it removes the worktree anyway.
A session in the main checkout gets no question.

Booley keeps the worktree without asking, and prints `Kept worktree s3: <path>`,
when nobody can answer: output goes to a pipe or a file, the terminal was
closed, or the agent was stopped by a signal. A kept worktree is normal; find it
with `booley worktree list` and remove it with `booley worktree rm`.

Every `booley chat` launch also runs `booley worktree gc` first and prints only
the worktrees it removed, so idle ones do not pile up. See
[Cleaning up idle worktrees](CLI-REFERENCE.md#worktrees).

#### Resuming a conversation

Claude Code and Codex keep a conversation with the directory it ran in, so
a conversation that ran in worktree `s3` cannot be resumed from the main
checkout. Booley therefore sends a resume to the worktree the conversation ran
in and keeps its handle (`S3` stays `S3`). It prints

```text
Booley session S3 resumes conversation <id> in worktree <path>.
```

- By id: `booley chat --resume <id>` (also `--resume=<id>` and `-r <id>`) for
  Claude, `booley chat resume <id>` for Codex. Put the id right after `resume`.
- The latest conversation: Claude's `-c`/`--continue` and Codex's
  `resume --last` continue the most recent conversation (by the agent CLI's
  own last-update time) among the main checkout and the worktrees whose
  session is not running; a main checkout in use is skipped too. Booley then resumes that conversation by its id. If
  Booley cannot tell which is newest, the agent CLI picks from the main
  checkout, as before. `resume --last --all` is left to Codex.
- A conversation that ran in the main checkout, or that Booley has no record
  of, resumes in the main checkout.
- If the worktree has since been removed, the conversation resumes in the main
  checkout after a `NOTE`.
- Resuming refuses a checkout that is in use, except the main checkout under
  `[chat] worktree = "never"`, which is shared. A conversation whose worktree
  has a running session (a Codex started by hand counts) is refused with exit 2
  (end that session first). A conversation that belongs in the main checkout
  while it is in use is refused too; run `booley chat --here ...` to resume it
  beside that session.
- `--here` and `--new-worktree` keep their meaning, and a launch from inside a
  linked worktree stays there: none of them is redirected by a resume. The
  `[chat] worktree` setting decides where new sessions start, not where a
  resume goes.
- A conversation you started by hand inside a worktree (not through
  `booley chat`) reopens in that worktree, but may get a new handle once its
  earlier session has ended.

Put both options before any arguments meant for the agent CLI. Claude Code's
own `--worktree` still goes to the agent.

The check is best effort for agents that did not start through Booley. A plain
`claude` tab or a VS Code panel does not hold the main checkout, so Booley only
notices it once it has made a Booley tool call. Start such sessions in a
worktree yourself (ask the agent: *"Create a worktree named `fifo-fix` and work
there."*) or use `booley chat`. Read-only sessions, such as one that only
explains code or runs reviews, can share the main checkout. See
[TROUBLESHOOTING.md](TROUBLESHOOTING.md#two-developer-agents-keep-clobbering-each-others-edits).

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

<details markdown>
<summary>In stealth mode</summary>

A Git hook removes AI and Booley names from commit messages and rejects
`Co-Authored-By` lines. If a commit is rejected, the agent just retries without
that line. See [STEALTH.md](STEALTH.md#commit-messages).

</details>

Per-session logs of Flow and Specialist calls through Booley’s tool server
are saved in `.booley/.interactive_logs/`. Chat transcripts live in
the Developer Agent CLI’s own session storage. If the
Developer Agent can't reach Booley (`booley` is missing from its `/mcp` list), see
[TROUBLESHOOTING.md](TROUBLESHOOTING.md#booley-is-missing-from-mcp-in-claude-code-or-codex).

### Editing Project instructions

The Developer Agent's project instructions live in one file:
`.booley/AGENTS.md` (or `AGENTS.md` in your Booley Directory, if you renamed
it). The `AGENTS.md` and `CLAUDE.md` in your project root are just links to that
file, so editing any of them changes the same text. Booley creates the links
and keeps them out of Git.

To save your edits, commit the real file (`git add .booley/AGENTS.md`).

<details markdown>
<summary>In stealth mode</summary>

The Booley Directory is its own Git repository, so commit `AGENTS.md` there
instead: `git -C .booley commit`. See [STEALTH.md](STEALTH.md#turning-it-on).

</details>

### Viewing waveforms

When a simulation with tracing fails, the Developer Agent reads the waveform on its own
with `bwave`. When *you* want to see it, ask: *"show me the FIFO handshake
around the failure"*. The agent opens it in a VaporView tab in VS Code, showing
just the signals and time range that matter. VS Code installs the extension
automatically when you attach to the container. If it is missing, see
[TROUBLESHOOTING.md](TROUBLESHOOTING.md#bwave-gui-fails-on-a-scoped-view).

## When Booley itself misbehaves

Maybe a Flow failed with no useful message, a doc describes a setting that
doesn't exist, or a message looks like a crash when nothing crashed. Type
**`/booley-feedback`** in the Developer Agent chat while the problem is still on screen.
It records how to reproduce it, checks Booley's own code before blaming it,
and saves it to a local report. Booley never sends it anywhere.

To send it, ask the skill for a shareable copy (it strips your project's
names), then either
[open a Booley issue on GitHub](https://github.com/boldaxolotl/Booley/issues/new),
which is public, or email it privately to the maintainer at
[boldaxolotl@proton.me](mailto:boldaxolotl@proton.me).

It doesn't have to be a bug. Confusing behavior, praise, complaints, wishes, or
"this wasn't worth the setup cost" are all worth one sentence to
`/booley-feedback`. See [CONFIG.md](CONFIG.md#feedback-feedback).
