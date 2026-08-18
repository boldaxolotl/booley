"""Tests for harness.mcp_config — Codex/Claude MCP config generation."""

from __future__ import annotations

from booley.harness.mcp_config import generate_codex_config


class TestGenerateCodexConfig:
    def test_default_all_mcp_tools(self):
        config = generate_codex_config()
        assert "[mcp_servers.booley]" in config
        assert 'command = "python"' in config
        assert 'args = ["-m", "booley.mcp_server"]' in config
        assert "enabled_mcp_tools" not in config

    def test_baseline_env_always_present(self):
        """PATH and HOME must always be present so the MCP server can start."""
        config = generate_codex_config()
        assert "PATH = " in config
        assert 'HOME = "/home/agent"' in config
        assert "PYTHONUSERBASE = " in config

    def test_session_runtime_path_is_preserved(self, monkeypatch):
        """Image/project tools must remain visible after Codex replaces env."""
        runtime_path = "/opt/riscv/bin:/home/agent/.local/bin:/usr/local/bin:/usr/bin:/bin"
        monkeypatch.setenv("PATH", runtime_path)

        config = generate_codex_config()

        assert f'PATH = "{runtime_path}"' in config

    def test_empty_parent_path_uses_safe_baseline(self, monkeypatch):
        monkeypatch.setenv("PATH", "")

        config = generate_codex_config()

        assert 'PATH = "/home/agent/.local/bin:/usr/local/bin:/usr/bin:/bin"' in config

    def test_enabled_mcp_tools_exposed_via_env(self):
        config = generate_codex_config(enabled_mcp_tools=["tb_coder", "reviewer"])
        assert "[mcp_servers.booley]" in config
        assert "enabled_mcp_tools" not in config
        assert 'BOOLEY_MCP_TOOLS = "tb_coder,reviewer"' in config

    def test_empty_enabled_mcp_tools_exposes_empty_allowlist(self):
        config = generate_codex_config(enabled_mcp_tools=[])
        assert "[mcp_servers.booley]" in config
        assert "enabled_mcp_tools" not in config
        assert 'BOOLEY_MCP_TOOLS = ""' in config

    def test_extra_env_merged_with_baseline(self):
        config = generate_codex_config(extra_env={"BOOLEY_SLUG": "test"})
        assert 'BOOLEY_SLUG = "test"' in config
        # Baseline vars still present
        assert 'HOME = "/home/agent"' in config
        assert "PATH = " in config

    def test_extra_env_overrides_baseline(self):
        """Caller-provided env wins over baseline defaults."""
        config = generate_codex_config(extra_env={"HOME": "/custom"})
        assert 'HOME = "/custom"' in config

    def test_proxy_env_forwarded_when_set(self, monkeypatch):
        """The sandbox's egress proxy must survive Codex's env replacement,
        or every nested agent under the MCP server loses network access."""
        monkeypatch.setenv("HTTPS_PROXY", "http://booley-proxy:8080")
        monkeypatch.setenv("NO_PROXY", "localhost,127.0.0.1")
        config = generate_codex_config()
        assert 'HTTPS_PROXY = "http://booley-proxy:8080"' in config
        assert 'NO_PROXY = "localhost,127.0.0.1"' in config

    def test_proxy_env_absent_when_unset(self, monkeypatch):
        """Unset (or empty) forwarded vars must not emit empty entries."""
        for var in (
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "http_proxy",
            "https_proxy",
            "NO_PROXY",
            "no_proxy",
        ):
            monkeypatch.delenv(var, raising=False)
        config = generate_codex_config()
        assert "PROXY" not in config
        assert "proxy" not in config
