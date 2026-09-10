---
name: booley-qa-run
description: Execute one evidence-producing run from a Booley public QA Profile.
---

# Run Booley public QA

Coordinate one QA Run from its declared inputs through cleanup and the Profile
Verdict. The Scenario defines the work and acceptance requirements; this skill owns
sequencing, evidence integration, and the final record.

## Required inputs

Confirm these before starting product exercises:

- Profile ID from [`profiles.yaml`](../profiles.yaml);
- Profile run definition to execute, selected by its `id` under that Profile's
  `runs`; infer it when the Profile contains only one;
- writable artifact root outside disposable Project state;
- credentials and licensed EDA access required by the selected Profile run
  definition.

If an input is missing, report it and stop before product work. Capability probes may
mark declared infrastructure unavailable, but they do not change Profile scope. Keep
secret values out of prompts and run records; obtain them through the approved
provider and EDA mechanisms.

Create a fresh QA Run ID when preparing the run. It must not collide with an existing
run beneath the artifact root. The Profile run definition selects the Scenario and
environment; its `id` is not the identity of the new QA Run.

Explicit invocation of this skill grants authority for the resources and mutations
declared by the selected Scenario. Record that declared scope in `run.json`. An action
outside the declared scope still requires the user's explicit authority.

## Execute the run

1. Read the selected Profile and resolve its named `check_sets` in listed order. Read
   the matching production `scenario.yaml`, the shared
   [protocol](../agents/PROTOCOL.md), and the [run-record
   format](../agents/FORMAT.md). The Scenario owns actions, evidence requirements,
   authority limits, phase budgets, recovery, and cleanup.
2. Run `python qa/validate.py` from the repository root. Stop if validation fails.
   This command validates the suite definition; it does not execute QA.
3. Generate the fresh QA Run ID and create its run directory at the provided artifact
   root. Write `run.json` before product exercises with the exact release,
   suite and input identities, selected Profile run definition, environment,
   deadline, invocation-granted authority, and capability probe results.
4. You are the Scenario Operator. Execute selected Checks in Scenario order and
   delegate setup, development, evaluation, diagnostics, or cleanup to sub-agents
   where useful. You retain responsibility for sequencing, integrating evidence, and
   producing the final record. Follow every Step's prerequisites and recovery
   instructions. Required artifacts cannot be replaced by agent prose.
5. After each Check attempt, append a Check Result containing its `pass`, `fail`,
   `blocked`, or `unavailable` outcome and supporting evidence. Separately append a
   Finding when the run reveals something worth following up beyond the Check outcome,
   such as a product defect, workflow friction, broader impression, or notable win.
   Track owned resources before or as they are created. Preserve expected fault
   observations, unexpected failures, retries, and recovery evidence.
6. Start cleanup at the Scenario's cleanup boundary on every exit path. Finalize
   missing selected Checks as `blocked`, record resource disposition, calculate the
   Profile Verdict under [Qualification](../user/QUALIFICATION.md), and write
   `summary.md`.

The run is complete when every selected Check has a recorded outcome, mandatory
cleanup has evidence, owned resources are reconciled, and the summary names both the
Profile Verdict and Operational Completion.
