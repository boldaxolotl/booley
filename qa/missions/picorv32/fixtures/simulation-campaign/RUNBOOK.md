# PicoRV32 Simulation Campaign QA fixture

Recipes for the picorv32 mission's `sim-campaign` area. Automated regressions
and this fixture's validator protect the public contracts; they do not replace
running the steps below against the build under test.

Use this owned Icarus fixture only inside a run-owned copy of the PicoRV32
Project. Do not edit the pinned upstream source. Copy `campaign.core` and
`campaign_tb.sv` into one declared core root and merge the `sim_campaign`
table from `tests.toml` into the Project's active test catalog. Add the copied
files and catalog entry to `resources.md` before running anything.

All commands below must use an explicit run-owned report root. Save exact
argv, stdout/stderr, exit status, the printed manifest path, directory listings,
and copies of every referenced JSON document under `evidence/sim-campaign/`.
The `validate_campaign.py` helper is a read-only structural cross-check for
exact selection, manifest/summary/compatibility backlinks, interrupted resume,
fail-closed rejection, and Criteria journal scope; your own reading of the
evidence against the steps below is what decides a finding.

## Exact selection and tests-file normalization

Run `sim_campaign` with `--test tail --test quick`. The manifest selection,
work-item order, summary observations, and terminal results must remain
`tail, quick`, not catalog order. Then run a new Simulation Campaign with
`--tests-file reverse-tests.txt`; comments and blank lines must disappear and
the same normalized selection must remain.

Before the rejection step, snapshot the report root and process table. Invoke
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
and its process tree (TERM, bounded wait, then KILL). Log the termination
timeline and prove no simulator survives. Save the completed result bytes and
attempt-directory listing, then resume only with
`--resume-from <exact-manifest.json>`. The `quick` result and attempt count must
not change; `slow` is retried and `tail` is admitted afterward.

Keep an interrupted Simulation Campaign copy for mismatch checks. First change
one owned `campaign_tb.sv` byte and attempt exact resume. Require exit 2 with a named
source mismatch and no new attempt. Restore the byte exactly, then append one
owned catalog test to the copied `tests.toml`, repeat the rejection, restore the
catalog exactly, and finish the resume successfully. Compare the files against
a saved copy after every restoration.

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

Before the first mutation, add to `resources.md` the run-owned fixture copy,
core/catalog entries, report roots, Ticket state, process groups, and every
source/catalog file a negative step changes. When a negative step leaves
something broken, record the finding, restore only owned bytes, and still run
the valid control; it does not depend on the negative step passing.

Keep the evidence, reap owned processes, then remove only the run-owned
core/catalog entries, report roots, and Ticket state. Confirm the pinned upstream
checkout is byte-for-byte unchanged, the Project catalog no longer exposes
`sim_campaign`, and every `resources.md` row for this area is marked released.
