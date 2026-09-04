---
status: accepted
---

# Deepen the Simulation Adapter Seam

Simulation keeps campaign policy in `SimulateFlow` and gives simulator variance
to a private adapter boundary. The Flow selects Targets and tests, coordinates
Cycle Count baselines, applies Criteria, renders reports, and chooses the public
exit code. Adapter composition owns Verilator, Icarus, and Cocotb command
shaping; leaf adapters own simulator launch, verdict normalization, and trace
finalization.

The caller expresses test intent as either an ordered, nonempty `NamedTests`
value or `DefaultSelection`. `None` is not an adapter-level test identity. A
default native selection means one default simulator invocation; a default
Cocotb selection means an unfiltered module run whose test names may be learned
only from current-attempt Cocotb evidence.

Every prepared adapter invocation may carry a versioned result path, an
unpredictable attempt token, the durable Target identity, and the complete
ordered selected-test set. The child publishes its normalized result by atomic
replacement. The decoder rejects a schema, token, adapter, Target, selection,
count, or verdict contradiction. Existing summary lines and result files remain
compatibility evidence while callers migrate to the typed channel.

Project-owned settings are resolved from `TargetHandle.project_root` for each
invocation. This applies equally to the active checkout and an ephemeral Cycle
Count baseline worktree; a module cache populated for one checkout cannot
supply another checkout's test list, selector, environment, or Pre-Run
Commands.

## Considered Options

- Keeping simulator command construction in `SimulateFlow` was rejected because
  it makes each new simulator concern widen an already broad orchestration
  module.
- Treating output markers or mutable build-directory files as the new adapter
  interface was rejected because stale or mismatched evidence can be mistaken
  for the current attempt.
- Giving each adapter its own result schema was rejected because the parent
  would still need simulator-specific parsing and precedence policy.
- Moving build preparation into each adapter was rejected because full
  Simulation and Elaboration Check deliberately share the same authenticated
  `sim.build` contract.
- A single end-to-end timeout was rejected for this refactor. The established
  simulator budget, wrapper trace-cleanup margin, and independent Pre-Run
  budget remain behaviorally compatible.

## Consequences

Leaf adapters import neither sibling adapters nor `sim.flow`; one composition
module is the only adapter selector. Artifact paths crossing the seam are
validated for containment, regular-file identity, and freshness, with an
explicit allowance for configured absolute trace destinations. Transport
failure is typed infrastructure evidence, except that a wrapper timeout retains
its established timeout precedence when the child could not publish a terminal
result.

Pre-Run Commands fire after Target preparation and before the composite
build/run subprocess: once per selected native test and once per Cocotb batch.
Elaboration Check does not enter this adapter seam and continues to share only
build preparation and classification with full Simulation.
