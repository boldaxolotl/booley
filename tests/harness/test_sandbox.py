"""Tests for sandbox.py — the `booley shell` ephemeral-container machinery.

ADR 0028 demolished the per-MCP-tool-call container spawning (DockerRunner.run /
run_sync, the CLI adapters, DockerSandboxBackend); what survives here is the
argv builder backing `booley shell` plus DockerSandboxConfig.verify.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from booley.harness.sandbox import (
    DockerRunner,
    DockerSandboxConfig,
)
from booley.platform_paths import docker_mount_path as _docker_mount_path

# ===========================================================================
# _docker_mount_path
# ===========================================================================


class TestDockerMountPath:
    @patch("booley.platform_paths.IS_WINDOWS", False)
    def test_linux_passthrough(self):
        p = MagicMock()
        p.as_posix.return_value = "/home/user/project"
        assert _docker_mount_path(p) == "/home/user/project"

    @patch("booley.platform_paths.IS_WINDOWS", True)
    def test_windows_drive_letter(self):
        p = MagicMock()
        p.as_posix.return_value = "C:/projects/project"
        assert _docker_mount_path(p) == "/c/projects/project"

    @patch("booley.platform_paths.IS_WINDOWS", True)
    def test_windows_lowercase_drive(self):
        p = MagicMock()
        p.as_posix.return_value = "D:/data/files"
        assert _docker_mount_path(p) == "/d/data/files"

    @patch("booley.platform_paths.IS_WINDOWS", True)
    def test_windows_unc_passthrough(self):
        p = MagicMock()
        p.as_posix.return_value = "//server/share/dir"
        assert _docker_mount_path(p) == "//server/share/dir"


# ===========================================================================
# DockerRunner._build_docker_cmd
# ===========================================================================


class TestDockerRunnerBuildCmd:
    def _make_runner(self, worktree: str = "/tmp/wt") -> DockerRunner:
        config = DockerSandboxConfig(image="test-image", needs_network=False)
        return DockerRunner(config, worktree=Path(worktree))

    @patch("booley.platform_paths.IS_WINDOWS", False)
    def test_basic_command(self):
        runner = self._make_runner()
        cmd = runner._build_docker_cmd(["echo", "hello"])
        assert cmd[:3] == ["docker", "run", "--init"]
        assert "--mount" in cmd
        mount_args = [cmd[i + 1] for i, x in enumerate(cmd) if x == "--mount"]
        assert any("target=/work" in v for v in mount_args)
        assert "-w" in cmd
        idx = cmd.index("-w")
        assert cmd[idx + 1] == "/work"
        assert "test-image" in cmd
        assert cmd[-2:] == ["echo", "hello"]

    @patch("booley.platform_paths.IS_WINDOWS", False)
    def test_security_hardening_flags(self):
        runner = self._make_runner()
        cmd = runner._build_docker_cmd(["echo", "hello"])
        assert "--memory" in cmd
        assert "--pids-limit" in cmd
        cap_idx = cmd.index("--cap-drop")
        assert cmd[cap_idx + 1] == "ALL"
        sec_idx = cmd.index("--security-opt")
        assert cmd[sec_idx + 1] == "no-new-privileges"

    @patch("booley.platform_paths.IS_WINDOWS", False)
    def test_custom_cwd(self):
        runner = self._make_runner()
        cmd = runner._build_docker_cmd(["ls"], cwd="/work/subdir")
        idx = cmd.index("-w")
        assert cmd[idx + 1] == "/work/subdir"

    @patch("booley.platform_paths.IS_WINDOWS", False)
    def test_interactive_flag(self):
        runner = self._make_runner()
        cmd = runner._build_docker_cmd(["cat"], interactive=True)
        assert "-i" in cmd

    @patch("booley.platform_paths.IS_WINDOWS", False)
    def test_config_extra_env_vars(self):
        config = DockerSandboxConfig(
            image="test-image",
            needs_network=False,
            extra_env={"FOO": "bar"},
        )
        runner = DockerRunner(config, worktree=Path("/tmp/wt"))
        cmd = runner._build_docker_cmd(["test"])
        env_entries = [cmd[i + 1] for i, x in enumerate(cmd) if x == "-e"]
        assert "FOO=bar" in env_entries

    @patch("booley.platform_paths.IS_WINDOWS", False)
    def test_no_network_uses_none(self):
        runner = self._make_runner()  # needs_network=False
        cmd = runner._build_docker_cmd(["test"])
        idx = cmd.index("--network")
        assert cmd[idx + 1] == "none"

    @patch("booley.platform_paths.IS_WINDOWS", False)
    def test_remove_and_tty_flags(self):
        runner = self._make_runner()
        cmd = runner._build_docker_cmd(
            ["cat"],
            interactive=True,
            tty=True,
            remove=True,
        )
        assert "--rm" in cmd
        assert "-t" in cmd
        assert "-i" in cmd


# ===========================================================================
# DockerRunner.ephemeral_argv  (backs `booley shell`)
# ===========================================================================


class TestEphemeralArgv:
    def _make_runner(self) -> DockerRunner:
        config = DockerSandboxConfig(image="test-image", needs_network=False)
        return DockerRunner(config, worktree=Path("/tmp/wt"), label="shell")

    @patch("booley.platform_paths.IS_WINDOWS", False)
    def test_interactive_shell_is_ephemeral_tty(self):
        runner = self._make_runner()
        argv = runner.ephemeral_argv(["/bin/bash", "-l"], tty=True)
        # throwaway + interactive tty
        assert "--rm" in argv
        assert "-t" in argv and "-i" in argv
        # sandbox hardening / mount flags
        assert argv[argv.index("--network") + 1] == "none"
        assert argv[argv.index("--cap-drop") + 1] == "ALL"
        assert any("target=/work" in argv[i + 1] for i, x in enumerate(argv) if x == "--mount")
        assert "test-image" in argv
        # payload reaches the preflight wrapper's exec
        assert argv[-2:] == ["/bin/bash", "-l"]

    @patch("booley.platform_paths.IS_WINDOWS", False)
    def test_oneoff_command_has_no_tty(self):
        runner = self._make_runner()
        argv = runner.ephemeral_argv(["verilator", "--version"], tty=False)
        assert "--rm" in argv
        assert "-t" not in argv  # no pseudo-TTY for a piped one-off
        assert argv[-2:] == ["verilator", "--version"]

    def test_cleanup_ephemeral_removes_env_file(self, tmp_path):
        runner = self._make_runner()
        stub = tmp_path / "booley_env.env"
        stub.write_text("K=V\n")
        runner._env_file = stub
        proxy = MagicMock()
        runner._proxy = proxy
        runner.cleanup_ephemeral()
        assert not stub.exists()
        proxy.stop.assert_called_once()
        assert runner._env_file is None and runner._proxy is None

    @patch("booley.platform_paths.IS_WINDOWS", False)
    def test_needs_network_uses_bridge_with_proxy(self):
        config = DockerSandboxConfig(image="test-image", needs_network=True)
        runner = DockerRunner(config, worktree=Path("/tmp/wt"))
        mock_proxy = MagicMock()
        mock_proxy.url = "http://host.docker.internal:9999"
        runner._proxy = mock_proxy
        cmd = runner._build_docker_cmd(["test"])
        idx = cmd.index("--network")
        assert cmd[idx + 1] == "bridge"

    @patch("booley.harness.sandbox.secrets.token_urlsafe", return_value="fresh-secret")
    @patch("booley.harness.sandbox._load_egress_proxy_class")
    def test_host_proxy_all_interface_bind_requires_fresh_authentication(
        self, load_proxy, token_urlsafe
    ):
        proxy_type = MagicMock()
        proxy = proxy_type.return_value
        proxy.url = "http://booley:fresh-secret@host.docker.internal:9999"
        load_proxy.return_value = proxy_type
        runner = DockerRunner(
            DockerSandboxConfig(image="test-image", needs_network=True),
            worktree=Path("/tmp/wt"),
        )

        assert runner._ensure_proxy() == proxy.url
        token_urlsafe.assert_called_once_with(32)
        proxy_type.assert_called_once_with(host="0.0.0.0", auth_token="fresh-secret")
        proxy.start.assert_called_once_with()

    @patch("booley.platform_paths.IS_WINDOWS", False)
    def test_needs_network_injects_proxy_env_vars(self):
        config = DockerSandboxConfig(image="test-image", needs_network=True)
        runner = DockerRunner(config, worktree=Path("/tmp/wt"))
        mock_proxy = MagicMock()
        mock_proxy.url = "http://host.docker.internal:9999"
        runner._proxy = mock_proxy
        cmd = runner._build_docker_cmd(["test"])
        env_entries = [cmd[i + 1] for i, x in enumerate(cmd) if x == "-e"]
        assert "HTTPS_PROXY=http://host.docker.internal:9999" in env_entries
        assert "HTTP_PROXY=http://host.docker.internal:9999" in env_entries
        assert "https_proxy=http://host.docker.internal:9999" in env_entries
        assert "http_proxy=http://host.docker.internal:9999" in env_entries

    @patch("booley.platform_paths.IS_WINDOWS", False)
    def test_no_network_no_proxy_env(self):
        runner = self._make_runner()  # needs_network=False
        cmd = runner._build_docker_cmd(["test"])
        env_entries = [cmd[i + 1] for i, x in enumerate(cmd) if x == "-e"]
        assert not any("PROXY" in e or "proxy" in e for e in env_entries)

    def test_stop_proxy_cleanup(self):
        config = DockerSandboxConfig(needs_network=True)
        runner = DockerRunner(config, worktree=Path("/tmp/wt"))
        mock_proxy = MagicMock()
        runner._proxy = mock_proxy
        runner.stop_proxy()
        mock_proxy.stop.assert_called_once()
        assert runner._proxy is None

    def test_stop_proxy_noop_when_no_proxy(self):
        config = DockerSandboxConfig(needs_network=False)
        runner = DockerRunner(config, worktree=Path("/tmp/wt"))
        runner.stop_proxy()  # should not raise
