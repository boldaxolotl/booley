# Finish a Scenario Run

Enter after execution, at the cleanup reserve boundary, or when authority, evidence
integrity, or resource control is lost. Stop new product work and preserve completed
records.

## Reconcile resources

Stop active authority and attempt to release every privileged, scarce, or externally
visible run-owned resource. Preserve evidence of the action without exposing secrets
or changing borrowed state. If safe shutdown cannot be established, flag the resource
as possibly retaining active authority and escalate it promptly to the Human
Maintainer. Preserve the available records by sealing the run.

Claim `retained-for-review` only when evidence proves all of these:

- it is inert and carries no live authority;
- it consumes no scarce quota and contains no secret material;
- its exact identity, bounded storage location, review purpose, Human Maintainer
  owner, and later deletion instructions are recorded; and
- every claim that depends on it also has immutable evidence outside that mutable
  state.

Safely shut down and clean up unknown or ineligible state; resource type alone does
not prove it inert. Preserve borrowed resources unchanged.

Record the observed disposition when known: `released`, `retained-for-review`,
`borrowed-preserved`, or `release-failed`. Leave `actual_disposition` null when it
cannot be established; never copy the intended disposition into it. Record a short
reason and affected identities for `failed` or `unverified` in the ledger and summary.
Classify whether each uncertain resource may still retain active authority, and link
safe shutdown evidence when available. Missing disposition or evidence does not by
itself prevent a seal. Do not decide the Scenario Run Outcome here.

Publish each cleanup reconciliation as a complete candidate through
`python3 qa/record_cleanup.py <run-root> <candidate.json>`. The candidate must retain
every resource row, use only the supported resource fields, and reference only
finished regular files beneath the run's `evidence/` directory. Do not edit the
ledger directly.

Scenario Checks that exercise product cleanup behavior remain authoritative: execute
their declared deletion or preservation stimulus even when the final workspace would
otherwise be retained. Archive its evidence first when the Scenario requires it.

## Finalize and seal the run

Finish within the original deadline when possible. If time expires, record the
overrun and continue required resource cleanup without extending the run or claiming
timely completion.

Append `blocked` Check Results for selected Checks without one. Finish only after each
selected Check has a Check Result, every Observation and correction is recorded, and
active assignments and uncertain mutations are reconciled. Report execution status
as `completed`, `deadline-reached`, or `operator-error`. Use cleanup status `complete`
when known actions have supporting evidence and resources were safely released or
eligible for retention; `failed` for an observed failed release or known unsafe
resource; `unverified` for missing or inconclusive disposition or evidence. Do not
infer failure from an unknown disposition.

Record operator narrative in structured Observations. The helper regenerates
`run-summary.md` from structured records before sealing; it contains no Findings,
Scenario Run Outcome, or Qualification.

Set `operator-state.json` to Protocol Stage `finish` with status `complete`, then run:

```sh
python qa/triage.py seal-run <run-root> --suite-root <frozen-suite-root>
```

The helper validates every version-2 record against the frozen Scenario snapshot and
reconciles cleanup status with precedence `failed` > `unverified` > `complete`. It
generates the evidence manifest and summary from validated records, updates the
terminal checkpoint when needed, and writes `run-manifest.json` last. A valid manifest
is the completion authority. If interrupted before the manifest, retry Finish to
reconcile and seal the same available records; this may update terminal status and
timestamp, but never reopen product work. Once sealed, do not modify the run.

The manifest's Scenario snapshot hash covers Scenario YAML paths and bytes, while
`suite_revision` remains a declared label. Successful sealing establishes run-content
admission validity against this snapshot; triage still checks its session, target
revisions, and any needed editorial-equivalence decision separately. Review any
result-ID diagnostics in the generated summary before handing off the run.

For selected borrowed-resource preservation Checks, the helper rejects a `pass`
without scoped identities and linked setup/end evidence, or `unavailable` without
a linked pre-run absence assessment. Record `blocked` when that evidence is missing.

Report the sealed artifact root to the Human Maintainer and tell them that Findings,
outcomes, and Qualification require a separate explicit invocation of
`booley-qa-triage`.

After sealing, record and locally commit the compact observed-Check snapshot described
in [Result history](../results/README.md). It indexes run-to-run observations, not
Findings or a Scenario Run Outcome. Keep the sealed records and evidence in the
artifact root for separate Human Maintainer triage.
