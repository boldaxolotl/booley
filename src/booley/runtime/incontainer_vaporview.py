"""Adapt the container's VaporView install to a remote Sandbox window — run
from the devcontainer ``postAttachCommand`` as
``python -m booley.runtime.incontainer_vaporview``.

VaporView runs in the Sandbox's remote extension host, which creates two
problems for Booley's fixed waveform palette:

1. **Theme lookup.** VaporView resolves the desktop's active color theme at
   startup and whenever ``workbench.colorTheme`` changes. Desktop themes live
   on the UI host, not in the Sandbox, so the remote lookup fails: VaporView
   shows an "Error getting user theme" notification on every window load and
   falls back to a grey palette. We disable both call sites in the shipped
   bundle and leave the method definition in place, so ``themeValid`` stays
   false and the webview builds its palette from the VS Code CSS variables
   plus the custom color slots. A desktop theme is never queried, copied, or
   installed in the container.

2. **Color setting scope.** ``vaporview.customColor5``..``7`` back
   ``bwave gui``'s stable red/blue/green names, but VaporView declares them
   ``scope: application``. VS Code reads application-scoped settings only
   from the local host's user settings, never from the container's Machine
   settings where the generated spec writes them. We rewrite those three
   properties to ``machine`` scope and seed their manifest defaults with
   Booley's palette, so the colors are right even before Machine settings
   apply.

The bundle edit is restricted to the exact SHA-256 of the official VaporView
release Booley verifies, and its result must hash to the expected patched
fingerprint; any other bundle is left untouched and reported, never partially
rewritten. The manifest edit applies only to that same version with all three
color properties present. Upstream follow-ups whose release lets Booley delete
this module: Lramseyer/vaporview#207 (theme lookup in remote windows) and
machine-scoped custom colors.

Idempotent and defensive: a no-op if the extension is absent (install may still
be in flight) or already patched, and it never raises out of :func:`main` — a
patch failure must not fail the attach hook. VS Code reads the manifest and
bundle when the extension host starts, so a patch that lands after the first
window's activation takes effect at the next Reload Window. Re-running on every
attach re-applies the patch after VS Code reinstalls the vendored files.

**Install race.** The hook fires on *attach*, and VS Code installs the
configured extensions concurrently with that attach — so on the first window of
a fresh container the manifest can simply not be on disk yet when we look.
Reload Window does not rerun ``postAttachCommand``, so we first wait a short
bounded spell for the manifest, paid **at most once per container**. When that
expires, a singleton detached watcher continues until VaporView is installed,
without holding the attach hook open. It also patches VS Code's hidden staging
directory before the completed extension is published into the registry, so
the extension scanner sees the patched files atomically rather than racing the
user's next Reload Window. Tune or disable only the foreground budget with
``BOOLEY_VAPORVIEW_WAIT_SECONDS``.
"""

from __future__ import annotations

import argparse
import hashlib
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
from booley.runtime import file_lock, process_group, vaporview, vscode_server
from booley.runtime.vaporview import find_manifests

# Settings whose declared scope we relax from ``application`` (host user
# settings only) to ``machine`` (honored in the container's Machine settings,
# where the generated spec writes them): the custom palette slots that back
# `bwave gui`'s stable red/blue/green color names.
_MACHINE_SCOPED_KEYS = tuple(vaporview.PRESENTATION_COLOR_SETTINGS)

# VaporView resolves the active desktop theme at startup and whenever
# ``workbench.colorTheme`` changes. Both calls execute in the remote extension
# host, whose registry intentionally has no UI-side theme contributions. Leave
# its method definition in place but disable the two call sites; the webview's
# existing ``themeValid == false`` path uses UI-provided CSS color variables.
# Identifier names are flexible because the Marketplace bundle is minified.
_THEME_LOOKUP_CALL_RE = re.compile(r"[A-Za-z_$][A-Za-z0-9_$]*\.getTokenColorsForTheme\(\)")
_THEME_LOOKUP_CALL_COUNT = 2
_THEME_LOOKUP_METHOD = "getTokenColorsForTheme"
_VAPORVIEW_BUNDLE = Path("dist") / "extension.js"

# Exact fingerprints of the official release bundle (``dist/extension.js``)
# before and after the theme edit. No other compiled bundle is rewritten.
_SUPPORTED_BUNDLE_SHA256 = "e73debb62c2ba1803c34bed5416ce786f5464b6f7453b2c9a18543cf48e880bd"
_PATCHED_BUNDLE_SHA256 = "62d1ca30dd29feeac7989fcb3255467e25ac611dbcee4878d9d11ffd678b9ab6"

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
# extension scanner never observes the vendor files during a late install.
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


def _configuration_properties(manifest: dict) -> list[dict]:
    """Return every ``contributes.configuration`` properties table."""
    contributes = manifest.get("contributes")
    configuration = contributes.get("configuration") if isinstance(contributes, dict) else None
    blocks = configuration if isinstance(configuration, list) else [configuration]
    return [
        block["properties"]
        for block in blocks
        if isinstance(block, dict) and isinstance(block.get("properties"), dict)
    ]


def patch_manifest(manifest: dict) -> bool:
    """Machine-scope Booley's color slots and seed their defaults, in place.

    Returns ``True`` when *manifest* was changed (so the caller only rewrites
    the file when needed), ``False`` when it was already fully patched.
    """
    changed = False
    for properties in _configuration_properties(manifest):
        for key in _MACHINE_SCOPED_KEYS:
            prop = properties.get(key)
            if not isinstance(prop, dict):
                continue
            if prop.get("scope") != "machine":
                prop["scope"] = "machine"
                changed = True
            default = vaporview.PRESENTATION_COLOR_SETTINGS[key]
            if prop.get("default") != default:
                prop["default"] = default
                changed = True
    return changed


def _manifest_is_complete(manifest: dict) -> bool:
    """Validate the verified VaporView manifest shape before mutating it."""
    if f"{manifest.get('publisher')}.{manifest.get('name')}".lower() != vaporview.EXTENSION_ID:
        return False
    if manifest.get("version") != vaporview.VERIFIED_VERSION:
        return False
    configured = {
        key
        for properties in _configuration_properties(manifest)
        for key in _MACHINE_SCOPED_KEYS
        if isinstance(properties.get(key), dict)
    }
    return configured == set(_MACHINE_SCOPED_KEYS)


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


def _bundle_sha256(source: str) -> str:
    """Fingerprint the exact UTF-8 bundle text used by the adapter."""
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


def _prepare_theme_source(source: str) -> tuple[str, bool, str | None]:
    """Disable both theme lookup calls in memory, or explain why not."""
    updated, replacements = _THEME_LOOKUP_CALL_RE.subn("void 0", source)
    if replacements == _THEME_LOOKUP_CALL_COUNT:
        return updated, True, None
    if replacements == 0 and _THEME_LOOKUP_METHOD in source:
        return source, False, None
    return (
        source,
        False,
        f"bundle has {replacements} of {_THEME_LOOKUP_CALL_COUNT} expected theme lookup calls",
    )


def _prepare_compatible_bundle(extension_dir: Path) -> tuple[Path, str | None, str | None]:
    """Prepare the exact supported bundle as one atomic replacement.

    Returns the bundle path, the replacement text (``None`` when the bundle is
    already patched), and an incomplete-state detail (``None`` on success).
    """
    bundle = extension_dir / _VAPORVIEW_BUNDLE
    try:
        source = bundle.read_text(encoding="utf-8")
    except OSError as exc:
        return bundle, None, f"bundle is not readable yet ({exc})"
    fingerprint = _bundle_sha256(source)
    if fingerprint == _PATCHED_BUNDLE_SHA256:
        return bundle, None, None
    if fingerprint != _SUPPORTED_BUNDLE_SHA256:
        return (
            bundle,
            None,
            f"bundle fingerprint {fingerprint} is not the official VaporView "
            f"{vaporview.VERIFIED_VERSION} build",
        )
    updated, changed, problem = _prepare_theme_source(source)
    if problem is not None:
        return bundle, None, problem
    if not changed or _bundle_sha256(updated) != _PATCHED_BUNDLE_SHA256:
        return bundle, None, "prepared bundle does not match the verified patched build"
    return bundle, updated, None


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
        and _bundle_sha256(bundle) == _PATCHED_BUNDLE_SHA256
    )


def _read_complete_manifest(path: Path) -> tuple[dict | None, str | None]:
    """Read and validate a manifest without accepting partial extraction."""
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return None, f"manifest is not readable JSON yet ({exc})"
    if not isinstance(manifest, dict):
        return None, "manifest root is not an object"
    if manifest.get("version") != vaporview.VERIFIED_VERSION:
        return (
            None,
            f"unsupported VaporView version {manifest.get('version')!r}; "
            f"expected {vaporview.VERIFIED_VERSION}",
        )
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
        # a patched manifest never becomes visible beside an unpatched bundle.
        # Atomic replacements make interruption recoverable.
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
    return sorted(
        path
        for root in vscode_server.extension_dirs(home)
        for path in root.glob(".*/package.json")
        if _manifest_identifies_vaporview(path)
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
    "Patch the container's VaporView extension so it skips the desktop theme "
    "lookup that fails in a remote window, and honors Booley's waveform colors "
    "from the container's Machine settings. "
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
