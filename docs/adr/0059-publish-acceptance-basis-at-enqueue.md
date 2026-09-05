---
status: accepted
---

# Publish Acceptance Basis at Enqueue with a Hard Cutoff

Executable Tickets need one immutable authority for authored inputs, repository
routing, Target identities, reset, readiness, and completion. Booley therefore
publishes an Acceptance Basis automatically when an Authoring Generation is
enqueued, records its participant commits through prepare-first journals and
identity-checked ref updates, and treats the stored basis as authoritative for
the rest of that generation.

Target Contract fields, commands, journal schemas, and compatibility adapters
are rejected rather than upgraded. Existing Tickets using the retired format
must be recreated and enqueued as a new Authoring Generation. This hard cutoff
supersedes only ADR-0058's promise that acceptance journals remain compatible
with existing records; the Acceptance Journal remains the active acceptance
module.

## Considered Options

- Maintaining dual readers or silently upgrading legacy Tickets was rejected
  because it preserves two authorities and makes recovery behavior depend on
  historical schema details.
- Recomputing acceptance inputs during readiness or completion was rejected
  because resolver and workspace changes could alter what an already-enqueued
  Ticket means.

## Consequences

Enqueue, reset, and acceptance publication roll forward after interruption and
fail closed on unknown ref identities. Checkouts are disposable projections of
the recorded participant commits; moving a ref backward is not a rollback
mechanism.
