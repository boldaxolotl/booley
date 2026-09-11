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

Use the declared artifact form. An unreleased candidate, including a local wheel,
must be immutable and bound to a source commit and content hash. Reject undeclared
substitutions, floating references, editable installs, and source-checkout execution
or imports.

## Reconcile candidate and Host Bootstrap

After the static gates pass, reconcile only missing or stale state needed to admit the
declared build:

1. When the Human Maintainer selected an exact source commit and no matching immutable
   wheel exists, build or replace the candidate wheel from a clean isolated checkout
   of that commit using the repository's packaged build path. Verify the embedded
   source commit and payload fingerprint, calculate the wheel SHA-256, and bind the
   matching documentation snapshot before use. A dirty, unstamped, mismatched, or
   multiply resolved build is not admissible.
2. Install or update that wheel in an isolated operator-only host CLI environment.
   Confirm the executable, imported package, version, source commit, and wheel hash
   all identify the declared build. This environment is separate from the isolated
   installation exercised later by Scenario Checks.
3. Run the declared build's `booley bootstrap --check-only`. When it reports pending
   work, run `booley bootstrap`, then repeat `--check-only` and require a ready result.
   Use `--force` only when the Human Maintainer or Configured Scenario explicitly
   authorized forced reconciliation.

Before each action, capture the command, source and destination identities, and
relevant pre-state in operator-owned temporary storage. Capture exit status, output,
content hashes, and post-state immediately afterward. On successful admission, retain
that evidence under `evidence/admission/` and link it from `run.json`; on failure,
preserve the diagnostic outside Project state and compensate any partially created
operator-owned resource. Preserve shared caches, existing credentials, borrowed EDA
installations, and unrelated managed resources.

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
