# CLI reference

Every `booley` command, where it runs, and the page that explains it. Day to
day you rarely type these: ask the Developer Agent and it picks the command.
Use this page when you prepare a machine or a Project, check health, manage
worktrees, or run a Flow yourself to reproduce a problem. For the everyday
workflow, read [Usage](USAGE.md).

## Commands

Each command runs either in a **host terminal** (your own computer) or in a
**container terminal** (inside the Sandbox, the Docker container that holds
the EDA tools); see [where you type](USAGE.md#read-this-first). A command run
in the wrong place stops and prints how to run it in the right one.

| Location | Public commands, in order |
|---|---|
| Sandbox | `dashboard`, `flow`, `measure`, `specialist`, `worktree` |
| Either | `cheat`, `coverage`, `doctor`, `help`, `init`, `resources`, `targets` |
| Host-only | `auth`, `bootstrap`, `eda`, `projects` |

`booley flow` and `booley specialist` run and list only in the Sandbox, but
`booley flow <name> --help` and `booley specialist <name> --help` also work on
the host inside an initialized Project. `booley targets` lists Targets
anywhere; showing one Target in full detail needs the Sandbox.
`booley cheat --metrics` lists a Project's Targets, so it needs an initialized Project.

`booley help`, `booley --help` and bare `booley` print the same command list;
on the host, bare `booley` adds a getting-started hint.
`booley --version` prints the version, plus its commit and that commit's UTC
date when known. [A few more commands](#commands-for-skills) serve the agent's
skills and stay out of that list.

### Set up the machine and the Project · host

```bash
booley bootstrap                  # prepare this machine once for every Project
booley bootstrap --update         # rerun after every Booley upgrade
booley bootstrap --check-only     # report pending work; change nothing
booley bootstrap --egress unproxied  # record the Sandbox egress mode, then bootstrap
booley init                       # set up the Project in the current directory
booley auth                       # save a long-lived Claude login so long runs stay signed in
booley auth --cli codex           # the same for Codex: paste an API key
booley auth --status              # show which login each agent would use
```

Host Bootstrap is described in [INSTALL.md](INSTALL.md#host-bootstrap);
`init` and its options in [SETUP.md](SETUP.md); logins and billing in
[Sign-in and billing](SETUP.md#sign-in-and-billing).

### Work in the Sandbox · container

```bash
booley dashboard                  # serve each worktree's Dashboard page and JSON (--port PORT, default 8090)
booley measure                    # measure HEAD against its committed metrics.toml and store the run
booley measure <sha> --base <sha> # measure another commit; compare with another Base
booley measure --all --json       # evaluate every table after a failure; print the report as JSON
booley measure --again            # measure again even when the commit is already Measured
booley measure --target <name>    # measure one Target's tables only
booley measure --validate         # check .booley/metrics.toml; measures nothing
```

- `booley dashboard` serves a page and JSON on `127.0.0.1` until you stop it,
  on port 8090 unless `--port` names another. A Sandbox that starts T3 Code
  already runs this server on 8090, so to start another, pass `--port`.
  Open `http://127.0.0.1:<port>/?worktree=<absolute path>` for a worktree's
  page, which refreshes every 3 s. Without `?worktree=`, `/` lists the main
  checkout and T3 Code's worktrees to pick from, or goes to the main checkout
  when it is the only one.
  `GET /api/snapshot?worktree=<absolute path>` answers for the main checkout or
  a linked worktree with that worktree's Jobs only: the unfinished Jobs, the
  newest 50 finished Jobs with a `report_url` for each report, and the
  Sandbox's CPU, memory, disk and Job admission figures. A Job started from an
  agent's thread has an `owner`. For Claude it is the thread's T3 Code title,
  else the agent and a short thread id. For Codex it is the agent and the time
  the Job was submitted. A Standalone Job's `owner` is `you`. A missing,
  relative or unknown `worktree` gets a JSON error (status 400 or 404) and no
  Jobs.

Sessions and parallel work are explained in
[Several agents at once](USAGE.md#several-agents-at-once), and measuring in
[Metric-Driven Development](METRIC-DRIVEN-DEVELOPMENT.md).

### Worktrees

```bash
booley worktree prepare [PATH]           # give a worktree made with Git or T3 Code its own Booley Directory
```

Run it in a container terminal. What it creates, checks, and keeps is in
[Worktrees](WORKTREES.md).

### Run a Flow or a Specialist · container

```bash
booley flow                                  # list the Flows
booley flow sim --target sim_soc --test reset
booley flow sim --help                       # every option of one Flow
booley specialist                            # list the Specialists
booley specialist reviewer --help            # a Specialist's arguments
booley specialist reviewer --recheck <evidence.json>   # re-check an earlier review's findings
booley specialist reviewer --recheck <evidence.json> --waive <finding-id> "<reason>"   # propose a waiver
```

Targets, options, exit codes, and results are in
[Running Flows](FLOW_REFERENCE.md). Specialists are in
[Specialists](SPECIALISTS.md), [Reviewer](REVIEWER.md), and
[Analyzing a Coverage Record](COVERAGE.md#analyzing-a-coverage-record). `booley cheat --flows`
and `booley cheat --specialists` print both lists with their main options.

### Check and look things up · either

```bash
booley cheat                      # list the cheatsheet sections
booley cheat --all                # the whole cheatsheet
booley cheat --metrics            # the measuring rules, then each Target's metrics as metrics.toml text (needs a Project)
booley coverage uncovered <coverage.json>   # uncovered points of a coverage run
booley coverage uncovered <coverage.json> --metric branch --source rtl/fifo.sv
booley doctor                     # check setup and environment health
booley doctor --concise           # show only problems
booley doctor --deep              # also run short real simulations, lints, syntheses, and FPGA implementations
booley doctor --deep --skip-agent-checks   # skip the agent login and provider checks
booley targets                    # list Targets
booley targets --for sim          # only Targets a Flow can use
booley targets <name>             # one Target in detail (Sandbox)
booley resources                  # peak memory and run time per Target and Job phase, and per Specialist
booley resources --json           # the same, machine-readable
```

Doctor also runs by itself at certain points; see [Doctor](DOCTOR.md).
Targets are explained in [TARGETS.md](TARGETS.md), resource history in
[Job resource history](CONFIG.md#job-resource-history), and the coverage
listing in [Finding uncovered points](COVERAGE.md#finding-uncovered-points).

### Projects on this machine · host

```bash
booley projects                          # list known Projects and their EDA Grants
booley projects discover <dir>...        # find existing Projects under these directories
booley projects forget <project>         # forget a Project and list what it still holds in Docker
booley projects forget --purge <project> # also remove its unused images and state volumes
booley projects prune-keepers            # preview images left by earlier forgets
booley projects prune-keepers --confirm <digest>   # remove exactly that preview
booley projects prune-stamps [<project>...]   # list, or remove, stamps of deleted Projects
```

What each command lists, keeps, and removes is in
[Projects on this machine](SETUP.md#projects-on-this-machine--host).

### Commercial EDA · host

```bash
booley eda installation register --kind vivado --source <install-dir> <name>
booley eda installation list
booley eda license list
booley eda grant add --kind vivado --installation <name> <project>
booley eda grant revoke --kind vivado <project>
```

A **Grant** is your permission for one Project to use one registered
installation or license profile. The setup is in
[Commercial EDA provisioning](SANDBOX.md#commercial-eda-provisioning);
`booley eda <group> --help` lists every subcommand.

### Commands for skills

These commands exist mainly for the agent's skills and are left out of
`booley help`. You may still run them yourself:

| Command | Where | What it's for |
|---|---|---|
| `booley session` | host | Start, enter, or stop the Sandbox; see [SANDBOX.md](SANDBOX.md#entering-the-sandbox) |
| `booley upgrade` | either | Review a Booley upgrade; see [Upgrade review](DOCTOR.md#upgrade-review) |
| `booley feedback` | either | Record Booley bugs and impressions; use `/booley-feedback` instead |
| `booley cleanup` | either | Remove a skill's scratch files |

## Selecting a Project

By default a command works on the checkout that contains the current
directory. To pick another one, pass `-C PATH` or `--project PATH`: any
directory inside a Project's checkout or worktree, relative to where you run
the command.

```bash
booley -C ../other targets
booley flow lint --target lint_style --project ../other
booley session -C ../other status
```

Give it once, before or after the command name. Every command that works on a
Project accepts it; `bootstrap`, `projects`, and `eda` don't, because they work
on the whole machine. Inside the Sandbox, `booley -C <path>` picks the Project
the Developer Agent opens in. The directory must exist; only `booley init`
accepts one that Booley hasn't set up yet.

If a command misbehaves or this page is wrong, see
[When Booley itself misbehaves](USAGE.md#when-booley-itself-misbehaves).
