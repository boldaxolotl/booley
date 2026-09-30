"""Read-only running artifact observation and incarnation-bound receipt transport.

Docker inspection is authoritative on the host. A root-owned receipt in the
container's own filesystem carries that observation to bare, editor, automatic,
and supervised commands without exposing Docker or host-private authority.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import socket
import stat
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from booley.core.boundary import (
    BoundaryError,
    require_dict,
    require_int,
    require_str,
    require_str_value,
)
from booley.runtime import runtime_context
from booley.runtime.image_provenance import is_local_image_id

RECEIPT_PATH = Path("/run/booley-artifact/receipt.json")
_CONTAINER_ID = re.compile(r"[a-f0-9]{64}")
_OBSERVER_SECONDS = 180
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SandboxArtifact:
    """Verified local Docker image ID, or a bounded unavailable reason."""

    image_id: str | None = None
    container_id: str | None = None
    booley_version: str | None = None
    unavailable_reason: str = "image-identity-unavailable"


def _docker_stdout(argv: list[str]) -> str | None:
    try:
        result = subprocess.run(argv, capture_output=True, text=True, check=False, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def _container_state(workspace: Path, container: str) -> dict | None:
    from booley.runtime import session_runtime as sr

    raw = _docker_stdout(["docker", "inspect", container])
    state = sr._decode_container_inspect(raw) if raw is not None else None
    if state is None:
        return None
    try:
        config = require_dict(state.get("Config"), field="Config")
        labels = require_dict(config.get("Labels"), field="Labels")
        running = require_dict(state.get("State"), field="State").get("Running")
        owned = labels.get("booley.role") == "interactive" and labels.get(
            "booley.project-id"
        ) == sr.dc.canonical_project_id(workspace)
        if not owned or running is not True:
            return None
        name = require_str_value(
            state.get("Name", container), field="container name"
        ).removeprefix("/")
        if not sr._inspected_container_serves_workspace(name, raw, workspace):
            return None
        image_id, container_id = require_str(state, "Image"), require_str(state, "Id")
        if not is_local_image_id(image_id) or _CONTAINER_ID.fullmatch(container_id) is None:
            return None
    except (BoundaryError, OSError, ValueError):
        return None
    return state


def observe(workspace: Path, *, container: str | None = None) -> SandboxArtifact:
    """Observe actual running machinery; never start, issue, pull, or rebuild it."""
    if runtime_context.inside_session_runtime():
        return observe_inside()
    from booley.runtime import session_runtime as sr

    if container is None:
        names = _docker_stdout(
            [
                "docker",
                "ps",
                "--filter",
                f"label={sr.dc.INTERACTIVE_ROLE_LABEL}",
                "--format",
                "{{.Names}}",
            ]
        )
        states = [
            state
            for name in (names or "").splitlines()
            if (state := _container_state(workspace, name)) is not None
        ]
        if len(states) != 1:
            return SandboxArtifact(unavailable_reason="sandbox-absent-or-ambiguous")
        state = states[0]
    else:
        state = _container_state(workspace, container)
    if state is None:
        return SandboxArtifact()
    return SandboxArtifact(state["Image"], state["Id"], unavailable_reason="")


def observe_execution(workspace: Path, *, container: str | None = None) -> SandboxArtifact:
    """Also read the executing package version from the exact observed container."""
    observed = observe(workspace, container=container)
    if observed.image_id is None or runtime_context.inside_session_runtime():
        return observed
    version = _container_booley_version(observed.container_id)
    if version is None:
        return SandboxArtifact(unavailable_reason="sandbox-version-unavailable")
    return SandboxArtifact(observed.image_id, observed.container_id, version, "")


def _container_booley_version(container_id: str) -> str | None:
    """Read and validate the package version in one exact container."""
    version = _docker_stdout(
        [
            "docker",
            "exec",
            container_id,
            "python3",
            "-I",
            "-c",
            "import booley; print(booley.__version__)",
        ]
    )
    if version is None or not re.fullmatch(r"[a-zA-Z0-9_.+-]{1,128}", version):
        return None
    return version


def _incarnation() -> tuple[str, str]:
    return socket.gethostname(), str(Path("/proc/self/ns/uts").readlink())


def _root_owned_readonly(path: Path) -> bool:
    file_state, parent = path.lstat(), path.parent.lstat()
    return (
        stat.S_ISREG(file_state.st_mode)
        and file_state.st_uid == 0
        and not file_state.st_mode & 0o222
        and stat.S_ISDIR(parent.st_mode)
        and parent.st_uid == 0
        and not parent.st_mode & 0o022
    )


def observe_inside() -> SandboxArtifact:
    """Accept only a protected receipt belonging to this live UTS incarnation."""
    try:
        if not _root_owned_readonly(RECEIPT_PATH):
            return SandboxArtifact(unavailable_reason="artifact-receipt-untrusted")
        values = require_dict(json.loads(RECEIPT_PATH.read_text()), field="artifact receipt")
        schema = require_int(values.get("schema_version"), field="schema_version")
        image_id = require_str(values, "image_id")
        container_id = require_str(values, "container_id")
        hostname, namespace = _incarnation()
        matching = (hostname, namespace) == (
            require_str(values, "hostname"),
            require_str(values, "uts_namespace"),
        )
        version = require_str(values, "booley_version")
        if schema != 1 or not matching or not is_local_image_id(image_id):
            return SandboxArtifact(unavailable_reason="artifact-receipt-stale")
        if _CONTAINER_ID.fullmatch(container_id) is None or len(version) > 128:
            return SandboxArtifact(unavailable_reason="artifact-receipt-invalid")
    except (OSError, ValueError, UnicodeError):
        return SandboxArtifact(unavailable_reason="artifact-receipt-unavailable")
    return SandboxArtifact(image_id, container_id, version, "")


# Stdlib-only transport: it also works when the active image predates this module.
# Root writes only diagnostic metadata, never credentials or execution authority.
_RECEIPT_WRITER = """import json, os, pathlib, socket, stat, sys, tempfile
root = pathlib.Path('/run/booley-artifact')
root.mkdir(mode=0o755, exist_ok=True)
s = root.lstat()
if not stat.S_ISDIR(s.st_mode) or s.st_uid != 0 or s.st_mode & 0o022:
    sys.exit(1)
image, container, version = sys.argv[1:]
values = dict(schema_version=1, image_id=image, container_id=container,
              hostname=socket.gethostname(), uts_namespace=os.readlink('/proc/self/ns/uts'),
              booley_version=version)
fd, tmp = tempfile.mkstemp(dir=root)
try:
    with os.fdopen(fd, 'w') as handle:
        json.dump(values, handle)
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(tmp, 0o444)
    os.replace(tmp, root / 'receipt.json')
finally:
    pathlib.Path(tmp).unlink(missing_ok=True)
"""


def publish_receipt(workspace: Path, container: str) -> bool:
    """Provision diagnostic identity after inspecting this exact running container."""
    state = _container_state(workspace, container)
    if state is None:
        return False
    # Version discovery runs as the ordinary Sandbox user. The privileged
    # writer uses stdlib only and excludes user PATH, site hooks, and imports.
    version = _container_booley_version(state["Id"])
    if version is None:
        return False
    return _docker_stdout(_receipt_command(state, version)) is not None


def _receipt_command(state: dict, version: str) -> list[str]:
    """Fixed privileged transport; every variable is validated diagnostic data."""
    return [
        "docker",
        "exec",
        "--user",
        "0",
        "--workdir",
        "/",
        state["Id"],
        "/usr/bin/env",
        "-i",
        "PATH=/usr/local/bin:/usr/bin:/bin",
        "python3",
        "-I",
        "-S",
        "-c",
        _RECEIPT_WRITER,
        state["Image"],
        state["Id"],
        version,
    ]


def launch_vscode_observer(workspace: Path, spec_digest: str) -> None:
    """Watch a later editor start for at most three minutes, without a daemon."""
    argv = [
        sys.executable,
        "-m",
        "booley.runtime.sandbox_artifact",
        "--workspace",
        str(workspace),
        "--spec-digest",
        spec_digest,
    ]
    options = (
        {"creationflags": subprocess.DETACHED_PROCESS}
        if os.name == "nt"
        else {
            "start_new_session": True,
        }
    )
    try:
        process = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            **options,
        )
        threading.Thread(target=process.wait, daemon=True).start()
    except OSError:
        logger.info("Sandbox artifact observer unavailable; deep Doctor will report identity due")


def _prepared_vscode_state(workspace: Path, spec_digest: str) -> dict | None:
    from booley.runtime import session_runtime as sr

    names = _docker_stdout(
        [
            "docker",
            "ps",
            "--filter",
            f"label={sr.dc.INTERACTIVE_ROLE_LABEL}",
            "--filter",
            f"label=devcontainer.local_folder={workspace}",
            "--format",
            "{{.Names}}",
        ]
    )
    candidates = (names or "").splitlines()
    state = _container_state(workspace, candidates[0]) if len(candidates) == 1 else None
    if state is None or state["Config"]["Labels"].get("booley.spec-digest") != spec_digest:
        return None
    return state


def watch_vscode_start(workspace: Path, spec_digest: str) -> bool:
    """Cover reuse and a rebuild that starts after preparation observes the old editor."""
    deadline = time.monotonic() + _OBSERVER_SECONDS
    initial = _prepared_vscode_state(workspace, spec_digest)
    previous = None
    written = False
    state = initial
    while time.monotonic() < deadline:
        if state is not None:
            token = (state["Id"], state["State"].get("StartedAt"))
            if token != previous and publish_receipt(workspace, state["Id"]):
                written = True
                # A new start was observed, rather than the editor already
                # running when prepare returned. Its receipt is now available.
                if initial is None or previous is not None:
                    return True
                previous = token
        time.sleep(0.5)
        state = _prepared_vscode_state(workspace, spec_digest)
    return written


def main() -> int:
    """Bounded host observer used by the fixed VS Code preparation command."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--spec-digest", required=True)
    args = parser.parse_args()
    return 0 if watch_vscode_start(args.workspace, args.spec_digest) else 1


if __name__ == "__main__":
    raise SystemExit(main())
