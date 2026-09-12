# Record a Scenario Run

Use the version-2 files and fields in [Format](FORMAT.md). Append every Check attempt
to `check-results.jsonl` immediately:

| Status | Meaning |
|---|---|
| `pass` | Trustworthy evidence satisfies the declared expectation |
| `fail` | Trustworthy evidence contradicts it |
| `blocked` | The Check lacks trustworthy evidence because of a failed prerequisite, timeout, infrastructure/operator error, or invalid execution |
| `unavailable` | A pre-run assessment proved an applicable declared capability absent |

A capability lost after admission is `fail` or `blocked`. Exclusions are fixed by the
Configured Scenario and are not passes. Preserve every trustworthy failed attempt;
later success does not erase it.

Candidate creation, operator-only CLI installation, and Host Bootstrap during
admission are preparation provenance, not Check Results. Retain their verified
pre/post evidence under `evidence/admission/` when admission succeeds and link it from
`run.json`. A later Scenario Check performs and evidences its own declared stimulus.
Before admission mutates state, create the records defined by
[Format](FORMAT.md#admission-attempt-records), ledger planned resources, and update
identities and dispositions atomically.

Append evidence-backed corrections before the run is sealed. Record explicit causes
only when one exact prior Check Result caused the current result. Record integrity
concerns, deviations, and conflicts in their structured fields so triage can enumerate
them deterministically.

Append unexpected behavior, incidental facts, friction, impressions, and wins to
`observations.jsonl` without classifying them as Findings. Preserve the original text
and link any producing Step, Check Result, cause, correction, and evidence. The Human
Maintainer owns their later Triage Dispositions.

Bind identities created by a Step to that Step and its evidence. Before creating an
owned resource, add its planned identity, ownership, and intended disposition to
`cleanup-ledger.json`. When its exact identity is unknowable in advance, record it
immediately after acquisition and before dependent work. Keep the ledger sufficient
for another operator to safely shut down and clean up run-owned resources without
touching unrelated state.

Keep immutable evidence outside mutable Project state; retained workspace state cannot
be the only evidence for a claim. The Scenario Operator does not create
`findings.jsonl`, decide a Scenario Run Outcome, or calculate Qualification.
