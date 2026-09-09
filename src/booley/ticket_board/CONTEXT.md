# Ticket Board glossary

This is the canonical vocabulary for Ticket authoring, execution state, and
acceptance. Shared Booley concepts such as **Target**, **Booley Flow**,
**Developer Agent**, and **Harness** are defined in the
[shared glossary](../../../docs/CONTEXT.md).

**Ticket**:
A self-contained unit of hardware development work that carries its own acceptance criteria and lifecycle state, expressed as a Markdown file with YAML frontmatter.
_Avoid_: task, issue, story

**Ticket Creation Guidance**:
Project-authored prose that guides the Criteria, optional **Target Plan**, and successful-run disposition chosen while drafting a Ticket. It augments Booley's built-in inference, yields to explicit instructions for that Ticket, is never read during execution, and never changes an existing Ticket.
_Avoid_: Ticket Creation Defaults, ticket format, user preferences, runtime defaults

**Criterion**:
A named boolean condition that must be satisfied for Ticket completion, bound to a **Target** by name, automatically invalidated when its dependency category (RTL, TB) changes. Tracks whether it was ever met across resets; any Flow requirement not enforced by the Harness itself must be expressed as an explicit Criterion.
_Avoid_: check, gate, acceptance test

**Target Pair**:
A directed baseline/candidate pair of frozen **Targets** used by a baseline-relative Criterion. A single Target name denotes the equal pair whose baseline and candidate are that Target.
_Avoid_: mutable Target, recipe patch, before/after config

**Target Plan**:
An optional, machine-readable **Ticket** transition plan that classifies every Target authored during Ticket creation as persistent, replacement, or ephemeral. Its absence means the Ticket authors no Target changes of its own; the dispositions published in the Ticket's **Acceptance Basis** determine which Targets remain in the accepted Project.
_Avoid_: Target removal list, Target metadata, build migration

**Persistent Target**:
A Target authored by a Target Plan as an additional supported build that remains independently selectable after Ticket acceptance.
_Avoid_: permanent Target, default Target

**Replacement Target**:
A Target authored by a **Target Plan** to supersede one runnable baseline Target. Both recipes remain available while the Ticket runs; acceptance removes the baseline and retains the replacement exactly as approved.
_Avoid_: modified Target, in-place Target edit, temporary Target

**Ephemeral Target**:
A Target authored by a Target Plan solely to collect one Ticket's evidence and removed during acceptance.
_Avoid_: disposable config, temporary persistent Target

**Acceptance Evidence**:
An immutable, completion-ordered record of one normalized Criterion outcome produced during Ticket execution. It identifies the Criterion and its baseline or candidate role, carries the effective result after aliases and thresholds are resolved, and retains execution and Acceptance Basis data as provenance; mutable runtime state is only a projection of these observations.
_Avoid_: booley_state entry, raw Flow result, execution identity

**Acceptance Snapshot**:
The content-addressed, immutable projection of all Criteria selected when a Ticket crosses the acceptance lifecycle boundary. Accepted review and done lifecycle readers use this snapshot for Criterion status while continuing to use live runtime data for operational history such as timeline and cost; a missing legacy snapshot is reported as unavailable, never as failed.
_Avoid_: final booley_state, cached status, review report

**Simulation Criterion**:
A Criterion satisfied by a passing simulation Booley Flow run. Any Ticket that authorizes RTL or testbench edits must include at least one Simulation Criterion; otherwise the Ticket shape is invalid before development. The required testbench may already exist or be created during ticket execution when Scope permits it.
_Avoid_: optional sim, smoke test

**Cycle Count**:
A non-negative integer emitted by one named test for one execution of its declared workload on a Target. It is a performance measurement whose desired direction is supplied by a Criterion; lower is not inherently better.
_Avoid_: cycle time, runtime, performance score

**Cycle Count Criterion**:
A specialized Simulation Criterion for one Target and named test, satisfied only when the test passes and its Cycle Count meets every declared threshold. A mandatory Cycle Count Criterion fulfills the simulation requirement for that test without requiring a duplicate Simulation Criterion.
_Avoid_: cycle budget, synthesis criterion, benchmark score

**Unaccepted Review**:
Human inspection of a Ticket whose work has not passed acceptance. It retains outstanding Criteria and supports human-directed verification before first acceptance; entering review alone never permits completion.
_Avoid_: forced acceptance, accepted hold

**Ticket Board**:
The filesystem-backed state machine that tracks one Ticket from draft through execution and review. Its normal route is draft → queued → running → review → done, with waiting and blocked as execution pauses; blocked work may explicitly enter unaccepted human review while retaining outstanding gates; review can instead archive the Ticket or explicitly reset it to a clean queued state, but never sends retained work back for partial rework.
_Avoid_: bare "Board", kanban, tracker, backlog

**Acceptance Basis**:
The immutable authored Ticket inputs and repository identities for one executable Ticket generation, published automatically when that Ticket is enqueued. It includes the canonicalized **Target Plan** and derived Target dispositions, and is the authority for execution, baseline comparison, protected acceptance controls, and completion.

**Basis Refresh**:
A recoverable, automatic replacement of an untouched waiting Ticket's **Acceptance Basis** after its dependencies are accepted. It rebases the unchanged approved authoring inputs onto current destinations, retains the old basis as evidence, and promotes the Ticket only when publication and the Board transition complete together. Drift requires a new **Authoring Generation** through `return-to-draft`.
_Avoid_: Target Contract, target snapshot, config patch, mutable recipe

**Authoring Generation**:
One draft period that ends when enqueue publishes an Acceptance Basis. Retry preserves the generation; returning a blocked Ticket to draft starts a new generation while retaining the old basis and evidence.
_Avoid_: seal generation, execution attempt, retry

**Ticket Workspace**:
The disposable checkout set materialized from a Ticket generation's repository refs for authoring or Developer Agent execution. Its outer and optional project-data worktrees may be destroyed and reconstructed; the Ticket Branch commits, not checkout paths, preserve the work.
_Avoid_: permanent worktree, ticket sandbox, integration checkout

**Acceptance Journal**:
The active deep module and recoverable record for accepting a basis-bound Ticket. It owns source preservation, candidate preparation and finalization, multi-repository publication, post-approval destination verification, and identity-checked cleanup, while the Ticket Board owns approval policy and the review-to-done transition. Its journal lets acceptance roll forward after interruption, keeps the Ticket in review until every destination ref has landed, and distinguishes an accepted Ticket whose recovery or cleanup is still pending.
_Avoid_: merge log, rollback record, transaction database

**Scope**:
The files a Ticket plans to change; ordinary changes outside this set are allowed and highlighted during review. The Developer Agent must justify every file in the final change set before submitting its run report; acceptance inputs and Harness bookkeeping remain protected independently of Scope.
_Avoid_: allowlist

**Escalation**:
A signal that a decision exceeds the current authority level, flowing Specialist to Developer Agent to Human. When the Developer Agent escalates, the Ticket moves to blocked on the Ticket Board.
_Avoid_: spec gap, blocker, impediment

**Runner**:
The CLI entry point (`booley run`) that drives Ticket execution inside the Session Runtime it is invoked from, launching a Developer Agent within the Harness for each selected Ticket. It works only inside a Session Runtime. Specific to Ticket Mode.
_Avoid_: launcher, executor

**Execution Rationale**:
A concise final-summary explanation of the Booley Flows and Specialists the Developer Agent used and the code edits it made, and why. It accounts for actions taken rather than requiring justification for every unused capability.
_Avoid_: skipped-Flow audit, mandatory route log

## Retired terminology

- **"abandoned" / "failed"**: Removed Ticket states. Use **archived** for a
  Ticket that will not be completed.
- **"effort"**: Deprecated Ticket resource hint. Workflow Regions and
  Specialist selection do not derive from it.
