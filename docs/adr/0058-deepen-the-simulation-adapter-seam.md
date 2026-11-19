# Deepen the Simulation Adapter Seam

Status: accepted

## Context

`SimulateFlow` once built simulator commands, scheduled work and read output
markers itself, so every new simulator concern widened an already broad
orchestration module, and a stale or forged output line could pass for the
current attempt's evidence.

## Decision

### Who owns what

- The Simulation Record owns policy: the immutable plan, manifest, durable
  serial scheduling and resume. `SimulateFlow` owns Target and test selection,
  Cycle Count prerequisites, reports and the exit code. Neither
  depends on leaf-adapter details.
- Adapter composition is the only place that selects a simulator. Leaf
  adapters run only their own simulator (launch, verdict normalization, trace
  finalization) and import neither a sibling adapter nor the Flow. Waveform
  mechanics (inspection, conversion, discovery) stay in B-Wave's store
  interface; adapters do not duplicate them.
- A work item reaches the adapter through one execution boundary that
  compiles, launches and returns immutable normalized evidence. The record's
  executor owns the surrounding attempt, build, bundle verification and Pre-Sim
  sequencing.
- External callers read a Simulation Record, Coverage Record or retention
  eligibility only through the record package's read capabilities, which
  return immutable typed evidence. Store layout, recovery scanning and summary
  decoding stay private. Read-only inspection never regenerates projections and
  keeps incomplete recovery, an incomplete summary and corrupt storage distinct.

### Evidence crosses the seam typed

- The caller states test intent as an ordered, nonempty set of named tests or
  as the default selection. `None` is not a test identity. A default native
  selection is one simulator invocation; a default Cocotb selection is an
  unfiltered module run whose test names are learned from current-attempt
  evidence.
- The child publishes a versioned result by atomic replacement, bound to an
  unpredictable attempt token, the adapter, the durable Target identity and
  the full ordered selection. The decoder rejects any contradiction. The
  parent accepts the file only as a fresh, contained current-attempt artifact
  and validates logs and trace artifacts on its own. Production grading uses
  this channel; summary lines and result files are for humans and older
  integrations. Build, Pre-Sim, workload, artifact and infrastructure evidence
  all return in the outcome, never rebuilt from output markers.
- Transport failure is typed infrastructure evidence, except that a wrapper
  timeout keeps its precedence when the child published no result.
- Record evidence is authenticated when a Simulator Bundle is launched (in
  place, before launch and after the process tree is dead) or reused, and
  trusted on read: a scan binds each work item's final
  attempt and result by attempt ID and ordinal without re-hashing the build
  chain.
- Project-owned settings (test list, selector, environment, Pre-Sim Commands)
  resolve from the Target's own checkout on every invocation, so a Cycle Count
  baseline worktree is judged by its own configuration, never a cache filled
  for another checkout.

### Pre-Sim and Elaboration Check

- On the durable path, Pre-Sim Commands fire once per Simulation Attempt,
  after bundle authentication and runtime-input staging and before launch. The
  explicit legacy per-test access mode uses a private build per work item and
  fires before compilation. Cocotb fires once per batch, or once per Test
  Run in a coverage run.
- Elaboration Check does not use this seam; it shares only build preparation
  and classification with full Simulation.

## Rejected alternatives

- **Per-adapter result schemas:** the parent would still need simulator-
  specific parsing and precedence rules.
- **Output markers or mutable build files as the interface:** stale evidence
  passes for the current attempt.
- **Build preparation inside each adapter:** full Simulation and Elaboration
  Check deliberately share one authenticated build contract.
- **One end-to-end timeout:** the simulator budget, wrapper trace-cleanup
  margin and independent Pre-Sim budget stay separate.

## Consequences

- A source-dependency rule enforces the leaf-adapter isolation.
- Resume and full pruning cancel an interrupted attempt's lane execution
  before removing its run directory, and fail closed when the lane cannot be
  proven terminal.
