# Booley Dashboard replaces the Console

Status: proposed (2026-10-03)

The Console showed one Ticket execution at a time. Goal Mode (ADR 0067)
removes Ticket execution and runs several agent sessions, often unattended,
in one Sandbox, so the human needs one place to see all of them. The Booley
Dashboard is a read-only terminal view, one per Sandbox, opened by
`booley dashboard`. The Console retires; its Textual code may be reused.

## Decisions

- **One per Sandbox.** A devcontainer task opens the Dashboard in a VS Code
  terminal on Sandbox attach. It covers every worktree and session in that
  Sandbox.
- **Sessions.** One MCP connection to Booley is one session, registered
  when it opens (worktree, branch, start time) and removed when it closes;
  entries whose process died are pruned. A connection, not a process,
  because the host MCP daemon may serve several sessions. A tab that never
  connects to Booley's MCP is invisible. The same registry drives ADR
  0067's shared-worktree warning.
- **v1 content.** Sessions are the top-level rows: worktree and branch,
  uptime, the last Booley MCP call and its raw age (no label or colour,
  since silence is ambiguous), its running Booley Flow jobs, and its Goal
  Mode if any (Goal Branch, each Goal met, unmet, or stale, and the count
  of pending Goal-change proposals). Jobs started outside any session are
  listed separately. Health shows Doctor status, its freshness, and a
  pending upgrade review. With no session or Goal Mode, jobs and health
  still show. The client is not shown: a Sandbox runs one agent client.
- **Sandbox only.** No host-wide view across Sandboxes in v1.
- **Read-only.** Approvals stay with the proposing MCP tool and chat;
  abandoning stays on the CLI.
- **Not in v1.** No "waiting on you" signal: Booley cannot see the chat,
  and inferring it from tool-call silence cannot tell waiting from
  thinking. Also deferred: active Specialist runs, Sandbox resources,
  per-session token usage, and recent Feedback findings.
