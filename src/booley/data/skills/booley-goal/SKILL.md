---
name: booley-goal
description: Enter, resume, and finish Booley Goal Mode for RTL work; translate Project Goalsets, gather evidence, and handle human decisions on Goal changes.
---

# Work in Goal Mode

Goal Mode binds this Developer Agent session to one linked worktree and Goal Branch.
Only Booley Flow and Specialist evidence can meet its mandatory Goals.
Use it for human-steered or unattended work inside the Sandbox.

## Enter

1. Use an existing clean linked worktree or create one with
   `booley worktree new <name>` in the Sandbox. Goal Mode cannot enter in the
   main checkout. Preserve existing edits; obtain the human's direction before
   committing or stashing work that is not yours.
2. Remember the absolute worktree root as `work_dir`. Pass it to Booley Flows,
   Specialists, Goal tools, `booley_targets`, and `booley_report`. While a
   Goal Record occupies the Project, calls without it are refused (only
   `booley_targets` may omit it), and Project-defined endpoints must declare
   `work_dir` to run. `booley_status`, `booley_poll`, `booley_cancel`,
   and `bwave` accept no `work_dir`. With several Jobs running, call `booley_poll`
   with no `run_id` and handle whichever of your Jobs finishes first; pass a
   `run_id` only to wait for one specific Job. Never wait for a Job with `sleep`
   loops or log tails.
3. Read the selected `*.md` Goalsets from `goalsets/` in the Main Booley Directory: `BOOLEY_DIR` in the Sandbox, the same directory
   where entry checks `default.md`. Never read them from a Worktree Booley Directory inside `work_dir`; a `default.md` present only there does not
   apply. If `BOOLEY_DIR` is unset or points inside `work_dir`, stop
   and ask the human to restart the session with it set to the
   Main Booley Directory; changing it mid-session does not change
   what entry checks.
   Initialization seeds `feature`, `bugfix`, `refactor`, and `verification`
   create-only. The Project owns their prose and may add others.
   If `default.md` exists, include it unless the human explicitly skips it;
   record `default_skipped=true` and their reason as `skip_reason`. With no
   default file, leave `default_skipped=false`.
4. Resolve the request into a slug, affected Targets, selected Goalsets, and
   any ad-hoc Goals. Use `booley_targets` to discover Targets. A new Target can
   be named at entry but remains unmet until it exists. A spec review names a
   spec file relative to the worktree. Clarify unresolved choices together;
   do not re-ask decisions the human has already made.
5. Translate Goalset prose into concrete `goals` arguments. Use uppercase
   `family` capabilities (`LINT`, `SIM`, `ELAB`, `SYNTH`, `FPGA`, `CYCLE_COUNT`,
   `COVERAGE`, `MUTATION`, `REVIEW`); names render as `SIM:sim_core` or
   `REVIEW:rtl_bugs`, with requirements in parentheses. Expand each
   `<target>` for the Targets that family applies to, and `<spec>` for the
   specification. When a Goalset or the human asks for a particular Test
   Variant, seed, or number of seeds, write it as a Test Run selector; see
   [Goals that name Test Runs](#goals-that-name-test-runs). Set each Goal's `origin` to its Goalset name; ad-hoc Goals
   may omit it. Do not merge duplicates yourself: the tool chooses the stricter
   Goal and reports merges. Show the final Goals to the human before entry; proceed when their
   existing instruction settles the choices.
6. Call `goal_enter(work_dir=..., slug=..., goals=..., goalsets_used=...)`.
   Record the Goalsets used with **`goalsets_used`** (not `goals_used`). Include
   `default` there when used.
   Save the returned record ID and Goal Branch. Read and follow the returned
   rules and warnings. On refusal, resolve the named cause; never retry with
   weakened Goals just to obtain entry.

## Goals that name Test Runs

A Test Run is one Test Variant simulated with one seed, written
`test[+variant][@seed]`. Test Variants are declared per Test in `tests.toml`;
a bare Test name means its default Variant, and no seed means the Target's
Default Seed.

| `family` | Argument | Accepts |
|---|---|---|
| `SIM` | optional `test` | `test`, `test+variant`, either with `@seed`, or either with `@xN` (N Test Runs with different seeds, N from 1 to 1000). Without `test`: every Test of the Target that is not skipped, each as its default Variant at the Default Seed. |
| `CYCLE_COUNT` | `test` | `test`, `test+variant`, either with `@seed`. `@xN` is refused. |
| `COVERAGE` | `tests` | `"all"` or a list of the `SIM` selector forms. |

```json
{"family": "SIM", "target": "sim_fifo", "test": "stress+soak@x20"}
```

- Entry resolves each such Goal to exact Test Runs and freezes them in the
  Goal Record, including the N seeds Booley draws for `@xN`. Later
  `tests.toml` edits to a default Variant or `default_seed` do not change the
  Goal. A seed on an unseeded Target fails entry: report it; do not drop the
  seed yourself.
- A Target, Test or Variant that does not exist yet is pending. Booley
  freezes it when it first exists. Until then the Goal is unmet.
- Only a Test Run with a frozen name and unchanged declared arguments and
  environment counts. `flow sim` on the Target without `test` takes its
  Test Runs from the Target's Goal that covers every Test (`SIM` without
  `test`, or `COVERAGE` with `tests="all"`); with no such Goal it runs the
  default Test Run of each Test. `test="test+variant@xN"` runs the frozen seeds of the Goal with
  that Target, Variant and N. For any other Goal, pass each frozen Test Run
  name from `goal_status` in `test`. Any other Test Run is diagnostic: it
  records no evidence and its failure blocks no Goal.
- A `diagnostic=true` run records nothing even for frozen Test Runs; its
  report names the Goals whose frozen Test Runs ran and failed. Use it to
  debug. A failure it shows is real: fix the cause, then run without
  `diagnostic` to record the outcome. Never report a Goal's Test Runs as
  passing on the strength of a diagnostic run.
- Each frozen Test Run is `passed`, `failed` or `missing` in `goal_status`. A
  simulation Goal is met when all are `passed`; they may pass in separate
  runs. A failure stays until the code changes: do not rerun a failed seed
  hoping for a pass. Reproduce it with the rerun command `goal_status`
  prints for reproduction only, then fix the cause and record evidence through
  a Goal-bound MCP `sim` call with `work_dir`.
- A Cycle Count Goal is measured by a run of its frozen Test Run, which must
  pass. A coverage Goal is measured by one `coverage=true` run of exactly its
  frozen Test Runs and is decided by its coverage floors alone: it can be met
  while one of those Test Runs fails. Report such a failure to the human; do
  not present a met coverage Goal as proof that its Test Runs pass.
- Never edit `tests.toml` to make a frozen Test Run easier: changing or
  removing its Variant leaves the Goal unmet (`goal-test-run-changed`,
  `goal-test-run-missing`) until the human approves a retarget.
- Adding a Goal that names a Test Run is an `add`. Pinning a seed, naming
  another Variant, or raising N is a `retarget`; lowering N is a `relax`. All
  need human approval through `goal_propose_change`, and the Goal's outcomes
  start again. When a diagnostic Test Run fails on a real bug, tell the human
  and offer to propose it as a Goal.

For an unmet reason you do not recognize, read "A Goal that names Test Runs
stays unmet" in the packaged troubleshooting guide:
`python3 -c "from booley.runtime.paths import troubleshooting_path; print(troubleshooting_path())"`.

## Work and evidence

Work only in the Goal worktree and commit on its Goal Branch. Use the relevant
Booley Flows and Specialists through Goal-bound MCP calls, passing `work_dir`
every time. CLI runs never record Goal evidence. Shell output,
Developer Agent prose, and manually edited records cannot meet a Goal. Consult
`goal_status` for unmet or stale Goals and rerun their producers after code
changes. Selected immutable observations govern evidence; cache discrepancies
recover automatically. For missing current evidence, follow the named fields and
producer remedy in `goal_status`, preserving state and evidence files. Rerun ELAB
with `sim mode=elab_only` for its Target; rerun Reviewer with the Goal's category
and focus. Report validation corruption with its diagnostic, or a persistence
failure if a successful rerun still cannot publish. For a bug, reproduce the
reported failure before fixing it; a setup failure or an already passing test is
not evidence of the bug.

Keep Protected Inputs at their entry content: `booley.toml`, `FUSESOC_IGNORE`,
and the Project's `hooks`, `.managed`, `generators`, and `mcp_tools` directories.
Editing one blocks Finish until reverted and discards affected evidence. Never
weaken tests or Targets to meet a Goal. Target and constraint edits appear in
the Review Package.

If work is blocked by missing intent or an unmet Goal, explain the evidence
and the decision needed. Continue independent work when possible. If the cause
is Booley machinery or documentation, use `/booley-feedback` for diagnosis and
private reproducer handling; it creates a redacted export only on explicit
human request and leaves submission to the human. Do not weaken a Goal to hide
an infrastructure failure.

## Review Goals

- **Translate.** Each `REVIEW` Goal needs `review`, `verdict`, and exactly one
  scope: `target` (its RTL or TB files plus dependency closure) or `paths`
  (category-matching files relative to the Goal worktree). Spec review also
  needs `spec`.
- **Discover.** Call `reviewer(category="rtl", focus="bugs")` with the matching
  category and focus only. The Goal supplies scope and spec; `scope`, `spec`
  and `steer` are refused.
- **`done`.** Discovery meets the Goal. Open findings are advisory: list them in
  the Review Package. They need no verify and never block another review.
- **`clean`.** Fix the findings, then call `reviewer(verify=true)`. It rechecks
  only the latest review's open findings and adds none. To waive one, add
  `waive=[["<finding-id>", "<justification>"]]`; the Reviewer decides. Verify
  accepts no other review arguments.
- **One open `clean` review at a time.** Verify reaches only the latest review,
  so resolve each open `clean` review before discovering another focus.
- **Freshness.** Editing reviewed files makes a pass stale. A changed spec, TB
  policy or Target file selection needs new discovery; check `goal_status`
  after verify.
- **Budget.** Verify allows two attempts per category reset and three in total.
  When exhausted, keep the findings and ask the human for a Goal Change or
  abandonment.

## Propose and approve changes

Use `goal_propose_change(operation="create", kind=..., rationale=...)` for
`add`, `relax`, `retarget`, or `waiver`. Show the exact before/after Goal and
why it is needed. Read the exact "goal_key" from entry or status
structured output for an existing Goal; the MCP client must expose `structuredContent`
to the agent. Use that machine address in a change
request and its public name when describing it to the human. A waiver names a screened Coverage Waiver Candidate with
`candidate_id`; the Coverage Analyst cannot approve it. Save `proposal_id` and
use `operation="resume"` with that exact ID after interruption.

Only the human can approve or reject. Use the client's elicitation form when
available; never record approvals yourself when the form is available.
Decline, cancellation, or invalid form content leaves the proposal pending.
For chat fallback, obtain the human's explicit decision and nonblank reason,
then record `operation="approve"` or `"reject"`, the exact `proposal_id`,
`approval_quote` containing their quoted words, and `reason`. Never fabricate
approval or infer it from silence. Inspect the result and refreshed status;
approved changes are recorded in the Change Log and may require new evidence.

## Finish and review

Once all Goals are met with fresh evidence at a clean, committed HEAD, write
`summary`: the Session Summary describing what changed and why, which Flows
and Specialists ran, their results, and remaining uncertainties. Call
`goal_finish` with `work_dir`, the exact `record_id`, a caller-stable UUID
`operation_id`, and `summary`. Save these arguments before calling; retries
use identical IDs, summary, and options to recover the saved result, even
following a later entry. `revalidation_required` needs fresh evidence, a fresh
operation ID, and a new Session Summary.

Read [review.md](review.md) when Finish returns the Review Package or the human
asks to inspect it. Open `done` review findings remain visible and do not
require a second approval to finish. Completion does not authorize merging,
publishing, or deleting the branch or worktree. Outside Stealth, the Session
Summary is committed under `.booley/goals/history/` in the Goal
worktree when that path is Git-trackable and owned by the RTL repository;
otherwise it stays local. Paired Checkout summaries always stay local.

## Resume or abandon

After a crash, reconnection, or context compaction, call
`goal_status(work_dir=..., rules=true)` first. Recover the record ID, Goal
Branch, Goals, evidence, pending proposals, and returned rules before editing.
Resume the same saved proposal or Finish operation; do not create a new record
or edit Goal state files to bypass recovery. If the tool refuses, resolve its
reported identity or protected-input problem.

Abandon only on explicit human instruction. Call `goal_finish` with
`abandon=true`, `instruction_quote` containing that instruction, the exact
`record_id`, and a caller-stable `operation_id`. Retry with identical arguments.
Do not abandon because the Developer Agent is stuck, quiet, or out of context. Abandonment
leaves the worktree and branch for the human to preserve or remove.

## Dashboard

`booley dashboard` inside the Sandbox shows sessions, Goals, and Jobs. It opens
on VS Code folder attachment by default; `[sandbox].dashboard=false` opts out
of the owned task. `booley goal status` inspects local
records; MCP `goal_status` is the worktree-specific evidence authority.
Quiet presence is a presentation hint, not permission to abandon or prune a
worktree. Inspect its record and preserve work before any human-directed cleanup.
