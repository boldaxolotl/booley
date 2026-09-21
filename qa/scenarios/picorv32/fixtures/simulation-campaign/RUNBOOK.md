# PicoRV32 Simulation Campaign QA fixture

These Checks are registered but remain pending until a separately authorized
Configured Scenario Run captures their product evidence. Automated regressions
and this fixture's validator protect the public contracts; they are not a
substitute for a Scenario Run and do not mark Checks 1–7 complete.

Use this owned Icarus fixture only inside a run-owned copy of the PicoRV32
Project. Do not edit the pinned upstream source. Copy `campaign.core` and
`campaign_tb.sv` into one declared core root and merge the `sim_campaign`
table from `tests.toml` into the Project's active test catalog. Record the
resulting paths and SHA-256 digests before running any Check.

All commands below must use an explicit run-owned report root. Preserve exact
argv, stdout/stderr, exit status, the printed manifest path, directory listings,
and copies plus SHA-256 digests of every referenced JSON document. The
`validate_campaign.py` helper is a read-only structural cross-check for exact
selection, manifest/summary/compatibility backlinks, interrupted resume,
fail-closed rejection, and Criteria journal scope; the Scenario Check remains
the authority.

## Exact selection and tests-file normalization

Run `sim_campaign` with `--test tail --test quick`. The manifest selection,
work-item order, summary observations, and terminal results must remain
`tail, quick`, not catalog order. Then run a new Simulation Campaign with
`--tests-file reverse-tests.txt`; comments and blank lines must disappear and
the same normalized selection must remain.

Before the rejection Check, snapshot the report root and process table. Invoke
`--test quick --test quick`. Require exit 2, no new manifest, no new Icarus
process, and no simulator log. Do not reuse evidence from a parser-only unit
test.

## Manifest authority

For a successful fixture Simulation Campaign, save `manifest.json` bytes before the first
work item completes and again after completion. They must be identical. Inspect
the strict schema, exact Target identity, Required Simulation Suite, workload
fingerprint, and ordered work items. Authenticate every consumed result and
require both `summary.json` and the Target's `simulation.json` to identify the
same manifest. Run the validator with the expected ordered tests.

## Interruption and resume

Start the full suite in catalog order. After `quick` has a terminal
`result.json` and the `slow` attempt exists, terminate the owning Booley process
using the Scenario protocol's bounded process-tree procedure. Record the
termination timeline and prove no simulator survives. Save the completed
result bytes and attempt-directory listing, then resume only with
`--resume-from <exact-manifest.json>`. The `quick` result and attempt count must
not change; `slow` is retried and `tail` is admitted afterward.

Keep an interrupted Simulation Campaign copy for mismatch checks. First change
one owned `campaign_tb.sv` byte and attempt exact resume. Require exit 2 with a named
source mismatch and no new attempt. Restore the byte exactly, then append one
owned catalog test to the copied `tests.toml`, repeat the rejection, restore the
catalog exactly, and finish the resume successfully. Hash before every mutation
and after every restoration.

## Criteria scope

Use a run-owned Ticket whose mandatory Criterion is
`sim_pass_sim_campaign`. Capture the Criteria state and Acceptance Journal
before and after each invocation. A passing strict subset (`quick`) must leave
the Criterion unmet. A new Simulation Campaign containing the complete Required
Simulation Suite (`quick`, `slow`, `tail`) must publish it only after all three
durable results commit. If an extra registered test is temporarily added and
explicitly selected, its failure must still make the Simulation Campaign grade strict;
restore the catalog and fixture before cleanup.

## Cleanup

Before the first mutation, ledger the run-owned fixture copy, core/catalog
entries, report roots, Ticket state, process groups, and every source/catalog
byte restored by a negative Check. Recovery always archives the observed
failure first, restores only owned bytes, and executes the valid control in the
same Scenario Run; it does not depend on the detection Check passing.

Retain all Check evidence, reap owned processes, then remove only the run-owned
core/catalog entries, report roots, and Ticket state. Prove the pinned upstream
checkout is byte-for-byte unchanged, the Project catalog no longer exposes
`sim_campaign`, and every ledgered resource has a cleanup disposition.
