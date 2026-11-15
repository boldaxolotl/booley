# Drive VaporView through a Booley extension bridge

Status: accepted

## Context

`bwave gui` opens a trace in the VS Code window attached to the Sandbox and
shows a scoped view: chosen signals, named groups, radix and color, time
brackets, a cursor, appending to what is already shown. A scoped view that
cannot be built fails loudly; it never degrades to the whole trace.

VaporView's WCP server cannot carry that: no groups, radix, alternate marker
or state read-back, and its color command targets the active tab. Injecting
methods into the minified bundle fills the gaps but must be redone for every
upstream build. VaporView does expose public VS Code commands to open a file,
apply a whole saved session to the tab identified by URI, and read it back.
They are reachable only from code in the window's extension host, where
Booley already ships a workspace extension.

## Decision

### The channel is a bridge in Booley's own extension

- The Booley Sandbox Integration extension runs a Viewer Bridge in every
  attached window, independent of Dashboard policy. `bwave` speaks no WCP.
- The bridge offers generic primitives only (ping, open, read state, apply
  state, set viewport) and never builds rows. `bwave gui` composes the
  session, applies it, polls until the viewer reports it, and verifies rows,
  groups, formats, colors, markers, and that every added row resolved to a
  netlist item. Verification lives in Python, testable without VS Code.
- Unresolved signals are retried once under their literal name, then removed
  and reported in a warning. Any failure after the first apply restores the
  prior snapshot and verifies it came back.

### The transport is one local socket per window

Each bridge listens on its own Unix socket, one request per connection.
Filesystem permissions limit it to the Sandbox user, several windows coexist
without port allocation, and a refused connect marks a dead window. A loopback
TCP port would need a fixed number that collides across windows, or discovery
files, and would be reachable by every container user.

A command typed in a window's integrated terminal targets that window.
Otherwise `bwave` picks the only window, the one already showing the trace, or
the most recently focused one, and errors with guidance when it cannot tell.

### Viewport uses the per-document WCP listener, inside the bridge only

A session carries a viewport only as pixels per tick plus a scroll offset,
and the webview converts that with a pixel width nothing reports. An exact
time window therefore goes through the tab's own WCP listener, which uses the
live width. This is the one place WCP survives. The viewport is acknowledged,
not read back, because VaporView reports no change for it. VaporView rejects
a window ending past the trace, so `bwave` clamps the end (with a note) and
rejects a start past it before changing anything.

### Session files are Booley-owned

Applying a session makes its file the tab's save target, so the bridge writes
it in a Booley directory, never next to the trace: saving the tab cannot
overwrite a user's own session file.

### VaporView ships as a bundled, patched VSIX

- The Sandbox Image bundles a verified VaporView release as a VSIX and the
  generated spec installs it by path. A pinned gallery ID cannot work: a local
  Sandbox has no direct internet, the VS Code server relays gallery installs
  to the client, and the client cannot resolve a versioned ID. Drift detection
  treats any gallery VaporView entry as drift.
- Booley applies two edits, and only to the exact verified release (bundle
  and manifest fingerprints checked before and after; anything else is left
  untouched, never partly rewritten):
  1. The desktop theme lookup is disabled. In a remote window it runs in the
     container's extension host, cannot see desktop themes, raises an error on
     every window load, and paints every waveform grey. VaporView falls back
     to VS Code CSS colors plus its custom palette.
  2. The custom color slots behind the stable red, blue and green names move
     to machine scope with Booley's palette as defaults. Upstream scopes them
     to the application, which VS Code reads only from host user settings, so
     the generated Machine settings would be ignored.
- The image build patches the VSIX, so VS Code installs it already patched.
  An attach-time patch remains as a fallback for older images and installs
  from elsewhere; installs can land after attach, so it keeps watching.
- The patch is deleted once upstream fixes theme resolution in remote windows
  and makes the color settings machine-scoped. Booley does not extend the
  patch to cover more: re-injecting methods into a minified bundle is the cost
  this decision exists to avoid.
- GPL-3.0 notices travel with the modified VSIX.

Doctor fails when no window's bridge answers or a window reports no usable
VaporView.

## Accepted residuals

- Saved rows carry no bit range, so re-applying existing rows (`--append`,
  rollback) can turn a hand-added bit-slice into the whole vector, as
  VaporView's own Save/Load Session does. Every apply replaces all rows.
- VaporView can move a marker but never clear one. A plan sets and verifies
  only the markers it asks for; rollback cannot remove a marker a failed run
  created.

## Rejected alternatives

- **A WCP client in `bwave` against VaporView's configured endpoint.** It
  lacks groups, radix, the alternate marker and read-back, its color command
  targets the active tab, and every client of the endpoint shares one bound
  document.
- **Viewport from zoom and scroll in the session.** Needs the webview width,
  which can only be guessed.
- **Per-row marker and variable commands.** The marker command posts to the
  last active tab, and per-row commands cannot create groups or verify order.

## Consequences

- The bridge protocol is versioned; an older bridge is reported as needing a
  Sandbox refresh.
- Doctor's probe assumes `docker exec` runs as the Sandbox user; an image that
  ends as another user makes it report that no window answered.
- `bwave gui` works only from inside the Sandbox, against a window attached to
  that Sandbox.
- Unit tests mock `vscode`; the bridge needs a manual check in a real window.
