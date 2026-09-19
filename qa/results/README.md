# Observed Check history

Each sealed Scenario Run gets a compact JSON snapshot at
`qa/results/<scenario-id>/<run-id>.json`. `add` validates the version-2 run seal
and derives the snapshot; full records and immutable evidence stay in the run's
artifact root. Snapshots omit logs, evidence payloads, secrets, and machine-local
paths. They record observations, not Findings, Scenario Run Outcomes, or
Qualification, which require Human Maintainer triage.

The `format_version: 1` snapshot records run, Scenario, and Configured Scenario
IDs; product and suite revisions; normalized UTC completion time; execution and
cleanup statuses; run-manifest SHA-256; and a `checks` map for every selected
Check. Cleanup status is `complete`, `unverified`, or `failed`; historical
`complete` retains the earlier reporting contract. Check values are `pass`,
`fail`, `blocked`, or `unavailable`. Corrected attempts
do not count, but later success does not erase an uncorrected `fail`. A Check
that could not be exercised is `blocked` or `unavailable`, never missing.
`add` refuses to overwrite a run ID with different data; a reviewed correction
commit preserves the previous snapshot in Git history.

After sealing, run from the Booley source checkout:

```sh
python -m qa.results add /path/to/sealed-run
python -m qa.results validate
python -m qa.results report --configured-scenario CONFIGURED_SCENARIO_ID
git add qa/results/SCENARIO_ID/RUN_ID.json
git commit -m "qa: record SCENARIO_ID RUN_ID observations"
```

The commit stays local; pushing or opening a pull request needs a separate
request. `report` shows failing-Check counts for each Scenario and changes from
the prior run of the same Configured Scenario. It also shows per-configuration
pass/fail/blocked/unavailable counts in completion order, each currently
selected Check's latest status, and whether it was ever exercised. `pass` and
`fail` mean exercised; `blocked`, `unavailable`, and `not exercised` do not.
Latest statuses name their product and suite revisions; compare those before
treating a count change as a product improvement or regression.
