"""Complete Session Runtime observations, independent of Doctor presentation.

Inspection validates current issuance; it never issues or repairs a Runtime.
Host validation may create/lock the private authority store when EDA is active.
The version probe uses a temporary, network-disabled container (30s CLI timeout).
In-runtime validation probes mounted Vivado (90s), without host authority access.
A CLI timeout does not itself prove remote container cleanup. Live execution
must independently validate authority; a diagnostic report grants no authority.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

import booley
from booley.audit.diagnostic_results import DiagnosticReport, Findings
from booley.core.boundary import require_dict
from booley.eda.config import EdaConfig
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
from booley.runtime import auth_token, runtime_context, session_runtime
from booley.runtime import devcontainer as dc
from booley.runtime import interactive_docker as idk
from booley.runtime import session_issuance as runtime_spec
from booley.runtime.devcontainer import (
    devcontainer_path,
    spec_mounts_token_seed,
    spec_state_is_persisted,
)


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
    ``image`` must match it, else the Session Runtime runs a stale image.
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
            json.loads(path.read_text(encoding="utf-8")), field="devcontainer.json"
        )
    except (OSError, ValueError) as exc:
        report.fail(f"{path} does not parse: {exc}", "re-run booley init")
        return
    if not _provider_persistence_current(request, spec, report):
        return
    if not _image_current(request, spec, report):
        return
    if not _seed_mounts_current(request, spec, report):
        return
    if not _viewer_extensions_current(request, spec, report):
        return
    if not _preview_terminal_current(request, spec, report):
        return
    report.pass_("devcontainer.json present and structurally current")


def _provider_persistence_current(
    request: RuntimeInspectionRequest, spec: dict, report: Findings
) -> bool:
    spec_app = dc.spec_agent_app(spec)
    if (
        request.declared_provider is not None
        and spec_app is not None
        and spec_app != request.declared_provider
    ):
        report.fail(
            f"devcontainer.json BOOLEY_AGENT_APP '{spec_app}' != [agent] provider "
            f"'{request.declared_provider}': the Booley MCP entry is registered for "
            f"'{spec_app}', so a '{request.declared_provider}' session sees no Booley "
            f"MCP tools, and '{request.declared_provider}' home-state is not persisted",
            "re-run `booley init --seed`, then rebuild the container in VS Code",
        )
        return False
    if spec_state_is_persisted(spec) is False:
        _spec_warning(
            request,
            report,
            "devcontainer.json is stale: no persistent volume for the agent's "
            "home-state - in-container transcripts/plans are lost on every "
            "rebuild; re-run `booley init` to regenerate",
        )
        return False
    return True


def _image_current(request: RuntimeInspectionRequest, spec: dict, report: Findings) -> bool:
    spec_image = spec.get("image")
    resolved_image = idk.image_id(request.image)
    immutable_spec = isinstance(spec_image, str) and re.fullmatch(
        r"sha256:[0-9a-f]{64}", spec_image
    )
    if immutable_spec and resolved_image is None:
        report.note(
            "devcontainer.json has an immutable image pin, but [sandbox].image "
            f"'{request.image}' cannot be resolved here; image drift is unverified "
            "and host `booley doctor` is authoritative"
        )
    elif isinstance(spec_image, str) and spec_image != (resolved_image or request.image):
        _spec_warning(
            request,
            report,
            f"devcontainer.json image '{spec_image}' != immutable ID for [sandbox].image "
            f"'{request.image}': the Session Runtime runs a stale image "
            "(missing toolchains it should have) - re-run `booley init --seed`, "
            "then rebuild the container in VS Code",
        )
        return False
    return True


def _seed_mounts_current(request: RuntimeInspectionRequest, spec: dict, report: Findings) -> bool:
    if request.expected_cache_mount and not dc.spec_mounts_target(
        spec, request.expected_cache_mount
    ):
        _spec_warning(
            request,
            report,
            "devcontainer.json omits the verified Nangate45 cache mount at "
            f"{request.expected_cache_mount}: ASIC synthesis will fail before "
            "elaboration - re-run `booley init --seed`, then rebuild the container",
        )
        return False
    remote_env = spec.get("remoteEnv")
    app = remote_env.get("BOOLEY_AGENT_APP") if isinstance(remote_env, dict) else None
    if (
        app in auth_token.CREDENTIALS
        and auth_token.read_stored_token(app)
        and spec_mounts_token_seed(spec) is False
    ):
        _spec_warning(
            request,
            report,
            "devcontainer.json predates the stored `booley auth` credential: "
            "VS Code sessions fall back to the refreshing one - re-run "
            "`booley init --seed`, then rebuild the container in VS Code",
        )
        return False
    return True


def _viewer_extensions_current(
    request: RuntimeInspectionRequest, spec: dict, report: Findings
) -> bool:
    if not dc.spec_installs_vaporview(spec):
        _spec_warning(
            request,
            report,
            "devcontainer.json predates the Waveform Viewer (ADR 0035) or its "
            "WCP auto-start patch: VaporView/WCP settings or the postAttach "
            "manifest patch are missing, so in-container `bwave gui` finds no "
            "running viewer - re-run `booley init --seed`, then rebuild the "
            "container in VS Code",
        )
        return False
    if not dc.spec_installs_hdl_highlight(spec):
        report.note(
            "devcontainer.json predates the Verilog/SystemVerilog + Tcl "
            "highlighting extensions: RTL and SDC/XDC constraints render as "
            "plain text in attached VS Code windows - re-run "
            "`booley init --seed`, then reload the container window"
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
            "devcontainer.json lacks the collision-safe Live Preview setup: "
            "rendered review HTML may open as a blank preview - re-run init "
            "with `--seed`, then rebuild the container in VS Code",
        )
        return False
    if not dc.spec_disables_python_terminal_activation(spec):
        report.note(
            "devcontainer.json predates the Python terminal activation fix: a "
            "synced Python extension can inject a delayed `source .venv/bin/activate` "
            "into new terminals - re-run init with `--seed`, then rebuild the "
            "container in VS Code"
        )
        return False
    return True


def _spec_warning(request: RuntimeInspectionRequest, report: Findings, message: str) -> None:
    report.warn(
        message,
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
            json.loads(path.read_text(encoding="utf-8")), field="devcontainer.json"
        )
        issuance = runtime_spec.validate(request.project_root, spec, path)
    except (OSError, ValueError, runtime_spec.RuntimeSpecError) as exc:
        report.fail(
            f"Session Runtime host issuance is invalid: {exc}",
            "run `booley init --seed` on the host and recreate the Session Runtime",
        )
        return
    report.pass_(f"Session Runtime spec has valid host issuance ({issuance.spec_sha256[:12]})")

    if not request.docker_exe:
        report.skip("live issued Session Runtime labels/topology - container runtime unavailable")
        return
    _check_runtime_booley_version(
        request.docker_exe,
        issuance.image,
        report,
    )
    _inspect_live_runtime(request, spec, issuance, report)


def _inspect_current_runtime(request: RuntimeInspectionRequest, report: Findings) -> None:
    from booley.eda.config import PROVISIONING_HOST

    vivado = request.eda.get("vivado") if request.eda is not None else None
    if not _check_runtime_isolation(report):
        return
    mounted = Path("/opt/booley-eda/vivado").is_dir()
    if (
        vivado is not None
        and vivado.provisioning == PROVISIONING_HOST
        and request.fpga_enabled
        and not mounted
    ):
        report.fail(
            "host-provisioned Vivado is absent from the Session Runtime",
            "reissue the spec on the host and recreate the Session Runtime",
        )
        return
    if mounted:
        _check_mounted_vivado_runtime(report)
    else:
        report.pass_("Session Runtime has no active host-mounted commercial EDA request")
    return


def _inspect_live_runtime(
    request: RuntimeInspectionRequest,
    spec: dict,
    issuance: runtime_spec.Issuance,
    report: Findings,
) -> None:
    try:
        result = _list_issued_containers(request, issuance)
    except (OSError, subprocess.SubprocessError) as exc:
        report.fail(f"could not inspect issued Session Runtime resources: {exc}", "start Docker")
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
        report.fail("could not list issued Session Runtime resources", "start Docker")
    elif drifted:
        report.fail(
            "live Session Runtime state differs from current host issuance",
            session_runtime.issued_runtime_drift_fix(
                request.project_root,
                issuance,
                drifted,
            ),
        )
    elif containers:
        report.pass_("live Session Runtime state matches the current host issuance")
    else:
        report.pass_("no stale live Session Runtime resources for this Project")
    _check_issued_license_relay(
        request.project_root,
        containers,
        issuance,
        report,
    )


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


def _probe_runtime_booley_version(
    docker_exe: str,
    image: str,
) -> subprocess.CompletedProcess[str]:
    """Read Booley's version from the issued image without network access."""
    probe = "import booley; print(booley.__version__)"
    return subprocess.run(
        [
            docker_exe,
            "run",
            "--rm",
            "--pull=never",
            "--network",
            "none",
            "--entrypoint",
            "python3",
            image,
            "-c",
            probe,
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def _check_runtime_booley_version(docker_exe: str, image: str, report: Findings) -> None:
    """Require the host package and issued Runtime Image to agree."""
    try:
        result = _probe_runtime_booley_version(docker_exe, image)
    except (OSError, subprocess.SubprocessError) as exc:
        report.fail(
            f"could not read the issued Session Runtime Booley version: {exc}",
            "rebuild the sandbox image with `booley init --force`, then recreate the Session Runtime",
        )
        return

    runtime_version = result.stdout.strip()
    if result.returncode != 0 or not runtime_version:
        detail = (result.stderr or result.stdout).strip()
        report.fail(
            "issued Runtime Image cannot report its Booley version",
            f"rebuild it with `booley init --force` ({detail or f'exit {result.returncode}'})",
        )
        return

    host_version = booley.__version__
    if runtime_version != host_version:
        report.fail(
            f"host Booley {host_version} != Session Runtime Booley {runtime_version}",
            "run `booley init --force`, then `booley session down` and "
            "`booley session up` to recreate the Session Runtime",
        )
        return
    report.pass_(f"host and Session Runtime use Booley {host_version}")


def _check_runtime_isolation(report: Findings) -> bool:
    """Enforce authority absence and fixed Project-data identity in every runtime."""
    if os.environ.get("BOOLEY_PROJECT_DIR") != "/booley-project":
        report.fail(
            "Session Runtime Project-data identity differs from /booley-project",
            "reissue the spec on the host and recreate the Session Runtime",
        )
        return False
    required = Path("/booley-project")
    forbidden = (
        Path("/var/run/docker.sock"),
        Path("/run/docker.sock"),
        Path("/root/.ssh"),
        Path("/home/agent/.ssh"),
        Path("/root/.config/booley/eda"),
        Path("/home/agent/.config/booley/eda"),
    )
    if not required.is_dir() or any(_runtime_path_exposed(path) for path in forbidden):
        report.fail(
            "Session Runtime exposes a forbidden host-authority surface",
            "reissue the spec on the host and recreate the Session Runtime",
        )
        return False
    report.pass_("Session Runtime Project data and host-authority isolation verified")
    return True


def _runtime_path_exposed(path: Path) -> bool:
    """Treat an agent-inaccessible root path as isolated, not as a Doctor crash."""
    try:
        return path.exists()
    except PermissionError:
        return False


def _check_issued_license_relay(
    project_root: Path, containers: list[str], issuance: runtime_spec.Issuance, report: Findings
) -> None:
    """Validate exact live relay bytes, endpoints, aliases, and hardening."""
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
            "issued License Profile lacks an immutable relay image", "run `booley init --seed`"
        )
        return
    relay = resources_for_session(str(project_root.resolve()))
    if not session_runtime._relay_objects_exist(relay):
        if containers:
            report.fail(
                "licensed Session Runtime has no relay topology",
                "run `booley session up --rebuild`",
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
        report.fail(f"mounted Vivado wrapper is unreadable: {exc}", "rebuild the Runtime Image")
        return False
    if digest != wrapper_sha256() or not os.access(executable, os.X_OK):
        report.fail(
            "mounted Vivado wrapper/release layout differs from built-in policy",
            "rebuild the image and reissue the Session Runtime spec on the host",
        )
        return False
    compatibility = (
        Path("/usr/lib/x86_64-linux-gnu/libudev.so.1"),
        Path("/usr/lib/x86_64-linux-gnu/libpixman-1.so.0"),
        Path("/usr/lib/locale/locale-archive"),
    )
    if any(not path.is_file() for path in compatibility):
        report.fail(
            "Runtime Image lacks the fixed Vivado compatibility libraries or locale",
            "rebuild the Runtime Image and reissue the spec",
        )
        return False
    return True


def _mounted_vivado_topology(report: Findings) -> bool:
    try:
        mountinfo = Path("/proc/self/mountinfo").read_text(encoding="utf-8")
    except OSError as exc:
        report.fail(
            f"cannot inspect mounted Vivado release: {exc}", "recreate the Session Runtime"
        )
        return False
    fields = [line.split(" - ", 1)[0].split() for line in mountinfo.splitlines()]
    matches = [parts for parts in fields if len(parts) > 5 and parts[4] == CONTAINER_TARGET]
    if len(matches) != 1 or "ro" not in matches[0][5].split(","):
        report.fail(
            "Vivado release root is not one exact read-only runtime mount",
            "reissue the spec and recreate the Session Runtime",
        )
        return False
    license_pointer = os.environ.get("XILINXD_LICENSE_FILE")
    if (
        license_pointer is not None
        and re.fullmatch(r"[1-9][0-9]{0,4}@booley-license-xilinx", license_pointer) is None
    ):
        report.fail(
            "XILINXD_LICENSE_FILE differs from the fixed private-relay contract",
            "reissue the Session Runtime from the host License Profile",
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
    mine = {dc.state_volume_name(app, project_id) for app in (dc.APP_CLAUDE, dc.APP_CODEX)}
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
        report.pass_("no retained Runtime Image keepers")
        return

    from booley.runtime import session_issuance as runtime_spec

    mine = runtime_spec.keeper_image(project_root)
    others = [tag for tag in tags if tag != mine]
    if mine in tags:
        report.pass_("issued Runtime Image is retained for this Project")
    if others:
        report.note(
            f"{len(others)} issued image keeper(s) from other projects persist; "
            "after confirming those projects are gone, remove one with: "
            "docker image rm <keeper-tag>"
        )
        for tag in others:
            report.detail(f"    {tag}")
