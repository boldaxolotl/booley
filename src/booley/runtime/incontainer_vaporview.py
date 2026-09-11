"""Patch the container's VaporView extension so its WCP control server
auto-starts on window load — run from the devcontainer ``postAttachCommand``
as ``python -m booley.runtime.incontainer_vaporview``.

Two independent VS Code facts otherwise defeat Booley's generated
``vaporview.wcp.enabled`` setting and leave bwave's scoped-view WCP calls with
"WCP server not running":

1. **Lazy activation.** VaporView ships ``activationEvents: []`` — the
   extension wakes only when a waveform document is the *active* editor. Its
   own auto-start (``if wcp.enabled && !server: server.start()``) therefore
   never runs on a fresh window, and every extension-host restart (Reload
   Window, container rebuild, crash-respawn) silently drops the server with no
   waveform tab to revive it. We add ``onStartupFinished`` to
   ``activationEvents`` so the extension activates on every window load and its
   auto-start runs unattended.

2. **Wrong setting scope.** ``vaporview.wcp.enabled`` / ``.port`` are declared
   ``scope: application``, and VS Code reads application-scoped settings ONLY
   from the *local host's* user settings — never from a remote container's
   Machine settings, which is exactly where the devcontainer
   ``customizations.vscode.settings`` land (ADR 0035). So Booley's
   ``wcp.enabled: true`` was inert for every user. We rewrite the scope to
   ``machine`` so the Machine-settings value the spec already writes is
   honored, with zero dependency on any user's host settings.

The same scope defect affects VaporView's custom colors. Booley reserves three
custom slots for the stable red/blue/green names accepted by ``bwave gui``;
the patch makes their generated Machine settings effective and updates their
manifest defaults so an existing runtime also receives the palette after
Reload Window.

VaporView 1.5.4 also loses the server object created by that auto-start path.
If its manual Start command activates the extension, auto-start binds the port
first and the command immediately tries to bind it again, producing a false
``EADDRINUSE`` error even though WCP is healthy. We disable the manual command
while ``wcp.enabled`` is true; it remains available when a user deliberately
turns auto-start off.

Finally, VaporView asks the remote extension registry to resolve the desktop's
active color theme. Desktop themes intentionally live on the UI host, not in
the sandbox, so the lookup fails and VaporView displays an error notification.
We disable the remote bundle's startup and theme-change lookup calls, leaving
``themeValid`` false so its webview derives fallback waveform colors from the
VS Code CSS variables already provided by the UI. A desktop theme is never
queried, copied, or installed in the container.

VaporView 1.5.4 has native, collapsible signal groups, but its WCP server
flattens them when reading viewer state and exposes no command that can create
them. We add two narrowly scoped WCP methods to the shipped bundle:
``set_signal_layout`` applies VaporView's own saved-row hierarchy, and
``get_signal_layout`` reads that same hierarchy back. ``bwave gui`` uses the
pair to create and verify presentation, and to restore the prior layout when a
later update fails. The patch is strictly shape-checked against the verified
1.5.4 bundle; an unfamiliar future bundle is left untouched rather than
partially rewritten.

Idempotent and defensive: a no-op if the extension is absent (install may still
be in flight), or already patched, and it never raises out of :func:`main` — a
patch failure must not fail the attach hook. Because the edits change the
extension *manifest*, they take effect at the NEXT extension-host start (VS Code
reads ``package.json`` once, at activation): the first window after a fresh
rebuild needs one Reload Window, and every reload/resume after that is
automatic. Re-running on every attach also re-applies the patch after a
VaporView update restores the vendored manifest.

**Install race.** The hook fires on *attach*, and VS Code installs the
configured extensions concurrently with that attach — so on the first window of
a fresh container the manifest can simply not be on disk yet when we look. A
single early miss used to leave the server dark until the user opened another
window because Reload Window does not rerun ``postAttachCommand``. We first wait
a short bounded spell for the manifest, paid **at most once per container**.
When that expires, a singleton detached watcher continues until VaporView is
installed, without holding the attach hook open. It also patches VS Code's
hidden staging directory before the completed extension is published into the
registry. That makes the patched manifest visible atomically to the extension
scanner, rather than racing the user's next Reload Window. Tune or disable only
the foreground budget with ``BOOLEY_VAPORVIEW_WAIT_SECONDS``.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO, TypeVar

from booley.core.boundary import as_float
from booley.runtime import file_lock, process_group, vaporview
from booley.runtime.vaporview import find_manifests

# Activation event that fires once per window after startup finishes, without
# needing an open waveform document — the trigger for the extension's own
# WCP auto-start.
_STARTUP_EVENT = "onStartupFinished"

# Settings whose declared scope we relax from ``application`` (host user
# settings only) to ``machine`` (honored in the container's Machine settings,
# where the generated spec writes them). The custom palette slots back
# `bwave gui`'s stable red/blue/green color names.
_MACHINE_SCOPED_KEYS = (
    "vaporview.wcp.enabled",
    "vaporview.wcp.port",
    *vaporview.PRESENTATION_COLOR_SETTINGS,
)

# VaporView's manual Start command races its own configured auto-start in 1.5.4.
# A declarative command enablement keeps it available in manual mode without
# patching the extension's bundled JavaScript.
_WCP_START_COMMAND = "vaporview.wcp.start"
_MANUAL_START_ENABLEMENT = "!config.vaporview.wcp.enabled"

# VaporView 1.5.4 resolves the active desktop theme at startup and whenever
# ``workbench.colorTheme`` changes. Both calls execute in the remote extension
# host, whose registry intentionally has no UI-side theme contributions. Leave
# its method definition in place but disable the two call sites; the webview's
# existing ``themeValid == false`` path uses UI-provided CSS color variables.
# Identifier names are flexible because the Marketplace bundle is minified.
_THEME_LOOKUP_CALL_RE = re.compile(r"[A-Za-z_$][A-Za-z0-9_$]*\.getTokenColorsForTheme\(\)")
_THEME_LOOKUP_CALL_COUNT = 2
_THEME_LOOKUP_METHOD = "getTokenColorsForTheme"
_VAPORVIEW_BUNDLE = Path("dist") / "extension.js"

# Exact anchors from the production-minified VaporView 1.5.4 bundle. Keeping
# the adapter here makes the upstream compatibility debt explicit and lets a
# changed upstream bundle fail closed. The injected methods deliberately call
# VaporView's public document.applySettings surface with its native saved-row
# representation instead of reproducing group behavior in Booley.
_LAYOUT_CAPABILITY = "set_signal_layout"
_WCP_SWITCH_ANCHOR = (
    'case"get_capabilities":t=await this.handleGetCapabilities();break;case"open_document":'
)
_WCP_SWITCH_PATCH = (
    'case"get_capabilities":t=await this.handleGetCapabilities();break;'
    'case"set_signal_layout":oe(i,["items"]),'
    "t=await this.handleSetSignalLayout(i);break;"
    'case"get_signal_layout":t=await this.handleGetSignalLayout(i);break;'
    'case"open_document":'
)
_WCP_HANDLER_ANCHOR = (
    "async handleGetCapabilities(){return{capabilities:await this.getCapabilitiesList()}}"
    "async handleOpenDocument(e)"
)
_WCP_HANDLER_PATCH = (
    "async handleGetCapabilities(){return{capabilities:await this.getCapabilitiesList()}}"
    "async handleSetSignalLayout(e){let t=this.getDocumentFromParams(e);"
    'if(!t)throw new Error("No active document");if(!Array.isArray(e.items))'
    'throw new Error("items must be an array");return await t.applySettings('
    "{displayedSignals:e.items,markerTime:e.marker_time,"
    "altMarkerTime:e.alt_marker_time,displayTimeUnit:e.time_unit,"
    "zoomRatio:e.zoom_ratio,scrollLeft:e.scroll_left},5,!1),{success:!0}}"
    "async handleGetSignalLayout(e){let t=this.getDocumentFromParams(e);"
    'if(!t)throw new Error("No active document");return{items:'
    "t.webviewContext.displayedSignals||[]}}async handleOpenDocument(e)"
)
_WCP_CAPABILITIES_ANCHOR = '"add_variables","get_capabilities","open_document"'
_WCP_CAPABILITIES_PATCH = (
    '"add_variables","get_capabilities","set_signal_layout","get_signal_layout","open_document"'
)

# Install-race wait: how long to wait for VaporView's manifest to land before
# concluding it is absent, how often to look, and the env knob that overrides
# the budget (``0`` disables the wait entirely). Kept modest — it is paid at
# most once per container (see :func:`_wait_for_manifests`).
_WAIT_SECONDS_DEFAULT = 20.0
_POLL_INTERVAL_SECONDS = 0.25
_WAIT_ENV = "BOOLEY_VAPORVIEW_WAIT_SECONDS"

# A late Marketplace retry can finish arbitrarily long after
# ``postAttachCommand`` returned. Keep one detached watcher for the container's
# lifetime so configured installs are never abandoned. It patches VS Code's
# hidden extraction directory before the final atomic rename, ensuring the
# extension scanner never observes the vendor manifest during a late install.
_WATCH_FLAG = "--watch-for-install"
_WATCH_LOCK = "vaporview-install-watch.lock"
_WATCH_LOG = "vaporview-install-watch.log"

_T = TypeVar("_T")


@dataclass(frozen=True)
class _PatchResult:
    """Outcome of one installation patch attempt."""

    complete: bool
    changed: bool = False
    detail: str | None = None


@dataclass(frozen=True)
class _PatchBatch:
    """Aggregate result for the installed, externally visible copies."""

    found: int
    changed: int
    incomplete: tuple[str, ...]

    @property
    def complete(self) -> bool:
        return self.found > 0 and not self.incomplete


def _agent_home() -> Path:
    return vaporview.session_home()


def _patch_machine_settings(contributes: dict) -> bool:
    """Make generated machine settings effective and seed color defaults."""
    changed = False
    configuration = contributes.get("configuration")
    blocks = (
        configuration
        if isinstance(configuration, list)
        else [configuration]
        if isinstance(configuration, dict)
        else []
    )
    for block in blocks:
        properties = block.get("properties") if isinstance(block, dict) else None
        if not isinstance(properties, dict):
            continue
        for key in _MACHINE_SCOPED_KEYS:
            prop = properties.get(key)
            if isinstance(prop, dict) and prop.get("scope") != "machine":
                prop["scope"] = "machine"
                changed = True
            default = vaporview.PRESENTATION_COLOR_SETTINGS.get(key)
            if isinstance(prop, dict) and default is not None and prop.get("default") != default:
                prop["default"] = default
                changed = True
    return changed


def _gate_manual_start(contributes: dict) -> bool:
    """Disable the broken manual Start path only while auto-start is enabled."""
    commands = contributes.get("commands")
    if not isinstance(commands, list):
        return False
    for command in commands:
        if (
            isinstance(command, dict)
            and command.get("command") == _WCP_START_COMMAND
            and "enablement" not in command
        ):
            command["enablement"] = _MANUAL_START_ENABLEMENT
            return True
    return False


def patch_manifest(manifest: dict) -> bool:
    """Apply the WCP integration fixes to a parsed ``package.json`` in place.

    Returns ``True`` when *manifest* was changed (so the caller only rewrites
    the file when needed), ``False`` when it was already fully patched.
    """
    changed = False

    # 1. Eager activation — append, never clobber, so a future version that
    #    declares its own activation events keeps them.
    events = manifest.get("activationEvents")
    if not isinstance(events, list):
        events = []
    if _STARTUP_EVENT not in events:
        manifest["activationEvents"] = [*events, _STARTUP_EVENT]
        changed = True

    # 2. Relax the WCP settings' scope and keep the redundant manual Start
    #    command out of the auto-start path that makes it race.
    contributes = manifest.get("contributes")
    if isinstance(contributes, dict):
        changed |= _patch_machine_settings(contributes)
        changed |= _gate_manual_start(contributes)

    return changed


def _manifest_is_complete(manifest: dict) -> bool:
    """Validate the current VaporView manifest shape before mutating it."""
    if f"{manifest.get('publisher')}.{manifest.get('name')}".lower() != vaporview.EXTENSION_ID:
        return False
    contributes = manifest.get("contributes")
    if not isinstance(contributes, dict):
        return False
    configuration = contributes.get("configuration")
    blocks = configuration if isinstance(configuration, list) else [configuration]
    properties = [block.get("properties") for block in blocks if isinstance(block, dict)]
    configured = {
        key
        for block in properties
        if isinstance(block, dict)
        for key in _MACHINE_SCOPED_KEYS
        if isinstance(block.get(key), dict)
    }
    commands = contributes.get("commands")
    has_start = isinstance(commands, list) and any(
        isinstance(command, dict) and command.get("command") == _WCP_START_COMMAND
        for command in commands
    )
    return configured == set(_MACHINE_SCOPED_KEYS) and has_start


def _atomic_write_text(path: Path, content: str) -> None:
    """Replace *path* atomically while preserving its permission bits."""
    mode = path.stat().st_mode
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.booley-",
            delete=False,
        ) as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
            temporary = Path(handle.name)
        temporary.chmod(mode)
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _prepare_theme_source(source: str) -> tuple[str, bool, str | None]:
    """Prepare the theme fallback in memory without risking a partial write."""
    updated, replacements = _THEME_LOOKUP_CALL_RE.subn("void 0", source)
    if replacements == _THEME_LOOKUP_CALL_COUNT:
        return updated, True, None
    if replacements == 0 and _THEME_LOOKUP_METHOD in source:
        return source, False, None
    return (
        source,
        False,
        (f"bundle has {replacements} of 2 expected theme lookup calls and is not fully extracted"),
    )


def _group_layout_patch_is_complete(source: str) -> bool:
    """Whether all injected group protocol surfaces are present."""
    return all(
        marker in source
        for marker in (
            'case"set_signal_layout"',
            'case"get_signal_layout"',
            "async handleSetSignalLayout",
            "async handleGetSignalLayout",
            '"set_signal_layout","get_signal_layout"',
        )
    )


def _prepare_theme_bundle(extension_dir: Path) -> tuple[Path, str | None, str | None]:
    """Return the bundle, an optional replacement, and an incomplete-state detail."""
    bundle = extension_dir / _VAPORVIEW_BUNDLE
    try:
        source = bundle.read_text(encoding="utf-8")
    except OSError as exc:
        return bundle, None, f"bundle is not readable yet ({exc})"
    updated, changed, problem = _prepare_theme_source(source)
    return bundle, updated if changed else None, problem


def _disable_remote_theme_lookup(extension_dir: Path) -> bool:
    """Keep VaporView from resolving a desktop theme inside the container."""
    bundle = extension_dir / _VAPORVIEW_BUNDLE
    try:
        source = bundle.read_text(encoding="utf-8")
    except OSError:
        return False
    updated, changed, problem = _prepare_theme_source(source)
    if not changed or problem is not None:
        return False
    try:
        _atomic_write_text(bundle, updated)
    except OSError:
        return False
    return True


def _prepare_grouped_layout_source(source: str) -> tuple[str, bool, str | None]:
    """Prepare the saved-row WCP adapter, shape-checking every anchor."""
    if _LAYOUT_CAPABILITY in source:
        if _group_layout_patch_is_complete(source):
            return source, False, None
        return source, False, "bundle contains an incomplete grouped-layout patch"
    replacements = (
        (_WCP_SWITCH_ANCHOR, _WCP_SWITCH_PATCH),
        (_WCP_HANDLER_ANCHOR, _WCP_HANDLER_PATCH),
        (_WCP_CAPABILITIES_ANCHOR, _WCP_CAPABILITIES_PATCH),
    )
    if any(source.count(anchor) != 1 for anchor, _patch in replacements):
        return source, False, "bundle grouped-layout anchors do not match VaporView 1.5.4"
    updated = source
    for anchor, patch in replacements:
        updated = updated.replace(anchor, patch, 1)
    return updated, True, None


def _enable_grouped_layout_wcp(extension_dir: Path) -> bool:
    """Expose VaporView's native saved-row hierarchy over WCP.

    Each anchor must occur exactly once before any replacement happens. This
    prevents a future or locally modified bundle from receiving a partial
    protocol patch. All injected surfaces form the idempotency marker.
    """
    bundle = extension_dir / _VAPORVIEW_BUNDLE
    try:
        source = bundle.read_text(encoding="utf-8")
    except OSError:
        return False
    updated, changed, problem = _prepare_grouped_layout_source(source)
    if not changed or problem is not None:
        return False
    try:
        _atomic_write_text(bundle, updated)
    except OSError:
        return False
    return True


def _prepare_compatible_bundle(extension_dir: Path) -> tuple[Path, str | None, str | None]:
    """Prepare every bundle compatibility edit as one atomic replacement."""
    bundle = extension_dir / _VAPORVIEW_BUNDLE
    try:
        source = bundle.read_text(encoding="utf-8")
    except OSError as exc:
        return bundle, None, f"bundle is not readable yet ({exc})"
    updated, theme_changed, problem = _prepare_theme_source(source)
    if problem is not None:
        return bundle, None, problem
    updated, layout_changed, problem = _prepare_grouped_layout_source(updated)
    if problem is not None:
        return bundle, None, problem
    changed = theme_changed or layout_changed
    return bundle, updated if changed else None, None


def _installation_is_patched(path: Path) -> bool:
    """Verify the manifest and bundle are at the patcher's fixed point."""
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
        bundle = (path.parent / _VAPORVIEW_BUNDLE).read_text(encoding="utf-8")
    except (OSError, ValueError):
        return False
    return (
        isinstance(manifest, dict)
        and _manifest_is_complete(manifest)
        and not patch_manifest(manifest)
        and _THEME_LOOKUP_CALL_RE.search(bundle) is None
        and _THEME_LOOKUP_METHOD in bundle
        and _group_layout_patch_is_complete(bundle)
    )


def _read_complete_manifest(path: Path) -> tuple[dict | None, str | None]:
    """Read and validate a manifest without accepting partial extraction."""
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return None, f"manifest is not readable JSON yet ({exc})"
    if not isinstance(manifest, dict):
        return None, "manifest root is not an object"
    if not _manifest_is_complete(manifest):
        return None, "manifest does not contain all expected VaporView fields"
    return manifest, None


def _patch_file(path: Path) -> _PatchResult:
    """Patch one complete VaporView install, retrying incomplete external state."""
    manifest, problem = _read_complete_manifest(path)
    if manifest is None:
        return _PatchResult(False, detail=problem)

    manifest_changed = patch_manifest(manifest)
    bundle, updated_bundle, problem = _prepare_compatible_bundle(path.parent)
    if problem is not None:
        return _PatchResult(False, detail=problem)

    try:
        # The bundle is prepared first and the manifest is the commit point:
        # eager activation cannot become visible before every compatibility
        # edit is present. Atomic replacements make interruption recoverable.
        if updated_bundle is not None:
            _atomic_write_text(bundle, updated_bundle)
        if manifest_changed:
            _atomic_write_text(path, json.dumps(manifest, indent=2) + "\n")
    except OSError as exc:
        return _PatchResult(False, detail=f"atomic replacement failed ({exc})")

    changed = manifest_changed or updated_bundle is not None
    if not _installation_is_patched(path):
        return _PatchResult(False, changed, "post-write verification failed")
    return _PatchResult(True, changed)


def _budget_seconds(name: str, default: float) -> float:
    """Read a non-negative seconds budget without failing the attach hook."""
    parsed = as_float(os.environ.get(name), default)
    assert parsed is not None
    return max(0.0, parsed)


def _wait_budget_seconds() -> float:
    """Seconds to wait synchronously for the manifest.

    ``0`` (or any non-negative override) is honored; a malformed value falls
    back to the default rather than failing the hook.
    """
    return _budget_seconds(_WAIT_ENV, _WAIT_SECONDS_DEFAULT)


def _wait_sentinel(home: Path) -> Path:
    """Marker recording that this container already spent its one install-race
    wait, so later attaches don't re-pay it when the viewer is truly absent."""
    return home / ".booley" / "vaporview-wait-done"


def _mark_waited(sentinel: Path) -> None:
    """Best-effort sentinel write; on failure we simply might wait again."""
    try:
        sentinel.parent.mkdir(parents=True, exist_ok=True)
        sentinel.touch()
    except OSError:
        pass


def _never_stop() -> bool:
    return False


def _poll_until(
    attempt: Callable[[], _T | None],
    *,
    sleep: Callable[[float], None],
    clock: Callable[[], float],
    deadline: float | None = None,
    stop: Callable[[], bool] = _never_stop,
) -> _T | None:
    """Poll *attempt* until it returns a result, a deadline, or cancellation."""
    while not stop():
        if deadline is not None and clock() >= deadline:
            return None
        result = attempt()
        if result is not None:
            return result
        delay = _POLL_INTERVAL_SECONDS
        if deadline is not None:
            delay = min(delay, max(0.0, deadline - clock()))
        sleep(delay)
    return None


def _wait_for_manifests(home: Path, *, sleep, clock) -> list[Path]:
    """Poll up to the budget for VaporView's manifest to appear, then return it.

    Returns ``[]`` immediately when the wait is disabled (budget ``0``) or was
    already spent for this container (sentinel present) — so only the first
    attach of a fresh container ever blocks, and only until the extension
    finishes installing. ``sleep``/``clock`` are injected for testing.
    """
    budget = _wait_budget_seconds()
    sentinel = _wait_sentinel(home)
    if budget <= 0 or sentinel.exists():
        return []
    manifests = _poll_until(
        lambda: find_manifests(home) or None,
        sleep=sleep,
        clock=clock,
        deadline=clock() + budget,
    )
    _mark_waited(sentinel)
    return manifests or []


def _open_watch_lock(home: Path) -> TextIO:
    """Open the singleton watcher lock file."""
    path = home / ".booley" / _WATCH_LOCK
    path.parent.mkdir(parents=True, exist_ok=True)
    return path.open("a+", encoding="utf-8")


def _manifest_identifies_vaporview(path: Path) -> bool:
    """Whether a staging manifest identifies the configured extension."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(payload, dict) and (
        f"{payload.get('publisher')}.{payload.get('name')}".lower() == vaporview.EXTENSION_ID
    )


def _staged_manifests(home: Path) -> list[Path]:
    """Find VaporView inside VS Code's hidden pre-publication directories."""
    root = home / ".vscode-server" / "extensions"
    return sorted(
        path for path in root.glob(".*/package.json") if _manifest_identifies_vaporview(path)
    )


def _patch_installed(home: Path) -> _PatchBatch:
    """Patch and verify every published VaporView installation."""
    manifests = find_manifests(home)
    results = [(path, _patch_file(path)) for path in manifests]
    incomplete = tuple(
        f"{path}: {result.detail or 'incomplete'}"
        for path, result in results
        if not result.complete
    )
    return _PatchBatch(
        found=len(results),
        changed=sum(result.changed for _, result in results),
        incomplete=incomplete,
    )


def _watch_for_late_install(
    home: Path,
    *,
    sleep: Callable[[float], None],
    clock: Callable[[], float],
    stop: Callable[[], bool] = _never_stop,
    err: TextIO = sys.stderr,
) -> bool:
    """Patch staging and published installs until a complete patch is verified."""
    lock_path = home / ".booley" / _WATCH_LOCK
    try:
        lock = _open_watch_lock(home)
    except OSError as exc:
        print(f"vaporview watcher: cannot open lock {lock_path}: {exc}", file=err)
        return False

    previous_problems: tuple[str, ...] = ()

    def attempt() -> _PatchBatch | None:
        nonlocal previous_problems
        for manifest in _staged_manifests(home):
            _patch_file(manifest)
        batch = _patch_installed(home)
        if batch.incomplete and batch.incomplete != previous_problems:
            print(
                "vaporview watcher: installation incomplete: " + "; ".join(batch.incomplete),
                file=err,
            )
        previous_problems = batch.incomplete
        return batch if batch.complete else None

    try:
        with lock, file_lock.try_file_lock(lock) as acquired:
            if not acquired:
                return False
            result = _poll_until(attempt, sleep=sleep, clock=clock, stop=stop)
    except OSError as exc:
        print(f"vaporview watcher: polling failed for home {home}: {exc}", file=err)
        return False
    if result is not None:
        print(f"vaporview watcher: verified {result.found} patched install(s)", file=err)
    return result is not None


def _start_install_watcher(home: Path) -> bool:
    """Start the persistent watcher independently of the attach-hook process."""
    command = [sys.executable, "-m", "booley.runtime.incontainer_vaporview", _WATCH_FLAG]
    env = dict(os.environ, HOME=str(home))
    log_path = home / ".booley" / _WATCH_LOG
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as log:
            subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=log,
                env=env,
                **process_group.new_group_kwargs(),
            )
    except OSError as exc:
        print(
            f"vaporview: could not start watcher {command!r} for home {home}; "
            f"log {log_path}: {exc}",
            file=sys.stderr,
        )
        return False
    return True


_USAGE_DESCRIPTION = (
    "Patch the container's VaporView extension so its WCP control server "
    "auto-starts safely on window load, grouped layouts can be controlled, "
    "and an unavailable desktop theme uses the fallback palette quietly. "
    "Takes no arguments; runs from the devcontainer postAttachCommand. "
    "Idempotent, and leaves a singleton repair watcher when the extension is "
    "not installed yet."
)
_USAGE_EPILOG = (
    f"env: {_WAIT_ENV}=<seconds> bounds the once-per-container wait for the "
    f"extension install to land (default {_WAIT_SECONDS_DEFAULT:.0f}, 0 disables). "
    "A singleton background watcher remains until the configured extension is installed."
)


def _parse_options(argv: Sequence[str]) -> argparse.Namespace | None:
    """Parse command-line options without letting usage errors fail the hook."""
    try:
        parser = argparse.ArgumentParser(
            prog="python -m booley.runtime.incontainer_vaporview",
            description=_USAGE_DESCRIPTION,
            epilog=_USAGE_EPILOG,
        )
        parser.add_argument(_WATCH_FLAG, action="store_true", help=argparse.SUPPRESS)
        options = parser.parse_args(argv)
    except SystemExit as exc:
        if exc.code:  # usage error — argparse already wrote it to stderr
            print("vaporview: bad arguments; nothing patched", file=sys.stderr)
        return None
    return options


def _report_patch(batch: _PatchBatch) -> None:
    if batch.changed:
        print(
            f"vaporview: patched {batch.changed} extension install(s) for container "
            "compatibility (effective after the next Reload Window)"
        )
    else:
        print("vaporview: extension already patched for container compatibility")


def _patch_on_attach(home: Path, *, sleep, clock) -> int:
    """Patch now or leave a persistent repair process for a late installation."""
    batch = _patch_installed(home)
    if not batch.found:
        _wait_for_manifests(home, sleep=sleep, clock=clock)
        batch = _patch_installed(home)
    if batch.complete:
        _report_patch(batch)
        return 0
    for problem in batch.incomplete:
        print(f"vaporview: installation incomplete: {problem}", file=sys.stderr)
    watching = _start_install_watcher(home)
    state = "incomplete" if batch.found else "not present yet"
    suffix = "; watching in background" if watching else "; automatic repair unavailable"
    print(f"vaporview: extension {state}{suffix}")
    return 0


def main(argv: Sequence[str] = (), *, sleep=time.sleep, clock=time.monotonic) -> int:
    """Patch every installed VaporView manifest; never fail the attach hook.

    ``argv`` defaults to empty so library callers never inherit ambient command
    line arguments. Help and usage errors retain argparse's output but are
    converted to success because this command is a best-effort lifecycle hook.
    """
    options = _parse_options(argv)
    if options is None:
        return 0

    home = _agent_home()
    if options.watch_for_install:
        _watch_for_late_install(home, sleep=sleep, clock=clock)
        return 0
    return _patch_on_attach(home, sleep=sleep, clock=clock)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
