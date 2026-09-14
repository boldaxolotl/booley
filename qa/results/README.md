# Compact observed Check history

This directory holds one small, reviewable JSON snapshot per sealed Scenario Run.
It supports run-to-run counts of observed failing Checks and per-Check exercise
history. The sealed records and immutable evidence remain under each run's artifact
root. A snapshot contains no logs, evidence payloads, secrets, or machine-local
paths. It is **not** a Finding, Scenario Run Outcome, or Qualification; those are
determined later by Human Maintainer triage.

The path is `qa/results/<scenario-id>/<run-id>.json`. The `add` command derives it
from a valid version-2 run seal. It records the run and Configured Scenario IDs,
product and suite revisions, normalized UTC completion time, execution and cleanup
statuses, the run-manifest
SHA-256, and one observed status per selected Check. Corrected attempts do not count;
an uncorrected failure remains `fail` after a later successful attempt. A Check
that could not be exercised has `blocked` or `unavailable`, not a missing entry.
The format is `format_version: 1` and the `checks` map has `pass`, `fail`, `blocked`,
or `unavailable` values. Git retains prior versions if a snapshot is ever corrected,
but `add` refuses to silently overwrite one run ID with different data.

After the Scenario Operator seals a run, use the Booley source checkout:

```sh
python -m qa.results add /path/to/sealed-run
python -m qa.results validate
python -m qa.results report --configured-scenario CONFIGURED_SCENARIO_ID
git add qa/results/SCENARIO_ID/RUN_ID.json
git commit -m "qa: record SCENARIO_ID RUN_ID observations"
```

The local commit records the snapshot in Git. Pushing the branch or opening a pull
request requires a separate request. `report` shows failing-Check counts for every
Scenario and the change from the prior run of the same Configured Scenario. It then
shows pass/fail/blocked/unavailable counts per configuration in completion order,
followed by every currently selected Check's status in the
latest recorded run and whether it has ever been exercised. `pass` and `fail` mean
exercised; `blocked`, `unavailable`, and `not exercised` do not. Each latest status
is tied to the displayed product and suite revisions. Compare those revisions
before interpreting a change in counts as a product improvement or regression.
