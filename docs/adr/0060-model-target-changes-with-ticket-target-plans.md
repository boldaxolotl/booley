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
Ticket's evidence. Ticket creation commits the approved Target definitions,
their referenced filesets, and owned test tables before enqueue. Added filesets
may be referenced only by planned Targets. Acceptance derives removals from the
plan: replacement baselines and ephemeral Targets disappear, and persistent and
replacement Targets remain. Enqueue validates and commits these approved
authoring inputs while publishing the Acceptance Basis.

This replaces the unstructured `on_success.remove_targets` operation with an
explicit transition model. The old field has a hard cutoff: any Ticket that
contains it is invalid, including Tickets already queued or in progress. The
published Acceptance Basis may retain a machine-owned canonical removal set, but
it derives that set exclusively from `target_plan`; it is not an authoring
surface or user-facing workflow detail.

An eligible basis-published Ticket in `waiting`, `queued`, `running`, `blocked`, or
`review` may provide its planned persistent and replacement Targets to
dependent Tickets. Ephemeral Targets and retiring replacement baselines are
never providers. The consumer declares the provider as a normal Ticket
dependency; Booley pins and materializes the provider's published Target surface
internally, then refreshes the untouched consumer Ticket and publishes a new
Acceptance Basis against the accepted dependency state before its first execution. Many Tickets may
consume one planned Target, and replacements may form an ordered chain, but
sibling replacements of the same eventual baseline must be ordered or
resolved explicitly.

This ADR amends ADR-0059's one-basis-per-Authoring-Generation rule only for a
pre-execution **Basis Refresh**. Once all dependencies are accepted, Booley may replace
the basis of a still-untouched waiting Ticket without user approval when its approved
authored inputs are unchanged. The publication and waiting-to-queued transition are one
recoverable transaction; the old basis and receipt remain retained evidence. Any drift
blocks for `return-to-draft` instead of being treated as a refresh.

Because a provider may itself pass through this refresh, downstream consumers do not
require its accepted basis ID to equal the earlier pin. They require the same exported
role and normalized Target/control surface from the provider's accepted basis.

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
exactly once. Existing Target and fileset definitions cannot be edited or deleted
during ticket creation. A newly authored fileset must be referenced only by
planned or materialized provider Targets; it cannot change an unchanged baseline
Target's inputs. Replacement baselines must remain resolvable and runnable enough
to collect evidence, although their Criteria may fail.

Target-plan approval shows complete definitions, referenced filesets, and owned
test tables for persistent and ephemeral Targets. A replacement shows its
candidate's focused diff against the declared baseline, including referenced
filesets and the owned test table. Provider materialization, pinning, and refresh
use that same focused surface. Acceptance removes a newly authored fileset only
when removal of an ephemeral Target leaves it unreferenced; existing and
still-shared filesets remain. Inability to produce that focused diff
unambiguously is an approval blocker; acceptance effects are explicit. Provider
mechanics remain internal and are omitted from normal user-facing output.
