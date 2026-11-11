# Measure commits against Target Metrics

Status: accepted (2026-11-11)

Decided in [#3045](https://github.com/boldaxolotl/booley/issues/3045)
(Metric-Driven Development). Once Goal Mode is removed (0.5.0) this ADR:

- **supersedes [ADR 0067](0067-replace-ticket-mode-with-goal-mode.md)**: Goal
  Mode, its Goal Records, Goal changes and Review Package are removed, and
  acceptance becomes a **Measured Commit**;
- **supersedes the acceptance gate of
  [ADR 0074](0074-make-the-review-goal-the-whole-review-contract.md)**: a
  review is no longer part of what makes work acceptable. Its review contract
  is replaced by [ADR 0091](0091-review-commits-outside-measurement.md);
- **amends [ADR 0068](0068-booley-dashboard.md)**: the Dashboard's Goal status
  summary, Goal views and Goal-change proposals are replaced by one
  session-row line about Measured Commits;
- **amends [ADR 0072](0072-declare-test-variants-and-seeds-instead-of-run-overrides.md)**:
  its Goal rules (Goals naming Test Runs, multi-seed `@xN` Goals, evidence
  matching) go with Goal Mode. Test Variants, seeds and Test Runs stand. A
  per-test metric names exactly one Test Run;
- **amends [ADR 0083](0083-position-booley-as-an-agentic-development-environment.md)**:
  the engineer judges a Measurement Report, and a Review Report when reviews
  are declared, where that ADR says Goal status and Review Package. The
  positioning stands.

It **supersedes
[ADR 0076](0076-reuse-results-at-committed-states-through-a-project-result-cache.md)**
now: the Result Cache was never finished, and its remaining code is removed
with Goal Mode.

## Context

Three planned features each carried part of one job, "measure a commit and
decide whether it is acceptable":

- Goal Mode (ADR 0067) and its planned successor, Sign-off (#2811), ask the
  user to approve a list of criteria per task before any work starts.
- The Result Cache (ADR 0076) guards a whole-tree key against wrong hits on a
  mutable working tree: dirty trees, ignored files, overlapping runs, before
  and after snapshots, sampled re-runs.
- `booley ci` (#2593) is a third entry point to the same evaluation.

Goal Mode also carries a large evidence apparatus (the Criteria catalog,
freshness, the evidence ledger) whose purpose is to prove that a working tree
that keeps changing still matches its recorded results.

## Decision

Metrics belong to **Targets**, not to sessions or tasks, and Booley measures
**commits**, never a working tree.

- **Target Metrics.** A Project declares metrics per Target and Booley Flow in
  `metrics.toml` in the Booley Directory. A metric with a bound is
  **enforced**; a metric listed as **tracked** is reported and its value never
  blocks. Bounds are absolute only: no ratchet, no tolerance, no bound
  relative to another commit. Noise is handled by choosing the bound. Only
  listed Targets are measured, and a Flow runs for a Target only when that
  Target declares metrics for it. Adoption is optional: a Project that
  declares nothing pays nothing.
- **One command.** `booley measure [<sha>]` measures a commit and records, per
  declared metric, whether it is met. It is the only place a value is
  compared with a bound: Booley Flows return typed metrics and compare
  nothing. The rules that apply are the measured commit's own, so a waiver is
  an ordinary edit to `metrics.toml`.
- **A clean checkout.** The commit is exported whole, without `.git`, into
  scratch space outside the live tree, and every Flow reads only that
  checkout. Booley treats the checkout like a linked worktree's **Snapshot**:
  inputs come from the checkout, and shared state (Job admission, the Resource
  History, Job records, the stored runs) stays in the **Main Booley
  Directory**. Freshness tracking is not needed, because a commit's bytes do
  not change.
- **Runs are keyed by Git tree.** A stored **Measurement Run** belongs to the
  tree it measured, not to the commit sha. A rewrite that leaves the files
  identical (an amended message, a squash or rebase with the same content)
  keeps the commit Measured. With a **Versioned Booley Directory** the rules
  are not in the RTL repository's tree, so the key is the pair of trees. The
  toolchain is recorded with each run and is not part of the key.
- **Tree facts are stored, the comparison is recomputed.** A run stores what
  follows from the tree alone: values, verdicts, the effective rules, the
  toolchain. Whatever depends on the commit and its **Base** (changed files,
  rule changes, deltas, warnings) is recomputed each time the run is shown.
- **The Base never decides a verdict.** A stored run of the Base is optional
  and never started automatically. It adds deltas and warnings, and changes
  neither the exit status nor whether the commit is Measured.
- **Fail closed.** A value that could not be produced is an error, and any
  error blocks, for tracked metrics too. A **Measured Commit** is one whose
  tree's latest full run met every enforced metric with no error.
- **Loosened rules are shown, not refused.** The report compares the rules
  with the Base's and marks a loosened rule, or any change to the declared
  tests, prominently. It never blocks on it.
- **One parent Job.** A running `booley measure` is one Job that holds no
  admission of its own. Each declared Flow runs as a child that is admitted
  like any other Flow Job, so measurement competes fairly with interactive
  work and needs no scheduler of its own.
- **Local results are advisory.** The Developer Agent can write to the store
  and to the repository from the Sandbox, so a local Measured Commit stops
  honest mistakes, not a hostile agent. Measuring again in CI, which has no
  live tree, is the trust boundary. Hosted CI is designed separately.
- **Reviews stay outside** (ADR 0091). `booley measure` is purely numeric, so
  a local verdict and a CI verdict mean the same thing.
- **Removal.** Goal Mode, the Criteria package and the Result Cache code are
  removed in 0.5.0, after this feature ships. Until then Goal Mode keeps
  working: typed Flow metrics are added beside what Flows return today.

## Considered Options

- **Sign-off with a per-task criteria list (#2811).** It keeps Goal Mode's
  up-front approval step under a new name, and "sign-off" already means
  tape-out sign-off (STA, DRC, LVS) in chip design.
- **The Result Cache (ADR 0076).** Reusing results on a working tree needs
  the snapshot, overlap and sampled re-run machinery to stay correct.
  Measuring only commits, keyed by tree, gives the reuse that matters without
  it.
- **`booley ci` as a third entry point (#2593).** With one command that
  measures a commit, CI runs that command.
- **Keying runs by commit sha.** Every message amend, squash and
  same-content rebase would need a new run.
- **A real `git worktree` for the checkout.** It shares Git metadata with the
  live repository, which the Developer Agent can write, and it does not cover
  submodules at their pinned commits or a Versioned Booley Directory.
- **A separate state-directory override**, so the Booley Directory selection
  could point at the checkout while state went elsewhere. It adds a second
  resolution rule for every caller. Treating the checkout like a Snapshot
  reuses the split ADR 0082 already made. The cost is that a missed
  checkout-explicit read would take an input from the live tree, so that
  case is guarded by a test whose Main Booley Directory differs from the
  commit.
- **Relative bounds and tolerances.** A bound relative to the Base makes the
  verdict depend on a second run and its toolchain. A task's relative target
  is given to the agent in prose and read from the delta column.

## Consequences

- A commit must exist before anything is accepted. The inner loop stays plain
  `booley flow`; `booley measure` is an end-of-task check.
- `metrics.toml`, `tests.toml` and `booley.toml` must be versioned. A Booley
  Directory outside any repository cannot be measured.
- A file that needs a Git filter (Git LFS included) cannot be exported, so
  such a Project cannot be measured in v1. A generator or Pre-Sim Command
  that calls Git fails in the checkout with a clear error.
- The Sandbox cannot hide the live tree from a subprocess, so a deliberate
  absolute-path read of it is still possible locally.
- A metric that drifts inside a slack bound goes unremarked unless a run of
  the Base exists.
- Interactive Mode loses its evidence recording and staleness tracking
  together with the Criteria package.
- The glossary gains **Metric-Driven Development**, **Target Metrics**,
  **Measurement Run**, **Measured Commit**, **Measurement Report** and
  **Base**. The Goal Mode vocabulary retires with Goal Mode.
