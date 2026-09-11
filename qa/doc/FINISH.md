# Finish a Scenario Run

Enter after execution, at the cleanup reserve boundary, or when authority, evidence
integrity, or resource control is lost. Stop new product work and preserve completed
records.

## Reconcile resources

Quiesce every active, privileged, scarce, or externally visible run-owned resource.
Prove its release without exposing secrets or changing borrowed state.

An owned resource may be `retained-for-review` only when evidence proves all of these:

- it is inert and carries no live authority;
- it consumes no scarce quota and contains no secret material;
- its exact identity, bounded storage location, review purpose, Human Maintainer
  owner, and later deletion instructions are recorded; and
- every claim that depends on it also has immutable evidence outside that mutable
  state.

Quiesce unknown or ineligible state; resource type alone does not prove it inert.
Preserve borrowed resources unchanged.

Record one ledger disposition for every resource: `released`, `retained-for-review`,
`borrowed-preserved`, or `release-failed`. Intentional retention does not prevent a
pass. `release-failed` or missing evidence for mandatory quiescence does.

Scenario Checks that exercise product cleanup behavior remain authoritative: execute
their declared deletion or preservation stimulus even when the final workspace would
otherwise be retained. Archive its evidence first when the Scenario requires it.

## Finalize the run

Finish within the original deadline when possible. If time expires, record the
overrun and continue mandatory quiescence without extending the run or claiming timely
completion.

Append `blocked` Check Results for selected Checks without one. Report execution
status as `completed`, `deadline reached`, or `operator error`, and quiescence status
as `complete` or `failed`. Calculate the Scenario Run Outcome and aggregate
Qualification under [Qualification](QUALIFICATION.md). A product failure
remains a failure when work is missing or quiescence fails.

Finish is complete when every selected Check has a Check Result, each ledger resource
has a disposition, mandatory quiescence has evidence, retained review state satisfies
the retention predicate, and `summary.md` contains the required outcomes and statuses.
Only then set `operator-state.json` to Protocol Stage `finish` with status `complete`.
