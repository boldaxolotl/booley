---
status: accepted
---

# Make the Simulation Campaign the resume authority

`SimulateFlow` remains the CLI and immediate-presentation adapter, while the
existing endpoint acceptance stage delegates Campaign Outcomes to a dedicated
`SimulationAcceptanceCoordinator`, the sole Simulation Campaign Criteria owner.
A deep `SimulationCampaign` module owns the immutable workload plan, Simulator Bundle,
scheduling, per-work-item durability, aggregation, and explicit resume. An
immutable `manifest.json` and immutable terminal work-item results are the
authority; replaceable summaries and `simulation.json` are projections. This
supersedes ADR 0058's placement of campaign policy in `SimulateFlow` while
retaining its normalized simulator-adapter seam and authenticated evidence
contract.

## Considered Options

- Extending `SimulateFlow` was rejected because storage, recovery, build reuse,
  and capacity accounting would widen the CLI adapter and expose those policies
  to every caller.
- Making `simulation.json` authoritative was rejected because it is a
  replaceable compatibility projection whose completion also reflects Criteria
  publication.
- Inferring the latest resumable run was rejected because an implicit choice can
  combine evidence with the wrong Target or workload.

## Consequences

Resume names one exact Simulation Campaign Manifest. Workload changes fail
closed, while execution policy may change. Ordinary HDL tests are independently
resumable; Cocotb and coverage initially publish coarser work-item results that
match their real execution granularity. Durable evidence precedes terminal
results, and terminal results precede Criteria publication.
