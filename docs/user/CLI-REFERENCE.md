# CLI reference

Every `booley` command, plus the Booley Flows and Specialists the Developer
Agent runs for you. Day to day you rarely type these: ask the Developer Agent
and it picks the command. Use this page when you prepare a machine or a
Project, check health, manage worktrees, or run a Flow yourself to reproduce a
problem. For the everyday workflow, read [Usage](USAGE.md).

## Commands

Each command runs either in a **host terminal** (your own computer) or in a
**container terminal** (inside the Sandbox, the Docker container that holds
the EDA tools); see [where you type](USAGE.md#read-this-first). A command run
in the wrong place stops and prints how to run it in the right one.

| Location | Public commands, in order |
|---|---|
| Sandbox | `chat`, `dashboard`, `flow`, `specialist`, `worktree` |
| Either | `cheat`, `doctor`, `help`, `resources`, `targets` |
| Host-only | `auth`, `bootstrap`, `eda`, `init`, `projects` |

`booley flow` and `booley specialist` run and list only in the Sandbox, but
`booley flow <name> --help` and `booley specialist <name> --help` also work on
the host inside an initialized Project. `booley targets` lists Targets
anywhere; showing one Target in full detail needs the Sandbox.

`booley help` and `booley --help` print the same command list.
`booley --version` prints the version, plus its commit and that commit's UTC
date when known. [A few more commands](#commands-for-skills) serve the agent's
skills and stay out of that list.

### Set up the machine and the Project · host

```bash
booley bootstrap                  # prepare this machine once for every Project
booley bootstrap --update         # rerun after every Booley upgrade
booley bootstrap --check-only     # report pending work; change nothing
booley init                       # set up the Project in the current directory
booley init --scaffold my_ip      # write a new-IP starter (run by /booley-setup new; asks nothing)
booley auth                       # save a long-lived Claude login for unattended runs
booley auth --cli codex           # the same for Codex: paste an API key
booley auth --status              # show which login each agent would use
```

Host Bootstrap is described in [INSTALL.md](INSTALL.md#host-bootstrap);
`init` and its options in [SETUP.md](SETUP.md); logins and billing in
[Sign-in and billing](SETUP.md#sign-in-and-billing).

### Work in the Sandbox · container

```bash
booley                            # open the Developer Agent (same as booley chat)
booley chat --cli codex           # open another Agent CLI your Project allows
booley chat --resume <id>         # Claude: arguments after chat and its options go to the agent CLI
booley chat resume --last         # Codex: the same, with its own subcommands
booley chat -- --help             # the agent CLI's help (Booley's is booley chat --help)
booley dashboard                  # sessions and Jobs; see Usage
booley worktree new <name>        # a linked worktree for parallel work; see below
booley worktree list              # each worktree's branch, changes, and use
```

The Dashboard opens by itself when you attach to the container; see
[The dashboard](USAGE.md#the-dashboard).

### Check and look things up · either

```bash
booley cheat                      # list the cheatsheet sections
booley cheat --all                # the whole cheatsheet
booley cheat --goals              # Goal Mode, the Goal catalog, and thresholds
booley doctor                     # check setup and environment health
booley doctor --concise           # show only problems
booley doctor --deep              # also run short real simulations, lints, and syntheses
booley doctor --deep --skip-agent-checks   # skip the agent login and provider checks
booley targets                    # list Targets
booley targets --for sim          # only Targets a Flow can use
booley targets <name>             # one Target in detail (Sandbox)
booley resources                  # peak memory and run time per Target and Job phase
booley resources --json           # the same, machine-readable
```

Doctor also runs by itself at certain points; see [Doctor](DOCTOR.md).
Targets are explained in [TARGETS.md](TARGETS.md), and resource history in
[Job resource history](CONFIG.md#job-resource-history).

### Projects on this machine · host

```bash
booley projects                          # list known Projects and their EDA Grants
booley projects discover <dir>...        # find existing Projects under these directories
booley projects forget <project>         # forget a Project and list what it still holds in Docker
booley projects forget --purge <project> # also remove its unused images and state volumes
booley projects prune-keepers            # preview images left by earlier forgets
booley projects prune-keepers --confirm <digest>   # remove exactly that preview
```

Each successful `booley init` remembers its Project Root. `booley projects`
marks each one `present`, `missing`, or `uninitialized`.

`discover` scans only the directories you name and doesn't follow symlinks.
It stops at each Project it finds, so it doesn't crawl that Project's RTL and
build trees; to import a Project nested inside another, name its path directly.

`forget` refuses while the Project still has a live Grant (see below). It
lists what the Project still holds in Docker, with sizes: images, agent state
volumes (a copy of the agent's login and history), containers, and networks.
It removes none of them unless you add `--purge`, which removes the unused
ones, even for a Project already forgotten or deleted. To use a forgotten
Project again, run `booley init` in it.

### Commercial EDA · host

Commercial tools installed on your computer, currently Vivado 2025.2 on Linux
x86-64, reach a Sandbox only through a **Grant**: your permission for one exact
Project path to use one registered installation or license profile.

```bash
booley eda installation register --kind vivado --source <install-dir> <name>
booley eda installation list
booley eda license list
booley eda grant add --kind vivado --installation <name> <project>
booley eda grant revoke --kind vivado <project>
```

The setup is in [Commercial EDA provisioning](SANDBOX.md#commercial-eda-provisioning);
`booley eda <group> --help` lists every subcommand.

### Worktrees

A worktree is a second checkout of your Project where a separate Developer
Agent can work without touching your files, for example one per Goal Mode
session. Usually you ask the agent to create one. In a container terminal,
from the main checkout:

```bash
booley worktree new <name>
```

This creates `.booley/worktrees/<name>`, prints its path, and starts it at the
current commit on a new branch `booley/<name>`. An existing `booley/<name>`
branch or destination is refused and left untouched, and so is a name Git
cannot use in a branch name (such as `x.lock` or `a..b`). A name of the form
`<branch>--<description>` checks out the existing branch `<branch>` instead and
creates no branch. Git submodules are filled in from the main
checkout, without network access. Then the Project's `post-setup` hook
(`.booley/hooks/post-setup.sh`, `.py`, or no suffix), if it has one, runs in the
new worktree. Its output is shown and saved to
`.booley/worktrees/.logs/<name>.log` in the main Booley Directory. If the
submodule step or the hook fails, Booley removes the worktree and its
`booley/<name>` and `booley-worktree/<name>` branches and exits nonzero. A
branch that already gained commits is kept.

The worktree gets its own Booley Directory (the `.booley/` folder):

- **Paired Checkout**, when your Booley Directory is its own Git repository:
  a checkout of its last commit on branch `booley-worktree/<name>`.
  Uncommitted and untracked files are left out; one warning names them (the
  first 10, then a count). Commit them first if the worktree needs them.
  Booley refuses a repository with no commits or an existing branch of that
  name. Ignored files stay behind. Booley refuses to create a worktree from
  inside a Paired Checkout.
- **Snapshot**, otherwise: a copy of the Booley Directory's configuration
  (settings, `cores/`, hooks, Goal and Ticket history). Build results, reports, logs,
  and evidence stay in the main Booley Directory. A path Booley does not
  classify is not copied, and the command names it.

**Listing worktrees.** `booley worktree list` shows one row per worktree
under `.booley/worktrees/`, from the main checkout or from any worktree:

| Column | Meaning |
|---|---|
| `NAME` | The worktree name |
| `BRANCH` | Its branch, or `detached` on a detached HEAD |
| `BOOLEY DIR` | `Paired Checkout` or `Snapshot` |
| `UNCOMMITTED` | Changed and untracked entries (an untracked folder counts once), not counting untracked files in the worktree's own `.booley/`. A Paired Checkout reads `N, paired M`, `M` being the same count in its inner checkout |
| `UNMERGED` | Commits not yet in the main checkout's branch. Commits whose change is already there count as merged, including a branch merged by squash. A Paired Checkout reads `N, paired M` |
| `SESSION` | Handles of live sessions in the worktree |
| `GOAL` | Its active Goal, if any |
| `LAST USED` | When the worktree was created or a Booley tool call last ran there; `never` for a worktree that predates the record |

A worktree whose directory is gone is not listed; a warning names it.

**Removing a worktree** deletes everything in it, ignored files included, and
Booley has no command for it. First save what you need:

1. Put the commits you want on a branch you keep. Commits made in the worktree
   are on `booley/<name>`; an older worktree, or one named
   `<branch>--<description>`, may be on a detached HEAD, so its commits may be
   on no branch at all. For a Paired Checkout, the same goes for its
   `booley-worktree/<name>` branch.
2. Copy needed untracked and ignored files into a new folder outside the
   worktree, overwriting nothing. Look through the whole worktree, including
   its `.booley/`: `reviewer-evidence/`, `handoffs/`, the Findings Log
   (`findings.jsonl`), reports, and logs. A finding can attach files that live
   elsewhere; copy those as well. Keep this archive separate from the
   Project's own Findings Log; don't merge the two.

Then, in the same Sandbox and from outside the worktree:

1. Clean dirty and untracked files, including a Snapshot's copied files when
   Git lists them as untracked.
2. Paired Checkout only: remove the inner checkout with
   `git -C <absolute-booley-directory> worktree remove <absolute-worktree>/.booley`.
3. Remove the worktree with
   `git -C <absolute-project-root> worktree remove <absolute-worktree>`.
4. Optional: to reuse the name, delete its branch with
   `git -C <absolute-project-root> branch -D booley/<name>`, and for a Paired
   Checkout also
   `git -C <absolute-booley-directory> branch -D booley-worktree/<name>`.

Git refuses to remove a worktree that holds populated submodules; once your
files are saved, add `--force` to each `git worktree remove`. Check for locks
before you unlock a worktree or force a removal.

Use Sandbox paths in these commands, not host paths: the host may see the
worktree somewhere else. If `booley worktree new` fails and leaves a checkout
behind, it prints these steps with the real paths.

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

## Booley Flows & Specialists

Ask the Developer Agent (*"run the reset test on `sim_lite`"*, *"how much area
did that cost?"*) and it picks the Flow or Specialist, the Target, and the
options. The **Sets** column shows which
[Goal evidence](GOAL-MODE.md#working-with-evidence) each one can produce. The
EDA tool that runs underneath depends on the Target; see
[SUPPORTED-EDA-TOOLS.md](SUPPORTED-EDA-TOOLS.md). `booley cheat --flows` and
`booley cheat --specialists` print the same lists.

<!-- BEGIN GENERATED: flows -->
**Booley Flows**

Deterministic end-to-end orchestration; no LLM:

| Booley Flow | Purpose | Sets |
|--------|---------|------|
| `sim` | Run RTL simulation for one or more Targets | `ELAB:{target}`, `SIM:{target}`, `CYCLE_COUNT:{target}`, `COVERAGE:{target}` |
| `lint` | Run lint for one or more Targets | `LINT:{target}` |
| `synth` | Run ASIC synthesis for one or more Targets with optional baseline comparison | `SYNTH:{target}` |
| `fpga` | Run FPGA implementation for one or more Targets with optional baseline comparison | `FPGA:{target}` |

Shared Flow options and results are in [FLOW_REFERENCE.md](FLOW_REFERENCE.md), each Flow's own in [FLOW-SIM.md](FLOW-SIM.md), [FLOW-LINT.md](FLOW-LINT.md), [FLOW-SYNTH.md](FLOW-SYNTH.md), and [FLOW-FPGA.md](FLOW-FPGA.md), including the report files each one writes; `booley flow <name> --help` prints the options too.

**Specialists**

LLM-powered Specialists running in scoped, isolated workspaces:

Ask your Developer Agent session to invoke a Specialist by name with the arguments below. Or run `booley specialist <name> [args...]` inside the Sandbox. `booley specialist` lists visible Specialists; `booley specialist <name> --help` shows their arguments. `--max-turns` and `--timeout` are CLI-only controls.

For example: `booley specialist reviewer --category rtl --focus bugs --scope rtl`. Common options are `-C/--project PATH`, `--report-dir`, `--diagnostic`, and `--target` where supported. `--timeout DURATION` accepts positive seconds (`90` or `90s`), minutes (`30m`), hours (`2h`), and combinations (`1h30m`). Model-call minimum budgets apply; seconds-only providers round up. CLI Flow and Specialist runs never record Goal evidence; use the Developer Agent's Goal-bound MCP calls with `work_dir`.

| Specialist | Purpose | Sets | Modifies code |
|------------|---------|------|:-------------:|
| `coverage_analyst` | Explain one exact coverage.json Campaign and propose advisory next steps | — | — |
| `mutation_tester` | Proposal-locked mutation testing: creator selects exact replacements, tester builds isolated variants | `MUTATION:{target}` | — |
| `reviewer` | Single-focus code review: reports issues by severity | `REVIEW:rtl_spec`, `REVIEW:rtl_bugs`, `REVIEW:rtl_protocol`, `REVIEW:tb_quality`, `REVIEW:rtl_security`, `REVIEW:rtl_optimization`, `REVIEW:rtl_code_style` | — |

#### `coverage_analyst`

Call the `coverage_analyst` Specialist from your Developer Agent session with `campaign="<exact-coverage.json>"` and optional `instruction="<question>"`. The Analyst explains retained native evidence and proposes advisory next steps; its model only reads evidence. It does not run Simulation, read waveforms, evaluate Criteria, or approve waivers. In Goal Mode, screened Waiver Candidates support a proposed Goal change, which needs human approval. Finish requires every Goal met; advisory reports do not mark strict coverage met. Verified Target sources are optional; stale sources give report-only analysis.

#### `reviewer`

Read-only, single-focus code review. It reports `CRITICAL`, `MAJOR`, and `MINOR` findings. In Interactive Mode, review the selected files using your specification or steering. In Goal Mode, `done` reports findings without requiring fixes; the review package includes open findings. `clean` requires every finding to be fixed or waived with a justification.

Interactive source review accepts Verilog/SystemVerilog (.v, .vh, .sv, .svh), plus Python (.py) and explicitly scoped C/C++ testbench sources and headers (.c, .cc, .cpp, .cxx, .h, .hh, .hpp, .hxx) for category=tb. Goal Mode also accepts other explicitly scoped support files. Sim C/C++ harness inputs count as TB for isolation and cannot be mutation scope. Declared opposite-category sources are refused in either mode; unregistered scoped sources retain the registration warning and receive only an exact per-file category grant.

The result links saved review evidence, including rejected proposals for inspection. Rejected proposals do not affect Criteria.

Call the `reviewer` Specialist from your Developer Agent session with `category="<category>"` and `focus="<focus>"`.

| Category | Focus | What it checks | Sets |
|----------|-------|----------------|------|
| `rtl` | `bugs` | Functional bug patterns, synthesis hazards, reset/width/signing mistakes, and ifdef/config consistency | `REVIEW:rtl_bugs` |
| `rtl` | `protocol` | Bus/protocol rule compliance, handshake behavior, ordering, and clock-domain crossings (CDC) | `REVIEW:rtl_protocol` |
| `rtl` | `spec` | Spec compliance: the RTL implements what the change request/spec requires, no more and no less | `REVIEW:rtl_spec` |
| `rtl` | `code_style` | Comments, naming, readability, maintainability, magic values, and assertion/cover-point quality | `REVIEW:rtl_code_style` |
| `rtl` | `optimization` | Unused/dead RTL and strict power/performance/area improvements with no functional or engineering trade-off | `REVIEW:rtl_optimization` |
| `rtl` | `security` | Fault-injection resistance, simple power/timing leakage, secret exposure, and unsafe failure behavior | `REVIEW:rtl_security` |
| `tb` | `quality` | False-pass paths in scoped testbench sources, missing checks and edge cases, coverage gaps, timing/sampling mistakes, and TB code quality | `REVIEW:tb_quality` |

**Interactive Mode:** also pass `scope="<file,...>"`. Add `spec="<path>"` (required for the `spec` focus) or `steer=["<context>"]` for context. `dry_run=true` previews the call. Each new review replaces the previous findings.

**Goal Mode:** the Review Goal fixes scope and spec, so `scope`, `spec` and `steer` are refused. Declare each Review Goal with exactly one `target` or `paths` (see `booley cheat --goals`).

**Verify fixes (both modes):** after fixing findings, call `reviewer` with `verify=true` (CLI: `--verify`). It rechecks only the latest review's open findings, finds nothing new, and passes when none remain open. To argue a finding away, add `waive=[["<finding-id>", "<justification>"]]` (CLI: `--waive <finding-id> "<justification>"`, repeatable); the Reviewer accepts or rejects each. No other argument is accepted with verify. After repeated failed verifies, Goal Mode stops and asks you to change or abandon the Goal.

#### `mutation_tester`

Proposal-locked mutation testing. A read-only LLM creator returns exact source replacements; Booley runs a pristine baseline, then compiles and tests each replacement in isolation. It does not parse HDL or inject runtime selectors.

**Mutation campaign modes:**

| Campaign | Goal arguments | Interactive Mode arguments |
|----------|-----------------------------------------|------------------------|
| Default fixed | Target campaign with `target` + `scope` — generate 10 mutations and require all 10 detected | _(no goal arguments)_ — the same 10-of-10 campaign |
| Explicit fixed | add `total: N` and `min_detected: K` | `count="N"` requires all N; add `min_detected=K` to require K |
| Size-scaled | add `auto: true` — choose 3-25 mutations from language-neutral source size and the time budget | `count="auto"`; add `min_detected=K` for an explicit threshold |

`dry_run=true` validates Target metadata and prints the source-size breakdown and proposed auto count without invoking a Specialist or simulator.

Call the `mutation_tester` Specialist from your Developer Agent session: `scope="<rtl-file,...>"` chooses mutation sites; `target="<sim-target>"` chooses exactly one complete runnable Target suite; `steer=["<context>"]` biases mutation selection. A valid lock is reused on later runs, so new steering takes effect only with `regen_lock=true`. The Target supplies the testbench top and complete RTL closure; they are not separate caller inputs.
<!-- END GENERATED: flows -->

## Running a Flow yourself

Run a Flow yourself to check your setup or reproduce a problem. In a
container terminal:

```bash
booley flow                                  # list the Flows
booley targets --for sim                     # Targets a Flow can use
booley flow sim --target sim_soc --test reset
booley flow sim --help                       # every option
booley specialist                            # list the Specialists
booley specialist reviewer --help            # a Specialist's arguments
```

Without `--target`, a Flow runs when there is exactly one eligible Target,
and that selection is announced; with zero or multiple eligible Targets it
stops with exit code 2 and lists them. A resumed simulation (`--resume-from`) keeps the Target of the
run it resumes. Runs you start yourself never count as Goal evidence.

Options, exit codes, and results are in [FLOW_REFERENCE.md](FLOW_REFERENCE.md);
Specialists in [SPECIALISTS.md](SPECIALISTS.md);
coverage runs and the Coverage Analyst in [COVERAGE.md](COVERAGE.md).

If a command misbehaves or this page is wrong, see
[When Booley itself misbehaves](USAGE.md#when-booley-itself-misbehaves).
