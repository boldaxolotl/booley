# Drive VaporView through a Booley extension bridge

Status: accepted (amended 2026-10-28: VaporView ships as a bundled VSIX, see Amendments)

Supersedes the transport and compatibility-patch parts of ADR 0035 (scoped
Waveform Viewer views over VaporView's WCP server). ADR 0035's user-facing
contract stands: `bwave gui` opens a trace in the attached VS Code window and a
scoped view fails loudly rather than degrading to the whole trace.

## Context

`bwave gui` scoped views (signal lists, named groups, radix and color suffixes,
`--time` brackets, `--cursor`, `--append`, readback verification) were built on
VaporView 1.5.4: a newline-JSON WCP dialect on a fixed port, plus a
fingerprinted bundle patch that injected `set_signal_layout` /
`get_signal_layout`, forced eager activation, relaxed setting scopes, and gated
a racy manual Start command. VaporView 1.6.0
([#2262](https://github.com/boldaxolotl/booley/issues/2262),
[#2263](https://github.com/boldaxolotl/booley/issues/2263), parent
[#2254](https://github.com/boldaxolotl/booley/issues/2254)) replaced that
server with standard WCP v0: NUL-framed, greeting-first, no groups, no radix,
no alternate marker, no viewer-state readback, no URIs. `set_item_color`
targets the active tab. Re-injecting the old methods into a new minified bundle
would deepen the patch Booley wants to delete.

VaporView 1.6.0 does expose, as public VS Code commands, everything a scoped
view needs (verified against tag `1.6.0-rel`, d401914c):

- `vaporview.openFile{uri}` opens or reveals a tab and waits for its webview.
- `waveformViewer.loadViewerState{documentUri, sessionFileUri}` applies a whole
  saved session (rows, groups, number formats, color indices, both markers) to
  the tab identified by URI.
- `waveformViewer.getViewerState{uri}` reads the same session back.
- `vaporview.wcp.openWaveform(uri)` returns a per-document WCP v0 listener
  bound to that tab.

Those commands are reachable only from code running in the window's extension
host. Booley already ships a workspace extension in every Sandbox Image (the
Dashboard opener).

## Decision

**Channel: a Viewer Bridge in Booley's own extension.** The image-local
Booley Sandbox Integration extension (`data/vscode/dashboard/`, extension ID
`booley.dashboard`) starts a Viewer Bridge in every attached window,
independent of Dashboard policy, trust, or remote type. `bwave` no longer
speaks WCP; `bwave/wcp.py` is deleted. The generated spec installs the Booley
Sandbox Integration extension in every Sandbox (it was Dashboard-only), drops
the `vaporview.wcp.enabled`/`port` settings, and installs
VaporView 1.6.0 from the VSIX bundled in the Sandbox Image (amended
2026-10-28, see Amendments; it first pinned `lramseyer.vaporview@1.6.0`).
Drift detection requires exactly the bundled VSIX path as the VaporView
entry, the Booley Sandbox Integration extension, the color settings, and the attach
patch, so every spec from the previous generator reads as drift.

**Thin bridge, verification in Python.** The bridge offers generic
primitives only: `ping`, `open`, `state`, `apply`, `viewport`. `apply` writes
the requested session to a file and calls `loadViewerState`; it never builds
rows. `bwave gui` composes the complete session (a `--signals`/`--group`
request without `--append` starts empty; `--append`, or `--time`/`--cursor`
alone, starts from the read-back rows), applies it, polls `state` until
the viewer reports it, and checks it: row order, group membership, placement,
number format, color index, both markers, and that every row it added resolved
to a netlist item. VaporView 1.6.0 keeps an unresolved row as a placeholder
without a netlist id (the webview's `addVariable` creates the row either way);
bwave also accepts a viewer that leaves such a row out, treating an absent
added row as unresolved once the read-back state has changed from the
pre-apply state (or the settle deadline passes). bwave
retries a 1-bit `foo[3]` once under its literal name, then removes rows that
still have no netlist item, re-applies, and reports them in a WARNING. Any
failure after the first apply re-applies the snapshot taken before the first
change (rows, markers, time unit, zoom, scroll) and verifies its rows came back.

**Transport: one Unix socket per window.** Each bridge listens on
`~/.booley/vaporview-bridge/window-<extension-host-pid>.sock` (directory
0700, socket 0600). Each request is one connection carrying one JSON line, and
the bridge answers with one JSON line. Reasons: it needs no port allocation or
port setting; filesystem permissions restrict it to the Sandbox user; several
windows attached to one container (each with its own extension host) coexist
without contention; liveness is a connect, and a refused connect identifies a
dead window's socket, which the client removes. A loopback TCP port would need
a fixed number (collides across windows) or discovery files anyway, and is
reachable by every container user. The bridge also sets
`BOOLEY_VAPORVIEW_BRIDGE` in its window's integrated terminals, so a command
typed there targets that window. Otherwise bwave picks the only window, the
only window already showing the trace, then the most recently focused window,
and errors with guidance when it cannot tell. Sockets whose path would exceed
the platform `sun_path` limit are refused rather than truncated.

**Viewport: the per-document WCP listener, inside the bridge only.** A session
file can carry a viewport only as `zoomRatio` (pixels per tick) plus
`scrollLeft`, and the webview turns that into a time window using its own
pixel width, which neither the session nor any command reports. An exact
`--time START:END` window therefore uses `vaporview.wcp.openWaveform(uri)` and
WCP `set_viewport_range` on that tab's own listener, which converts with the
live width. A lone `--cursor` uses `set_viewport_to` to scroll the marker into
view. WCP v0 survives only in this one bridge function. Its NUL-framed,
greeting-first exchange is covered by a fake WCP v0 server in the bridge's Node
test (`tests/vscode/vaporview_bridge.test.cjs`), which replaces #2263's "v0
fake server" criterion; bwave has no WCP code to test. The viewport is
acknowledged but not read back, because VaporView reports no state change for
it. Markers, which used to be ack-only, are now read back.

VaporView rejects a window that ends past its `timeEnd`. That value is the last
time-table entry plus a small margin (`wellen_parser/lib.rs`), never less than
the FST end time `bwave list` reports as `total_ticks`, so bwave clamps a
`--time` end to `total_ticks` (with a note) and rejects a start at or past it
before changing anything.

**Session files are Booley-owned.** `loadViewerState` reads a file, not
inline state, and makes that file the tab's save target (Ctrl+S, auto-save, and
revert use it). The bridge writes one file per document under
`~/.booley/vaporview-bridge/sessions/` (atomically, 0600), never next to the
trace. Saving the tab therefore cannot overwrite a user's own session file.

**Residual patch, minimal.** `runtime/incontainer_vaporview.py` keeps exactly
two edits, restricted to the official 1.6.0 build. The bundle must hash to
`e73debb6…80bd` before and `62d1ca30…9ab6` after, and the manifest must be
version 1.6.0 with the three color keys:

1. A bundle edit replaces the two `getTokenColorsForTheme()` call sites (the
   provider constructor and the `workbench.colorTheme` change handler) with
   `void 0`. In a remote window the lookup runs in the container's extension
   host, which cannot see desktop themes. It fails with an "Error getting user
   theme" notification on every window load (1.6.0 activates at startup) and
   paints every waveform `#CCCCCC`. Skipped, VaporView uses its fallback path:
   VS Code CSS colors for slots 0–3 and the custom palette for 4–7.
2. A manifest edit moves `vaporview.customColor5..7` (the slots behind
   `@red`/`@blue`/`@green`, `PRESENTATION_COLORS`) to `machine` scope and
   seeds their defaults with Booley's palette. In 1.6.0 they are `application`
   scoped, which VS Code reads only from host user settings, so the generated
   spec's Machine settings would be ignored.

Activation, WCP-setting scope, manual-start gating, and injected WCP methods
are gone: 1.6.0 activates on startup natively, Booley no longer uses its
configured WCP endpoint, and the bridge replaced the injected methods. The
attach-time wait and the singleton watcher (including staging-directory
patching) stay, because Marketplace installs can land after attach.

The patch is deleted once upstream ships both follow-ups:
[Lramseyer/vaporview#207](https://github.com/Lramseyer/vaporview/issues/207)
(theme resolution in remote windows) and machine-scoped custom colors. The
second is not yet filed upstream; filing it is a follow-up owned by the
maintainer.

**Doctor** replaces the WCP port probe with a bridge probe run in the attached
container (or locally inside a Sandbox). It fails when no window's bridge
answers, or when a window reports no usable VaporView, and recommends Reload
Window, a Sandbox refresh, or the pinned install, depending on the extension
registry. The installed-version check from #2261 is kept.

## Accepted residuals

- Viewport placement is acknowledged, not verified (as before).
- VaporView's saved rows carry the bare instance path without `msb`/`lsb`.
  `--append` and rollback re-apply existing rows through that format, so a
  hand-added bit-slice row of a vector may come back as the whole vector. This
  is the same round trip as VaporView's own Save/Load Session.
- Every apply replaces all rows (that is how `loadViewerState` works), so the
  view redraws once per apply, up to three times when signals need a fallback
  or removal.
- VaporView can move a marker but never clear one: a session applies a marker
  only when its key is present, a `null` value only hides the label, and the
  extension never drops a stored marker time. So a plan sets, and readback
  verifies, only the markers it asks for; any other marker stays where it was
  (for example, `--cursor` after an earlier `--time` leaves the old END
  marker). Rollback moves existing markers back but cannot remove one a failed
  run placed where none existed.

## Rejected alternatives

**Keep a WCP client in bwave against 1.6.0's configured endpoint.** It has no
groups, radix, alternate marker, or readback, its color command targets the
active tab, and every client of that endpoint shares one bound document.
Re-adding those would mean another injected-method patch on a minified bundle.

**Viewport from `zoomRatio`/`scrollLeft` in the session.** Exact only if
Booley knew the webview width; guessing it, or deriving it from a zoom-to-fit
readback, is fragile and races with animation.

**`waveformViewer.setMarker`/`addVariable` per row.** `setMarker` posts to the
last active tab, and per-row commands cannot create groups or verify order.

## Consequences

- Doctor's bridge probe runs `python3` through `docker exec` and reads the
  bridge directory under that process's home, so it assumes `docker exec` runs
  as the Sandbox user (the image's final `USER agent`), as the installed-version
  probe already does. An image that ends as another user would make Doctor
  report that no window answered.
- One more component to keep in step: the bridge protocol is versioned
  (`protocol: 1`), and an older bridge is reported as needing a Sandbox
  refresh.
- `bwave gui` works only from inside the Sandbox, against a window attached to
  that Sandbox (as before).
- Live behavior of the bridge in a real VS Code attach (activation order,
  session-file loading, the per-document listener) is covered by unit tests
  with a mocked `vscode` module only, and needs a manual check in a real
  attached window.

## Amendments (2026-10-28)

- **VaporView ships as a bundled VSIX, not a pinned gallery ID.** A local
  Sandbox has no direct internet, so the VS Code server relays gallery
  installs to the client, and the client cannot resolve the versioned ID
  `lramseyer.vaporview@1.6.0`: VaporView never installed. The Sandbox runtime
  base now downloads the official 1.6.0 VSIX from Open VSX (SHA-256 pinned)
  to `/usr/local/libexec/booley/vaporview-1.6.0.vsix`, with its GPL-3.0
  license and a corresponding-source pointer under
  `/usr/local/share/booley/licenses/vaporview/`. The spec installs that path;
  VS Code records a VSIX install as pinned, so auto-update still leaves it
  alone. Drift detection requires that path as the only VaporView entry, so
  a spec carrying any gallery VaporView entry reads as drift. The installed
  files are byte-identical to the Marketplace build, so the residual patch's
  fingerprints are unchanged.
