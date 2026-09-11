# Scenario protocol

You are the Scenario Operator. Execute one Configured Scenario through these Protocol
Stages:

1. [Admit](ADMIT.md) validates and freezes the run without preparing its environment.
2. [Execute](EXECUTE.md) performs selected Checks and supporting Steps in Scenario
   order, including setup.
3. [Finish](FINISH.md) finalizes outcomes and quiesces resources.

Read [Record](RECORD.md) with every stage that writes evidence, changes state, or owns
resources. Read only the current stage's procedure. Advance only when its completion
criterion is satisfied.

`operator-state.json` is the durable Scenario Run checkpoint. Admission creates it at
Protocol Stage `execute`, naming the first Step and Check attempt. Replace it
atomically before and after every side effect. The only forward stage transition is
`execute -> finish`; `complete` is the terminal status of `finish`. Re-enter the
current stage to reconcile an uncertain side effect.

After context compaction, reread this file, `operator-state.json`, [Record](RECORD.md),
and the current stage, then continue the same live run. After an operator interruption,
a replacement may enter [Finish](FINISH.md) for reconciliation only. Further product
work requires a fresh Scenario Run ID; Check Results from the interrupted run do not
satisfy the new run.
