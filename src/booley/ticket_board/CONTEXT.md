# Ticket Board glossary

This is the canonical vocabulary for Ticket authoring, execution state, and
acceptance. Shared Booley concepts such as **Target**, **Booley Flow**,
**Developer Agent**, and **Harness** are defined in the
[shared glossary](../../../docs/CONTEXT.md).

## Language

### Board and Tickets

**Ticket Board**:
The durable lifecycle authority for Tickets from authoring through execution,
review, completion, or archival.
_Avoid_: bare "Board", kanban, tracker, backlog

**Ticket**:
A self-contained unit of hardware development work carrying its own Criteria
and lifecycle state.
_Avoid_: task, issue, story

### Authoring

**Ticket Creation Guidance**:
Project-authored policy that guides the Criteria, Target lifecycle, and
successful-run disposition chosen while drafting a Ticket.
_Avoid_: Ticket Creation Defaults, ticket format, user preferences, runtime defaults

**Scope**:
The files a Ticket plans to change.
_Avoid_: allowlist

**Target Plan**:
A Ticket's intended acceptance disposition for its authored Targets:
New, Replacement, or Temporal.
_Avoid_: Target removal list, Target metadata, build migration

**New Target**:
A Ticket-authored Target intended to remain independently selectable after
Ticket acceptance.
_Avoid_: Persistent Target, permanent Target, default Target

**Replacement Target**:
A Ticket-authored Target intended to supersede one runnable baseline Target at
acceptance.
_Avoid_: modified Target, in-place Target edit, temporary Target

**Temporal Target**:
A Ticket-authored Target that exists only to collect one Ticket's evidence.
_Avoid_: Ephemeral Target, disposable config, temporary persistent Target

**Ticket Workspace**:
The disposable checkout set used for Ticket authoring or Developer Agent
execution.
_Avoid_: permanent worktree, ticket sandbox, integration checkout

**Acceptance Basis**:
The immutable authored inputs and repository identities governing one
executable version of a Ticket.

**Basis Refresh**:
A replacement Acceptance Basis for an unchanged waiting Ticket after its
dependencies are accepted.
_Avoid_: Target Contract, target snapshot, config patch, mutable recipe

**Ticket Amendment**:
A Human-approved relaxation of Criteria or expansion of Scope on a blocked
Ticket, published as a new immutable Acceptance Basis while preserving its
implementation and the earlier basis.
_Avoid_: Ticket editing, reset, fresh authoring

### Execution and evidence

**Criterion**:
A named boolean condition in a Ticket's acceptance state, bound to the
evidence and subject it evaluates.
_Avoid_: check, gate, acceptance test

**Cycle Count Criterion**:
A Criterion requiring one named test to pass on one Target and its reported
cycle count to meet one declared threshold.
_Avoid_: cycle budget, synthesis criterion, benchmark score

**Escalation**:
A signal that a decision exceeds the current authority level and must move
from Specialist to Developer Agent to Human.
_Avoid_: spec gap, blocker, impediment

**Developer Report**:
A final report produced by the Developer Agent summarizing its changes, the
Booley Flows and Specialists it used and why, remaining uncertainties, and
required justifications.
_Avoid_: Execution Rationale, skipped-Flow audit, mandatory route log

**Review Inspection**:
An immutable, Ticket Board-selected view of one Ticket execution, including its
Acceptance Basis, Criteria state, participant heads, and accepted or unaccepted
disposition. Review artifact generation renders this selection but does not own it.
_Avoid_: review session, mutable report state

**Requested Review**:
An explicit unaccepted transition from blocked to review that preserves unmet
Criteria so a Human can inspect or run further Ticket-bound verification.
_Avoid_: manual acceptance, forced handoff

### Acceptance and completion

**Criteria Satisfaction Record**:
The immutable record created when a Ticket satisfies its required Criteria,
freezing the final state of all Criteria together with their evidence,
Acceptance Basis, and repository identities.
_Avoid_: Acceptance Evidence, Acceptance Snapshot, final booley_state, cached
status, review report

**Acceptance Journal**:
The recoverable record and authority for publishing and cleaning up an
accepted, basis-bound Ticket.
_Avoid_: merge log, rollback record, transaction database

## Retired terminology

- **"abandoned" / "failed"**: Removed Ticket states. Use **archived** for a
  Ticket that will not be completed.
- **"effort"**: Deprecated Ticket resource hint. Workflow Regions and
  Specialist selection do not derive from it.
