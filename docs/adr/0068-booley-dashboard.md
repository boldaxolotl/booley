# Booley Dashboard replaces the Console

Status: accepted

Once T3 Code replaces VS Code,
[ADR 0093](0093-show-jobs-per-worktree-in-the-t3-code-preview-panel.md)
replaces this ADR except its Jobs and Sandbox resources decisions.

## Context

The Console showed one Ticket execution at a time. Agents now run several
sessions, often unattended, in one Sandbox, so the human needs one place to see
all of them and the Jobs they started. The Dashboard is a terminal view, one
per Sandbox, opened by `booley dashboard`. It must never become a second
source of truth: it observes state other components own.

## Decision

### Scope and trust

- **One per Sandbox, no host-wide view.** It covers every worktree and session
  in that Sandbox. The agent client is not shown, because a Sandbox runs one.
- **Observational.** Navigation, filtering and refresh change no work. The
  human launches Booley Flows and Specialists from it as Standalone Jobs
  (ADR 0086). Goal-change approvals stay with the proposing MCP tool and chat,
  abandoning stays on the CLI, and the Dashboard never cancels, retries or
  otherwise changes a Job it did not start. It deletes no Job history.
- **Overview stays concise.** Sessions are the top-level rows; session, Job
  and Goal details are separate views. Sessions are grouped by what needs the
  human (waiting for user input or holding a pending proposal first). Doctor
  health is one compact line, always shown. Color carries the status meanings
  of the retired Console (green pass, red fail, orange needs recheck, dim not
  yet run), always with a label or symbol. Per-session token usage, active
  Specialist runs and Feedback findings are not shown.
- **Paths, not inline logs.** The Dashboard shows paths to published
  artifacts and opens one in the editor on click through the editor's command
  line. It does not embed a log viewer and does not use OSC 8 file links,
  because VS Code's terminal does not open them.
- **ADR 0092 replaces the Goal status, Goal views and Goal-change proposals**
  with one Measured Commit line per session when Goal Mode is removed.

### Sessions

- **A session is one agent client thread or process.** Codex sends a thread id
  in every request; Claude Code does not, but each tab is its own process,
  found from the loopback peer port; otherwise the key is the call's resolved
  `work_dir`. A Codex daemon shared by tabs never gets a process key, so a
  Codex client without a thread id falls back to the worktree. Built-in
  subagents get no separate session.
- **Rows come from Booley tool calls only.** `tools/list` and other requests
  create nothing, so a tab that never used Booley is invisible. A row goes
  away when its identity is dead, its worktree is gone, or the client reported
  the session ended after its last call. Ending is revocable: a later tool
  call shows the session again. A disconnected session keeps its Jobs and
  results.
- **The registry is presentational.** It feeds the Dashboard rows and the
  shared-worktree warning. No lifecycle rule reads it, and it never selects a
  Goal Record.
- **One exception: the caller's checkout routes `work_dir`.** ADR 0082
  decides how a call that omits `work_dir` reaches the caller's worktree.
- **Handles name things for humans.** Sessions are `S<n>` and Jobs are
  `J-<KIND><n>`; full keys and run IDs are not readable as names. Handles are
  reserved at registration or Job creation and persist across Dashboard
  restarts. Process keys display as the PID and namespace. Handles and
  readable keys are display only: stored keys, records and routing never use
  them. A handle-store failure never blocks a session or a Job; a damaged map
  is diagnosed, not reset.
- **Handles follow worktrees.** An interactive worktree is named after its
  session handle, so a handle is never reused while its worktree or branch
  exists, and allocation skips such numbers even when Git is unavailable.
  Pruning bounds map growth: expired entries go, and live ones never do.

### Session activity

- **Activity is reported, not inferred.** It is working, waiting for a tool
  call or waiting for user input, taken from agent-client lifecycle hooks.
  MCP silence or a running Job alone says nothing. A missing, ended or stale
  report shows unknown, never a guess. Busy states expire after the longest
  Booley tool call because a client that dies mid-turn sends no closing event.
- **Hooks live in the Sandbox Image's managed client policy**, so they run
  without per-user hook trust and survive a fresh state volume. Doctor warns
  when the image lacks them.
- **The hook script is standalone and does not import Booley.** Importing the
  package costs far more than the client's per-event wait budget allows over
  a long session, so the script repeats the event table and file layout and
  tests keep it in agreement with the reader. It always exits 0 and prints
  nothing on stdout.
- **A hook credits only its own session.** An inner client's events never
  update an outer session's activity; a hook that cannot name a session
  records nothing, so worktree-fallback sessions show unknown activity.
- **Awaited background work counts as waiting for a tool call.** A Claude Code
  `Stop` that leaves an MCP task, subagent or workflow in flight is not
  waiting for the user, because the client resumes the turn itself. Shells and
  monitors are not awaited (a dev server may run all session), and scheduled
  wakeups are not read.
- **Accepted inaccuracies:** parallel tool calls show the first to finish;
  after an approval the session shows waiting for user input until the tool
  ends; a tool call in flight at session end can bring the row back until it
  expires.

### Jobs

- **One Job list over filters.** Running, Finished and All filter the same
  list; an owner filter narrows it to a session or Standalone Jobs. A
  finished Job is still a Job. Execution completion is distinguished from
  passing checks, and execution failure from failed checks.
- **Finished history is bounded and accepts loss.** A snapshot lists a
  bounded number of finished Jobs, newest first, saying what it cut, so an old
  finished Job can drop out. Every unfinished Job is listed, whatever the age
  of its session.

### Attach

- **A workspace extension opens the Dashboard, not a devcontainer task.** VS
  Code treats automatic tasks as an application preference that a remote
  Machine setting cannot enable. The extension ships inside the Sandbox
  Image, installs without a Marketplace, and runs wholly inside the Sandbox.
  It supports Dev Containers and GitHub Codespaces and no other remote.
- **The attach is explicit policy with a fixed command.** The extension reads
  policy from Booley, opens a transient terminal running a fixed command, and
  Python rechecks configuration and ownership before starting. It respects a
  user's automatic-task preference and every opt-out (`[sandbox].dashboard =
  false`, the folder opt-out) and never enables unrelated tasks. A skip
  before policy writes its reason to an Output channel.
- **A command, `Booley: Open Dashboard`, is the explicit route.** Like manual
  `booley dashboard`, it ignores the automatic-attach settings but not
  workspace trust, and reveals an existing terminal instead of opening a
  second.
- **Open means seen.** A Sandbox socket decides who owns the view, but a
  socket alone does not prove the view is shown: a browser reload can leave a
  Dashboard process with no terminal. The Dashboard proves it is seen by a
  terminal round trip, and an unseen owner is retired and replaced.
  An owner that cannot prove it is unseen (older Booley, another user, a
  malformed reply) counts as open, so a live view is never taken away without
  evidence. The price is a short delay before a reload's view comes back.

## Consequences

- Hooks and the extension ship in the Sandbox Image; a session on an older
  image keeps the older behavior until the image is rebuilt, and Doctor says
  so.
- Hand-started Codex clients and worktree-fallback sessions have coarser
  identity and show unknown activity. That is accepted over guessing.
- Anything the Dashboard shows must come from state another component owns.
  A feature that needs the Dashboard to hold state, or a lifecycle rule that
  reads the registry, reopens this decision.
