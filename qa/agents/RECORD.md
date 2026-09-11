# Record a Scenario Run

Use the files and fields in [Format](FORMAT.md). After admission creates the durable
cursor, persist it before and after each side effect. Append a Check Result for each
Check attempt to `check-results.jsonl` immediately:

| Status | Meaning |
|---|---|
| `pass` | Trustworthy evidence satisfies the declared expectation |
| `fail` | Trustworthy evidence contradicts it |
| `blocked` | A required Check lacks trustworthy evidence because of a failed prerequisite, timeout, infrastructure or operator error, or invalid execution |
| `unavailable` | A pre-run assessment proved an applicable declared capability absent |

A capability lost after admission is `fail` or `blocked`. Exclusions are fixed by the
Configured Scenario and are not passes.

Append corrections; never rewrite Check Result history. A correction names the
mistaken Check Result and proves the recording error. Preserve every trustworthy
failed attempt. Conflicting trustworthy Check Results produce a flaky Finding and
prevent Qualification.

Append Findings, Friction Reports, Impressions, and wins to `findings.jsonl` with
stable source IDs, original text, kind, time, classification, Step and Check links,
and evidence. Defects also record expected and observed behavior, stimulus, and
reproduction details. Append status updates without rewriting observations. Preserve
unclassified observations for downstream triage.

Before creating an owned resource, add its planned identity, ownership, and intended
disposition to `cleanup-ledger.json`. When its identity is unknowable in advance,
record it immediately after acquisition and before dependent work. Keep the ledger
sufficient for another operator to quiesce the run without touching unrelated state.

Retain internal QA records unredacted except for secret values and directly usable by
Consolidate Findings. Do not submit reports externally during a run. Immutable
evidence lives outside disposable or retained mutable Project state; a retained
workspace may aid review but cannot be the only evidence for a claim.
