# Admit a Scenario Run

Read the selected production `scenario.yaml` and its Configured Scenario declaration.
Run `python qa/validate.py`, then resolve the selected Checks and supporting Steps in
Scenario order. Do not create a Scenario Run if either operation fails.

Admission is read-only until every requirement passes. Run only non-mutating
assessments; do not prepare the product or run environment. Successful admission may
create only the run directory, `run.json`, and `operator-state.json`.

Require a Scenario ID, Configured Scenario ID, writable artifact root outside
disposable Project state, and the credentials and licensed EDA access declared by the
Configured Scenario. Keep secret values out of prompts and records; obtain them
through approved provider and EDA mechanisms.

Use the declared artifact form. An unreleased candidate, including a local wheel,
must be immutable and bound to a source commit and content hash. Reject undeclared
substitutions, floating references, editable installs, and source-checkout execution
or imports.

A non-mutating assessment may mark a declared capability `unavailable`, but cannot
narrow the Configured Scenario. Stop without creating a Scenario Run when an input,
identity, permission, or required access is missing. Do not record admission as a
Check Result or use it to satisfy a selected provenance or installation Check.

After every gate passes, generate a fresh Scenario Run ID and write `run.json` and
`operator-state.json` beneath the artifact root as specified by [Format](FORMAT.md).
Record only pre-existing state as an initial identity; state produced later belongs
to its producing Step.

Admission is complete only when `run.json` contains every required initial identity
and the durable checkpoint names the first Step and Check attempt at Protocol Stage
`execute`. Then read [Execute](EXECUTE.md).
