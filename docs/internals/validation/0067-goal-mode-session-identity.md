# Goal Mode session identity spike (ADR 0067 and 0068, Phase 0)

Validated on 06 OCT 2026 for [ADR 0068](../../adr/0068-booley-dashboard.md)
(session registry) and ADR 0067's shared-worktree warning. Decides what the
Session Registry can key a session on.

## Setup

Same Sandbox and throwaway server tools as the
[elicitation spike](0067-goal-mode-elicitation.md). `spike_client_info`
recorded, per call: the negotiated protocol, `clientInfo`, every request
`_meta` key, the loopback peer `(host, port)` from the ASGI scope, and the
process owning that peer socket, found by matching the port in
`/proc/net/tcp` to an inode and the inode to a `/proc/<pid>/fd` entry
(same container, same user). Two tabs of each client were open at once.
The third case, a host-driven `booley session enter -- <cmd>`, was
exercised with `curl` from the host through `session enter`.

## Results

| | Claude Code 2.1.285 | Codex 0.160.0 |
| --- | --- | --- |
| Tab identity on the wire | none: `_meta` carries only `claudecode/toolUseId` (per call) | yes: `_meta.threadId`, `sessionId`, `windowId` (`<thread>:0`), plus `x-codex-turn-metadata.{session_id,thread_id,turn_id}` |
| Peer-port owner, tab 1 | PID 132, cmdline `claude` | PID 2164, `codex app-server --listen unix:// --managed-daemon` |
| Peer-port owner, tab 2 | PID 287, cmdline `claude` (distinct) | PID 2164 (same daemon) |
| False-match rate, two tabs | 0 of 2 | 2 of 2: every Codex tab in the container shares one app-server daemon |
| `booley session enter -- cmd` from the host | the exec'd process is in the container's PID namespace; the peer resolved to the `curl` PID and its full command line | same mechanics |

The port-to-PID lookup itself worked on every call (no misses). Both
clients negotiate `2026-07-28`, whose `TransportContext` exposes headers
only, so the ASGI scope remains the only place the peer address is visible.

## Decision for D2

The registry key is, in order:

1. `codex:<_meta.threadId>` when the client sends a thread id;
2. `pid:<pid>:<start ticks>` from the peer-port lookup, for clients that
   do not multiplex tabs through one process: Claude Code and one-off
   `session enter` commands. Codex (`clientInfo.name` `codex-mcp-client`)
   never gets a PID key, because its tabs share one daemon and a Codex
   build without thread ids would collapse every tab into one row
   silently;
3. the call's resolved `work_dir` otherwise (including Codex without a
   thread id).

A PID key whose PID already owns a row with a different thread id is
treated as rule 3 as well, as a second line of defence. Rows live
independently of HTTP connections, since both clients open a fresh
connection per call (Claude PID 132 used ports 49874, 60700, and 59736
across its calls): a row is upserted on every call and pruned when its
PID is dead, its thread has been quiet beyond the threshold, or its
worktree is gone. The registry stays presentational (Dashboard rows and the shared-worktree
warning); no lifecycle rule reads it, so a wrong key degrades the display,
never the Goal record. ADR 0068's "one MCP connection is one session"
becomes "one agent client thread or process, falling back to the
worktree", and its host MCP daemon sentence is dropped.
