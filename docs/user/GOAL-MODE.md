# Goal Mode

Goal Mode gives one Developer Agent session mandatory, evidence-backed completion
conditions. It is the recommended way to run long, unattended work: the Goals
keep the Developer Agent within the limits you set, such as passing tests,
area, and timing budgets, and the work is not done until Booley's own evidence
shows every Goal is met. Because nobody steers the agent once it starts, it
needs a detailed specification up front (see step 1 of
[How to use Goal Mode](#how-to-use-goal-mode)).

Goal Mode is most useful for performance, frequency, and area optimization,
where you want to squeeze the most out of a design while it stays functionally
correct. It also works well for building new features and modules. It is not
much help for debugging; use [Interactive Mode](USAGE.md#interactive-mode) for
that.

## How to use Goal Mode

1. **Describe the work in detail.** Open a Developer Agent session and type
   `/booley-goal` followed by a detailed specification of the work, or a path
   to a spec file in the repository. Once Goal Mode starts, the agent works
   unattended. Anything the spec leaves out, it has to guess, and the Goals
   only check what they measure. A good spec covers:

    - what to build or change, and why;
    - interfaces: ports, registers, protocols, and timing;
    - expected behavior, including corner cases and error handling;
    - limits: area, timing, cycle counts, and what must not change;
    - how to verify it: which tests to add or which existing tests must pass.

    The skill then helps you choose a clean linked worktree,
    [Goalsets](#goalsets), Targets, and ad-hoc Goals. Then it stops and shows
    you the final Goals; review them, ask for changes, and confirm. Only after
    you confirm does it enter Goal Mode: Booley creates a Goal Branch from the
    worktree's current HEAD and records that base for comparisons. The skill
    stops there.
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

Goals come in four groups:

- **Lint and compile:** the linter passes and the RTL elaborates cleanly (see
  the table below).
- **Simulation:** tests pass, a test finishes within a cycle budget, coverage
  reaches its floors, and mutation testing kills enough mutants. See
  [Simulation, Cycle Count, and coverage Goals](#simulation-cycle-count-and-coverage-goals).
- **Implementation:** ASIC synthesis or FPGA implementation stays within area,
  resource, and timing limits. See [Thresholds](#thresholds).
- **Review:** an AI Reviewer checks the RTL or testbench for bugs, spec
  compliance, protocol, security, optimization, or code style. See
  [Review Goals](#review-goals).

| Goal | Met when |
|---|---|
| `LINT:<target>` | The Target's linter passes with no unwaived findings |
| `ELAB:<target>` | The RTL and testbench compile and elaborate cleanly, without simulating |

Each Goal has a short name such as `SIM:sim_fifo` or `REVIEW:rtl_bugs`, with
details in parentheses, for example `SYNTH:synth_fifo (area increase ≤ 10%)`.
`<target>` is the Target's name, or its qualified selector when the name is
not unique. You describe Goals in plain words; the Developer Agent writes the
exact arguments. `booley cheat --goals` prints every Goal with its parameters.

## Thresholds

| Goal | Met when |
|---|---|
| `SYNTH:<target>` | ASIC synthesis completes within its area and timing limits |
| `FPGA:<target>` | FPGA implementation completes within its resource and timing limits |

Synthesis, FPGA, and Cycle Count Goals can carry limits. A limit is either
absolute, such as "at most 5,000 LUTs" or "Fmax at least 200 MHz", or relative
to the code the Goal Mode started from, such as "area grows at most 10%" or
"cycle count drops at least 8%". Describe the limits you want; the Developer
Agent looks up the exact parameter names with `booley cheat --goals`.

For a relative limit, Booley runs the same Target on the starting code in a
background Job and compares the two results. The Dashboard's Goal details and
the Review Package show the comparison, for example
`area 1,200 → 1,050 μm² (-150, -12.5%)`.

Booley also runs each simulation Goal's tests on the starting code, so the
Review Package can show what changed: regressions (`pass -> fail`), fixes
(`fail -> pass`), and new tests. It flags new tests that passed without ever
being seen failing, since such a test may not check anything. This comparison
is for your review only: it never decides whether a Goal is met.

## Simulation, Cycle Count, and coverage Goals

| Goal | Met when |
|---|---|
| `SIM:<target>` | The Goal's tests pass in RTL simulation |
| `CYCLE_COUNT:<target>` | A named test passes and its Cycle Count meets every limit |
| `COVERAGE:<target>` | A coverage run reaches the coverage floors |
| `MUTATION:<target>` | Mutation testing kills at least the minimum share of mutants |

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
for each reason a Goal can stay unmet.

**Seeing the frozen Test Runs.** The Dashboard's Goal details list each Goal's
frozen Test Runs with their outcomes, the parts still pending, the Test Runs
resolved after entry, and a rerun command for every failed Test Run. The
Review Package lists the same, and later runs do not change a saved package.

## Review Goals

A Review Goal asks an AI Reviewer to check the RTL or testbench for one focus:

| Goal | What the Reviewer checks |
|---|---|
| `REVIEW:rtl_spec` | The RTL matches the spec, no more and no less |
| `REVIEW:rtl_bugs` | Bug patterns, synthesis hazards, and `ifdef`/config consistency |
| `REVIEW:rtl_protocol` | Bus and protocol compliance, and clock-domain crossings (CDC) |
| `REVIEW:rtl_security` | Resistance to fault injection and simple power or timing analysis, and secret exposure |
| `REVIEW:rtl_optimization` | Dead code and missed power, performance, or area wins |
| `REVIEW:rtl_code_style` | Comments, naming, readability, and assertion coverage |
| `REVIEW:tb_quality` | Testbench false passes, coverage gaps, and code quality |

Each Review Goal covers either a Target's files or a list of paths, and asks
for one of two verdicts:

- **`done`**: the review just has to run. Its findings are advisory. They go
  into the Review Package for you to read, and they do not block Finish.
- **`clean`**: no finding may stay open. The Developer Agent fixes each one,
  then asks the Reviewer to check the fixes again. To skip a finding, the agent
  gives a reason, and the Reviewer decides whether to accept it. If the
  findings are still open after a few rounds, the agent asks you to change
  the Goal or abandon it.

Editing a file after it was reviewed makes that review out of date, so the
review has to run again.

## Working with evidence

Only Booley's own Flows and Specialists can meet a Goal. The Developer Agent
runs them through Goal-bound MCP calls that name the Goal worktree as
`work_dir`. Shell output, or the agent saying "done", does not count, and
CLI runs never record Goal evidence: a `booley flow sim` you run yourself is
just a look. After code changes a Goal may need a fresh run; the Dashboard shows which.

Some Project files decide how evidence is produced: `booley.toml`,
`FUSESOC_IGNORE`, and the Project's `hooks`, `.managed`, `generators`, and
`mcp_tools` directories. Editing one during Goal Mode blocks Finish until you
revert it. The Review Package flags edits to `.core` files, `tests.toml`, and
constraint files (`.sdc`, `.xdc`).

## Changes and human decisions

Once Goal Mode starts, the Developer Agent cannot change the Goals on its own.
If a Goal turns out to be wrong or out of reach, the agent proposes a change
and waits for you. A change can:

- **add** a Goal;
- **relax** one, such as a looser area limit or fewer seeds;
- **retarget** one, such as pointing it at a renamed Test or a different seed;
- **waive** coverage that cannot be reached. The Coverage Analyst can suggest
  waivers, but only you can approve them.

You see the exact Goal before and after the change, then approve or reject it
with a short reason. Staying silent never counts as approval. Approved changes
are listed in the Review Package's Change Log. A changed Goal may need fresh
results before it counts as met; a changed simulation Goal always does.

## Finish and review

Goal Mode finishes when every Goal is met with fresh results and all work is
committed. The Developer Agent writes a Session Summary of what changed and
why, and Booley builds the Review Package: the diff against the starting code,
the final Goals and their evidence, the Change Log, open review findings,
Target and constraint changes, and the Session Summary.

Read the Review Package before deciding what to merge; open `done` findings
are listed there. Completion keeps the Goal Branch and worktree; merge,
publication, and cleanup need separate instructions.

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

## Watching, recovery, and parallel sessions

Follow Goal Mode in the [Dashboard](USAGE.md#the-dashboard). A session waiting for
your answer or a Goal change decision moves to NEEDS YOU. Each Goal's details
show whether it is met, its evidence, its frozen Test Runs, and any baseline
comparison.

The same Goal session can work with your guidance or unattended. If intent is
missing or a Goal cannot be met, the Developer Agent explains what decision it needs.
After a crash or reconnection, type `/booley-goal start`; the agent reloads the
Goal Mode state before continuing. It picks up pending proposals and an
interrupted Finish without asking you twice or finishing twice.

Goal Mode is abandoned only when you say so: tell the Developer Agent, or run
`booley goal abandon` in the Goal worktree. A quiet or stuck agent never
abandons a Goal Mode on its own. The branch and worktree remain.

For parallel work, use one linked worktree and Goal Branch per session. Start
another Developer Agent in another container terminal and follow the same three
steps there. `/booley-goal start` asks which Goal Mode to work on.
Jobs share the Project's [admission caps](CONFIG.md#jobs--concurrency-jobs).

For scripted inspection, `booley goal status` shows this worktree's Goal Mode,
or every active Goal Mode in the Project when this worktree hosts none. In a
worktree without an active Goal Mode, `booley goal status` also shows its most
recently ended Goal Record: Goal id, state, end time, the
failure reason of a failed record, and the Session Summary and Review Package
paths when those files exist.

## Goalsets

A Goalset is a Markdown file under `.booley/goalsets/` that stores a set of
Goals you use often, written in plain prose. When you set up Goals, name a
Goalset and the Developer Agent turns it into concrete Goals. `booley init`
creates `feature.md`, `bugfix.md`, `refactor.md`, and `verification.md` for
you to edit. An optional `default.md` applies to every Goal Mode unless you
say to skip it.
