# Launch Booley Flows and Specialists from the Dashboard

Status: accepted

## Context

The Dashboard shows every session and Job in a Sandbox, but starting a run
meant typing `booley flow sim --target sim_fifo --test burst+stall@42` into a
Sandbox shell. Picking a compatible Target, recalling a Test or Test Variant
name and remembering a Flow's options is a lookup the Dashboard already has the
data for. Two facts about the runners shaped the design: a shell run is
foreground and left no Job record, so the Dashboard never saw it; and the MCP
Job runner kills its child when its owner shuts down and keeps no log file, so
it cannot host a run that must outlive the Dashboard.

## Decision

### Scope and trust

- A human may start any Booley Flow, and the built-in `reviewer` and
  `coverage_analyst` Specialists, from the Dashboard. Project custom
  Specialists are not offered. This is the only exception to the Dashboard
  being observational (ADR 0068): it never cancels, retries or changes a Job
  it did not start, Goal-change approvals stay with chat, and abandoning stays
  on the CLI. Its trust boundary does not change: it runs in the Sandbox with
  a Sandbox shell's rights.
- A read-only Targets browser shows the Targets of the selected worktree and
  the Flows that can drive each. Choosing one starts the launch with worktree
  and Target filled in. Any git worktree of the Project can be chosen.
- Rerun, cancel, waveform opening and other actions on an existing Job are out
  of scope.

### Forms

- **Flow forms are generated** from the Flow's own parser, so a new option
  needs no UI change. The same parser validates the whole command before
  submit, so a typo fails in the form, not as a Job. Target and test fields
  offer only compatible Targets and the Target's Tests and Test Variants.
- **Specialist forms are hand-made**, because a person starts a review with a
  category, focus and scope, not with every parser option. They have no
  free-form arguments field. `--verify` and `--waive` are omitted: they
  continue the Developer Agent's own review loop.
- A test requires every Specialist option to appear on its form or on a hidden
  list with a reason, so a new option fails CI until someone places it.

### Review before submit

Every launch ends on a review screen showing the exact equivalent CLI command.
It shows whether the Job runs now or waits for memory, a warning (not a block)
when the worktree has a connected session, running Jobs or uncommitted changes,
because the run uses the files as they are, and the timeout of long Flows. A
Specialist launch also states that it spends model tokens and refuses with the
fix when authentication is missing.

### Standalone Jobs

- A Job with no owning session is a **Standalone Job**. A shell `booley flow`
  or `booley specialist` stays foreground but writes a Job record and a
  `job.log`, so the Dashboard shows it like any Job. A Dashboard launch runs
  the same work in a detached worker in its own session, which writes the
  record, log and terminal status itself and so outlives the Dashboard. The
  worker strips session and Goal environment, and a detached Specialist
  disables its parent-death watchdog.
- Job detail links the report, `job.log` and a Specialist's transcript
  directory; there is no embedded log viewer.
- Standalone Jobs are admitted like any Job (ADR 0087) at the `interactive`
  priority.
- A Standalone Job is never Goal evidence: it has no Goal binding and never
  records, satisfies or invalidates evidence, even in a worktree in Goal Mode.
  The Developer Agent must run its own check.
- Standalone Jobs live as long as the Sandbox. `booley session down` lists the
  running ones and warns before it proceeds.

## Consequences

- The Dashboard now starts processes. Two runners share one record format:
  foreground for the shell, detached for the Dashboard.
- A detached worker started from inside a `booley session` terminal still ends
  with that attachment.
