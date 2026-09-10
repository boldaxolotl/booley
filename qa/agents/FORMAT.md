# Scenario and run format

The repository commit freezes the protocol, coverage, and Scenarios.
Use `format_version: 1` to identify the structured file shape. Keep stable Scenario
IDs and stable local Step/Check IDs. External references qualify a Check ID with its
Scenario ID. A revision to the same Check retains its ID; a different Check gets
another ID. Never repurpose IDs. Git provides history;
separate retirement and successor registries are unnecessary.

## Scenario files

Production layout:

```text
qa/
  agents/
    FORMAT.md
    PROTOCOL.md
    RUN.md
  user/
    AUTHORING.md
    QUALIFICATION.md
  coverage.yaml
  scenario.schema.json
  shared/probes/
  scenarios/<scenario-id>/scenario.yaml
```

An ordered step contains its ID, action, prerequisites where needed, checks,
scenario-specific authority, timeout/retry restrictions, recovery instructions,
owned resources, and named phase recovery point where useful. Shared behavior is
inherited from the protocol; override only to tighten it. Inputs and shared budgets
belong at scenario level. Keep lengthy prompts, Ticket payloads, and evaluator
material in referenced files rather than duplicating them.

Each Check has an ID, capability references, stimulus, expectation, public contract
source, evidence requirement, and capture point. Each Scenario owns named check sets
and the Configured Scenarios that select them. Keep navigation references where
the documentation path itself matters;
the run records the documentation actually consulted. Authority for expected behavior
and navigation guidance remain distinguishable without duplicating both everywhere.

Use one modest scenario JSON Schema and a small reference/completeness validator.
Validate unique IDs, required fields, valid earlier prerequisites, file/check/source
references, Configured Scenario selections including required supporting work, and capability coverage. Do not add a general DAG
scheduler, coverage-expression language, or typed event schema family. Validators
cannot decide whether an expectation or oracle is meaningful; review does that.

## Run files

| File | Minimum content |
|---|---|
| `run.json` | Unique execution ID, Configured Scenario ID, declared parameters, exact identities, initial inputs, authority, deadline, and pre-run observations |
| `results.jsonl` | Append-only records: result ID, step/check ID, timestamp, attempt, status, expected/observed outcome, evidence references; producing-step identities and recovery/correction links when applicable |
| `findings.jsonl` | Original findings and appended status updates, stable source IDs, kind/classification, original text, result links, evidence and reproduction data as appropriate |
| `resources.json` | Current explicit ownership and intended/actual cleanup disposition; sufficient identification to reconcile interrupted creation |
| `evidence/` | Immutable artifacts, logs, traces, diffs, reports, case manifests, and hashes where artifact identity matters |
| `summary.md` | Scenario Run Outcome, aggregate Qualification where applicable, tested identities, execution status, missing/failed work, findings, deviations, and cleanup |

The containing directory supplies the run ID to result/finding records; external
references use run ID plus record ID. Common product revision and Session Image inputs need not repeat
on every result. Changes created by steps are recorded as outputs, not by mutating
the original run declaration. Retain evidence outside disposable Project state.

The summary is a view of these records, not a competing source of truth. Consumers
can read the small files directly; event replay, supersession projection engines,
and digest-bound report generation are not prerequisites. Keep original observations
and explicit correction links visible. Findings must be usable directly by Consolidate
Findings without a separate Booley Feedback export.

## Implemented structural contract

[scenario.schema.json](../scenario.schema.json) is the structural authority. Each
scenario supplies `title`, ordered `phases` (`id`, `title`, `minutes`), and `budget`
with deadline, contingency, cleanup minutes and cleanup start. Phase minutes include
the cleanup phase; contingency is counted once in addition. Taxi names a separate
`final` phase and latest final-regression start. Step prerequisites name earlier
check IDs, not recovery points or future outcomes.

Inputs are named records (`id`, `kind`, `value`, `source`, `verification`). `git`
and `sha256` inputs require full literal lowercase hashes. `pre-run` inputs describe
an exact identity that must be selected and recorded before execution; this never
permits changing an already pinned IP, workload or threshold.

Assets carry a contained file `path`, explicit `audience`, and an optional SHA-256
checked against current bytes. `base` defaults to `scenario`; `base: shared` resolves
within `qa/shared/`. Both bases reject paths and symlinks that escape their allowed
directories. Each owning step explicitly lists shared assets, including any separate
shared contract it needs. Production assets record their digests. Templates must list permitted `substitutions`: `run_root`, `artifact_root`,
`ticket_id`, `commit_id`, `product_revision`, `provider`. An unlisted `{{variable}}` fails
validation; these substitutions cannot alter thresholds or disclose private assets.

A restoration step's `recovery` record names `baseline`, prior `detection` check IDs,
and its `instruction`. It requires its baseline, and cannot depend directly or
transitively on the detection's successful result. Cleanup has independent reachability.

Scenario check sets are flat and disjoint: each Check appears in one set. A
Configured Scenario may select several sets, which the validator concatenates into one
resolved Check list. It rejects missing sets, unused sets, duplicate Checks, and
unknown exclusions. Supporting Steps are derived from the selected Checks and
Scenario ordering.

Each Configured Scenario declares whether it is required, binds `host_os`,
`cpu_architecture`, `native_host`, `agent_provider`, `interactive_mode_client`, and
`ticket_mode_backend`, and lists its pre-run requirements, check sets, and justified
exclusions. These are required execution parameters; `run.json` records their actual
values and the exact identities observed during execution.

The validator is standalone so authoring does not import the tested Booley product revision
under test.
Its strict metadata checks reject unknown coverage, Scenario, and Configured Scenario fields without adding
another persistent schema family. HTTPS authorities are syntax-checked offline;
review and build-matched execution must verify their actual content and currency.
