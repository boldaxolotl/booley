"""Tests for harness.mcp_config — Codex/Claude MCP config generation."""

from __future__ import annotations

import os
import subprocess
import sys
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest

from booley.runtime.mcp_config import generate_codex_config


@pytest.mark.parametrize("failure", ["missing", "stale", "broken", "wrong-result"])
def test_codex_parser_installation_failure_is_not_scratch_repair(monkeypatch, caplog, failure):
    from booley.runtime import mcp_config

    def loads(source):
        if failure == "broken":
            raise RuntimeError("broken parser")
        if failure == "wrong-result":
            return {}
        raise ValueError("TOML1.0-only parser rejected capability probe")

    parser = None if failure == "missing" else SimpleNamespace(loads=loads)
    mcp_config._codex_toml_parser.cache_clear()
    monkeypatch.setitem(sys.modules, "tomli", parser)
    try:
        assert "suppress_unstable_features_warning = true" in generate_codex_config()
        assert "suppress_unstable_features_warning = true" in generate_codex_config(
            existing_config=""
        )
        for source in (" ", "broken=[", "suppress_unstable_features_warning=false\n"):
            with pytest.raises(
                RuntimeError, match=r"installation.*tomli>=2\.4\.0.*TOML1\.1"
            ) as error:
                generate_codex_config(existing_config=source)
            assert error.value.__cause__ is not None
        assert "malformed" not in caplog.text
    finally:
        mcp_config._codex_toml_parser.cache_clear()


def test_codex_parser_success_is_cached_but_failures_are_retryable(monkeypatch):
    import tomli

    from booley.runtime import mcp_config

    mcp_config._codex_toml_parser.cache_clear()
    monkeypatch.setitem(sys.modules, "tomli", None)
    try:
        with pytest.raises(RuntimeError):
            mcp_config._codex_toml_parser()
        monkeypatch.setitem(sys.modules, "tomli", tomli)
        assert mcp_config._codex_toml_parser() is tomli
        monkeypatch.setitem(sys.modules, "tomli", None)
        assert mcp_config._codex_toml_parser() is tomli
    finally:
        mcp_config._codex_toml_parser.cache_clear()


def test_blocked_tomli_does_not_break_booley_import_graph_or_claude_route():
    script = """
import importlib.abc
import inspect
import sys
class BlockTomli(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "tomli" or fullname.startswith("tomli."):
            importer = next(frame.frame.f_globals.get("__name__", "")
                            for frame in inspect.stack()[1:]
                            if not frame.frame.f_globals.get("__name__", "").startswith("importlib"))
            if importer.startswith("booley."):
                raise AssertionError("Booley eagerly imported tomli: " + importer)
            raise ImportError("blocked third-party optional tomli import")
sys.meta_path.insert(0, BlockTomli())
from booley.runtime import mcp_config, incontainer_setup
from booley.harness import incontainer_register
import booley.mcp.server
assert isinstance(mcp_config.http_port(), int)
for name in ("upsert_claude", "deploy_skills", "deploy_host_skills"):
    setattr(incontainer_setup, name, lambda *args: 0)
incontainer_setup.apply_stored_credential = lambda *args: "none"
incontainer_setup._apply_claude_permission_mode = lambda *args: "current"
assert incontainer_setup.register("claude").startswith("claude:current")
assert "tomli" not in sys.modules
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
        env={**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)},
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("kind", ["developer", "nested", "text-only"])
@pytest.mark.parametrize("failure", ["missing", "stale", "broken-import"])
def test_private_writer_installation_failure_preserves_destination(
    tmp_path, monkeypatch, caplog, kind, failure
):
    import builtins

    from booley.runtime import _codex_backend as cb
    from booley.runtime import mcp_config
    from booley.runtime.agent_backend import AgentCallParams
    from booley.runtime.text_only_agent import prepare_codex_text_only

    monkeypatch.setattr(cb, "_NESTED_HOMES", {})
    monkeypatch.setattr(
        cb,
        "Path",
        lambda value: (
            tmp_path / Path(value).name if str(value).startswith("/tmp/codex-") else Path(value)
        ),
    )
    monkeypatch.setenv("HOME", str(tmp_path / "ambient"))
    if kind == "text-only":
        original = tmp_path / "original"
        original.mkdir()
        (original / "models_cache.json").write_text('{"models":[{"slug":"chosen"}]}')
        params = AgentCallParams(
            prompt="Explain",
            model="chosen",
            cwd=tmp_path,
            text_only=True,
            nested_mcp_tools=["lint"],
        )

        def create():
            _, env = prepare_codex_text_only(
                ["codex", "exec", "-"], params, {"CODEX_HOME": str(original)}
            )
            return Path(env["CODEX_HOME"])
    else:
        writer = (
            cb._ensure_developer_codex_home
            if kind == "developer"
            else cb._ensure_nested_codex_home
        )

        def create():
            return Path(writer("test", ["lint"])) / ".codex"

    config = create() / "config.toml"
    source = b"suppress_unstable_features_warning=false\nfeatures={\n mcp_2026_07_28=true,\n}\n"
    config.write_bytes(source)
    cb._NESTED_HOMES.clear()
    mcp_config._codex_toml_parser.cache_clear()
    real_import = builtins.__import__

    def import_parser(name, *args, **kwargs):
        if name == "tomli" and failure == "broken-import":
            raise ImportError("partial parser installation")
        return real_import(name, *args, **kwargs)

    def stale_loads(source):
        raise ValueError("TOML1.0-only capability failure")

    monkeypatch.setattr(builtins, "__import__", import_parser)
    monkeypatch.setitem(
        sys.modules, "tomli", None if failure == "missing" else SimpleNamespace(loads=stale_loads)
    )
    try:
        with pytest.raises(RuntimeError, match=r"installation.*tomli>=2\.4\.0"):
            create()
        assert config.read_bytes() == source
        assert "malformed" not in caplog.text
    finally:
        mcp_config._codex_toml_parser.cache_clear()


class TestGenerateCodexConfig:
    def test_default_all_mcp_tools(self):
        config = generate_codex_config()
        parsed = tomllib.loads(config)
        assert "[mcp_servers.booley]" in config
        assert 'command = "python"' in config
        assert 'args = ["-m", "booley.mcp.server"]' in config
        assert "enabled_mcp_tools" not in config
        assert parsed["features"]["mcp_2026_07_28"] is True
        assert parsed["suppress_unstable_features_warning"] is True
        assert parsed["mcp_servers"]["booley"]["env"]["CODEX_MCP_PROTOCOL_VERSION"] == "2026-07-28"

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


@pytest.mark.parametrize("value", [True, False])
def test_generator_preserves_root_warning_preference(value):
    existing = f'"suppress_unstable_features_warning" = {str(value).lower()} # choice\n'
    result = tomllib.loads(generate_codex_config(existing_config=existing))
    assert result["suppress_unstable_features_warning"] is value
    assert result["features"]["mcp_2026_07_28"] is True


def test_generator_ignores_table_warning_preference():
    result = generate_codex_config(
        existing_config="[features]\nsuppress_unstable_features_warning=false\n"
    )
    assert tomllib.loads(result)["suppress_unstable_features_warning"] is True


@pytest.mark.parametrize("value", ['"bad"', "1", "[]", "{}"])
def test_generator_rejects_invalid_explicit_preference(value):
    with pytest.raises(ValueError, match=r"suppress_unstable_features_warning.*boolean"):
        generate_codex_config(existing_config=f"suppress_unstable_features_warning={value}")


def test_generator_heals_interrupted_scratch_config(caplog):
    result = generate_codex_config(existing_config='suppress_unstable_features_warning="')
    assert tomllib.loads(result)["suppress_unstable_features_warning"] is True
    assert "preference" in caplog.text and "malformed" in caplog.text


@pytest.mark.parametrize("preference", [None, True, False])
@pytest.mark.parametrize("quoted", [False, True])
def test_toml11_generator_preserves_valid_preference(preference, quoted, caplog):
    key = (
        '"suppress_unstable_features_warning"' if quoted else "suppress_unstable_features_warning"
    )
    choice = "" if preference is None else f"{key}={str(preference).lower()}\n"
    source = choice + "features = {\n mcp_2026_07_28=true,\n}\n"
    result = tomllib.loads(generate_codex_config(existing_config=source))
    assert result["suppress_unstable_features_warning"] is (
        True if preference is None else preference
    )
    assert "malformed" not in caplog.text
    assert result["features"]["mcp_2026_07_28"] is True


def test_toml11_generator_nested_decoy_is_not_root(caplog):
    source = "other = {\n suppress_unstable_features_warning=false,\n}\n"
    result = tomllib.loads(generate_codex_config(existing_config=source))
    assert result["suppress_unstable_features_warning"] is True
    assert "malformed" not in caplog.text


@pytest.mark.parametrize("value", ['"bad"', "1", "[]", "{}"])
def test_toml11_generator_rejects_explicit_nonbool(value):
    source = f"suppress_unstable_features_warning={value}\nother={{\n x=true,\n}}\n"
    with pytest.raises(ValueError, match="boolean"):
        generate_codex_config(existing_config=source)


@pytest.mark.parametrize(
    "source", ["features={\n x=true,", "x={\n a=1,\n a=2,\n}", "features={\n x=???\n}"]
)
def test_toml11_generator_only_malformed_scratch_heals(source, caplog):
    assert (
        tomllib.loads(generate_codex_config(existing_config=source))[
            "suppress_unstable_features_warning"
        ]
        is True
    )
    assert "malformed" in caplog.text
