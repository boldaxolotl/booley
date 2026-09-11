# Record a Scenario Run

Use the files and fields in [Format](FORMAT.md). Append every Check attempt to
`check-results.jsonl` immediately:

| Status | Meaning |
|---|---|
| `pass` | Trustworthy evidence satisfies the declared expectation |
| `fail` | Trustworthy evidence contradicts it |
| `blocked` | A required Check lacks trustworthy evidence because of a failed prerequisite, timeout, infrastructure or operator error, or invalid execution |
| `unavailable` | A pre-run assessment proved an applicable declared capability absent |

A capability lost after admission is `fail` or `blocked`. Exclusions are fixed by the
Configured Scenario and are not passes.

Candidate creation, operator-only CLI installation, and Host Bootstrap performed
during admission are preparation provenance, not Check Results. Retain their verified
pre/post evidence under `evidence/admission/` when admission succeeds and link it from
`run.json`. A later Scenario Check must perform and evidence its own declared stimulus.

Append corrections; never rewrite Check Result history. A correction names the
mistaken Check Result and proves the recording error. Preserve every trustworthy
failed attempt. Conflicting trustworthy Check Results produce a flaky Finding and
prevent Qualification.

Append Findings, Friction Reports, Impressions, wins, and later status updates to
`findings.jsonl` without rewriting original observations. Defects also record expected
and observed behavior, stimulus, and reproduction details. Preserve unclassified
observations.

Bind identities created by a Step to that Step and its evidence.

Before creating an owned resource, add its planned identity, ownership, and intended
disposition to `cleanup-ledger.json`. When its identity is unknowable in advance,
record it immediately after acquisition and before dependent work. Keep the ledger
sufficient for another operator to safely shut down and clean up the run-owned
resources without touching unrelated state.

Retain internal QA records unredacted except for secrets and in the form Consolidate
Findings accepts. Do not submit reports externally during a run. Keep immutable
evidence outside mutable Project state; a retained workspace cannot be the only
evidence for a claim.
