# Run a QA scenario

Use this document as the entry point for an agent-run QA session. The operator must
name a profile, choose one of that profile's scenario runs, provide an artifact root,
and grant the required authority. A request to "run QA" without those inputs is not
enough to start product exercises.

## Inputs from the operator

Confirm these before starting:

- profile ID from [`profiles.yaml`](../profiles.yaml);
- run ID from that profile;
- writable artifact root outside disposable Project state;
- authority for the run's declared resources and mutations;
- credentials and licensed EDA access required by the selected run.

If an input is missing, report it and stop before product work. Capability probes may
mark declared infrastructure unavailable, but they do not change profile scope.

## Run sequence

1. Read the selected profile and resolve its named `check_sets` in listed order. Read
   the matching production `scenario.yaml`, [protocol](PROTOCOL.md), and
   [run-record format](FORMAT.md). The scenario owns actions, evidence requirements,
   authority limits, phase budgets, recovery, and cleanup.
2. Run `python qa/validate.py` from the repository root. Stop if validation fails.
   This command validates the suite definition; it does not execute QA.
3. Create the run directory at the operator's artifact root. Write `run.json` before
   product exercises with the exact release, suite and input identities, environment,
   deadline, granted authority, and capability probe results.
4. Execute selected checks in scenario order. Delegate setup, development, evaluation,
   diagnostics, or cleanup where useful, but keep one coordinator responsible for
   sequencing and the final record. Follow each step's prerequisites and recovery
   instructions. Do not replace required artifacts with agent prose.
5. Append every result and finding as it occurs. Track owned resources before or as
   they are created. Preserve expected fault observations, unexpected failures,
   retries, recovery evidence, and unavailable capabilities.
6. Start cleanup at the scenario's cleanup boundary on every exit path. Finalize
   missing selected checks as `blocked`, record resource disposition, calculate the
   profile verdict under [Qualification](../user/QUALIFICATION.md), and write
   `summary.md`.

The run is complete when every selected check has a recorded outcome, mandatory
cleanup has evidence, owned resources are reconciled, and the summary names the
profile verdict and operational completion state.

## Prompt template

Give the agent this document plus concrete run inputs:

```text
Follow qa/agents/RUN.md. Execute run <run-id> from profile <profile-id>.
Store records and evidence under <artifact-root>. You have authority to
<granted-actions-and-resources>. Stop before product work if any required input,
identity, capability, credential, or authority is missing.
```

Do not place secret values in the prompt or run records. Supply credentials through
the approved provider and EDA mechanisms.
