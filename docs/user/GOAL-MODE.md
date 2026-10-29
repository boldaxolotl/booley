# Goal Mode

Goal Mode gives one Developer Agent session mandatory, evidence-backed completion
conditions. Open a Developer Agent session and type `/booley-goal` with the work
you want done. The skill helps you choose a clean linked worktree, Goalsets,
Targets, and ad-hoc Goals, then calls `goal_enter`. Booley creates a Goal Branch
from the worktree's current HEAD and records that base for comparisons.

## Goalsets and entry

Goalsets are Project-owned Markdown files under `.booley/goalsets/`.
`booley init` creates missing `feature.md`, `bugfix.md`, `refactor.md`, and
`verification.md` without rewriting existing files. Customize their prose to
express your project's rules. The Developer Agent translates them into concrete Goals;
Booley validates the arguments, not the prose. Each Goal records its origin.

An optional `default.md` applies to every entry. Skipping it requires your
explicit instruction and reason. Setup can create this file; initialization
does not seed it. You can also add Goals for this particular change. Duplicate
Goals merge to the stricter condition, and entry reports every merge.

Goalsets, including `default.md`, come from the **Main Booley Directory**. In the Sandbox that is `BOOLEY_DIR`, even when the
directory lives outside the main checkout. The Worktree Booley Directory inside
the Goal worktree is not used: a `default.md` that exists only there does not
apply. `work_dir` selects the worktree, not the Goalsets.

A session started inside a worktree without `BOOLEY_DIR` can resolve
that worktree's copy instead. Start the session with `BOOLEY_DIR` set
to the Main Booley Directory; entry refuses a Booley Directory
inside the Goal worktree.

A Goal has one name, `CAPABILITY:target`, for example `SIM:sim_fifo`.
Review Goals use `REVIEW:rtl_bugs`. Details such as tests, thresholds and the
review requirement follow the name in parentheses. Goal arguments use the
uppercase capability as `family`. Every Goal is mandatory. For example, a Goalset excerpt can ask for:

```json
[
  {"family": "LINT", "target": "lint_fifo"},
  {"family": "SIM", "target": "sim_fifo"},
  {"family": "REVIEW", "review": "rtl_bugs", "verdict": "clean"},
  {"family": "SYNTH", "target": "synth_fifo", "thresholds": {"area_increase_at_most": "10%"}}
]
```

Use the [Goal catalog](#goal-catalog) and `booley cheat --criteria` for
the public Goal names and threshold parameters. A new Target can be
named at entry but stays unmet until it exists. Spec reviews name a spec file
relative to the worktree.

## Threshold parameters

<!-- BEGIN GENERATED: criteria-params -->
`synth` and `fpga` Goals name each Target directly and accept metric thresholds. Four flavours apply per metric: two absolute, two relative to the Goal base commit:

| Flavour param suffix | Baseline? | Meaning |
|----------------------|:---------:|---------|
| `_max` | no | metric must stay **≤** the given value |
| `_min` | no | metric must stay **≥** the given value |
| `_increase_at_most` | yes | metric may grow **at most N%** above baseline |
| `_reduce_at_least` | yes | metric must shrink **at least N%** below baseline |

Percentage threshold values must include the `%` suffix (for example, `"cell_count_reduce_at_least": "8%"` inside `thresholds`).

For a relative threshold on a new Target, set `baseline` to an existing Target. The baseline Target defaults to the candidate name and must exist at the Goal base commit. Booley runs it on base code and the candidate on current code. Missing or mismatched baseline evidence fails the check.

Goal argument examples:

```json
{"family": "SYNTH", "target": "asic_small", "baseline": "asic_base",
 "thresholds": {"cell_count_reduce_at_least": "8%"}}
```

```json
{"family": "FPGA", "target": "fpga_top", "thresholds": {"lut_count_max": 5000}}
```

**`SYNTH:{target}` (ASIC)**

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

**`FPGA:{target}` (FPGA)**

| Metric | _max | _min | _increase_at_most | _reduce_at_least |
|--------|:---:|:---:|:---:|:---:|
| `bram_count` | ✓ | — | ✓ | ✓ |
| `critical_path_ps` | ✓ | — | ✓ | ✓ |
| `dsp_count` | ✓ | — | ✓ | ✓ |
| `ff_count` | ✓ | — | ✓ | ✓ |
| `fmax_mhz` | — | ✓ | — | — |
| `lut_count` | ✓ | — | ✓ | ✓ |

> Mutually exclusive: `critical_path_ps_max` ⊕ `fmax_mhz_min`.

**`CYCLE_COUNT` of one Test Run**

`CYCLE_COUNT:{target}` Goals name the Target with `target` and one Test Run with `test` (`test`, `test+variant`, or either with `@seed`); put bounds in `thresholds`. All thresholds for that Test Run must pass. Relative forms compare the same Test Run of the same Target at the Goal base commit.

```json
{"family": "CYCLE_COUNT", "target": "sim", "test": "smoke",
 "thresholds": {"cycle_count_max": 1000, "cycle_count_reduce_at_least": "8%"}}
```

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

A named `[SIM_CYCLES] <test> <count>` observation is gated evidence only when that exact test passes. Missing, malformed, duplicate, unnamed, failed, or inconclusive evidence fails closed. Without a `CYCLE_COUNT:{target}` Goal, existing Cycle Count records remain observational.

Relative comparisons report an **observed Cycle Count change**. When declared workload inputs differ, review reports disclose the changes and do not attribute the result to RTL alone.
<!-- END GENERATED: criteria-params -->

For a SYNTH or FPGA Goal with a relative threshold (`*_increase_at_most`,
`*_reduce_at_least`) whose evidence includes a baseline, the long
`booley goal status` view (one Goal), the `goal_status` tool, the Dashboard's
Goal details and the Review Package also show the baseline revision and, per
thresholded metric, `baseline → current` with the signed change and percentage,
for example
`baseline 0123456789ab → current: area 1,200 → 1,050 μm² (-150, -12.5%)`.
Absolute Goals and Goals without baseline evidence show their usual metrics.

## Goals that name Test Runs

A **Test Run** is one Test Variant simulated with one seed, written
`test[+variant][@seed]`
([reference](FLOW-SIM.md#test-runs-test-variants-and-seeds)). Simulation,
Cycle Count and coverage Goals require exact Test Runs, and each can name them
with a selector:

```json
[
  {"family": "SIM", "target": "sim_fifo"},
  {"family": "SIM", "target": "sim_fifo", "test": "stress+soak"},
  {"family": "SIM", "target": "sim_fifo", "test": "stress+soak@42"},
  {"family": "SIM", "target": "sim_fifo", "test": "stress+soak@x20"},
  {"family": "CYCLE_COUNT", "target": "sim_fifo", "test": "burst@7",
   "thresholds": {"cycle_count_max": 1000}},
  {"family": "COVERAGE", "target": "sim_fifo", "tests": ["reset", "stress@x5"],
   "metrics": {"line": 90}}
]
```

| `family` | `test` / `tests` | Requires |
|---|---|---|
| `SIM` without `test` | none | The default Test Variant of every Test of the Target that is not skipped, each at the Default Seed. |
| `SIM` with `test` | `test`, `test+variant`, either with `@seed`, or either with `@xN` | That Test Run. `@xN` requires N Test Runs of the Test Variant with different seeds, N from 1 to 1000. |
| `CYCLE_COUNT` | `test`, `test+variant`, either with `@seed` | One Test Run. `@xN` is refused: a threshold applies to the Cycle Count of one Test Run. |
| `COVERAGE` | `"all"`, or a list of selectors, `@xN` included | Coverage floors over the merged coverage of the listed Test Runs. |

A selector without a Test Variant means the Test's default Variant. A selector
without a seed means the Target's Default Seed.

**Freezing.** At entry Booley resolves every such Goal to exact Test Runs and
stores them in the Goal Record. From then on the Goal requires those Test
Runs. Editing a Test's `default` Variant or the Target's `default_seed` in
`tests.toml` afterwards does not change what the Goal requires. For `@xN`
Booley draws N distinct seeds at random at entry, so each Goal Mode samples
new seeds, and reruns inside one Goal Mode use the same ones. Goals of one
Target that name the same Test Variant with the same N share one set of
seeds.

Goals that resolve to the same Test Runs merge at entry, like other duplicate
Goals. Of two `@xN` Goals on the same Test Variant, the larger N remains. A
Goal with a pinned seed and an `@xN` Goal stay separate Goals. A Goal that
names a seed on an unseeded Target is refused at entry. So is a Goal that names
a Test its Target skips in `tests.toml`, and so is a Goal change that would
freeze one: remove the Test from `skip` or drop the Goal.

**Targets and Tests that do not exist yet.** A Goal can name a Target, Test or
Test Variant that the work will add. That part of the Goal is *pending* and
the Goal is unmet. Booley freezes it when it first exists, at the next Flow
run or status read, with the defaults `tests.toml` declares at that moment. A
Goal over a whole Target also freezes each Test that is added to the Target
later. The Review Package lists every such Test Run under the heading
`Test Runs resolved after entry`.

**Evidence.** A Test Run counts for a Goal only when its name and its declared
arguments and environment equal one of the Goal's frozen Test Runs. Each
frozen Test Run is `passed`, `failed` or `missing`, computed from applicable
evidence history at the same Goal revision and dependent file contents.
Observations retain their own Test Run rows; older stored outcome copies are
ignored. Evidence without Test Run rows supplies no outcome:

- It becomes `passed` when it passes at the current contents of the files the
  Goal depends on.
- It becomes `failed` when it fails at those contents, and a later pass at the
  same contents does not change that. A flaky seed therefore stays failed
  until the code changes.
- It is `missing` until it has run. An `error` or `inconclusive` Test Run
  leaves the outcome as it was.

A diagnostic MCP run changes none of these outcomes, even when it runs a
frozen Test Run. Its report names the Goals whose frozen Test Runs ran and
the ones that failed; use a Goal-bound Developer Agent MCP call with `work_dir`
and without `diagnostic` to record them. CLI runs never record Goal evidence.

A simulation Goal is met when every frozen Test Run is `passed`. They do not
have to pass in one invocation: 20 seeds can pass over several runs and
Simulation Campaigns, including after an interruption, and a run of a few
named Test Runs adds their outcomes without disturbing the others. A Cycle
Count Goal takes its measurement from a run of its frozen Test Run, and that
Test Run must be `passed`.

A coverage Goal is decided by its coverage floors alone. It takes its
measurement from one `--coverage` run that ran exactly its frozen Test Runs,
because coverage is not merged across runs. Whether those Test Runs passed
does not matter to it: when the floors are met the Goal is met, even if a
Test Run failed in that run or in any other. `goal_status` shows the outcomes
of the measuring run under the Goal, with a rerun command for each failure.
Add a simulation Goal for the same Test Runs when they must also pass. A run
that does not measure the Goal, such as a plain simulation, leaves a
measurement at the same file contents as it was. A failing Test Run that ends
before the simulator writes its coverage data makes collection incomplete, so
that run measures nothing either. With no earlier measurement at those
contents, the Goal stays unmet as not measured.

If a frozen Test Run's Test or Test Variant is removed, or its declared
arguments or environment change, the Goal stays unmet until you approve a
retarget. If its Test is skipped in `tests.toml`, the Goal stays unmet until
the Test leaves `skip` or you approve a Goal change that drops it; a retarget of
a Goal that names the skipped Test is refused, while a retarget of a Goal on the
whole Target freezes its suite again without that Test. See
[A Goal that names Test Runs stays unmet](TROUBLESHOOTING.md#a-goal-that-names-test-runs-stays-unmet)
for each reason `goal_status` can show.

**Seeing the frozen Test Runs.** `goal_status`, `booley goal status` and the
Dashboard list each Goal's frozen Test Runs with their outcomes, the parts
still pending, the Test Runs resolved after entry, and a rerun command for every
failed Test Run. The Review Package computes the same outcomes from its frozen
evidence and lists each frozen Test Run with its rerun command. It retains the
contributing observations, so later runs do not change a saved package.

## Working with evidence

The Developer Agent passes the absolute worktree root as `work_dir` to every tool that
accepts it.

Built-in MCP worktree routing:

| Accepts `work_dir` | Does not accept `work_dir` |
| --- | --- |
| `sim`, `lint`, `synth`, `fpga` | `booley_status` |
| `reviewer`, `mutation_tester`, `coverage_analyst` | `booley_poll`, `booley_cancel` |
| `goal_enter`, `goal_status`, `goal_propose_change`, `goal_finish` | `booley_sleep`, `coverage_evidence` |
| `booley_targets`, `booley_report` | `bwave` |

`booley_sleep` appears only when diagnostic tools are enabled, and
`coverage_evidence` only in the Coverage Analyst's nested server.

While any Goal Record occupies the Project, Booley Flows, Specialists, Goal
tools, and `booley_report` refuse a call without `work_dir`, even a diagnostic
call outside that worktree; `booley_targets` may omit it. Project-defined
endpoints must declare `work_dir` to run.

Only Booley Flows and Specialists meet Goals. Shell output or the Developer Agent saying
"done" does not. `goal_status` shows each Goal's current evidence and freshness;
code changes can require another run. For a bugfix, reproduce the reported bug
with a failing test before editing the design.

Protected Inputs decide how evidence is produced: `booley.toml`,
`FUSESOC_IGNORE`, and the Project's `hooks`, `.managed`, `generators`, and
`mcp_tools` directories. Editing one blocks Finish until reverted and discards
affected evidence. Never weaken tests or Targets to make a Goal pass. The
Review Package flags `.core`, `tests.toml`, and `.sdc`/`.xdc` edits.

## Simulation baseline reports

Entering Goal Mode starts a background Job for each SIM Goal's frozen Test Runs
at the Goal Branch's base contents. Adding a SIM Goal through an approved Goal
Change schedules its baseline too. Entry returns while the Job runs; status
shows the baseline as pending. Booley reuses a result for the same Target,
resolved Test Run and dependent file contents across sessions and worktrees.
Protected execution inputs are part of the cache identity too.

`goal_status`, `booley goal status`, the Dashboard's Goal details and the Review
Package show a summary for each SIM Goal. Every Test Run other than
`pass -> pass` is named, with its Variant and seed:

| Transition | Meaning |
|---|---|
| `pass -> pass` | Passed at baseline and passes at current contents. |
| `fail -> pass` | Failed at baseline and passes now. |
| `pass -> fail` | Passed at baseline and fails now: a regression. |
| `fail -> fail` | Failed at baseline and still fails. |
| `new (seen failing) -> pass` | Absent at baseline; a matching functional failure was recorded earlier in this session at different dependent contents. |
| `new (never seen failing) -> pass` | Absent at baseline and no qualifying earlier failure was recorded. |
| `new -> fail` | Absent at baseline and fails now. |

Baseline and current evidence must name the same resolved Test Run, including
Variant, seed and declared arguments. An existing Test with incompatible inputs
is **not comparable**, with a reason. A new Test never runs against the baseline design
using a newly authored testbench. A stale or missing current result is shown as unavailable
rather than a transition. The Review Package calls attention to regressions and
new Test Runs that passed without being seen failing.

These reports do not change any Goal's met, unmet or stale state and never block
Finish. A build error, timeout, interrupted Job, unavailable dependency or
ungraded result is **unavailable**, with its reason, rather than a failing Test.
Only a recorded functional Test failure counts as seen failing; a timeout or
infrastructure error does not. Earlier evidence without that classification
cannot prove a functional failure. Finish freezes the report as it is, so its
Review Package may still show a pending baseline.

Booley verifies versioned baseline dependencies against the base commit and
uses captured entry configuration for an unversioned Booley Directory. Dependencies
outside those pinned or captured inputs make the report unavailable. The Project
must ignore Booley's baseline scratch directory, as Project Initialization
configures it; otherwise Booley reports an unavailable baseline without leaving
scratch files that would affect entry or Finish. The advice to reproduce a bug
before editing remains unconditional.

## Synthesis baseline Jobs

Entering Goal Mode, or approving a Goal Change that adds one, starts a
background synthesis Job for each baseline-relative SYNTH Goal. It synthesises
the Goal's baseline Target at the Goal Branch's base contents, with the
synthesis recipe frozen at that entry or change. Absolute SYNTH Goals get none.
Booley reuses a result for the same Target identity, frozen recipe, dependent
file contents and Protected Inputs across sessions and worktrees. Candidate
runs still synthesise the baseline themselves.

At most one baseline Job per Flow runs at a time, SIM included; others wait,
and the wait does not count against a Job's time budget. `goal_status` and
`booley goal status` print `baseline: pending`, `baseline: complete` or
`baseline: unavailable (<reason>)` for each such Goal. An interrupted,
timed-out, infrastructure-failed or recipe-mismatched Job is unavailable and
stores no result. Like simulation baselines, it never changes a Goal's met,
unmet or stale state.

## Changes and human decisions

When a Goal needs to be added, relaxed, retargeted, or changed through a coverage
waiver, the Developer Agent uses `goal_propose_change`. You see the exact proposal and
approve or reject it with a reason. The client form is used when available;
chat fallback records your quoted instruction against the saved proposal ID.
Silence cannot approve a change. Approved changes appear in the Change Log.
Coverage Waiver Candidates need your decision; the Analyst cannot approve them.

Changing which Test Runs a Goal requires is a Goal change like any other:

| Change | Kind | Frozen Test Runs |
|---|---|---|
| Add a Goal that names a Test Run | `add` | Frozen when the change is applied. |
| Pin a seed, name another Test Variant, or raise N | `retarget` | Raising N keeps the frozen seeds and draws only the difference. |
| Lower N | `relax` | The Goal keeps its first N frozen seeds. |
| Retarget a Goal without changing how it is written | `retarget` | Frozen again from the Target as it is now. Use this when a frozen Test Run is gone or its declaration changed. |

A change that would freeze a Test Run of a Test the Target skips in `tests.toml`
is refused.

After any of these changes the Goal's Test Run outcomes start again.

Goal Mode clients must expose MCP `structuredContent` to the Developer Agent.
Entry and status return the internal `goal_key` change addresses there, separately
from human Goal names. A client that forwards only text can read status but cannot
address an existing Goal for a change.

## Finish and review

Finish requires all Goals met with fresh evidence at a clean, committed HEAD
and a Session Summary. The Developer Agent calls `goal_finish` with the record ID, a
stable operation ID, and that summary. The Review Package contains the diff
against the base, final Goals and evidence, the Change Log, open review
findings, Target changes, constraint edits, and the Session Summary.

A `clean` review requires no open findings. A `done` review is advisory:
findings remain in the package and do not require a second approval to Finish.
Read the package before deciding what to merge. Completion keeps the Goal
Branch and worktree; merge, publication, and cleanup need separate instructions.

Local Goal Records live under `.booley/goals/<goal-id>/`. Outside
Stealth, Finish commits the Session Summary under
`.booley/goals/history/<goal-id>.md` in the Goal worktree when that
path is Git-trackable and owned by the RTL repository. Stealth, excluded, or
Paired Checkout summaries stay local.

Booley deletes a finished, abandoned, or failed Goal Record, with its Session
Summary and Review Package, once both its worktree and its Goal Branch are gone.
A worktree is gone when it is absent from Git’s worktree registry: after
`git worktree remove`, or
after `git worktree prune` for a worktree directory you deleted by hand.
Booley starts a background cleanup after every Goal entry, Finish, and abandonment;
the command returns without waiting for Git checks or deletion. If the worker
cannot start or is interrupted, the next lifecycle command retries cleanup. A record is kept
whenever Git cannot confirm that the worktree or branch is gone, the record is
unreadable, or another command is using it. Committed summaries under `goals/history/`
are never deleted. Copy anything you want to keep before you remove both the
worktree and the branch.

## Recovery, unattended work, and parallel sessions

The same Goal session can work with your guidance or unattended. If intent is
missing or a Goal cannot be met, the Developer Agent explains what decision it needs.
After a crash or reconnection, ask it to call `goal_status(rules=true)` in the
same worktree before continuing. Saved proposal IDs and Finish operation IDs
allow recovery without duplicating a decision or completion. A Finish result
of `revalidation_required` needs fresh evidence and a new summary and operation ID.

Abandonment is available only on your explicit instruction, through
`goal_finish(abandon=true)` with your quoted words. Quiet presence or a Developer Agent
being stuck never abandons a Goal automatically. The branch and worktree remain.

For parallel work, use one linked worktree and Goal Branch per session. Start
another Developer Agent in another container terminal and invoke `/booley-goal` there.
Jobs share the Project's [admission caps](CONFIG.md#jobs--concurrency-jobs).
`booley dashboard` shows sessions, Goals, and Jobs inside the Sandbox; it opens
on VS Code folder attachment by default. Opt out with `[sandbox].dashboard=false`.
Each session shows what its agent client last reported: working, waiting for
tool call, or waiting for user input. Click a report, artifact, or evidence
path in a detail view to open it in the editor.

The Dashboard has two windows, Sessions and Jobs. Press `←` or `→` to switch
between them; each window keeps its place, so switching back returns to the
same detail and row. Press `↑` on the first row (or in an empty list) to reach
the top bar with the window tabs and Help: `←` and `→` choose, `Enter` applies,
and `↓` or `Esc` return to the list. `Esc` goes back one level inside a window
and does nothing at its top level.

The Sessions window groups sessions by what needs you: NEEDS YOU (waiting for
your input, or holding a pending Goal-change proposal), WORKING, and UNKNOWN
(no fresh activity report). A STANDALONE JOBS group lists running Jobs that
belong to no session, and only while one runs. Inside a group the session that
has been in its state longest comes first. Each row has the columns Session,
Mode, Branch, Work dir, Started and Activity; a missing branch or work dir
shows as a dim `—`. When the list is narrow (a narrow terminal, or beside the
preview) Started disappears first, then Work dir, then Branch, and values that
still do not fit end in `…`. On a terminal 120 columns or wider, a preview of
the highlighted row sits beside the list: a session's mode, activity, branch,
work dir, start age, connection, last Booley call, shared-worktree warning,
Goal summary and unfinished Jobs, or a standalone Job's detail. `Enter` still
opens the session detail. The Sessions tab reads `Sessions N◆` while N sessions
need you.

While the Jobs window is shown, the top bar also holds its filters: Running
(queued or running Jobs), Finished (the newest 50) and All, then an owner chip.
`Enter` on the chip cycles All sessions, each session handle (including
sessions known only through retained Jobs), Standalone, and back. The Jobs tab
shows how many Jobs are unfinished, and nothing when there are none. An empty
list says why and which filter shows something.

Press `s` for the session list, `j` for running Jobs, `r` for finished Jobs,
or `a` for all Jobs; the Job keys open the Jobs window. The Jobs window opens
on all sessions, whichever window or detail you came from, and keeps the
Running, Finished or All choice while you switch windows; press `u` to go back
to all sessions.
`?` shows help.

The Jobs list has the columns Job, Target, Session, State, Result, Crit and
Elapsed. Crit repeats the detail's `Criterion:` line as `✓` (met) or `✗` (not
met) and is blank when the Job has none. When the list is too narrow, long
Targets are cut with `…` first (down to six characters), then long Result
words. The Job detail (beside the list on wide windows, or open with Enter) shows the
full values, plus the exit code, the stage and the session's connection.

Sessions have short, persistent Project handles such as `S17`. Jobs include
their kind: `J-SIM25`, `J-SYN34`, `J-LINT45`, or `J-FPGA33`; simulation baseline
Jobs use `SIM`, synthesis baseline Jobs use `SYN`, and other endpoints use `J-JOB<n>`. One Job counter covers every
kind, so numbers may skip within a kind. Handles survive Dashboard restarts
and session pruning. The `1`–`9` keys remain navigation shortcuts, independent
of handles. Full session keys and Job run IDs are secondary text in details;
MCP polling continues to use the full run ID.

Sessions receive handles when registered; Jobs receive them when created.
Historical records without allocations keep `S?` or `J-SIM?` (and the equivalent
for other kinds). Handle-storage failures show a diagnostic. The Dashboard
itself only reads handles and never allocates them.

For scripted inspection, `booley goal status` shows this worktree's Goal Mode,
or every active Goal Mode in the Project when this worktree hosts none. In a
worktree without an active Goal Mode, `booley goal status` and `goal_status`
also show its most recently ended Goal Record: Goal id, state, end time, the
failure reason of a failed record, and the Session Summary and Review Package
paths when those files exist.

## Goal catalog

Every supported Goal has the same name in status, proposals, the Dashboard and
Review Package. `{target}` is its bare name when unique, or its qualified selector otherwise.

<!-- BEGIN GENERATED: criteria -->
| Goal | Requirement | Evidence producer |
|------|-------------|-------------------|
| `LINT:{target}` | The Target's linter passes with no unwaived findings | `lint` |
| `SIM:{target}` | RTL simulation passes all tests | `sim` |
| `ELAB:{target}` | RTL/TB compiles and elaborates cleanly (no simulation) | `sim` (`mode="elab-only"`) |
| `SYNTH:{target}` | ASIC synthesis completes within area/timing budgets | `synth` |
| `FPGA:{target}` | FPGA implementation completes within resource/timing budgets | `fpga` |
| `CYCLE_COUNT:{target}` | A named test passes and its observed Cycle Count meets every declared threshold | `sim` |
| `COVERAGE:{target}` | Native Coverage Campaign policy for one Simulation Target | `sim` (`coverage=true`) |
| `MUTATION:{target}` | Mutation testing achieves minimum kill rate | `mutation_tester` |
| `REVIEW:rtl_spec` | RTL review: spec compliance (RTL matches the Goal/spec, no more, no less) | `reviewer` (`category="rtl"`, `focus="spec"`) |
| `REVIEW:rtl_bugs` | RTL review: bug patterns, synthesis hazards, and ifdef/config consistency (the RTL as hardware, not against the spec) | `reviewer` (`category="rtl"`, `focus="bugs"`) |
| `REVIEW:rtl_protocol` | RTL review: bus/protocol compliance and clock-domain crossings (CDC) | `reviewer` (`category="rtl"`, `focus="protocol"`) |
| `REVIEW:rtl_security` | RTL review: hardware attack resistance to fault injection, simple power/timing analysis, and secret exposure (post-sim) | `reviewer` (`category="rtl"`, `focus="security"`) |
| `REVIEW:rtl_optimization` | RTL review: unused/dead code and missed power/performance/area wins, strict improvements only (post-sim) | `reviewer` (`category="rtl"`, `focus="optimization"`) |
| `REVIEW:rtl_code_style` | RTL review: comments, naming, readability, and assertion coverage (post-sim) | `reviewer` (`category="rtl"`, `focus="code_style"`) |
| `REVIEW:tb_quality` | Source-scoped TB review: false-pass detection, coverage gaps, and TB code quality | `reviewer` (`category="tb"`, `focus="quality"`) |

`{target}` is the bare Target name when unique, or its qualified selector otherwise. Details (thresholds, tests, baseline,
review verdict and spec) follow the name in parentheses. Review Goals require
`clean` or `done`; the name is identical for both requirements.
Each Review Goal also names exactly one scope: `target` (that Target's RTL or TB
files plus dependencies) or `paths` (files relative to the Goal worktree). Examples:
`{"family":"REVIEW","review":"rtl_bugs","verdict":"clean","target":"top"}`,
`{"family":"REVIEW","review":"tb_quality","verdict":"done","paths":["tb/top_tb.sv"]}`.

Goal argument `family` uses the uppercase capability before the colon.
Internal evidence keys and Project-defined evidence bindings are not Goal names.
<!-- END GENERATED: criteria -->
