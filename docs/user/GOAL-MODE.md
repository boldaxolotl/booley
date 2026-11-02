# Goal Mode

Goal Mode gives one Developer Agent session mandatory, evidence-backed completion
conditions. It is the recommended way to run long, unattended work: the Goals
keep the Developer Agent within the limits you set, such as passing tests,
area, and timing budgets, and the work is not done until Booley's own evidence
shows every Goal is met.

## How to use Goal Mode

1. **Set up the Goals.** Open a Developer Agent session and type
   `/booley-goal` followed by the work you want done. The skill helps you
   choose a clean linked worktree, [Goalsets](#goalsets), Targets, and ad-hoc
   Goals, shows you the final Goals, then calls `goal_enter`. Booley creates a
   Goal Branch from the worktree's current HEAD and records that base for
   comparisons. The skill stops there.
2. **Clear the chat.** Once entry succeeds, type `/clear`. The Goals live in
   the Goal Record, not in the chat, so nothing is lost, and the work starts
   with a full context window.
3. **Type `/booley-goal start`.** In the same chat, now cleared, type
   `/booley-goal start`. The skill finds the active Goal Record, then does the
   work and gathers evidence until every Goal is met and it can finish. With
   several active Goal Records in the Project, it asks you which one to work
   on.

In Codex, type `$booley-goal` instead of `/booley-goal` in steps 1 and 3.

## Goals

A Goal is one condition the work must meet before it counts as done, such as
"the simulation passes" or "area grows by at most 10%". Every Goal is
mandatory. The Developer Agent cannot declare a Goal met: Booley checks it
against fresh results from its own Flows and Specialists.

The main groups of Goals are:

- **Lint and compile:** the linter passes and the RTL elaborates cleanly.
- **Simulation:** tests pass, a test finishes within a cycle budget, coverage
  reaches its floors, and mutation testing kills enough mutants.
- **Implementation:** ASIC synthesis or FPGA implementation stays within area,
  resource, and timing limits, as absolute values or relative to the code you
  started from.
- **Review:** an AI Reviewer checks the RTL or testbench for bugs, spec
  compliance, protocol, security, optimization, or code style.

Each Goal has a short name such as `SIM:sim_fifo` or `REVIEW:rtl_bugs`, with
details in parentheses, for example `SYNTH:synth_fifo (area increase ≤ 10%)`.
You describe Goals in plain words; the Developer Agent writes the exact
arguments. The [Goal catalog](#goal-catalog) lists every Goal, and
`booley cheat --criteria` prints the threshold parameters.

## Thresholds

Synthesis, FPGA, and Cycle Count Goals can carry limits. A limit is either
absolute, such as "at most 5,000 LUTs" or "Fmax at least 200 MHz", or relative
to the code the Goal Mode started from, such as "area grows at most 10%" or
"cycle count drops at least 8%". For a relative limit, Booley runs the same
Target on the starting code and compares the two results. Describe the limits
you want; the Developer Agent looks up the exact parameter names with
`booley cheat --criteria`.

For a SYNTH or FPGA Goal with a relative limit whose evidence includes a
baseline, the long
`booley goal status` view (one Goal), the `goal_status` tool, the Dashboard's
Goal details and the Review Package also show the baseline revision and, per
thresholded metric, `baseline → current` with the signed change and percentage,
for example
`baseline 0123456789ab → current: area 1,200 → 1,050 μm² (-150, -12.5%)`.
Absolute Goals and Goals without baseline evidence show their usual metrics.

## Simulation, Cycle Count, and coverage Goals

Simulation, Cycle Count, and coverage Goals can say exactly how a test must
run. You can ask for a test to run several times with different random seeds,
for example "the stress test passes with 20 seeds", or once with a specific
seed you want to reproduce. You can also pick a
[Test Variant](FLOW-SIM.md#test-runs-test-variants-and-seeds) declared in
`tests.toml`. One simulation of one Variant with one seed is a **Test Run**.

| Goal | What it can ask for | Example selector |
|---|---|---|
| Simulation | Every Test of the Target that is not skipped, each once at the Default Seed | none |
| Simulation | One Test, or one Variant of it | `stress`, `stress+soak` |
| Simulation | One Test Run with a specific seed | `stress+soak@42` |
| Simulation | N Test Runs with different random seeds, N from 1 to 1000 | `stress+soak@x20` |
| Cycle Count | One Test Run, optionally with a specific seed. Several seeds are refused: a threshold applies to the Cycle Count of one Test Run | `burst@7` |
| Coverage | All Tests, or a list of Test Runs, several seeds included. The coverage floors apply to their merged coverage | `all`, `reset, stress@x5` |

Without a Variant, a selector means the Test's default Variant. Without a
seed, it means the Target's Default Seed.

**Freezing.** When Goal Mode starts, Booley fixes each Goal's exact Test Runs.
Later edits to defaults in `tests.toml` do not change them. For "N seeds",
Booley picks the N random seeds once: reruns in the same Goal Mode reuse them,
and the next Goal Mode gets new ones. Goals that ask for the same Test Runs
merge, keeping the larger N. A seed on an unseeded Target, or a Test the
Target skips in `tests.toml`, is refused.

**Targets and Tests that do not exist yet.** A Goal can name a Target, Test or
Test Variant that the work will add. That part of the Goal is *pending* and
the Goal is unmet. Booley freezes it when it first exists, at the next Flow
run or status read, with the defaults `tests.toml` declares at that moment. A
Goal over a whole Target also freezes each Test that is added to the Target
later. The Review Package lists every such Test Run under the heading
`Test Runs resolved after entry`.

**Evidence.** Each frozen Test Run is `passed`, `failed`, or `missing` (not
run yet). A failure stays until the code changes, so rerunning a flaky seed
until it passes does not help. Diagnostic runs and runs you start yourself do
not count (see [Working with evidence](#working-with-evidence)).

- A **simulation** Goal is met when all its Test Runs pass. They can pass over
  several runs.
- A **Cycle Count** Goal is measured from a passing run of its Test Run.
- A **coverage** Goal is met when one coverage run of exactly its Test Runs
  reaches the floors, even if some of those Test Runs failed. Add a simulation
  Goal too if they must also pass.

If you remove, change, or skip a frozen Test, its Goal stays unmet until you
approve a Goal change. See
[A Goal that names Test Runs stays unmet](TROUBLESHOOTING.md#a-goal-that-names-test-runs-stays-unmet)
for each reason `goal_status` can show.

**Seeing the frozen Test Runs.** `goal_status`, `booley goal status` and the
Dashboard list each Goal's frozen Test Runs with their outcomes, the parts
still pending, the Test Runs resolved after entry, and a rerun command for every
failed Test Run. The Review Package computes the same outcomes from its frozen
evidence and lists each frozen Test Run with its rerun command. It retains the
contributing observations, so later runs do not change a saved package.

## Working with evidence

Only Booley's own Flows and Specialists can meet a Goal. The Developer Agent
runs them through Goal-bound MCP calls that name the Goal worktree as
`work_dir`. Shell output, or the agent saying "done", does not count, and
CLI runs never record Goal evidence: a `booley flow sim` you run yourself is
just a look. After code changes a Goal may need a fresh run; `goal_status` shows which.

Some Project files decide how evidence is produced: `booley.toml`,
`FUSESOC_IGNORE`, and the Project's `hooks`, `.managed`, `generators`, and
`mcp_tools` directories. Editing one during Goal Mode blocks Finish until you
revert it. The Review Package flags edits to `.core` files, `tests.toml`, and
constraint files (`.sdc`, `.xdc`).

## Baseline comparisons

Booley also runs some Goals on the code the Goal Mode started from, so you can
see what changed. These runs never change whether a Goal is met.

### Simulation

Entering Goal Mode starts a background Job for each SIM Goal's frozen Test Runs
at the Goal Branch's base contents. Adding a SIM Goal through an approved Goal
Change schedules its baseline too. Entry returns while the Job runs; status
shows the baseline as pending.

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

### Synthesis

Entering Goal Mode, or approving a Goal Change that adds one, starts a
background synthesis Job for each baseline-relative SYNTH Goal. It synthesises
the Goal's baseline Target at the Goal Branch's base contents, with the
synthesis recipe frozen at that entry or change. Absolute SYNTH Goals get none.

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
Summary and Review Package, once both its worktree (`git worktree remove`, or
`git worktree prune` after deleting the directory by hand) and its Goal Branch
are gone. Committed summaries under `goals/history/` are kept. Copy anything
you want to keep before you remove both the worktree and the branch.

## Recovery, unattended work, and parallel sessions

The same Goal session can work with your guidance or unattended. If intent is
missing or a Goal cannot be met, the Developer Agent explains what decision it needs.
After a crash or reconnection, type `/booley-goal start`; it calls
`goal_status(rules=true)` in the Goal worktree before continuing. Saved proposal IDs and Finish operation IDs
allow recovery without duplicating a decision or completion. A Finish result
of `revalidation_required` needs fresh evidence and a new summary and operation ID.

Abandonment is available only on your explicit instruction, through
`goal_finish(abandon=true)` with your quoted words. Quiet presence or a Developer Agent
being stuck never abandons a Goal automatically. The branch and worktree remain.

For parallel work, use one linked worktree and Goal Branch per session. Start
another Developer Agent in another container terminal and follow the same three
steps there. `/booley-goal start` asks which Goal Mode to work on.
Jobs share the Project's [admission caps](CONFIG.md#jobs--concurrency-jobs).

For scripted inspection, `booley goal status` shows this worktree's Goal Mode,
or every active Goal Mode in the Project when this worktree hosts none. In a
worktree without an active Goal Mode, `booley goal status` and `goal_status`
also show its most recently ended Goal Record: Goal id, state, end time, the
failure reason of a failed record, and the Session Summary and Review Package
paths when those files exist.

## Dashboard

`booley dashboard` shows sessions, Goals, and Jobs inside the Sandbox. It opens
when VS Code attaches to the folder; opt out with `[sandbox].dashboard=false`.
Each session shows what its agent client last reported: working, waiting for
tool call, or waiting for user input. Click a report, artifact, or evidence
path in a detail view to open it in the editor.

The Dashboard has two windows, Sessions and Jobs. Press `←` or `→` to switch
between them. The Sessions window groups sessions by state; NEEDS YOU holds the
ones waiting for your input or a Goal-change decision, and the tab reads
`Sessions N◆` while N sessions need you. The Jobs window filters Running,
Finished (the newest 50) or All Jobs, and by owning session. Press `↑` on the
first row to reach the top bar with the tabs, filters, and Help. The footer
shows the keys that apply where the cursor is.

Sessions have short Project handles such as `S17`; Jobs carry their kind, as in
`J-SIM25`, `J-SYN34`, `J-LINT45`, `J-FPGA33` or `J-JOB<n>`. A session resumed
after a long pause may get a new handle.

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

## Goalsets

A Goalset is a Markdown file under `.booley/goalsets/` that stores a set of
Goals you use often, written in plain prose. When you set up Goals, name a
Goalset and the Developer Agent turns it into concrete Goals. `booley init`
creates `feature.md`, `bugfix.md`, `refactor.md`, and `verification.md` for
you to edit. An optional `default.md` applies to every Goal Mode unless you
say to skip it.
