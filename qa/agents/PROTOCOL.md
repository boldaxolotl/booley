# Scenario protocol

You are the Scenario Operator. Execute one Configured Scenario through these Protocol
Stages:

1. [Admit](ADMIT.md) decides whether the run may start and creates its immutable
   identity record.
2. [Prepare](PREPARE.md) validates the suite and makes the selected Scenario ready.
3. [Execute](EXECUTE.md) performs the selected Checks in Scenario order.
4. [Finish](FINISH.md) finalizes outcomes, quiesces resources, and preserves review
   state.

Read [Record](RECORD.md) with every stage that writes evidence, changes state, or owns
resources. Read only the current stage's procedure. Advance only when its completion
criterion is satisfied.

`operator-state.json` is the durable cursor. Update it atomically before and after
each side effect with the current Protocol Stage, stage status and timestamps, next
Step and Check attempt, active assignments, and outstanding mutations. Legal forward
transitions are `admit -> prepare -> execute -> finish -> complete`; recording occurs
within every stage. A stage may re-enter itself to reconcile a side effect whose
completion is uncertain.

After context compaction, reread this file, `operator-state.json`, [Record](RECORD.md),
and the current stage, then continue the same live run. After an operator interruption,
a replacement may enter [Finish](FINISH.md) for reconciliation only. Further product
work requires a fresh Scenario Run ID; Check Results from the interrupted run do not
satisfy the new run.

The Scenario owns its parameters, actions, Checks, evidence, authority limits,
budgets, recovery, and product-behavior cleanup requirements. Explicit skill
invocation grants only that declared authority. Preserve unexpected observations in
both discovery and qualification runs; run purpose is descriptive metadata.
