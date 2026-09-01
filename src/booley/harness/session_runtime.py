"""Headless lifecycle for the Session Runtime container (``booley session``).

ADR 0018 enters Interactive Mode through VS Code's "Reopen in Container", which
reads the generated ``.devcontainer/devcontainer.json``. That is the only
supported door, and it needs a UI: there is no ``devcontainer`` CLI on a stock
Windows host, and Booley shipped no equivalent. An agent-driven or CI setup had
to hand-translate the spec into a ``docker run`` line — mounts, env, network,
user, and the ``postCreateCommand`` — and keep that translation in sync by eye.

This module is that translation, done once and tested: it reads the spec Booley
already generates and derives the ``docker run`` argv from it, so the two doors
open onto the same container. It is emphatically *not* the ``booley up`` daemon
the lineage rejected (see ``interactive_docker``): nothing is supervised here.
The container carries the same ``booley.role=interactive`` label as the VS Code
one, so the idle reaper owns its lifecycle either way.

The long-lived Docker objects (egress network, proxy, reaper) remain ``booley
init``'s to create; this module only refuses to start without them.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from booley.harness import devcontainer as dc
from booley.harness import interactive_docker as idk
from booley.runtime import auth_token
from booley.runtime.platform_paths import docker_mount_path, host_path_from_docker_mount

if TYPE_CHECKING:
    from booley.eda.authority import LicenseProfile
    from booley.eda.runtime_spec import Issuance

logger = logging.getLogger(__name__)

# The command that keeps the container alive. "Reopen in Container" leaves the
# VS Code server running as PID 1's child; headlessly there is no server, so the
# container needs a process that never exits or `docker run -d` would return an
# immediately-exited container.
KEEPALIVE_CMD = ("sleep", "infinity")

# Shell used to run the devcontainer lifecycle hooks. They are `;`-joined shell
# strings (see devcontainer.build_devcontainer_spec), not argv, so they need one.
_HOOK_SHELL = ("bash", "-lc")

_LOCAL_ENV_RE = re.compile(r"\$\{localEnv:([^}:]+)\}")


class SessionError(RuntimeError):
    """A precondition for running the Session Runtime is missing."""


def _requested_issued_license(workspace: Path, issuance: Issuance) -> LicenseProfile | None:
    """Resolve exactly the licence named by a validated runtime issuance."""
    from booley.eda import runtime_spec

    return runtime_spec.requested_license(
        workspace,
        expected_name=issuance.license_profile,
    )


def session_container_name(workspace: Path) -> str:
    """Container name for *workspace*'s session (one per canonical Project).

    Distinct from the VS Code container (Dev Containers derives its own name), so
    `booley session up` never adopts or clobbers a container VS Code is driving.
    """
    return f"booley-session-{dc.canonical_project_id(workspace)}"


# Label the Dev Containers CLI stamps on the container it creates, naming the
# host folder it was opened from. Our own container never carries it.
_DEVCONTAINER_FOLDER_LABEL = "devcontainer.local_folder"


def _docker_stdout(argv: list[str]) -> str | None:
    """Stripped stdout of *argv*, or None if docker is missing or the call failed.

    Every probe below is advisory — it decides whether to *warn*, never whether
    to proceed — so a host without Docker, or an object that vanished mid-probe,
    must read as "nothing to say" rather than blow up the caller.
    """
    try:
        result = _run(argv)
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def _running_interactive_containers() -> list[str]:
    """Names of every running container carrying the Interactive Mode role label.

    Covers both doors onto Interactive Mode: the container VS Code's Dev
    Containers CLI creates and the headless one `booley session up` runs.
    """
    names = _docker_stdout(
        [
            "docker",
            "ps",
            "--filter",
            f"label={dc.INTERACTIVE_ROLE_LABEL}",
            "--format",
            "{{.Names}}",
        ]
    )
    if names is None:
        return []
    return [name for line in names.splitlines() if (name := line.strip())]


def _vscode_local_folder(name: str) -> str | None:
    """Host folder the Dev Containers CLI opened *name* from, if it stamped one."""
    return _docker_stdout(
        [
            "docker",
            "inspect",
            name,
            "--format",
            f'{{{{index .Config.Labels "{_DEVCONTAINER_FOLDER_LABEL}"}}}}',
        ]
    )


def _serves_workspace(name: str, workspace: Path) -> bool:
    """True if container *name* is Interactive Mode for *workspace*.

    Two shapes to recognise: our own session container is named after the
    workspace, while VS Code's carries the host folder in a label (with whatever
    drive-letter case the host used, hence the casefold).
    """
    if name == session_container_name(workspace):
        return True
    folder = _vscode_local_folder(name)
    return bool(folder) and folder.lower() == str(workspace).lower()


def vscode_session_container(workspace: Path) -> str | None:
    """Name of the running VS Code-created devcontainer for *workspace*, if any.

    Recognised by the ``devcontainer.local_folder`` label the Dev Containers CLI
    stamps — our own headless `session up` container never carries it, so this
    distinguishes the two container origins. The label does not prove that a
    renderer or extension host is still attached; callers such as the VaporView
    doctor check must probe their live service after finding the container.
    """
    ours = session_container_name(workspace)
    for name in _running_interactive_containers():
        if name == ours:
            continue
        folder = _vscode_local_folder(name)
        # Windows hosts stamp the label with a drive letter of either case.
        if folder and folder.lower() == str(workspace).lower():
            return name
    return None


def conflicting_vscode_session(workspace: Path) -> str | None:
    """Name of a running VS Code devcontainer for *workspace*, if any.

    Both containers mount the same per-project home-state volume read-write at
    the agent's state dir, so running ours alongside VS Code's puts two agents on
    one set of credentials, transcripts, and todos. That was impossible before —
    "Reopen in Container" only ever made one — so warn rather than silently
    create the second. Not an error: a one-off `session enter -- booley doctor`
    beside an open editor is a reasonable thing to want.
    """
    return vscode_session_container(workspace)


def sessions_on_stale_image(workspace: Path, image: str) -> list[str]:
    """Running Interactive Mode containers for *workspace* not on *image*'s current ID.

    A rebuild moves the tag; it does not touch a container already created from
    the old image, which keeps serving the layers it was born with. The tag being
    unchanged is precisely what makes this invisible — so compare resolved image
    *IDs*, never names. Callers use this to tell the user their fresh build is not
    what the live session is running.

    Empty on any doubt (no Docker, no such image, inspect failed): this only ever
    gates a warning, and a false alarm is worse than a missed one.
    """
    image_id = _docker_stdout(["docker", "image", "inspect", "--format", "{{.Id}}", image])
    if not image_id:
        return []
    stale = []
    for name in _running_interactive_containers():
        if not _serves_workspace(name, workspace):
            continue
        container_image = _docker_stdout(["docker", "inspect", "--format", "{{.Image}}", name])
        if container_image and container_image != image_id:
            stale.append(name)
    return stale


# ---------------------------------------------------------------------------
# Spec -> docker run argv
# ---------------------------------------------------------------------------


def _local_env(name: str) -> str:
    """Resolve one ``${localEnv:NAME}``; a stored credential backs its own var.

    Works for either agent app: the var name identifies the app (Claude's
    CLAUDE_CODE_OAUTH_TOKEN, Codex's OPENAI_API_KEY).
    """
    value = os.environ.get(name, "")
    if not value:
        credential = auth_token.credential_for_env_var(name)
        if credential is not None:
            value = auth_token.resolve_token(credential.app) or ""
    return value


def substitute(value: str, workspace: Path) -> str:
    """Resolve the devcontainer variables Booley's own spec uses.

    The Dev Containers CLI resolves these at container-create time; headlessly we
    do it ourselves. Only the three forms :mod:`devcontainer` emits are handled —
    an unknown ``${...}`` is left alone rather than silently blanked, so a spec
    using a variable this function does not model fails loudly at ``docker run``
    instead of quietly mounting the wrong path.

    ``${localEnv:VAR}`` resolves to the empty string when unset, matching the Dev
    Containers CLI (that is how an absent ``CLAUDE_CODE_OAUTH_TOKEN`` is meant to
    fall through to the mounted subscription credentials). The one exception is
    that token itself: when it is not exported, a credential stored by
    ``booley auth`` resolves instead. Booley's own paths therefore work with no
    export at all — unlike VS Code, which only ever reads its own process env.
    """
    # Basename first: it is a prefix-extension of localWorkspaceFolder, so the
    # shorter key would otherwise consume the front of the longer one.
    value = value.replace("${localWorkspaceFolderBasename}", workspace.name)
    value = value.replace("${localWorkspaceFolder}", docker_mount_path(workspace))
    return _LOCAL_ENV_RE.sub(lambda m: _local_env(m.group(1)), value)


def docker_run_argv(
    spec: dict,
    workspace: Path,
    name: str,
) -> list[str]:
    """Derive the ``docker run`` argv for *spec*.

    Mirrors how the Dev Containers CLI consumes the spec:
    ``workspaceMount`` + ``mounts`` -> ``--mount`` (used over ``-v`` because a
    Windows source path contains a colon, which ``-v`` would mis-split),
    ``remoteEnv`` -> ``-e``, ``remoteUser`` -> ``--user``, ``workspaceFolder`` ->
    ``--workdir``, and ``runArgs`` verbatim (that is where the network, label,
    and hardening flags live). ``customizations``/``shutdownAction`` are VS Code
    concerns with no ``docker run`` analog and are ignored.
    """
    image = spec.get("image")
    if not image:
        raise SessionError("devcontainer.json has no 'image' key")

    argv = ["docker", "run", "-d", "--name", name]

    for mount in [spec.get("workspaceMount"), *(spec.get("mounts") or [])]:
        if mount:
            argv += ["--mount", substitute(str(mount), workspace)]

    for key, value in (spec.get("containerEnv") or {}).items():
        argv += ["-e", f"{key}={substitute(str(value), workspace)}"]
    for key, value in (spec.get("remoteEnv") or {}).items():
        argv += ["-e", f"{key}={substitute(str(value), workspace)}"]

    if spec.get("remoteUser"):
        argv += ["--user", str(spec["remoteUser"])]
    if spec.get("workspaceFolder"):
        argv += ["--workdir", str(spec["workspaceFolder"])]

    argv += [substitute(str(a), workspace) for a in (spec.get("runArgs") or [])]
    argv += [str(image), *KEEPALIVE_CMD]
    return argv


def hook_argv(name: str, hook: str) -> list[str]:
    """``docker exec`` argv running a devcontainer lifecycle *hook* in *name*.

    ``docker exec`` inherits the container's configured user and env, so the hook
    sees exactly what ``docker run --user/-e`` established — the same environment
    the Dev Containers CLI gives it.
    """
    return ["docker", "exec", name, *_HOOK_SHELL, hook]


def exec_argv(
    name: str,
    command: list[str],
    *,
    tty: bool = True,
    env: Mapping[str, str] | None = None,
) -> list[str]:
    """Build a ``docker exec`` argv for an already-issued Session Runtime."""
    argv = ["docker", "exec"]
    if tty:
        argv.append("-t")
    else:
        argv += ["-e", "TERM=dumb"]
    for key, value in (env or {}).items():
        argv += ["-e", f"{key}={value}"]
    return [*argv, "-i", name, *command]


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


def _preflight(spec: dict, *, license_required: bool = False) -> None:
    """Refuse to start when `booley init`'s Docker objects are absent."""
    if not idk.network_exists(dc.EGRESS_NETWORK):
        raise SessionError(
            f"the '{dc.EGRESS_NETWORK}' network does not exist — run `booley init` "
            "first (it creates the network, egress proxy, and idle reaper)"
        )
    image = str(spec.get("image", ""))
    if image and not idk.image_exists(image):
        raise SessionError(
            f"the sandbox image '{image}' is not built — run `booley init` "
            "(or `booley init --force` to rebuild it)"
        )
    if license_required:
        from booley.eda.flexnet_docker import RELAY_IMAGE

        if not idk.image_exists(RELAY_IMAGE):
            raise SessionError(
                f"the license relay image '{RELAY_IMAGE}' is not built — run `booley init` "
                "before starting this licensed Session Runtime"
            )


def _load_spec(workspace: Path) -> dict:
    import json

    path = dc.devcontainer_path(workspace)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise SessionError(
            f"no {path} — run `booley init` in this folder to generate it"
        ) from None
    except (OSError, json.JSONDecodeError) as exc:
        raise SessionError(f"could not read {path}: {exc}") from exc


def _warn_on_image_drift(spec: dict, workspace: Path) -> None:
    """Warn when the spec's image no longer matches ``[sandbox].image`` (F-6).

    The devcontainer spec is generated from ``[sandbox].image`` at `booley
    init` time and left untracked, so editing the toml afterwards (say,
    pointing it at a freshly baked project image) leaves the spec frozen on
    the old image — the session then silently starts a container missing the
    toolchain the toml promised, indistinguishable from a real failure.
    `doctor` already WARNs on this drift; surface it here too, right where the
    stale image is about to run. Advisory only: the spec is the user's to
    regenerate (`booley init --seed`), never rewritten behind their back.
    """
    # Deferred import: init_cmd pulls in the whole host-side wizard stack,
    # which this thin lifecycle module otherwise never needs.
    from booley.harness.init_cmd import project_sandbox_image

    expected = project_sandbox_image(workspace)
    spec_image = spec.get("image")
    if not isinstance(spec_image, str) or not spec_image:
        return
    # Issuance pins devcontainer.json to an immutable ID while booley.toml
    # normally retains the human-facing tag. Compare what both names resolve
    # to; string inequality alone turns every freshly issued spec into a false
    # stale warning.
    spec_id = idk.image_id(spec_image)
    expected_id = idk.image_id(expected)
    images_match = bool(spec_id and expected_id and spec_id == expected_id)
    if spec_image != expected and not images_match:
        logger.warning(
            "devcontainer.json image '%s' != [sandbox].image '%s' — this session "
            "runs the stale spec image. Re-run `booley init --seed`, then "
            "`booley session down` and `booley session up` to pick up the new one.",
            spec_image,
            expected,
        )


def _warn_on_stale_booley_bake(workspace: Path) -> None:
    """Warn when the managed Session Image is stale by authoritative provenance."""
    from booley.harness.image_lifecycle import Intent, ProjectImageScope, Status, reconcile

    result = reconcile(ProjectImageScope(workspace), Intent.CHECK)
    if result.status is Status.STALE:
        logger.warning(
            "sandbox image '%s' was built from Booley sources that no longer "
            "match this checkout — the session runs stale Booley code. Rebuild "
            "with `booley session refresh`.",
            result.selected_reference,
        )


def _warn_on_stale_session_containers(spec: dict, workspace: Path) -> None:
    """Warn when a live session container was created from a superseded image.

    A rebuild moves the tag but never touches running containers, so a fresh
    build is silently not what the open session executes. Uses
    :func:`sessions_on_stale_image`, which compares resolved image IDs.
    """
    image = spec.get("image")
    if not isinstance(image, str) or not image:
        return
    for name in sessions_on_stale_image(workspace, image):
        logger.warning(
            "container '%s' is running an image superseded by a rebuild of "
            "'%s' — `booley session down` then `booley session up` (or rebuild "
            "the VS Code window) to pick up the new image.",
            name,
            image,
        )


def _run(argv: list[str], *, capture: bool = True) -> subprocess.CompletedProcess:
    logger.debug("running: %s", " ".join(argv))
    return subprocess.run(
        argv,
        capture_output=capture,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


@dataclass(frozen=True)
class _ParkedSession:
    name: str
    backup: str
    was_running: bool


def _park_session_for_rebuild(name: str) -> _ParkedSession:
    """Stop and rename an unlicensed Session so refresh can roll it back."""
    backup = f"{name}-pre-refresh"
    if idk.container_exists(backup):
        raise SessionError(
            f"cannot refresh while recovery container {backup!r} exists; inspect it first"
        )
    was_running = idk.container_running(name)
    if was_running:
        stopped = _run(["docker", "stop", name])
        if stopped.returncode != 0:
            raise SessionError(
                f"could not stop existing Session Runtime: {stopped.stderr.strip()}"
            )
    renamed = _run(["docker", "rename", name, backup])
    if renamed.returncode != 0:
        if was_running:
            _run(["docker", "start", name])
        raise SessionError(f"could not park existing Session Runtime: {renamed.stderr.strip()}")
    return _ParkedSession(name, backup, was_running)


def _restore_parked_session(parked: _ParkedSession) -> None:
    """Restore the pre-refresh Session after replacement failed."""
    if idk.container_exists(parked.name):
        _run(["docker", "rm", "-f", parked.name])
    renamed = _run(["docker", "rename", parked.backup, parked.name])
    if renamed.returncode != 0:
        raise SessionError(
            f"refresh failed and recovery container {parked.backup!r} could not be restored"
        )
    if parked.was_running:
        _start_session_container(parked.name)


def _discard_parked_session(parked: _ParkedSession) -> None:
    result = _run(["docker", "rm", "-f", parked.backup])
    if result.returncode != 0:
        logger.warning(
            "replacement succeeded but old recovery Session %r could not be removed: %s",
            parked.backup,
            result.stderr.strip() or "docker rm failed",
        )


def _remove_failed_candidate(request: _UpRequest, *, remove_relay: bool) -> None:
    """Remove an unverified candidate and any topology created only for it."""
    if idk.container_exists(request.name):
        removed = _run(["docker", "rm", "-f", request.name])
        if removed.returncode != 0:
            raise SessionError(
                f"could not remove failed Session candidate {request.name!r}: "
                f"{removed.stderr.strip()}"
            )
    if remove_relay:
        _remove_license_relay(request.relay)


@dataclass(frozen=True)
class _UpRequest:
    spec: dict
    issuance: Any
    profile: Any
    name: str
    labels: tuple[str, ...]
    relay: Any


def _validate_up_request(workspace: Path, image_override: str | None) -> _UpRequest:
    from booley.eda import runtime_spec

    spec = _load_spec(workspace)
    try:
        issuance = runtime_spec.validate(workspace, spec, dc.devcontainer_path(workspace))
    except runtime_spec.RuntimeSpecError as exc:
        raise SessionError(
            f"refusing Session Runtime startup: {exc}; run `booley init --seed` on the host"
        ) from exc
    _warn_on_image_drift(spec, workspace)
    if image_override is not None and image_override != spec.get("image"):
        raise SessionError(
            "session refresh cannot bypass a host-issued spec; re-run `booley init --seed`"
        )
    profile = _requested_issued_license(workspace, issuance)
    _preflight(spec, license_required=profile is not None)
    _warn_on_stale_booley_bake(workspace)
    return _UpRequest(
        spec,
        issuance,
        profile,
        session_container_name(workspace),
        runtime_spec.labels(issuance),
        _relay_resources(workspace),
    )


def _create_or_resume_session(
    workspace: Path,
    request: _UpRequest,
    *,
    exists: bool,
) -> bool:
    relay, relay_created = _prepare_license_relay(
        workspace,
        request.relay,
        request.name,
        request.profile,
        request.labels,
        exists,
        request.issuance.relay_image_id,
    )
    created = not exists
    if created:
        _create_session_container(
            request.spec,
            workspace,
            request.name,
            request.labels,
            request.profile,
            relay,
            relay_created,
            request.issuance.relay_image_id,
        )
    else:
        _start_session_container(request.name)
    if created and request.spec.get("postCreateCommand"):
        _run_hook(request.name, str(request.spec["postCreateCommand"]), "postCreateCommand")
    if request.spec.get("postStartCommand"):
        _run_hook(request.name, str(request.spec["postStartCommand"]), "postStartCommand")
    return relay_created


def _run_up_transaction(
    workspace: Path,
    request: _UpRequest,
    *,
    rebuild: bool,
    expected_image_id: str | None,
    expected_payload_fingerprint: str | None,
) -> None:
    replacing = rebuild and idk.container_exists(request.name)
    if replacing and request.profile is not None:
        raise SessionError(
            "transactional refresh cannot yet preserve a licensed relay topology; "
            "run `booley session down` before `booley session refresh`"
        )
    parked = _park_session_for_rebuild(request.name) if replacing else None
    exists = idk.container_exists(request.name)
    if exists and not _container_matches_issuance(
        request.name, request.issuance, spec=request.spec, workspace=workspace
    ):
        raise SessionError(
            f"existing Session Runtime {request.name!r} does not match the current host "
            "issuance; run `booley session up --rebuild`"
        )
    candidate_ready = False
    relay_created = False
    try:
        relay_created = _create_or_resume_session(
            workspace,
            request,
            exists=exists,
        )
        candidate_ready = True
        if expected_image_id is not None:
            verify_refreshed_session(
                workspace,
                expected_image_id,
                expected_payload_fingerprint,
            )
    except BaseException:
        if parked is not None:
            _restore_parked_session(parked)
        elif not exists and candidate_ready:
            _remove_failed_candidate(request, remove_relay=relay_created)
        raise
    if parked is not None:
        _discard_parked_session(parked)


def up(
    workspace: Path,
    *,
    rebuild: bool = False,
    image_override: str | None = None,
    expected_image_id: str | None = None,
    expected_payload_fingerprint: str | None = None,
) -> str:
    """Create-or-start the Session Runtime for *workspace*; return its name.

    Idempotent, and split along the same seam the Dev Containers CLI uses:
    ``postCreateCommand`` runs only when the container is created (it seeds the
    agent's credentials and config), while ``postStartCommand`` runs on every
    start (it revives the in-container MCP endpoint after a stop->start, which a
    resumed container needs and a fresh one gets from postCreate anyway).
    """
    request = _validate_up_request(workspace, image_override)
    _run_up_transaction(
        workspace,
        request,
        rebuild=rebuild,
        expected_image_id=expected_image_id,
        expected_payload_fingerprint=expected_payload_fingerprint,
    )
    # Last, so it judges the container that actually ended up running (a
    # just-created one trivially matches its image and stays silent).
    _warn_on_stale_session_containers(request.spec, workspace)
    return request.name


def validate(workspace: Path) -> str:
    """Validate the host-issued spec used by VS Code and the headless CLI."""
    from booley.eda import runtime_spec

    spec = _load_spec(workspace)
    issuance = runtime_spec.validate(workspace, spec, dc.devcontainer_path(workspace))
    return issuance.spec_sha256


def prepare(workspace: Path) -> str:
    """Validate the issued spec and prepare licensed topology for VS Code.

    Dev Containers runs this fixed host command before container creation.  The
    private network named in the sealed spec must therefore already exist, and
    the relay must be healthy before Docker consumes the spec.
    """
    from booley.eda import runtime_spec
    from booley.eda.flexnet_docker import RelayDockerError, validate_relay

    spec = _load_spec(workspace)
    try:
        pending_project_data = runtime_spec.authorized_project_data_source(workspace)
        _reject_legacy_project_data_visibility(workspace, pending_project_data)
        issuance = runtime_spec.validate(workspace, spec, dc.devcontainer_path(workspace))
    except runtime_spec.RuntimeSpecError as exc:
        raise SessionError(
            f"refusing Session Runtime preparation: {exc}; run `booley init --seed` on the host"
        ) from exc
    _reconcile_stopped_vscode_containers(workspace, issuance)
    profile = _requested_issued_license(workspace, issuance)
    _preflight(spec, license_required=profile is not None)
    if profile is None:
        return issuance.spec_sha256
    relay = _relay_resources(workspace)
    issuance_labels = runtime_spec.labels(issuance)
    if _relay_objects_exist(relay):
        try:
            validate_relay(
                relay,
                None,
                _relay_profile(profile),
                issuance_labels=issuance_labels,
                image=issuance.relay_image_id or "",
            )
        except RelayDockerError as exc:
            raise SessionError(f"licensed Session Runtime topology is invalid: {exc}") from exc
    else:
        _provision_license_relay(
            workspace,
            profile,
            issuance_labels,
            issuance.relay_image_id,
        )
    return issuance.spec_sha256


def _reject_legacy_project_data_visibility(workspace: Path, pending_project_data: Path) -> None:
    """Block creation while an old runtime can rename the next bind source."""
    for name, raw in _strict_running_interactive_states():
        if not _inspected_container_serves_workspace(name, raw, workspace):
            continue
        if _project_data_mount_root_is_pinned(raw, workspace, pending_project_data):
            continue
        raise SessionError(
            f"running Session Runtime {name!r} predates the protected Project-data "
            "mount contract; stop and remove it before preparing the replacement"
        )


def _strict_running_interactive_states() -> list[tuple[str, str]]:
    return _strict_interactive_states(
        [
            "docker",
            "ps",
            "--filter",
            f"label={dc.INTERACTIVE_ROLE_LABEL}",
            "--format",
            "{{.Names}}",
        ],
        inventory_error="cannot inventory running Session Runtime containers",
    )


def _strict_all_interactive_states(project_id: str) -> list[tuple[str, str]]:
    """Inspect this Project's running or stopped Interactive Mode containers."""
    return _strict_interactive_states(
        [
            "docker",
            "ps",
            "-a",
            "--filter",
            f"label={dc.INTERACTIVE_ROLE_LABEL}",
            "--filter",
            f"label=booley.project-id={project_id}",
            "--format",
            "{{.Names}}",
        ],
        inventory_error="cannot inventory Session Runtime containers",
    )


def _strict_interactive_states(
    inventory_argv: list[str], *, inventory_error: str
) -> list[tuple[str, str]]:
    """Inspect every container returned by a strict Interactive Mode inventory."""
    names = _docker_stdout(inventory_argv)
    if names is None:
        raise SessionError(inventory_error)
    states = []
    for name in (line.strip() for line in names.splitlines()):
        if not name:
            continue
        raw = _docker_stdout(["docker", "inspect", name])
        if raw is None or _decode_container_inspect(raw) is None:
            raise SessionError(f"cannot inspect Session Runtime {name!r}")
        states.append((name, raw))
    return states


def _reconcile_stopped_vscode_containers(workspace: Path, issuance: object) -> None:
    """Discard stopped VS Code containers that predate the current issuance."""
    from booley.eda import runtime_spec

    expected = dict(label.split("=", 1) for label in runtime_spec.labels(issuance))
    expected_config = str(dc.devcontainer_path(workspace))
    project_id = expected["booley.project-id"]
    for name, raw in _strict_all_interactive_states(project_id):
        state = _decode_container_inspect(raw)
        assert state is not None
        config = state.get("Config")
        labels = config.get("Labels") if isinstance(config, dict) else None
        if not isinstance(labels, dict):
            continue
        # The headless `booley session` container deliberately shares Booley's
        # role and issuance labels.  Only Dev Containers stamps local_folder,
        # so require that positive origin marker before removing anything.
        local_folder = labels.get(_DEVCONTAINER_FOLDER_LABEL)
        if (
            not isinstance(local_folder, str)
            or local_folder.casefold() != str(workspace).casefold()
        ):
            continue
        if labels.get("booley.project-id") != expected.get("booley.project-id"):
            continue
        actual_config = labels.get("devcontainer.config_file")
        config_matches = (
            isinstance(actual_config, str)
            and actual_config.casefold() == expected_config.casefold()
        )
        issuance_matches = config_matches and all(
            labels.get(key) == value for key, value in expected.items()
        )
        running = state.get("State", {}).get("Running") is True
        if running:
            if not issuance_matches:
                raise SessionError(
                    f"running Session Runtime {name!r} uses an older host issuance; "
                    "stop it before recreating the VS Code container"
                )
            continue
        if issuance_matches and not _container_has_unavailable_bind(name, state):
            continue
        _remove_stopped_vscode_container(name)


def _remove_stopped_vscode_container(name: str) -> None:
    """Remove one inspected-stopped container without crossing a start race."""
    result = _run(["docker", "rm", name])
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "docker rm failed"
        raise SessionError(
            f"cannot remove stale Session Runtime {name!r}: {detail}. Booley will "
            "not force-remove it because it may have become active; stop the "
            "container and retry"
        )
    logger.info("removed stopped stale Session Runtime %r", name)


def _container_has_unavailable_bind(name: str, state: dict) -> bool:
    """Whether Docker would find an inspected container's bind source unavailable."""
    mounts = state.get("Mounts")
    if not isinstance(mounts, list):
        raise SessionError(f"cannot inspect bind mounts for Session Runtime {name!r}")
    for mount in mounts:
        if not isinstance(mount, dict):
            raise SessionError(f"cannot inspect bind mounts for Session Runtime {name!r}")
        if mount.get("Type") != "bind":
            continue
        source = mount.get("Source")
        if not isinstance(source, str) or not source:
            raise SessionError(f"cannot inspect bind mounts for Session Runtime {name!r}")
        host_path = host_path_from_docker_mount(source)
        if host_path is None:
            continue
        try:
            host_path.stat()
        except OSError:
            return True
    return False


def _inspected_container_serves_workspace(name: str, raw: str, workspace: Path) -> bool:
    if name == session_container_name(workspace):
        return True
    state = _decode_container_inspect(raw)
    assert state is not None
    config = state.get("Config")
    labels = config.get("Labels") if isinstance(config, dict) else None
    folder = labels.get(_DEVCONTAINER_FOLDER_LABEL) if isinstance(labels, dict) else None
    return isinstance(folder, str) and folder.casefold() == str(workspace).casefold()


def _project_data_mount_root_is_pinned(
    raw: str, workspace: Path, pending_project_data: Path
) -> bool:
    local_source = workspace.resolve() / ".booley_project"
    if pending_project_data != local_source:
        return True
    state = _decode_container_inspect(raw)
    if state is None:
        return False
    mounts = state.get("Mounts")
    if not isinstance(mounts, list) or any(not isinstance(item, dict) for item in mounts):
        return False
    by_target = {item.get("Destination"): item for item in mounts}
    project_data = by_target.get("/booley-project")
    if not _writable_bind(project_data):
        return False
    source = Path(str(project_data.get("Source", "")))
    if source != local_source:
        return False
    workspace_view = by_target.get("/work/.booley_project")
    return _writable_bind(workspace_view) and workspace_view.get("Source") == str(source)


def _decode_container_inspect(raw: str) -> dict | None:
    import json

    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(decoded, list) or len(decoded) != 1 or not isinstance(decoded[0], dict):
        return None
    return decoded[0]


def _writable_bind(raw: object) -> bool:
    return (
        isinstance(raw, dict)
        and raw.get("Type") == "bind"
        and raw.get("RW") is True
        and isinstance(raw.get("Source"), str)
    )


def _container_matches_issuance(  # noqa: PLR0911, PLR0912, PLR0915 - fail-closed inspect ladder
    name: str,
    issuance: object,
    *,
    spec: dict | None = None,
    workspace: Path | None = None,
) -> bool:
    """True when an existing container exactly matches its trusted issuance.

    Label-only inspection is retained for Doctor's cross-origin inventory. The
    resume path supplies the spec and workspace and additionally compares the
    immutable image, user/workdir, mounts, environment, hardening, and networks.
    """
    import json

    from booley.eda import runtime_spec

    expected = set(runtime_spec.labels(issuance))
    output = _docker_stdout(["docker", "inspect", name, "--format", "{{json .Config.Labels}}"])
    if output is None:
        return False
    try:
        labels = json.loads(output)
    except json.JSONDecodeError:
        return False
    if not isinstance(labels, dict):
        return False
    if not expected.issubset({f"{key}={value}" for key, value in labels.items()}):
        return False
    if spec is None or workspace is None:
        return True
    raw = _docker_stdout(["docker", "inspect", name])
    if raw is None:
        return False
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError:
        return False
    if not isinstance(decoded, list) or len(decoded) != 1 or not isinstance(decoded[0], dict):
        return False
    state = decoded[0]
    config = state.get("Config", {})
    host = state.get("HostConfig", {})
    networks = state.get("NetworkSettings", {}).get("Networks", {})
    if (
        not isinstance(config, dict)
        or not isinstance(host, dict)
        or not isinstance(networks, dict)
    ):
        return False
    image_id = getattr(issuance, "image_id", None)
    if (
        state.get("Image") != image_id
        or config.get("Image") != spec.get("image")
        or config.get("User") != spec.get("remoteUser")
    ):
        return False
    if config.get("WorkingDir") != spec.get("workspaceFolder"):
        return False
    if not _session_hardening_matches(config, host):
        return False
    memory = _flag_values(spec.get("runArgs", []), "--memory")
    expected_memory = 0 if not memory else _memory_bytes(memory[0])
    if expected_memory is None or host.get("Memory") != expected_memory:
        return False
    expected_networks = _flag_values(spec.get("runArgs", []), "--network")
    if set(networks) != set(expected_networks):
        return False
    devcontainer_workspace = labels.get("devcontainer.local_folder")
    try:
        is_devcontainer = (
            isinstance(devcontainer_workspace, str)
            and Path(devcontainer_workspace).resolve() == workspace.resolve()
        )
    except OSError:
        is_devcontainer = False
    env_sections = (spec.get("containerEnv") or {},)
    if not is_devcontainer:
        env_sections += (spec.get("remoteEnv") or {},)
    expected_env = {
        f"{key}={substitute(str(value), workspace)}"
        for section in env_sections
        for key, value in section.items()
    }
    actual_env = config.get("Env")
    if not isinstance(actual_env, list) or not expected_env.issubset(set(actual_env)):
        return False
    return _mounts_match_spec(
        state.get("Mounts"),
        spec,
        workspace,
        allow_vscode_mounts=is_devcontainer,
    )


def _session_hardening_matches(config: dict, host: dict) -> bool:
    security = host.get("SecurityOpt")
    return (
        host.get("CapAdd") in (None, [])
        and host.get("CapDrop") == ["ALL"]
        and host.get("Privileged") is False
        and host.get("PidMode") in (None, "")
        and host.get("IpcMode") in (None, "", "private")
        and host.get("UsernsMode") in (None, "", "private")
        and host.get("Devices") in (None, [])
        and host.get("DeviceRequests") in (None, [])
        and host.get("PortBindings") in (None, {})
        and host.get("PublishAllPorts") is False
        and config.get("ExposedPorts") in (None, {})
        and isinstance(security, list)
        and len(security) == 1
        and security[0] in {"no-new-privileges", "no-new-privileges:true"}
        and host.get("PidsLimit") == 4096
    )


def _flag_values(raw: object, flag: str) -> list[str]:
    if not isinstance(raw, list):
        return []
    return [raw[index + 1] for index, value in enumerate(raw[:-1]) if value == flag]


def _memory_bytes(value: str) -> int | None:
    match = re.fullmatch(r"([1-9][0-9]*)([kKmMgG]?)", value)
    if match is None:
        return None
    multipliers = {"": 1, "k": 1024, "m": 1024**2, "g": 1024**3}
    return int(match.group(1)) * multipliers[match.group(2).lower()]


def _mounts_match_spec(
    raw: object,
    spec: dict,
    workspace: Path,
    *,
    allow_vscode_mounts: bool = False,
) -> bool:
    """Compare Docker's resolved mount state with every issued spec mount."""
    if not isinstance(raw, list) or any(not isinstance(item, dict) for item in raw):
        return False
    expected_raw = [spec.get("workspaceMount"), *(spec.get("mounts") or [])]
    expected: dict[str, tuple[str, str, bool]] = {}
    for value in expected_raw:
        if not isinstance(value, str):
            return False
        fields = dict(
            field.split("=", 1)
            for field in substitute(value, workspace).split(",")
            if "=" in field
        )
        target = fields.get("target")
        source = fields.get("source")
        kind = fields.get("type")
        if not target or not source or not kind:
            return False
        expected[target] = (source, kind, "readonly" not in value)
    observed = {str(item.get("Destination", "")): item for item in raw}
    extras = [item for target, item in observed.items() if target not in expected]
    missing = set(expected) - set(observed)
    unsafe_extras = extras and (
        not allow_vscode_mounts or not all(_is_vscode_managed_mount(item) for item in extras)
    )
    if missing or unsafe_extras:
        return False
    for target, item in observed.items():
        if target not in expected:
            continue
        source, kind, writable = expected[target]
        observed_source = item.get("Name") if kind == "volume" else item.get("Source")
        source_matches = observed_source == source
        if kind == "bind" and isinstance(observed_source, str) and not source_matches:
            source_matches = docker_mount_path(Path(observed_source)) == source
        if not source_matches or item.get("Type") != kind or item.get("RW") is not writable:
            return False
    return True


def _is_vscode_managed_mount(item: dict) -> bool:
    """Recognize only the mounts Dev Containers injects outside the sealed spec."""
    target = item.get("Destination")
    if target == "/vscode":
        return (
            item.get("Type") == "volume"
            and item.get("Name") == "vscode"
            and item.get("RW") is True
        )
    if (
        not isinstance(target, str)
        or re.fullmatch(r"/tmp/vscode-wayland-[\w-]+\.sock", target) is None
    ):
        return False
    source = item.get("Source")
    return (
        item.get("Type") == "bind"
        and item.get("RW") is True
        and isinstance(source, str)
        and re.fullmatch(r"/run/user/[0-9]+/wayland-[0-9]+", source) is not None
    )


def _relay_resources(workspace: Path):
    """Return deterministic relay resources without reading current authority."""
    from booley.eda.flexnet_docker import resources_for_session

    return resources_for_session(str(workspace.resolve()))


def _relay_objects_exist(relay) -> bool:
    """True when any exact deterministic relay object survived."""
    return (
        idk.container_exists(relay.relay_container)
        or idk.network_exists(relay.private_network)
        or idk.network_exists(relay.outbound_network)
    )


def _prepare_license_relay(
    workspace: Path,
    relay,
    name: str,
    profile: Any,
    labels: tuple[str, ...],
    session_exists: bool,
    relay_image_id: str | None,
) -> tuple[Any, bool]:
    """Provision before create or validate before resume; return whether created."""
    if profile is None:
        return relay, False
    if relay_image_id is None:
        raise SessionError("licensed Session Runtime issuance lacks an immutable relay image")
    if session_exists:
        _validate_license_relay(relay, name, profile, labels, relay_image_id)
        return relay, False
    return _provision_license_relay(workspace, profile, labels, relay_image_id), True


def _create_session_container(
    spec: dict,
    workspace: Path,
    name: str,
    labels: tuple[str, ...],
    profile: Any,
    relay,
    relay_created: bool,
    relay_image_id: str | None,
) -> None:
    """Create the issued Session and roll back every owned relay on failure."""
    result = _run(docker_run_argv(spec, workspace, name))
    if result.returncode != 0:
        if relay_created:
            _remove_license_relay(relay)
        raise SessionError(f"docker run failed: {result.stderr.strip()}")
    if profile is None:
        return
    if relay_image_id is None:
        raise SessionError("licensed Session Runtime issuance lacks an immutable relay image")
    try:
        _connect_and_validate_license_relay(
            relay,
            name,
            profile,
            labels,
            relay_image_id,
        )
    except SessionError:
        _run(["docker", "rm", "-f", name])
        _remove_license_relay(relay)
        raise


def _start_session_container(name: str) -> None:
    """Start a stopped issued container after its relay validation."""
    if idk.container_running(name):
        return
    result = _run(["docker", "start", name])
    if result.returncode != 0:
        raise SessionError(f"docker start failed: {result.stderr.strip()}")


def _relay_profile(profile: Any):
    """Translate the authority record into the relay module's validated type."""
    from booley.eda.flexnet_docker import RelayProfile

    return RelayProfile(
        profile.server_ipv4,
        profile.server_hostid,
        profile.lmgrd_port,
        profile.vendor_port,
    )


def _provision_license_relay(
    workspace: Path,
    profile: Any,
    labels: tuple[str, ...],
    relay_image_id: str | None,
):
    """Create and health-gate the relay before creating a licensed Session."""
    from booley.eda.flexnet_docker import (
        RelayDockerError,
        recreate_relay,
    )

    try:
        if relay_image_id is None:
            raise SessionError("licensed Session Runtime issuance lacks an immutable relay image")
        return recreate_relay(
            _relay_profile(profile),
            str(workspace.resolve()),
            image=relay_image_id,
            issuance_labels=labels,
        )
    except RelayDockerError as exc:
        raise SessionError(f"could not start licensed Session Runtime: {exc}") from exc


def _connect_and_validate_license_relay(
    relay,
    name: str,
    profile: Any,
    labels: tuple[str, ...],
    relay_image_id: str,
) -> None:
    """Attach only the private network and verify the resulting exact topology."""
    from booley.eda.flexnet_docker import RelayDockerError, validate_relay

    try:
        validate_relay(
            relay,
            name,
            _relay_profile(profile),
            issuance_labels=labels,
            image=relay_image_id,
        )
    except RelayDockerError as exc:
        raise SessionError(f"licensed Session Runtime topology failed: {exc}") from exc


def _validate_license_relay(
    relay,
    name: str,
    profile: Any,
    labels: tuple[str, ...],
    relay_image_id: str,
) -> None:
    """Fail closed when a resumed licensed Session's relay has drifted."""
    from booley.eda.flexnet_docker import RelayDockerError, validate_relay

    try:
        validate_relay(
            relay,
            name,
            _relay_profile(profile),
            issuance_labels=labels,
            image=relay_image_id,
        )
    except RelayDockerError as exc:
        raise SessionError(f"licensed Session Runtime topology is invalid: {exc}") from exc


def _remove_license_relay(relay) -> None:
    """Remove exact relay objects, preserving an actionable residual error."""
    from booley.eda.flexnet_docker import RelayDockerError, remove_relay

    try:
        remove_relay(relay)
    except RelayDockerError as exc:
        raise SessionError(f"could not clean up license relay: {exc}") from exc


def _run_hook(name: str, hook: str, label: str) -> None:
    """Run a lifecycle hook, warning (not failing) when it errors.

    The hooks are best-effort by construction — the seed steps already end in
    ``|| true`` because a missing host credential is a legitimate state — so a
    non-zero exit here means the *registrar* failed. That leaves a usable
    container with no MCP endpoint, which is worth a loud warning but not a
    teardown: the user can re-run `booley session up` once the cause is fixed.
    """
    result = _run(hook_argv(name, hook))
    if result.returncode != 0:
        logger.warning(
            "%s failed (exit %d): %s",
            label,
            result.returncode,
            (result.stderr or result.stdout).strip(),
        )


# An argv element carrying a Windows drive path (`C:\...` or `C:/...`), anywhere
# in the string so `--report-dir=C:/tmp/x` is caught alongside a bare value.
_HOST_PATH_IN_ARG_RE = re.compile(r"(?:^|=)[A-Za-z]:[\\/]")


def _warn_on_mangled_args(command: list[str]) -> None:
    """Warn when a forwarded argument holds a Windows path (MSYS argv rewriting).

    ``booley`` is a native Windows exe, so Git Bash/MSYS translates POSIX-looking
    argv on the way in: ``-- ... --report-dir /tmp/rep`` arrives here already
    rewritten to ``C:/Users/<you>/AppData/Local/Temp/rep``. We hand the command
    to ``docker exec`` verbatim (correctly — we cannot know which arguments are
    paths), but inside the Linux container that string denotes nothing, and a
    program that treats it as a path will silently create a junk ``C:`` directory
    in the bind-mounted workspace rather than fail. The EDA tools now reject such a
    ``--report-dir`` outright; this catches the same mangling for every other
    argument, where we can only advise.
    """
    suspects = [a for a in command if _HOST_PATH_IN_ARG_RE.search(a)]
    if not suspects:
        return
    logger.warning(
        "argument(s) %s look like Windows host paths, which do not exist inside "
        "the session container — Git Bash/MSYS rewrites '/tmp/...' style "
        "arguments when it launches booley. If you meant a container path, "
        "re-run with MSYS_NO_PATHCONV=1 (or MSYS2_ARG_CONV_EXCL='*'), or double "
        "the leading slash ('//tmp/rep').",
        ", ".join(repr(s) for s in suspects),
    )


def enter(workspace: Path, command: list[str] | None = None, *, tty: bool = True) -> int:
    """``docker exec`` into the running session; return the command's exit code.

    With no *command*, opens an interactive login shell. Starts the container
    first if it exists but is stopped, so this is the one entry point a script
    needs.
    """
    name = up(workspace)
    if command:
        _warn_on_mangled_args(command)
        from booley.harness.runtime_attachment import run_command

        return run_command(workspace, name, list(command), tty=tty).exit_code
    argv = exec_argv(name, ["/bin/bash", "-l"], tty=tty)
    return _run(argv, capture=False).returncode


def verify_refreshed_session(
    workspace: Path,
    expected_image_id: str,
    expected_payload_fingerprint: str | None,
) -> None:
    """Verify the recreated Session uses the reconciled artifact and payload."""
    name = session_container_name(workspace)
    actual_image_id = _docker_stdout(["docker", "inspect", name, "--format", "{{.Image}}"])
    if actual_image_id != expected_image_id:
        raise SessionError(
            f"refreshed Session Runtime uses {actual_image_id or '<unknown>'}, "
            f"expected {expected_image_id}"
        )
    if expected_payload_fingerprint is None:
        return
    probe = (
        "from booley.runtime.build_metadata import current_build_metadata; "
        "print(current_build_metadata().payload_fingerprint)"
    )
    result = _run(
        ["docker", "exec", name, "python3", "-I", "-c", probe],
        capture=True,
    )
    actual_fingerprint = result.stdout.strip()
    if result.returncode != 0 or actual_fingerprint != expected_payload_fingerprint:
        detail = result.stderr.strip() or actual_fingerprint or "probe produced no output"
        raise SessionError(
            "refreshed Session Runtime payload does not match the reconciled image: " + detail
        )


def down(workspace: Path, *, remove: bool = True) -> bool:
    """Stop (and by default remove) the session container. False if absent."""
    name = session_container_name(workspace)
    relay = _relay_resources(workspace)
    session_exists = idk.container_exists(name)
    relay_exists = _relay_objects_exist(relay)
    if session_exists:
        _run(["docker", "stop", name])
        if remove:
            _run(["docker", "rm", "-f", name])
    if remove and relay_exists:
        _remove_license_relay(relay)
    return session_exists or relay_exists


def status(workspace: Path) -> str:
    """One of ``"running"``, ``"stopped"``, ``"absent"``."""
    name = session_container_name(workspace)
    if not idk.container_exists(name):
        return "absent"
    return "running" if idk.container_running(name) else "stopped"
