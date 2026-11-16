"""Authoritative VaporView installation-state probes, recovery guidance, and the
attached-window Viewer Bridge diagnosis, which also checks the bundled Verilog-HDL
and Live Preview extensions."""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from booley.audit.diagnostic_results import (
    DiagnosticFinding,
    DiagnosticReport,
    Findings,
    Severity,
)
from booley.core.boundary import (
    BoundaryError,
    as_dict,
    is_str_list,
    require_bool_value,
    require_dict,
    require_int,
    require_list,
    require_opt_str,
)
from booley.runtime import runtime_context, vscode_server
from booley.runtime.platform_paths import run_captured_bounded

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


def server_root_versions(server_root: Path, extension_id: str = EXTENSION_ID) -> list[str] | None:
    """Return the sorted live versions of an extension in one server root, or None if unknown.

    The extension is VaporView unless ``extension_id`` names another.

    The root's ``extensions.json`` is VS Code's own registry and is
    authoritative: after a downgrade the replaced build's directory stays on
    disk, unmarked, until the next VS Code start. A root without that file falls
    back to the directories VS Code has not marked obsolete. An unreadable
    registry or manifest makes the answer unknown rather than partial, as does
    a root with no extensions directory.
    """
    versions = _registry_versions(server_root / "extensions", extension_id)
    return None if versions is None else sorted(versions)


def _registry_versions(root: Path, extension_id: str) -> set[str] | None:
    """Collect live versions of one extension from one extension root."""
    try:
        source = (root / "extensions.json").read_text(encoding="utf-8")
    except FileNotFoundError:
        return _directory_versions(root, extension_id)
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
        fields = as_dict(entry) or {}
        identifier = as_dict(fields.get("identifier")) or {}
        ext_id = identifier.get("id")
        if not isinstance(ext_id, str) or ext_id.lower() != extension_id.lower():
            continue
        version = fields.get("version")
        if not isinstance(version, str):
            return None
        versions.add(version)
    return versions


def _directory_versions(root: Path, extension_id: str) -> set[str] | None:
    """Collect one extension's manifest versions from non-obsolete extension directories."""
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
        if not name.startswith(f"{extension_id.lower()}-") or name in obsolete:
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


def _vaporview_versions_probe_source(roots: tuple[str, ...]) -> str:
    """``python3 -c`` source printing the live VaporView versions of each named server root.

    A root whose registry cannot be read prints null. An older in-container
    Booley without ``server_root_versions`` fails the import, which the runner
    treats as "probe could not run".
    """
    return (
        "import json;from pathlib import Path;"
        "from booley.runtime.vaporview import server_root_versions as v;"
        f"print(json.dumps({{r:v(Path.home()/r) for r in {roots!r}}}))"
    )


# Prints every live bridge's VaporView report as JSON. An in-container Booley
# without the bridge client fails the import, which the runner treats as
# "probe could not run" rather than as a dark window.
_VIEWER_BRIDGE_PROBE_SOURCE = (
    "import json;from booley.bwave.vaporview_bridge import doctor_summary;"
    "print(json.dumps(doctor_summary()))"
)

# Exit 3 when no VS Code extension host process runs; else exit 0 printing the
# server roots (under the home directory) whose hosts run and how many hosts
# run elsewhere. Any other exit (an interpreter error, a missing python3) stays
# "unknown", so a broken probe can never pass for "no window attached". It is
# stdlib-only so an in-container Booley of any age can run it.
_VSCODE_EXTENSION_HOST_PROBE_SOURCE = """\
import json
import sys
from pathlib import Path

marker = b"bootstrap-fork\\x00--type=extensionHost"
names = __ROOT_NAMES__
home = Path.home()
roots = set()
unidentified = 0
attached = False
for cmdline in Path("/proc").glob("[0-9]*/cmdline"):
    try:
        head, found, _ = cmdline.read_bytes().partition(marker)
    except OSError:
        continue
    if not found:
        continue
    attached = True
    program = head.rsplit(b"\\x00", 1)[-1].decode("utf-8", "replace")
    try:
        root = Path(program).relative_to(home).parts[0]
    except (ValueError, IndexError):
        root = ""
    if root in names:
        roots.add(root)
    else:
        unidentified += 1
if attached:
    print(json.dumps({"roots": sorted(roots), "unidentified": unidentified}))
sys.exit(0 if attached else 3)
""".replace("__ROOT_NAMES__", repr(vscode_server.ROOT_NAMES))

_HOST_PROBE_FAILED = "extension host probe could not run"
_HOST_OUTSIDE_ROOTS = "VS Code extension host runs outside a known VS Code server root"


@dataclass(frozen=True, slots=True)
class _Attachment:
    """The server roots with a live extension host, and why any other host is unplaced."""

    roots: tuple[str, ...]
    unknown: str


@dataclass(frozen=True, slots=True)
class BundledExtension:
    """One extension the generated spec installs from a VSIX in the Sandbox Image.

    ``requirement`` is a sentence appended to the remedy when the extension
    needs more than the install.
    """

    name: str
    extension_id: str
    version: str
    vsix: str
    requirement: str = ""


@dataclass(frozen=True, slots=True)
class ViewerBridgeRequest:
    """Inputs to inspect the attached VS Code window's Viewer Bridge and VaporView build."""

    project_root: Path
    docker_exe: str | None


def inspect_viewer_bridge(request: ViewerBridgeRequest) -> DiagnosticReport:
    """Live probe: does an attached window's Viewer Bridge answer, with VaporView ready?

    Two more findings report whether the window has the Verilog-HDL and Live
    Preview extensions at the versions the Sandbox Image bundles.

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
    attachment = _attachment(prefix)
    if attachment is None:
        report.skip(absent)
        return report.report()
    _check_version(prefix, where, attachment, report)
    _check_bridges(prefix, where, report)
    _check_bundled_extensions(prefix, where, attachment, report)
    return report.report()


#: Seconds one ``python3 -c`` probe may run before its process tree is killed.
_PROBE_TIMEOUT_S = 20


def _run_probe(prefix: list[str], source: str) -> subprocess.CompletedProcess[str] | None:
    """Run one probe source with ``python3 -c``; None when the process cannot run."""
    try:
        return run_captured_bounded([*prefix, "python3", "-c", source], timeout=_PROBE_TIMEOUT_S)
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        return None


def _attachment(prefix: list[str]) -> _Attachment | None:
    """The attached server roots; None when no extension host runs.

    An answer that cannot be read is an attachment without roots whose reason
    says so: the extension checks then skip instead of reading every root.
    """
    unreadable = _Attachment((), _HOST_PROBE_FAILED)
    result = _run_probe(prefix, _VSCODE_EXTENSION_HOST_PROBE_SOURCE)
    if result is None or result.returncode not in (0, 3):
        return unreadable
    if result.returncode == 3:
        return None
    try:
        answer = require_dict(json.loads(result.stdout), field="extension host probe")
        names = require_list(answer.get("roots"), field="extension host roots")
        unidentified = require_int(answer.get("unidentified"), field="unidentified")
    except (ValueError, BoundaryError):
        return unreadable
    if not is_str_list(names) or not set(names) <= set(vscode_server.ROOT_NAMES):
        return unreadable
    if not names and unidentified < 1:
        return unreadable
    return _Attachment(tuple(names), _HOST_OUTSIDE_ROOTS if unidentified > 0 else "")


def _extension_state(prefix: list[str]) -> ExtensionState:
    """Probe the remote extension registry; unexpected failures remain unknown."""
    result = _run_probe(prefix, _VAPORVIEW_STATE_PROBE_SOURCE)
    if result is not None and result.returncode == 0:
        return ExtensionState.INSTALLED
    if result is not None and result.returncode == 3:
        return ExtensionState.MISSING
    return ExtensionState.UNKNOWN


def _remote_versions(
    prefix: list[str], roots: tuple[str, ...]
) -> dict[str, list[str] | None] | None:
    """Live VaporView versions per server root (None: unreadable), or None without an answer."""
    result = _run_probe(prefix, _vaporview_versions_probe_source(roots))
    if result is None or result.returncode != 0:
        return None
    try:
        answer = require_dict(json.loads(result.stdout), field="version probe")
    except (ValueError, BoundaryError):
        return None
    versions: dict[str, list[str] | None] = {}
    for root in roots:
        found = answer.get(root, False)
        if found is not None and not is_str_list(found):
            return None
        versions[root] = found
    return versions


def _check_version(
    prefix: list[str], where: str, attachment: _Attachment, report: Findings
) -> None:
    """Report whether each attached window runs the VaporView build Booley verified.

    The bridge answers for any VaporView build, but Booley's compatibility
    patch and its readback rules are verified against exactly one; only the
    installed version separates them.
    """
    description = f"VaporView version is the verified {VERIFIED_VERSION}"
    versions: dict[str, list[str] | None] = {}
    if attachment.roots:
        probed = _remote_versions(prefix, attachment.roots)
        if probed is None:
            report.skip(f"{description} - version probe could not run{where}")
            return
        versions = probed
    _report_per_root(
        report,
        description,
        where,
        attachment,
        lambda root: _version_finding(description, versions[root], where),
    )


def _version_finding(
    description: str, versions: list[str] | None, where: str
) -> DiagnosticFinding:
    """Judge one root's live VaporView versions."""
    if versions is None:
        return DiagnosticFinding(
            Severity.SKIP, f"{description} - version probe could not run{where}"
        )
    if not versions:
        # Absence is reported by the bridge check with install guidance.
        return DiagnosticFinding(Severity.SKIP, f"{description} - VaporView not installed{where}")
    other = [version for version in versions if version != VERIFIED_VERSION]
    if not other:
        return DiagnosticFinding(Severity.PASS, description)
    return DiagnosticFinding(
        Severity.FAIL,
        f"VaporView {', '.join(other)} is installed{where}; Booley verifies only "
        f"{VERIFIED_VERSION}",
        "in the attached remote window's terminal run "
        f"`code --install-extension {BUNDLED_VSIX}`, then "
        "'Developer: Reload Window'; for a lasting fix refresh the Sandbox so "
        "its generated spec installs the bundled verified build",
    )


_SEVERITY_RANK = {Severity.FAIL: 0, Severity.SKIP: 1, Severity.PASS: 2}


def _report_per_root(
    report: Findings,
    description: str,
    where: str,
    attachment: _Attachment,
    judge: Callable[[str], DiagnosticFinding],
) -> None:
    """Record one line judging every attached root on its own.

    A single result is recorded as is. Several become one line: the worst
    severity, every result worst first with its root named, and the worst
    result's remedy. A host outside the known roots adds a SKIP result.
    """
    results = [(f"[{root}] ", judge(root)) for root in attachment.roots]
    if attachment.unknown:
        skipped = f"{description} - {attachment.unknown}{where}"
        results.append(("", DiagnosticFinding(Severity.SKIP, skipped)))
    results.sort(key=lambda item: _SEVERITY_RANK[item[1].severity])
    worst = results[0][1]
    message = worst.message
    if len(results) > 1:
        message = "; ".join(f"{name}{finding.message}" for name, finding in results)
    if worst.severity is Severity.PASS:
        report.pass_(message)
    elif worst.severity is Severity.SKIP:
        report.skip(message)
    else:
        report.fail(message, worst.fix)


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


def _bundled_extensions_probe_source(
    extensions: Iterable[BundledExtension], roots: tuple[str, ...]
) -> str:
    """``python3 -c`` source printing, per extension ID, its live versions per root and VSIX presence.

    An in-container Booley without ``server_root_versions`` fails the import,
    which reads as "probe could not run".
    """
    pairs = tuple((item.extension_id, item.vsix) for item in extensions)
    return (
        "import json,os;from pathlib import Path;"
        "from booley.runtime.vaporview import server_root_versions as v;"
        "h=Path.home();"
        f"print(json.dumps({{i:{{'versions':{{r:v(h/r,i) for r in {roots!r}}},"
        f"'vsix':os.path.isfile(f)}} for i,f in {pairs!r}}}))"
    )


def _remote_bundled(
    prefix: list[str], extensions: tuple[BundledExtension, ...], roots: tuple[str, ...]
) -> dict[str, tuple[dict[str, list[str] | None], bool]] | None:
    """Per extension ID: (live versions per root, VSIX present); None if no answer."""
    result = _run_probe(prefix, _bundled_extensions_probe_source(extensions, roots))
    if result is None or result.returncode != 0:
        return None
    try:
        answer = require_dict(json.loads(result.stdout), field="bundled extension probe")
        observed: dict[str, tuple[dict[str, list[str] | None], bool]] = {}
        for item in extensions:
            entry = require_dict(answer.get(item.extension_id), field=item.extension_id)
            by_root = require_dict(entry.get("versions"), field="versions")
            versions: dict[str, list[str] | None] = {}
            for root in roots:
                found = by_root.get(root, False)
                if found is not None and not is_str_list(found):
                    raise BoundaryError("versions must be a list of strings or null")
                versions[root] = found
            observed[item.extension_id] = (
                versions,
                require_bool_value(entry.get("vsix"), field="vsix"),
            )
        return observed
    except (ValueError, BoundaryError):
        return None


def _check_bundled_extensions(
    prefix: list[str], where: str, attachment: _Attachment, report: Findings
) -> None:
    """Report each bundled extension besides VaporView, from one probe of the attached roots."""
    # Deferred: devcontainer imports this module.
    from booley.runtime.devcontainer import BUNDLED_EXTENSIONS

    observed: dict[str, tuple[dict[str, list[str] | None], bool]] = {}
    if attachment.roots:
        probed = _remote_bundled(prefix, BUNDLED_EXTENSIONS, attachment.roots)
        if probed is None:
            for extension in BUNDLED_EXTENSIONS:
                description = f"{extension.name} extension is the bundled {extension.version}"
                report.skip(f"{description} - extension probe could not run{where}")
            return
        observed = probed
    for extension in BUNDLED_EXTENSIONS:
        description = f"{extension.name} extension is the bundled {extension.version}"
        versions, vsix_present = observed.get(extension.extension_id, ({}, False))
        _report_per_root(
            report,
            description,
            where,
            attachment,
            lambda root, extension=extension, versions=versions, vsix_present=vsix_present: (
                _bundled_finding(extension, versions[root], vsix_present, where)
            ),
        )


def _bundled_finding(
    extension: BundledExtension, versions: list[str] | None, vsix_present: bool, where: str
) -> DiagnosticFinding:
    """Judge one bundled extension against what one root has."""
    name, version = extension.name, extension.version
    description = f"{name} extension is the bundled {version}"
    if versions is None:
        return DiagnosticFinding(
            Severity.SKIP, f"{description} - extension registry could not be read{where}"
        )
    other = [found for found in versions if found != version]
    if not versions:
        message = f"{name} extension ({extension.extension_id}) is not installed{where}"
    elif other:
        message = (
            f"{name} {', '.join(other)} is installed{where}; the Sandbox Image bundles {version}"
        )
    elif not vsix_present:
        message = f"{name} {version} is installed{where}, but its bundled VSIX is absent"
    else:
        return DiagnosticFinding(Severity.PASS, description)
    return DiagnosticFinding(Severity.FAIL, message, _bundled_fix(extension, vsix_present))


def _bundled_fix(extension: BundledExtension, vsix_present: bool) -> str:
    """The remedy: install the bundled VSIX, or rebuild the image when it is absent."""
    if vsix_present:
        fix = (
            "in the attached remote window's terminal run "
            f"`code --install-extension {extension.vsix}`, then 'Developer: Reload Window'"
        )
    else:
        fix = (
            f"{extension.vsix} is absent from the Sandbox Image: rebuild the Sandbox "
            "Image, then rebuild/reopen the Sandbox"
        )
    return f"{fix}; {extension.requirement}" if extension.requirement else fix
