# Target Plan implementation plan

## Objective

Replace user-authored `on_success.remove_targets` with an optional,
machine-readable `target_plan`. Most Tickets omit the field and use existing
Targets unchanged. A Ticket that authors Targets must classify every new
definition and commit the approved `.core` and owned `tests.toml` changes in
its Ticket Workspace before enqueue.

The design is fixed by
[ADR 0060](../adr/0060-model-target-changes-with-ticket-target-plans.md).
This plan describes the implementation order and acceptance tests; it does not
reopen the decisions recorded there.

## Contract

### Authored schema

`target_plan` is an optional top-level Ticket field. When present, it is a
nonempty list of discriminated entries:

```yaml
target_plan:
  - target: lint_style
    role: persistent

  - target: sim_core_v2
    role: replacement
    replaces: sim_core

  - target: lint_negative_control
    role: ephemeral
```

Each entry has exactly these rules:

| Role | Required keys | Accepted Project result |
|---|---|---|
| `persistent` | `target`, `role` | Keep the new Target alongside the existing Target surface. |
| `replacement` | `target`, `role`, `replaces` | Keep `target`; remove the runnable baseline named by `replaces`. |
| `ephemeral` | `target`, `role` | Remove the new Target. |

`replaces` is required for `replacement` and forbidden otherwise. Unknown
keys and roles are errors. Selectors must be nonempty strings and resolve
uniquely when the Acceptance Basis is published. A Target may occur as an
authored `target` once, a
baseline may be replaced once, self-replacement is invalid, and the directed
replacement graph must be acyclic.

Omission means that the current Ticket authors no Target definitions or
Target-owned test tables of its own. Internally materialized provider Targets
do not count as current-Ticket authoring. `target_plan: []` is invalid rather
than a second spelling of omission. Any nonempty Target Plan requires
`on_success.merge: true`.

### Selection policy

The post-acceptance Target surface decides the role:

- Use `persistent` only when the new build and every existing build remain
  intentionally supported and independently selectable.
- Use `replacement` when a new recipe supersedes one existing build and the
  old recipe should disappear after acceptance.
- Use `ephemeral` only when ticket-specific evidence cannot be obtained from
  a persistent or replacement Target.

Different Flow families, EDA tools, toplevels, testbenches, build-time
parameter or define values, FPGA parts or constraint sets, and cocotb modules
presume a persistent addition when both builds remain supported. Source-list,
test-list, option, waiver, tool, toplevel, parameter, constraint, or naming
changes presume replacement when the old form is retired. Explicit Ticket
intent may reverse either presumption; coexistence after acceptance is the
authoritative test.

A new test, threshold, source file, or submodule does not by itself justify a
new persistent Target. Classic HDL test functions normally share the existing
simulation Target. A second cocotb module remains a mechanical exception
because `cocotb_module` is a per-Target option.

### Authoring boundary

Ticket creation may write only:

- the Ticket;
- Target definitions named by its Target Plan;
- each planned Target's unambiguously owned `tests.toml` table; and
- zero-byte placeholders for Scope paths marked `[new]`.

It must not edit or delete an existing Target definition in place. It must not
author RTL, HDL or Python testbenches, firmware, scripts, constraints,
generators, hooks, or other implementation/support content. A planned Target
may reference existing protected controls, or missing RTL/TB paths declared
Scope `[new]` where the existing Acceptance Basis rules permit that. If basis
publication would require other content, ticket creation reports a blocker.

The ticket creator opens the Ticket Workspace, applies the approved plan, and
`enqueue` validates and commits that authoring state while publishing the
Acceptance Basis. A plan is not satisfied merely because a matching Target
already exists on the destination branch: planned `target` entries must be
definitions newly authored by this Ticket.

### Acceptance Basis publication invariants

At enqueue, compare canonical Target declarations and owned test tables in
the authoring worktree with the exact destination baseline. Enforce that:

1. every current-Ticket-authored Target is represented by exactly one Target
   Plan entry, while every provider-materialized Target matches a pinned
   provider binding;
2. every planned `target` is a current-Ticket-authored Target;
3. no existing Target was modified or deleted directly;
4. persistent and ephemeral entries have no baseline;
5. each replacement baseline exists on the destination baseline;
6. all planned Targets and replacement baselines are bound by Ticket Criteria;
7. replacement baselines resolve and are runnable enough to collect evidence;
8. Target-owned test-table additions and changes match the approved plan;
9. omission of `target_plan` accompanies no current-Ticket-authored Target or
    owned-test-table delta;
10. the authoring repositories contain no other non-placeholder changes; and
11. the published participant commits contain every validated authoring change.

"Same build intent" remains a ticket-creation and approval judgment. Machine
validation enforces structural consequences but does not pretend to infer product
intent from `.core` syntax.

### Derived acceptance behavior

The authored plan is the sole source for Target disposition. Acceptance Basis
publication resolves its selectors to canonical identities and derives the
internal removal set:

```text
replacement entries -> remove each `replaces` Target
ephemeral entries   -> remove each `target` Target
persistent entries  -> remove nothing
```

Store the normalized Target Plan, derived removal set, and internal provider bindings
in its committed Acceptance Basis record. Keep Ticket frontmatter as the minimal
`schema` plus `participants` pointer. Acceptance continues to remove only the selected Target definition
and its unambiguously owned test table; shared filesets, parameters, sources,
constraints, generators, and hooks remain. Completion reads the removal set
from the Acceptance Basis, never from `on_success`.

`on_success.remove_targets` is retired without compatibility. Validation must
reject the field wherever the Ticket lives—draft, waiting, queued, active,
blocked, review, done, or archived—and no execution, completion, or migration
path may silently translate it. The CLI no longer accepts or emits the key.

## Planned Target dependencies

### Export rules

An eligible basis-published provider in `waiting`, `queued`, `running`, `blocked`, or
`review` exports only:

- a `persistent` entry's `target`; and
- a `replacement` entry's `target`.

It never exports an `ephemeral` Target or a replacement baseline. A consuming
Ticket names the provider in its ordinary `dependencies` field. Human ticket
creation proposes the dependency; agent mode reports an error if the caller
omits it. If several active Tickets offer the same selector, or one offers a
selector another removes, creation fails as ambiguous.

The provider relationship is internal. Do not add it to the authored Target
Plan or show it in the approval artifact, reports, or normal ticket output.
The existing `dependencies` field remains visible.

### Prospective basis validation

To validate a consumer before its provider completes:

1. read the provider's published Acceptance Basis and Target Plan;
2. materialize the exact provider-owned Target/control surface in the
   consumer's Ticket Workspace;
3. validate the consumer's Criteria against that future surface;
4. pin the provider Ticket, exported canonical Target identity, exported role,
   and normalized surface identity in machine-owned Acceptance Basis data; and
5. exclude provider entries from the consumer's own Target Plan accounting.

Materialization may expose other declarations that share a `.core` file, but
validation must reject any consumer Criterion that binds a non-exported Target.
The later refresh prevents provider-only or ephemeral declarations from
leaking into the accepted Project through the consumer branch.

### Pre-execution refresh

After all dependencies reach `done`, but before the consumer's first Developer
Agent starts:

1. verify each provider published the pinned export;
2. reconstruct the still-untouched consumer Ticket Branch from the current
   destination branches;
3. verify the accepted provider Target/control surface is semantically
   identical to the pinned surface;
4. reapply the consumer's approved Target Plan and placeholder changes;
5. rerun full Ticket and Acceptance Basis validation;
6. create fresh participant commits and publish a replacement Acceptance Basis
   as a Basis Refresh, retaining the previous basis and receipt; and
7. publish the new Board pointer and waiting-to-queued transition as the same
   crash-recoverable transaction.

No user confirmation is required when the provider surface and the consumer's
approved plan are unchanged. A missing or materially changed export, dirty or
already-executed consumer workspace, ambiguous provider, or failed reapply
blocks with `acceptance-input-change-required` and uses the existing
return-to-draft recovery path.

A provider may itself publish a pre-execution Basis Refresh. Its accepted basis
identity may therefore differ from the basis observed by the consumer at creation;
the pinned exported role and normalized Target/control surface, rather than basis-ID
equality, decide whether the downstream refresh is unchanged.

Many consumers may depend on one export. A later Ticket may replace an earlier
Ticket's replacement candidate, producing an ordered replacement chain. Two
sibling Tickets cannot both replace the same eventual baseline: ticket
creation must order them into a chain or ask which transition wins.

## User experience

### Ticket creation

Update `booley-ticket-create` to inspect existing and active planned Targets
before proposing any new Target. It should prefer omission of `target_plan`,
then replacement, and choose a persistent addition only when both recipes must
survive. Ephemeral Targets are the last resort for otherwise unobtainable
evidence.

Project Ticket Creation Guidance may influence `criteria`, `target_plan`, and
`on_success`, but it cannot bypass validation or approval. Explicit current-
Ticket instructions remain authoritative.

### Approval artifact

Replace **New Targets** with **Target Plan**. If no plan exists, show
`Target Plan: none`.

For `persistent` and `ephemeral`, show:

- role, name, destination file, and acceptance result;
- the complete Target definition; and
- the complete owned `tests.toml` table, when present.

For `replacement`, show:

- baseline and candidate selectors, candidate destination file, and the
  acceptance result;
- a focused Target-definition diff against the baseline; and
- a focused owned-test-table diff against the baseline table.

If a focused replacement diff cannot be produced safely, stop with an approval
blocker. Shared or ambiguous table ownership is likewise a blocker. Any edit
returns to the full approval gate.

Provider pinning and materialization are absent from the artifact. The full
Ticket already exposes its normal dependency list.

## Implementation slices

### 1. Domain model and parsing

- Add immutable `TargetPlanEntry`, `TargetPlan`, and role types below the
  Ticket Board/Harness boundary.
- Add `target_plan` to known Ticket fields and implement strict discriminated
  parsing and validation.
- Remove `remove_targets` from `OnSuccess`, its JSON parser, CLI flags, help,
  formatting, fixtures, and defaults.
- Emit a targeted hard-cutoff diagnostic for nested
  `on_success.remove_targets` rather than treating it as a generic unknown key.
- Add `--target-plan` JSON/file input to `create-file`; omission must stay
  omission in generated frontmatter.

Primary modules:

- `src/booley/core/models.py`
- `src/booley/ticket_board/constants.py`
- `src/booley/ticket_board/validation.py`
- `src/booley/ticket_board/cli.py`
- `src/booley/ticket_board/cli_handlers.py`
- `src/booley/ticket_board/io.py`

### 2. Target-surface differ and Acceptance Basis validation

- Replace the current changed-`.core` approximation—which treats every Target
  in a changed file as changed—with a semantic destination/candidate Target
  differ.
- Classify added, modified, and deleted canonical Target identities and
  correlate owned test-table changes.
- Validate exact Target Plan coverage and reject in-place mutation/deletion.
- Canonicalize the plan, derive removals, and include both in a new Acceptance
  Basis schema revision.
- Keep internal removal data immutable after publication.

Primary modules:

- `src/booley/ticket_board/acceptance_basis.py`
- `src/booley/ticket_board/acceptance_targets.py`
- `src/booley/ticket_board/workspace_ops.py`
- `src/booley/ticket_board/target_finalization.py`
- `src/booley/fusesoc/fusesoc_registry.py`

### 3. Acceptance cutover

- Make completion and the Acceptance Journal consume only the published
  Acceptance Basis's derived removal set.
- Retain the narrow, source-span Target/test-table finalizer, but rename APIs
  and diagnostics away from the retired user field.
- Reject every old Ticket carrying `on_success.remove_targets`; do not
  translate, repair, or complete it automatically.
- Verify crash recovery pins the same derived removals across retries.

Primary modules:

- `src/booley/ticket_board/completion.py`
- `src/booley/ticket_board/acceptance_journal/`
- `src/booley/ticket_board/target_finalization.py`
- `src/booley/ticket_board/operations.py`

### 4. Provider composition and refresh

- Discover exportable Target Plan entries only in eligible basis-published Tickets during ticket
  creation and dependency scanning.
- Add internal provider bindings to the Acceptance Basis without adding a
  user-authored field or user-facing output.
- Materialize pinned provider surfaces for prospective basis validation.
- Add a pre-execution refresh operation for untouched waiting Tickets and run
  it before waiting-to-queue promotion or intake.
- Verify provider content, reconstruct from current destinations, reapply local
  authoring changes, republish the basis, and fail closed on drift.
- Validate fan-out consumers, replacement chains, ambiguous providers, sibling
  replacement conflicts, provider reset/republish, failure, and archive cases.

Primary modules:

- `src/booley/ticket_board/acceptance_basis.py`
- `src/booley/ticket_board/acceptance_targets.py`
- `src/booley/ticket_board/workspace_ops.py`
- `src/booley/ticket_board/basis_publication.py`
- `src/booley/ticket_board/enqueue_publication.py`
- `src/booley/ticket_board/operations.py`
- `src/booley/ticket_board/io.py`
- `src/booley/harness/setup/intake.py`

### 5. Skill and documentation cutover

- Update `booley-ticket-create` inference, detailed grilling, agent mode,
  approval gate, CLI workflow, Criteria rules, and Ticket Creation Guidance
  authority.
- Update its ticket template to show optional `target_plan` and the four-field
  `on_success` mapping.
- Update user and implementation documentation to use Target Plan terminology
  and remove the old authoring surface.
- Regenerate or update bundled references that repeat Ticket fields.

Primary files:

- `src/booley/data/skills/booley-ticket-create/SKILL.md`
- `src/booley/data/skills/booley-ticket-create/TICKET_TEMPLATE.md`
- `src/booley/data/skills/booley-ticket-create/grilling.md`
- `src/booley/data/skills/booley-ticket-create/TICKET_CREATION_TEMPLATE.md`
- `docs/user/USAGE.md`
- `docs/internals/FLOW_IMPLEMENTATION.md`
- `docs/CONTEXT.md`

## Verification matrix

### Schema and cutoff

- Ticket without `target_plan` and without Target authoring validates.
- Present empty plan, malformed entry, unknown role/key, missing/extra
  `replaces`, duplicate/cyclic relationship, or `merge: false` fails.
- Any `on_success.remove_targets` value—including `[]`—fails at create,
  validate, enqueue, intake, and completion boundaries.
- CLI generation omits `target_plan` by default and round-trips every valid
  role when supplied.

### Acceptance Basis behavior

- Each role publishes with exact canonical identities and derived removals.
- Missing, extra, modified, or deleted Target definitions fail plan coverage.
- Changed `.core` files containing untouched sibling Targets do not
  misclassify those siblings as authored.
- Owned tests tables publish; unplanned or ambiguously owned changes fail.
- Replacement baseline existence, resolution, Criterion binding, and minimum
  runnability are enforced.
- A planned candidate may defer only already-supported Scope `[new]` RTL/TB
  paths.

### Acceptance

- Persistent Target remains beside the old surface.
- Replacement baseline is removed and candidate remains unchanged.
- Ephemeral Target is removed.
- Mixed plans derive and apply the exact union of removals.
- Multi-repository acceptance, review correction, retries, and cleanup remain
  recoverable and idempotent.

### Planned dependencies

- A consumer can publish against a provider's persistent or replacement Target
  and automatically gains/requires the dependency.
- Ephemeral Targets and replacement baselines cannot be consumed.
- Fan-out consumers are valid; ordered replacement chains are valid.
- Ambiguous providers and unordered sibling replacements fail.
- Successful dependency completion refreshes and republishes the consumer against
  the accepted implementation without another approval.
- Missing or changed exports and non-pristine consumers block without losing
  recoverable work.

### Skill contract

- Ordinary tickets show `Target Plan: none` and author no Target controls.
- Persistent and ephemeral entries show full definitions and owned tables.
- Replacement entries show focused definition/table diffs and explicit
  acceptance effects; an unsafe or ambiguous focused diff blocks approval.
- Agent mode rejects incomplete plans and missing provider dependencies.
- Project Ticket Creation Guidance can influence a plan but cannot bypass the
  plan or approval validators.

## Completion criteria

The implementation is complete when all old `remove_targets` authoring and
runtime paths are gone, every new Target authored by ticket creation is covered
by a Target Plan in its published Acceptance Basis, acceptance applies only
derived transitions, planned
Target dependencies refresh safely before first execution, bundled/user docs
describe one coherent model, and the full test suite plus
`ruff check src/ tests/` pass.
