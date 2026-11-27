# Coverage aggregate Simulation Record fixture

This runbook gives the QA agent running the coverage mission the exact steps
for two Simulation Record recovery cases in area 6, `cov-retention`
([MISSION.md](../../../missions/coverage/MISSION.md)): refusing to resume an
unfinished coverage collection and rejecting a corrupt terminal Result.
Fixture setup, the `boundary.so` fault library and `filesystem.py` live in the
[shared coverage runbook](../RUNBOOK.md). The validators here are independent
cross-checks over saved evidence; only a real run against the candidate tests
the product.

## Paths

`<asset>` is `qa/shared/coverage/` in the Booley checkout under test. A
coverage run writes, under `<reports>/sim/<N>/targets/sim_toggle/`:

| Path | What it is |
|---|---|
| `record/manifest.json` | The Simulation Record Manifest; the `--resume-from` input |
| `record/work-items/<item>/result.json` | The work item's terminal Result; absent while unfinished |
| `record/work-items/<item>/attempts/<attempt>/attempt.json` | One Simulation Attempt |
| `record/work-items/<item>/attempts/<attempt>/coverage-record/` | That attempt's nested Coverage Record |
| `simulation.json` | The Target's compatibility projection |

A coverage Simulation Record has exactly one work item, of kind
`coverage_aggregate`.

The validators are Python functions, not CLIs. Call them like this, with the
arguments listed in each case below:

```bash
PYTHONPATH=<asset>/simulation-record python3 -c \
  'import sys, validate_aggregate as v; from pathlib import Path; v.validate(*map(Path, sys.argv[1:]))' \
  <args...>
```

## Unfinished-collection resume refusal

1. Use the shared fixture's `sim_toggle` Target and its `half` test. Start
   `booley flow sim --target sim_toggle --test half --coverage --report-dir <owned>`.
2. After native collection has started but before the aggregate Result
   commits, terminate and reap only the owned producer process group.
3. Save the interrupted Simulation Attempt, its nested Coverage Record
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
7. Run the shared numeric evaluator over the nested Coverage Record that
   the control's Result selects (paths per the shared runbook's "Coverage Record
   paths"); its storage and integrity checks must pass:

   ```bash
   python3 <asset>/evaluator/measurements.py <nested>/coverage.json \
     --expected <asset>/expected.json --case half \
     --native run:001:half@1=<nested>/native/raw/001-half@1.dat
   ```

## Corrupt terminal rejection

1. Archive a byte-exact valid `result.json`, then change one byte in a field
   resume validates in its live copy: the grade (`"pass"` → `"Pass"`),
   `attempt_id` or `manifest_sha256`. Evidence is trusted on read (ADR 0064),
   so a well-formed change to an unvalidated field, such as a stored digest, is
   accepted and proves nothing. Save the valid and corrupt digests, all other
   terminal records, the attempt inventory and a timestamped process sample.
2. Resume the exact Manifest with `booley flow sim --resume-from
   <manifest.json>`. It must exit 2 with a diagnostic naming the Simulation
   Result, an unchanged attempt inventory, and no EDA process launch. Save
   the rejected corrupt bytes even when the diagnostic differs.
3. Restore only the archived Result bytes, check their digest, and resume the
   same Manifest with the same command as the valid control. It must exit 2 with
   the coverage refusal that names a new `booley flow sim --coverage` run, with
   no EDA process launch and an unchanged attempt inventory. Save the diagnostic
   and `simulation.json`.
4. Build the rejection record from the captured exit codes, diagnostic,
   process samples, inventories and digests only; it is an index, not
   evidence. Fields: `exit_code`, `diagnostic`, `attempts_before`,
   `attempts_after`, `attempts_after_restore`, `eda_processes_started` (a
   list), `control_exit_code`, `control_diagnostic`, `valid_sha256`,
   `corrupt_sha256` and `restored_sha256`.
5. Call `validate_recovery.validate_corrupt_terminal(archived_result,
   restored_result, rejection_record)`.

## Resources and cleanup

Before the first mutation, list in `resources.md` every run-owned report root,
nested Coverage Record, fault control directory, corrupt copy, process group and
fixture registration. Recovery always saves the observed failure first, restores
only owned bytes or paths, then runs the valid control in the same run, whether
or not the negative case behaved as expected.

After capture, reap all owned producers, and remove only run-owned report and
runtime state, injection environment, control directories, corrupt working
copies and copied fixture registration. Keep the interrupted, failed, corrupt
and restored evidence copies for findings. Confirm the shared fixture and pinned
sources are unchanged and mark each `resources.md` row released. The two cases
reuse one terminal aggregate; if time runs out, log the remaining cases as
skipped.
