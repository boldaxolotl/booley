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
| Sandbox | `chat`, `dashboard`, `flow`, `measure`, `specialist`, `worktree` |
| Either | `cheat`, `coverage`, `doctor`, `help`, `init`, `resources`, `targets` |
| Host-only | `auth`, `bootstrap`, `eda`, `projects` |

`booley flow` and `booley specialist` run and list only in the Sandbox, but
`booley flow <name> --help` and `booley specialist <name> --help` also work on
the host inside an initialized Project. `booley targets` lists Targets
anywhere; showing one Target in full detail needs the Sandbox.
`booley cheat --metrics` lists a Project's Targets, so it needs an initialized Project.

`booley help` and `booley --help` print the same command list.
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
booley auth                       # save a long-lived Claude login for unattended runs
booley auth --cli codex           # the same for Codex: paste an API key
booley auth --status              # show which login each agent would use
```

Host Bootstrap is described in [INSTALL.md](INSTALL.md#host-bootstrap);
`init` and its options in [SETUP.md](SETUP.md); logins and billing in
[Sign-in and billing](SETUP.md#sign-in-and-billing).

`booley init` also runs inside a hand-written Sandbox such as a Codespace, where it does Project
Initialization only; a Booley-issued Sandbox refuses it. See
[Project setup in a hand-written spec](SANDBOX.md#project-setup-in-a-hand-written-spec-booley-init).

### Work in the Sandbox · container

```bash
booley                            # open the Developer Agent (same as booley chat)
booley chat --cli codex           # open another Agent CLI your Project allows
booley chat --name uart-fix       # label the session: the tab and Dashboard show S3 · uart-fix
booley chat --new-worktree        # start in a new worktree s<N> even if the main checkout is free
booley chat --here                # start in the main checkout even if it is in use (in a linked worktree: stay there)
booley chat --resume <id>         # Claude: arguments after chat and its options go to the agent CLI
booley chat resume --last         # Codex: the same, with its own subcommands
booley chat -- --help             # the agent CLI's help (Booley's is booley chat --help)
booley dashboard                  # sessions and Jobs; see Usage
booley measure                    # measure HEAD against its committed metrics.toml and store the run
booley measure <sha> --base <sha> # measure another commit; compare with another Base
booley measure --all --json       # evaluate every table after a failure; print the report as JSON
booley measure --again            # measure again even when the commit is already Measured
booley measure --target <name>    # measure one Target's tables only
booley measure --validate         # check .booley/metrics.toml; measures nothing
```

- Booley's `chat` options are read only before the agent's own arguments.
  Claude's own `--name` is `booley chat -- --name <name>`.
- `--new-worktree` and `--here` are mutually exclusive.
- `--name` takes 1-32 ASCII letters, digits, spaces, `.`, `_` or `-`, starting
  with a letter or digit; an invalid name exits with status 2.
- `--new-worktree` is refused when you run it from inside a linked worktree.
- If Booley cannot reserve a session handle for a worktree launch, it exits
  with status 2 and suggests `--here`.

Sessions and parallel work are explained in
[Several agents at once](USAGE.md#several-agents-at-once), and measuring in
[Metrics](METRICS.md). The Dashboard opens by itself when you attach to the
container; see [The dashboard](USAGE.md#the-dashboard).

### Worktrees

```bash
booley worktree new <name>               # a linked worktree for parallel work
booley worktree list                     # each worktree's branch, changes, and use
booley worktree rm <name> [--force]      # remove a worktree, keeping its evidence and branches
booley worktree gc [--older-than AGE]    # remove idle worktrees (default: a week), where safe
```

Run them in a container terminal. What each one creates, checks, and keeps is
in [Worktrees](WORKTREES.md).

### Run a Flow or a Specialist · container

```bash
booley flow                                  # list the Flows
booley flow sim --target sim_soc --test reset
booley flow sim --help                       # every option of one Flow
booley specialist                            # list the Specialists
booley specialist reviewer --help            # a Specialist's arguments
```

Targets, options, exit codes, and results are in
[Running Flows](FLOW_REFERENCE.md). Specialists are in
[Specialists](SPECIALISTS.md), [Reviewer](REVIEWER.md), and
[Analyzing a Campaign](COVERAGE.md#analyzing-a-campaign). `booley cheat --flows`
and `booley cheat --specialists` print both lists with their main options.

### Check and look things up · either

```bash
booley cheat                      # list the cheatsheet sections
booley cheat --all                # the whole cheatsheet
booley cheat --goals              # Goal Mode, the Goal catalog, and thresholds
booley cheat --metrics            # the measuring rules, then each Target's metrics as metrics.toml text (needs a Project)
booley coverage uncovered <coverage.json>   # uncovered points of a coverage run
booley coverage uncovered <coverage.json> --metric branch --source rtl/fifo.sv
booley doctor                     # check setup and environment health
booley doctor --concise           # show only problems
booley doctor --deep              # also run short real simulations, lints, and syntheses
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
| `booley session` | host | Start, enter, or stop the Sandbox without VS Code; see [SANDBOX.md](SANDBOX.md#entering-the-sandbox-without-vs-code) |
| `booley goal` | container | Show or abandon a Goal Mode; see [GOAL-MODE.md](GOAL-MODE.md#watching-recovery-and-parallel-sessions) |
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
