# Codex chat tab handles (#1583)

Dated evidence record of the spike for issue
[#1583](https://github.com/boldaxolotl/booley/issues/1583), validated on
15 OCT 2026 against Sandbox Codex 0.160.0 (npm) and host Codex 0.162.0
(standalone Linux executable). It checked that `booley chat` can give each
Codex terminal tab its own `S<n>` handle that matches its Dashboard row. Read
it when you change how `booley chat` launches Codex or attributes its calls.
The identity rules it settled are recorded in
[ADR 0068's Codex chat tab identity amendment](../../adr/0068-booley-dashboard.md#amendments-2026-10-15-codex-chat-tab-identity);
this record keeps the launch design and the observed evidence. Results are not
updated afterward.

## Launch design

- **Keep Codex's own launcher.** For an npm install, a small Node ES module
  (`src/booley/data/codex_chat.mjs`) sets `process.title`, restores the argv
  the wrapper expects, and imports the installed `codex.js`. The wrapper still
  finds the native executable, sets its package-manager environment, forwards
  signals, and mirrors the exit status, so Booley copies none of its vendor
  paths or package-manager detection. A native Linux install keeps the launch
  PID and sets `argv[0]` directly.
- **Classify by file name only, on every launch.** Supported names are `codex`
  and the published x86_64/aarch64 Linux musl/gnu executable names. An npm
  update that retargets the executable symlink is therefore picked up. An
  unsupported install, or a Node runtime whose resolved name is not `node` or
  `nodejs` (for example a version-manager shim), falls back to a plain launch
  that reserves no handle, so a tab never carries an unusable identity. A
  shell wrapper named `codex` is launched as native: it reserves a handle but
  its tab title is not `S<n>`.
- **One private backend per tab.** A named launch passes `--no-daemon`. The
  [session identity spike](0067-goal-mode-session-identity.md) showed every
  Codex tab in a container sharing one app-server daemon, and walking that
  daemon's ancestors cannot recover which tab made a call. With its own
  backend, the launch process is a stable session identity, as Claude's
  process is.
- **One handle per terminal, not per conversation.** The handle names the main
  interactive session in the terminal. Built-in subagent conversations share
  its row and handle. This replaces the issue's suggested first-thread rule,
  so no conversation races to claim the reserved handle, and a subagent's
  Stop event cannot mark the main session idle.

## Observed results

- In a disposable `booley-sandbox:latest` container with networking disabled,
  two real `chat_cmd.run` launches through separate PTYs had foreground command
  names `S1` and `S2`. Both matched the handles returned by the real registry
  and Dashboard reader, which reported two sessions and a next-handle counter
  of 3. Peer attribution used the actual native child identities; no
  model-generated MCP call was made.
- The npm wrapper's native child kept
  `CODEX_MANAGED_PACKAGE_ROOT=/opt/agent-clis/node_modules/@openai/codex` and
  `CODEX_MANAGED_BY_NPM=1`.
- The ordinary npm launcher and the shim both reported `codex-cli 0.160.0` and
  the same logged-in ChatGPT status from the same read-only authentication
  file. The interactive TUI started normally.
- With a marked invalid model provider in `config.toml`, both npm launch paths
  failed with the same error naming that marker, so the shim reads the usual
  Codex configuration rather than an isolated profile.
- The standalone executable accepted a changed `argv[0]` and reported
  `codex-cli 0.162.0` and its existing logged-in status.
- A second offline Sandbox ran the real HTTP MCP server and a local Responses
  API stub with a synthetic model catalog and no account credentials. A
  managed Codex daemon was already running. A real `booley chat` terminal in a
  worktree with its own `.booley` snapshot executed the stub's `goal_status`
  call. Socket-owner attribution mapped the call to the launch's process
  session, terminal `S1` equaled the Dashboard handle, and Codex's own
  lifecycle events produced that session's `waiting for user input` signal.
  Nothing was injected. The shared runtime Booley Directory received the
  reservation; the worktree's snapshot did not get its own handle map. All
  model traffic stayed on container loopback.
- The pinned upstream
  [Codex hook dispatcher](https://github.com/openai/codex/blob/rust-v0.160.0/codex-rs/core/src/hook_runtime.rs)
  reports a subagent's completion as `SubagentStop`, which Booley ignores, and
  subagent tool hooks carry `agent_id`, which cannot update a named tab. Real
  subagent execution was not part of the offline probe.

Regression tests live in `tests/harness/test_codex_chat.py` (launch,
classification, handle persistence, the real Node shim) and
`tests/runtime/test_chat_sessions.py` (tab attribution, PID reuse, registry
migration). The selected chat, attribution, registry, handle, activity,
Dashboard, and text-only Specialist suites passed 329 tests on that date.

## Not verified at the time

- VS Code's visible tab labels: the planned manual check was to open two
  `booley chat` tabs in a Codex Project with VS Code's default `${process}`
  tab titles and confirm the `S<n>` labels match their Dashboard rows.
- The Claude comparison, which waited on
  [#1521](https://github.com/boldaxolotl/booley/issues/1521).
- Future Codex releases: the update test proves the executable is found
  again, not that an unknown future wrapper stays compatible.
