# Booley Dashboard replaces the Console

Status: accepted (2026-10-06; proposed 2026-10-03; amended 2026-10-06: session identity, see Amendments)

The Console showed one Ticket execution at a time. Goal Mode (ADR 0067)
removes Ticket execution and runs several agent sessions, often unattended,
in one Sandbox, so the human needs one place to see all of them. The Booley
Dashboard is a read-only terminal view, one per Sandbox, opened by
`booley dashboard`. The Console retires; its Textual code may be reused.
The overview stays concise; session, Goal, and Job details have separate views.

## Decisions

- **One per Sandbox.** A devcontainer task opens the Dashboard in a VS Code
  terminal on Sandbox attach. It covers every worktree and session in that
  Sandbox. No host-wide view across Sandboxes in v1. The client is not shown:
  a Sandbox runs one agent client.
- **Sessions.** One MCP connection to Booley is one session, registered
  when it opens (worktree, branch, start time) and removed when it closes;
  entries whose process died are pruned. A connection, not a process,
  because the host MCP daemon may serve several sessions. A tab that never
  connects to Booley's MCP is invisible. The same registry drives ADR
  0067's shared-worktree warning. Session disconnection does not discard its
  Jobs or their results; an open detail view indicates the disconnection.
- **Read-only.** Approvals stay with the proposing MCP tool and chat;
  abandoning stays on the CLI. Navigation and filtering do not start,
  cancel, retry, or otherwise change work.
- **Visual language.** Reuse the Ticket Mode Console's color scheme and
  status meanings: green for passing, red for failing, orange for needs
  recheck, and dim text for not yet run. Goal descriptions use the existing
  accent color for readability; status headings and symbols retain their
  status colors. Labels and symbols accompany color.

### Dashboard overview

- **Sessions are the top-level rows.** Show Interactive sessions and sessions
  in Goal Mode together: worktree, branch, mode, uptime, and session activity
  state. Show shared-worktree warnings, a compact Goal status summary and
  pending Goal-change proposal count when in Goal Mode, and short summaries
  of running Booley Flow Jobs (Flow, Target, elapsed time). Omit the last MCP
  call and its age, and omit an extra "Goal Mode is off" line.
- **Session activity is reported, not inferred.** Distinguish working,
  waiting for a tool call, and waiting for user input using explicit
  agent-client lifecycle signals. A running background Job alone does not
  mean the session is waiting for it. MCP silence does not establish any of
  these states. If signals are unavailable or stale, show unknown activity
  rather than guessing. Reliable signal collection and association with the
  MCP session must be established during implementation; the approved mock
  uses synthetic signals.
- **Sandbox resources.** Show total CPU usage, memory usage against the
  Sandbox limit, and free disk space in a compact strip. Define CPU usage
  against total Sandbox CPU capacity. Per-Job CPU and memory belong in the
  Jobs view, not in overview Job summaries. Unavailable measurements display
  `—`.
- **Health only when attention is needed.** Show Doctor failures and pending
  upgrade reviews, with relevant diagnostic freshness. Hide the healthy
  Doctor line. Design failures in Booley Flows are not Doctor failures.
- **Standalone Jobs only when running.** Jobs started outside a session get
  their own compact section, hidden when none are running. Jobs and health
  remain visible when applicable even with no connected sessions or Goal Mode.
- **Recent results are not overview content.** They are available through
  the combined Jobs and results view.

### Session detail

- **Orientation.** Show the session's worktree, branch, mode, uptime,
  shared-worktree warning, and connection state. The last Booley call and its
  age may remain in this detail view, without implying session activity.
- **Goals in Goal Mode.** Show every Goal, grouped as failing, needs recheck,
  not yet run, checking, or passing. Preserve the existing evidence rules:
  passing means met, failing and not yet run are distinct unmet states, and
  needs recheck means stale. Checking is an activity indication, not evidence
  that the Goal is met. Adapt the number of Goal columns to terminal width,
  preserving predictable keyboard reading order.
- **Generated, concise Goal descriptions.** Each Goal occupies one short
  line generated from its structured definition: Goal family, Target, Test
  Run selector, and requirement or threshold as applicable. Reuse the existing
  criterion presentation machinery; do not require an agent-authored prose
  title. For example: `Simulation sim_fifo · burst+stall@42 must pass`, or
  `Synthesis synth_fifo · Fmax ≥ 200 MHz`. Keep full descriptions accessible
  in the detail view when a row must be truncated.
- **Goal evidence.** Selecting a Goal opens its requirement, status, last
  check time, available observations or failure details, and evidence paths.
  Stale evidence is identified as requiring recheck, without naming the
  change that made it stale, consistent with ADR 0067.
- **Proposals and activity.** Show pending Goal-change proposals with their
  proposed changes; approval remains in chat. Show the session's running
  Jobs and a bounded list of recent Booley calls and outcomes. Session Job
  history is reached through the Jobs and results view. For Interactive
  sessions without Goal Mode, Jobs and activity are the main content.

### Jobs and results

- **One combined view.** Running, Recent, and All are filters over the same
  Job list. `j` opens Running; `r` opens Recent. From a session detail view,
  these shortcuts scope the list to that session; the user can switch to all
  sessions and standalone Jobs. Recent results are ordered newest first.
- **Job list.** Show Flow, Target, owning session or standalone identity,
  queued/running/completed/failed state, elapsed time, and reported execution
  stage when available. Running Jobs show CPU and memory usage; completed
  Jobs show their outcome instead of current resource measurements.
- **Selected Job.** Show the Flow configuration, EDA tool, start/end times,
  elapsed time, execution stage, available resource measurements including
  peak memory, result summary, and artifact paths. Distinguish execution
  completion from passing checks, and execution failure from failed checks.
  Show useful metrics or failure explanations when supported by evidence.
- **Responsive details.** On wide terminals, place the selected Job's details
  beside the list. On narrow terminals, `Enter` opens a dedicated detail view.
  Keep selection on a Job when it completes so the result can be inspected
  in place; automatic refresh preserves selection and scroll position.
- **Paths, not inline logs.** Display log, report, and artifact paths, preferably
  clickable through the terminal or editor's supported file-link mechanism.
  Do not embed a log viewer. Publish only paths to available artifacts.

### Navigation and remaining implementation choices

- **Keyboard navigation.** Give sessions stable shortcuts `1`–`9` while
  connected; removing one does not renumber the others. Arrow keys and
  `Enter` allow selecting any session, including sessions beyond nine, and
  inspecting Goals and Jobs. `Esc` returns to the previous view. A visible
  footer shows the applicable shortcuts; `?` opens help.
- **Freshness and empty states.** Refresh automatically, identify unavailable
  or stale data, and explain empty session or filtered Job lists. Avoid empty
  health and standalone-Job sections on the overview.
- **Implementation choices still open.** Establish the client lifecycle
  integration for session activity, the bounds and retention of Job history,
  and terminal/editor file-link compatibility. The visual design does not
  establish that these integrations already exist.
- **Not in v1.** Active Specialist runs, per-session token usage, and recent
  Feedback findings remain deferred.


## Amendments (2026-10-06)

Recorded after the Phase 0
[session identity spike](../internals/validation/0067-goal-mode-session-identity.md).

- **Sessions.** A session is one agent client thread or process, falling
  back to the worktree: Codex sends a thread id in every request's `_meta`;
  Claude Code does not, but each tab is its own process, found from the
  loopback peer port; when neither applies the key is the call's resolved
  `work_dir`. Codex tabs share one daemon process, so a process key is never
  used for a PID that already owns a row with a different thread id. There
  is no host MCP daemon; the sentence about it is withdrawn.
- **Registry is presentational.** It feeds the Dashboard rows and ADR
  0067's shared-worktree warning only. No lifecycle rule reads it, and it
  never selects a Goal record or a `work_dir`.
