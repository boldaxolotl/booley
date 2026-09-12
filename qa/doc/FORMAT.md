# Public QA record format

Scenario YAML keeps `format_version: 1`. Scenario Run records use
`run_record_format_version: 2`; triage records use `triage_format_version: 1`.
[`run-record.schema.json`](../run-record.schema.json) and
[`triage-record.schema.json`](../triage-record.schema.json) are the structural
authorities. Every JSON object and JSONL record carries its format version and record
type.

## Admission Attempt records

Before mutating admission reconciliation, create
`<artifact-root>/admission-attempts/<Admission Attempt ID>/` with a fresh ID and these
durable records:

| File | Minimum content |
|---|---|
| `admission-state.json` | Admission Attempt ID, Scenario and Configured Scenario IDs, exact requested source and resolved commit, intended candidate identity, status, current action, timestamps, and last atomic update |
| `admission-cleanup-ledger.json` | Planned or exact resource identity, ownership, active-authority and scarcity classification, intended and actual disposition, evidence, and retention details when applicable |
| `evidence/` | Immutable commands, output, pre/post state, provenance, and hashes for admission reconciliation |

Write both JSON records by atomic replacement. Ledger every operator-owned resource
before creation; when its exact identity is unknowable in advance, record the planned
identity first and replace it immediately after acquisition. On admission failure,
retain the finalized attempt record and reconcile its ledger. On success, retain the
record under the Scenario Run's `evidence/admission/`, link it from `run.json`, and
transfer every still-owned resource to `cleanup-ledger.json` before execution.

## Scenario Run records

| File | Ownership and content |
|---|---|
| `run.json` | Frozen run, Scenario, Configured Scenario, product and suite revisions; selected Check IDs; parameters, identities, authority, deadline, and admission evidence |
| `operator-state.json` | Mutable Protocol Stage checkpoint, active work, terminal execution status, and cleanup status |
| `check-results.jsonl` | Append-only Check attempts with expected/observed behavior, evidence, status, correction, cause, review, integrity, deviation, recovery, and producing-Step fields |
| `observations.jsonl` | Append-only unclassified Observations with stable identity, original text, producing Step, Check Result/cause/correction links, and evidence |
| `cleanup-ledger.json` | Mutable resource ownership, authority/scarcity, intended and actual disposition, evidence, and retention details |
| `evidence/` | Immutable admission provenance, artifacts, logs, traces, diffs, reports, and case manifests |
| `evidence-manifest.json` | Every retained evidence path, SHA-256, size, and referencing Check Result or Observation |
| `run-summary.md` | Tested identities, execution and cleanup statuses, Check Result counts and links, Observations, deviations, and retained review locations; no Findings or verdicts |
| `run-manifest.json` | Final seal: terminal statuses, record counts, evidence-manifest hash, and hashes of every final run record except itself |

Check Result statuses are `pass`, `fail`, `blocked`, or `unavailable` as defined by
[Record](RECORD.md). `caused_by_result_ids` names exact earlier attempts. For a blocked
prerequisite, the relationship must agree with the Scenario's declared dependency
direction. Similar text, timing, tool, or resource identity is not a causal link.

`review_reasons` uses `nonpass`, `conflicting`, `correction-chain`, `deviation`, or
`evidence-integrity`. Evidence integrity is `valid`, `invalid`, or `uncertain`.
Structured deviations record kind, declared action/input, actual action/input, reason,
and evidence. Observations always carry the `observation` review reason and may also
carry `correction-chain`.

Append corrections and preserve their targets. Before sealing, a Check Result or
Observation correction names an earlier record in the same log. After sealing, only a
Human Maintainer may record a clerical interpretation correction during triage, using
evidence already in the sealed evidence manifest. New behavioral evidence requires a
new Scenario Run.

`python qa/triage.py seal-run <run-root>` validates the terminal records, writes the
evidence manifest, and writes `run-manifest.json` last. A valid manifest—not terminal
operator state alone—is the completion authority. A crash after the terminal
checkpoint may resume Finish only to regenerate final projections and seal the same
records. Once sealed, the run is immutable.

## Triage records

A distinct triage artifact root owns one expandable session:

| File | Ownership and content |
|---|---|
| `.triage.lock` | Persistent one-byte cross-process serialization lock; not evidence |
| `session.json` | Immutable session ID, target product and suite revisions, exact Scenario-file snapshot, policy/helper revisions, and required/optional Configured Scenario matrix |
| `triage-events.jsonl` | Totally ordered, append-only run admissions and supersessions, case changes, human dispositions, and Qualification calculations with idempotency keys |
| `input-runs.json` | Atomically regenerated projection of admitted sealed runs, manifest hashes, compatibility, and active role |
| `triage-state.json` | Atomically regenerated resumable progress and pending cases |
| `findings.jsonl` | Atomically regenerated, maintainer-confirmed product/documentation Findings with provenance from linked duplicate cases |
| `qa-changes.jsonl` | Atomically regenerated actionable QA defects and improvements, including whether each invalidates evidence |
| `triage-summary.md` | Candidate-to-case accounting, dispositions, Findings, QA Changes, outcomes, missing work, and evidence locations |
| `qualification.json` | Input identities, compatibility/reuse/supersession decisions, outcome reasons, current Scenario Run Outcomes, and aggregate Qualification; generated only after every active case is dispositioned |

The event log is the source of truth. Writers hold a bounded cross-process lock while
assigning sequences and replacing the log. Each event has one monotonic session sequence,
stable event ID, idempotency key, timestamp, type, and payload. Replay applies
split/merge/reopen events and requires every candidate to belong to exactly one active
case. Derived IDs come from disposition event IDs. Replaying after interruption
regenerates projections without duplicating outputs.

Adding a run or reopening a case removes the current derived `qualification.json`
until triage completes again; historical calculations remain in the event log. The
session's product, target suite, policy, and helper revisions never change. Another
suite revision requires a recorded Human Maintainer editorial-equivalence decision;
another product revision requires another session.

Every admitted run initially has an active Qualification role. Runs for the same
Configured Scenario aggregate conservatively: failure takes precedence over
incompleteness, which takes precedence over pass. A passing rerun does not change an
earlier run's role. After all cases are dispositioned, the Human Maintainer may record
`run-superseded` for an incomplete run and a complete passing rerun. A failed run may
be superseded only when sealed evidence proves it invalid. Superseded runs remain in
the Qualification audit as historical inputs with the replacement, basis, and reason.

## Triage Dispositions

One active case has exactly one of these dispositions. Split a case first when its
candidates need different dispositions.

| Disposition | Required result | Outcome effect |
|---|---|---|
| `product-defect` | Finding with Feedback owner and qualification scope | In-scope fails; out-of-scope is neutral; unresolved scope is incomplete |
| `documentation-defect` | Finding with Feedback owner and qualification scope | In-scope fails; out-of-scope is neutral; unresolved scope is incomplete |
| `duplicate-finding` | Link to a prior case or external Finding | Inherits the linked Finding's scope and effect |
| `qa-invalidating-defect` | QA Change with `invalidates_evidence: true` | Incomplete unless another case fails |
| `qa-improvement` | QA Change with `invalidates_evidence: false` | Neutral; cannot dismiss a non-pass |
| `infrastructure-failure` | Reason | Incomplete unless another case fails |
| `operator-failure` | Reason | Incomplete unless another case fails |
| `recording-error` | Corrected status, reason, and sealed evidence | Corrected pass is neutral; blocked/unavailable is incomplete |
| `expected-observation` | Reason | Neutral; cannot dismiss a non-pass |
| `friction`, `impression`, `win` | Reason | Neutral; remains an Observation rather than a QA Finding |
| `unresolved` | Reason | Incomplete unless another case fails |

Independently of case dispositions, a terminal execution status other than
`completed`, invalid or uncertain evidence integrity, or failed required cleanup
makes the linked run incomplete unless a failure-producing disposition takes
precedence.

Finding-producing dispositions record owner (`project`, `Booley`, `documentation`, or
unresolved), qualification scope (`in-scope`, `out-of-scope`, or `unresolved`), and a
scope reason. Out-of-scope Findings stay visible but cannot neutralize a trustworthy
failed Check. A duplicate of an in-scope Finding still fails every linked run.

Structured records are authoritative; Markdown summaries are projections. Keep
internal records unredacted except for secrets. External references use the session or
run ID plus the record ID. Findings must remain directly consumable by Consolidate
Findings. No Scenario Run or triage action implicitly authorizes external submission.
