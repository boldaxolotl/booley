# Codex chat tab handles (#1583)

Validated on 15 OCT 2026 against Sandbox Codex 0.160.0 (npm) and host Codex
0.162.0 (standalone Linux executable).

## Launch and identity decision

Use a small Node ES module to set `process.title`, restore the wrapper's
expected `process.argv`, and import the installed `codex.js`. Codex's wrapper
continues to discover the native executable, set its package-manager environment,
forward signals, and mirror exit status. Booley does not copy its vendor paths or
package-manager detection. Resolve the executable and package on every launch;
an npm update that retargets the executable symlink is covered by a unit test.
Native Linux installations can instead retain the launch PID and set `argv[0]`
directly. Supported executable basenames are `codex` and the published
x86_64/aarch64 Linux musl/gnu names; launcher, registry and hook agree on these
boundaries. Unsupported installations and naming failures use the plain launch.
This includes Node version-manager shims that add another process, and runtime
directory resolution failures; neither should reserve an unusable tab identity.

Pass `--no-daemon` for a named launch. The previous
[session identity spike](0067-goal-mode-session-identity.md) observed multiple
tabs sharing one Codex daemon. Walking that daemon's ancestors cannot recover
the tab that made a call. A private backend per launched tab makes the captured
launch process its stable session identity, as with Claude's process identity.

Reserve the process handle under the existing session-writer lock and persist a
`chat:codex:<process-key>` marker in the handle map. Only a matching, revalidated
launch ancestor can confer that identity. A nearer, separately launched Codex
CLI prevents inheritance; app-server helpers are part of their owning CLI.
Ordinary unmarked Codex launches continue to use thread attribution. Reservations
use the shared runtime Booley Directory, while provider settings can still come
from a checkout-specific snapshot.

The handle identifies the main interactive session represented by the terminal.
Built-in subagent conversations share its session row and handle; subagents do
not receive separate Booley IDs. Separate nested CLIs retain thread attribution.
This deliberately replaces the
issue's suggested first-thread inheritance rule. There is no race to decide
which conversation claims the reserved handle. The lifecycle hook uses the same
process identity, and a subagent's Stop event cannot mark the main session idle.

## Observed results

- In a disposable `booley-sandbox:latest` container with networking disabled,
  two real `chat_cmd.run` launches through separate PTYs had foreground command
  names `S1` and `S2`. Both matched the handles returned by the real registry and
  Dashboard reader, which reported two sessions and a next-handle counter of 3.
  Peer attribution in this check used the actual native child identities;
  it did not exercise a model-generated MCP call.
- The npm wrapper's native child retained
  `CODEX_MANAGED_PACKAGE_ROOT=/opt/agent-clis/node_modules/@openai/codex` and
  `CODEX_MANAGED_BY_NPM=1`.
- Both the ordinary npm launcher and the shim reported `codex-cli 0.160.0` and
  the same logged-in ChatGPT status with the same read-only authentication file.
  The interactive TUI started normally. No model inference was needed.
- With an identifying invalid model provider in `config.toml`, both npm launch
  paths produced the same configuration error containing that marker. The shim
  therefore reads the usual Codex configuration rather than an isolated profile.
- The standalone executable accepted a changed `argv[0]` and reported
  `codex-cli 0.162.0` and its existing logged-in status.
- The lifecycle hook's process-key signals were readable for both launched
  sessions. Hook inputs were injected for this check; actual Codex lifecycle
  event delivery was established separately below.
- A second disposable offline Sandbox ran the real HTTP MCP server and a local
  Responses API stub, with a synthetic model catalog and no account credentials.
  An existing managed Codex daemon was started first. A real `booley chat`
  terminal in a worktree with its own `.booley` snapshot then executed the stub's
  namespaced `goal_status` function call. Actual socket-owner attribution mapped
  its start/completion to the launch's process session, with terminal `S1` equal
  to the Dashboard handle. Codex-delivered lifecycle events produced that
  session's `waiting for user input` signal. No peer identity or hook payload was
  injected. The shared runtime received the reservation; the local worktree did
  not acquire its own handle map. All model traffic stayed on container loopback.

## Automated coverage

`test_codex_chat.py` covers native and npm command construction, resolving an
updated npm installation, handle persistence, missing runtime/executable,
unrecognized packages, failed process capture, corrupt handle storage, lock
contention, and the real Node shim's title, arguments, inherited environment and
exit status. Existing chat-command tests cover delegation to the prepared launch.

`test_chat_sessions.py` covers distinct tabs, shared conversation ownership,
ordinary unmarked ancestors, bounded/unreadable procfs, PID reuse, and migration
of an existing registry row without renumbering historical allocations. Observer
tests exercise predecessor migration after resuming an existing thread and after
a transient peer-lookup failure, including absence of a false shared-worktree
warning, retention of omitted worktree facts, and late completions after the
thread row has migrated. Persisted ownership cannot revive a vanished tab.
Other regressions cover worktree/runtime directory disagreement and nested CLI
boundaries for native and npm launches across supported executable basenames.
Lifecycle-hook tests cover matching activity keys, ignoring a subagent Stop,
excluding a nested CLI's Stop, and agreement with the registry on malformed maps.
The pinned upstream [Codex hook dispatcher](https://github.com/openai/codex/blob/rust-v0.160.0/codex-rs/core/src/hook_runtime.rs)
dispatches child completion as `SubagentStop`, which Booley ignores. Ordinary
subagent tool hooks carry `agent_id`; these cannot update a named tab even when
that ID equals their `session_id`. Actual subagent execution was not part of the
offline TUI probe. A Claude observer regression also verifies that worktree
fallback rows, histories and handles remain separate across peer recovery.

The selected chat, attribution, registry, handle, activity, Dashboard and
text-only Specialist suites passed 329 tests. Repository-wide Ruff lint and
format checks also passed. The process-identity exception is recorded in
[ADR 0068](../../adr/0068-booley-dashboard.md).

## Remaining manual acceptance

After installing the changed wheel and rebuilding the Sandbox base (the
standalone lifecycle script is part of that image), open two `booley chat` tabs
in a Codex Project with VS Code's default `${process}` tab titles. Confirm that
their visible `S<n>` labels match their Dashboard rows, and that a normal prompt
and tool call update session activity. Then compare with a Claude Project after
#1521 supplies Claude's naming behavior.

VS Code's visible tab labels have not been observed by this automated spike.
The Claude comparison remains dependent on #1521. Real future Codex releases
still require compatibility checks; the update test proves rediscovery rather
than promising compatibility with an unknown wrapper interface.
