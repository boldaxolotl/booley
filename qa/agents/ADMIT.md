# Admit a Scenario Run

Read [Record](RECORD.md), then admit one production Configured Scenario.

Require a Scenario ID, Configured Scenario ID, writable artifact root outside
disposable Project state, and the credentials and licensed EDA access declared by the
Configured Scenario. Keep secret values out of prompts and records; obtain them
through approved provider and EDA mechanisms.

Use the artifact form declared by the Configured Scenario. Published releases and
unreleased candidates are eligible. A candidate package, including a local wheel,
must be an immutable artifact bound to a source commit and content hash. Reject
undeclared substitutions, floating references, editable installs, and execution or
imports from a source checkout.

Run only non-mutating capability assessments needed for admission. An assessment may
establish that a declared capability is unavailable; it cannot narrow the Configured Scenario.
Stop before product work when an input, identity, permission, or required access is
missing. Admission is not a Check Result and does not replace a selected provenance
or installation Check.

On success, generate a fresh Scenario Run ID, create its directory beneath the
artifact root, and write `run.json` before product work. Record both IDs, declared
parameters, exact immutable Booley revision and package identity, matching
documentation snapshot, suite commit, pinned IP inputs, native-host OS and
architecture, provider, Runtime Attachment, agent backend, relevant Runtime Image and
EDA tool identities, deadline, artifact root, capability assessments, and granted
authority. A Runtime Image produced later is a Step output rather than an initial
identity.

Create `run.json` and `operator-state.json` together after every gate passes, with the
cursor at Protocol Stage `prepare`. Admission is complete only when `run.json`
contains every required initial identity and the durable cursor names `prepare`. Then
read [Prepare](PREPARE.md).
