# PicoRV32 Simulation Record QA fixture

This runbook gives the QA agent running the picorv32 mission the exact steps
for this optional hunt bait ([MISSION.md](../../MISSION.md), `hunt` area): exact
test selection, manifest authority, interruption and resume, and measurement
scope on a small Icarus Target. Coverage Simulation
Record recovery lives in the [coverage
fixture](../../../../shared/coverage/simulation-record/RUNBOOK.md).
Automated regressions and `validate_campaign.py` protect the public contracts;
they do not replace running these steps against the build under test.

## Setup

Use this fixture only inside a run-owned copy of the PicoRV32 Project, and do
not edit the pinned upstream source. The Project uses Stealth with
`ignore_native_cores = true`, so `.booley/cores/` is the only core root and
fileset paths resolve from the repository root
([STEALTH.md](../../../../../docs/user/STEALTH.md)).

1. Copy `campaign.core` and `campaign_tb.sv` into `.booley/cores/qa_campaign/`.
2. In the copied core, change the `campaign_tb.sv` entry to
   `.booley/cores/qa_campaign/campaign_tb.sv`.
3. Merge the `sim_campaign` table from `tests.toml` into the Project's active
   test catalog. Its suite is `quick`, `slow`, `tail`, in that order.
4. Add the copied files and the catalog entry to `resources.md` before running
   anything.

`slow` spins simulation time so the interruption case has a real wall-clock
window; if it finishes in under about 15 s on your host, lengthen it as the
header comment in `campaign_tb.sv` says.

Every command below uses `--report-dir <run-owned-root>`. Save the exact argv,
stdout and stderr, exit status, the printed manifest path, directory listings,
and copies of every referenced JSON document under `evidence/sim-record/`.
A Target's files are under `<root>/sim/<N>/targets/sim_campaign/`: the
manifest and `summary.json` in `record/`, terminal Results at
`record/work-items/<item>/result.json`, and the compatibility projection
`simulation.json`.

`validate_campaign.py` is a read-only structural cross-check with one
subcommand per check; run `python3 validate_campaign.py <check> --help` for
its arguments. Your own reading of the evidence against the steps below
decides a finding.

| Check | What it validates |
|---|---|
| `selection` | Exact ordered selection in the manifest |
| `backlinks` | Manifest, `summary.json` and `simulation.json` point at each other |
| `resume` | Completed work is unchanged across a resume |
| `rejection` | A refused request left no new attempt and started no simulator |
| `journal` | One acceptance transaction is added only when the observed tests cover the required suite |

## Exact selection and tests-file normalization

1. Run `booley flow sim --target sim_campaign --test tail --test quick`. The
   manifest selection, work-item order, summary observations and terminal
   Results must stay `tail, quick`, not catalog order.
2. Run a new Simulation Record with `--tests-file reverse-tests.txt`.
   Comments and blank lines must drop out, leaving the same selection.
3. Snapshot the report root and process table, then invoke
   `--test quick --test quick`. It must exit 2 with no new manifest, no new
   Icarus process and no simulator log. Evidence from a parser-only unit test
   does not count.

## Manifest authority

For a successful Simulation Record, save the `manifest.json` bytes before
the first work item completes and again after completion; they must be
identical. Inspect the strict schema, exact Target identity, required suite
(`required_suite`), workload fingerprint and ordered work items. Authenticate
every consumed Result, and require both `summary.json` and `simulation.json`
to identify the same manifest. Run `validate_campaign.py selection` with the
expected ordered tests, and `validate_campaign.py backlinks` on the manifest,
`summary.json` and `simulation.json` in place; the projection's manifest
reference resolves relative to its own Target directory.

## Interruption and resume

1. Start the full suite in catalog order. After `quick` has a terminal
   `result.json` and the `slow` attempt exists, terminate the owning Booley
   process and its process tree: TERM, a bounded wait, then KILL. Log the
   termination timeline and prove no simulator survives.
2. Save the completed Result bytes and the attempt-directory listing, then
   resume with `booley flow sim --resume-from <exact-manifest.json>` only. The
   `quick` Result and its attempt count must not change; `slow` is retried and
   `tail` runs afterward.
3. Keep a copy of an interrupted Simulation Record for mismatch checks.
   Change one byte of the copied `campaign_tb.sv` and attempt the exact resume:
   it must exit 2 with a named source mismatch and no new attempt. Restore the
   byte exactly.
4. Append one test to the copied catalog's `sim_campaign` suite and repeat the
   rejection. Restore the catalog exactly and finish the resume successfully.
   Compare the files against a saved copy after every restoration.

## Measurement scope

1. Commit the fixture registration and a `[sim_campaign.sim]` table in the
   Booley Directory's repository, and create a clean linked worktree.
2. A passing `booley flow sim --test quick` makes nothing Measured.
3. `booley measure` runs the whole registered suite (`quick`, `slow`, `tail`)
   in a clean checkout and stores one Measurement Run.
4. A temporarily added failing registered test, committed, makes `tests_failed`
   unmet (exit 1). Restore the catalog and fixture before cleanup.

## Cleanup

Before the first mutation, add to `resources.md` the run-owned fixture copy,
core and catalog entries, report roots, measuring worktree, process groups, and
every
source or catalog file a negative step changes. When a negative step leaves
something broken, record the finding, restore only owned bytes, and still run
the valid control; it does not depend on the negative step passing.

Keep the evidence and reap owned processes. Then remove the measuring
worktree and only the run-owned core and catalog entries and report roots.
Confirm the pinned upstream checkout is byte-for-byte
unchanged, the Project catalog no longer lists `sim_campaign`, and every
`resources.md` row for this fixture is marked released.
