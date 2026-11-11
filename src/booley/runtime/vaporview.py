"""Authoritative VaporView installation-state probes, recovery guidance, and the
attached-window Viewer Bridge diagnosis."""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from booley.audit.diagnostic_results import DiagnosticReport, Findings
from booley.core.boundary import (
    BoundaryError,
    as_dict,
    is_str_list,
    require_bool_value,
    require_dict,
    require_list,
    require_opt_str,
)
from booley.runtime import runtime_context, vscode_server

EXTENSION_ID = "lramseyer.vaporview"
VERIFIED_VERSION = "1.6.0"
# The Sandbox Image bundles this exact official build (Dockerfile.base keeps its
# VAPORVIEW_VERSION in lockstep with VERIFIED_VERSION) and the generated spec
# installs it by path. A gallery ID cannot work there: an egress-locked Sandbox
# relays gallery installs to the client, which cannot resolve a versioned ID.
# VS Code records a VSIX install as pinned, so auto-update never replaces it.
BUNDLED_VSIX = f"/usr/local/libexec/booley/vaporview-{VERIFIED_VERSION}.vsix"
EDITOR_PROBE_TIMEOUT_SECONDS = 30

# Stable human-facing colors backed by VaporView custom palette slots. Its
# built-in palette has no red and its theme-derived indices are not
# deterministic, so every adapter consumes these settings and indices from one
# source of truth.
PRESENTATION_COLORS = {
    "red": ("vaporview.customColor5", 4, "#e5484d"),
    "blue": ("vaporview.customColor6", 5, "#3b82f6"),
    "green": ("vaporview.customColor7", 6, "#2da44e"),
}
PRESENTATION_COLOR_INDICES = {
    name: color_index for name, (_setting, color_index, _value) in PRESENTATION_COLORS.items()
}
PRESENTATION_COLOR_SETTINGS = {
    setting: value for setting, _index, value in PRESENTATION_COLORS.values()
}


class ExtensionState(Enum):
    """What an authoritative extension-registry observation established."""

    INSTALLED = "installed"
    MISSING = "missing"
    UNKNOWN = "unknown"


def session_home() -> Path:
    """Resolve the Sandbox user's home consistently on every host OS."""
    return Path(os.environ.get("HOME", "/home/agent"))


def find_manifests(home: Path) -> list[Path]:
    """Return installed VaporView manifests from every VS Code server root."""
    return [
        manifest
        for root in vscode_server.extension_dirs(home)
        for manifest in sorted(root.glob(f"{EXTENSION_ID}-*/package.json"))
    ]


def installed_versions(home: Path) -> list[str] | None:
    """Return the sorted versions of every live VaporView install, or None if unknown.

    Each extension root's ``extensions.json`` is VS Code's own registry and is
    authoritative: after a downgrade the replaced build's directory stays on
    disk, unmarked, until the next VS Code start. Roots without that file fall
    back to the directories VS Code has not marked obsolete. Any unreadable
    registry or manifest makes the answer unknown rather than partial, as does
    a home with no VS Code server root at all.
    """
    roots = vscode_server.extension_dirs(home)
    if not roots:
        return None
    versions: set[str] = set()
    for root in roots:
        root_versions = _registry_versions(root)
        if root_versions is None:
            return None
        versions |= root_versions
    return sorted(versions)


def _registry_versions(root: Path) -> set[str] | None:
    """Collect live VaporView versions from one extension root."""
    try:
        source = (root / "extensions.json").read_text(encoding="utf-8")
    except FileNotFoundError:
        return _directory_versions(root)
    except OSError:
        return None
    try:
        entries = json.loads(source)
    except ValueError:
        return None
    if not isinstance(entries, list):
        return None
    versions: set[str] = set()
    for entry in entries:
        fields = as_dict(entry, default={})
        identifier = as_dict(fields.get("identifier"), default={})
        ext_id = identifier.get("id")
        if not isinstance(ext_id, str) or ext_id.lower() != EXTENSION_ID:
            continue
        version = fields.get("version")
        if not isinstance(version, str):
            return None
        versions.add(version)
    return versions


def _directory_versions(root: Path) -> set[str] | None:
    """Collect VaporView manifest versions from non-obsolete extension directories."""
    try:
        entries = list(root.iterdir())
    except OSError:
        return None
    obsolete = _obsolete_extensions(root)
    if obsolete is None:
        return None
    versions: set[str] = set()
    for entry in entries:
        name = entry.name.lower()
        if not name.startswith(f"{EXTENSION_ID}-") or name in obsolete:
            continue
        try:
            payload = json.loads((entry / "package.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        version = payload.get("version") if isinstance(payload, dict) else None
        if not isinstance(version, str):
            return None
        versions.add(version)
    return versions


def probe_home(home: Path) -> ExtensionState:
    """Inspect every VS Code server root's registry; disagreeing roots are unknown."""
    return aggregate_states(_probe_registry(root) for root in vscode_server.extension_dirs(home))


def _probe_registry(root: Path) -> ExtensionState:
    """Inspect one extension registry without guessing on I/O failure."""
    try:
        entries = list(root.iterdir())
    except OSError:
        return ExtensionState.UNKNOWN
    obsolete = _obsolete_extensions(root)
    if obsolete is None:
        return ExtensionState.UNKNOWN
    candidates = [
        entry
        for entry in entries
        if entry.name.lower().startswith(f"{EXTENSION_ID}-") and entry.name.lower() not in obsolete
    ]
    manifest_states = [_manifest_matches(candidate / "package.json") for candidate in candidates]
    if any(state is True for state in manifest_states):
        return ExtensionState.INSTALLED
    if candidates:
        return ExtensionState.UNKNOWN
    return ExtensionState.MISSING


def _obsolete_extensions(root: Path) -> set[str] | None:
    """Return VS Code's obsolete extension-directory names, or unknown on corruption."""
    try:
        source = (root / ".obsolete").read_text(encoding="utf-8")
    except FileNotFoundError:
        return set()
    except OSError:
        return None
    try:
        payload = json.loads(source)
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    return {str(name).lower() for name, removed in payload.items() if removed is True}


def _manifest_matches(path: Path) -> bool | None:
    """Validate that a readable package manifest identifies VaporView."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    publisher = payload.get("publisher")
    name = payload.get("name")
    if not isinstance(publisher, str) or not isinstance(name, str):
        return None
    return f"{publisher}.{name}".lower() == EXTENSION_ID


def probe_editor(
    editor: str,
    *,
    run: Callable[..., subprocess.CompletedProcess[str]] | None = None,
) -> ExtensionState:
    """Ask one editor CLI for its extensions; failed queries remain unknown."""
    runner = run or subprocess.run
    try:
        result = runner(
            [editor, "--list-extensions"],
            capture_output=True,
            text=True,
            timeout=EDITOR_PROBE_TIMEOUT_SECONDS,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError):
        return ExtensionState.UNKNOWN
    if result.returncode != 0:
        return ExtensionState.UNKNOWN
    installed = {line.strip().lower() for line in result.stdout.splitlines()}
    if EXTENSION_ID in installed:
        return ExtensionState.INSTALLED
    return ExtensionState.MISSING


def aggregate_states(states: Iterable[ExtensionState]) -> ExtensionState:
    """Combine editor observations without turning uncertainty into absence."""
    observed = tuple(states)
    if observed and all(state is observed[0] for state in observed):
        return observed[0]
    return ExtensionState.UNKNOWN


def install_guidance(*, editor: str = "code") -> str:
    """Explain installing the image-bundled build into the attached remote window."""
    return (
        "Install the build bundled in the Sandbox Image from the attached remote "
        "window's terminal (no network needed):\n"
        f"  {editor} --install-extension {BUNDLED_VSIX}\n"
        "Booley's attach watcher patches a late install automatically; afterward run "
        '"Developer: Reload Window". Verify in the remote terminal with:\n'
        f"  {editor} --list-extensions --show-versions\n"
        f"which must list {EXTENSION_ID}@{VERIFIED_VERSION}. If {BUNDLED_VSIX} is "
        "missing, the Sandbox Image predates it: refresh the Sandbox. A host CLI "
        "without the remote window selected may install locally instead of into "
        "the container."
    )


# --- Attached-window Viewer Bridge diagnosis ------------------------------------

# Doctor line label for the `bwave gui` Viewer Bridge probe.
_VIEWER_BRIDGE_DESCRIPTION = "Viewer Bridge answers in the attached VS Code window (`bwave gui`)"

# Recovery when no attached window's bridge answers. The Booley workspace
# extension starts the Viewer Bridge when a window loads; an extension host
# that predates a spec refresh (or a spec that predates the bridge) has none.
_BRIDGE_RELOAD_FIX = (
    "run 'Developer: Reload Window' in the attached VS Code window so the Booley "
    "extension starts its Viewer Bridge; if Doctor also reports devcontainer drift, "
    "refresh the Sandbox so its spec installs the Booley Sandbox Integration extension"
)

_BRIDGE_MISSING_EXTENSION_FIX = install_guidance()
_BRIDGE_UNKNOWN_EXTENSION_FIX = (
    "could not determine whether VaporView is installed; verify it in the attached "
    "remote window, then install it if absent or run 'Developer: Reload Window' if present"
)

_VAPORVIEW_STATE_PROBE_SOURCE = (
    "import sys;from pathlib import Path;"
    "from booley.runtime.vaporview import ExtensionState,probe_home;"
    "s=probe_home(Path.home());"
    "sys.exit(0 if s is ExtensionState.INSTALLED else 3 if s is ExtensionState.MISSING else 4)"
)

# Prints the live VaporView versions as JSON; exit 4 means the registry could
# not be read. An older in-container Booley without installed_versions fails the
# import, which the runner also treats as unknown.
_VAPORVIEW_VERSIONS_PROBE_SOURCE = (
    "import json,sys;from pathlib import Path;"
    "from booley.runtime.vaporview import installed_versions;"
    "v=installed_versions(Path.home());"
    "print(json.dumps(v)) if v is not None else sys.exit(4)"
)

# Prints every live bridge's VaporView report as JSON. An in-container Booley
# without the bridge client fails the import, which the runner treats as
# "probe could not run" rather than as a dark window.
_VIEWER_BRIDGE_PROBE_SOURCE = (
    "import json;from booley.bwave.vaporview_bridge import doctor_summary;"
    "print(json.dumps(doctor_summary()))"
)

# Exit 0 when a VS Code extension host process runs, 3 when none does. Any
# other exit (an interpreter error, a missing python3) stays "unknown", so a
# broken probe can never pass for "no window attached".
_VSCODE_EXTENSION_HOST_PROBE_SOURCE = """\
import sys
from pathlib import Path

marker = b"bootstrap-fork\\x00--type=extensionHost"
attached = False
for cmdline in Path("/proc").glob("[0-9]*/cmdline"):
    try:
        attached = marker in cmdline.read_bytes()
    except OSError:
        continue
    if attached:
        break
sys.exit(0 if attached else 3)
"""


@dataclass(frozen=True, slots=True)
class ViewerBridgeRequest:
    """Inputs to inspect the attached VS Code window's Viewer Bridge and VaporView build."""

    project_root: Path
    docker_exe: str | None


def inspect_viewer_bridge(request: ViewerBridgeRequest) -> DiagnosticReport:
    """Live probe: does an attached window's Viewer Bridge answer, with VaporView ready?

    The static devcontainer spec check (``runtime.inspection``) proves the
    devcontainer *asks* for the viewer and the Booley Sandbox Integration
    extension, never that a window is running them. Without this probe the
    failure surfaces only when a human runs `bwave gui` in a Doctor-clean
    project. Never repairs anything.

    From the host, scope the probe to a VS Code-created devcontainer: a headless
    `booley session up` container has no extension host by design. From inside a
    runtime, probe its own socket directory because Docker metadata is
    unavailable there.
    """
    report = Findings()
    description = _VIEWER_BRIDGE_DESCRIPTION
    if runtime_context.inside_session_runtime():
        prefix: list[str] = []
        where = ""
        absent = f"{description} - no VS Code extension host attached"
    else:
        if not request.docker_exe:
            report.skip(f"{description} - container runtime unavailable")
            return report.report()
        # Deferred: session_runtime imports devcontainer, which imports this
        # module, and in-Sandbox probes that import this module stay light.
        from booley.runtime import session_runtime

        try:
            container = session_runtime.vscode_session_container(request.project_root)
        except session_runtime.SessionError as exc:
            report.fail(f"{description} - {exc}", "resolve the Sandbox inventory before retrying")
            return report.report()
        if container is None:
            report.skip(f"{description} - no VS Code devcontainer running for this project")
            return report.report()
        prefix = [request.docker_exe, "exec", container]
        where = f" in '{container}'"
        absent = f"{description} - no VS Code extension host attached in '{container}'"
    if _extension_host_running(prefix) is False:
        report.skip(absent)
        return report.report()
    _check_version(prefix, where, report)
    _check_bridges(prefix, where, report)
    return report.report()


def _run_probe(prefix: list[str], source: str) -> subprocess.CompletedProcess[str] | None:
    """Run one probe source with ``python3 -c``; None when the process cannot run."""
    try:
        return subprocess.run(
            [*prefix, "python3", "-c", source],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        return None


def _extension_host_running(prefix: list[str]) -> bool | None:
    """True/False when a live VS Code extension host can be detected, else None."""
    result = _run_probe(prefix, _VSCODE_EXTENSION_HOST_PROBE_SOURCE)
    if result is None or result.returncode not in (0, 3):
        return None
    return result.returncode == 0


def _extension_state(prefix: list[str]) -> ExtensionState:
    """Probe the remote extension registry; unexpected failures remain unknown."""
    result = _run_probe(prefix, _VAPORVIEW_STATE_PROBE_SOURCE)
    if result is not None and result.returncode == 0:
        return ExtensionState.INSTALLED
    if result is not None and result.returncode == 3:
        return ExtensionState.MISSING
    return ExtensionState.UNKNOWN


def _remote_versions(prefix: list[str]) -> list[str] | None:
    """Live VaporView versions in the remote registry, or None when unknown."""
    result = _run_probe(prefix, _VAPORVIEW_VERSIONS_PROBE_SOURCE)
    if result is None or result.returncode != 0:
        return None
    try:
        versions = json.loads(result.stdout)
    except ValueError:
        return None
    return versions if is_str_list(versions) else None


def _check_version(prefix: list[str], where: str, report: Findings) -> None:
    """Report whether the attached window runs the VaporView build Booley verified.

    The bridge answers for any VaporView build, but Booley's compatibility
    patch and its readback rules are verified against exactly one; only the
    installed version separates them.
    """
    description = f"VaporView version is the verified {VERIFIED_VERSION}"
    versions = _remote_versions(prefix)
    if versions is None:
        report.skip(f"{description} - version probe could not run{where}")
        return
    if not versions:
        # Absence is reported by the bridge check with install guidance.
        report.skip(f"{description} - VaporView not installed{where}")
        return
    other = [version for version in versions if version != VERIFIED_VERSION]
    if not other:
        report.pass_(description)
        return
    report.fail(
        f"VaporView {', '.join(other)} is installed{where}; Booley verifies only "
        f"{VERIFIED_VERSION}",
        "in the attached remote window's terminal run "
        f"`code --install-extension {BUNDLED_VSIX}`, then "
        "'Developer: Reload Window'; for a lasting fix refresh the Sandbox so "
        "its generated spec installs the bundled verified build",
    )


def _remote_bridges(prefix: list[str]) -> list[dict] | None:
    """Live bridge reports (``vaporview``/``error``/``trusted``), or None when the probe failed."""
    result = _run_probe(prefix, _VIEWER_BRIDGE_PROBE_SOURCE)
    if result is None or result.returncode != 0:
        return None
    try:
        summary = require_dict(json.loads(result.stdout), field="bridge probe")
        entries = require_list(summary.get("bridges"), field="bridge probe bridges")
        return [_bridge_entry(require_dict(item, field="bridge entry")) for item in entries]
    except (ValueError, BoundaryError):
        return None


def _bridge_entry(entry: dict) -> dict:
    """Validate one bridge report; ``trusted`` is None when the bridge omits it."""
    trusted = entry.get("trusted")
    return {
        "vaporview": require_opt_str(entry, "vaporview"),
        "error": require_opt_str(entry, "error"),
        "trusted": None if trusted is None else require_bool_value(trusted, field="trusted"),
    }


def _dark_bridge_fix(prefix: list[str], state: ExtensionState | None = None) -> str:
    """Choose recovery from an authoritative remote extension observation.

    ``state`` is a registry observation the caller already made; None probes.
    """
    state = _extension_state(prefix) if state is None else state
    if state is ExtensionState.MISSING:
        return "VaporView extension is not installed. " + _BRIDGE_MISSING_EXTENSION_FIX
    if state is ExtensionState.UNKNOWN:
        return _BRIDGE_UNKNOWN_EXTENSION_FIX
    return _BRIDGE_RELOAD_FIX


def _check_bridges(prefix: list[str], where: str, report: Findings) -> None:
    """Turn the bridge inventory into one pass/skip/fail line."""
    description = _VIEWER_BRIDGE_DESCRIPTION
    bridges = _remote_bridges(prefix)
    if bridges is None:
        report.skip(f"{description} - probe could not run{where}")
        return
    if not bridges:
        report.fail(f"{description}: no window answered{where}", _dark_bridge_fix(prefix))
        return
    # Restricted Mode disables VaporView, so a window that reports itself
    # untrusted cannot show it: that is waiting for the user, not a fault.
    broken = [entry for entry in bridges if entry["vaporview"] is None]
    waiting = [entry for entry in broken if entry["trusted"] is False]
    state = _extension_state(prefix) if waiting else None
    # The registry does not depend on trust, so a really absent VaporView
    # (MISSING) is never hidden behind a window that is not yet trusted.
    if state is ExtensionState.MISSING:
        waiting = []
    if len(broken) > len(waiting):
        reasons = "; ".join(sorted({entry["error"] or "no detail" for entry in broken}))
        report.fail(
            f"{description}: {len(broken)} of {len(bridges)} window(s){where} report no "
            f"usable VaporView ({reasons})",
            _dark_bridge_fix(prefix, state),
        )
        return
    _report_waiting_for_trust(len(bridges), len(waiting), where, report)


def _report_waiting_for_trust(windows: int, waiting: int, where: str, report: Findings) -> None:
    """Pass or skip a bridge inventory in which no window is broken."""
    description = _VIEWER_BRIDGE_DESCRIPTION
    if not waiting:
        count = f" ({windows} windows)" if windows > 1 else ""
        report.pass_(f"{description}{count}")
    elif waiting < windows:
        report.pass_(f"{description} ({windows} windows; {waiting} waiting for workspace trust)")
    else:
        report.skip(
            f"{description} - VaporView waits for workspace trust in {waiting} "
            f"window(s){where}; trust the folder, then run `booley doctor`"
        )
