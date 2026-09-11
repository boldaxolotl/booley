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
When that expires, a singleton detached watcher continues polling without
holding the attach hook open and patches a later installation before the next
reload. The watcher is bounded too, so a sandbox that never ships VaporView
does not retain it indefinitely. Tune or disable the foreground and background
budgets with ``BOOLEY_VAPORVIEW_WAIT_SECONDS`` and
``BOOLEY_VAPORVIEW_WATCH_SECONDS`` respectively.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from typing import TextIO

from booley.core.boundary import as_float
from booley.runtime import file_lock, process_group, vaporview
from booley.runtime.vaporview import find_manifests

# Activation event that fires once per window after startup finishes, without
# needing an open waveform document — the trigger for the extension's own
# WCP auto-start.
_STARTUP_EVENT = "onStartupFinished"

# The two WCP settings whose declared scope we relax from ``application`` (host
# user-settings only) to ``machine`` (honored in the container's Machine
# settings, where the spec writes them).
_MACHINE_SCOPED_KEYS = ("vaporview.wcp.enabled", "vaporview.wcp.port")

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
_VAPORVIEW_BUNDLE = Path("dist") / "extension.js"

# Install-race wait: how long to wait for VaporView's manifest to land before
# concluding it is absent, how often to look, and the env knob that overrides
# the budget (``0`` disables the wait entirely). Kept modest — it is paid at
# most once per container (see :func:`_wait_for_manifests`).
_WAIT_SECONDS_DEFAULT = 20.0
_POLL_INTERVAL_SECONDS = 1.5
_WAIT_ENV = "BOOLEY_VAPORVIEW_WAIT_SECONDS"

# A late Marketplace retry can finish minutes after ``postAttachCommand`` has
# returned. Keep a detached, bounded watcher alive after the synchronous wait
# so the next window reload sees a patched manifest. An advisory lock prevents
# repeated attaches or manual invocations from accumulating watcher processes.
_WATCH_SECONDS_DEFAULT = 15 * 60.0
_WATCH_ENV = "BOOLEY_VAPORVIEW_WATCH_SECONDS"
_WATCH_FLAG = "--watch-for-install"
_WATCH_LOCK = "vaporview-install-watch.lock"


def _agent_home() -> Path:
    return vaporview.session_home()


def _patch_wcp_setting_scopes(contributes: dict) -> bool:
    """Make the container's machine-scoped WCP settings effective."""
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
        changed |= _patch_wcp_setting_scopes(contributes)
        changed |= _gate_manual_start(contributes)

    return changed


def _disable_remote_theme_lookup(extension_dir: Path) -> bool:
    """Keep VaporView from resolving a desktop theme inside the container.

    A missing or changed bundle is a clean no-op so an upstream VaporView
    update cannot break the devcontainer attach hook.
    """
    bundle = extension_dir / _VAPORVIEW_BUNDLE
    try:
        source = bundle.read_text(encoding="utf-8")
    except OSError:
        return False
    updated, replacements = _THEME_LOOKUP_CALL_RE.subn("void 0", source)
    if replacements != _THEME_LOOKUP_CALL_COUNT:
        return False
    try:
        bundle.write_text(updated, encoding="utf-8")
    except OSError:
        return False
    return True


def _patch_file(path: Path) -> bool:
    """Patch one VaporView install; return whether any file was rewritten."""
    manifest_changed = False
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        manifest = None
    if isinstance(manifest, dict) and patch_manifest(manifest):
        try:
            # 2-space indent matches VS Code's own manifest formatting; keeps
            # the diff minimal and the file human-readable if inspected.
            path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
            manifest_changed = True
        except OSError:
            pass
    bundle_changed = _disable_remote_theme_lookup(path.parent)
    return manifest_changed or bundle_changed


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


def _watch_budget_seconds() -> float:
    """Seconds for the detached late-install watcher to remain alive."""
    return _budget_seconds(_WATCH_ENV, _WATCH_SECONDS_DEFAULT)


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
    deadline = clock() + budget
    manifests: list[Path] = []
    while clock() < deadline:
        sleep(_POLL_INTERVAL_SECONDS)
        manifests = find_manifests(home)
        if manifests:
            break
    _mark_waited(sentinel)
    return manifests


def _open_watch_lock(home: Path) -> TextIO | None:
    """Open the singleton watcher lock file, or return ``None`` on I/O failure."""
    path = home / ".booley" / _WATCH_LOCK
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        return path.open("a+", encoding="utf-8")
    except OSError:
        return None


def _watch_for_late_install(home: Path, *, sleep, clock) -> bool:
    """Patch VaporView if it appears within the detached watch budget."""
    budget = _watch_budget_seconds()
    if budget <= 0:
        return False
    lock = _open_watch_lock(home)
    if lock is None:
        return False
    with lock:
        try:
            with file_lock.try_file_lock(lock) as acquired:
                if not acquired:
                    return False
                deadline = clock() + budget
                while clock() < deadline:
                    manifests = find_manifests(home)
                    if manifests:
                        return bool(sum(_patch_file(path) for path in manifests))
                    sleep(min(_POLL_INTERVAL_SECONDS, max(0.0, deadline - clock())))
        except OSError:
            return False
        return False


def _start_install_watcher(home: Path) -> bool:
    """Start the bounded watcher independently of the attach-hook process."""
    if _watch_budget_seconds() <= 0:
        return False
    env = dict(os.environ, HOME=str(home))
    try:
        subprocess.Popen(
            [sys.executable, "-m", "booley.runtime.incontainer_vaporview", _WATCH_FLAG],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=env,
            **process_group.new_group_kwargs(),
        )
    except OSError:
        return False
    return True


_USAGE_DESCRIPTION = (
    "Patch the container's VaporView extension so its WCP control server "
    "auto-starts safely on window load and an unavailable desktop theme uses "
    "the fallback palette quietly. Takes no arguments; runs from the "
    "devcontainer postAttachCommand. Idempotent, and a no-op when the extension "
    "is not installed."
)
_USAGE_EPILOG = (
    f"env: {_WAIT_ENV}=<seconds> bounds the once-per-container wait for the "
    f"extension install to land (default {_WAIT_SECONDS_DEFAULT:.0f}, 0 disables); "
    f"{_WATCH_ENV}=<seconds> bounds the detached late-install watcher "
    f"(default {_WATCH_SECONDS_DEFAULT:.0f}, 0 disables)."
)


def main(argv: Sequence[str] = (), *, sleep=time.sleep, clock=time.monotonic) -> int:
    """Patch every installed VaporView manifest; never fail the attach hook.

    Arguments are parsed even though there are none to accept: the module used
    to ignore ``argv`` entirely, so ``-m booley.runtime.incontainer_vaporview --help``
    silently *patched manifests* instead of printing help — a surprising side
    effect for anyone probing the hook. ``argv`` defaults to *empty*, not to
    ``sys.argv[1:]``: only the ``__main__`` entry point below speaks for the
    command line, so an in-process caller can never inherit stray argv.

    Parsing must not become a *new* way to fail the attach. argparse answers
    both ``--help`` and a bad flag by raising SystemExit — code 0 for the
    former, 2 for the latter — and letting the 2 escape would break the
    postAttachCommand over a typo, which is the one thing this module
    promises never to do. Both are swallowed into a 0 return: ``--help``
    has already printed the help text, a usage error has already printed
    its complaint, and neither leaves anything left to patch.
    """
    import argparse

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
        return 0

    home = _agent_home()
    if options.watch_for_install:
        _watch_for_late_install(home, sleep=sleep, clock=clock)
        return 0
    manifests = find_manifests(home)
    if not manifests:
        # Likely the install race: VS Code is still unpacking the extension
        # under us. Wait a bounded, once-per-container spell for it to land.
        manifests = _wait_for_manifests(home, sleep=sleep, clock=clock)
    if not manifests:
        # Install still in flight past the wait (or the viewer is genuinely
        # absent). Continue watching without keeping postAttachCommand alive.
        watching = _start_install_watcher(home)
        suffix = "; watching in background" if watching else "; nothing to patch"
        print(f"vaporview: extension not present yet{suffix}")
        return 0
    patched = sum(_patch_file(p) for p in manifests)
    if patched:
        print(
            f"vaporview: patched {patched} extension install(s) for container compatibility "
            "(effective after the next Reload Window)"
        )
    else:
        print("vaporview: extension already patched for container compatibility")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
