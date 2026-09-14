---
status: accepted
---

# Record the Ticket Baseline at Enqueue with a Hard Cutoff

Executable Tickets need one authority for authored inputs, repository routing,
Target identities, reset, readiness, and completion. Booley records the baseline
commits and generation in the Ticket's machine-only frontmatter at enqueue.
Prepare-first journals and identity-checked ref updates publish the commits;
commit trailers anchor the Ticket metadata to the authoring lineage. The Ticket
is authoritative until it returns to draft or receives an approved amendment.

ADR-0060 adds one narrow exception: a pre-execution Basis Refresh may replace
the machine generation of an untouched waiting Ticket after its dependencies
are accepted, using a recoverable publication-plus-promotion transaction.

A second exception is a Human-approved Ticket Amendment for a blocked Ticket.
The Board records supported Criteria relaxations or Scope additions in a new
Ticket machine generation and pinned authoring commits. It retains the prior
execution history and joins the existing implementation to the new authoring
lineage. Developer execution cannot edit acceptance inputs or repair input drift.

The separate Acceptance Basis record and receipt, Target Contract fields, and
compatibility adapters are rejected rather than upgraded. Existing Tickets
using retired formats must be recreated from a fresh draft and enqueued. This
hard cutoff supersedes ADR-0058's promise that acceptance journals remain
compatible with existing records; the Acceptance Journal remains active.

## Considered Options

- Maintaining dual readers or silently upgrading legacy Tickets was rejected
  because it preserves two authorities and makes recovery behavior depend on
  historical schema details.
- Recomputing acceptance inputs during readiness or completion was rejected
  because resolver and workspace changes could alter what an already-enqueued
  Ticket means.

## Consequences

Enqueue, amendment, reset, and acceptance publication roll forward after
interruption and fail closed on unknown ref identities. Checkouts are disposable
projections of the Ticket's recorded commits; moving a ref backward is not a
rollback mechanism.
