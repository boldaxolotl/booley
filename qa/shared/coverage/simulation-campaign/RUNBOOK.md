# Coverage aggregate Simulation Campaign fixture

This runbook gives the QA agent running the coverage mission the exact steps
for three Simulation Campaign recovery cases in area 6, `cov-retention`
([MISSION.md](../../../missions/coverage/MISSION.md)): refusing to resume an
unfinished coverage collection, recovering a failed Goal acceptance write, and
rejecting a corrupt terminal Result. Fixture setup, the `boundary.so` fault
library and `filesystem.py` live in the [shared coverage
runbook](../RUNBOOK.md); Goal-child rules live in
[GOAL-CHILD.md](../../GOAL-CHILD.md). The validators here are independent
cross-checks over saved evidence; only a real run against the candidate tests
the product.

Code, messages and files still say "Simulation Campaign" and "Coverage
Campaign" (the [Simulation Record](../../../../docs/GLOSSARY.md) rename has not
landed), so this runbook does too.

## Paths

`<asset>` is `qa/shared/coverage/` in the Booley checkout under test. A
coverage run writes, under `<reports>/sim/<N>/targets/sim_toggle/`:

| Path | What it is |
|---|---|
| `campaign/manifest.json` | The Simulation Campaign Manifest; the `--resume-from` input |
| `campaign/work-items/<item>/result.json` | The work item's terminal Result; absent while unfinished |
| `campaign/work-items/<item>/attempts/<attempt>/attempt.json` | One Simulation Attempt |
| `campaign/work-items/<item>/attempts/<attempt>/coverage-campaign/` | That attempt's nested Coverage Campaign |
| `simulation.json` | The Target's compatibility projection |

A coverage Simulation Campaign has exactly one work item, of kind
`coverage_aggregate`.

The validators are Python functions, not CLIs. Call them like this, with the
arguments listed in each case below:

```bash
PYTHONPATH=<asset>/simulation-campaign python3 -c \
  'import sys, validate_aggregate as v; from pathlib import Path; v.validate(*map(Path, sys.argv[1:]))' \
  <args...>
```

## Unfinished-collection resume refusal

1. Use the shared fixture's `sim_toggle` Target and its `half` test. Start
   `booley flow sim --target sim_toggle --test half --coverage --report-dir <owned>`.
2. After native collection has started but before the aggregate Result
   commits, terminate and reap only the owned producer process group.
3. Save the interrupted Simulation Attempt, its nested Coverage Campaign
   directory, the native database identity, the merge inputs, the process
   timeline and the terminal state. Do not edit or delete the interrupted
   records.
4. Run `booley flow sim --resume-from <manifest.json>`, then the same command
   with `--dry-run`. Each must exit 2 before any EDA launch, with a diagnostic
   that names a new `booley flow sim ... --coverage` command (a resume would
   rebuild and rerun every test). The attempt inventory must not change and no
   new invocation directory may appear.
5. Run the command the diagnostic prints, plus `--report-dir <owned>`, as the
   control. It must pass.
6. Write a rejection record (JSON) with `exit_code`, `dry_run_exit_code`,
   `diagnostic`, `attempts_before` and `attempts_after` (the attempt inventory
   as lists), `eda_launches` and `control_exit_code`. Then call
   `validate_aggregate.validate(manifest, interrupted_attempt_json,
   interrupted_result_json, rejection_record)` on saved copies;
   `interrupted_result_json` is the path where the work item's `result.json`
   would be, and it must not exist.
7. Run the shared numeric evaluator over the nested Coverage Campaign that
   the control's Result selects (paths per the shared runbook's "Campaign
   paths"); its storage and integrity checks must pass:

   ```bash
   python3 <asset>/evaluator/measurements.py <nested>/coverage.json \
     --expected <asset>/expected.json --case half \
     --native run:001:half@1=<nested>/native/raw/001-half@1.dat
   ```

## Acceptance recovery

The Goal state is written by the Flow process that the container's shared
HTTP MCP server starts, not by the Codex child: the child reaches that server
through a URL, so a fault injected into the child never reaches the writer.
That is why this case wraps the server.

1. Commit the fixture registration and initialized Project files, then create
   a clean linked worktree. Start a Codex child with `$booley-goal` naming a
   `sim` Goal on `sim_toggle` with its complete registered suite. Follow
   GOAL-CHILD.md, including `work_dir` on every MCP call that takes it.
2. Save the Goal Record and its `logs/acceptance/` evidence. Before any
   collection, archive the Goal Record's `booley_state.json` (the Development
   State) and its evidence ledger.
3. Make sure no other MCP client is attached to the container. With the Goal
   active and before the first collection call, stop the running server
   (`python -m booley.mcp.server --transport http`, log
   `/tmp/booley_mcp_http.log`). Restart the same command under
   `<asset>/faults/filesystem.py`, with `BOOLEY_MCP_MODE=interactive` and with
   `BOOLEY_NESTED_AGENT`, `BOOLEY_NESTED_MCP_TOOLS` and `BOOLEY_MCP_TOOLS`
   unset, as the container start does:

   ```bash
   python3 <asset>/faults/filesystem.py --operation rename \
     --suffix /goals/<record-id>/booley_state.json --owned <main-booley-dir> \
     --gate acceptance --baseline-state <archived-booley_state.json> \
     --control <new-control-dir> --library <owned>/boundary.so \
     --timeout <seconds-longer-than-the-sim-call> \
     -- python -m booley.mcp.server --transport http
   ```

   `--owned` is the resolved Main Booley Directory and `--control` must not
   exist yet. Confirm the server answers before the child calls it.
4. Have the child collect through MCP `sim(work_dir=..., coverage=true)` with
   the complete suite; CLI Flows bind no Goal evidence. The gate lets earlier
   state writes pass. The Campaign publishes every terminal Result, then the
   gate fails the state replacement whose temporary JSON adds a new acceptance
   transaction.
5. After the failed call, send SIGTERM to the faulted server process only, not
   to `filesystem.py`, so the wrapper sees it exit, services the last event and
   checks for the `consumed` marker. Save the paused JSON, the consumed event,
   the failed response and the wrapper's exit status. A missing `consumed`
   marker is a fixture failure. A wrapper `TimeoutError` means the server was
   not stopped: repeat the step.
6. Archive the failed publication, the evidence-ledger intent, transaction and
   evidence records, the failed Development State, the attempt inventory, the
   summary and the incomplete `simulation.json`.
7. Restart the server with the same command and environment, without
   `filesystem.py`. Have the child resume the exact printed Manifest through
   MCP `sim` with its `resume_from` argument, absolute `work_dir`, and the same
   Goal Record.
8. Require: the same intent recorded or verified, exactly one matching
   transaction directory, exactly one selection of its transaction ID in the
   Development State, no contradictory duplicate records, no new Simulation
   Attempt, and a final `simulation.json` with `complete: true`.
9. Call `validate_recovery.validate_acceptance_recovery(manifest,
   [result_json, ...], intent, transaction, evidence_root, archived_state,
   failed_state, recovered_state, simulation_json)` on the saved copies. It
   checks each record's bytes, digest, ordinal, sequence, role, envelope
   binding and the absence of extra matching evidence.

## Corrupt terminal rejection

1. Archive a byte-exact valid `result.json`, then change one byte in its live
   copy. Save the valid and corrupt digests, all other terminal records, the
   attempt inventory and a timestamped process sample.
2. Have the child resume the exact Manifest through MCP `sim` with absolute
   `work_dir`. It must exit 2 with a diagnostic naming Simulation Result
   integrity, an unchanged attempt inventory, and no EDA process launch. Save
   the rejected corrupt bytes even when the diagnostic differs.
3. Restore only the archived Result bytes, check their digest, and resume the
   same Manifest through MCP again as the valid control. The completed work
   item must not rerun. Save the final summary and `simulation.json`.
4. Build the rejection record from the captured exit codes, diagnostic,
   process samples, inventories and digests only; it is an index, not
   evidence. Fields: `exit_code`, `diagnostic`, `attempts_before`,
   `attempts_after`, `attempts_after_restore`, `eda_processes_started` (a
   list), `control_exit_code`, `control_complete`, `valid_sha256`,
   `corrupt_sha256` and `restored_sha256`.
5. Call `validate_recovery.validate_corrupt_terminal(archived_result,
   restored_result, rejection_record)`.

## Resources and cleanup

Before the first mutation, list in `resources.md` every run-owned report root,
nested Coverage Campaign, Goal Record, evidence ledger directory, fault control
directory, corrupt copy, process group and fixture registration. Recovery
always saves the observed failure first, restores only owned bytes or paths,
then runs the valid control in the same run, whether or not the negative case
behaved as expected.

After capture, finish or explicitly abandon the Goal, reap all owned
producers, and remove only run-owned report and runtime state, Goal Record
state, injection environment, control directories, corrupt working copies and
copied fixture registration. Keep the interrupted, failed, corrupt and
restored evidence copies for findings. Confirm the shared fixture and pinned
sources are unchanged and mark each `resources.md` row released. The three
cases reuse one terminal aggregate; if time runs out, log the remaining cases
as skipped.
