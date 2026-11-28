# Usage

This guide shows how to use Booley day to day. You don't need any experience
with AI coding agents to follow it.

**Contents**

1. [Read this first](#read-this-first)
2. [First, verify your setup](#first-verify-your-setup)
3. [Developer Agent sessions](#developer-agent-sessions):
    [first session](#open-your-first-developer-agent-session),
    [what to ask for](#what-you-can-ask-for), [skills](#booley-skills),
    [good prompts](#write-a-useful-prompt),
    [long runs and results](#long-runs-and-results),
    [dashboard](#the-dashboard), [parallel work](#several-agents-at-once),
    [reviewing changes](#what-the-developer-agent-is-allowed-to-do),
    [editing instructions](#editing-project-instructions),
    [waveforms](#viewing-waveforms)
4. [Reporting problems and feedback](#when-booley-itself-misbehaves)

Every command is listed in [CLI-REFERENCE.md](CLI-REFERENCE.md).

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

- **Flows** (`sim`, `lint`, `synth`, `fpga`, `mutation`) run EDA tools, or
  your tests, in a fixed, predictable way. No AI is involved.
- **Specialists** (`reviewer`, `coverage_analyst`) are small AI capabilities
  that each do one focused job. See [Specialists](SPECIALISTS.md).

Both run inside the **Sandbox**, a Docker container that holds the EDA tools
and keeps the Developer Agent away from the rest of your computer.

**Where you type.** This guide uses three places:

| Place | What goes there | Example |
| --- | --- | --- |
| **Host terminal** | Commands on your own computer, outside Docker | `booley doctor`, `booley auth` |
| **Container terminal** | Commands inside the Sandbox: a T3 Code thread's terminal, or `booley session enter` on the host | `booley measure`, `git diff` |
| **Developer Agent chat** | Plain-language requests and skills that start with `/` | *"Run lint and explain every finding"* |

Command blocks go in the terminal named in the text around them. Sentences in
*italics* are prompts for the Developer Agent chat. Skills such as
`/booley-heal` also go in the Developer Agent chat, not in a terminal.

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
  available sections; print the ones you need, for example `booley cheat --metrics`
  or `booley cheat --commands --project-files`. Use `booley cheat --all` for the
  whole cheatsheet; `booley cheat --list` also shows the section list.
- `booley doctor --deep` goes further and runs short real simulations, lints,
  and syntheses. Run it in a container terminal.
- Booley also runs plain Doctor by itself: when the container starts, about once
  a week, and after configuration changes. It never blocks work; new problems
  show up in the next Flow result. When it says a deep check is **due** (after a
  Booley or Sandbox Image update), run `booley doctor --deep`. See
  [DOCTOR.md](DOCTOR.md#automatic-checks).

## Developer Agent sessions

### Open your first Developer Agent session

1. In a **host terminal** at the Project Root, run `booley session up`. It
   starts the Sandbox and prints a pairing link.

2. Open the link in a browser on this machine. T3 Code, the web UI the
   Sandbox serves, opens with your Project. The other `booley session`
   commands are in [SANDBOX.md](SANDBOX.md).

3. In T3 Code, start a thread on the main checkout (your Project folder) and
   choose Claude or Codex.

    Every Agent CLI listed in `[agent] clis` in `.booley/booley.toml` gets
    Booley's tools and skills and the sign-in its `[agent.auth]` policy allows;
    an unlisted one gets none. See
    [Agent CLI logins in the Sandbox](SANDBOX.md#agent-cli-logins-in-the-sandbox).

    If the agent can't sign in, run `booley auth --status` in a
    **host terminal** and follow its advice (usually `booley auth`). Then run
    `booley session down` and `booley session up` there and try again. See
    [Sign-in and billing](SETUP.md#sign-in-and-billing).

4. Type this first request into the chat:

    > Check whether Booley is ready in this session. List the available
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
| `sim` | Builds and runs your tests and reports which pass, optionally with coverage on Verilator Targets | *"Debug the backpressure test failure on the `sim_heavy` target."* | [FLOW-SIM.md](FLOW-SIM.md), [TESTS.md](TESTS.md), [COVERAGE.md](COVERAGE.md) |
| `lint` | Checks RTL for mistakes without simulating it and lists the findings | *"Run lint and explain every finding."* | [FLOW-LINT.md](FLOW-LINT.md) |
| `synth` | Quick ASIC area and timing estimate while you iterate on RTL | *"Compare synth area between these two commits: ..."* | [FLOW-SYNTH.md](FLOW-SYNTH.md) |
| `fpga` | Implements the design for an FPGA and reports resources and timing; needs AMD Vivado installed on a Linux x86-64 host | *"Run FPGA implementation and tell me whether timing is met."* | [FLOW-FPGA.md](FLOW-FPGA.md) |
| `mutation` | Plants small bugs in your RTL and checks that your tests catch them | *"Check whether the FIFO tests catch injected bugs."* | [FLOW-MUTATION.md](FLOW-MUTATION.md) |
| `reviewer` | AI code review with one focus: bugs, protocol, spec, security, style, optimization, or testbench quality | *"Run a security review on the control unit module."* | [REVIEWER.md](REVIEWER.md) |
| `coverage_analyst` | Explains a coverage report and suggests what to test next | *"Explain the coverage gaps from the last run."* | [COVERAGE.md](COVERAGE.md#analyzing-a-coverage-record) |

You don't need to know Flow names, flags, or where reports go. Give the agent
the goal and any limits, and it works out the details. Options and results
shared by all Flows are in [FLOW_REFERENCE.md](FLOW_REFERENCE.md); which EDA
tools each Flow supports is in [SUPPORTED-EDA-TOOLS.md](SUPPORTED-EDA-TOOLS.md).

**New module or test?** Ask the agent to add it, for example *"Add a test for
the new `fifo_ptr` module to the `sim_full` Target."* It updates the `.core`
file and `.booley/tests.toml` for you. The background is in
[TARGETS.md](TARGETS.md) and [TESTS.md](TESTS.md).

### Booley skills

Skills are guided workflows you start by typing their name in the Developer
Agent chat:

| Skill | Use it when |
| --- | --- |
| `/booley-setup` | Setting up a Project (you started this on the host during setup) |
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
synthesis. A simulation, synthesis or other EDA run starts while its estimated
memory fits the Memory Budget; others wait in a queue, so a run that seems
stuck may just be waiting its turn. To size the budget, see
[Jobs & concurrency](CONFIG.md#jobs--concurrency-jobs).

The agent tells you the verdict and the paths to the report and logs. Reports
from normal runs are kept under `.booley/flow-reports/`, never in your RTL. `booley resources`
shows peak memory and run time per Target. How to read a result:
[FLOW_REFERENCE.md](FLOW_REFERENCE.md#reading-results).

### The dashboard

The Dashboard is a web page of one worktree's Jobs, the long runs of Flows and
Specialists. Open it to see what runs there, what waits and how each run ended.

1. Click **Booley Dashboard** in T3 Code's top bar. The page opens in the
   preview panel; the Sandbox already serves it.
2. Pick the worktree: the main checkout or one of your T3 Code threads'
   worktrees. Each has its own page. With no T3 Code worktree, the page opens
   the main checkout.

The page shows the running and queued Jobs, and the finished ones with a link
to each report. A `booley flow` or `booley specialist` run you type in a
container terminal is a Standalone Job, owned by `you`.

Each Job has a short handle that carries its kind, as in `J-SIM25`, `J-SYN34`,
`J-MEAS3` for a `booley measure` run or `J-MUT7` for a Mutation Flow run.

### Several agents at once

Agents that share a checkout overwrite each other's edits. Start each parallel
task as its own T3 Code thread on a new worktree: T3 Code creates the
worktree, Booley prepares it, and the thread's Booley tool calls run in it.
Read-only threads, such as one that only explains code or runs reviews, can
share the main checkout. What a worktree gets, and how to prepare one you made
yourself, is in [Worktrees](WORKTREES.md).

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

A Git hook rejects commit messages that name AI tools or Booley, and
`Co-Authored-By` lines. If a commit is rejected, the agent rewords the message
and commits again. See [STEALTH.md](STEALTH.md#commit-messages).

</details>

Logs of the Flow and Specialist calls made through Booley's tool server are
saved under `job_logs/` in the Main Booley Directory (`.booley/job_logs/` in
the main checkout), for threads on a worktree too. Chat transcripts live in
the Developer Agent CLI's own session storage.

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

When a simulation with tracing fails, the Developer Agent reads the waveform
on its own with `bwave`. Booley has no waveform viewer for now, and
`bwave gui` fails with a message. When *you* want to know what happened, ask:
*"what does the FIFO handshake do around the failure"*. The agent answers from
the waveform, naming the signals, times and values. You can open the FST file
in a viewer of your own, such as GTKWave or Surfer.

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
