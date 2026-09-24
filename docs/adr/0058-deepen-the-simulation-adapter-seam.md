---
status: accepted
---

# Deepen the Simulation Adapter Seam

Simulation Campaign policy lives in the deep `SimulationCampaign` module rather
than `SimulateFlow`. That module owns immutable planning, manifest publication,
durable serial scheduling and resume. Its private `OrdinaryHdlSerialExecutor`
bridges one admitted work item to the existing adapter boundary. `SimulateFlow`
selects Targets and tests, coordinates Cycle Count prerequisites, applies
Criteria, renders reports, and chooses the public exit code. Adapter composition
continues to own Verilator, Icarus, and Cocotb command shaping; leaf adapters own
simulator launch, verdict normalization, and trace finalization.

The public `booley.flows.sim.campaign` package also owns storage-backed read
capabilities. External Simulation, Coverage, and retention callers authenticate
one terminal work item or inspect retention eligibility through that package and
receive immutable typed evidence. Durable store construction, path layout,
manifest/result recovery scanning, and summary decoding remain private to the
Campaign implementation. Campaign coordination and child linkage retain direct
store access because they own the durable transaction; callers do not receive a
store, recovery collection, or storage-relative helper to interpret themselves.

Here, trace finalization means current-attempt orchestration and final evidence:
leaf adapters consume B-Wave's waveform-store interface for inspection,
conversion, discovery, and converter-process mechanics. They do not own or
duplicate those B-Wave-specific mechanics.

`SimulationExecution.ordinary_group(handle, selection)` owns compilation and
launch mechanics for an ordinary-HDL selection and returns immutable normalized
evidence. `OrdinaryHdlSerialExecutor` owns the surrounding Simulation Attempt,
Simulator Bundle Build Attempt/Result, executable snapshot, runtime-input and
Pre-Sim sequencing required by the manifest. The compatibility
`SimulationExecution.run(handle, selection)` and `preview(handle, selection)`
operations remain available for execution models that have not moved to the
durable serial path. Neither `SimulationCampaign` nor `SimulateFlow` depends on
leaf-adapter details.

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
compatibility evidence for human logs and older integrations; production Flow
grading uses the typed channel.

The authenticated result carries normalized per-test verdicts and diagnostics.
The parent validates the result file as a fresh, contained current-attempt
artifact before accepting it, and independently validates run logs and trace
artifacts. Build, Pre-Run, workload, artifact, and infrastructure evidence all
cross the execution boundary in the returned outcome instead of being rebuilt
from output markers by the Flow.

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

For the durable ordinary-HDL path, immutable Pre-Sim Commands fire once per
Simulation Attempt after Simulator Bundle authentication and runtime-input
staging but before snapshot launch. Explicit `legacy-per-test` access instead
uses a private build per work item and runs the command before compilation.
Cocotb retains one command per batch until it gains finer durable work-item
isolation. Elaboration Check does not enter this adapter seam and continues to
share only build preparation and classification with full Simulation.

Read-only Campaign inspection never regenerates projections. It preserves the
existing distinction between incomplete recovery, an incomplete summary or
projection, and corrupt storage while normalizing storage I/O failures at the
public Campaign boundary. A source-dependency rule and named-type ownership gate
enforce this refined seam for production code.
