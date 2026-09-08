# QA workflow simplification plan

Status: accepted for implementation, 2026-09-08. The shared design is implemented
in [qa/](../../qa/README.md); that contract and the updated Wayfinder issue bodies
supersede the earlier shared machinery. This file retains the rationale and plan.
Production encoding and full migration/feasibility reconciliation remain in #376.

## Objective and scope

Make the public QA suite straightforward for an agent to execute and a maintainer
to author, inspect, and revise. Preserve the three accepted project journeys and
their workloads: PicoRV32 published-demo continuity/evolution, Taxi port/evolution,
and OpenTitan UART clean-room greenfield.

This proposal changes shared execution bookkeeping, scenario representation,
coverage accounting, maintenance, and qualification reporting. It does not shrink
the hardware exercises, remove their checks, change the IP pins, relax thresholds,
replace the independent UART evaluator, reduce platform/provider commitments, or
implement a runner. Existing scenario feasibility questions remain explicit work
for the handoff; simplification must not conceal them.

Source map: [#246](https://github.com/boldaxolotl/booley/issues/246).
Remaining handoff: [#376](https://github.com/boldaxolotl/booley/issues/376).

## 1. Keep the essential execution rules

Keep these shared requirements in one short protocol:

- Freeze the exact published Booley release, suite commit, IP inputs, relevant
  images/tool versions, platform/provider, selected qualification profile, and
  pre-run authority. Record identities once per run; checks reference the run.
- Execute through published documentation/help and ordinary Project inspection.
  Capture an observation before consulting Booley source to classify it.
- Keep the coordinator/delegate arrangement. Record delegate identity and work
  assignment without introducing a delegation event protocol.
- Give every required check a stimulus, expected outcome, contract source, and
  concrete evidence requirement. Agent prose alone cannot prove product behavior.
- Capture failures before recovery. Recovery and retries never erase a trustworthy
  failure. Continue independent work only when its prerequisites remain sound.
- Preserve baseline → fault → observed failure → restoration checks for seeded
  faults. Retain the scenario-specific retry restrictions and time budgets.
- Register run-owned resources as they are created. Reserve time for cleanup on
  every exit path and retain cleanup evidence. Preserve pre-existing resources.
- Keep findings and artifacts available for direct Consolidate Findings use.
  Do not route them through Booley Feedback or capture secret values.

These rules provide the rigor. A large metadata model is not required to enforce
them.

## 2. Reduce the authored model

Authors work with a scenario, its ordered steps and checks, a capability inventory,
and named qualification profiles. A check is the existing Scenario Assertion;
this plan uses the shorter word without adding a second concept.

| Current machinery | Proposed v1 representation |
|---|---|
| Capability Group → Coverage Obligation → Coverage Cell → Allocation → Assertion | Capability inventory mapped directly to scenario checks and explicit profiles |
| Globally registered Verification Chain | Ordered fault/recovery steps with dependencies inside the scenario |
| Separate checkpoint catalogue with permanent references | Named phase recovery points beside the affected steps |
| Protocol path, identity, major version, stable rule references, exact digest | One suite commit freezes protocol and scenarios together; one format version identifies the file shape |
| Separate stimulus/expectation/evidence fields in obligations and assertions | Define those facts once, beside the check |
| Permanent global identities and successor registries for every entity | Stable scenario IDs and scenario-qualified step/check IDs; Git preserves revisions and removals |
| Per-cell invalidation declarations | Re-run affected scenarios under the required profiles |

Checks must still distinguish independently observable outcomes. Do not replace
specific success, rejection, failure, and restoration checks with a vague claim
such as “Ticket lifecycle covered.” Removing the obligation layer removes duplicate
representation, not test detail.

Keep a check ID through revisions to the same check; the suite commit distinguishes
its historical meaning. Assign a new ID to a different check and never repurpose
an old ID. Git history is sufficient for v1; a retirement registry is unnecessary.

Use an ordered step list with optional explicit prerequisites where continuation
requires them. A general workflow graph/scheduler is deferred. Each step references
shared defaults and supplies only its scenario-specific actions and exceptions.

## 3. Keep structured scenarios, simplify the file contract

Retain the already accepted Markdown/YAML division to avoid a wholesale rewrite:

```text
qa/
  PROTOCOL.md
  AUTHORING.md
  profiles.yaml
  coverage.yaml
  scenario.schema.json
  scenarios/
    picorv32/scenario.yaml
    taxi/scenario.yaml
    uart/scenario.yaml
```

The scenario YAML contains inputs, steps, checks, prerequisites, recovery points,
and scenario-specific authority, budgets, and cleanup. Explanatory prose remains
in YAML block scalars. Supporting prompts, Ticket payloads, and evaluator assets
remain separate files where that improves readability; reference rather than copy
them. This plan does not redesign those payloads or the evaluator.

Keep one modest scenario schema. Use a small validator for reference integrity,
required fields, profile membership, and coverage completeness. Do not require a
schema family for a generic event stream or a generic coverage-expression language.
Shared checks and evidence requirements belong in the protocol; the authoring guide
is a concise procedure and worked example, not a second governance specification.

Checks carry capability references. Generate the reverse capability-to-check index
from them. `coverage.yaml` holds the supported inventory and public sources. Checks alone
own capability references; `profiles.yaml` alone owns required run and check
selections. Neither repeats the other's assignments or check semantics.
Preserve source links and all existing coverage responsibilities during conversion.

## 4. Use a small run record rather than an event-sourced workflow

```text
<run-artifact-root>/<run-id>/
  run.json
  results.jsonl
  findings.jsonl
  resources.json
  evidence/
  summary.md
```

- `run.json`: immutable inputs, profile, authority, deadline, and run identity.
- `results.jsonl`: append-only check observations with step/check ID, attempt,
  status, timestamp, expected/observed outcome, and artifact references. Recovery
  observations identify the failure they follow. Corrections append a reference
  to the corrected record and justification.
- `findings.jsonl`: original findings and subsequent status updates, with stable
  source references and the fields needed by Consolidate Findings. Link to check
  results instead of repeating all run metadata. Retain friction, impressions,
  and wins where applicable; a passed check need not create a duplicate win record.
- `resources.json`: a current ownership/cleanup ledger. Persist ownership before
  the next dependent action. Cleanup results and evidence go in `results.jsonl`.
- `summary.md`: completion, qualification by profile, missing/failed checks,
  findings, deviations, and cleanup. It summarizes source records and is never
  an alternative source of results. A digest-bound report pipeline is deferred.

Use four check statuses:

| Status | Meaning |
|---|---|
| `pass` | Trustworthy evidence satisfies the expectation |
| `fail` | Trustworthy evidence contradicts the expectation |
| `blocked` | A required check lacks trustworthy evidence: prerequisite failure, timeout, operator/infrastructure failure, or invalid execution |
| `unavailable` | A pre-run capability probe established that the declared capability was absent |

Record a reason and issue/finding link where needed. A missing result for a selected
required check counts as blocked when finalizing the run. Loss of a capability after
it was declared available is fail or blocked, never retrospectively unavailable.
Classify product versus infrastructure findings in the record, rather than adding
another mandatory status hierarchy for every check.

A scripted expected rejection passes when the expected rejection occurs. A
trustworthy unexpected failure followed by recovery remains a failure for the run;
the recovery is separate evidence. A correction may fix a demonstrated recording
error but cannot erase a real failure.

Record deviations in the associated result/finding. If an alternative was explicitly
allowed, evaluate it normally. If it changes a required input, action, authority,
or evidence contract, the affected check cannot pass. Defer the generic three-class
deviation taxonomy.

## 5. Keep recovery points; defer general resume

Preserve the scenarios' named phase boundaries and the saved source state/artifact
references necessary for their fault/recovery exercises. Keep expensive baseline
artifacts and accepted commits so investigations remain practical.

V1 promises bounded continuation in the current run and cleanup, not arbitrary
restart of an interrupted coordinator. After interruption, preserve the partial
record, reconcile owned resources, and start a new run. Do not skip required checks
in that new run using old evidence. This intentionally trades some repeated work
for much less state-reconstruction machinery.

Revisit automated resume after real run data shows that repetition is a material
cost. It would then need explicit identity validation and resource reconciliation;
calling a saved directory a checkpoint does not provide those guarantees.

## 6. Make qualification scope explicit and fixed before execution

Report results against named profiles with enumerated required checks and runs.
Do not expand a generic dimensional matrix. Preserve the existing cadence: all
three scenarios with Codex on Ubuntu and native Windows, representative PicoRV32
Claude on Ubuntu, and Claude Windows when usage permits. Preserve Linux Vivado's
applicability and the existing EDA, image, Stealth, and mode responsibilities.

Define a core profile for runnable semantic/product checks and a GUI/client
integration profile for checks requiring qualified UI execution or visual evidence.
Separate the profile definition from the pre-run availability probe: an unavailable
dependency never silently removes a required check from its profile.

Actual VS Code client coverage requires evidence from the supported client. A
headless child with MCP access may prove semantic behavior but cannot receive
VS Code client credit. During migration, classify each Interactive Mode check by
what it actually proves. Keep supported-client and GUI requirements allocated and
visible even where they cannot currently execute.

Qualification rules:

- A profile passes only when every required check has trustworthy passing evidence
  from its required runs and cleanup is complete. A new trustworthy Booley/docs
  defect within that profile's scope prevents its pass even if discovered outside
  a prewritten check. Friction and impressions remain visible without failing it.
- A required failure makes that profile fail. Otherwise blocked or unavailable
  required work leaves it incomplete, with the reason displayed.
- Core may pass while GUI/client integration is unavailable. This is explicitly
  “core passed,” never unqualified “suite passed.” Full qualification requires all
  required profiles to pass.
- Operational completion remains a small run-summary field: completed, deadline
  reached, or operator error. It does not create a second per-check state model.
- Report useful counts and lists directly. Defer separate allocation, coverage,
  freshness, promotion, and green state machines.

Example report:

```text
Core — Ubuntu/Codex: passed
Core — Windows/Codex: passed
Compatibility — Ubuntu/Claude: passed
GUI/client integration: unavailable; no qualified observer/driver
Full qualification: incomplete
Cleanup: complete
```

This is a formatting example, not a claim that any qualification has run. Real
reports must identify scenario and release revisions and name the missing checks.

## 7. Replace assertion promotion with ordinary specification review

A check backed by published documentation or an accepted product decision is
mandatory as soon as its scenario change is reviewed. It does not need a successful
reference run before it can expose a defect. An uncertain expectation is a research
question/finding, not a candidate assertion requiring its own lifecycle.

Discovery and qualification use the same checks and continuation rules. Run purpose
may remain a descriptive label; it does not change which outcomes count. Preserve
unexpected observations and investigate them without maintaining separate candidate
and established assertion populations.

For maintenance:

1. Change the relevant scenario/check and capability mapping in one reviewed change.
2. Validate structure and review the expected behavior and evidence.
3. Re-run affected scenarios under the required profiles. Shared behavioral protocol
   changes require all affected scenarios; editorial changes require no rerun.
4. Record results against the exact released Booley and suite revisions. Historical
   evidence remains historical, with no automatic qualification carry-forward to a
   new release.

Use issue links to identify known defects. They remain failures. Defer a separate
Known Condition registry with owners, expiration, and disposition workflows. Preserve
every trustworthy retry failure and flag conflicting results as flaky. Resolving a
flake requires a documented cause/classification and targeted validation chosen in
review; remove the universal three-clean-runs-per-cell rule.

## 8. Revise the accepted decisions before the implementation handoff

Implementation updates the issue bodies with explicit superseding decisions while
preserving their earlier questions and discussion. Journey-specific requirements
remain binding. The remaining handoff is defined in [qa/HANDOFF.md](../../qa/HANDOFF.md).

| Decision | Required revision |
|---|---|
| [#249: execution contract](https://github.com/boldaxolotl/booley/issues/249) | Compact run files/results; phase recovery points; defer generic resumability and typed workflow events; simplify deviations/statuses |
| [#250: promotion and maintenance](https://github.com/boldaxolotl/booley/issues/250) | Specification-backed checks from first run; ordinary review; scenario-level reruns; issue-linked defects; scoped qualification |
| [#251: coverage accounting](https://github.com/boldaxolotl/booley/issues/251) | Direct capability/check mapping and explicit profiles; remove obligations/cells/chains as separately authored entities; separate GUI qualification |
| [#253: public format](https://github.com/boldaxolotl/booley/issues/253) | Simplified files/schema, references, IDs and run records; preserve structured scenarios |
| [#270: authoring](https://github.com/boldaxolotl/booley/issues/270) | Short authoring process and example; remove promotion, registry, and fine-grained invalidation governance |
| [#246: map](https://github.com/boldaxolotl/booley/issues/246) | Update shared constraints and decision pointers, including the precise qualification scope and resume promise |
| [#374](https://github.com/boldaxolotl/booley/issues/374), [#377](https://github.com/boldaxolotl/booley/issues/377), [#375](https://github.com/boldaxolotl/booley/issues/375): journeys | Preserve workloads; adapt shared-format references, records, recovery semantics, and profile allocation |
| [#376: handoff](https://github.com/boldaxolotl/booley/issues/376) | Replace exhaustive metadata reconciliation with check/evidence completeness, profile feasibility, and a lossless migration checklist |

Execution sequence after acceptance:

1. Publish the superseding shared decisions and revise #376's deliverables.
2. Prepare one small representative encoding for review: a normal check, a seeded
   fault/restoration sequence, and a GUI capability exclusion. Show the associated
   run records and profile report. Do not build a runner for this example.
3. Convert all three scenario designs, retaining their checks and responsibilities.
   Inventory every old assertion/obligation and mark it retained, combined into a
   named equivalent check, or moved to GUI/client qualification. No silent deletion.
4. Validate IDs/references, required profile assignments, evidence contracts,
   deadlines, continuation, and cleanup. Expose coverage gaps explicitly.
5. Complete the implementation-ready handoff. Encoding the production suite,
   implementing the evaluator/runner, and executing qualification remain subsequent
   work unless separately authorized.

## Acceptance criteria for the simplified handoff

- A maintainer can find a check's action, expectation, authority, evidence, and
  failure behavior together without traversing obligation/allocation registries.
- Every existing required behavior has a concrete check and profile assignment.
  The submodule coverage gap remains visible until actually resolved.
- A run can be assessed from its manifest, results, findings, and artifacts without
  replaying a workflow event stream or consulting a qualification database.
- All required unsupported/unavailable GUI/client checks are named. Core can pass
  without falsely claiming full qualification or supported-client coverage.
- A failed check, recovery, deadline, malformed result, new scoped defect, and
  incomplete cleanup each produce an unambiguous report without hiding evidence.
- Scenario workloads, independent evaluation, pre-run authority, clean-room rules,
  exact release identity, and fault/restoration requirements remain intact.
- Phase budgets are reconciled with the stated deadlines before declaring feasibility.
  The current PicoRV32 allocations total 10h20 and Taxi allocations 8h30 against
  eight-hour deadlines; this plan flags the discrepancy without choosing workload cuts.
- No runner, general resume engine, promotion registry, coverage-cell database,
  or event replay implementation is a prerequisite for the first suite execution.
