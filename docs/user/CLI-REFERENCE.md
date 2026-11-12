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
| Sandbox | `chat`, `dashboard`, `flow`, `measure`, `specialist`, `worktree` |
| Either | `cheat`, `coverage`, `doctor`, `help`, `init`, `resources`, `targets` |
| Host-only | `auth`, `bootstrap`, `eda`, `projects` |

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
booley measure --validate         # check .booley/metrics.toml; measures nothing
booley worktree new <name>        # a linked worktree for parallel work; see below
booley worktree list              # each worktree's branch, changes, and use
booley worktree rm <name>         # remove a worktree, keeping its evidence and branches
booley worktree gc                # remove idle worktrees (default: a week), where safe
```

- Booley's `chat` options are read only before the agent's own arguments.
  Claude's own `--name` is `booley chat -- --name <name>`.
- `--new-worktree` and `--here` are mutually exclusive.
- `--name` takes 1-32 ASCII letters, digits, spaces, `.`, `_` or `-`, starting
  with a letter or digit; an invalid name exits with status 2.
- `--new-worktree` is refused when you run it from inside a linked worktree.
- If Booley cannot reserve a session handle for a worktree launch, it exits
  with status 2 and suggests `--here`.

Measuring a commit is not available yet: `booley measure` without
`--validate` says so and exits with status 2.

The Dashboard opens by itself when you attach to the container; see
[The dashboard](USAGE.md#the-dashboard).

### Check and look things up · either

```bash
booley cheat                      # list the cheatsheet sections
booley cheat --all                # the whole cheatsheet
booley cheat --goals              # Goal Mode, the Goal catalog, and thresholds
booley coverage uncovered <coverage.json>   # uncovered points of a coverage run
booley coverage uncovered <coverage.json> --metric branch --source rtl/fifo.sv
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

Each successful host `booley init` remembers its Project Root. `booley projects`
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
session. `booley chat` creates one (`s<N>`, on branch `booley/s<N>`) when the
main checkout is in use; see
[Several agents at once](USAGE.md#several-agents-at-once). You can also run, in
a container terminal from the main checkout:

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

The worktree gets its own Booley Directory (the `.booley/` folder), a
**Snapshot**: a copy of the Booley Directory's configuration (settings,
`cores/`, hooks, Goal and Ticket history). Build results, reports, logs, and
evidence stay in the main Booley Directory. A path Booley does not classify is
not copied, and the command names it.

<details markdown>
<summary>In stealth mode</summary>

Your Booley Directory is its own Git repository, so the worktree gets a
**Paired Checkout** instead: a checkout of its last commit on branch
`booley-worktree/<name>`. Uncommitted and untracked files are left out; one
warning names them (the first 10, then a count). Commit them first if the
worktree needs them. Booley refuses a repository with no commits or an
existing branch of that name. Ignored files stay behind. Booley refuses to
create a worktree from inside a Paired Checkout. See
[STEALTH.md](STEALTH.md).

</details>

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
| `LAST USED` | When the worktree was created, a Booley tool call last ran there, `booley chat` was last launched inside it, or the session of a worktree that `booley chat` created ended (a resumed session counts through its tool calls); `never` for a worktree that predates the record |

A worktree whose directory is gone is not listed; a warning names it.

**Removing a worktree** takes one command, run in the Sandbox from the main
checkout or another worktree (never from inside the one you remove):

```text
booley worktree rm <name> [--force]
```

It moves the worktree's evidence aside, removes the worktree, and keeps its
branch (`booley/<name>`, or the Goal Branch), so commits made in the worktree
stay reachable. A worktree on a detached HEAD, which has no branch, is reported
as `detached HEAD was <commit>`; save that commit on a branch if you need it.

Without `--force` it refuses, listing every reason, while the worktree has:

- a live Session or an active Goal Mode (an unreadable Session Registry or Goal
  Record also refuses);
- uncommitted changes in the worktree, including untracked files in a
  Snapshot's `.booley/` that are neither evidence nor a copy of the Main
  Booley Directory's file;
- commits not yet in the main checkout's branch (a branch merged by squash
  counts as merged);
- a populated submodule at a commit the main checkout's submodule lacks;
- a Git lock (`git worktree lock`);
- a damaged checkout: registered but its directory is gone, or no working
  `.git`.

`--force` removes it anyway; the evidence is still kept. Another worktree
registered inside the one you remove (a `.baseline-wt-*` checkout, a Snapshot's
own `worktrees/`) is refused even with `--force`: remove it first with
`booley worktree rm` or `git worktree remove`. So is a worktree whose `.booley/`
is a link or lies outside it.

Evidence is the worktree's `.booley/` content that Git does not keep (the
untracked files of a directory Git tracks in part, such as `plans/`, included): Common
Booley State such as `reviewer-evidence/`, `handoffs/`, the Findings Log
(`findings.jsonl`), `setup-evidence/`, reports, and logs, plus Flow reports with
their Trace Artifacts. It moves to
`.booley/worktrees/.evidence/<name>-<time>/` in the Main Booley Directory, at
the same relative paths; each attempt gets its own new directory and nothing is
overwritten. Copy out what you need from there, and keep that archive separate
from the Project's own Findings Log; don't merge the two. If the move fails, for
example across file systems, nothing is removed.

If the command is interrupted, run it again: it finishes the removal, and checks
a worktree that is still intact again, so work added since is never removed
without the refusal. It never prunes other worktrees' registrations. To reuse
the name, delete the kept branch first with
`git -C <absolute-project-root> branch -D booley/<name>`.

**Cleaning up idle worktrees.** `booley worktree gc` removes every worktree that
nobody has used for a while, so abandoned ones do not pile up:

```text
booley worktree gc [--older-than AGE]
```

A worktree counts as idle when its last use (the `LAST USED` column of
`booley worktree list`) is older than `AGE`: days, hours, minutes, and seconds
as `7d`, `36h`, `1d12h`, `90m`, or `30s`. The default is `7d`; set another
default with `gc_older_than` in the main checkout's `.booley/booley.toml`
([configuration](CONFIG.md#worktree-cleanup-worktree)). `--older-than` wins over
the setting. Run it from the main checkout or another worktree.

`booley chat` runs the same cleanup at the start of every launch, with the same
age, and prints only the worktrees it removed: it stays silent about the ones it
keeps and about a failed run (an invalid `gc_older_than`, for instance). Run
`booley worktree gc` to see why a worktree was kept.

Each idle worktree goes through `booley worktree rm` without `--force`, so
everything `rm` refuses is kept: a live Session, an active Goal Mode, and
unsaved work (see the list above). A removed worktree prints the same lines as
`rm`, evidence and branches included. Each worktree that stays prints `Kept
worktree <name>:` and why, such as that it was used too recently, that the
worktree you are in cannot be removed from inside, that it predates the
last-use record (`LAST USED` reads `never`), or `rm`'s refusal reasons. The
command ends with the totals, and exits 1 if the setting is invalid, the
worktrees cannot be listed, or a removal failed.

If `booley worktree new` fails and leaves a checkout behind, it prints the
manual Git steps with the real Sandbox paths; use those, not host paths.

<details markdown>
<summary>In stealth mode</summary>

`booley worktree rm` removes the Paired Checkout first, then the outer
worktree, and also keeps the `booley-worktree/<name>` branch. Uncommitted
changes in the Paired Checkout count toward the refusal too. To reuse the name,
also delete that branch with
`git -C <absolute-booley-directory> branch -D booley-worktree/<name>`.

</details>

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

Specialists (the Mutation Tester runs no LLM) running in scoped, isolated workspaces:

Ask your Developer Agent session to invoke a Specialist by name with the arguments below. Or run `booley specialist <name> [args...]` inside the Sandbox. `booley specialist` lists visible Specialists; `booley specialist <name> --help` shows their arguments. `--max-turns` and `--timeout` are CLI-only controls.

For example: `booley specialist reviewer --category rtl --focus bugs --scope rtl`. Common options are `-C/--project PATH`, `--report-dir`, `--diagnostic`, and `--target` where supported. `--timeout DURATION` accepts positive seconds (`90` or `90s`), minutes (`30m`), hours (`2h`), and combinations (`1h30m`). Model-call minimum budgets apply; seconds-only providers round up. CLI Flow and Specialist runs never record Goal evidence; use the Developer Agent's Goal-bound MCP calls with `work_dir`. A CLI run is also a Standalone Job: it shows in the Dashboard and copies its output to `.interactive_logs/standalone/<run_id>/job.log` in the Project's common Booley Directory (the main checkout's `.booley` from a linked worktree).

| Specialist | Purpose | Sets | Modifies code |
|------------|---------|------|:-------------:|
| `coverage_analyst` | Explain one exact coverage.json Campaign and propose advisory next steps | — | — |
| `mutation_tester` | Deterministic mutation testing: seeded Mutants from fixed Mutation Operators, each built and tested in isolation | `MUTATION:{target}` | — |
| `reviewer` | Single-focus code review: reports issues by severity | `REVIEW:rtl_spec`, `REVIEW:rtl_bugs`, `REVIEW:rtl_protocol`, `REVIEW:tb_quality`, `REVIEW:rtl_security`, `REVIEW:rtl_optimization`, `REVIEW:rtl_code_style` | — |

#### `coverage_analyst`

Call the `coverage_analyst` Specialist from your Developer Agent session with `campaign="<exact-coverage.json>"` and optional `instruction="<question>"`. The Analyst explains retained native evidence and proposes advisory next steps; its model only reads evidence. It does not run Simulation, read waveforms, evaluate Criteria, or approve waivers. In Goal Mode, screened Waiver Candidates support a proposed Goal change, which needs human approval. Finish requires every Goal met; advisory reports do not mark strict coverage met. Verified Target sources are optional; stale sources give report-only analysis.

#### `reviewer`

Read-only, single-focus code review. It reports `CRITICAL`, `MAJOR`, and `MINOR` findings. In Interactive Mode, review the selected files using your specification or steering. In Goal Mode, `done` reports findings without requiring fixes; the review package includes open findings. `clean` requires every finding to be fixed or waived with a justification.

Interactive source review accepts Verilog/SystemVerilog (.v, .vh, .sv, .svh), plus Python (.py) and explicitly scoped C/C++ testbench sources and headers (.c, .cc, .cpp, .cxx, .h, .hh, .hpp, .hxx) for category=tb. Goal Mode also accepts other explicitly scoped support files. Sim C/C++ harness inputs count as TB for isolation and cannot be mutation scope. Declared opposite-category sources are refused in either mode; unregistered scoped sources retain the registration warning and receive only an exact per-file category grant. Outside Goal Mode, both Reviewers also read firmware, the non-HDL files under `fw/`, as read-only context: they never report findings in it, such a finding is shown as out of scope, and a `scope` of only firmware is refused.

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

**Interactive Mode:** also pass `scope="<file,...>"`. Add `spec="<path>"` (required for the `spec` focus) or `spec_text="<text>"` (CLI: `--spec-text`; not with `spec`), or `steer=["<context>"]` for context. A completed review exits 0; pass `fail_on="critical|major|minor"` (CLI: `--fail-on`) to exit 1 when an open finding is at or above that severity. `dry_run=true` previews the call. Each new review replaces the previous findings.

**Changes only:** add `base="<commit>"` (CLI: `--base <commit>`) to review only the `scope` files changed since the merge-base of that commit and HEAD, uncommitted changes included; Goal Mode refuses it. Unchanged `scope` files that use them are context, the diff and commit messages are given to the Reviewer, and no change in scope exits 0 without a review. Pass every file of the category as the `scope` to give the Reviewer the files that use the changed ones. Findings already present before the change are listed separately as pre-existing and are never open or verified.

**Goal Mode:** the Review Goal fixes scope, spec and verdict, so `scope`, `spec`, `spec_text`, `fail_on` and `steer` are refused. Declare each Review Goal with exactly one `target` or `paths` (see `booley cheat --goals`).

**Verify fixes (both modes):** after fixing findings, call `reviewer` with `verify=true` (CLI: `--verify`). It rechecks only the latest review's open findings and finds nothing new; none remaining open is a pass. To argue a finding away, add `waive=[["<finding-id>", "<justification>"]]` (CLI: `--waive <finding-id> "<justification>"`, repeatable); the Reviewer accepts or rejects each. Outside Goal Mode `fail_on` (CLI: `--fail-on`) is the one other argument accepted with verify, and counts the findings still open. After repeated failed verifies, Goal Mode stops and asks you to change or abandon the Goal; Interactive Mode refuses further verifies.

#### `mutation_tester`

Deterministic mutation testing; it runs no LLM. Booley generates Mutants from the RTL, runs a pristine baseline, then compiles and tests each Mutant in isolation. The same RTL, `count`, and `seed` give identical Mutants, so results are reproducible.

**Mutation campaign modes:**

| Campaign | Goal arguments | Interactive Mode arguments |
|----------|-----------------------------------------|------------------------|
| Default fixed | Target campaign with `target` — generate 10 Mutants and require 8 detected (80 %) | _(no goal arguments)_ — the same 10-Mutant, 8-detected campaign |
| Explicit fixed | add `total: N` and `min_detected: K` | `count="N"` requires 80 % of N, rounded up; add `min_detected=K` to require K |
| Size-scaled | add `auto: true` — choose 3-25 Mutants from language-neutral source size and the time budget | `count="auto"`; add `min_detected=K` for an explicit threshold |

The default threshold is ceil(0.8 x count) detected Mutants, also for a Goal without `min_detected`; an explicit `min_detected` overrides it. Survivors are reported as `not detected`, with one note that some survivors may be equivalent Mutants, which the threshold allows for. An invalid (non-elaborating) Mutant is reported `invalid` and not scored.

`dry_run=true` validates Target metadata and prints the source-size breakdown and proposed auto count without running a simulator.

Call the `mutation_tester` Specialist from your Developer Agent session: `target="<sim-target>"` chooses exactly one complete runnable Target suite; `scope="<rtl-file,...>"` optionally chooses mutation sites (default: the Target's own RTL, meaning the Project's Verilog/SystemVerilog sources in the Target closure, minus testbench, vendored, and generated files and include headers); `seed=<int>` (default 0) picks a different reproducible Mutant set. The Target supplies the testbench top and complete RTL closure; they are not separate caller inputs.

Each Mutant's per-test timeout is max(10 x that test's baseline duration, 30 s), capped at 300 s. A timed-out Mutant counts as detected and is reported separately as `detected (timeout)`. Results and criterion detail record the reproducibility identity: scope file hashes, generator version, pyslang version, seed, and count.
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
