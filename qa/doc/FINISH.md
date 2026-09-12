# Finish a Scenario Run

Enter after execution, at the cleanup reserve boundary, or when authority, evidence
integrity, or resource control is lost. Stop new product work and preserve completed
records.

## Reconcile resources

Safely shut down and clean up every active, privileged, scarce, or externally visible
run-owned resource. Prove its release without exposing secrets or changing borrowed
state.

An owned resource may be `retained-for-review` only when evidence proves all of these:

- it is inert and carries no live authority;
- it consumes no scarce quota and contains no secret material;
- its exact identity, bounded storage location, review purpose, Human Maintainer
  owner, and later deletion instructions are recorded; and
- every claim that depends on it also has immutable evidence outside that mutable
  state.

Safely shut down and clean up unknown or ineligible state; resource type alone does
not prove it inert. Preserve borrowed resources unchanged.

Record one ledger disposition for every resource: `released`, `retained-for-review`,
`borrowed-preserved`, or `release-failed`. Intentional retention is allowed when it
satisfies the predicate. Record `release-failed` or missing cleanup evidence without
trying to decide the Scenario Run Outcome.

Scenario Checks that exercise product cleanup behavior remain authoritative: execute
their declared deletion or preservation stimulus even when the final workspace would
otherwise be retained. Archive its evidence first when the Scenario requires it.

## Finalize and seal the run

Finish within the original deadline when possible. If time expires, record the
overrun and continue required resource cleanup without extending the run or claiming
timely completion.

Append `blocked` Check Results for selected Checks without one. Finish only after each
selected Check has a Check Result, every Observation and correction is recorded, every
ledger resource has a disposition, active assignments and uncertain mutations are
reconciled, and required cleanup has evidence. Report execution status as `completed`,
`deadline-reached`, or `operator-error`, and cleanup status as `complete` or `failed`.

Write `run-summary.md` with identities, execution and cleanup status, Check Result
counts and links, Observations, deviations, and retained review locations. It contains
no Findings, Scenario Run Outcome, or Qualification.

Set `operator-state.json` to Protocol Stage `finish` with status `complete`, then run:

```sh
python qa/triage.py seal-run <run-root>
```

The helper validates every version-2 record, writes `evidence-manifest.json`, and
writes `run-manifest.json` last. A valid manifest is the completion authority. If the
operator stops after terminal state but before the manifest, a replacement may resume
only to validate or regenerate the final projections and seal the same records. Once
sealed, do not modify the run.

Report the sealed artifact root to the Human Maintainer and tell them that Findings,
outcomes, and Qualification require a separate explicit invocation of
`booley-qa-triage`.
