"""Tests for booley.runtime_context -- the Session Runtime context guard (ADR 0028)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from booley import runtime_context

# ---------------------------------------------------------------------------
# Runtime simulation helpers. Tests run on a host machine today, but the suite
# could someday run inside CI containers -- so every test pins BOTH signals
# (env marker + filesystem probes) instead of trusting the real environment.
# ---------------------------------------------------------------------------


@pytest.fixture
def host(monkeypatch):
    """Simulate the host: env marker unset, no container marker files."""
    monkeypatch.delenv("BOOLEY_CONTAINER", raising=False)
    with patch.object(Path, "exists", lambda self: False):
        yield


@pytest.fixture
def container_env(monkeypatch):
    """Simulate the sandbox via the baked-in env marker (primary signal)."""
    monkeypatch.setenv("BOOLEY_CONTAINER", "1")


# ===========================================================================
# inside_session_runtime()
# ===========================================================================


class TestInsideSessionRuntime:
    def test_false_on_host(self, host):
        assert runtime_context.inside_session_runtime() is False

    def test_true_with_env_marker(self, container_env):
        assert runtime_context.inside_session_runtime() is True

    def test_env_marker_must_be_exactly_1(self, monkeypatch):
        """BOOLEY_CONTAINER=0 (or junk) does not count as inside."""
        monkeypatch.setenv("BOOLEY_CONTAINER", "0")
        with patch.object(Path, "exists", lambda self: False):
            assert runtime_context.inside_session_runtime() is False

    def test_true_with_dockerenv_fallback(self, monkeypatch):
        """/.dockerenv probe covers images built before the env var existed."""
        monkeypatch.delenv("BOOLEY_CONTAINER", raising=False)
        # as_posix(): plain str() renders "\\.dockerenv" on Windows and the
        # comparison silently never matches (F-16).
        with patch.object(Path, "exists", lambda self: self.as_posix() == "/.dockerenv"):
            assert runtime_context.inside_session_runtime() is True

    def test_true_with_containerenv_fallback(self, monkeypatch):
        """/run/.containerenv covers Podman/OCI runtimes."""
        monkeypatch.delenv("BOOLEY_CONTAINER", raising=False)
        with patch.object(Path, "exists", lambda self: self.as_posix() == "/run/.containerenv"):
            assert runtime_context.inside_session_runtime() is True


# ===========================================================================
# ensure_proxy_env()
# ===========================================================================


class TestEnsureProxyEnv:
    """docker-exec entry drops the spec's remoteEnv — the self-heal must
    default the proxy vars in the sandbox, and ONLY in the sandbox."""

    @pytest.fixture(autouse=True)
    def _no_inherited_proxy(self, monkeypatch):
        for var in (*runtime_context._PROXY_ENV_VARS, "NO_PROXY"):
            monkeypatch.delenv(var, raising=False)

    def test_defaults_all_proxy_vars_in_sandbox(self, container_env):
        import os

        assert runtime_context.ensure_proxy_env() is True
        for var in runtime_context._PROXY_ENV_VARS:
            assert os.environ[var] == runtime_context._PROXY_URL
        assert os.environ["NO_PROXY"] == "localhost,127.0.0.1"

    def test_noop_on_host(self, host):
        import os

        assert runtime_context.ensure_proxy_env() is False
        assert "HTTPS_PROXY" not in os.environ

    def test_noop_in_foreign_container(self, monkeypatch):
        """/.dockerenv alone is NOT enough — a foreign container has no
        booley-proxy, and forcing a dead proxy on it would break egress."""
        import os

        monkeypatch.delenv("BOOLEY_CONTAINER", raising=False)
        with patch.object(Path, "exists", lambda self: self.as_posix() == "/.dockerenv"):
            assert runtime_context.ensure_proxy_env() is False
        assert "HTTPS_PROXY" not in os.environ

    def test_existing_proxy_config_is_respected_wholesale(self, container_env, monkeypatch):
        """Any pre-set proxy var means the entry path DID deliver config —
        partially overwriting it could mix two proxy setups."""
        import os

        monkeypatch.setenv("https_proxy", "http://corp-proxy:3128")
        assert runtime_context.ensure_proxy_env() is False
        assert "HTTPS_PROXY" not in os.environ
        assert os.environ["https_proxy"] == "http://corp-proxy:3128"


# ===========================================================================
# agent_session_app()
# ===========================================================================


class TestAgentSessionApp:
    """Which agent CLI spawned us — the other half of "host agent, no Booley Flows"."""

    @pytest.fixture(autouse=True)
    def _no_inherited_markers(self, monkeypatch):
        """The suite may itself run from an agent shell; start from a clean slate."""
        for markers in dict(runtime_context._AGENT_SESSION_MARKERS).values():
            for marker in markers:
                monkeypatch.delenv(marker, raising=False)

    def test_none_in_a_plain_shell(self):
        assert runtime_context.agent_session_app() is None

    @pytest.mark.parametrize("marker", ["CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT"])
    def test_claude_markers(self, monkeypatch, marker):
        monkeypatch.setenv(marker, "1")
        assert runtime_context.agent_session_app() == "claude"

    @pytest.mark.parametrize("marker", ["CODEX_SANDBOX", "CODEX_HOME"])
    def test_codex_markers(self, monkeypatch, marker):
        monkeypatch.setenv(marker, "1")
        assert runtime_context.agent_session_app() == "codex"

    def test_empty_marker_does_not_count(self, monkeypatch):
        """An exported-but-empty var is not a session."""
        monkeypatch.setenv("CLAUDECODE", "")
        assert runtime_context.agent_session_app() is None


# ===========================================================================
# container_only_error()
# ===========================================================================


class TestContainerOnlyError:
    def test_none_inside_container(self, container_env):
        assert runtime_context.container_only_error("booley run") is None

    def test_message_on_host(self, host):
        msg = runtime_context.container_only_error("booley run")
        assert msg is not None
        assert msg.startswith("ERROR: `booley run` runs inside the Booley Session Runtime")

    def test_message_names_command_and_fix(self, host):
        """Refusal must name the offending command AND how to fix it."""
        msg = runtime_context.container_only_error("bwave")
        assert "`bwave`" in msg
        assert "Reopen in Container" in msg
        assert "docker exec" in msg


# ===========================================================================
# host_only_error()
# ===========================================================================


class TestHostOnlyError:
    def test_none_on_host(self, host):
        assert runtime_context.host_only_error("booley init") is None

    def test_message_inside_container(self, container_env):
        msg = runtime_context.host_only_error("booley init")
        assert msg is not None
        assert "`booley init`" in msg
        assert "HOST terminal" in msg  # the fix: run it outside the container
