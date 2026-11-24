"""Tests for sandbox.py: the `booley shell` ephemeral-container machinery.

ADR 0028 demolished the per-MCP-tool-call container spawning (DockerRunner.run /
run_sync, the CLI adapters, DockerSandboxBackend); what survives here is the
argv builder backing `booley shell`. Every test drives `ephemeral_argv`.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from booley.harness import sandbox
from booley.harness.sandbox import DockerRunner, DockerSandboxConfig
from booley.runtime import auth_token
from booley.runtime.platform_paths import docker_mount_path

_CLAUDE_LOGIN = '{"claudeAiOauth":{"accessToken":"SYNTHETIC_LOGIN"}}'
_CODEX_LOGIN = '{"tokens":{"access_token":"SYNTHETIC_LOGIN"},"OPENAI_API_KEY":null}'
_AGENT_ENV_KEYS = (
    "BOOLEY_DIR",
    "BOOLEY_PRIMARY_AUTH",
    "BOOLEY_PRIMARY_PROVIDER",
    "BOOLEY_AGENT_APP",
)


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A Project Root with `.booley/`, under a home that holds no credentials."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    for key in (*_AGENT_ENV_KEYS, *auth_token.CREDENTIAL_ENV_KEYS):
        monkeypatch.delenv(key, raising=False)
    root = tmp_path / "project"
    (root / ".booley").mkdir(parents=True)
    return root


def _write_logins() -> None:
    for app, body in (("claude", _CLAUDE_LOGIN), ("codex", _CODEX_LOGIN)):
        login = auth_token.subscription_creds_path(app)
        login.parent.mkdir(parents=True, exist_ok=True)
        login.write_text(body, encoding="utf-8")


def _runner(root: Path, **config: Any) -> DockerRunner:
    config.setdefault("needs_network", False)
    return DockerRunner(DockerSandboxConfig(image="test-image", **config), root)


def _values(argv: list[str], flag: str) -> list[str]:
    return [argv[i + 1] for i, word in enumerate(argv) if word == flag]


def test_docker_mount_path_keeps_a_unc_share() -> None:
    # The host platform is the boundary: only a Windows host rewrites drive letters.
    with patch("booley.runtime.platform_paths.IS_WINDOWS", True):
        assert docker_mount_path(Path("//server/share/dir")) == "//server/share/dir"


def test_shell_argv_runs_a_hardened_throwaway_container(project: Path) -> None:
    (project / ".booley" / "booley.toml").write_text(
        '[project]\nname = "configured-project"\n', encoding="utf-8"
    )
    runner = _runner(project)

    argv = runner.ephemeral_argv(["/bin/bash", "-l"], tty=True)

    assert argv[:3] == ["docker", "run", "--init"]
    assert {"--rm", "-i", "-t", "test-image"} <= set(argv)
    assert _values(argv, "-w") == ["/work"]
    assert any(value.endswith("target=/work") for value in _values(argv, "--mount"))
    assert _values(argv, "--network") == ["none"]
    assert "--add-host" not in argv
    assert not any("proxy" in value.lower() for value in _values(argv, "-e"))
    assert _values(argv, "--cap-drop") == ["ALL"]
    assert _values(argv, "--security-opt") == ["no-new-privileges"]
    assert "--pids-limit" in argv
    assert "--memory-swap" not in argv
    assert "booley.project=configured-project" in argv
    assert argv[-3:] == ["booley-preflight", "/bin/bash", "-l"]
    oneoff = runner.ephemeral_argv(["verilator", "--version"], tty=False)
    assert "-i" in oneoff
    assert "-t" not in oneoff
    assert oneoff[-2:] == ["verilator", "--version"]


def test_shell_limits_and_environment_follow_config(project: Path) -> None:
    argv = _runner(project, memory_limit="8g", memory_swap="10g", extra_env={"FOO": "bar"})
    argv = argv.ephemeral_argv(["true"], tty=False)

    assert _values(argv, "--memory") == ["8g"]
    assert _values(argv, "--memory-swap") == ["10g"]
    assert "FOO=bar" in _values(argv, "-e")


class FakeProxy:
    """Stand in for the egress proxy, which opens an all-interface listener."""

    started: list[FakeProxy]

    def __init__(self, *, host: str, auth_token: str) -> None:
        self.host = host
        self.auth_token = auth_token
        self.port = 9999
        self.url = f"http://booley:{auth_token}@host.docker.internal:{self.port}"
        self.stopped = 0

    def start(self) -> None:
        self.started.append(self)

    def stop(self) -> None:
        self.stopped += 1


def test_networked_shell_uses_an_authenticated_proxy_and_releases_it(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    FakeProxy.started = []
    # Private: the proxy binds 0.0.0.0 and no public entry takes a stand-in for it.
    monkeypatch.setattr(sandbox, "_load_egress_proxy_class", lambda: FakeProxy)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "SYNTHETIC_KEY")
    (project / ".booley" / "booley.toml").write_text(
        '[agent]\nclis = ["claude"]\nauth = "api_key"\n', encoding="utf-8"
    )
    runner = _runner(project, needs_network=True)

    argv = runner.ephemeral_argv(["true"], tty=False)

    proxy = FakeProxy.started[0]
    assert _values(argv, "--network") == ["bridge"]
    assert _values(argv, "--add-host") == ["host.docker.internal:host-gateway"]
    env = _values(argv, "-e")
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        assert f"{name}={proxy.url}" in env
    assert (proxy.host, len(proxy.auth_token) >= 43, len(FakeProxy.started)) == (
        "0.0.0.0",
        True,
        1,
    )
    env_file = Path(_values(argv, "--env-file")[0])
    assert "ANTHROPIC_API_KEY=SYNTHETIC_KEY" in env_file.read_text(encoding="utf-8")

    runner.cleanup_ephemeral()

    assert not env_file.exists()
    assert proxy.stopped == 1
    monkeypatch.delenv("ANTHROPIC_API_KEY")  # no env-file for the next runner to leave behind
    other = _runner(project, needs_network=True)
    other.ephemeral_argv(["true"], tty=False)
    other.ephemeral_argv(["true"], tty=False)
    assert len(FakeProxy.started) == 2  # a runner starts one proxy and reuses it
    second = FakeProxy.started[1]
    assert second.auth_token != proxy.auth_token
    other.stop_proxy()
    assert second.stopped == 1
    runner.stop_proxy()
    assert proxy.stopped == 1


_GATEWAY_KEYS = (
    "BOOLEY_GATEWAY_URL",
    "BOOLEY_GATEWAY_TOKEN",
    "BOOLEY_GATEWAY_TOKEN_COMMAND",
    "BOOLEY_GATEWAY_MODEL",
    "BOOLEY_GATEWAY_EFFORT",
)
_WARNING = "starting without Agent CLI logins or API keys"
_CREDENTIAL_CASES = {
    # A runner built for one Project follows that Project's policy, not the process's
    # BOOLEY_DIR or BOOLEY_PRIMARY_AUTH (fix b1ffe1e58).
    "runner-project-policy": SimpleNamespace(
        toml='[agent]\nprovider="claude"\nauth="api_key"\n',
        env={
            "BOOLEY_DIR": "wrong-project",
            "BOOLEY_PRIMARY_AUTH": "subscription",
            "ANTHROPIC_API_KEY": "SYNTHETIC_KEY",
            "CLAUDE_CODE_OAUTH_TOKEN": "SYNTHETIC_OAUTH",
        },
        env_file=["ANTHROPIC_API_KEY=SYNTHETIC_KEY"],
        absent=[".credentials.json", "SYNTHETIC"],
        absent_env=["CLAUDE_CODE_OAUTH_TOKEN"],
    ),
    "listed-clis-only": SimpleNamespace(
        toml='[agent]\nclis = ["claude"]\nauth = "api_key"\n',
        env={"OPENAI_API_KEY": "SYNTHETIC_OPENAI", "ANTHROPIC_API_KEY": "SYNTHETIC_KEY"},
        env_file=["ANTHROPIC_API_KEY=SYNTHETIC_KEY"],
        absent=[".codex", "SYNTHETIC_OPENAI"],
        absent_env=["OPENAI_API_KEY"],
    ),
    # The gateway reaches a Sandbox only through a Booley-issued spec.
    "codex-gateway": SimpleNamespace(
        toml='[agent]\nclis = ["claude", "codex"]\n[agent.auth]\ncodex = "gateway"\n',
        env=dict.fromkeys(_GATEWAY_KEYS, "SYNTHETIC_GATEWAY_VALUE"),
        absent=[*_GATEWAY_KEYS, "SYNTHETIC_GATEWAY_VALUE"],
        absent_env=[*_GATEWAY_KEYS, "SYNTHETIC_GATEWAY_VALUE"],
    ),
    "default-not-listed": SimpleNamespace(
        toml='[agent]\ndefault = "codex"\nclis = ["claude"]\n',
        env={"ANTHROPIC_API_KEY": "SYNTHETIC_KEY"},
        absent=[".credentials.json", ".codex", "--env-file"],
        warns=True,
    ),
    "unlisted-auth-key": SimpleNamespace(
        toml='[agent]\nclis = ["claude"]\n[agent.auth]\ncodex = "subscription"\n',
        env={"ANTHROPIC_API_KEY": "SYNTHETIC_KEY"},
        absent=[".credentials.json", ".codex", "--env-file"],
        warns=True,
    ),
    # An unresolvable BOOLEY_AGENT_APP falls back instead of stopping the shell (19d62a372).
    "ambiguous-agent-app": SimpleNamespace(
        toml='[agent]\nclis = ["claude", "codex"]\n',
        env={"BOOLEY_AGENT_APP": "auto"},
        mounts=["/home/agent/.claude/.credentials.json:ro", "/home/agent/.codex/auth.json:ro"],
    ),
}


@pytest.mark.parametrize("case", _CREDENTIAL_CASES)
def test_shell_forwards_only_the_credentials_the_project_permits(
    case: str, project: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    spec = _CREDENTIAL_CASES[case]
    for key, value in spec.env.items():
        monkeypatch.setenv(key, value)
    _write_logins()
    (project / ".booley" / "booley.toml").write_text(spec.toml, encoding="utf-8")
    runner = _runner(project)

    argv = runner.ephemeral_argv(["true"], tty=False)

    try:
        env_files = _values(argv, "--env-file")
        body = Path(env_files[0]).read_text(encoding="utf-8") if env_files else ""
        for line in getattr(spec, "env_file", []):
            assert line in body
        for fragment in getattr(spec, "absent", []):
            assert fragment not in " ".join(argv)
        for fragment in getattr(spec, "absent_env", []):
            assert fragment not in body
        for mount in getattr(spec, "mounts", []):
            assert any(word.endswith(mount) for word in _values(argv, "-v"))
    finally:
        runner.cleanup_ephemeral()
    assert (_WARNING in capsys.readouterr().err) is getattr(spec, "warns", False)
