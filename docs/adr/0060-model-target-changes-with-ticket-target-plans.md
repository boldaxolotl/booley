---
status: accepted
---

# Model Target Changes with Ticket Target Plans

Most Tickets should use the Project's existing Targets unchanged. When ticket
creation must change the Target surface, the Ticket carries an optional,
machine-readable `target_plan` that classifies every newly authored Target as
`persistent`, `replacement`, or `ephemeral`. A persistent Target adds a build
that remains independently selectable; a replacement Target supersedes one
runnable baseline Target; and an ephemeral Target exists only to collect that
Ticket's evidence. Ticket creation commits the approved Target definitions and
owned test tables before enqueue, while acceptance derives removals from the
plan: replacement baselines and ephemeral Targets disappear, and persistent
and replacement Targets remain.

This replaces the unstructured `on_success.remove_targets` operation with an
explicit transition model. The old field has a hard cutoff: any Ticket that
contains it is invalid, including Tickets already queued or in progress. The
published Acceptance Basis may retain a machine-owned canonical removal set, but
it derives that set exclusively from `target_plan`; it is not an authoring
surface or user-facing workflow detail.

An active Ticket may provide its planned persistent and replacement Targets to
dependent Tickets. Ephemeral Targets and retiring replacement baselines are
never providers. The consumer declares the provider as a normal Ticket
dependency; Booley pins and materializes the provider's published Target surface
internally, then refreshes the untouched consumer Ticket and publishes a new
Acceptance Basis against the accepted dependency state before its first execution. Many Tickets may
consume one planned Target, and replacements may form an ordered chain, but
sibling replacements of the same eventual baseline must be ordered or
resolved explicitly.

## Considered options

- Editing an existing Target in place during ticket creation was rejected
  because the accepted baseline recipe would no longer be runnable alongside
  its candidate during development.
- Creating a new persistent Target for every changed recipe was rejected
  because it accumulates obsolete Targets and obscures which builds the
  Project still supports.
- Storing lifecycle metadata in the FuseSoC Target was rejected because
  replacement and ephemeral are Ticket-relative roles, not durable properties
  of a build recipe; replacement metadata would become stale at acceptance.
- Preserving `on_success.remove_targets` for legacy Tickets was rejected in
  favor of one explicit Target-transition model and a deliberate hard cutoff.

## Consequences

Omitting `target_plan` means ticket creation authors no Targets of its own; the
Ticket may still consume an exportable planned Target from a declared
dependency. A present plan is nonempty, requires
`on_success.merge: true`, and must account for every newly authored Target
exactly once. Existing Target definitions cannot be edited or deleted during
ticket creation. Replacement baselines must remain resolvable and runnable
enough to collect evidence, although their Criteria may fail.

Target-plan approval shows complete definitions and owned test tables for
persistent and ephemeral Targets. A replacement shows its candidate's focused
diff against the declared baseline, including the owned test table; acceptance
effects are explicit. Provider materialization, pinning, and refresh remain
internal Acceptance Basis mechanics and are omitted from normal user-facing
output.
