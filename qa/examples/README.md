# Worked format example

This illustrative slice borrows the PicoRV32 Wishbone fault/recovery shape. It is
not a production scenario and its evidence paths, hashes, identities, timestamps,
and results are examples, not execution evidence. It grants no coverage credit.
Production encoding must supply the complete accepted Scenario and concrete inputs.

Read [scenario.yaml](scenario.yaml) beside [run.json](run.json),
[results.jsonl](results.jsonl), and [summary.md](summary.md). The example shows:

- a normal baseline check with a local stimulus and evidence contract;
- an expected seeded failure followed by restored-state proof;
- an unavailable Waveform Viewer check that leaves GUI qualification incomplete;
- cleanup and a core pass that does not claim full qualification.

The fault-detection check passes because the injected fault is expected. If baseline
instead fails unexpectedly, append its failure and any recovery evidence separately;
core remains failed. If restoration has no result, core is incomplete unless another
trustworthy failure already makes it failed. If cleanup has no proof, core cannot pass.

The Scenario declares its named check sets and Configured Scenarios; checks own their
capability references. Actual production Configured Scenarios select all sets required by
their full Scenario. Here the selections describe only this slice, not qualification
of PicoRV32.

The GUI Configured Scenario includes baseline/fault/restoration work needed to
supply its trace. Each Scenario Run is a separate execution; supporting work is not
reused from a prior run or silently omitted from the GUI run.

The scenario uses the production [JSON Schema](../scenario.schema.json). Result
interpretation additionally covers these independent cases: a missing selected
record is blocked; an unexpected failure followed by successful recovery still
fails the Scenario Run; contradictory trustworthy attempts create a flaky Finding and
a failed Scenario Run Outcome; and absent cleanup proof leaves the Outcome incomplete unless a
failure already takes precedence. The example records are illustrative only.
