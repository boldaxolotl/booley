# Production encoding and review contract

Draft for [Assemble the public Booley QA suite implementation handoff](https://github.com/boldaxolotl/booley/issues/376).
This specifies later implementation. It does not supply production scenarios,
a validator, an evaluator, or an execution runner.

## Exact file change set

| File | Implementation responsibility |
| --- | --- |
| `qa/README.md` | Entry point, execution prerequisites, links to the three scenarios and named qualification profiles; distinguish design availability from executed qualification. |
| `qa/PROTOCOL.md` | Retain shared execution/evidence/recovery/cleanup rules; make the approved companion-Project authority and product-feedback probe boundary explicit by reference. |
| `qa/AUTHORING.md` | Link the validator command and complete worked example; require review of expectation authority, prerequisite completeness, profile selection, and budgets. |
| `qa/QUALIFICATION.md` | Retain verdicts; link the explicit production profile definitions and the unavailable GUI/client requirements. |
| `qa/FORMAT.md` | Incorporate the field contract below and distinguish check prerequisites from phase recovery points. |
| `qa/coverage.yaml` | Encode all 62 inventory entries with public contract sources and current supported boundaries, plus the separately identified EDA integrations. Contains no scenario assignments. |
| `qa/profiles.yaml` | Encode every named required/optional run, selected check ID, supporting step, prerequisite capability, and explicit platform exclusion from the reviewed handoff. |
| `qa/scenario.schema.json` | One JSON Schema for `format_version: 1` scenarios; structural validation only. |
| `qa/validate.py` | Small standard CLI for structural, reference, coverage, profile, and budget validation. No runner, execution, scheduling, or qualification database. |
| `qa/requirements-validation.txt` | Exact reviewed pins for PyYAML and jsonschema needed by the validator; choose supported versions when implementation begins. |
| `qa/scenarios/picorv32/scenario.yaml` | Encode the PicoRV32 catalogue, applicable inventory probes, budgets, and clean/fault/recovery ordering. |
| `qa/scenarios/picorv32/prompts/interactive.md` | Accepted Interactive Mode prompt, without leaking future hidden fault information. |
| `qa/scenarios/picorv32/tickets/continuity.md`, `tickets/evolution.md` | Complete accepted Ticket Create payloads and explicit substitutions for run-owned identities. |
| `qa/scenarios/taxi/scenario.yaml` | Encode the Taxi catalogue and explicitly named submodule companion phase in the same run. |
| `qa/scenarios/taxi/prompts/setup.md`, `prompts/interactive.md` | Accepted Setup and Interactive Mode intents. |
| `qa/scenarios/taxi/tickets/observability.md`, `tickets/repair.md` | Complete Verification and Bug Fix Ticket Create payloads. The repair payload omits the seed location and repair. |
| `qa/scenarios/taxi/fixtures/submodules.md` | Exact companion topology, fixture construction recipe, local commit identities, negative variants, authority, evidence, and cleanup from the submodule design. |
| `qa/scenarios/uart/scenario.yaml` | Encode the UART catalogue, approved documentation packet, developer/evaluator separation, bounded repairs, and external-image lifecycle check. |
| `qa/scenarios/uart/prompts/setup.md`, `prompts/interactive.md`, `tickets/feature.md` | Accepted greenfield and Feature Ticket contracts; no evaluator implementation or private tests in developer prompts. |
| `qa/scenarios/uart/spec/` | Frozen public documentation packet and the exact reviewed executable-contract inputs named in the UART catalogue; source identities and license attribution. |
| `qa/scenarios/uart/evaluator/` | Independent evaluator implementation, case manifest, source-isolation contract and evaluator validation assets specified by the UART design. This is later implementation, not part of this planning change. |
| `qa/scenarios/<scenario>/probes/` | Scenario-owned small fixtures for the explicitly allocated inventory checks. Each concrete path must be listed by its owning step; this directory is not permission to invent new exercises. |
| `qa/examples/` | Keep clearly illustrative; update references to the finalized schema and explain negative-check pass, recovery preserving failure, missing results, unavailable GUI, flaky results, and cleanup verdicts. |
| `tests/qa/test_validation.py` | Focused validation tests for malformed references/profiles and lost coverage; fixtures target real failure modes, not a duplicate implementation. |
| `qa/HANDOFF.md`, `qa/handoff/` | Preserve the reviewed migration record and source decision links. Mark implementation items completed only with the actual corresponding change. |

Directory contents for payloads and evaluator assets come from the journey catalogue,
not a guessed filename in an executor prompt. Implementation may rename descriptive
asset files in review while updating every reference; content and disclosure
boundaries are binding. Do not turn the list into several independent schema
families or a plugin/runner project.

## Scenario field contract

Use readable YAML with block scalars for prose. Reject unknown structural keys to
catch misspellings. The schema's field definitions are authoritative; documentation
explains them without adding hidden required keys.

| Field | Shape and rule |
| --- | --- |
| `format_version` | Integer, exactly `1`. |
| `scenario_id` | Stable ID from the accepted journey. Public check IDs are `<scenario_id>.<check_id>`; the migration catalogue prints those exact full identities. |
| `title`, `source` | Human title and canonical design-resolution URL. |
| `inputs` | Named entries with kind, exact authored value or required pre-run selection, source, and verification/evidence instruction. Pins already selected by the designs are literal; Booley release is an exact published release selected before the run. No floating IP refs. |
| `authority` | Explicit permitted actions/resources, source-edit scope, accepted/discarded output disposition, secret-handling restrictions and prohibited actions. An empty grant permits nothing. |
| `budget` | Deadline minutes, cleanup reserve, contingency minutes, latest final-regression start where applicable; phase allocations whose total includes every required exercise. |
| `phases` | Ordered named phase IDs and work-minute caps; steps name their phase. Recovery and allowed retries consume that phase's allocation plus remaining contingency, never a second hidden budget. |
| `steps` | Ordered list of the following step records. |

Each step contains `id`, `phase`, `action`, `checks`, and where relevant
`requires`, `authority`, `timeout_minutes`, `retry`, `recovery`,
`recovery_point`, `resources`, and `assets`. Step `requires` contains **earlier
check IDs**, including IDs belonging to the same earlier step. Never mix step IDs
and check IDs in that list. `recovery_point` names saved source and evidence
identities; it does not license skipping work after a coordinator restart.

Each check contains `id`, `capabilities`, `stimulus`, `expected`, `authority_ref`,
`evidence`, and `capture`. Optional `navigation_refs` identify how the executor
finds the documented operation. `expected` describes a falsifiable result;
`evidence` specifies artifact content and identity, not only a filename or exit
code. `capture` identifies the relative artifact destination. Check IDs are unique
within a scenario and remain unchanged across revisions of the same check.

Conditional execution means a declared capability probe or a failed prerequisite,
not a predicate that can quietly remove required work. Recovery/cleanup steps use
the earliest trustworthy prerequisite, not the negative check that they must run
after even if it fails. Every seeded fault has independent baseline, detection,
restoration and restored-behavior checks.

Asset references identify a relative path and disclosure audience: coordinator,
setup delegate, Interactive Mode delegate, Developer Agent, or independent
evaluator. Authored substitution variables are an explicit allowlist (run paths,
actual created Ticket/commit IDs, release/provider identity). Reject unresolved
variables. Do not use a template to let an executor select thresholds, omit tests,
change pins, or expose hidden seeds/evaluator material.

## Coverage and profile fields

`coverage.yaml` has `format_version` and `capabilities`: unique `id`, `title`,
public `sources`, supported `contract`, and `applicability`. Keep explicit
experimental/out-of-scope notes alongside relevant capabilities; those notes do
not satisfy supported outcomes. A source change that supersedes an old inventory
expectation must appear in the migration record with the old and replacement
check, never as an unexplained deletion.

`profiles.yaml` has `format_version` and named `profiles`. Each profile declares
`required` (boolean), its qualification claim, and explicit `runs`. Each run has
an ID, scenario ID, OS/architecture, native host execution requirement, provider,
Interactive Mode client kind, Ticket backend, capability probes, selected
scenario-qualified `checks`, `supporting_steps`, and explicit `exclusions` with
product applicability rationale. No wildcards, generic cross-product, automatic
prerequisite expansion, or profile membership duplicated on checks.

A combined run selecting core and GUI/client profiles executes their union once
with one deadline and ownership ledger; each profile retains its own verdict.
Standalone GUI/client qualification lists and reruns its complete supporting
setup/baseline work. Missing infrastructure never changes that declared list.

## Modest validation commands

The later implementation exposes these commands from the repository root:

```text
python -m pip install -r qa/requirements-validation.txt
python qa/validate.py
python qa/validate.py --scenario picorv32-published-demo-continuity
python qa/validate.py --coverage-index <output-path>
```

The first validation command checks the complete suite; the scenario filter is
an authoring aid and cannot confer whole-suite validation. Index generation first
validates and then emits a reverse capability-to-check/profile view derived from
scenario references and profile selections. It does not write assignments back
to the source inventory. Return 0 on successful validation and nonzero with
file/ID diagnostics on errors. None of these commands execute a scenario.

Validate: required types/fields; unique IDs; earlier check prerequisites and
absence of cycles through ordered steps; source/asset existence and contained
paths; exact authored pins; no unresolved substitutions; known capability IDs;
all required capability outcomes represented; complete explicit profile
selections including prerequisite/supporting work; valid exclusions; every phase
assigned and total budget at most 480 minutes including reserves; final-regression
and cleanup deadlines; fault recovery reachable after negative-check failure.

Deterministic validation cannot prove behavioral oracle quality, meaningful
workload coverage, supported-client evidence, safe fixture scope, or runtime
feasibility. Those remain review items. Rerunning a parser is not a scenario run.

## Review examples for result interpretation

| Observation | Required scoped verdict |
| --- | --- |
| Normal expected behavior with trustworthy artifacts | Check `pass`; profile passes only when all selected required work and cleanup pass. |
| Prescribed seed produces its prescribed failure | Detection check `pass`; retain the underlying failed product grade; restoration still required. |
| Unexpected failure followed by successful recovery | Original check `fail`, recovery recorded separately; affected profile remains failed. |
| Selected check has no record, malformed artifact, or timed out | Check `blocked`; profile incomplete unless another failure takes precedence. |
| Pre-run probe proves qualified GUI/client mechanism absent | Selected GUI checks `unavailable`; GUI profile incomplete; core assessed independently. |
| Capability disappears after pre-run availability | `fail` or `blocked` according to evidence, never retroactive `unavailable`. |
| Trustworthy pass and failure across permitted attempts | Retain both and a flaky Finding; affected profile failed. |
| Missing mandatory cleanup evidence | Profile incomplete, or failed if another failure takes precedence; report cleanup separately. |
| New trustworthy Booley/docs defect within profile scope | Profile failed even if no prewritten check predicted it. |
| Unclassified observation could invalidate profile | Profile incomplete until classification; do not manufacture a pass. |

The review must also trace every old independently observable requirement to
one or more named new checks and verify that any combined evidence still proves
each distinct outcome. A migration row is design evidence, never execution credit.
