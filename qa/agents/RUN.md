# Run a QA scenario

Use this document as the entry point for an agent-run QA session. The Human Maintainer must
name a Scenario, choose one of its Configured Scenarios, provide an artifact root,
and grant the required authority. A request to "run QA" without those inputs is not
enough to start product exercises.

## Inputs from the Human Maintainer

Confirm these before starting:

- Scenario ID and Configured Scenario ID from the production `scenario.yaml`;
- writable artifact root outside disposable Project state;
- authority for the run's declared resources and mutations;
- credentials and licensed EDA access required by the selected run.

If an input is missing, report it and stop before product work. Capability probes may
mark declared infrastructure unavailable, but they do not change the selected Configured Scenario's scope.

## Run sequence

1. Read the selected production `scenario.yaml` and resolve the Configured Scenario's named
   `check_sets` in listed order. Read the [protocol](PROTOCOL.md) and
   [run-record format](FORMAT.md). The Scenario owns parameters, actions, evidence
   requirements, authority limits, phase budgets, recovery, and cleanup.
2. Run `python qa/validate.py` from the repository root. Stop if validation fails.
   This command validates the suite structure; it does not execute QA.
3. Create the run directory at the Human Maintainer's artifact root. Write `run.json` before
   product exercises with the exact product revision, artifact, suite and input identities, environment,
   deadline, granted authority, and capability probe results.
4. Execute selected checks in scenario order. Delegate setup, development, evaluation,
   diagnostics, or cleanup where useful, but keep one Scenario Operator responsible for
   sequencing and the final record. Follow each step's prerequisites and recovery
   instructions. Do not replace required artifacts with agent prose.
5. Append every result and finding as it occurs. Track owned resources before or as
   they are created. Preserve expected fault observations, unexpected failures,
   retries, recovery evidence, and unavailable capabilities.
6. Start cleanup at the scenario's cleanup boundary on every exit path. Finalize
   missing selected checks as `blocked`, record resource disposition, calculate the
   Scenario Run Outcome and aggregate Qualification under [Qualification](../user/QUALIFICATION.md), and write
   `summary.md`.

The run is complete when every selected check has a recorded outcome, mandatory
cleanup has evidence, owned resources are reconciled, and the summary names the
Scenario Run Outcome, aggregate Qualification state where applicable, and execution status.

## Prompt template

Give the agent this document plus concrete run inputs:

```text
Follow qa/agents/RUN.md. Execute Scenario <scenario-id> using Configured Scenario
<configured-scenario-id>.
Store records and evidence under <artifact-root>. You have authority to
<granted-actions-and-resources>. Stop before product work if any required input,
identity, capability, credential, or authority is missing.
```

Do not place secret values in the prompt or run records. Supply credentials through
the approved provider and EDA mechanisms.
