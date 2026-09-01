"""Docker sandbox infrastructure for host-side debug shells.

ADR 0028 made Booley container-only: EDA tools, Flows, Specialists, and agents run as plain
subprocesses inside the one Session Runtime (devcontainer), so the old
per-MCP-tool-call container spawning (``run``/``run_sync``) is gone. What
remains backs ``booley shell`` — a throwaway sandbox container spawned
from the host for debugging — plus the ``DockerSandboxConfig.verify``
health probe.
"""

from __future__ import annotations

import contextlib
import logging
import os
import secrets
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from booley.config.project_config import project_name
from booley.runtime import auth_token
from booley.runtime.platform_paths import docker_mount_path as _docker_mount_path

logger = logging.getLogger(__name__)


def _load_egress_proxy_class() -> type:
    """Load EgressProxy from the booley.docker package."""
    from booley.docker.egress_proxy import EgressProxy

    return EgressProxy


# ---------------------------------------------------------------------------
# Docker configuration
# ---------------------------------------------------------------------------


@dataclass
class DockerSandboxConfig:
    """Configuration for the Docker sandbox."""

    image: str = "booley-sandbox"
    extra_mounts: list[tuple[str, str]] = field(default_factory=list)
    extra_env: dict[str, str] = field(default_factory=dict)
    needs_network: bool = True
    memory_limit: str = "512m"
    pids_limit: int = 512

    def verify(self) -> str | None:  # noqa: PLR0911 — ordered precondition ladder; each early return is a distinct health-check failure
        """Check Docker is available and image exists.

        Returns error message string, or None if healthy.
        """
        if not shutil.which("docker"):
            return "docker not found on PATH"

        try:
            result = subprocess.run(
                ["docker", "info"],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
            if result.returncode != 0:
                stderr = (result.stderr or "").strip()
                # Distinguish permission errors from a down daemon — collapsing
                # both into "daemon not running" sends the operator down the
                # wrong path (e.g. restarting a daemon that is actually up but
                # the user lacks docker-group access in the current session).
                low = stderr.lower()
                if "permission denied" in low:
                    return (
                        "Docker permission denied — user lacks access to the "
                        "Docker socket. Add the user to the 'docker' group and "
                        "start a fresh login session (or run via 'sg docker'). "
                        f"Detail: {stderr}"
                    )
                detail = f" — {stderr}" if stderr else ""
                return f"Docker daemon not running (docker info failed){detail}"
        except subprocess.TimeoutExpired:
            return "Docker daemon not responding (docker info timed out)"
        except FileNotFoundError:
            return "docker not found on PATH"

        try:
            result = subprocess.run(
                ["docker", "image", "inspect", self.image],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
            if result.returncode != 0:
                return (
                    f"Docker image '{self.image}' not found. "
                    f"Build it with: booley init (or docker build)"
                )
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return f"Could not verify Docker image '{self.image}'"

        return None


# ---------------------------------------------------------------------------
# DockerRunner (ephemeral `booley shell` containers only)
# ---------------------------------------------------------------------------


def _append_auth_mounts(docker_cmd: list[str]) -> None:
    """Mount CLI auth files read-only for OAuth inside containers."""
    codex_auth = Path.home() / ".codex" / "auth.json"
    if codex_auth.is_file():
        docker_cmd.extend(
            [
                "-v",
                f"{_docker_mount_path(codex_auth)}:/home/agent/.codex/auth.json:ro",
            ]
        )
    claude_auth = Path.home() / ".claude" / ".credentials.json"
    if claude_auth.is_file():
        docker_cmd.extend(
            [
                "-v",
                f"{_docker_mount_path(claude_auth)}:/home/agent/.claude/.credentials.json:ro",
            ]
        )


def _append_persistent_volumes(docker_cmd: list[str]) -> None:
    """Append named Docker volumes for pip caching.

    The cargo registry/git volumes that used to be mounted here went away with
    the image's Rust toolchain (bwave is built in a throwaway builder stage and
    copied in): with no cargo in the image there was nothing to populate them.
    Pre-existing ``booley-cargo-*`` volumes on a host are simply left orphaned.
    """
    docker_cmd.extend(
        [
            "-v",
            "booley-pip-local:/home/agent/.local",
        ]
    )


def _append_extra_mounts(
    docker_cmd: list[str],
    extra_mounts: list[tuple[str, str]],
) -> None:
    """Append configured extra mounts (dedup by container path, last wins)."""
    seen_targets: dict[str, str] = {}
    for host_path, container_path in extra_mounts:
        seen_targets[container_path] = _docker_mount_path(Path(host_path))
    for container_path, host_docker_path in seen_targets.items():
        docker_cmd.extend(["-v", f"{host_docker_path}:{container_path}"])


def _append_env_vars(
    docker_cmd: list[str],
    extra_env: dict[str, str],
    proxy_url: str | None,
) -> None:
    """Append environment variable flags: config and proxy."""
    for k, v in extra_env.items():
        docker_cmd.extend(["-e", f"{k}={v}"])
    if proxy_url:
        docker_cmd.extend(
            [
                "-e",
                f"HTTP_PROXY={proxy_url}",
                "-e",
                f"HTTPS_PROXY={proxy_url}",
                "-e",
                f"http_proxy={proxy_url}",
                "-e",
                f"https_proxy={proxy_url}",
            ]
        )


class DockerRunner:
    """Builds throwaway sandbox containers with the worktree bind-mounted.

    Post ADR 0028 this backs only ``booley shell``: the per-MCP-tool-call
    ``run``/``run_sync`` execution paths were demolished with the split-runtime
    architecture.
    """

    def __init__(self, config: DockerSandboxConfig, worktree: Path, label: str = "") -> None:
        self.config = config
        self.worktree = worktree
        self._env_file: Path | None = None
        self._proxy: Any = None

    def _ensure_proxy(self) -> str:
        """Start the egress proxy if not already running. Returns proxy URL."""
        if self._proxy is not None:
            return self._proxy.url
        EgressProxy = _load_egress_proxy_class()
        # Docker reaches this host listener through its bridge gateway, so a
        # loopback bind is insufficient. Authenticate the exceptional
        # all-interface bind with a fresh high-entropy credential: unrelated
        # LAN clients can see the ephemeral port but cannot use the relay.
        self._proxy = EgressProxy(
            host="0.0.0.0",
            auth_token=secrets.token_urlsafe(32),
        )
        self._proxy.start()
        logger.debug("Egress proxy started for Docker sandbox on port %d", self._proxy.port)
        return self._proxy.url

    def stop_proxy(self) -> None:
        """Stop the egress proxy (call on teardown)."""
        if self._proxy is not None:
            self._proxy.stop()
            self._proxy = None

    def __del__(self) -> None:
        with contextlib.suppress(Exception):
            self.stop_proxy()

    def _build_docker_cmd(
        self,
        cmd: list[str],
        *,
        cwd: str = "/work",
        interactive: bool = False,
        tty: bool = False,
        remove: bool = False,
    ) -> list[str]:
        """Build the full `docker run` command."""
        proxy_url, effective_network = self._resolve_network()
        docker_cmd = self._base_docker_flags(cwd, effective_network, remove=remove)
        if interactive:
            docker_cmd.insert(3, "-i")
        if tty:
            docker_cmd.insert(3, "-t")

        self._append_api_key_env_file(docker_cmd)
        _append_auth_mounts(docker_cmd)
        _append_persistent_volumes(docker_cmd)
        _append_extra_mounts(docker_cmd, self.config.extra_mounts)
        _append_env_vars(docker_cmd, self.config.extra_env, proxy_url)

        docker_cmd.append(self.config.image)
        docker_cmd.extend(self._with_worktree_preflight(cmd))
        return docker_cmd

    def ephemeral_argv(
        self,
        payload: list[str],
        *,
        tty: bool,
    ) -> list[str]:
        """Build a ``docker run --rm`` argv for a throwaway sandbox container.

        Backs ``booley shell``: a fresh container with the worktree at ``/work``
        and the sandbox security / mount / env flags, auto-removed on exit.
        ``payload`` is the command to exec (an interactive login shell, or a
        one-off command); ``tty`` allocates a pseudo-TTY for interactive use.
        The caller execs this directly and inherits stdio.
        """
        return self._build_docker_cmd(
            payload,
            interactive=True,
            tty=tty,
            remove=True,
        )

    def cleanup_ephemeral(self) -> None:
        """Release transient resources created while building an ephemeral argv.

        ``ephemeral_argv`` may have written an API-key env-file and started the
        egress proxy (when networking is enabled). The exec path never returns
        through a ``finally``, so callers invoke this after the child exits to
        avoid leaking the temp file / proxy process.
        """
        if self._env_file is not None:
            with contextlib.suppress(OSError):
                self._env_file.unlink()
            self._env_file = None
        if self._proxy is not None:
            with contextlib.suppress(Exception):  # best-effort teardown
                self._proxy.stop()
            self._proxy = None

    def _resolve_network(self) -> tuple[str | None, str]:
        """Return (proxy_url, network_mode) based on config.needs_network."""
        if self.config.needs_network:
            return self._ensure_proxy(), "bridge"
        return None, "none"

    def _base_docker_flags(
        self,
        cwd: str,
        network: str,
        *,
        remove: bool = False,
    ) -> list[str]:
        """Build core docker run flags (worktree, security, resource limits)."""
        mount_spec = f"type=bind,src={_docker_mount_path(self.worktree)},target=/work"
        flags = [
            "docker",
            "run",
            "--init",
            *(["--rm"] if remove else []),
            "--label",
            f"booley.project={project_name(self.worktree)}",
            "--mount",
            mount_spec,
            "-w",
            cwd,
            "--network",
            network,
            "--memory",
            self.config.memory_limit,
            "--pids-limit",
            str(self.config.pids_limit),
            "--security-opt",
            "no-new-privileges",
            "--cap-drop",
            "ALL",
        ]
        # When networking is enabled the container reaches host services
        # (for example, the egress proxy) via host.docker.internal. Docker
        # Desktop maps that name automatically, but Linux Docker Engine does
        # NOT — without this the proxy is unreachable (proxy connect is
        # refused). host-gateway is a no-op where the name already resolves,
        # so it is safe everywhere.
        if network != "none":
            flags += ["--add-host", "host.docker.internal:host-gateway"]
        return flags

    @staticmethod
    def _with_worktree_preflight(cmd: list[str]) -> list[str]:
        """Refuse payload execution if the /work mount is unusable."""
        script = (
            "if ! [ -d /work ] || ! [ -r /work ] || ! [ -x /work ]; then "
            'echo "BOOLEY_INFRA_ERROR: /work mount is unavailable; refusing to run command" >&2; '
            "exit 97; "
            "fi; "
            "if ! [ -w /work ]; then "
            'echo "BOOLEY_INFRA_ERROR: /work mount is not writable; refusing to run command" >&2; '
            "exit 97; "
            "fi; "
            "if ! (cd /work) >/dev/null 2>&1; then "
            'echo "BOOLEY_INFRA_ERROR: cannot enter /work; refusing to run command" >&2; '
            "exit 97; "
            "fi; "
            'exec "$@"'
        )
        return ["/bin/sh", "-lc", script, "booley-preflight", *cmd]

    def _append_api_key_env_file(self, docker_cmd: list[str]) -> None:
        """Write API keys to a transient env-file and append --env-file flag."""
        from booley.config.agent import resolve_auth_policy

        # [agent] auth = "subscription": the API keys are exactly what must NOT
        # reach the agents (an exported key outranks — and bills over — the
        # subscription under the agent CLI's precedence), so they are not
        # forwarded. Claude's OAuth token still rides: it bills the
        # subscription and is the rotation-free credential for long runs.
        scrubbed: frozenset[str] = frozenset()
        if resolve_auth_policy() == "subscription":
            scrubbed = frozenset(auth_token.API_KEY_ENV.values())

        # CLAUDE_CODE_OAUTH_TOKEN rides along so a one-year `claude setup-token`
        # credential reaches headless in-container runs. Only set vars are
        # written (the walrus guard), so an unset token is simply omitted.
        _api_keys = {
            k: v
            for k in (
                "ANTHROPIC_API_KEY",
                "OPENAI_API_KEY",
                "CLAUDE_CODE_OAUTH_TOKEN",
            )
            if k not in scrubbed and (v := os.environ.get(k))
        }
        # A credential stored by `booley auth` fills the gap when nothing is
        # exported; an exported value still wins (setdefault). Covers both apps:
        # Claude's OAuth token and Codex's API key. Never inject an empty value —
        # it would shadow the mounted subscription credentials.
        for credential in auth_token.CREDENTIALS.values():
            if credential.env_var in scrubbed:
                continue
            if stored := auth_token.resolve_token(credential.app):
                _api_keys.setdefault(credential.env_var, stored)
        self._env_file: Path | None = None
        if not _api_keys:
            return
        import tempfile

        fd, env_path = tempfile.mkstemp(prefix="booley_env_", suffix=".env")
        try:
            with os.fdopen(fd, "w") as f:
                for k, v in _api_keys.items():
                    f.write(f"{k}={v}\n")
        except OSError:
            os.close(fd)
            raise
        self._env_file = Path(env_path)
        docker_cmd.extend(["--env-file", env_path])
