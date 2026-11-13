"""Tests for harness.mcp_config — Codex/Claude MCP config generation."""

from __future__ import annotations

import os
import sys
import tempfile
import tomllib
from pathlib import Path
from types import ModuleType

import pytest

from booley.runtime.mcp_config import _warning_preference, generate_codex_config
from tests.conftest import tomlkit_copy
from tests.subprocess_budget import run_bounded_child

WARNING_KEY = "suppress_unstable_features_warning"
INSTALL_ERROR = r"installation.*tomlkit>=0\.15\.2.*TOML1\.1"
# Invalid user text or a bad root value must not matter when the install is broken.
USER_SOURCES = (" ", "broken=[", f"{WARNING_KEY}=false\n")


def _tomlkit_copy(monkeypatch, version):
    """Install a copy of the real tomlkit that reports ``version`` (parsing still works)."""
    monkeypatch.setitem(sys.modules, "tomlkit", tomlkit_copy(version))


def _break_tomlkit(monkeypatch, failure):
    """Make the editor unusable the way an incompatible installation would."""
    if failure == "missing":
        monkeypatch.setitem(sys.modules, "tomlkit", None)
    elif failure == "broken":
        monkeypatch.setitem(sys.modules, "tomlkit", ModuleType("tomlkit"))  # no __version__
    else:
        _tomlkit_copy(monkeypatch, "0.13.2")


@pytest.mark.parametrize("failure", ["missing", "broken", "too-old"])
def test_codex_parser_installation_failure_is_not_scratch_repair(monkeypatch, caplog, failure):
    _break_tomlkit(monkeypatch, failure)
    # Fresh generation never parses, so it still works.
    assert f"{WARNING_KEY} = true" in generate_codex_config()
    assert f"{WARNING_KEY} = true" in generate_codex_config(existing_config="")
    for source in USER_SOURCES:
        with pytest.raises(RuntimeError, match=INSTALL_ERROR) as error:
            generate_codex_config(existing_config=source)
        assert error.value.__cause__ is not None
    assert "malformed" not in caplog.text


@pytest.mark.parametrize("version", ["0.15.2", "0.16.0rc1", "1.0.0.dev3"])
def test_codex_editor_accepts_suffixed_versions(monkeypatch, version):
    _tomlkit_copy(monkeypatch, version)
    assert _warning_preference(f"{WARNING_KEY}=false\n") is False


def test_blocked_tomlkit_does_not_break_booley_import_graph_or_claude_route(tmp_path):
    script = """
import importlib.abc
import inspect
import sys
strict = True
class BlockTomlkit(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "tomlkit" or fullname.startswith("tomlkit."):
            importer = next(frame.frame.f_globals.get("__name__", "")
                            for frame in inspect.stack()[1:]
                            if not frame.frame.f_globals.get("__name__", "").startswith("importlib"))
            if strict and importer.startswith("booley."):
                raise AssertionError("Booley eagerly imported tomlkit: " + importer)
            raise ImportError("blocked tomlkit import")
sys.meta_path.insert(0, BlockTomlkit())
from booley.runtime import mcp_config, incontainer_setup
from booley.harness import incontainer_register
import booley.mcp.server
assert isinstance(mcp_config.http_port(), int)
for name in ("upsert_claude", "deploy_skills", "deploy_host_skills"):
    setattr(incontainer_setup, name, lambda *args: 0)
incontainer_setup.apply_stored_credential = lambda *args: "none"
incontainer_setup._apply_claude_permission_mode = lambda *args: "current"
incontainer_setup._apply_claude_first_run = lambda *args, **kwargs: "current"
assert incontainer_setup.register("claude").startswith("claude:current")
assert "tomlkit" not in sys.modules
strict = False  # the Codex route legitimately needs tomlkit and must fail clearly
try:
    mcp_config.generate_codex_config(existing_config="x = 1\\n")
except RuntimeError as error:
    assert "tomlkit>=0.15.2" in str(error)
else:
    raise AssertionError("Codex route accepted a missing tomlkit")
"""
    result = run_bounded_child(
        [sys.executable, "-c", script],
        env={**os.environ, "PYTHONPATH": os.pathsep.join(sys.path), "HOME": str(tmp_path)},
    )
    assert result.returncode == 0, result.stdout + result.stderr


def _private_codex_destination(tmp_path, monkeypatch, kind):
    from booley.runtime import _codex_backend as cb
    from booley.runtime.agent_backend import AgentCallParams
    from booley.runtime.text_only_agent import prepare_codex_text_only

    monkeypatch.setattr(cb, "_NESTED_HOMES", {})
    # Nested homes are created under the system temp directory.
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
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
        writer = cb._ensure_nested_codex_home

        def create():
            return Path(writer("test", ["lint"])) / ".codex"

    return create, create() / "config.toml"


@pytest.mark.parametrize("kind", ["nested", "text-only"])
@pytest.mark.parametrize("failure", ["missing", "broken", "too-old", "nonbool"])
def test_private_writer_invalid_or_incompatible_config_preserves_destination(
    tmp_path, monkeypatch, caplog, kind, failure
):
    from booley.runtime import _codex_backend as cb

    create, config = _private_codex_destination(tmp_path, monkeypatch, kind)
    value = b'"invalid"' if failure == "nonbool" else b"false"
    source = WARNING_KEY.encode() + b"=" + value + b"\nfeatures={\n mcp_2026_07_28=true,\n}\n"
    config.write_bytes(source)
    cb._NESTED_HOMES.clear()
    if failure == "nonbool":
        error_type, message = ValueError, "boolean"
    else:
        _break_tomlkit(monkeypatch, failure)
        error_type, message = RuntimeError, INSTALL_ERROR
    with pytest.raises(error_type, match=message):
        create()
    assert config.read_bytes() == source
    assert "malformed" not in caplog.text


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
        config = generate_codex_config(enabled_mcp_tools=["mutation", "reviewer"])
        assert "[mcp_servers.booley]" in config
        assert "enabled_mcp_tools" not in config
        assert 'BOOLEY_MCP_TOOLS = "mutation,reviewer"' in config

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


@pytest.mark.parametrize("value", ['"bad"', "1", "[]", "{}"])
@pytest.mark.parametrize("layout", ["", "other={\n x=true,\n}\n"])
def test_generator_rejects_invalid_explicit_preference(value, layout):
    with pytest.raises(ValueError, match=rf"{WARNING_KEY}.*boolean"):
        generate_codex_config(existing_config=f"{WARNING_KEY}={value}\n{layout}")


@pytest.mark.parametrize(
    "source",
    [
        f'{WARNING_KEY}="',
        "features={\n x=true,",
        "x={\n a=1,\n a=2,\n}",
        "features={\n x=???\n}",
    ],
)
def test_generator_heals_interrupted_scratch_config(source, caplog):
    result = tomllib.loads(generate_codex_config(existing_config=source))
    assert result[WARNING_KEY] is True
    assert "preference" in caplog.text and "malformed" in caplog.text


# Existing destination text -> expected root preference. TOML 1.1 forms included.
PREFERENCE_CASES = {
    "missing": ("", True),
    "root-false": (f"{WARNING_KEY} = false # choice\n", False),
    "root-true": (f"{WARNING_KEY} = true\n", True),
    "quoted-key": (f'"{WARNING_KEY}"=false\nfeatures = {{\n mcp_2026_07_28=true,\n}}\n', False),
    "decoy-comment": (f"# {WARNING_KEY} = false\n", True),
    "decoy-table-key": (f"[features]\n{WARNING_KEY}=false\n", True),
    "decoy-nested-key": (f"other = {{\n {WARNING_KEY}=false,\n}}\n", True),
}


@pytest.mark.parametrize("kind", ["nested", "text-only"])
@pytest.mark.parametrize("case", PREFERENCE_CASES)
def test_private_generators_keep_root_warning_preference(
    tmp_path, monkeypatch, caplog, kind, case
):
    from booley.runtime import _codex_backend as cb

    create, config = _private_codex_destination(tmp_path, monkeypatch, kind)
    source, expected = PREFERENCE_CASES[case]
    config.write_bytes(source.encode())
    cb._NESTED_HOMES.clear()
    create()
    generated = config.read_bytes()
    assert tomllib.loads(generated.decode())[WARNING_KEY] is expected
    # Regenerating from the result is a fixed point.
    cb._NESTED_HOMES.clear()
    create()
    assert config.read_bytes() == generated
    assert "malformed" not in caplog.text
