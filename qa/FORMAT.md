# Scenario and run format

The repository commit freezes the protocol, profiles, coverage, and scenarios.
Use `format_version: 1` to identify the structured file shape. Keep stable scenario
IDs and scenario-qualified step/check IDs. A revision to the same check retains its
ID; a different check gets another ID. Never repurpose IDs. Git provides history;
separate retirement and successor registries are unnecessary.

## Scenario files

Production layout to encode in the implementation handoff:

```text
qa/
  PROTOCOL.md
  AUTHORING.md
  QUALIFICATION.md
  FORMAT.md
  profiles.yaml
  coverage.yaml
  scenario.schema.json
  scenarios/<scenario-id>/scenario.yaml
```

An ordered step contains its ID, action, prerequisites where needed, checks,
scenario-specific authority, timeout/retry restrictions, recovery instructions,
owned resources, and named phase recovery point where useful. Shared behavior is
inherited from the protocol; override only to tighten it. Inputs and shared budgets
belong at scenario level. Keep lengthy prompts, Ticket payloads, and evaluator
material in referenced files rather than duplicating them.

Each check has an ID, capability references, stimulus, expectation, public contract
source, evidence requirement, and capture point. Profiles alone select check IDs;
checks do not repeat profile membership. Keep navigation references where the documentation path itself matters;
the run records the documentation actually consulted. Authority for expected behavior
and navigation guidance remain distinguishable without duplicating both everywhere.

Use one modest scenario JSON Schema and a small reference/completeness validator.
Validate unique IDs, required fields, valid earlier prerequisites, file/check/source
references, profile selections including required supporting work, and capability coverage. Do not add a general DAG
scheduler, coverage-expression language, or typed event schema family. Validators
cannot decide whether an expectation or oracle is meaningful; review does that.

## Run files

| File | Minimum content |
|---|---|
| `run.json` | Immutable run ID, profile, identities, initial inputs, authority, deadline, capability probes |
| `results.jsonl` | Append-only records: result ID, step/check ID, timestamp, attempt, status, expected/observed outcome, evidence references; producing-step identities and recovery/correction links when applicable |
| `findings.jsonl` | Original findings and appended status updates, stable source IDs, kind/classification, original text, result links, evidence and reproduction data as appropriate |
| `resources.json` | Current explicit ownership and intended/actual cleanup disposition; sufficient identification to reconcile interrupted creation |
| `evidence/` | Immutable artifacts, logs, traces, diffs, reports, case manifests, and hashes where artifact identity matters |
| `summary.md` | Profile verdicts, tested identities, operational completion, missing/failed work, findings, deviations, and cleanup |

The containing directory supplies the run ID to result/finding records; external
references use run ID plus record ID. Common release/image inputs need not repeat
on every result. Changes created by steps are recorded as outputs, not by mutating
the original run declaration. Retain evidence outside disposable Project state.

The summary is a view of these records, not a competing source of truth. Consumers
can read the small files directly; event replay, supersession projection engines,
and digest-bound report generation are not prerequisites. Keep original observations
and explicit correction links visible. Findings must be usable directly by Consolidate
Findings without a separate Booley Feedback export.

## Implemented structural contract

[scenario.schema.json](scenario.schema.json) is the structural authority. Each
scenario supplies `title`, ordered `phases` (`id`, `title`, `minutes`), and `budget`
with deadline, contingency, cleanup minutes and cleanup start. Phase minutes include
the cleanup phase; contingency is counted once in addition. Taxi names a separate
`final` phase and latest final-regression start. Step prerequisites name earlier
check IDs, not recovery points or future outcomes.

Inputs are named records (`id`, `kind`, `value`, `source`, `verification`). `git`
and `sha256` inputs require full literal lowercase hashes. `pre-run` inputs describe
an exact identity that must be selected and recorded before execution; this never
permits changing an already pinned IP, workload or threshold.

Assets carry a scenario-relative contained file `path`, explicit `audience`, and
an optional SHA-256 checked against current bytes. Production assets record their
digests. Templates must list permitted `substitutions`: `run_root`, `artifact_root`,
`ticket_id`, `commit_id`, `release`, `provider`. An unlisted `{{variable}}` fails
validation; these substitutions cannot alter thresholds or disclose private assets.

A restoration step's `recovery` record names `baseline`, prior `detection` check IDs,
and its `instruction`. It requires its baseline, and cannot depend directly or
transitively on the detection's successful result. Cleanup has independent reachability.

The validator is standalone so authoring does not import the system under test.
Its strict metadata checks reject unknown coverage/profile fields without adding
another persistent schema family. HTTPS authorities are syntax-checked offline;
review and release-matched execution must verify their actual content and currency.
