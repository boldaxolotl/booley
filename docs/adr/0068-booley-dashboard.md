# Booley Dashboard replaces the Console

Status: accepted (2026-10-06; proposed 2026-10-03; amended 2026-10-06: session identity; amended 2026-10-11: open implementation choices; amended 2026-10-14: persistent handles; amended 2026-10-15: Sandbox attach; amended 2026-10-16: compact Doctor health, see Amendments)

The Console showed one Ticket execution at a time. Goal Mode (ADR 0067)
removes Ticket execution and runs several agent sessions, often unattended,
in one Sandbox, so the human needs one place to see all of them. The Booley
Dashboard is a read-only terminal view, one per Sandbox, opened by
`booley dashboard`. The Console retires; its Textual code may be reused.
The overview stays concise; session, Goal, and Job details have separate views.

## Decisions

- **One per Sandbox.** A bundled workspace extension opens the Dashboard in a VS Code
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
  `work_dir`. Codex tabs share one daemon process, so Codex never gets a
  process key: without a thread id it falls back to the worktree. Rows
  are not tied to HTTP connections (a client opens a new one per call):
  a row is upserted on every Booley call under its key and removed when
  its identity is dead (the process is gone, or a thread's last call is
  older than the quiet threshold) or when its worktree disappears. "One
  MCP connection is one session, registered when it opens and removed
  when it closes" is replaced by this rule. There is no host MCP daemon;
  the sentence about it is withdrawn.
- **Registry is presentational.** It feeds the Dashboard rows and ADR
  0067's shared-worktree warning only. No lifecycle rule reads it, and it
  never selects a Goal record or a `work_dir`.

## Amendments (2026-10-11)

Settles the three "implementation choices still open". The hook findings come
from spikes on Claude Code 2.1.285/2.1.295 and Codex 0.160.0/0.162.0.

- **Session activity comes from client lifecycle hooks.** The Sandbox Image
  declares the hooks in the files that already carry its web policy:
  `/etc/claude-code/managed-settings.json` and `/etc/codex/requirements.toml`.
  Both clients treat hooks there as managed, so they run without per-user
  hook trust and survive a fresh state volume. Every hook runs
  `/usr/local/libexec/booley/session_activity_hook.py <client>`, which stores
  the session's last event under `runtime/session_activity/` in the Project
  directory. The client waits for the hook on every prompt and tool call, so
  it has a 3 s timeout, prints nothing to stdout, reports a problem on stderr
  only, and always exits 0.
- **The hook does not import Booley.** It is a standalone standard-library
  script in the runtime base image. Importing the `booley` package costs
  about 0.1 s per event, which adds minutes to a long session. The script
  repeats the event table, the registry key formats, the signal file layout
  and the Project directory resolution order; tests hold it in agreement with
  the package that reads the signals.
- **Event mapping.** `UserPromptSubmit`, `PostToolUse` and
  `PostToolUseFailure` mean working; `PreToolUse` means waiting for a tool
  call; `PermissionRequest` and `Stop` mean waiting for user input;
  `SessionEnd` removes the signal. The last event wins. Codex has no
  failed-tool event; its next event after a failed call is `PostToolUse` or
  `Stop`.
- **Interrupted turns.** A turn the user interrupts ends without `Stop`.
  Codex reports `Interrupt`, and Claude Code reports the `idle_prompt`,
  `permission_prompt` and `elicitation_dialog` notifications; all mean
  waiting for user input. These two rules follow the clients' documentation
  and accepted configuration; neither event was observed firing in the
  spikes. Where they do not fire, an interrupted session shows its last busy
  state until the next event or the freshness limit.
- **Accepted inaccuracies.** With parallel tool calls the first to finish
  shows working. After an approval the session shows waiting for user input
  until the tool finishes, because neither client reports that an approval
  was resolved.
- **Association with the session.** A Claude Code hook is a descendant of the
  process that owns the session's MCP connections: `CLAUDE_PID` names that
  process when it is an ancestor of the hook; otherwise the nearest ancestor
  with a Session Registry row is used, and the search stops at a nearer
  Claude Code process that has no row, so an inner client's events are never
  credited to an outer session. A Codex hook payload carries the
  thread id the registry keys on (`agent_id` for a subagent thread,
  otherwise `session_id`). A hook that cannot name a session records nothing.
  Worktree-fallback sessions therefore always show unknown activity.
- **Freshness.** Working and waiting for a tool call become unknown after
  7200 s without a new event, the longest Booley tool call: a client that
  dies mid-turn sends no closing event. Waiting for user input does not go
  stale while the session's row lives. Registry maintenance removes a signal
  once its session has no row and the signal is older than the quiet
  threshold. The hook and maintenance share no lock: maintenance re-examines
  a signal immediately before removing it and spares one the hook has just
  replaced. Losing the remaining instant shows unknown until the next event.
- **Doctor.** Inside the Sandbox, Doctor warns when either client's hooks
  are missing from the image; the Dashboard then shows unknown activity.
- **Job history bounds and retention.** The Dashboard deletes nothing; Job
  records stay with the interactive session or Goal Record that produced
  them. One snapshot examines at most 16384 record files, in this order: the
  caller's interactive session, every Goal Record, then the 64 newest older
  interactive sessions. It reads the newest 4096 of those by modification
  time and says what it cut. Past either bound the oldest interactive
  history goes first. A record is rewritten when its Job changes state, so a
  Job that has run unchanged while 4096 newer records were written drops out
  of the snapshot; this is accepted. Recent lists the newest 50 results and
  keeps the selected one listed; All lists every record in the snapshot.
- **File links.** A report, artifact or evidence path on a line of its own
  opens in the installed VS Code-family editor when clicked, through
  `code --goto`. Opening a file is navigation: it starts a detached editor
  process and changes no work. OSC 8 `file://` links are not used: VS Code's terminal,
  where the Dashboard normally runs, does not open them. Without an editor
  command the Dashboard says so and the path stays copyable.

## Amendments (2026-10-14)

Settles [#1512](https://github.com/boldaxolotl/booley/issues/1512): full session
keys and Job run IDs are unreadable as names, and the nine navigation shortcuts
cannot identify every session or remain meaningful across Dashboard restarts.

- **Persistent Project handles.** Sessions use `S<n>`. Jobs use `J-SIM<n>`,
  `J-SYN<n>`, `J-LINT<n>`, or `J-FPGA<n>`, with `SIM` also covering simulation
  baselines and `J-JOB<n>` for other endpoints. Sessions and Jobs have separate
  monotonic counters; one Job counter spans every kind. Handles appear in
  overview rows, running summaries, session and Job details, Job lists, owner
  labels and scoped Jobs orientation. Full keys remain secondary detail text.
  Keyboard shortcuts and internal run IDs keep their existing meanings.
- **Allocation and retention.** Session registration reserves a handle under
  the registry writer lock in `runtime/sessions/handles/map.json`. This separate
  map survives presence pruning. Job creation reserves a handle in
  `runtime/job-handles/map.json` under its own bounded writer lock. Both maps
  live in the resolved Booley Directory and publish complete, fsynced state
  atomically. Reservation precedes row/record publication; a failed publication
  can leave a gap, and registration of the same key or run ID reuses its
  reservation. A new invocation with a new run ID gets a new handle. Numbers
  are never recycled; inline Jobs retain allocations after their transient
  records are removed. Snapshot creation excludes both maps from worktree copies.
- **Worktree fallback migration.** A path key becoming a repository/admin key
  carries its handle forward through a direct alias if the canonical key has
  no allocation. If both already have allocations, preserve both. Dashboard
  ownership and scoped lists follow established aliases without rewriting Job
  records or changing execution routing.
- **Observational Dashboard.** Session registration and Job creation allocate
  handles. Historical records without allocations keep `S?` / `J-TYPE?`.
  The Dashboard only reads maps; each refresh shares its session-map snapshot.
  Handle-store failures are advisory and never prevent session presence or Job
  execution. Schema 1 and string mappings are checked at the boundary; counters
  are repaired above existing numeric suffixes. Unreadable, unsupported,
  redirected or oversized maps are diagnosed rather than reset. The 16 MiB
  bound prevents allocation from recycling handles through a reset.

## Amendments (2026-10-15)

- **Attach through a workspace extension.** VS Code defines
  `task.allowAutomaticTasks` as an application preference, so a devcontainer's
  remote Machine setting cannot enable a `folderOpen` task. Application images
  build a deterministic VSIX from wheel-owned sources at
  `/usr/local/libexec/booley/dashboard.vsix`; Dev Containers installs that local
  artifact before extension activation. No Marketplace publication or local
  editor/profile extension installation is required. The extension and every
  Dashboard process run inside the Sandbox.
- **Explicit policy, fixed process.** In a trusted Dev Containers window with
  the generated attach protocol enabled and one Git Project Root opened, the
  extension reads live policy through
  `booley dashboard --attach-policy`, then opens a transient terminal running
  the fixed executable and arguments `booley dashboard --on-attach`. Python
  rechecks configuration and ownership immediately before starting the existing
  Dashboard. A Sandbox socket remains the final singleton guard. Ordinary manual
  `booley dashboard` retains its behavior.
- **Preserve preferences and task ownership.** The extension does not enable
  unrelated automatic tasks. An explicit user automatic-task preference of off,
  the existing folder opt-out, and `[sandbox].dashboard = false` suppress attach.
  Default-off automatic-task permission is distinct from an explicit user opt-out.
  The owned task remains a manual shortcut; reconciliation removes its old
  `folderOpen` option only when its bytes still match ownership. Retained task
  conflicts, deliberate removals and pending migration suppress the new opener.
  Existing compare-and-swap publication and issuance rollback remain in force.
- **Terminal lifetime.** One launch attempt per editor window session persists
  through an extension-host restart, which leaves its terminal running. A new
  window/reload can open the view again;
  deliberately closing it does not restart it in that session. The extension
  disposes only terminals it owns, with no terminal-name-based cleanup. Output
  channel diagnostics distinguish policy opt-outs and startup failures.

## Amendments (2026-10-27, GitHub Codespaces attach)

- **Codespaces is a Sandbox remote.** VS Code reports `env.remoteName` as
  `codespaces` in a GitHub Codespace, whose workspace extension host runs in the
  Codespace container. The workspace extension treats it like the Dev Containers
  remotes (`dev-container`, `attached-container`): trust, the attach protocol,
  the automatic-task preference, one opened folder, a Git Project Root and
  `booley dashboard --attach-policy` gate the same fixed terminal. Other remote
  kinds stay ineligible (#2255).
- **Skips before policy are visible.** An ineligible remote kind, an untrusted
  window and a disabled attach protocol each write one reason line to the
  **Booley Dashboard** Output channel. A local window, with no remote, stays
  quiet.

## Amendments (2026-10-15, Codex chat tab identity)

- **Marked Codex launches are an exception to thread identity.** The earlier
  prohibition on Codex process keys assumes a shared daemon. A named
  `booley chat` launch passes `--no-daemon`, reserves its process identity's
  handle, and marks that allocation before exec. Its own native CLI, including
  app-server helpers, resolves to that one process session. Built-in subagent
  conversations receive no separate Booley IDs. Ordinary unmarked Codex clients
  retain thread identity. This gives a terminal a stable handle without making
  the first conversation to call Booley its owner.
- **One runtime owner.** Launch reservations, MCP registration and lifecycle
  signals use the shared runtime Booley Directory, including `BOOLEY_DIR` when
  set. Checkout-specific agent-provider settings can still come from a local
  worktree snapshot. They do not choose a separate handle registry.
- **Migration and liveness.** The observer carries the original thread key
  through process attribution so registration can migrate a previous thread row
  and establish handle aliases. Historical allocations are never renumbered.
  Migration retains the predecessor's worktree when a call omits `work_dir`.
  A thread-ownership alias redirects delayed peerless observations only while
  that marked process is proven running in the same namespace; cached outcomes
  are normalized again before observer ordering.
  Marked process rows follow process liveness rather than thread quiet expiry.
- **Activity boundaries.** The lifecycle hook uses the same process identity
  and validates the same handle-map contract as the registry. Only the main
  conversation's events update that tab's activity. A separately launched inner
  Codex CLI, such as `codex exec`, retains its own thread attribution and cannot
  update the outer tab's signal. The nearest CLI must belong to the marked
  launch; its app-server helpers do not constitute another interactive client.

### 2026-10-16: Compact Doctor health (#1511)

The Dashboard always shows one compact Doctor line: active FAIL and WARN counts,
or dim `Doctor: OK` for a clean result. A due result adds ` · stale`, and a
missing or unreadable result shows a dim unavailable notice. This supersedes the
earlier “Health only when attention is needed” and “Hide the healthy Doctor line”
rules.
Independent upgrade-review notices and non-consuming, read-only snapshot rules
remain unchanged; full findings stay in the existing Doctor presentations.
