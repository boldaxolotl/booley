---
name: booley-goal
description: Enter, resume, and finish Booley Goal Mode for RTL work; translate Project Goalsets, gather evidence, and handle human decisions on Goal changes.
---

# Work in Goal Mode

Goal Mode binds this agent session to one linked worktree and Goal Branch.
Only Booley Flow and Specialist evidence can meet its mandatory Goals.
Use it for human-steered or unattended work inside the Sandbox.

## Enter

1. Use an existing clean linked worktree or create one with
   `booley worktree new <name>` in the Sandbox. Goal Mode cannot enter in the
   main checkout. Preserve existing edits; obtain the human's direction before
   committing or stashing work that is not yours.
2. Remember the absolute worktree root as `work_dir`. Pass `work_dir` on
   every Booley call, including Flow, Specialist, and Goal calls.
3. Read the selected `*.md` Goalsets under the resolved Project directory's
   `goalsets/`. Initialization seeds `feature`, `bugfix`, `refactor`, and
   `verification` create-only. The Project owns their prose and may add others.
   If `default.md` exists, include it unless the human explicitly skips it;
   record `default_skipped=true` and their reason as `skip_reason`. With no
   default file, leave `default_skipped=false`.
4. Resolve the request into a slug, affected Targets, selected Goalsets, and
   any ad-hoc Goals. Use `booley_targets` to discover Targets. A new Target can
   be named at entry but remains unmet until it exists. A spec review names a
   spec file relative to the worktree. Clarify unresolved choices together;
   do not re-ask decisions the human has already made.
5. Translate Goalset prose into concrete `goals` arguments. Expand each
   `<target>` for the Targets that family applies to, and `<spec>` for the
   specification. Set each Goal's `origin` to its Goalset name; ad-hoc Goals
   may omit it. Do not merge duplicates yourself: the tool chooses the stricter
   Goal and reports merges. Show the final Goals to the human before entry; proceed when their
   existing instruction settles the choices.
6. Call `goal_enter(work_dir=..., slug=..., goals=..., goalsets_used=...)`.
   Record the Goalsets used with **`goalsets_used`** (not `goals_used`). Include
   `default` there when used.
   Save the returned record ID and Goal Branch. Read and follow the returned
   rules and warnings. On refusal, resolve the named cause; never retry with
   weakened Goals just to obtain entry.

## Work and evidence

Work only in the Goal worktree and commit on its Goal Branch. Use the relevant
Booley Flows and Specialists, passing `work_dir` every time. Shell output,
agent prose, and manually edited records cannot meet a Goal. Consult
`goal_status` for unmet or stale Goals and rerun their producers after code
changes. For a bug, reproduce the reported failure before fixing it; a setup
failure or an already passing test is not evidence of the bug.

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

## Propose and approve changes

Use `goal_propose_change(operation="create", kind=..., rationale=...)` for
`add`, `relax`, `retarget`, or `waiver`. Show the exact before/after Goal and
why it is needed. A waiver names a screened Coverage Waiver Candidate with
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
Summary is committed under `.booley_project/goals/history/` in the Goal
worktree when that path is Git-trackable; otherwise it stays local.

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
Do not abandon because the agent is stuck, quiet, or out of context. Abandonment
leaves the worktree and branch for the human to preserve or remove.

## Dashboard

`booley dashboard` inside the Sandbox shows sessions, Goals, and Jobs. It opens
on VS Code folder attachment by default; `[sandbox].dashboard=false` opts out
of the owned task. `booley goal status` inspects local
records; MCP `goal_status` is the worktree-specific evidence authority.
Quiet presence is a presentation hint, not permission to abandon or prune a
worktree. Inspect its record and preserve work before any human-directed cleanup.
