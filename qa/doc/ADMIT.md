# Admit a Scenario Run

Read the selected production `scenario.yaml` and its Configured Scenario declaration.
Run `python qa/validate.py`, then resolve the selected Checks and supporting Steps in
Scenario order. Do not create a Scenario Run if either operation fails.

Begin with non-mutating assessments. Freeze every supplied identity and resolve a
revision expression such as `origin/main` to one exact commit before using it. Do not
prepare a Project or execute a Scenario Check during admission.

Require a Scenario ID, Configured Scenario ID, writable artifact root outside
disposable Project state, and the credentials and licensed EDA access declared by the
Configured Scenario. Keep secret values out of prompts and records; obtain them
through approved provider and EDA mechanisms.

When the selected production Scenario's shared pre-run requirements authorize
`booley bootstrap` during admission and `booley init` during execution, gate on the
permission, capacity, and external inputs needed to run them, not on the prior
existence of managed images or toolchains they create. Admission may reconcile Host
Bootstrap as described below, but must leave Project Initialization and its evidence
to the selected execution Step.

Use the declared artifact form. An unreleased candidate, including a local wheel,
must be immutable and bound to a source commit and content hash. Reject undeclared
substitutions, floating references, editable installs, and source-checkout execution
or imports.

## Reconcile candidate and Host Bootstrap

After the static gates pass, reconcile only missing or stale state needed to admit the
declared build:

1. Resolve the immutable candidate wheel for the Human Maintainer's exact source
   commit. When no matching wheel exists, build or replace it from a clean isolated
   checkout of that commit using the repository's packaged build path.
2. Whether the wheel is new or reused, verify its embedded source commit and payload
   fingerprint, calculate its SHA-256, and bind the matching documentation snapshot
   before use. A dirty, unstamped, mismatched, or multiply resolved build is not
   admissible.
3. Install or update that wheel in an isolated operator-only host CLI environment.
   Confirm the executable, imported package, version, source commit, and wheel hash
   all identify the declared build. This environment is separate from the isolated
   installation exercised later by Scenario Checks.
4. Run the declared build's `booley bootstrap --check-only`. When it reports pending
   work, run `booley bootstrap`, then repeat `--check-only` and require a ready result.
   Use `--force` only when the Human Maintainer or Configured Scenario explicitly
   authorized forced reconciliation.

Before the first mutating action, allocate a fresh Admission Attempt ID under the
writable artifact root and create the durable admission records specified by
[Format](FORMAT.md#admission-attempt-records). Before each action, record the command,
source and destination identities, relevant pre-state, and every planned
operator-owned resource. Capture exit status, output, content hashes, post-state, and
actual resource identities immediately afterward using atomic record updates.

On successful admission, finalize and retain that record under
`evidence/admission/`, link it from `run.json`, and transfer every still-owned resource
to the Scenario Run cleanup ledger before entering execution. On failure, preserve the
diagnostic outside Project state and reconcile every ledger entry, compensating any
partially created operator-owned resource. Preserve shared caches, existing
credentials, borrowed EDA installations, and unrelated managed resources.

These actions establish inputs and reusable host readiness only. They do not satisfy
a Check, replace a Scenario Step, initialize a Project, or count as product evidence.

A non-mutating assessment may mark a declared capability `unavailable`, but cannot
narrow the Configured Scenario. After allowed reconciliation, stop without creating a
Scenario Run when an input, identity, permission, or required access is still missing.
Do not record admission as a Check Result or use it to satisfy a selected provenance,
installation, or Host Bootstrap Check.

After every gate passes, generate a fresh Scenario Run ID and write `run.json` and
`operator-state.json` beneath the artifact root as specified by [Format](FORMAT.md).
Record pre-assessment state and verified admission-reconciliation outputs as initial
identities. State produced after admission belongs to its producing Step.

Admission is complete only when `run.json` contains every required initial identity
and the durable checkpoint names the first Step and Check attempt at Protocol Stage
`execute`. Then read [Execute](EXECUTE.md).
