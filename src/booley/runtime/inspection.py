"""Complete Sandbox observations, independent of Doctor presentation.

Inspection validates current issuance; it never issues or repairs a Runtime.
Host validation may create/lock the private authority store when EDA is active.
In-runtime validation probes mounted Vivado (90s), without host authority access.
A CLI timeout does not itself prove remote container cleanup. Live execution
must independently validate authority; a diagnostic report grants no authority.
"""

from __future__ import annotations

import functools
import hashlib
import json
import os
import re
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path

from booley.audit.diagnostic_results import DiagnosticReport, Findings
from booley.config import agent_app
from booley.config.eda import EdaConfig
from booley.core.boundary import require_dict
from booley.eda.provisioning.licensing.flexnet_docker import (
    RelayDockerError,
    RelayProfile,
    resources_for_session,
    validate_relay,
)
from booley.eda.provisioning.policies.vivado import (
    CONTAINER_TARGET,
    SUPPORTED_VERSION,
    wrapper_sha256,
)
from booley.runtime import (
    auth_token,
    image_identity,
    runtime_context,
    sandbox_layout,
    session_runtime,
    vaporview,
)
from booley.runtime import devcontainer as dc
from booley.runtime import interactive_docker as idk
from booley.runtime import session_issuance as runtime_spec
from booley.runtime.booley_dir import BOOLEY_DIR_NAME
from booley.runtime.devcontainer import (
    devcontainer_path,
    spec_mounts_token_seed,
    spec_state_is_persisted,
)
from booley.runtime.filesystem_utils import read_file_bytes


@dataclass(frozen=True)
class RuntimeInspectionRequest:
    """External inputs; Runtime collects its own spec, authority and live state."""

    project_root: Path
    image: str
    docker_exe: str | None
    declared_provider: str | None = None
    eda: dict[str, EdaConfig] | None = None
    fpga_enabled: bool = False
    expected_cache_mount: str | None = None
    booley_dir: Path | None = None

    @functools.cached_property
    def remedy(self) -> str:
        """Host workflow for this inspection, selected once on first use.

        One request is one inspection: callers build a fresh request per run.
        """
        return session_runtime.sandbox_remedy(self.project_root)


@dataclass(frozen=True)
class RuntimeInspectionResult:
    """Independent configuration currency and authority observations.

    A stale convenience setting does not skip authority validation. Consumers
    may inspect either result while Doctor renders their combined order.
    """

    configuration: DiagnosticReport
    issuance: DiagnosticReport

    @property
    def report(self) -> DiagnosticReport:
        return DiagnosticReport(self.configuration.findings + self.issuance.findings)


def inspect_runtime(request: RuntimeInspectionRequest) -> RuntimeInspectionResult:
    """Inspect spec drift then issuance, preserving independent failure findings.

    Files, Docker and authority reads happen here, not in the caller. Missing
    evidence is reported using each check's established failure/skip policy.
    """
    configuration = Findings()
    _check_devcontainer_spec(request, configuration)
    issuance = Findings()
    _check_issued_session_runtime(request, issuance)
    return RuntimeInspectionResult(configuration.report(), issuance.report())


def inspect_retained_resources(project_root: Path, docker_exe: str | None) -> DiagnosticReport:
    """Observe persistent volumes and image keepers without deleting resources."""
    report = Findings()
    _check_interactive_state_volumes(project_root, docker_exe, report)
    _check_issued_image_keepers(project_root, docker_exe, report)
    return report.report()


def _devcontainer_tracked(project_root: Path) -> bool:
    try:
        result = subprocess.run(
            ["git", "ls-files", "--", ".devcontainer"],
            cwd=project_root,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        return False
    return result.returncode == 0 and bool(result.stdout.strip())


def _check_devcontainer_spec(request: RuntimeInspectionRequest, report: Findings) -> None:  # noqa: PLR0911 — ordered drift preconditions
    """ADR 0018: untracked, valid devcontainer.json; never a tracked one.

    *request.image* is the project-resolved ``[sandbox].image``; the spec's own
    ``image`` must match it, else the Sandbox runs a stale image.
    *request.declared_provider* is the project's *explicit* ``[agent] provider``
    (``None`` = undeclared); the spec's ``BOOLEY_AGENT_APP`` must match it.
    """
    if _devcontainer_tracked(request.project_root):
        report.fail(
            ".devcontainer/ is tracked by git - Interactive Mode unavailable",
            "remove it from git history; Booley keeps the spec untracked",
        )
        return
    path = devcontainer_path(request.project_root)
    if not path.is_file():
        _spec_warning(
            request, report, "no .devcontainer/devcontainer.json - run `booley init` (or `--seed`)"
        )
        return
    try:
        spec = require_dict(
            json.loads(read_file_bytes(path).decode("utf-8")), field="devcontainer.json"
        )
    except (OSError, ValueError) as exc:
        report.fail(f"{path} does not parse: {exc}", "re-run booley init")
        return
    if not _provider_persistence_current(request, spec, report):
        return
    if not _image_current(request, spec, report):
        return
    seed_currency = _seed_mounts_current(request, spec, report)
    if seed_currency is False:
        return
    if not _viewer_extensions_current(request, spec, report):
        return
    if not _preview_terminal_current(request, spec, report):
        return
    if not _retired_entries_absent(request, spec, report):
        return
    if seed_currency is not None:
        report.pass_("devcontainer.json present and structurally current")


def _provider_persistence_current(
    request: RuntimeInspectionRequest, spec: dict, report: Findings
) -> bool:
    spec_app = dc.spec_agent_app(spec)
    pinned = request.declared_provider
    if pinned is not None and spec_app == agent_app.AUTO_APP:
        # "auto" resolves per start; a Sandbox without the pinned app's
        # credential registers the other app while Specialists keep the pin.
        _spec_warning(
            request,
            report,
            f"devcontainer.json BOOLEY_AGENT_APP 'auto' may resolve to an app other than "
            f"[agent] provider '{pinned}': without that app's credential the session "
            f"registers the other app while Specialists still use '{pinned}'; remove "
            f"[agent] provider or set BOOLEY_AGENT_APP to '{pinned}'",
        )
    elif pinned is not None and spec_app is not None and spec_app != pinned:
        report.fail(
            f"devcontainer.json BOOLEY_AGENT_APP '{spec_app}' != [agent] provider "
            f"'{request.declared_provider}': the Booley MCP entry is registered for "
            f"'{spec_app}', so a '{request.declared_provider}' session sees no Booley "
            f"MCP tools, and '{request.declared_provider}' home-state is not persisted",
            request.remedy,
        )
        return False
    if spec_state_is_persisted(spec) is False:
        _spec_warning(
            request,
            report,
            "devcontainer.json is stale: no persistent volume for the agent's "
            "home-state - in-container transcripts/plans are lost on every "
            "rebuild",
        )
        return False
    return True


def _image_current(request: RuntimeInspectionRequest, spec: dict, report: Findings) -> bool:
    spec_image = spec.get("image")
    if not isinstance(spec_image, str) or spec_image == request.image:
        return True
    comparison = idk.compare_issued_image(
        spec_image,
        request.image,
        project_root=request.project_root,
        executable=request.docker_exe or "docker",
    )
    if comparison.status is image_identity.Status.UNKNOWN:
        report.note(
            "devcontainer.json has an immutable image pin, but [sandbox].image "
            f"'{request.image}' cannot be compared conclusively here; image drift is unverified "
            "and host `booley doctor` is authoritative"
        )
    elif comparison.status is image_identity.Status.MISMATCH:
        _spec_warning(
            request,
            report,
            f"devcontainer.json image '{spec_image}' != logical [sandbox].image "
            f"'{request.image}': the Sandbox runs a stale image "
            "(missing toolchains it should have)",
        )
        return False
    return True


def _seed_mounts_current(
    request: RuntimeInspectionRequest, spec: dict, report: Findings
) -> bool | None:
    if request.expected_cache_mount and not dc.spec_mounts_target(
        spec, request.expected_cache_mount
    ):
        _spec_warning(
            request,
            report,
            "devcontainer.json omits the verified Nangate45 cache mount at "
            f"{request.expected_cache_mount}: ASIC synthesis will fail before "
            "elaboration",
        )
        return False
    remote_env = spec.get("remoteEnv")
    app = remote_env.get("BOOLEY_AGENT_APP") if isinstance(remote_env, dict) else None
    if app not in auth_token.CREDENTIALS:
        return True
    expected_seed = _expected_token_seed(request, app, report)
    if expected_seed is None:
        return None
    if expected_seed and spec_mounts_token_seed(spec) is False:
        _spec_warning(
            request,
            report,
            "devcontainer.json predates the stored `booley auth` credential: "
            "VS Code sessions fall back to the refreshing one",
        )
        return False
    return True


def _expected_token_seed(
    request: RuntimeInspectionRequest, app: str, report: Findings
) -> bool | None:
    from booley.config.agent import BackendConfigError, project_credential_policy
    from booley.core.booley_layout import BooleyLayoutError
    from booley.core.checkout_role import SourceCheckoutProjectError

    try:
        _, policy = project_credential_policy(request.project_root, booley_dir=request.booley_dir)
        return auth_token.credential_plan(app, policy).token_seed is not None
    except (
        BackendConfigError,
        BooleyLayoutError,
        SourceCheckoutProjectError,
        ValueError,
        OSError,
    ):
        report.fail(
            "Sandbox credential mount expectation is unavailable: Project credential policy or seed could not be inspected",
            "repair the selected Project credential policy and seed configuration, then refresh the Sandbox",
        )
        return None


def _viewer_extensions_current(
    request: RuntimeInspectionRequest, spec: dict, report: Findings
) -> bool:
    if not dc.spec_installs_vaporview(spec):
        _spec_warning(
            request,
            report,
            "devcontainer.json predates the current Waveform Viewer (ADR 0078) "
            "setup: the VaporView extension entry is missing or is not exactly the "
            f"image-bundled {vaporview.BUNDLED_VSIX}, or the Booley Sandbox Integration extension that hosts the "
            "Viewer Bridge, the waveform color settings, or the postAttach patch "
            "are missing, so in-container `bwave gui` finds no usable viewer",
        )
        return False
    if not dc.spec_installs_hdl_highlight(spec):
        _spec_warning(
            request,
            report,
            "devcontainer.json's Verilog/SystemVerilog highlighting extension "
            f"entry is missing or is not exactly the image-bundled {dc.HDL_VSIX}: "
            "RTL renders as plain text in attached VS Code windows",
        )
        return False
    return True


def _retired_entries_absent(
    request: RuntimeInspectionRequest, spec: dict, report: Findings
) -> bool:
    retired = dc.spec_retired_vscode_entries(spec)
    if retired:
        _spec_warning(
            request,
            report,
            "devcontainer.json still lists VS Code entries Booley no longer "
            f"installs ({', '.join(retired)})",
        )
        return False
    return True


def _preview_terminal_current(
    request: RuntimeInspectionRequest, spec: dict, report: Findings
) -> bool:
    if not dc.spec_installs_live_preview(spec):
        _spec_warning(
            request,
            report,
            "devcontainer.json lacks the collision-safe Live Preview setup (the "
            f"extension entry must be exactly the image-bundled {dc.LIVE_PREVIEW_VSIX}): "
            "rendered review HTML may open as a blank preview",
        )
        return False
    if not dc.spec_disables_python_terminal_activation(spec):
        report.note(
            "devcontainer.json predates the Python terminal activation fix: a "
            "synced Python extension can inject a delayed `source .venv/bin/activate` "
            "into new terminals; " + request.remedy
        )
        return False
    return True


def _spec_warning(request: RuntimeInspectionRequest, report: Findings, message: str) -> None:
    report.warn(
        message + "; " + request.remedy,
        check_id="interactive.devcontainer-drift",
        subject=str(devcontainer_path(request.project_root)),
    )


def _check_issued_session_runtime(request: RuntimeInspectionRequest, report: Findings) -> None:
    """Enforce the immutable host issuance and mounted-Vivado runtime contract."""
    if runtime_context.inside_session_runtime():
        _inspect_current_runtime(request, report)
        return
    from booley.runtime import session_issuance as runtime_spec

    path = devcontainer_path(request.project_root)
    try:
        spec = require_dict(
            json.loads(read_file_bytes(path).decode("utf-8")), field="devcontainer.json"
        )
        issuance = runtime_spec.validate(request.project_root, spec, path)
    except (OSError, ValueError, runtime_spec.RuntimeSpecError) as exc:
        report.fail(
            f"Sandbox host issuance is invalid: {exc}",
            request.remedy,
        )
        return
    report.pass_(f"Sandbox spec has valid host issuance ({issuance.spec_sha256[:12]})")

    if not request.docker_exe:
        report.skip("live issued Sandbox labels/topology - container runtime unavailable")
        return
    _check_runtime_booley_version(request, request.docker_exe, issuance.image, report)
    _inspect_live_runtime(request, spec, issuance, report)


def _inspect_current_runtime(request: RuntimeInspectionRequest, report: Findings) -> None:
    from booley.config.eda import PROVISIONING_HOST

    vivado = request.eda.get("vivado") if request.eda is not None else None
    # Identity and host-authority surfaces are independent: a wrong Booley
    # Directory must not hide an exposed Docker socket, credential or SSH key.
    _check_booley_dir_identity(request.project_root, report)
    if not _check_host_authority_isolation(report):
        return
    mounted = Path("/opt/booley-eda/vivado").is_dir()
    if (
        vivado is not None
        and vivado.provisioning == PROVISIONING_HOST
        and request.fpga_enabled
        and not mounted
    ):
        report.fail(
            "host-provisioned Vivado is absent from the Sandbox",
            session_runtime.SANDBOX_HOST_INSPECTION_REMEDY,
        )
        return
    if mounted:
        _check_mounted_vivado_runtime(report)
    else:
        report.pass_("Sandbox has no active host-mounted commercial EDA request")
    return


def _inspect_active_sandbox(
    request: RuntimeInspectionRequest,
    spec: dict,
    issuance: runtime_spec.Issuance,
    report: Findings,
) -> None:
    try:
        selected = session_runtime.resolve_running_sandbox(request.project_root)
        if selected is not None and not session_runtime._container_matches_issuance(
            selected.container_id,
            issuance,
            spec=spec,
            workspace=request.project_root,
            expected_id=selected.container_id,
        ):
            report.fail("active Sandbox differs from current host issuance", "rebuild its Sandbox")
    except session_runtime.SessionError as exc:
        report.fail(str(exc), "resolve the Sandbox inventory before retrying")


def _inspect_live_runtime(
    request: RuntimeInspectionRequest,
    spec: dict,
    issuance: runtime_spec.Issuance,
    report: Findings,
) -> None:
    _inspect_active_sandbox(request, spec, issuance, report)
    try:
        result = _list_issued_containers(request, issuance)
    except (OSError, subprocess.SubprocessError) as exc:
        report.fail(f"could not inspect issued Sandbox resources: {exc}", "start Docker")
        return
    containers = [name for name in result.stdout.splitlines() if name]
    drifted = [
        name
        for name in containers
        if not session_runtime._container_matches_issuance(
            name,
            issuance,
            spec=spec,
            workspace=request.project_root,
        )
    ]
    if result.returncode != 0:
        report.fail("could not list issued Sandbox resources", "start Docker")
    elif drifted:
        report.fail(
            "live Sandbox state differs from current host issuance",
            session_runtime.issued_runtime_drift_fix(
                request.project_root,
                issuance,
                drifted,
            ),
        )
    elif containers:
        report.pass_("live Sandbox state matches the current host issuance")
    else:
        report.pass_("no stale live Sandbox resources for this Project")
    _check_issued_license_relay(request, containers, issuance, report)


def _list_issued_containers(
    request: RuntimeInspectionRequest, issuance: runtime_spec.Issuance
) -> subprocess.CompletedProcess[str]:
    identity = next(
        label for label in runtime_spec.labels(issuance) if label.startswith("booley.project-id=")
    )
    return subprocess.run(
        [
            request.docker_exe,
            "ps",
            "-aq",
            "--filter",
            f"label={identity}",
            "--filter",
            f"label={dc.INTERACTIVE_ROLE_LABEL}",
            "--format",
            "{{.Names}}",
        ],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )


def _check_runtime_booley_version(
    request: RuntimeInspectionRequest, docker_exe: str, image: str, report: Findings
) -> None:
    """Require comparable host and issued-image Booley identities to agree."""
    result = idk.compare_issued_build(image, executable=docker_exe)
    if result.status is image_identity.Status.MISMATCH:
        report.fail(
            f"host and issued Sandbox Image contain different Booley code ({result.detail})",
            request.remedy,
        )
        return
    if result.status is image_identity.Status.MATCH:
        report.pass_("host and issued Sandbox Image use the same Booley code")


def _accepted_booley_dir(value: str, project_root: Path) -> bool:
    """Return whether ``value`` is an accepted in-Sandbox ``BOOLEY_DIR`` spelling (ADR 0073).

    Accepted spellings are the image alias ``/booley-project`` and the ``.booley``
    entry directly under the inspected Project Root, either one resolving to an
    existing directory. The entry may be a symlink; only its parent is resolved,
    so the comparison tolerates aliased Project Root spellings. Paths alone
    decide: ambient platform variables such as ``CODESPACES`` are spoofable
    (ADR 0077) and never widen the rule.
    """
    selected = Path(value)
    if value == sandbox_layout.BOOLEY_DIR_TARGET:
        # Compared as text: a POSIX alias is not "absolute" to a Windows-flavoured Path.
        return selected.is_dir()
    if not selected.is_absolute() or not selected.is_dir():
        return False  # relative spellings depend on cwd; dangling entries are not directories
    if selected.name != BOOLEY_DIR_NAME:
        return False
    try:
        return selected.parent.resolve(strict=True) == project_root.resolve(strict=True)
    except (OSError, RuntimeError):
        return False  # unresolvable parent or Project Root cannot prove identity


def _check_booley_dir_identity(project_root: Path, report: Findings) -> None:
    """Report whether ``BOOLEY_DIR`` names an accepted Sandbox Booley Directory."""
    value = os.environ.get("BOOLEY_DIR", "")
    if _accepted_booley_dir(value, project_root):
        report.pass_(f"Sandbox Booley Directory identity verified ({value})")
        return
    expected = project_root / BOOLEY_DIR_NAME
    report.fail(
        f"Sandbox Booley Directory identity is not accepted: BOOLEY_DIR={value or '<unset>'} "
        f"is neither {sandbox_layout.BOOLEY_DIR_TARGET} nor {expected}",
        f"set BOOLEY_DIR to {expected} (an existing directory or a symlink to one), "
        "or open the Project through an issued Sandbox",
    )


def _check_host_authority_isolation(report: Findings) -> bool:
    """Enforce host-authority absence (Docker socket, EDA config, SSH) in every runtime."""
    forbidden = (
        Path("/var/run/docker.sock"),
        Path("/run/docker.sock"),
        Path("/root/.config/booley/eda"),
        Path("/home/agent/.config/booley/eda"),
    )
    if any(_runtime_path_exposed(path) for path in forbidden):
        report.fail(
            "Sandbox exposes a forbidden host-authority surface",
            session_runtime.SANDBOX_HOST_INSPECTION_REMEDY,
        )
        return False
    for ssh_dir in (Path("/root/.ssh"), Path("/home/agent/.ssh")):
        entry = _ssh_authority_entry(ssh_dir)
        if entry is not None:
            report.fail(
                f"Sandbox exposes a forbidden host-authority surface: {entry}",
                session_runtime.SANDBOX_HOST_INSPECTION_REMEDY,
            )
            return False
    report.pass_("Sandbox host-authority isolation verified")
    return True


def _ssh_authority_entry(ssh_dir: Path) -> Path | None:
    """Return the first SSH entry granting outbound authority, or None.

    Inbound-only access (a real directory that is empty or holds just a
    regular ``authorized_keys``) is not host authority. All checks use lstat
    semantics so a symlink never stands in for a permitted file or directory.
    A mounted ``.ssh`` or ``authorized_keys`` is host authority: a host file
    bound in looks like a plain file, and a writable one lets a Sandbox
    process add its own key to log into the host.
    """
    try:
        mode = ssh_dir.lstat().st_mode
    except (FileNotFoundError, PermissionError):
        return None  # absent or agent-inaccessible path: isolated
    if not stat.S_ISDIR(mode):
        return ssh_dir  # symlink (even dangling) or non-directory
    try:
        mount_points = _runtime_mount_points()
        if str(ssh_dir) in mount_points:
            return ssh_dir  # mounted in from outside the Sandbox
        for entry in sorted(ssh_dir.iterdir()):
            if (
                entry.name != "authorized_keys"
                or not stat.S_ISREG(entry.lstat().st_mode)
                or str(entry) in mount_points
            ):
                return entry
    except OSError:
        return ssh_dir  # unlistable directory or unreadable mountinfo: fail closed
    return None


def _runtime_mount_points() -> frozenset[str]:
    """Return this process's mount points; raise OSError when mountinfo is unreadable."""
    mountinfo = Path("/proc/self/mountinfo").read_text(encoding="utf-8")
    fields = (line.split(" - ", 1)[0].split() for line in mountinfo.splitlines())
    return frozenset(parts[4] for parts in fields if len(parts) > 4)


def _runtime_path_exposed(path: Path) -> bool:
    """Treat an agent-inaccessible root path as isolated, not as a Doctor crash."""
    try:
        return path.exists()
    except PermissionError:
        return False


def _check_issued_license_relay(
    request: RuntimeInspectionRequest,
    containers: list[str],
    issuance: runtime_spec.Issuance,
    report: Findings,
) -> None:
    """Validate exact live relay bytes, endpoints, aliases, and hardening."""
    project_root = request.project_root
    try:
        profile = runtime_spec.requested_license(project_root)
    except runtime_spec.RuntimeSpecError as exc:
        report.fail(
            f"License Profile authority is invalid: {exc}", "repair the host EDA authority"
        )
        return
    if profile is None:
        return
    image_id = issuance.relay_image_id
    if not isinstance(image_id, str):
        report.fail(
            "issued License Profile lacks an immutable relay image",
            request.remedy,
        )
        return
    relay = resources_for_session(str(project_root.resolve()))
    if not session_runtime._relay_objects_exist(relay):
        if containers:
            report.fail(
                "licensed Sandbox has no relay topology",
                "run `booley session down`, then `booley session up --rebuild`",
            )
        return
    try:
        validate_relay(
            relay,
            containers[0] if len(containers) == 1 else None,
            RelayProfile(
                profile.server_ipv4,
                profile.server_hostid,
                profile.lmgrd_port,
                profile.vendor_port,
            ),
            issuance_labels=runtime_spec.labels(issuance),
            image=image_id,
        )
    except RelayDockerError as exc:
        report.fail(
            f"live FlexNet relay topology differs from host issuance: {exc}",
            "run `booley session down`, then `booley session up --rebuild`",
        )
        return
    report.pass_("live FlexNet relay bytes, hardening, endpoints, and aliases verified")


def _check_mounted_vivado_runtime(report: Findings) -> None:
    """Prove wrapper, release mount, architecture support, and runtime identity."""
    if not _mounted_vivado_files(report):
        return
    if not _mounted_vivado_topology(report):
        return
    _probe_mounted_vivado(report)


def _mounted_vivado_files(report: Findings) -> bool:
    wrapper = Path("/usr/local/bin/vivado")
    executable = Path(CONTAINER_TARGET) / "Vivado" / "bin" / "vivado"
    try:
        digest = hashlib.sha256(wrapper.read_bytes()).hexdigest()
    except OSError as exc:
        report.fail(
            f"mounted Vivado wrapper is unreadable: {exc}",
            session_runtime.SANDBOX_HOST_INSPECTION_REMEDY,
        )
        return False
    if digest != wrapper_sha256() or not os.access(executable, os.X_OK):
        report.fail(
            "mounted Vivado wrapper/release layout differs from built-in policy",
            session_runtime.SANDBOX_HOST_INSPECTION_REMEDY,
        )
        return False
    compatibility = (
        Path("/usr/lib/x86_64-linux-gnu/libudev.so.1"),
        Path("/usr/lib/x86_64-linux-gnu/libpixman-1.so.0"),
        Path("/usr/lib/locale/locale-archive"),
    )
    if any(not path.is_file() for path in compatibility):
        report.fail(
            "Sandbox Image lacks the fixed Vivado compatibility libraries or locale",
            session_runtime.SANDBOX_HOST_INSPECTION_REMEDY,
        )
        return False
    return True


def _mounted_vivado_topology(report: Findings) -> bool:
    try:
        mountinfo = Path("/proc/self/mountinfo").read_text(encoding="utf-8")
    except OSError as exc:
        report.fail(f"cannot inspect mounted Vivado release: {exc}", "recreate the Sandbox")
        return False
    fields = [line.split(" - ", 1)[0].split() for line in mountinfo.splitlines()]
    matches = [parts for parts in fields if len(parts) > 5 and parts[4] == CONTAINER_TARGET]
    if len(matches) != 1 or "ro" not in matches[0][5].split(","):
        report.fail(
            "Vivado release root is not one exact read-only runtime mount",
            session_runtime.SANDBOX_HOST_INSPECTION_REMEDY,
        )
        return False
    license_pointer = os.environ.get("XILINXD_LICENSE_FILE")
    if (
        license_pointer is not None
        and re.fullmatch(r"[1-9][0-9]{0,4}@booley-license-xilinx", license_pointer) is None
    ):
        report.fail(
            "XILINXD_LICENSE_FILE differs from the fixed private-relay contract",
            session_runtime.SANDBOX_HOST_INSPECTION_REMEDY,
        )
        return False
    return True


def _probe_mounted_vivado(report: Findings) -> None:
    wrapper = Path("/usr/local/bin/vivado")
    try:
        result = subprocess.run(
            [str(wrapper), "-version"],
            capture_output=True,
            text=True,
            timeout=90,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        report.fail(
            f"Vivado runtime identity probe failed: {exc}", "check the mounted installation"
        )
        return
    output = f"{result.stdout}\n{result.stderr}"
    if result.returncode != 0 or f"vivado v{SUPPORTED_VERSION}" not in output.lower():
        report.fail(
            f"mounted Vivado did not report exact version {SUPPORTED_VERSION}",
            "register and grant an exact supported Vivado installation",
        )
        return
    report.pass_(
        f"mounted Vivado {SUPPORTED_VERSION} wrapper, read-only release, and identity verified"
    )


def _check_interactive_state_volumes(
    project_root: Path, docker_exe: str | None, report: Findings
) -> None:
    """Surface persistent home-state volumes and flag orphans for pruning.

    The named volumes that keep an interactive session's plans/transcripts alive
    across rebuilds persist by design; the reaper never removes them. They
    accumulate one-per-project, so list any belonging to *other* projects as
    prunable (a removed project leaves its volume behind).
    """
    if not docker_exe:
        report.skip("interactive state-volume check skipped - runtime unavailable")
        return
    vols = idk.state_volumes()
    if not vols:
        report.pass_("no persistent interactive state volumes")
        return

    project_id = dc.canonical_project_id(project_root)
    mine = dc.state_volume_names(project_id)
    others = sorted(v for v in vols if v not in mine)

    present_mine = sorted(v for v in vols if v in mine)
    if present_mine:
        report.pass_(f"interactive state persists for this project ({', '.join(present_mine)})")

    if others:
        report.note(
            f"{len(others)} interactive state volume(s) from other projects persist; "
            "remove unused ones with: docker volume rm <name>",
        )
        for v in others:
            report.detail(f"    {v}")
    elif not present_mine:
        report.pass_(f"{len(vols)} interactive state volume(s) present")


def _check_issued_image_keepers(
    project_root: Path, docker_exe: str | None, report: Findings
) -> None:
    """Surface retained issuance images and possible keepers from old projects."""
    if not docker_exe:
        report.skip("issued image-keeper check skipped - runtime unavailable")
        return
    tags = idk.issued_image_tags()
    if not tags:
        report.pass_("no retained Sandbox Image keepers")
        return

    from booley.runtime import session_issuance as runtime_spec

    mine = runtime_spec.keeper_image(project_root)
    others = [tag for tag in tags if tag != mine]
    if mine in tags:
        report.pass_("issued Sandbox Image is retained for this Project")
    if others:
        report.note(
            f"{len(others)} issued image keeper(s) from other projects persist; "
            "preview with booley projects prune-keepers, then release confirmed "
            "orphans with booley projects prune-keepers --confirm <digest>"
        )
        for tag in others:
            report.detail(f"    {tag}")
