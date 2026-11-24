"""Doctor's check of the bundled VS Code extensions in the attached window.

The Sandbox Image bundles the Verilog-HDL and Live Preview extensions and the
generated spec installs them by path. This module probes an attached window for
their installed versions and VSIX files.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable, Iterable
from dataclasses import dataclass
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
)
from booley.runtime import runtime_context, vscode_server
from booley.runtime.platform_paths import run_captured_bounded

# Doctor line label when the attached window cannot be inspected.
_DESCRIPTION = "Bundled VS Code extensions in the attached window"


def server_root_versions(server_root: Path, extension_id: str) -> list[str] | None:
    """Return the sorted live versions of an extension in one server root, or None if unknown.

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
class BundledExtensionsRequest:
    """Inputs to inspect the bundled extensions in the attached VS Code window."""

    project_root: Path
    docker_exe: str | None


def inspect_bundled_extensions(request: BundledExtensionsRequest) -> DiagnosticReport:
    """Live probe: does the attached window have the bundled extensions at the bundled versions?

    The static devcontainer spec check (``runtime.inspection``) proves the
    devcontainer *asks* for the extensions, never that a window is running
    them. Never repairs anything.

    From the host, scope the probe to a VS Code-created devcontainer: a headless
    `booley session up` container has no extension host by design. From inside a
    runtime, probe its own socket directory because Docker metadata is
    unavailable there.
    """
    report = Findings()
    description = _DESCRIPTION
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
            report.fail(
                f"{description} - {exc}",
                "fix the Sandbox inventory error, then re-run `booley doctor`",
            )
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
        "from booley.runtime.bundled_extensions import server_root_versions as v;"
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
    """Report each bundled extension, from one probe of the attached roots."""
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
