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
| `results.jsonl` | Append-only result records: result ID, Step ID, Check ID, timestamp, attempt, status, expected and observed outcomes, evidence references, producing-Step identities, and recovery or correction links when applicable |
| `findings.jsonl` | Original Findings and appended status updates, stable source IDs, kind, classification, original text, result links, evidence, and reproduction data as appropriate |
| `cleanup-ledger.json` | Mutable cleanup ledger for resources owned or borrowed by the Scenario Run: exact identity, ownership, intended disposition, actual cleanup result, and evidence. It must support safe cleanup after interruption without affecting unrelated resources |
| `evidence/` | Immutable artifacts, logs, traces, diffs, reports, case manifests, and hashes where artifact identity matters |
| `summary.md` | Scenario Run Outcome and, when applicable, Qualification; tested identities, execution status, missing and failed Checks, Findings, deviations, and cleanup |

The containing directory supplies the Scenario Run ID to result and Finding records;
external references use the Scenario Run ID plus the record ID. Common Booley product
revision and Runtime Image inputs need not repeat in every result record. Changes
created by Steps are recorded as outputs rather than mutations to the original
`run.json`. Retain evidence outside disposable Project state.

The summary is a view of these records, not a competing source of truth. Consumers
can read the small files directly; event replay, supersession projection engines,
and digest-bound report generation are not prerequisites. Keep original observations
and explicit correction links visible. Findings must be usable directly by Consolidate
Findings without a separate Booley Feedback export.
