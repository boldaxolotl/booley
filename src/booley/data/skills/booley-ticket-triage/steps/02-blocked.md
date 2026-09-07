# Step 2: Blocked Tickets

For each `status: "blocked"` ticket:

## Fast path

Run exactly once:

```bash
booley board blocked-briefing $SLUG
```

When it succeeds, present the prepared dossier and proceed to **Log The
Diagnosis**. The separate post-developer report agent has already read the
ticket, transitions, blocked log, state, run log, developer failures, Flow and
Specialist reports, and worktree status. Do not repeat that evidence gathering.

If the dossier is missing or stale, use the manual Gather Context and Diagnosis
fallback below. This fallback exists for old tickets and report-generation
failures; do not run a new report agent while the user waits.

## 1. Gather Context

- Read ticket from `blocked/<file>`.
- Read `logs/<slug>/human-logs/transitions.log`; the last transition line has the board-level block reason.
- Read `logs/<slug>/blocked.md` if present; it may contain agent questions or the developer exception.
- Run `python -m booley.ticket_board validate-logs $SLUG`.
- Resolve the ticket and log paths to absolute paths for clickable local links.
- Before listing successful checks or asking the user what to do, present a
  **Blocked by** section. It must:
  - state the board-level block reason from the latest transition explicitly;
    never make the user infer it from a general run summary
  - list every independent condition that currently prevents the ticket from
    completing, one per numbered item, naming the affected criterion or Booley Flow
  - distinguish actual blockers from passing checks, stale-but-fixed findings,
    and other warnings; do not describe the latter as blockers
  - link the evidence as `[blocked.md](/absolute/path/to/blocked.md)` when the
    file exists, and say `Escalation log: not present` when it does not
- Then present the blocked stage, validate-logs result, relevant context, and
  any developer questions. Link the blocked ticket file as well.

Use this minimum shape (add detail when useful):

```markdown
### <slug>

**Blocked by:**

1. **<criterion, Flow, or Specialist> — <plain-language reason>.** <evidence and impact>
2. **<criterion, Flow, or Specialist> — <plain-language reason>.** <evidence and impact>

**Board reason:** <latest transition reason>
**Blocked stage:** <stage>
**Evidence:** [blocked.md](/absolute/path/to/blocked.md) · [ticket](/absolute/path/to/ticket.md)
**Passing / non-blocking:** <brief list>
**Recommended action:** <one action and why>
```

Do not open with the passing checks. If several failures exist, do not collapse
them into a vague label such as "workflow defects". If the recorded board
reason conflicts with the diagnosed blockers, call out that conflict rather
than silently replacing the recorded reason.

## 2. Diagnosis

- Read `logs/<slug>/.runtime/booley_state.json`: criteria met/unmet, `_blocked_reason`, and `timeline`.
- Read `logs/<slug>/human-logs/run.log` (last ~200 lines) for execution context.
- Read Booley Flow reports from `logs/<slug>/.runtime/flow-reports/` and
  Specialist reports from `logs/<slug>/.runtime/mcp-tool-reports/`. Both use
  `<name>/<N>/report.json` (highest `N` is latest) plus a flat `<name>.json`
  compatibility copy.
- Read `logs/<slug>/.runtime/developer/*.jsonl` and any `*.crash.json` if transitions/logs mention agent crash, timeout, max turns, API error, or orphaning.
- Inspect the worktree branch when code was modified:
  - `git status --short --branch`
  - `git log --oneline -8 --decorate`
  - `git diff <base>..HEAD --stat`

Source-of-truth rules:
- mandatory criteria unmet -> ticket/code or verification issue unless caused by a Flow, Specialist, or EDA-tool failure
- `_blocked_reason` present -> agent intentionally blocked
- missing/empty `booley_state.json` -> harness/developer issue
- developer transcript/API/container failure -> infrastructure or harness issue

## 3. Classify The Failure

- **Harness issue**: bug in harness scripts, developer logic, prompt, state transition, or criteria handling.
- **Infrastructure issue**: tooling, environment, Docker, EDA crash, OOM, API/server error, timeout.
- **Ticket/code issue**: genuine problem with implementation, testbench, spec, or acceptance criteria.

For **harness** or **infrastructure** issues: ask whether to **investigate now** or **defer**.

When investigation confirms that Booley itself is defective (rather than the
project, ticket, local configuration, EDA installation, or transient service),
mark it for the default `/booley-feedback` handoff after the immediate ticket
resolution. The incident log is ticket-local evidence, not a substitute for
that maintainer-facing workflow.

For **ticket/code** issues: proceed to resolution options.

## 4. Log The Diagnosis

Run `python -m booley.ticket_board log-incident $SLUG --type <type> --step <step> --description "<what happened>"` (all three flags are required; add `--resolution` if known). Fold into the description:
- classification
- evidence files read
- root cause or best current hypothesis
- recommended next action

## 5. Resolution Options

Three distinct recovery paths — do NOT conflate them:

- **Unblock (default retry)**: `unblock` moves the ticket blocked→queue, **preserves** the worktree/branch/logs, and appends your feedback to `blocked.md` so the developer reads it on resume. This is the retry-with-feedback path — use it whenever you have diagnosis or answers to pass forward.
- **Reset (clean execution retry)**: `reset` archives the current run artifacts,
  recreates the worktree and branch from the same immutable Acceptance Basis,
  and re-runs from the beginning. It takes **no feedback** (any feedback you
  compose is lost). Use only when the worktree is known-bad and a fresh
  execution against the original basis is required.
- **Return to draft (new authoring generation)**: this is required when
  `blocked_reason` is `acceptance-input-change-required`, or whenever the
  Acceptance Basis inputs must change. It preserves the old Acceptance Basis
  and worktrees for audit, archives the current run history under
  `logs/<slug>/runs/<NNN>/`, and opens a new generation-qualified authoring
  workspace from the committed destination refs. Correct the authoring inputs,
  validate the draft, and enqueue it to publish a new immutable Acceptance
  Basis. `unblock` and `reset` retain the original basis and are rejected for
  this block reason.
- **Archive**: give up on this ticket.
- **Skip**: leave as-is.

Notes:
- `blocked.md` is an append-only chronological log — read it from top to bottom for escalation history (blocks, failures, crashes, human responses).
- Feedback lives in `blocked.md`; `unblock --feedback` appends to it. A `reset`
  marks earlier entries as prior-run history.
- `return-to-draft` archives `blocked.md`, human logs, and runtime evidence with
  the previous run. It strips the prior `acceptance_basis`, `created`,
  `feature_branch`, `steps_completed`, and `stage` fields from the editable
  draft; it does not mutate the archived Ticket or old basis.

## 6. Collect Feedback

For an unblock retry:
- Compose feedback incorporating diagnosis and any question answers from `blocked.md`.
- Feedback should name concrete criterion, Flow, or Specialist reports, e.g. `sim_pass_tb_top_module_testname`, `sim`, `reviewer`.
- Confirm feedback with user before proceeding.

## 7. Execute

- **Unblock (retry with feedback)**: `python -m booley.ticket_board unblock $SLUG --feedback "..."` — then print: `Unblocked -> queued. Run ticket execution to resume.`
- **Reset (clean execution retry)**: confirm the correction reason, then run
  `python -m booley.ticket_board reset $SLUG --reason "<correction reason>"`
  (or `booley board reset $SLUG --reason "<correction reason>"`) — then print:
  `Reset -> queued. Run ticket execution from a clean state.`
- **Return to draft (Acceptance-input correction)**: confirm the correction
  with the user, then perform this sequence in order:

  1. Run `python -m booley.ticket_board return-to-draft "$SLUG"`.
  2. Correct the authoring filesets and any other Acceptance Basis inputs in
     the returned `outer_worktree` and `project_worktree` (when present), and
     update the draft Ticket when its authored fields must change.
  3. Run
     `python -m booley.ticket_board validate-ticket "$DRAFT_PATH" --check-git`
     and fix every error before continuing. `$DRAFT_PATH` is the moved Ticket's
     absolute path under the Project's `tickets/board/drafts/` directory.
  4. Run `python -m booley.ticket_board enqueue "$SLUG"` to publish the new
     Acceptance Basis, then print:
     `Returned to draft -> corrected authoring generation published and queued.`

  Do not use the main checkout for the authoring corrections: use the worktree
  paths emitted as JSON by `return-to-draft`. This is a new authoring generation,
  not an ordinary retry of the blocked execution.
- **Archive**: Confirm first, then `python -m booley.ticket_board archive $SLUG --force` (or `booley board archive $SLUG --force`). `--force` is required because the ticket is not `done`; archive also removes the worktree and branch itself, so no manual `git branch -D` is needed.
- **Skip**: leave as-is.

User can say "skip" to leave unchanged.

## 8. Route Confirmed Booley Bugs

After the ticket is unblocked, reset, returned to draft, archived, or skipped, invoke
`/booley-feedback` for every confirmed Booley-side bug or docs contradiction
found during this diagnosis. Pass it the reproduction, observed/expected
behavior, component, source verification, and relevant report/log attachment.
Do this by default; do not require the user to request bug reporting first.
Follow the feedback skill's own rules exactly, especially its explicit approval
gate before anything leaves the machine.
