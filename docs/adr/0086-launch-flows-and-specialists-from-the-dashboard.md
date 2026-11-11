# Launch Booley Flows and Specialists from the Dashboard

Status: accepted (2026-10-28; the `mutation_tester` Specialist, its scope
entry and form, is superseded by [ADR 0088](0088-generate-mutants-deterministically-in-a-mutation-flow.md))

Supersedes the **Read-only** decision of ADR 0068 (Booley Dashboard) for
human-started Booley Flows and Specialists only. Everything else that bullet
keeps out of the Dashboard stays out: Goal-change approvals stay with chat,
abandoning stays on the CLI, and the Dashboard still never cancels, retries,
or otherwise changes a Job it did not start. Builds on the two-window, arrow-key
navigation agreed in [#2240](https://github.com/boldaxolotl/booley/issues/2240).

## Context

The Dashboard shows every session and Job in a Sandbox, but starting a run
still means typing `booley flow sim --target sim_fifo --test burst+stall@42`
into a Sandbox shell. Picking a compatible Target, recalling a test or Test
Variant name, and remembering a Flow's options is exactly the lookup the
Dashboard already has the data for: `booley targets --for` filters Targets by
Flow, the Target test plan lists tests and Test Variants, and every Flow and
Specialist declares its options as an argparse parser.

Three facts about today's code shape the design:

- A shell `booley flow` or `booley specialist` runs in the foreground and
  writes no Job record, so the Dashboard never shows it, even while it holds a
  Job slot. ADR 0076 already assumes such runs write a Job record.
- The MCP Job runner (`JobLifecycle`) kills its child when its owning process
  shuts down, keeps output only as bounded in-memory tails, and writes no log
  file. It cannot host a run that must outlive the Dashboard.
- The Goal baseline sim worker (`booley.mcp.sim_baseline_jobs`) is a detached
  process that writes its own Job record, `job.log`, PID, and terminal status,
  and strips session environment so a session's end does not reap it.

## Decision

### Scope

- **Flows and built-in Specialists.** The Dashboard launches any Booley Flow
  and the built-in Specialists `reviewer`, `mutation_tester`, and
  `coverage_analyst`. Project custom Specialists are not offered.
- **Targets browser.** A read-only view of the selected worktree's Targets.
- **Not in this decision.** Rerun, cancel, waveform opening, and any other
  action on an existing Job.

### Navigation

Arrows, Enter, and Esc only, inside the windows and top bar of #2240.

- **Run.** A top-bar item that opens the launch wizard: worktree, then Flow or
  Specialist, then its form, then the review screen. Esc steps back one stage.
- **Targets window.** A third window after Sessions and Jobs. Each row is a
  Target with its core and the Flows that can drive it; Enter opens its detail
  (core, filesets, tests, Test Variants, constraints) and lists the Flows and
  Target-taking Specialists that can drive it. Choosing one opens the wizard
  with the worktree and Target filled in.
- **Worktree choice.** The worktree list offers every git worktree of the
  Project: worktrees with a session first, labeled with their session and
  mode, the main checkout last. The highlighted session's worktree is
  preselected. The Targets window names its worktree in a top-bar
  **Worktree** item; Enter on it opens the same list.
- **After submit.** The Jobs window opens with the new Job selected. Finished
  human Jobs follow ADR 0068's existing Job rules; no unseen state is kept.

### Forms

- **Flows: generated.** Common options come from the Flow's parser help
  groups. Other options go in an extra-arguments field. The same parser
  validates the whole command before submit, so a typo fails in the form, not
  as a Job. Target fields offer only compatible Targets; test fields offer the
  Target's tests and Test Variants.
- **Specialists: hand-made.** Each built-in Specialist has its own form:
  - `reviewer`: category (`rtl`/`tb`), focus filtered by category, spec path
    (shown only for `spec`, prefilled when the Project names one), scope, and
    instruction. `--verify` and `--waive` are omitted: they continue the
    Developer Agent's own review loop.
  - `mutation_tester`: Target, count (number or `auto`), minimum detected,
    scope, instruction, and **Start fresh** (`--regen-lock`, off by default).
  - `coverage_analyst`: a campaign picker listing `coverage.json` files from
    finished coverage Simulation Jobs in the worktree, newest first, with
    manual path entry. With none, it explains how to produce one.
  - Model tier and instruction are visible; max turns and timeout sit under
    **More**. There is no extra-arguments field.
- **Drift guard.** A test lists each built-in Specialist's parser options and
  requires every one to appear on its form or on a hidden list with a reason.

### Review screen

Every launch ends on a review screen. Enter submits; the exact equivalent CLI
command is shown and can be copied. It also shows:

- whether the Job runs now or queues, and its queue position; a full queue
  refuses the launch with the reason;
- a warning, not a block, when the worktree has a connected session, running
  Jobs, or uncommitted changes, because the run uses the files as they are;
- the timeout budget for long Flows;
- for Specialists: the model tier, that the run uses model tokens, and an
  authentication check that refuses the launch with the fix when it fails.

### Execution and records

- **One Job record for human runs.** A shell `booley flow` or
  `booley specialist` stays in the foreground but writes a Job record with no
  owning session, which makes it a **Standalone Job**. The Dashboard shows it
  like any other Job, and ADR 0076's overlap check can rely on it.
- **A detached Standalone Job worker.** The baseline sim worker becomes a
  generic worker for any Flow or Specialist. The Dashboard spawns it in its
  own session and returns; the worker writes the Job record, PID, `job.log`,
  and terminal status itself, so the run outlives the Dashboard. It strips
  session and Goal environment, and a detached Specialist disables its
  parent-death watchdog.
- **Logs.** Human-started Jobs, shell or Dashboard, write `job.log`. The Job
  detail shows the report, `job.log`, and, for Specialists, the transcript
  directory, all as file links. There is still no embedded log viewer.
  Developer Agent Jobs keep their current output handling.
- **Same queue.** Standalone Jobs take Job slots like any other run, as the
  `interactive` requester role, and queue first-in first-out.

### Evidence and lifecycle

- **Never Goal evidence.** A Standalone Job carries no Goal binding and never
  records, satisfies, or invalidates Goal evidence, even in a worktree in Goal
  Mode. The Developer Agent must run its own check.
- **Sandbox stop.** Standalone Jobs live as long as the Sandbox. `booley
  session down` lists running Standalone Jobs and warns before it proceeds.

## Consequences

- The Dashboard now changes state: it starts processes. Its trust boundary
  does not change, because it runs in the Sandbox with the same rights as a
  Sandbox shell.
- Two runners share one record format: foreground for the shell, detached for
  the Dashboard. The baseline sim worker moves onto the generic worker.
- New built-in Specialist options fail CI until someone places them on a form
  or hides them with a reason.
- Delivery order: Standalone Job records and the worker first; the launcher,
  Targets window, and forms after #2242 and #2243; `job.log` for Developer
  Agent Jobs separately.
