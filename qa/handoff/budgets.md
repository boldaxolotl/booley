# Eight-hour budgets and continuation

The maintainer confirmed the eight-hour absolute limit and explicit reallocation
without removing workloads in the handoff discussion on 2026-09-08. The allocations
below are agreed execution caps, **not measurements or a claim of feasibility**.
Actual qualification must demonstrate complete required work within the deadline.

The accepted PicoRV32 phase figures sum to **520 minutes (8h40)**, not the 10h20
previously written in the handoff. Taxi's prior figures sum to 510 minutes (8h30).
The revised Taxi budget includes the newly approved submodule companion Project.

| PicoRV32 phase | Prior minutes | Revised minutes |
| --- | ---: | ---: |
| Preparation, host/admin probes and Doctor | 60 | 50 |
| Clean demo baseline and provisioned Linux Vivado | 60 | 60 |
| Interactive Mode, waveform and allocated inventory probes | 45 | 45 |
| Both Ticket Create calls and authoring checks | 20 | 20 |
| Ticket 1 and its negative/recovery exercises | 60 | 60 |
| Ticket 2, remaining allocated checks and complete final regression | 240 | 210 |
| Shared contingency | 15 | 15 |
| Cleanup | 20 | 20 |
| **Total** | **520** | **480** |

PicoRV32 starts cleanup by minute 460 (7h40), even when required checks remain.
Its 210-minute combined phase includes final regression; the final regression
must not be moved into the cleanup reserve. The full profile, including GUI work
when available, has the same limit. Windows' explicit Vivado exclusion creates
no credit for the excluded checks and does not extend any other phase cap.

| Taxi phase | Prior minutes | Revised minutes |
| --- | ---: | ---: |
| Preparation, Project Initialization, derived image and complete Setup | 90 | 75 |
| Doctor, Target resolution and clean continuity, including both synthesis paths | 75 | 60 |
| Interactive Mode, FST/B-Wave and allocated inventory probes | 60 | 45 |
| Disposable submodule companion Project | — | 40 |
| Ticket 1 including the complete 7-of-8 mutation campaign | 105 | 100 |
| Seed proof and Ticket 2, including directed physical comparison | 105 | 100 |
| Complete final regression, including logical and physical synthesis | 30 | 30 |
| Shared contingency | 30 | 15 |
| Cleanup, including companion resources | 15 | 15 |
| **Total** | **510** | **480** |

Taxi finishes new development/companion work and starts final regression by
minute 435 (7h15), reserving 30 minutes for it and 15 for cleanup. This is tighter
than the earlier rule to start regression or cleanup by 7h30. Start cleanup by
minute 465 (7h45). If regression cannot safely start, proceed to cleanup and mark
its unmet checks blocked. The companion runs after clean continuity in a separate
Project and cannot modify Taxi's trusted checkpoints.

UART retains its accepted eight-hour budget, 15-minute external-image exercise,
and maximum two evaluator-guided repairs. Its precise existing phase allocations
and cleanup boundary are carried in the [UART catalogue](uart.md); any separate
inventory probe consumes its owning phase, never an unrecorded setup period.

## Common budget semantics

- The clock starts before the scenario's first product exercise, including any
  assigned fresh installation/Host Bootstrap. Pre-provisioned host prerequisites
  are only those explicitly outside the journey; moving required work before
  the clock is a deviation.
- All delegated work, artifact capture, bounded recovery and retries consume the
  enclosing phase. An operation's effective timeout is the minimum of its own
  allowed timeout, remaining phase allowance plus explicitly allocated remaining
  contingency, and time before the reserve boundary.
- The coordinator may spend the single contingency allowance on an identified
  phase and records that allocation. It cannot borrow the cleanup reserve,
  repeatedly reuse contingency, or silently raise another phase cap.
- Finishing a phase early does not authorize new workload or skipped evidence.
  Required later phases still execute. There is no assumed parallel speedup;
  concurrency is used only where independently safe and already prescribed.
- At a cap, preserve existing observations, stop dependent new work, continue
  independently safe checks only within their own caps, and enter cleanup on
  time. Timeout does not authorize automatic reruns or weakened thresholds.
- PicoRV32/Taxi retain one automatic retry only for the exact accepted stream-
  stall error. UART retains its separate maximum-two-repair rule. Neither grants
  general retry authority or coordinator resume across interrupted runs.
- A deadline ends evidence-taking for qualification except cleanup and final
  bookkeeping. If cleanup overruns, keep attempting authorized cleanup and
  retain the overrun; never report timely complete qualification.

The review can approve a bounded, fully specified initial experiment without
pretending it has passed. If executions show these caps cannot accommodate the
preserved workloads, raise a new design decision with actual timings; do not
quietly cut checks, reuse old results, or extend the deadline.
