# Ticket Board glossary

This is the canonical vocabulary for Ticket authoring, execution state, and
acceptance. Shared Booley concepts such as **Target**, **Booley Flow**,
**Developer Agent**, and **Harness** are defined in the
[shared glossary](../../../docs/CONTEXT.md).

## Language

**Ticket**:
A self-contained unit of hardware development work carrying its own Criteria and lifecycle state.
_Avoid_: task, issue, story

**Ticket Creation Guidance**:
Project-authored policy that guides the Criteria, optional Target Plan, and successful-run disposition chosen while drafting a Ticket.
_Avoid_: Ticket Creation Defaults, ticket format, user preferences, runtime defaults

**Criterion**:
A named, Target-bound boolean condition required for Ticket completion.
_Avoid_: check, gate, acceptance test

**Target Pair**:
A directed baseline/candidate pair of frozen Targets used by a baseline-relative Criterion.
_Avoid_: mutable Target, recipe patch, before/after config

**Target Plan**:
An optional Ticket-authored classification of proposed Targets as Persistent, Replacement, or Ephemeral.
_Avoid_: Target removal list, Target metadata, build migration

**Persistent Target**:
A Target Plan addition intended to remain independently selectable after Ticket acceptance.
_Avoid_: permanent Target, default Target

**Replacement Target**:
A Target Plan addition intended to supersede one runnable baseline Target at acceptance.
_Avoid_: modified Target, in-place Target edit, temporary Target

**Ephemeral Target**:
A Target Plan addition that exists only to collect one Ticket's evidence.
_Avoid_: disposable config, temporary persistent Target

**Acceptance Evidence**:
An immutable, completion-ordered record of one normalized Criterion outcome and its acceptance provenance.
_Avoid_: booley_state entry, raw Flow result, execution identity

**Acceptance Snapshot**:
The immutable projection of all selected Criteria when a Ticket crosses the acceptance boundary.
_Avoid_: final booley_state, cached status, review report

**Simulation Criterion**:
A Criterion whose evidence is a passing Simulation Flow result.
_Avoid_: optional sim, smoke test

**Cycle Count**:
A performance measurement emitted by one named test for one execution of its declared workload on a Target.
_Avoid_: cycle time, runtime, performance score

**Cycle Count Criterion**:
A Simulation Criterion combining a passing named test with declared Cycle Count thresholds for one Target.
_Avoid_: cycle budget, synthesis criterion, benchmark score

**Unaccepted Review**:
Human inspection of a Ticket whose work has not passed acceptance and therefore retains outstanding Criteria.
_Avoid_: forced acceptance, accepted hold

**Ticket Board**:
The durable lifecycle authority for Tickets from authoring through execution, review, completion, or archival.
_Avoid_: bare "Board", kanban, tracker, backlog

**Acceptance Basis**:
The immutable authored inputs and repository identities governing one executable Authoring Generation.

**Basis Refresh**:
A replacement Acceptance Basis for an unchanged waiting Ticket after its dependencies are accepted.
_Avoid_: Target Contract, target snapshot, config patch, mutable recipe

**Authoring Generation**:
One period of Ticket authoring that ends when its Acceptance Basis is published.
_Avoid_: seal generation, execution attempt, retry

**Ticket Workspace**:
The disposable checkout set materialized from an Authoring Generation's repository identities for authoring or Developer Agent execution.
_Avoid_: permanent worktree, ticket sandbox, integration checkout

**Acceptance Journal**:
The recoverable record and authority for publishing and cleaning up an accepted, basis-bound Ticket.
_Avoid_: merge log, rollback record, transaction database

**Scope**:
The files a Ticket plans to change.
_Avoid_: allowlist

**Escalation**:
A signal that a decision exceeds the current authority level and must move from Specialist to Developer Agent to Human.
_Avoid_: spec gap, blocker, impediment

**Runner**:
The Ticket Mode entry point that launches Developer Agents inside a Session Runtime and drives their Tickets through the Harness.
_Avoid_: launcher, executor

**Execution Rationale**:
A final-summary explanation of the Booley Flows, Specialists, and edits used to execute a Ticket, and why.
_Avoid_: skipped-Flow audit, mandatory route log

## Retired terminology

- **"abandoned" / "failed"**: Removed Ticket states. Use **archived** for a
  Ticket that will not be completed.
- **"effort"**: Deprecated Ticket resource hint. Workflow Regions and
  Specialist selection do not derive from it.
