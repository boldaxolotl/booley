# Human review in Goal Mode

Human review happens in the agent session and on its Goal Branch. Goal Mode has
no Ticket Board transition or human override of Finish. The Review Package
exposes the final diff, Goal evidence, Change Log, open review findings, Target
changes, constraint edits, and mandatory Session Summary.

## Inspect and verify

Continue in the linked worktree that owns the Goal Record. `/booley-goal`
guides the workflow; `goal_status(rules=true)` returns the entry rules again
when needed. The human can also inspect a snapshot with:

```bash
booley goal status --long
```

Invoke Booley Flows and Specialists through MCP with explicit `work_dir` for
that worktree. Ordinary CLI Flow calls remain diagnostic and do not bind Goals.
The admitted Run Binding selects the record and specification revisions that
may receive evidence; neither a new session nor a shared server retargets an
already-running Job. Relevant code or Target edits make old evidence stale,
so re-run the affected checks before Finish.

A Reviewer `clean` Goal requires current findings to be fixed or explicitly
waived with justification. A Reviewer `done` Goal completes the review while
retaining open findings for inspection in the Review Package; it does not add a
human approval gate to Finish. Inspect those findings before deciding to merge
with ordinary Git.

## Approve a Goal change

The agent proposes an addition, relaxation, retargeting, or coverage waiver with
`goal_propose_change`. A human approves or rejects it with a reason through the
client's elicitation form, or in chat when the form is unavailable. The fallback
records the human's quoted words and marks the approval as agent-recorded.
The Change Log preserves the decision; approval never substitutes for evidence.

A coverage Waiver Candidate must be screened and bound to the record's Campaign
and source identities. Its Provisional Coverage Verdict is advisory; approving
its Goal Change Proposal promotes the waiver and recalculates strict coverage
under the updated policy. A provisional pass alone cannot meet a Goal.

## Finish, abandon, and recover

Commit the changes and provide a Session Summary before `goal_finish`. Finish
requires every Goal met with fresh evidence at a clean committed HEAD and no
Protected Input violation. It presents the Review Package in chat; HTML is
available on request. Finish does not merge or clean up the worktree. Outside
Stealth it publishes a history summary when the destination is usable; local-only
publication is reported explicitly.

An interrupted Finish or approved change is recovered from its durable captured
intent on retry; changed or corrupt captured inputs fail closed. After an agent
crash, a session in the same worktree may resume with `goal_status(rules=true)`.
The human may abandon from that worktree with `booley goal abandon`; the work,
branch, and abandoned record remain available for inspection.

## Retained Ticket review implementation

The former requested-review workflow, its `board review` / `validate` / `approve`
handlers, immutable inspections, and Acceptance Journal are retained until
Phase 9a removes them. They are not a public workflow: `booley board` prints a
Goal Mode migration pointer and exits 2. Their source lives under
`src/booley/ticket_board/`; the [Ticket Board glossary](../../../src/booley/ticket_board/GLOSSARY.md)
describes those retained concepts. Historical Ticket packages remain evidence,
not instructions to invoke the retired CLI.
