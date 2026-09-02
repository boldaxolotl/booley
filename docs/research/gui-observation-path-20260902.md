# Repeatable GUI observation for Booley QA scenarios

Research for [Determine a repeatable GUI-observation
path](https://github.com/boldaxolotl/booley/issues/248), against Booley
`5d3426eb7452ca09d2c3ba4f7333326138a5801a`, VaporView
`e6536bbdd1eefbfc01054d7c5d623f7523a68224` (`package.json` version
`1.5.4`), and the first-party documentation available on 2 September 2026.

## Decision

The initial scenario suite should use **layered evidence**, not one universal
GUI automation mechanism:

1. Use machine-verifiable product state and artifacts as the pass/fail oracle.
2. Add one narrow, structured human visual checkpoint for each Codex and Claude
   Code extension scenario, and one for VaporView rendering.
3. For VaporView, make WCP state read-back a required machine gate; the human
   checkpoint proves that the canvas actually rendered.
4. Treat computer-use screenshots as an optional discovery aid, not a required
   or sufficient regression oracle.
5. Do not build the first scenarios on VS Code's private smoke-test driver or
   pixel-perfect screenshot comparison.

This gives the initial bug-discovery runs visible UI coverage now, preserves
Windows/Linux portability, and leaves deterministic assertions suitable for
later regression use.

## What Booley exposes today

The generated devcontainer installs exactly one configured agent extension
(`Anthropic.claude-code` or `openai.chatgpt`) plus VaporView, and pins the agent
extension to the remote workspace extension host
([source](https://github.com/boldaxolotl/booley/blob/5d3426eb7452ca09d2c3ba4f7333326138a5801a/src/booley/harness/devcontainer.py#L78-L91),
[generated-spec source](https://github.com/boldaxolotl/booley/blob/5d3426eb7452ca09d2c3ba4f7333326138a5801a/src/booley/harness/devcontainer.py#L994-L1029)).
VS Code's CLI can list installed extensions with versions, so a scenario can
prove which build was present, but installation is not evidence that a panel
activated or completed a turn
([VS Code CLI](https://code.visualstudio.com/docs/configure/command-line#_working-with-extensions)).

Booley's shipped Interactive Mode path is an attached VS Code devcontainer.
The documentation identifies separate extension behavior for Claude and Codex,
and records MCP-driven outputs under the Project's interactive log directory
([Interactive Mode](https://github.com/boldaxolotl/booley/blob/5d3426eb7452ca09d2c3ba4f7333326138a5801a/docs/user/USAGE.md#L97-L144),
[logs](https://github.com/boldaxolotl/booley/blob/5d3426eb7452ca09d2c3ba4f7333326138a5801a/docs/user/USAGE.md#L205-L222)).
A prescribed prompt can therefore create strong external evidence: a fresh
Booley Flow/Specialist report, expected project artifact, and a changed MCP log
set. Those artifacts prove the requested Booley operation occurred, while a
visual checkpoint proves the operation was initiated and reported through the
named extension rather than a CLI substitute.

OpenAI documents opening the Codex sidebar from the Command Palette, supplying
editor context, and reviewing the result in the extension, but does not publish
a test/control protocol for the chat panel
([Codex IDE extension](https://developers.openai.com/codex/ide)). Anthropic
similarly documents the Claude Code panel, its Command Palette entries,
prompts, permission UI, and review UI
([Claude Code for VS Code](https://code.claude.com/docs/en/vs-code)). Anthropic
also documents screen-reader announcements and transcript landmarks, which are
useful human accessibility evidence, but they are not a cross-provider or
cross-platform automation API.

## Why VaporView is different

VaporView has a first-party loopback WCP server with newline-delimited JSON
requests, capability discovery, asynchronous `waveform_loaded`, and explicit
document targeting
([WCP documentation](https://github.com/Lramseyer/vaporview/blob/e6536bbdd1eefbfc01054d7c5d623f7523a68224/WCP_DOCS.md#L1-L149)).
Its `get_viewer_state` response reads back the document URI, both markers, time
unit, zoom, scroll position, and displayed signals
([state contract](https://github.com/Lramseyer/vaporview/blob/e6536bbdd1eefbfc01054d7c5d623f7523a68224/WCP_DOCS.md#L260-L307),
[implementation](https://github.com/Lramseyer/vaporview/blob/e6536bbdd1eefbfc01054d7c5d623f7523a68224/src/extension_core/wcp_server.ts#L1111-L1167)).

Booley already uses this channel for `bwave gui`: it waits for the exact
document to load, reads the viewer's displayed-item list rather than trusting
an `add_signal` acknowledgement, and sets the viewport and two markers
([read-back](https://github.com/boldaxolotl/booley/blob/5d3426eb7452ca09d2c3ba4f7333326138a5801a/src/booley/bwave/cli.py#L909-L991),
[scoped view](https://github.com/boldaxolotl/booley/blob/5d3426eb7452ca09d2c3ba4f7333326138a5801a/src/booley/bwave/cli.py#L1034-L1117)).
`booley doctor` separately distinguishes a configured viewer from a live WCP
server in an attached extension host
([live probe](https://github.com/boldaxolotl/booley/blob/5d3426eb7452ca09d2c3ba4f7333326138a5801a/src/booley/harness/doctor.py#L3468-L3596)).

WCP is therefore a strong semantic oracle, but not a pixel oracle. VaporView
draws waveforms into HTML canvas elements
([webview](https://github.com/Lramseyer/vaporview/blob/e6536bbdd1eefbfc01054d7c5d623f7523a68224/media/webview.html#L157-L186)).
A healthy state response can prove the webview holds the intended document,
signals, markers, and viewport state; it cannot by itself prove that readable
pixels appeared on screen. That remaining claim needs a visual observation.

## Automation options

| Mechanism | Evidence strength | Portability and nondeterminism | Setup and maintenance | Initial-suite disposition |
| --- | --- | --- | --- | --- |
| Booley artifacts, logs, extension inventory | Strong for installation and functional effects; no visual proof | High; assertions live in the Linux Session Runtime on either supported host OS | Low | **Required** |
| VaporView WCP state read-back | Strong semantic proof of the real running webview state; no pixel proof | High; loopback protocol is independent of host UI coordinates | Low; already integrated, with a small QA read-back wrapper still needed | **Required for VaporView** |
| VS Code extension integration tests | Strong for public VS Code APIs and contributed commands, but cannot generically inspect another extension's private chat/webview DOM | Cross-platform in a controlled Extension Development Host, but that is not automatically Booley's already-attached shipped window | Medium/high; requires a probe extension and separate launch/install lifecycle | **Defer** |
| VS Code's internal UI smoke driver | Can inspect and drive VS Code DOM and capture strong UI evidence | Timing, focus, shared state, version, and remote-window sensitive | Very high; the package is private and tests must match the VS Code release | **Reject for initial suite** |
| OS-level computer use plus screenshots | Direct proof of visible pixels and useful for exploratory bug finding | Sensitive to OS chrome, scaling, theme, layout, latency, focus, extension updates, and model vision | Medium/high per execution environment | **Optional discovery lane** |
| Structured human checkpoint | Direct visual proof with flexible recovery and works on Windows/Linux | Human judgement is nondeterministic but can be bounded by an exact checklist | Low implementation cost; recurring human cost | **Required fallback initially** |

VS Code's supported extension-test tooling launches tests in an Extension
Development Host with access to the VS Code API and can preinstall another
extension
([official testing guide](https://code.visualstudio.com/api/working-with-extensions/testing-extension)).
That is useful later for asserting extension registration, activation, and
public commands. It is not a general foreign-webview DOM API: VS Code describes
a webview as an iframe controlled by its owning extension and communicating
through message passing
([webview API](https://code.visualstudio.com/api/extension-guides/webview)).
The conclusion that a separate probe cannot inspect arbitrary chat DOM through
the supported API is an inference from that ownership model and the absence of
such an API in the reference.

VS Code does have its own UI automation driver, but its package is marked
`private` and is explicitly used by VS Code's smoke tests
([automation package](https://github.com/microsoft/vscode/blob/main/test/automation/package.json),
[README](https://github.com/microsoft/vscode/blob/main/test/automation/README.md)).
The smoke-test instructions require matching tests to the release under test
and warn about shared state, focus, and timing
([smoke-test README](https://github.com/microsoft/vscode/blob/main/test/smoke/README.md)).
Vendoring this internal harness would turn every VS Code update into a Booley
test-infrastructure project and still would not remove marketplace-extension or
authenticated-model nondeterminism.

## Initial scenario contract

### Codex and Claude Code extension checkpoints

For each provider-specific scenario:

1. Record VS Code, configured app, and installed extension versions. Assert the
   expected Marketplace ID is installed in the remote workspace.
2. In the attached devcontainer, open the named extension using the provider's
   documented UI path. Do not substitute `booley`, `codex`, or `claude` in a
   terminal for this checkpoint.
3. Send a prescribed, nonce-bearing, no-edit prompt that requires
   `booley_status` followed by one deterministic Booley Flow or Specialist.
4. Machine-verify the fresh Booley report/log/artifact, its target, verdict,
   and nonce-correlated run interval. Agent prose and an extension success icon
   are not sufficient.
5. At the completion boundary, a human checks: the correct branded extension
   is visible; the prompt appears in that extension; a Booley MCP operation is
   visible in the turn; the final response reports the expected target and
   verdict; and no unexpected login, approval, reconnection, or error UI is
   present. Record pass/fail and any finding immediately.
6. Optionally retain a redacted screenshot. Never require screenshots to
   contain credentials, account identity, private source, or provider billing
   information.

A computer-use runner may perform step 2, type step 3, and capture step 5 when
available. It must use stable Command Palette titles rather than coordinates
where possible, wait on observable UI states, and emit `not runnable` if it
cannot attach to the real window. Its screenshot does not replace step 4 or the
human fallback.

### VaporView checkpoint

1. Require an attached VS Code extension host and a passing live WCP probe.
2. Run a prescribed traced simulation and `bwave gui` command with exact signal
   paths, a vector signal, time range, and cursor/marker expectations.
3. After `waveform_loaded`, machine-read `get_viewer_state` for the explicit
   trace URI and assert the document, ordered displayed signals, main and
   alternate marker times, and viewport-equivalent zoom/scroll state. Also
   cross-check representative signal values with B-Wave so a plausible empty
   picture cannot pass.
4. A human checks that waveform rows, signal labels, transitions, both markers,
   and the requested time region are visibly rendered. Capture one redacted
   screenshot if policy permits.

The scenario runner should own the small WCP read-back/verifier helper so the
executing agent still follows only published Booley documentation, cheat sheets,
and help. The verifier observes the result; it does not teach the agent a
maintainer-only route around the product.

## Portability and later regression use

The scenario should not name an operating system. Use VS Code command titles,
not platform shortcuts, and run semantic checks inside the Session Runtime.
Record the host OS, display scale, VS Code version, and extension versions as
run metadata. An unavailable GUI or observer is `not runnable`, never a pass.

During early bug discovery, retain screenshots and human notes because they
surface rendering, layout, login, and notification failures that semantic
checks miss. Once workflows are routinely green, run the semantic assertions
as the regression gate and schedule the human visual checkpoints as a smaller
qualification lane. Promote computer-use to a required regression gate only
after it has a pinned, cross-platform adapter and demonstrates low flake rates
on both Windows and Linux; do not make that a prerequisite for publishing the
first scenarios.
