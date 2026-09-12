# Scenario protocol

You are the Scenario Operator. Execute one Configured Scenario through these Protocol
Stages:

1. [Admit](ADMIT.md) validates inputs, performs the narrowly allowed candidate and
   Host Bootstrap reconciliation when needed, then freezes the run.
2. [Execute](EXECUTE.md) performs selected Checks and supporting Steps in Scenario
   order, including setup.
3. [Finish](FINISH.md) safely shuts down resources, completes records, and seals the
   run for later human triage.

Read [Record](RECORD.md) with every stage that writes evidence, changes state, or owns
resources. Read only the current stage's procedure. Advance only when its completion
criterion is satisfied.

`operator-state.json` is the live Scenario Run checkpoint. Admission creates it at
Protocol Stage `execute`, naming the first Step and Check attempt. Replace it
atomically before and after every side effect. The only forward stage transition is
`execute -> finish`. A valid `run-manifest.json`, written after terminal Finish state,
is the authoritative completion signal. Re-enter the current stage to reconcile an
uncertain side effect.

After context compaction, reread this file, `operator-state.json`, [Record](RECORD.md),
and the current stage, then continue the same live run. After an operator interruption,
a replacement may enter [Finish](FINISH.md) for reconciliation only. Terminal state
without a valid manifest permits manifest-only Finish recovery, never new product
work. Further product work requires a fresh Scenario Run ID; Check Results from the
interrupted run do not satisfy the new run.
