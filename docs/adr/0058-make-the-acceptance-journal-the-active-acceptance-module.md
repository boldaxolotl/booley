---
status: accepted
---

# Make the Acceptance Journal the Active Acceptance Module

Repository acceptance was split between a passive journal model and Ticket
Board completion code that also performed Git preparation, publication, and
recovery. This made the durable state machine's interface implicit and allowed
callers to depend on its implementation details.

The Acceptance Journal is now the active deep module for repository acceptance.
Its single `advance_acceptance` operation owns source keepalives, candidate
preparation and finalization, journal checkpoints, destination publication and
post-approval verification, and cleanup. Completion retains only Ticket Board
policy: it validates the requested policy, supplies the sealed Target Contract,
performs the approval transition when requested, and reports the journal's
outcome.

The journal schema remains compatible with existing records. Journal-owned Git
refs provide crash-safe reachability for pinned source and finalized commits;
unknown orphan refs and identity conflicts fail closed for manual inspection.
Once the Ticket is done, recoverable journal or cleanup work is reported as
accepted but pending instead of falsely claiming that the Ticket remains in
review.
