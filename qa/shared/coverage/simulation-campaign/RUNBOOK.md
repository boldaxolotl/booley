# Coverage aggregate Simulation Campaign fixture

This fixture backs three recovery cases in the coverage mission's retention
area: unfinished-collection resume refusal, acceptance recovery, and corrupt
terminal rejection. Unit or fixture validation says nothing about the product;
only a real run does.

Use the existing `sim_toggle` Target and exact `half` test from the shared
coverage fixture. Start one public `booley flow sim --coverage` Simulation
Campaign with an explicit report root. After native collection has started but
before the aggregate result commits, terminate and reap only the owned producer
process group. Preserve the interrupted Simulation Attempt, its distinct nested
Coverage Campaign directory, native database identity, merge inputs, process
timeline, and terminal state. Do not edit or delete the interrupted records.

Resume from the exact printed Simulation Campaign Manifest and, separately,
with `--dry-run`. Require exit 2 from both, with guidance naming a new
`booley flow sim --coverage` run, an unchanged Simulation Attempt inventory, no
new invocation directory, and no EDA launch. Then run the fresh `--coverage`
command as the control and require it to pass.

Run `validate_aggregate.py` against retained copies (the Manifest, the
interrupted attempt and result paths, and a rejection record with `exit_code`,
`dry_run_exit_code`, `diagnostic`, `attempts_before`, `attempts_after`,
`eda_launches`, and `control_exit_code`) as an independent integrity
cross-check. Also run the existing shared coverage evaluator over the selected
nested Coverage Campaign and require its ordinary storage/integrity checks to
pass. The validator is not a substitute for raw native data, exact argv, process
timeline, or immutable product artifacts.

## Acceptance recovery

Commit fixture registration and initialized Project files before creating a
linked worktree. Start a Codex child with `$booley-goal` naming a `sim` Goal on
`sim_toggle`; use its complete registered suite and pass `work_dir` on every
MCP call. Goal evidence uses purpose `goal_evidence`, subject `goal` and an
identity with record ID, Goal keys and spec revisions. Preserve the Goal Record
and its `logs/acceptance/` evidence. Before execution, archive its Development
State and evidence ledger.

The Goal state is written by the Flow process that the container's shared HTTP
MCP server starts, not by the Codex child. The child reaches that server
through a URL, so an injection on the child never reaches the writer. Run this
step with no other MCP client attached to the container. With the Goal active
and before the first collection call, stop the running server
(`python -m booley.mcp.server --transport http`; its log is
`/tmp/booley_mcp_http.log`) and start the same command under `../faults/filesystem.py` from the shared RUNBOOK fault
table, with `BOOLEY_MCP_MODE=interactive` and without `BOOLEY_NESTED_AGENT`,
`BOOLEY_NESTED_MCP_TOOLS` or `BOOLEY_MCP_TOOLS` (as the container start does).
Use `--operation rename`, the exact captured
`/goals/<record-id>/booley_state.json` suffix, the resolved control Project as
`--owned`, `--gate acceptance`, the archived state as `--baseline-state`, a
fresh control directory, the compiled `boundary.so` and a `--timeout` longer
than the sim call. Confirm the server answers before the child calls it.

The acceptance gate lets earlier state writes pass. The Campaign publishes all
terminal Results before the gate fails the Goal state replacement whose random
temporary JSON adds a new transaction. The child collects through MCP
`sim(work_dir=..., coverage=true)` using the complete suite; CLI Flows bind no
Goal evidence. After the failed call, send SIGTERM to the faulted server
process only (not to the wrapper), so `filesystem.py` sees it exit, services
the last event and checks `consumed`. Save the paused JSON, the consumed event,
the failed response and the wrapper's exit status. A missing `consumed` marker
is a fixture failure; a wrapper `TimeoutError` means the server was not stopped
and the step must be repeated.

Archive the failed publication, evidence-ledger intent, transaction, evidence records,
Development State, attempt inventory, summary, and incomplete compatibility
projection. Start the server again the normal way (no injection) and resume the exact
printed Manifest through the child's MCP `sim` call with absolute `work_dir`
and the same Goal Record, using its `resume_from` argument. Require
record-or-verify recovery of the same intent, exactly one
matching transaction directory and exactly one selection of its transaction ID
in Development State, no contradictory duplicate records, no new Simulation
Attempt, and final `simulation.json` with `complete:true`. Run
`validate_recovery.py` over the exact Manifest and terminal Results, frozen
Acceptance Intent, exact transaction commit, complete V2 evidence root,
byte-archived/failed/recovered Development State files, and final projection.
The validator authenticates every record's bytes, digest, ordinal, sequence,
role, envelope binding, and absence of extra matching evidence; it remains an
independent cross-check rather than replacement evidence.

## Corrupt terminal rejection

After archiving a byte-exact valid `result.json`, mutate one owned byte in its
live copy. Preserve its valid and corrupt digests, all other terminal records,
the before-attempt inventory, and a timestamped process sample. Resume the exact
Manifest through the child's MCP `sim` with absolute `work_dir`. Require exit 2
with a diagnostic that names Simulation Result
integrity, unchanged attempt inventory, and no EDA process launch. Preserve the
rejected corrupt bytes even when the diagnostic differs.

Restore only the archived Result bytes, authenticate their exact digest, and
resume the same Manifest through that MCP route as the valid control. The
completed work item must not rerun. Preserve the successful validation/recovery evidence, final summary, and
compatibility projection. Build the small rejection record consumed by
`validate_recovery.py` only from the captured public exit, diagnostic, process
samples, inventories, and digests; the record is an index, not substitute
evidence.

## Resource recovery and cleanup

List every run-owned report root, nested Coverage Campaign, Goal Record state,
evidence ledger directory, process-local rename fault, corrupt copy, process group,
and fixture registration in `resources.md` before the first mutation. Recovery
always saves the observed failure before restoring only owned bytes or paths,
then runs the valid control in the same run. It never depends on the negative
case having behaved as expected.

After capture, finish or explicitly abandon the Goal and reap all owned producers and remove only run-owned report/runtime
state, Goal Record state, injection environment and control directories, corrupt working copies, and copied fixture
registration. Keep the interrupted, failed, corrupt and restored evidence copies
for findings. Confirm the shared fixture and pinned sources are unchanged and
mark each `resources.md` row released. These cases reuse the terminal aggregate;
if time runs out, log the remaining cases as skipped.
