# Prepare a Scenario Run

Read [Record](RECORD.md), the selected production `scenario.yaml`, and the Configured
Scenario's named `check_sets` in order.

Run `python qa/validate.py` from the repository root. Stop if validation fails; it
validates suite structure without executing QA.

Use documentation and packaged skills matching the tested build, CLI or MCP help,
and ordinary Project inspection. Consult source only to verify or classify behavior
after preserving the original observation.

Prepare the declared environment and record every side effect through
`operator-state.json` and `cleanup-ledger.json`. Record produced repository, accepted
commit, Runtime Image, tool, and environment identities with the producing Step and
its evidence. Reconcile uncertain mutations before starting another one.

Preparation is complete when the selected Check list and its supporting Steps are
resolved in Scenario order, prerequisites and initial capability assessments are
recorded, the environment is ready for the first executable Step, and the durable
cursor names that Step and Check attempt at Protocol Stage `execute`. Then read
[Execute](EXECUTE.md).
