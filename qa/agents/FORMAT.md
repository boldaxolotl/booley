# Scenario Run record format

This document defines the files written by the Scenario Operator during a Scenario
Run. The Scenario Operator uses it to produce the record; Human Maintainers and other
consumers use the same contract to audit it. See the [authoring
guide](../user/AUTHORING.md) for the Scenario definition contract.

Use `format_version: 1` for structured Scenario Run files that declare a format
version.

## Files

| File | Minimum content |
|---|---|
| `run.json` | Fresh, unique Scenario Run ID, Configured Scenario ID, declared parameters, exact identities, initial inputs, authority, deadline, and evidence for pre-run requirements |
| `operator-state.json` | Scenario Run ID, mutable Protocol Stage and status, entered and completed timestamps, next Step and Check attempt, active assignments, outstanding mutations, and last atomic update |
| `check-results.jsonl` | Append-only Check Results: Check Result ID, Step ID, Check ID, timestamp, attempt, status, expected and observed outcomes, evidence references, producing-Step identities, and recovery or correction links when applicable |
| `findings.jsonl` | Original Findings and appended status updates, stable source IDs, kind, classification, original text, Check Result links, evidence, and reproduction data as appropriate |
| `cleanup-ledger.json` | Mutable resource ledger: exact identity, ownership, active-authority and scarcity classification, intended and actual disposition, evidence, and retention details when applicable |
| `evidence/` | Immutable artifacts, logs, traces, diffs, reports, case manifests, and hashes where artifact identity matters |
| `summary.md` | Scenario Run Outcome and, when applicable, Qualification; tested identities, execution and quiescence status, missing and failed Checks, Findings, deviations, and retained review locations |

The containing directory supplies the Scenario Run ID to Check Result and Finding records;
external references use the Scenario Run ID plus the record ID. Common Booley product
revision and Runtime Image inputs need not repeat in every Check Result. Changes
created by Steps are recorded as outputs rather than mutations to the original
`run.json`. Retain evidence outside disposable Project state.

Write mutable JSON files by atomic replacement. `operator-state.json` uses Protocol
Stages `prepare`, `execute`, `finish`, and `complete`; its cursor makes re-entry
idempotent by identifying work whose completion must be reconciled before retry. The
resource ledger uses `released`, `retained-for-review`, `borrowed-preserved`, and
`release-failed`. A retained entry also records its bounded location, review purpose,
Human Maintainer owner, deletion instructions, and evidence that it satisfies the
[retention predicate](FINISH.md#reconcile-resources).

The summary is a view of these records, not a competing source of truth. Consumers
can read the small files directly; event replay, supersession projection engines,
and digest-bound report generation are not prerequisites. Keep original observations
and explicit correction links visible. Findings must be usable directly by Consolidate
Findings without a separate Booley Feedback export.
