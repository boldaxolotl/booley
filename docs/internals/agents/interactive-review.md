# Requested human review

Use this workflow when a blocked Ticket needs human inspection or interactive
verification without another Developer Agent run. Review and acceptance are
separate: a requested review retains outstanding gates and cannot be approved.

## Enter review

Commit source changes in every Ticket repository, then run:

```bash
booley board request-review SLUG --reason "Finish verification interactively"
booley board review-briefing SLUG
```

The request prepares a complete package before publishing the board transition.
It preserves the Ticket worktree, branch, evidence and original block reason.
If preparation fails, the Ticket stays blocked and the command reports the
failure. Retry the request after correcting it. Generic `move-ticket` cannot
enter review or done.

The package contains structured facts, a terminal briefing and materialized
diffs. Model-enabled preparation also produces an HTML explanation when its
structured explanation validates. `on_success.triage_report: false` produces
the deterministic package without a model call or HTML. Both modes read the
persisted package when briefing; neither substitutes live criterion state.

A legacy Ticket mechanically moved into review without acceptance can use:

```bash
booley board request-review SLUG --repair --reason "Recover unaccepted review"
```

Repair still requires a valid retained Acceptance Basis and worktree. Corrupt
acceptance or missing generation identity is an error, not permission to
manufacture a Criteria Satisfaction Record.

## Verify interactively

Make corrections in the existing Ticket worktree. Use the explicit Ticket
context for every Flow, Specialist and final run report that should count as
Criterion evidence:

```bash
booley board review-exec SLUG -- python -m booley.mcp.submit_run_report --help
```

Replace the command after `--` with the endpoint's normal CLI invocation and
arguments. `review-exec` starts the command in the retained worktree, with
isolated Ticket state/log/Basis bindings and interactive scheduling priority.
An isolated MCP server can be started the same way. It never retargets a shared
server. Ordinary unbound endpoint calls do not record this Ticket's evidence.
A scoped process has a two-hour execution limit; detached Jobs must finish or
be canceled before refreshing or finalizing.

Commit corrections before generating another package. To capture new committed
heads and new verification evidence, run:

```bash
booley board refresh-review SLUG
booley board review-briefing SLUG
```

`prepare-review --force` regenerates the current unaccepted inspection; it
rejects changed heads/evidence and directs you to `refresh-review`. A failed
refresh preserves the previous package generation as historical evidence. A
stale package is never reported as current.

After verification, submit the normal final run report through `review-exec`,
including changed-file justifications and optional-criterion explanations.
Then run:

```bash
booley board finalize-review SLUG
booley board review-briefing SLUG
```

Finalization runs the normal Criteria checks. Unmet gates retain unaccepted
review; passing checks freeze the first Criteria Satisfaction Record and bind a
fresh accepted package. Approval/complete still applies normal merge and
cleanup policy. This workflow does not replace an existing Criteria
Satisfaction Record after further source edits; such edits remain subject to
the existing acceptance protections.

**Hold** leaves the Ticket in review. **Reset** is the existing destructive clean
restart. **Archive** of a review Ticket requires the existing `--force` option.

## Publication and recovery

Each preparation writes a separate package generation. A per-Ticket operation
record fences concurrent mutation, while report agents run outside the board
lock. Publication rechecks the Basis, execution identity, source cleanliness,
heads and evidence digest under the lock. An interruption during publication
retains a pending record; rerun the recorded request/refresh/finalize command to
finish publication. Completion remains fenced until publication is coherent.
Retries reuse the exact timestamp and selected Criteria Satisfaction Record,
rather than replacing write-once acceptance. Changed or corrupt pending inputs
fail closed with their evidence preserved.
