---
name: booley-qa-run
description: Execute one evidence-producing Scenario Run from Booley's public QA suite.
---

# Run Booley public QA

Coordinate one Scenario Run from its declared inputs through cleanup and its Scenario
Run Outcome. The Scenario defines the work and acceptance requirements; this skill
owns sequencing, evidence integration, and the final record.

## Required inputs

Confirm these before starting product exercises:

- Scenario ID and Configured Scenario ID from the production `scenario.yaml`; infer
  the Configured Scenario when the Scenario contains only one;
- writable artifact root outside disposable Project state;
- credentials and licensed EDA access required by the selected Configured Scenario.

If an input is missing, report it and stop before product work. Capability probes may
mark declared infrastructure unavailable, but they do not change the Configured
Scenario's scope. Keep secret values out of prompts and run records; obtain them
through the approved provider and EDA mechanisms.

Create a fresh Scenario Run ID when preparing the run. It must not collide with an
existing run beneath the artifact root. The Configured Scenario ID selects the
execution parameters; it is not the identity of the new Scenario Run.

Explicit invocation of this skill grants authority for the resources and mutations
declared by the selected Scenario. Record that declared scope in `run.json`. An action
outside the declared scope still requires the Human Maintainer's explicit authority.

## Execute the run

1. Read the selected production `scenario.yaml` and resolve the Configured Scenario's
   named `check_sets` in listed order. Read the shared
   [protocol](../agents/PROTOCOL.md), and the [run-record
   format](../agents/FORMAT.md). The Scenario owns parameters, actions, evidence
   requirements, authority limits, phase budgets, recovery, and cleanup.
2. Run `python qa/validate.py` from the repository root. Stop if validation fails.
   This command validates the suite definition; it does not execute QA.
3. Generate the fresh Scenario Run ID and create its run directory at the provided
   artifact root. Write `run.json` before product exercises with the Configured
   Scenario and declared parameters; exact product revision, artifact, suite, input,
   tool, and environment identities; deadline; invocation-granted authority; and
   pre-run requirement evidence.
4. You are the Scenario Operator. Execute selected Checks in Scenario order and
   delegate setup, development, evaluation, diagnostics, or cleanup to sub-agents
   where useful. You retain responsibility for sequencing, integrating evidence, and
   producing the final record. Follow every Step's prerequisites and recovery
   instructions. Required artifacts cannot be replaced by agent prose.
5. After each Check attempt, append a Check Result containing its `pass`, `fail`,
   `blocked`, or `unavailable` outcome and supporting evidence. Separately append a
   Finding when the run reveals something worth following up beyond the Check outcome,
   such as a product defect, workflow friction, broader impression, or notable win.
   Track owned and borrowed resources in `cleanup-ledger.json` before or as they are
   acquired. Preserve expected fault observations, unexpected failures, retries, and
   recovery evidence.
6. Start cleanup at the Scenario's cleanup boundary on every exit path. Finalize
   missing selected Checks as `blocked`, record resource disposition, calculate the
   Scenario Run Outcome and aggregate Qualification under
   [Qualification](../user/QUALIFICATION.md), and write `summary.md`.

The run is complete when every selected Check has a recorded outcome, mandatory
cleanup has evidence, owned resources are reconciled, and the summary names the
Scenario Run Outcome, aggregate Qualification where applicable, and execution status.
