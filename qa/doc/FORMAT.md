# Scenario Run record format

Use `format_version: 1` for structured Scenario Run files that declare a format.

## Files

| File | Minimum content |
|---|---|
| `run.json` | Fresh, unique Scenario Run ID, Configured Scenario ID, declared parameters, exact identities, initial inputs, authority, deadline, and evidence for pre-run requirements |
| `operator-state.json` | Scenario Run ID, mutable Protocol Stage and status, entered and completed timestamps, next Step and Check attempt, active assignments, outstanding mutations, and last atomic update |
| `check-results.jsonl` | Append-only Check Results: Check Result ID, Step ID, Check ID, timestamp, attempt, status, expected and observed outcomes, evidence references, producing-Step identities, and recovery or correction links when applicable |
| `findings.jsonl` | Original Findings and appended status updates, stable source IDs, kind, classification, original text, Check Result links, evidence, and reproduction data as appropriate |
| `cleanup-ledger.json` | Mutable resource ledger: exact identity, ownership, active-authority and scarcity classification, intended and actual disposition, evidence, and retention details when applicable |
| `evidence/` | Immutable artifacts, logs, traces, diffs, reports, case manifests, and hashes where artifact identity matters |
| `summary.md` | Scenario Run Outcome and, when applicable, Qualification; tested identities, execution and resource cleanup status, missing and failed Checks, Findings, deviations, and retained review locations |

The containing directory supplies the Scenario Run ID to Check Result and Finding records;
external references use the Scenario Run ID plus the record ID. Common Booley product
revision and Runtime Image inputs need not repeat in every Check Result. Changes
created by Steps are recorded as outputs rather than mutations to the original
`run.json`. Retain evidence outside disposable Project state.

Write mutable JSON files by atomic replacement. `operator-state.json` uses Protocol
Stages `execute` and `finish`; `complete` is the terminal `finish` status. The
checkpoint identifies work to reconcile before retry. The resource ledger uses
`released`, `retained-for-review`, `borrowed-preserved`, and `release-failed`. A
retained entry also records the fields and evidence required by the
[retention predicate](FINISH.md#reconcile-resources).

The structured records are the source of truth; `summary.md` is their view. Keep
original observations and explicit correction links visible. Findings must be usable
directly by Consolidate Findings without a separate Booley Feedback export. See the
[suite README](../README.md#scenario-and-check-structure) for Scenario and Check
structure.
