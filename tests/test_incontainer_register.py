"""Tests for the in-container MCP registrar (ADR 0018 WS4, ADR 0023 HTTP)."""

from __future__ import annotations

import builtins
import json
import os
import re
import sys
import tomllib
import types
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
import tomli

from booley.harness import incontainer_register as entry
from booley.mcp import (
    catalog,
)
from booley.runtime import incontainer_setup as reg
from tests.conftest import require_symlinks


@contextmanager
def _count_parses(monkeypatch, source):
    """Count every parse of exactly *source* by tomlkit (any path) or tomli."""
    import tomlkit.parser

    seen: list[str] = []
    real_init = tomlkit.parser.Parser.__init__

    def counted_init(self, string):
        seen.append(string)
        real_init(self, string)

    real_loads = tomli.loads

    def counted_loads(text, *args, **kwargs):
        seen.append(text)
        return real_loads(text, *args, **kwargs)

    monkeypatch.setattr(tomlkit.parser.Parser, "__init__", counted_init)
    monkeypatch.setattr(tomli, "loads", counted_loads)
    yield lambda: seen.count(source)


def _stub_codex_effects(monkeypatch):
    """Stub registration's side effects; return the list of calls they record."""
    calls: list[str] = []
    for name in ("deploy_skills", "deploy_host_skills", "apply_stored_credential"):
        monkeypatch.setattr(reg, name, lambda *args, name=name: calls.append(name) or 0)
    return calls


def _seed_codex(home, content):
    """Write *content* (str or bytes) as the Codex config, byte for byte."""
    path = reg.codex_config_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content if isinstance(content, bytes) else content.encode())
    return path


def _tomlkit_copy(version):
    """A working stand-in for tomlkit that reports *version*."""
    import tomlkit

    module = types.ModuleType("tomlkit")
    module.__dict__.update(tomlkit.__dict__)
    module.__version__ = version
    return module


@pytest.mark.parametrize("existing", [None, 'model="existing"\n', "broken=["])
@pytest.mark.parametrize("failure", ["missing", "broken-import", "too-old", "no-version"])
def test_codex_incompatible_editor_fails_before_any_write(
    tmp_path, monkeypatch, existing, failure
):
    calls = _stub_codex_effects(monkeypatch)
    publish = Mock(wraps=reg._publish_codex_config)
    monkeypatch.setattr(reg, "_publish_codex_config", publish)
    path = reg.codex_config_path(tmp_path)
    if existing is not None:
        _seed_codex(tmp_path, existing)
    real_import = builtins.__import__

    def import_editor(name, *args, **kwargs):
        if name == "tomlkit" and failure == "broken-import":
            raise ImportError("partial editor installation")
        return real_import(name, *args, **kwargs)

    editor = {
        "missing": None,
        "too-old": _tomlkit_copy("0.13.2"),
        "no-version": _tomlkit_copy("dev"),
    }.get(failure)
    monkeypatch.setattr(builtins, "__import__", import_editor)
    monkeypatch.setitem(sys.modules, "tomlkit", editor)

    with pytest.raises(RuntimeError, match=r"installation.*tomlkit>=0\.15\.2") as error:
        reg.register("codex", home=tmp_path)

    assert "repair this file" not in str(error.value)
    publish.assert_not_called()
    assert calls == []
    assert path.read_text() == existing if existing is not None else not path.exists()


def test_codex_registration_accepts_a_suffixed_editor_version(tmp_path, monkeypatch):
    _stub_codex_effects(monkeypatch)
    monkeypatch.setitem(sys.modules, "tomlkit", _tomlkit_copy("0.16.0rc1"))

    assert reg.register("codex", home=tmp_path).startswith("codex:written")


def test_main_launches_automatic_doctor_after_server(monkeypatch, capsys):
    events: list[str] = []
    monkeypatch.setenv("BOOLEY_AGENT_APP", "claude")
    monkeypatch.setattr(
        reg,
        "ensure_http_server",
        lambda *, mode: events.append(f"server:{mode}") or "started",
    )
    monkeypatch.setattr(reg, "register", lambda _app, *, trust_paths=(): "claude:current")
    monkeypatch.setattr(entry, "sandbox_trust_paths", lambda _start: ())
    monkeypatch.setattr(entry, "observe_upgrade", lambda: events.append("observe") or "current")
    monkeypatch.setattr(entry, "launch_auto_doctor", lambda: events.append("health") or "started")

    entry.main()

    assert (
        "server:started upgrade:current health:started claude:current" in capsys.readouterr().err
    )
    assert events == ["server:interactive", "observe", "health"]


# ===========================================================================
# Claude — ~/.claude.json mcpServers entry
# ===========================================================================


class TestClaude:
    def test_writes_entry(self, tmp_path):
        path = reg.claude_config_path(tmp_path)
        assert reg.upsert_claude(path) is True
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["mcpServers"]["booley"] == reg.desired_claude_entry()
        assert data["mcpServers"]["booley"] == {
            "type": "http",
            "url": reg.http_url(),
            "timeout": 7200000,
        }

    def test_idempotent(self, tmp_path):
        path = reg.claude_config_path(tmp_path)
        assert reg.upsert_claude(path) is True
        assert reg.upsert_claude(path) is False

    def test_migrates_stale_stdio_entry(self, tmp_path):
        # Pre-ADR-0023 registrations spawned a per-session stdio child that a
        # container resume never re-spawns; they must be rewritten to the URL.
        path = reg.claude_config_path(tmp_path)
        path.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "booley": {
                            "command": "python",
                            "args": ["-m", "booley.mcp.server"],
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        assert reg.upsert_claude(path) is True
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["mcpServers"]["booley"] == reg.desired_claude_entry()

    def test_preserves_other_servers(self, tmp_path):
        path = reg.claude_config_path(tmp_path)
        path.write_text(json.dumps({"mcpServers": {"other": {"command": "x"}}}), encoding="utf-8")
        reg.upsert_claude(path)
        data = json.loads(path.read_text(encoding="utf-8"))
        assert "other" in data["mcpServers"] and "booley" in data["mcpServers"]


# ===========================================================================
# ensure_http_server — start-if-absent for the loopback HTTP server
# ===========================================================================


class TestEnsureHttpServer:
    def test_already_running(self, monkeypatch):
        monkeypatch.setattr(reg, "_port_is_serving", lambda port, **kw: True)
        assert reg.ensure_http_server(mode="interactive") == "running"

    def test_explicit_interactive_mode_controls_spawned_server_surface(
        self, tmp_path, monkeypatch
    ):

        states = iter([False, True])
        monkeypatch.setattr(reg, "_port_is_serving", lambda port, **kw: next(states))
        monkeypatch.delenv("BOOLEY_MCP_MODE", raising=False)
        monkeypatch.setenv("BOOLEY_NESTED_AGENT", "1")
        monkeypatch.setenv("BOOLEY_NESTED_MCP_TOOLS", "submit_run_report")
        monkeypatch.setenv("BOOLEY_MCP_TOOLS", "submit_run_report")
        spawned = {}

        class FakeProc:
            def poll(self):
                return None

        def fake_popen(cmd, **kwargs):
            spawned["env"] = kwargs["env"]
            return FakeProc()

        monkeypatch.setattr(reg.subprocess, "Popen", fake_popen)

        assert (
            reg.ensure_http_server(
                mode="interactive",
                log_path=str(tmp_path / "server.log"),
            )
            == "started"
        )

        child_env = spawned["env"]
        assert child_env["BOOLEY_MCP_MODE"] == "interactive"
        assert "BOOLEY_NESTED_AGENT" not in child_env
        assert "BOOLEY_NESTED_MCP_TOOLS" not in child_env
        assert "BOOLEY_MCP_TOOLS" not in child_env

        with patch.dict(os.environ, child_env, clear=True):
            assert catalog.status_visible(catalog.Exposure.from_mapping(os.environ))

    def test_starts_and_waits_for_port(self, tmp_path, monkeypatch):
        # Port dead on the pre-check, alive once the (fake) server was spawned.
        states = iter([False, True])
        monkeypatch.setattr(
            reg,
            "_port_is_serving",
            lambda port, **kw: next(states),
        )

        spawned = {}

        class FakeProc:
            def poll(self):
                return None

        def fake_popen(cmd, **kwargs):
            spawned["cmd"] = cmd
            spawned["kwargs"] = kwargs
            return FakeProc()

        monkeypatch.setattr(reg.subprocess, "Popen", fake_popen)
        log = tmp_path / "server.log"
        assert reg.ensure_http_server(mode="interactive", log_path=str(log)) == "started"
        assert spawned["cmd"][-2:] == ["--transport", "http"]
        # Detached: must not die with the postStartCommand shell.
        assert spawned["kwargs"]["start_new_session"] is True

    def test_reports_early_death(self, tmp_path, monkeypatch):
        monkeypatch.setattr(reg, "_port_is_serving", lambda port, **kw: False)

        class DeadProc:
            returncode = 3

            def poll(self):
                return 3

        monkeypatch.setattr(
            reg.subprocess,
            "Popen",
            lambda cmd, **kw: DeadProc(),
        )
        log = tmp_path / "server.log"
        assert reg.ensure_http_server(mode="interactive", log_path=str(log)) == "failed"

    def test_spawn_oserror_is_failed_not_raised(self, tmp_path, monkeypatch):
        monkeypatch.setattr(reg, "_port_is_serving", lambda port, **kw: False)

        def boom(cmd, **kw):
            raise OSError("no exec")

        monkeypatch.setattr(reg.subprocess, "Popen", boom)
        log = tmp_path / "server.log"
        assert reg.ensure_http_server(mode="interactive", log_path=str(log)) == "failed"


# ===========================================================================
# Skill deployment into the per-app skills dir
# ===========================================================================


class TestSkills:
    @staticmethod
    def _fake_skills(root, names):
        """Create a packaged-skills layout under *root* and return the dir."""
        src = root / "data" / "skills"
        for name in names:
            (src / name).mkdir(parents=True)
            (src / name / "SKILL.md").write_text("---\nname: x\n---\n", encoding="utf-8")
        return src

    @staticmethod
    def _patch_src(monkeypatch, src):
        from booley.runtime import paths

        monkeypatch.setattr(paths, "skills_dir", lambda: src)

    def test_claude_dir(self, tmp_path):
        assert reg.skills_target_dir("claude", tmp_path) == tmp_path / ".claude" / "skills"

    def test_codex_dir(self, tmp_path):
        # Codex reads the generic cross-agent ~/.agents/skills (host Step 8 model).
        assert reg.skills_target_dir("codex", tmp_path) == tmp_path / ".agents" / "skills"

    def test_unknown_app_dir_is_none(self, tmp_path):
        assert reg.skills_target_dir("none", tmp_path) is None

    def test_links_all_skills(self, tmp_path, monkeypatch):
        require_symlinks(tmp_path)
        src = self._fake_skills(tmp_path, ["booley-a", "booley-b"])
        self._patch_src(monkeypatch, src)

        assert reg.deploy_skills("claude", tmp_path) == 2
        target = reg.skills_target_dir("claude", tmp_path)
        assert (target / "booley-a" / "SKILL.md").is_file()
        assert (target / "booley-b" / "SKILL.md").is_file()

    def test_codex_links_to_agents_dir(self, tmp_path, monkeypatch):
        require_symlinks(tmp_path)
        src = self._fake_skills(tmp_path, ["booley-a"])
        self._patch_src(monkeypatch, src)

        assert reg.deploy_skills("codex", tmp_path) == 1
        assert (tmp_path / ".agents" / "skills" / "booley-a" / "SKILL.md").is_file()

    def test_idempotent(self, tmp_path, monkeypatch):
        require_symlinks(tmp_path)
        src = self._fake_skills(tmp_path, ["booley-a"])
        self._patch_src(monkeypatch, src)

        assert reg.deploy_skills("claude", tmp_path) == 1
        assert reg.deploy_skills("claude", tmp_path) == 0  # already linked

    def test_skips_non_skill_dirs(self, tmp_path, monkeypatch):
        require_symlinks(tmp_path)
        src = self._fake_skills(tmp_path, ["booley-a"])
        (src / "not-a-skill").mkdir()  # no SKILL.md
        self._patch_src(monkeypatch, src)

        assert reg.deploy_skills("claude", tmp_path) == 1
        assert not (reg.skills_target_dir("claude", tmp_path) / "not-a-skill").exists()

    def test_unknown_app_is_noop(self, tmp_path, monkeypatch):
        src = self._fake_skills(tmp_path, ["booley-a"])
        self._patch_src(monkeypatch, src)
        assert reg.deploy_skills("none", tmp_path) == 0

    def test_missing_source_is_noop(self, tmp_path, monkeypatch):
        self._patch_src(monkeypatch, tmp_path / "nope")
        assert reg.deploy_skills("claude", tmp_path) == 0

    def test_prunes_dangling_links(self, tmp_path, monkeypatch):
        # Skills dir persists across rebuilds; a removed skill leaves a dead
        # link that must be pruned so the agent isn't offered a broken skill.
        require_symlinks(tmp_path)
        src = self._fake_skills(tmp_path, ["booley-a"])
        self._patch_src(monkeypatch, src)
        target = reg.skills_target_dir("claude", tmp_path)
        target.mkdir(parents=True)
        (target / "booley-gone").symlink_to(tmp_path / "removed-skill")  # dangling

        reg.deploy_skills("claude", tmp_path)
        assert not (target / "booley-gone").is_symlink()
        assert (target / "booley-a" / "SKILL.md").is_file()

    def test_link_failure_skips_only_that_skill(self, tmp_path, monkeypatch):
        require_symlinks(tmp_path)
        src = self._fake_skills(tmp_path, ["booley-a", "booley-b"])
        self._patch_src(monkeypatch, src)
        real_symlink_to = Path.symlink_to

        def failing_first(self, target, target_is_directory=False):
            if self.name == "booley-a":
                raise OSError("read-only file system")
            real_symlink_to(self, target, target_is_directory)

        monkeypatch.setattr(Path, "symlink_to", failing_first)

        assert reg.deploy_skills("claude", tmp_path) == 1
        target = reg.skills_target_dir("claude", tmp_path)
        assert not (target / "booley-a").exists()
        assert (target / "booley-b" / "SKILL.md").is_file()


class TestHostSkills:
    """deploy_host_skills — link the user's mounted host skills into the app dir."""

    @staticmethod
    def _fake_sidecar(home, names):
        """Create the mounted host-skills sidecar layout under *home*."""
        sidecar = home / reg._HOST_SKILLS_SIDECAR
        for name in names:
            (sidecar / name).mkdir(parents=True)
            (sidecar / name / "SKILL.md").write_text("---\nname: x\n---\n", encoding="utf-8")
        return sidecar

    def test_links_host_skills(self, tmp_path):
        require_symlinks(tmp_path)
        self._fake_sidecar(tmp_path, ["deslop", "grill-me"])

        assert reg.deploy_host_skills("claude", tmp_path) == 2
        target = reg.skills_target_dir("claude", tmp_path)
        assert (target / "deslop" / "SKILL.md").is_file()
        assert (target / "grill-me" / "SKILL.md").is_file()

    def test_codex_links_to_agents_dir(self, tmp_path):
        require_symlinks(tmp_path)
        self._fake_sidecar(tmp_path, ["deslop"])

        assert reg.deploy_host_skills("codex", tmp_path) == 1
        assert (tmp_path / ".agents" / "skills" / "deslop" / "SKILL.md").is_file()

    def test_builtin_wins_name_clash(self, tmp_path):
        # A built-in of the same name is deployed first; deploy_host_skills must
        # leave it alone so the in-image copy wins (exists() skip).
        require_symlinks(tmp_path)
        self._fake_sidecar(tmp_path, ["shared"])
        target = reg.skills_target_dir("claude", tmp_path)
        target.mkdir(parents=True)
        builtin = tmp_path / "builtin" / "shared"
        builtin.mkdir(parents=True)
        (builtin / "SKILL.md").write_text("builtin", encoding="utf-8")
        (target / "shared").symlink_to(builtin)  # built-in already linked

        assert reg.deploy_host_skills("claude", tmp_path) == 0
        assert (target / "shared").resolve() == builtin.resolve()

    def test_idempotent(self, tmp_path):
        require_symlinks(tmp_path)
        self._fake_sidecar(tmp_path, ["deslop"])
        assert reg.deploy_host_skills("claude", tmp_path) == 1
        assert reg.deploy_host_skills("claude", tmp_path) == 0

    def test_skips_non_skill_dirs(self, tmp_path):
        require_symlinks(tmp_path)
        sidecar = self._fake_sidecar(tmp_path, ["deslop"])
        (sidecar / "not-a-skill").mkdir()  # no SKILL.md
        assert reg.deploy_host_skills("claude", tmp_path) == 1
        assert not (reg.skills_target_dir("claude", tmp_path) / "not-a-skill").exists()

    def test_no_sidecar_is_noop(self, tmp_path):
        assert reg.deploy_host_skills("claude", tmp_path) == 0

    def test_unknown_app_is_noop(self, tmp_path):
        self._fake_sidecar(tmp_path, ["deslop"])
        assert reg.deploy_host_skills("none", tmp_path) == 0

    def test_prunes_link_of_unmounted_host_skill(self, tmp_path):
        # mount_host_skills turned off / skill removed -> its bind is gone, so the
        # dangling link must be pruned (the sidecar child no longer exists).
        require_symlinks(tmp_path)
        self._fake_sidecar(tmp_path, ["deslop"])
        target = reg.skills_target_dir("claude", tmp_path)
        target.mkdir(parents=True)
        (target / "gone").symlink_to(tmp_path / reg._HOST_SKILLS_SIDECAR / "gone")  # dangling

        reg.deploy_host_skills("claude", tmp_path)
        assert not (target / "gone").is_symlink()
        assert (target / "deslop" / "SKILL.md").is_file()


# ===========================================================================
# apply_stored_credential — the `booley auth` sidecar seed
# ===========================================================================


class TestApplyStoredCredential:
    """Container-side application of the rotation-free credential.

    This is the delivery path that reaches VS Code's "Reopen in Container",
    where the spec's ${localEnv:...} reference resolves empty.
    """

    @staticmethod
    def _clear_env(monkeypatch):
        monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    @staticmethod
    def _seed(tmp_path, app, value):
        from booley.runtime.auth_token import TOKEN_SEED_BASENAME

        (tmp_path / TOKEN_SEED_BASENAME[app]).write_text(value + "\n", encoding="utf-8")

    # --- Claude: settings.json env ---

    def test_claude_seed_written_to_settings_env(self, tmp_path, monkeypatch):
        self._clear_env(monkeypatch)
        self._seed(tmp_path, "claude", "sk-ant-oat01-stored")

        assert reg.apply_stored_credential("claude", tmp_path) == "written"
        settings = json.loads(reg.claude_settings_path(tmp_path).read_text())
        assert settings["env"]["CLAUDE_CODE_OAUTH_TOKEN"] == "sk-ant-oat01-stored"
        # The file now holds a secret — owner-only, like the host store.
        if os.name != "nt":
            assert reg.claude_settings_path(tmp_path).stat().st_mode & 0o077 == 0

    def test_claude_idempotent(self, tmp_path, monkeypatch):
        self._clear_env(monkeypatch)
        self._seed(tmp_path, "claude", "sk-ant-oat01-stored")
        assert reg.apply_stored_credential("claude", tmp_path) == "written"
        assert reg.apply_stored_credential("claude", tmp_path) == "current"

    def test_claude_nonempty_ambient_env_wins_over_seed(self, tmp_path, monkeypatch):
        # The export escape hatch: an explicitly exported value overrides the
        # store. Claude Code applies settings env ON TOP of the process env, so
        # the exported value must be the one written.
        self._clear_env(monkeypatch)
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-exported")
        self._seed(tmp_path, "claude", "sk-ant-oat01-stored")

        reg.apply_stored_credential("claude", tmp_path)
        settings = json.loads(reg.claude_settings_path(tmp_path).read_text())
        assert settings["env"]["CLAUDE_CODE_OAUTH_TOKEN"] == "sk-ant-oat01-exported"

    def test_claude_empty_ambient_env_is_absent(self, tmp_path, monkeypatch):
        # VS Code resolves an absent ${localEnv:...} to "" — that must fall
        # through to the seed, matching the CLI's own truthiness handling.
        self._clear_env(monkeypatch)
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "")
        self._seed(tmp_path, "claude", "sk-ant-oat01-stored")

        reg.apply_stored_credential("claude", tmp_path)
        settings = json.loads(reg.claude_settings_path(tmp_path).read_text())
        assert settings["env"]["CLAUDE_CODE_OAUTH_TOKEN"] == "sk-ant-oat01-stored"

    def test_claude_preserves_other_settings(self, tmp_path, monkeypatch):
        self._clear_env(monkeypatch)
        self._seed(tmp_path, "claude", "sk-ant-oat01-stored")
        path = reg.claude_settings_path(tmp_path)
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"model": "opus", "env": {"FOO": "bar"}}))

        reg.apply_stored_credential("claude", tmp_path)
        settings = json.loads(path.read_text())
        assert settings["model"] == "opus"
        assert settings["env"]["FOO"] == "bar"
        assert settings["env"]["CLAUDE_CODE_OAUTH_TOKEN"] == "sk-ant-oat01-stored"

    def test_claude_no_credential_no_entry_is_noop(self, tmp_path, monkeypatch):
        self._clear_env(monkeypatch)
        assert reg.apply_stored_credential("claude", tmp_path) == "none"
        assert not reg.claude_settings_path(tmp_path).exists()

    def test_claude_stale_entry_cleared_when_credential_gone(self, tmp_path, monkeypatch):
        # `booley auth --clear` + rebuild: settings.json lives on the PERSISTENT
        # state volume, so without cleanup the dead token would override the
        # freshly seeded subscription credentials forever.
        self._clear_env(monkeypatch)
        path = reg.claude_settings_path(tmp_path)
        path.parent.mkdir(parents=True)
        path.write_text(
            json.dumps({"env": {"CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-dead", "FOO": "bar"}})
        )

        assert reg.apply_stored_credential("claude", tmp_path) == "cleared"
        settings = json.loads(path.read_text())
        assert "CLAUDE_CODE_OAUTH_TOKEN" not in settings.get("env", {})
        assert settings["env"]["FOO"] == "bar"  # only our entry is managed

    # --- Codex: auth.json ---

    def test_codex_seed_written_to_auth_json(self, tmp_path, monkeypatch):
        self._clear_env(monkeypatch)
        self._seed(tmp_path, "codex", "sk-proj-stored")

        assert reg.apply_stored_credential("codex", tmp_path) == "written"
        auth = json.loads(reg.codex_auth_path(tmp_path).read_text())
        assert auth == {"OPENAI_API_KEY": "sk-proj-stored"}
        if os.name != "nt":
            assert reg.codex_auth_path(tmp_path).stat().st_mode & 0o077 == 0

    def test_codex_idempotent(self, tmp_path, monkeypatch):
        self._clear_env(monkeypatch)
        self._seed(tmp_path, "codex", "sk-proj-stored")
        assert reg.apply_stored_credential("codex", tmp_path) == "written"
        assert reg.apply_stored_credential("codex", tmp_path) == "current"

    def test_codex_key_wins_over_seeded_subscription_login(self, tmp_path, monkeypatch):
        # Runs after the postStart creds-seed cp in the same hook chain: a
        # stored key deliberately replaces the refreshing subscription login.
        self._clear_env(monkeypatch)
        self._seed(tmp_path, "codex", "sk-proj-stored")
        path = reg.codex_auth_path(tmp_path)
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"tokens": {"access_token": "x"}, "last_refresh": "y"}))

        reg.apply_stored_credential("codex", tmp_path)
        assert json.loads(path.read_text()) == {"OPENAI_API_KEY": "sk-proj-stored"}

    def test_codex_nonempty_ambient_env_wins_over_seed(self, tmp_path, monkeypatch):
        self._clear_env(monkeypatch)
        monkeypatch.setenv("OPENAI_API_KEY", "sk-proj-exported")
        self._seed(tmp_path, "codex", "sk-proj-stored")

        reg.apply_stored_credential("codex", tmp_path)
        auth = json.loads(reg.codex_auth_path(tmp_path).read_text())
        assert auth == {"OPENAI_API_KEY": "sk-proj-exported"}

    def test_codex_clears_only_booley_shaped_auth(self, tmp_path, monkeypatch):
        # Removal is shape-checked: the exact single-key file Booley writes is
        # cleaned up after `booley auth --clear`; a user's own login is not.
        self._clear_env(monkeypatch)
        path = reg.codex_auth_path(tmp_path)
        path.parent.mkdir(parents=True)

        path.write_text(json.dumps({"OPENAI_API_KEY": "sk-proj-dead"}))
        assert reg.apply_stored_credential("codex", tmp_path) == "cleared"
        assert not path.exists()

        path.write_text(json.dumps({"OPENAI_API_KEY": "k", "tokens": None}))
        assert reg.apply_stored_credential("codex", tmp_path) == "none"
        assert path.exists()  # not Booley's shape — left alone

    # --- dispatch ---

    def test_unknown_app_is_noop(self, tmp_path, monkeypatch):
        self._clear_env(monkeypatch)
        assert reg.apply_stored_credential("none", tmp_path) == "none"
        assert not list(tmp_path.iterdir())

    def test_register_reports_credential_status(self, tmp_path, monkeypatch):
        self._clear_env(monkeypatch)
        self._seed(tmp_path, "claude", "sk-ant-oat01-stored")
        assert "cred:written" in reg.register("claude", home=tmp_path)
        assert "cred:current" in reg.register("claude", home=tmp_path)

    def test_register_pins_permission_mode_after_the_credential(self, tmp_path, monkeypatch):
        # Both writers rewrite settings.json wholesale — running them in the
        # wrong order would drop whichever wrote first.
        self._clear_env(monkeypatch)
        self._seed(tmp_path, "claude", "sk-ant-oat01-stored")

        assert "perm:written" in reg.register("claude", home=tmp_path)
        settings = json.loads(reg.claude_settings_path(tmp_path).read_text())
        assert settings["env"]["CLAUDE_CODE_OAUTH_TOKEN"] == "sk-ant-oat01-stored"
        assert settings["permissions"]["defaultMode"] == "bypassPermissions"
        assert "perm:current" in reg.register("claude", home=tmp_path)


# ===========================================================================
# Permission mode (settings.json)
# ===========================================================================


class TestClaudePermissionMode:
    """The container IS the sandbox, so sessions launch in bypassPermissions.

    Claude Code only makes that mode selectable when the session was launched
    in it, so pinning it in settings.json is the only lever that reaches both
    doors (VS Code "Reopen in Container" and `booley session enter`).
    """

    def test_writes_bypass_mode_and_skips_disclaimer(self, tmp_path):
        assert reg._apply_claude_permission_mode(tmp_path) == "written"
        settings = json.loads(reg.claude_settings_path(tmp_path).read_text())
        assert settings["permissions"]["defaultMode"] == "bypassPermissions"
        assert settings["permissions"]["deny"] == ["WebFetch", "WebSearch"]
        # Without this the one-time disclaimer blocks a fresh state volume.
        assert settings["skipDangerousModePermissionPrompt"] is True

    def test_idempotent(self, tmp_path):
        assert reg._apply_claude_permission_mode(tmp_path) == "written"
        assert reg._apply_claude_permission_mode(tmp_path) == "current"

    def test_owner_only_mode(self, tmp_path):
        # Shares a file with the OAuth token, so it must not widen the mode.
        reg._apply_claude_permission_mode(tmp_path)
        if os.name != "nt":
            assert reg.claude_settings_path(tmp_path).stat().st_mode & 0o077 == 0

    def test_preserves_credential_and_permission_rules(self, tmp_path):
        path = reg.claude_settings_path(tmp_path)
        path.parent.mkdir(parents=True)
        path.write_text(
            json.dumps(
                {
                    "model": "opus",
                    "env": {"CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-stored"},
                    "permissions": {"deny": ["Bash(rm -rf /)"]},
                }
            )
        )

        assert reg._apply_claude_permission_mode(tmp_path) == "written"
        settings = json.loads(path.read_text())
        assert settings["model"] == "opus"
        assert settings["env"]["CLAUDE_CODE_OAUTH_TOKEN"] == "sk-ant-oat01-stored"
        assert settings["permissions"]["deny"] == [
            "Bash(rm -rf /)",
            "WebFetch",
            "WebSearch",
        ]
        assert settings["permissions"]["defaultMode"] == "bypassPermissions"

    def test_reasserted_over_a_downgrade(self, tmp_path):
        # The state volume persists; a mode left behind by an older Booley (or
        # hand-edited) must be pulled back on the next container start.
        path = reg.claude_settings_path(tmp_path)
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"permissions": {"defaultMode": "acceptEdits"}}))

        assert reg._apply_claude_permission_mode(tmp_path) == "written"
        settings = json.loads(path.read_text())
        assert settings["permissions"]["defaultMode"] == "bypassPermissions"

    def test_only_claude(self, tmp_path):
        reg.register("codex", home=tmp_path)
        assert not reg.claude_settings_path(tmp_path).exists()


# ===========================================================================
# register() dispatch
# ===========================================================================


class TestRegister:
    def test_claude(self, tmp_path):
        # Return string now carries both the MCP-write state and skill count.
        assert reg.register("claude", home=tmp_path).startswith("claude:written")
        assert reg.register("claude", home=tmp_path).startswith("claude:current")

    def test_codex(self, tmp_path):
        first = reg.register("codex", home=tmp_path)
        second = reg.register("codex", home=tmp_path)
        assert first.startswith("codex:written")
        assert "perm:written" in first
        assert "perm:current" in second

    def test_none_is_noop(self, tmp_path):
        assert reg.register("none", home=tmp_path) == "none"
        assert not list(tmp_path.iterdir())

    def test_main_uses_env(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("BOOLEY_AGENT_APP", "claude")
        # Registration must not depend on the real server spawn in tests.
        requested_modes = []
        monkeypatch.setattr(
            reg,
            "ensure_http_server",
            lambda *, mode: requested_modes.append(mode) or "running",
        )
        monkeypatch.setattr(entry, "launch_auto_doctor", lambda: "current")
        monkeypatch.setattr(entry, "observe_upgrade", lambda: "current")
        # Under pytest the cwd is Booley's own source checkout, not a Project.
        monkeypatch.setattr(entry, "sandbox_trust_paths", lambda _start: ())
        entry.main()
        assert reg.claude_config_path(tmp_path).exists()
        assert requested_modes == ["interactive"]

    @pytest.mark.parametrize("app", ["none", "", "gemini"])
    def test_main_skips_server_without_app(self, tmp_path, monkeypatch, app):
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("BOOLEY_AGENT_APP", app)

        def fail(**kw):
            raise AssertionError("must not start a server with no client app")

        monkeypatch.setattr(reg, "ensure_http_server", fail)
        monkeypatch.setattr(entry, "launch_auto_doctor", lambda: "current")
        monkeypatch.setattr(entry, "observe_upgrade", lambda: "current")
        entry.main()
        assert not list(tmp_path.iterdir())


# ===========================================================================
# Codex registration — one document step for MCP and permissions
# ===========================================================================

_FORM_TEMPLATES = {
    "dotted": "TBL.other=true\nTBL.KEY=false\n",
    "section": '["TBL"] # keep header\nother=true\n"KEY"=false # keep choice\n',
    "split": "[TBL]\nother=true\n# about other\n[other]\nx=1\n[TBL.sub]\ny=2\n",
    "inline": 'TBL = {other = {text = "a,b=}", values = [1, 2]}, KEY = false} # keep header\n',
    "inline-toml11": 'TBL = {\n # before\n other = {text="{a,b}", values=[1,2]}, # between\n}\n',
}
_CODEX_FORMS = {
    "absent": (None, ()),
    "empty": ("", ()),
    "stale-stdio-table": (
        '# my comment\n[user]\nname = "a"\n\n[mcp_servers.booley]\ncommand = "python"\n'
        'args = ["-m", "booley.mcp.server"]\n\n[other]\nkey = 1\n',
        ("[user]", "[other]"),
    ),
    "stale-quoted-header-no-final-newline": (
        '["mcp_servers"."booley"]\ncommand="old"\n["other"] # retain header\nx=1',
        (),
    ),
    "stale-with-env-subtable": (
        '[mcp_servers.booley]\ncommand="old"\n[mcp_servers.booley.env]\nA="b"\n[after]\nz=1\n',
        (),
    ),
    "stale-dotted-root": (
        'mcp_servers.booley.command="old"\nmcp_servers.other.url="http://other"\n',
        (),
    ),
    "stale-dotted-in-section": (
        '[mcp_servers]\nbooley.command="old"\nother.url="http://other"\n',
        (),
    ),
    "old-current-entry": ("features.mcp_2026_07_28=true\nSECTION", ("SECTION",)),
    "preference-true": ('"suppress_unstable_features_warning" = true # keep\n', ()),
    "preference-false": ('"suppress_unstable_features_warning" = false # keep\n', ()),
    "preference-decoy-table": (
        "[features] # flags\nsuppress_unstable_features_warning=false\n",
        (),
    ),
    "preference-decoy-comment": ("# suppress_unstable_features_warning = false\n", ()),
    "comment-introducing-next-table": (
        "a = 1\n\n# about other\n[other]\nx = 1\n",
        ("# about other\n[other]",),
    ),
    "unrelated-array-and-string": (
        "items = [\n[1, 2],\n[3]\n]\n# retain array\n"
        'description = """\n[features]\nmcp_2026_07_28 = false\n"""\n',
        ("items = [\n[1, 2],\n[3]\n]\n",),
    ),
    "unicode-line-separators": (
        '# keep\u2028approval_policy="on-request"\nmodel="a"\n# keep\u2029web_search="x"\n'
        '# keep\u0085sandbox_mode="y"\n',
        (),
    ),
    "downgraded-permissions": (
        'approval_policy="on-request"\nsandbox_mode="workspace-write"\n'
        "[notice]\nhide_full_access_warning=false\nhide_rate_limit_model_nudge = true\n",
        (),
    ),
    "crlf": ('# retain CRLF\r\nmodel="existing"\r\n', ('# retain CRLF\r\nmodel="existing"\r\n',)),
    "crlf-no-final-newline": ('model="existing"\r\n# tail', ('model="existing"\r\n# tail',)),
    "no-final-newline": ('model="existing"', ()),
}
for _table, _key in (("features", "mcp_2026_07_28"), ("notice", "hide_full_access_warning")):
    for _name, _template in _FORM_TEMPLATES.items():
        _CODEX_FORMS[f"{_table}-{_name}"] = (
            _template.replace("TBL", _table).replace("KEY", _key),
            ("# about other\n[other]",) if _name == "split" else (),
        )


def _registered(original):
    """The parsed config a registration must produce from the parsed *original*."""
    servers = {k: v for k, v in original.get("mcp_servers", {}).items() if k != "booley"}
    return {
        **original,
        "suppress_unstable_features_warning": original.get(
            "suppress_unstable_features_warning", True
        ),
        "approval_policy": "never",
        "sandbox_mode": "danger-full-access",
        "web_search": "disabled",
        "features": {**original.get("features", {}), "mcp_2026_07_28": True},
        "notice": {**original.get("notice", {}), "hide_full_access_warning": True},
        "mcp_servers": {
            **servers,
            "booley": {"url": reg.http_url(), "tool_timeout_sec": 7200},
        },
    }


@pytest.mark.parametrize(("source", "keeps"), _CODEX_FORMS.values(), ids=_CODEX_FORMS)
def test_codex_registration_forms(tmp_path, monkeypatch, source, keeps):
    _stub_codex_effects(monkeypatch)
    path = reg.codex_config_path(tmp_path)
    text = (source or "").replace("SECTION", reg.codex_section())
    if source is not None:
        _seed_codex(tmp_path, text)

    assert reg.register("codex", home=tmp_path).startswith("codex:written")

    body = path.read_bytes().decode()
    assert tomli.loads(body) == _registered(tomli.loads(text))
    for kept in (
        *re.findall(r"#[^\n]*", text),
        *(k.replace("SECTION", reg.codex_section()) for k in keeps),
    ):
        assert kept in body
    if source is None and os.name != "nt":
        assert path.stat().st_mode & 0o777 == 0o600
    assert "codex:current" in reg.register("codex", home=tmp_path)
    assert path.read_bytes().decode() == body


def test_codex_current_config_is_not_republished(tmp_path, monkeypatch):
    _stub_codex_effects(monkeypatch)
    reg.register("codex", home=tmp_path)
    path = reg.codex_config_path(tmp_path)
    before = (path.read_bytes(), path.stat().st_mtime_ns)
    publish = Mock(side_effect=AssertionError("current config must not be published"))
    monkeypatch.setattr(reg, "_publish_codex_config", publish)

    assert "codex:current" in reg.register("codex", home=tmp_path)

    assert (path.read_bytes(), path.stat().st_mtime_ns) == before


def test_codex_registration_parses_config_once(tmp_path, monkeypatch):
    source = 'model="existing"\nnotice.other=true\nother={time=12:34, escaped="\\e\\x41"}\n'
    _seed_codex(tmp_path, source)
    seen = []
    with _count_parses(monkeypatch, source) as parses:
        _stub_codex_effects(monkeypatch)
        monkeypatch.setattr(reg, "deploy_skills", lambda *args: seen.append(parses()) or 0)
        reg.register("codex", home=tmp_path)
    assert seen == [1], "the config was parsed more than once before the first effect"
    parsed = tomli.loads(reg.codex_config_path(tmp_path).read_text())
    assert parsed["notice"] == {"other": True, "hide_full_access_warning": True}
    assert parsed["other"] == tomli.loads(source)["other"]


@pytest.mark.parametrize("failing_publication", [0, 1])
def test_codex_publication_failure_preserves_destination(
    tmp_path, monkeypatch, failing_publication
):
    _stub_codex_effects(monkeypatch)
    existing = 'model="existing"\n'
    path = _seed_codex(tmp_path, existing)
    path.chmod(0o640)
    replaced = []
    real_replace = type(path).replace

    def replace(source, destination):
        assert destination == path
        if len(replaced) == failing_publication:
            raise OSError("interrupted publication")
        replaced.append(destination)
        real_replace(source, destination)

    monkeypatch.setattr(type(path), "replace", replace)
    with pytest.raises(OSError, match="interrupted publication"):
        reg.register("codex", home=tmp_path)

    parsed = tomllib.loads(path.read_text(encoding="utf-8"))
    assert parsed["model"] == "existing"
    assert ("approval_policy" in parsed) is False
    assert ("mcp_servers" in parsed) is bool(failing_publication)
    if os.name != "nt":
        assert path.stat().st_mode & 0o777 == 0o640
    assert list(path.parent.iterdir()) == [path]


def test_codex_atomic_publication_preserves_symlink_and_permissions(tmp_path):
    require_symlinks(tmp_path)
    path = reg.codex_config_path(tmp_path)
    path.parent.mkdir(parents=True)
    target = tmp_path / "shared-config.toml"
    target.write_text('model="existing"\n', encoding="utf-8")
    target.chmod(0o640)
    path.symlink_to(target)

    reg.register("codex", home=tmp_path)

    parsed = tomllib.loads(target.read_text())
    assert (parsed["suppress_unstable_features_warning"], parsed["approval_policy"]) == (
        True,
        "never",
    )
    assert parsed["model"] == "existing"
    assert path.is_symlink()
    if os.name != "nt":
        assert target.stat().st_mode & 0o777 == 0o640


@pytest.mark.parametrize(
    "existing",
    [
        'bad = "',
        'suppress_unstable_features_warning="bad"',
        "features=true\n",
        "notice=true\n",
        'notice="x"\n',
        "[notice]\nx=1\nx=2\n",
        "mcp_servers = 1\n",
        'mcp_servers = {other = {url="http://other"}}\n',
        'mcp_servers = {booley = {command="old"}, other = {url="http://other"}}\n',
    ],
)
def test_codex_invalid_config_is_refused_untouched(tmp_path, monkeypatch, existing):
    calls = _stub_codex_effects(monkeypatch)
    path = _seed_codex(tmp_path, existing)
    for _ in range(2):
        with pytest.raises(ValueError, match=re.escape(str(path))) as error:
            reg.register("codex", home=tmp_path)
        assert "repair this file and retry" in str(error.value)
        assert path.read_text() == existing
    assert calls == []


def test_codex_unsupported_inline_servers_fail_with_named_repair_diagnostic(tmp_path, monkeypatch):
    _stub_codex_effects(monkeypatch)
    existing = 'mcp_servers = {booley = {command="old"}}\n'
    path = _seed_codex(tmp_path, existing)

    with pytest.raises(ValueError, match="inline mcp_servers table") as error:
        reg.register("codex", home=tmp_path)

    assert str(path) in str(error.value) and "repair this file and retry" in str(error.value)
    assert path.read_text() == existing


def test_codex_editor_failure_during_an_edit_is_a_named_error(tmp_path, monkeypatch):
    from tomlkit.exceptions import TOMLKitError

    calls = _stub_codex_effects(monkeypatch)
    path = _seed_codex(tmp_path, 'model="existing"\n')
    monkeypatch.setattr(reg, "_set_codex_table_key", Mock(side_effect=TOMLKitError("no fit")))

    with pytest.raises(ValueError, match=re.escape(str(path))) as error:
        reg.register("codex", home=tmp_path)

    assert "no fit" in str(error.value) and "repair this file and retry" in str(error.value)
    assert calls == [] and path.read_text() == 'model="existing"\n'


def test_codex_unusable_mcp_migration_is_refused_before_any_write(tmp_path, monkeypatch):
    calls = _stub_codex_effects(monkeypatch)
    path = _seed_codex(tmp_path, 'model="existing"\n')
    monkeypatch.setattr(reg, "codex_section", lambda: '[mcp_servers.booley]\nurl = "http://x"\n')

    with pytest.raises(ValueError, match="did not produce current settings"):
        reg.register("codex", home=tmp_path)

    assert calls == [] and path.read_text() == 'model="existing"\n'


def test_codex_credential_failure_does_not_publish_permissions(tmp_path, monkeypatch):
    path = _seed_codex(tmp_path, 'approval_policy="on-request"\n')
    _stub_codex_effects(monkeypatch)
    monkeypatch.setattr(
        reg, "apply_stored_credential", Mock(side_effect=ValueError("credential policy conflict"))
    )
    with pytest.raises(ValueError, match="credential policy conflict"):
        reg.register("codex", home=tmp_path)
    failed = tomllib.loads(path.read_text())
    assert failed["approval_policy"] == "on-request"
    assert failed["mcp_servers"]["booley"]["url"] == reg.http_url()

    monkeypatch.setattr(reg, "apply_stored_credential", lambda *args: "none")
    assert "perm:written" in reg.register("codex", home=tmp_path)

    healed = tomllib.loads(path.read_text())
    assert healed["approval_policy"] == "never"
    assert healed["mcp_servers"] == failed["mcp_servers"]


@pytest.mark.parametrize("app", ["claude", "codex"])
def test_policy_switch_clears_only_managed_credential_entries(tmp_path, monkeypatch, app):
    from booley.runtime import auth_token

    monkeypatch.setenv("BOOLEY_PRIMARY_AUTH", "auto")
    for key in auth_token.CREDENTIAL_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    sidecar = tmp_path / auth_token.TOKEN_SEED_BASENAME[app]
    sidecar.write_text("SYNTHETIC")
    assert reg.apply_stored_credential(app, tmp_path) == "written"
    policy = "api_key" if app == "claude" else "subscription"
    monkeypatch.setenv("BOOLEY_PRIMARY_AUTH", policy)
    reg.apply_stored_credential(app, tmp_path)
    if app == "claude":
        assert "CLAUDE_CODE_OAUTH_TOKEN" not in reg.claude_settings_path(tmp_path).read_text()
    else:
        assert not reg.codex_auth_path(tmp_path).exists()


def test_api_policy_refuses_user_login_without_deleting_it(tmp_path, monkeypatch):
    monkeypatch.setenv("BOOLEY_PRIMARY_AUTH", "api_key")
    login = tmp_path / ".claude" / ".credentials.json"
    login.parent.mkdir()
    login.write_text('{"claudeAiOauth":{"accessToken":"SYNTHETIC_USER"}}')
    before = login.read_bytes()
    settings = reg.claude_settings_path(tmp_path)
    settings.write_text('{"env":{"CLAUDE_CODE_OAUTH_TOKEN":"SYNTHETIC_MANAGED"},"theme":"dark"}')
    settings_before = settings.read_bytes()
    with pytest.raises(ValueError, match="conflicts") as caught:
        reg.apply_stored_credential("claude", tmp_path)
    assert "SYNTHETIC" not in str(caught.value)
    assert login.read_bytes() == before
    assert settings.read_bytes() == settings_before


# ===========================================================================
# First-run state — no onboarding, login or folder-trust prompt (#2258)
# ===========================================================================

_FIRST_RUN_ENV_VARS = (
    "ANTHROPIC_API_KEY",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "OPENAI_API_KEY",
    "BOOLEY_PRIMARY_AUTH",
    "BOOLEY_DIR",
    "RTL_PROJECT_ROOT",
    "BOOLEY_CONTAINER",
    "CODEX_HOME",
)


@pytest.fixture
def first_run(tmp_path, monkeypatch):
    """An unseeded Sandbox home plus Project Root and Booley Directory trust keys."""
    from booley.runtime import booley_dir

    for name in _FIRST_RUN_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    booley_dir.reset_cache()
    home = tmp_path / "home"
    root = tmp_path / "work"
    booley = tmp_path / "booley-project"
    for directory in (home, root, booley):
        directory.mkdir()
    yield SimpleNamespace(
        home=home,
        root=str(root.resolve()),
        booley=str(booley.resolve()),
        keys=(str(root.resolve()), str(booley.resolve())),
    )
    booley_dir.reset_cache()


def _claude_config(home):
    return json.loads(reg.claude_config_path(home).read_text(encoding="utf-8"))


def _claude_settings(home):
    return json.loads(reg.claude_settings_path(home).read_text(encoding="utf-8"))


def _codex_config(home):
    return tomllib.loads(reg.codex_config_path(home).read_text(encoding="utf-8"))


def _write_claude_config(home, data):
    reg.claude_config_path(home).write_text(json.dumps(data), encoding="utf-8")


def _assert_claude_trusted(config, first_run):
    for key in first_run.keys:
        assert config["projects"][key]["hasTrustDialogAccepted"] is True


def test_claude_unseeded_with_credential_skips_first_run_prompts(first_run, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-test")

    reg.register("claude", home=first_run.home, trust_paths=first_run.keys)

    config = _claude_config(first_run.home)
    assert config["hasCompletedOnboarding"] is True
    assert config["hasSeenAutoDefaultNudge"] is True
    _assert_claude_trusted(config, first_run)
    assert _claude_settings(first_run.home)["theme"] == "dark"
    assert config["mcpServers"]["booley"]["type"] == "http"


def test_claude_unseeded_with_login_file_skips_first_run_prompts(first_run):
    login = first_run.home / ".claude" / ".credentials.json"
    login.parent.mkdir()
    login.write_text(json.dumps({"claudeAiOauth": {"accessToken": "fake"}}), encoding="utf-8")

    reg.register("claude", home=first_run.home, trust_paths=first_run.keys)

    config = _claude_config(first_run.home)
    assert config["hasCompletedOnboarding"] is True
    _assert_claude_trusted(config, first_run)
    assert _claude_settings(first_run.home)["theme"] == "dark"


def test_claude_api_key_keeps_key_approval_dialog(first_run, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-test")

    reg.register("claude", home=first_run.home, trust_paths=first_run.keys)

    config = _claude_config(first_run.home)
    assert config["hasCompletedOnboarding"] is True
    _assert_claude_trusted(config, first_run)
    # Claude's "use this API key?" dialog is the billing consent: never pre-answered.
    assert "customApiKeyResponses" not in config


def test_codex_unseeded_with_api_key_skips_login_and_trust(first_run, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-proj-test")

    reg.register("codex", home=first_run.home, trust_paths=first_run.keys)

    auth = json.loads(reg.codex_auth_path(first_run.home).read_text(encoding="utf-8"))
    assert auth == {"OPENAI_API_KEY": "sk-proj-test"}
    config = _codex_config(first_run.home)
    for key in first_run.keys:
        assert config["projects"][key]["trust_level"] == "trusted"
    assert config["mcp_servers"]["booley"]["url"].startswith("http://127.0.0.1:")


def test_claude_without_credential_keeps_login_and_writes_trust(first_run):
    reg.register("claude", home=first_run.home, trust_paths=first_run.keys)

    config = _claude_config(first_run.home)
    _assert_claude_trusted(config, first_run)
    assert config["hasSeenAutoDefaultNudge"] is True
    assert "hasCompletedOnboarding" not in config
    assert "theme" not in _claude_settings(first_run.home)


def test_codex_without_credential_keeps_login_and_writes_trust(first_run):
    reg.register("codex", home=first_run.home, trust_paths=first_run.keys)

    assert not reg.codex_auth_path(first_run.home).exists()
    config = _codex_config(first_run.home)
    for key in first_run.keys:
        assert config["projects"][key]["trust_level"] == "trusted"


def test_claude_seeded_config_keeps_seed_and_adds_sandbox_trust(first_run, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-test")
    host_entry = {"hasTrustDialogAccepted": True, "allowedTools": ["Bash(ls:*)"]}
    seed = {
        "hasCompletedOnboarding": True,
        "hasSeenAutoDefaultNudge": False,
        "autoUpdates": False,
        "cachedGrowthBookFeatures": {"gate": 1},
        "projects": {"/host/checkout": host_entry},
    }
    _write_claude_config(first_run.home, seed)

    reg.register("claude", home=first_run.home, trust_paths=first_run.keys)

    config = _claude_config(first_run.home)
    for kept in (
        "hasCompletedOnboarding",
        "hasSeenAutoDefaultNudge",
        "autoUpdates",
        "cachedGrowthBookFeatures",
    ):
        assert config[kept] == seed[kept]
    assert config["projects"]["/host/checkout"] == host_entry
    _assert_claude_trusted(config, first_run)


def test_claude_keeps_user_onboarding_choice(first_run, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-test")
    _write_claude_config(first_run.home, {"hasCompletedOnboarding": False})

    reg.register("claude", home=first_run.home, trust_paths=first_run.keys)

    config = _claude_config(first_run.home)
    assert config["hasCompletedOnboarding"] is False
    _assert_claude_trusted(config, first_run)


def test_claude_keeps_user_theme(first_run, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-test")
    settings = reg.claude_settings_path(first_run.home)
    settings.parent.mkdir()
    settings.write_text(json.dumps({"theme": "light"}), encoding="utf-8")

    reg.register("claude", home=first_run.home, trust_paths=first_run.keys)

    assert _claude_settings(first_run.home)["theme"] == "light"
    assert _claude_config(first_run.home)["hasCompletedOnboarding"] is True


def test_claude_legacy_theme_blocks_default_theme(first_run, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-test")
    _write_claude_config(first_run.home, {"theme": "light-daltonized"})

    reg.register("claude", home=first_run.home, trust_paths=first_run.keys)

    assert "theme" not in _claude_settings(first_run.home)
    config = _claude_config(first_run.home)
    assert config["theme"] == "light-daltonized"
    assert config["hasCompletedOnboarding"] is True


def test_claude_keeps_user_project_entry(first_run):
    user_entry = {"hasTrustDialogAccepted": False, "allowedTools": ["Read"]}
    _write_claude_config(first_run.home, {"projects": {first_run.root: dict(user_entry)}})

    reg.register("claude", home=first_run.home, trust_paths=first_run.keys)

    projects = _claude_config(first_run.home)["projects"]
    assert projects[first_run.root] == user_entry
    assert projects[first_run.booley]["hasTrustDialogAccepted"] is True


def test_codex_keeps_user_trust_level(first_run):
    block = (
        "# my notes\n"
        f"[projects.{json.dumps(first_run.root)}]\n"
        'trust_level = "untrusted"  # keep this folder out\n'
    )
    reg.codex_config_path(first_run.home).parent.mkdir()
    reg.codex_config_path(first_run.home).write_text(block, encoding="utf-8")

    reg.register("codex", home=first_run.home, trust_paths=first_run.keys)

    assert block in reg.codex_config_path(first_run.home).read_text(encoding="utf-8")
    projects = _codex_config(first_run.home)["projects"]
    assert projects[first_run.root]["trust_level"] == "untrusted"
    assert projects[first_run.booley]["trust_level"] == "trusted"


def test_sandbox_trust_paths_cover_linked_booley_dir(first_run, tmp_path):
    require_symlinks(tmp_path)
    real_root = tmp_path / "real-project"
    real_root.mkdir()
    spelled_root = tmp_path / "spelled-project"
    spelled_root.symlink_to(real_root, target_is_directory=True)
    linked_target = tmp_path / "outside-booley"
    linked_target.mkdir()
    (real_root / ".booley").symlink_to(linked_target, target_is_directory=True)

    keys = entry.sandbox_trust_paths(spelled_root)

    assert str(real_root.resolve()) in keys
    assert str(spelled_root.absolute()) in keys
    assert str(linked_target.resolve()) in keys
    assert str(spelled_root / ".booley") in keys


def test_main_registers_trust_for_hook_cwd(first_run, tmp_path, monkeypatch):
    project = tmp_path / "hook-project"
    project.mkdir()
    monkeypatch.chdir(project)
    monkeypatch.setenv("PWD", str(project))
    monkeypatch.setenv("HOME", str(first_run.home))
    monkeypatch.setenv("BOOLEY_DIR", str(tmp_path / "booley-project"))
    monkeypatch.setenv("BOOLEY_AGENT_APP", "claude")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-test")
    monkeypatch.setattr(reg, "ensure_http_server", lambda *, mode: "running")
    monkeypatch.setattr(entry, "launch_auto_doctor", lambda: "current")
    monkeypatch.setattr(entry, "observe_upgrade", lambda: "current")

    entry.main()

    projects = _claude_config(first_run.home)["projects"]
    assert projects[str(project.resolve())]["hasTrustDialogAccepted"] is True
    assert projects[first_run.booley]["hasTrustDialogAccepted"] is True


def test_claude_invalid_config_is_refused_untouched(first_run):
    path = reg.claude_config_path(first_run.home)
    path.write_text("{not json", encoding="utf-8")

    with pytest.raises(ValueError, match="repair") as raised:
        reg.register("claude", home=first_run.home, trust_paths=first_run.keys)

    assert str(path) in str(raised.value)
    assert path.read_text(encoding="utf-8") == "{not json"


def test_claude_invalid_settings_is_refused_untouched(first_run):
    path = reg.claude_settings_path(first_run.home)
    path.parent.mkdir()
    path.write_text("{ not json", encoding="utf-8")

    with pytest.raises(ValueError, match="repair") as raised:
        reg.register("claude", home=first_run.home, trust_paths=first_run.keys)

    assert str(path) in str(raised.value)
    assert path.read_text(encoding="utf-8") == "{ not json"


def test_codex_invalid_projects_table_is_refused_untouched(first_run):
    path = reg.codex_config_path(first_run.home)
    path.parent.mkdir()
    path.write_text('projects = "x"\n', encoding="utf-8")

    with pytest.raises(ValueError, match="repair") as raised:
        reg.register("codex", home=first_run.home, trust_paths=first_run.keys)

    assert str(path) in str(raised.value)
    assert path.read_text(encoding="utf-8") == 'projects = "x"\n'


def _snapshot(home):
    """Bytes and mtime of every file under *home*."""
    return {
        str(path): (path.read_bytes(), path.stat().st_mtime_ns)
        for path in sorted(home.rglob("*"))
        if path.is_file()
    }


def test_repeat_registration_is_a_no_op(first_run, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-test")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-proj-test")
    for app in ("claude", "codex"):
        first = reg.register(app, home=first_run.home, trust_paths=first_run.keys)
        assert "first-run:written" in first

    claude = _claude_config(first_run.home)
    _assert_claude_trusted(claude, first_run)
    assert claude["hasCompletedOnboarding"] is True
    assert _claude_settings(first_run.home)["theme"] == "dark"
    assert _codex_config(first_run.home)["projects"][first_run.root]["trust_level"] == "trusted"
    before = _snapshot(first_run.home)

    for app in ("claude", "codex"):
        again = reg.register(app, home=first_run.home, trust_paths=first_run.keys)
        assert "first-run:current" in again

    assert _snapshot(first_run.home) == before


@pytest.mark.parametrize(
    "existing",
    [
        {"projects": []},
        {"projects": None},
        {"projects": {"/x": "not an object"}},
        {"mcpServers": None},
    ],
)
def test_claude_misshaped_config_is_refused_before_any_write(first_run, existing):
    path = reg.claude_config_path(first_run.home)
    original = json.dumps(existing)
    path.write_text(original, encoding="utf-8")
    first_run_trust = ("/x", *first_run.keys)

    with pytest.raises(ValueError, match="repair"):
        reg.register("claude", home=first_run.home, trust_paths=first_run_trust)

    assert path.read_text(encoding="utf-8") == original
    assert not reg.claude_settings_path(first_run.home).exists()


def test_claude_existing_entry_without_trust_key_gains_it_and_keeps_others(first_run):
    _write_claude_config(
        first_run.home, {"projects": {first_run.root: {"allowedTools": ["Read"]}}}
    )

    reg.register("claude", home=first_run.home, trust_paths=first_run.keys)

    project = _claude_config(first_run.home)["projects"][first_run.root]
    assert project == {"allowedTools": ["Read"], "hasTrustDialogAccepted": True}


def _trust_layout(first_run, existing):
    """Seed the Codex config with *existing* (``{root}`` filled in) and return its path."""
    # JSON string escaping is valid TOML basic-string escaping (Windows backslashes).
    return _seed_codex(first_run.home, existing.replace('"{root}"', json.dumps(first_run.root)))


@pytest.mark.parametrize(
    ("existing", "trusted_keys", "skipped"),
    [
        ('# note\n[projects."{root}"]\nother = 1\n', "both", False),
        ('model = "x"\n', "both", False),
        ('model = "x"', "both", False),
        ('projects."{root}".other = 1\n', "both", False),
        ('[projects]\n"{root}".other = 1\n', "both", False),
        ('projects = { "/elsewhere" = { trust_level = "trusted" } }\n', "none", True),
    ],
    ids=[
        "existing-table",
        "new-table",
        "new-table-no-final-newline",
        "root-dotted-entry",
        "dotted-entry-in-table",
        "inline-table",
    ],
)
def test_codex_trust_layouts(first_run, capsys, existing, trusted_keys, skipped):
    path = _trust_layout(first_run, existing)
    before = path.read_text()

    status = reg._apply_codex_trust(first_run.home, first_run.keys)

    body = path.read_text()
    projects = tomllib.loads(body)["projects"]
    assert tomllib.loads(body).get("model") == tomllib.loads(before).get("model")
    for key in first_run.keys:
        assert (projects.get(key, {}).get("trust_level") == "trusted") is (trusted_keys == "both")
    if "other = 1" in before:
        assert projects[first_run.root]["other"] == 1
    warnings = capsys.readouterr().err
    assert all(key in warnings and "by hand" in warnings for key in first_run.keys) is skipped
    assert status == ("current" if skipped else "written")
    if skipped:
        assert body == before
    elif before.startswith("# note"):
        assert body.startswith("# note\n") and body.count("[projects.") == 2
    assert reg._apply_codex_trust(first_run.home, first_run.keys) == "current"
    assert path.read_text() == body


def test_codex_trust_skips_a_path_whose_edit_fails(first_run, monkeypatch, capsys):
    from tomlkit.exceptions import TOMLKitError

    existing = f"[projects.{json.dumps(first_run.root)}]\nother = 1\n"
    path = reg.codex_config_path(first_run.home)
    path.parent.mkdir()
    path.write_text(existing, encoding="utf-8")
    monkeypatch.setattr(reg, "_set_codex_table_key", Mock(side_effect=TOMLKitError("no fit")))

    assert reg._apply_codex_trust(first_run.home, first_run.keys) == "written"

    projects = tomllib.loads(path.read_text())["projects"]
    assert projects[first_run.root] == {"other": 1}
    assert projects[first_run.booley] == {"trust_level": "trusted"}
    warnings = capsys.readouterr().err
    assert first_run.root in warnings and first_run.booley not in warnings


def test_codex_inline_projects_table_never_breaks_registration(first_run):
    _trust_layout(first_run, 'projects = { "/elsewhere" = { trust_level = "trusted" } }\n')

    result = reg.register("codex", home=first_run.home, trust_paths=first_run.keys)

    assert "first-run:current" in result


def test_codex_trust_parses_config_once(first_run, monkeypatch):
    source = 'model = "existing"\n'
    _trust_layout(first_run, source)
    with _count_parses(monkeypatch, source) as parses:
        reg._apply_codex_trust(first_run.home, first_run.keys)
    assert parses() == 1


def test_codex_trust_covers_non_ascii_paths(first_run, tmp_path):
    odd = tmp_path / "café-\U0001f600"
    odd.mkdir()

    reg.register("codex", home=first_run.home, trust_paths=(str(odd.resolve()),))

    assert (
        _codex_config(first_run.home)["projects"][str(odd.resolve())]["trust_level"] == "trusted"
    )


def test_booley_directory_symlink_to_an_ancestor_is_not_trusted(first_run, tmp_path):
    require_symlinks(tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    (project / ".booley").symlink_to(tmp_path, target_is_directory=True)

    keys = entry.sandbox_trust_paths(project)

    assert str(tmp_path.resolve()) not in keys
    assert str(project.resolve()) in keys


def test_main_reports_refused_registration_but_still_runs_the_hook(
    first_run, tmp_path, monkeypatch, capsys
):
    project = tmp_path / "hook-project"
    project.mkdir()
    monkeypatch.chdir(project)
    monkeypatch.setenv("HOME", str(first_run.home))
    monkeypatch.setenv("BOOLEY_AGENT_APP", "claude")
    reg.claude_config_path(first_run.home).write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(reg, "ensure_http_server", lambda *, mode: "running")
    doctor_runs = []
    monkeypatch.setattr(entry, "launch_auto_doctor", lambda: doctor_runs.append(1) or "current")
    monkeypatch.setattr(entry, "observe_upgrade", lambda: "current")

    with pytest.raises(SystemExit) as raised:
        entry.main()

    assert raised.value.code == 1
    assert doctor_runs == [1]
    assert "registration refused" in capsys.readouterr().err


def test_codex_publish_refuses_invalid_text_by_default(tmp_path):
    """Writers outside the permission path (trust, gateway, MCP) rely on this check."""
    path = reg.codex_config_path(tmp_path)
    path.parent.mkdir()
    path.write_text('model="existing"\n')
    with pytest.raises(ValueError, match=re.escape(str(path))):
        reg._publish_codex_config(path, "notice=true\n")
    assert path.read_text() == 'model="existing"\n'


# ===========================================================================
# Per-CLI Codex gateway in a Booley-issued Sandbox (#2897)
# ===========================================================================

_GATEWAY_URL = "https://gw.example/v1"
_GATEWAY_KEYS = (
    "BOOLEY_GATEWAY_URL",
    "BOOLEY_GATEWAY_TOKEN",
    "BOOLEY_GATEWAY_TOKEN_COMMAND",
    "BOOLEY_GATEWAY_MODEL",
    "BOOLEY_GATEWAY_EFFORT",
)


def _gateway_start(first_run, tmp_path, monkeypatch, *, app, record, gateway_values=True):
    """Prepare a container start of the registrar hook; ``record=None`` is a hand-written spec."""
    from booley.config import agent as agent_config

    project = tmp_path / "hook-project"
    project.mkdir()
    monkeypatch.chdir(project)
    monkeypatch.setenv("PWD", str(project))
    monkeypatch.setenv("HOME", str(first_run.home))
    monkeypatch.setenv("USERPROFILE", str(first_run.home))
    monkeypatch.setenv("BOOLEY_DIR", str(tmp_path / "booley-project"))
    monkeypatch.setenv("BOOLEY_AGENT_APP", app)
    monkeypatch.setattr(agent_config, "_inside_container", lambda: True)
    for key in _GATEWAY_KEYS:
        monkeypatch.delenv(key, raising=False)
    if gateway_values:
        monkeypatch.setenv("BOOLEY_GATEWAY_URL", _GATEWAY_URL)
        monkeypatch.setenv("BOOLEY_GATEWAY_TOKEN", "SYNTHETIC_GATEWAY_TOKEN")
    if record is None:
        monkeypatch.delenv("BOOLEY_AGENT_CLIS", raising=False)
        monkeypatch.setenv("BOOLEY_PRIMARY_AUTH", "gateway")
    else:
        monkeypatch.setenv("BOOLEY_AGENT_CLIS", record)
        monkeypatch.setenv("BOOLEY_PRIMARY_AUTH", "subscription")
    monkeypatch.setattr(reg, "ensure_http_server", lambda *, mode: "running")
    monkeypatch.setattr(entry, "launch_auto_doctor", lambda: "current")
    monkeypatch.setattr(entry, "observe_upgrade", lambda: "current")


_ISSUED_RECORD = "claude=subscription,codex=gateway"


def _assert_gateway_hidden(home):
    env_map = _claude_settings(home)["env"]
    assert [env_map[key] for key in _GATEWAY_KEYS] == [""] * len(_GATEWAY_KEYS)
    assert env_map["BOOLEY_HIDDEN_GATEWAY"] == "1"
    return env_map


def test_claude_default_gateway_start_routes_codex_and_hides_gateway_from_claude(
    first_run, tmp_path, monkeypatch, capsys
):
    _gateway_start(first_run, tmp_path, monkeypatch, app="claude", record=_ISSUED_RECORD)
    settings_path = reg.claude_settings_path(first_run.home)
    settings_path.parent.mkdir()
    settings_path.write_text(json.dumps({"env": {"USER_KEY": "keep"}}), encoding="utf-8")

    entry.main()

    codex = _codex_config(first_run.home)
    assert codex["model_provider"] == "booley_gateway"
    assert codex["model_providers"]["booley_gateway"]["base_url"] == _GATEWAY_URL
    assert _assert_gateway_hidden(first_run.home)["USER_KEY"] == "keep"
    err = capsys.readouterr().err
    assert "gateway:written" in err
    assert "claude-gateway:hidden" in err


def test_gateway_start_hides_unset_gateway_keys_from_claude(
    first_run, tmp_path, monkeypatch, capsys
):
    """VS Code without exports: the keys are blanked anyway for a later attach."""
    _gateway_start(
        first_run,
        tmp_path,
        monkeypatch,
        app="claude",
        record=_ISSUED_RECORD,
        gateway_values=False,
    )

    entry.main()

    _assert_gateway_hidden(first_run.home)
    assert "gateway:invalid" in capsys.readouterr().err


def test_gateway_hiding_is_idempotent(first_run, tmp_path, monkeypatch):
    _gateway_start(first_run, tmp_path, monkeypatch, app="codex", record=_ISSUED_RECORD)

    assert reg.hide_gateway_from_claude(first_run.home) == "hidden"
    writes: list[Path] = []
    monkeypatch.setattr(reg, "_write_private_json", lambda path, _data: writes.append(path))

    assert reg.hide_gateway_from_claude(first_run.home) == "hidden"
    assert writes == []  # nothing changed, so the file is not rewritten


def test_gateway_hiding_replaces_a_misshaped_env_map(first_run, tmp_path, monkeypatch):
    _gateway_start(first_run, tmp_path, monkeypatch, app="codex", record=_ISSUED_RECORD)
    path = reg.claude_settings_path(first_run.home)
    path.parent.mkdir()
    path.write_text(json.dumps({"env": ["not", "a", "map"], "theme": "dark"}), encoding="utf-8")

    reg.hide_gateway_from_claude(first_run.home)

    _assert_gateway_hidden(first_run.home)
    assert _claude_settings(first_run.home)["theme"] == "dark"


def test_unreadable_claude_settings_refuses_a_gateway_start(
    first_run, tmp_path, monkeypatch, capsys
):
    _gateway_start(first_run, tmp_path, monkeypatch, app="codex", record=_ISSUED_RECORD)
    path = reg.claude_settings_path(first_run.home)
    path.parent.mkdir()
    path.write_text("{ not json", encoding="utf-8")

    with pytest.raises(SystemExit) as raised:
        entry.main()

    assert raised.value.code == 1
    assert "registration refused" in capsys.readouterr().err
    assert path.read_text(encoding="utf-8") == "{ not json"


def test_refused_claude_registration_still_hides_gateway(first_run, tmp_path, monkeypatch, capsys):
    _gateway_start(first_run, tmp_path, monkeypatch, app="claude", record=_ISSUED_RECORD)
    _write_claude_config(first_run.home, {"mcpServers": ["not", "an", "object"]})

    with pytest.raises(SystemExit) as raised:
        entry.main()

    assert raised.value.code == 1
    assert "refused" in capsys.readouterr().err
    _assert_gateway_hidden(first_run.home)


def test_invalid_codex_config_refuses_a_gateway_start_but_still_hides(
    first_run, tmp_path, monkeypatch, capsys
):
    _gateway_start(first_run, tmp_path, monkeypatch, app="claude", record=_ISSUED_RECORD)
    codex_path = reg.codex_config_path(first_run.home)
    codex_path.parent.mkdir()
    codex_path.write_text("broken = [", encoding="utf-8")

    with pytest.raises(SystemExit) as raised:
        entry.main()

    assert raised.value.code == 1
    assert "registration refused" in capsys.readouterr().err
    _assert_gateway_hidden(first_run.home)


def test_handwritten_gateway_start_leaves_claude_settings_alone(first_run, tmp_path, monkeypatch):
    """Guard: a hand-written Claude-app spec (no record) keeps Claude's environment."""
    _gateway_start(first_run, tmp_path, monkeypatch, app="claude", record=None)

    entry.main()

    assert "env" not in _claude_settings(first_run.home)
    assert not reg.codex_config_path(first_run.home).exists()


# ===========================================================================
# Every allowed Agent CLI is registered, each under its own policy (#2756)
# ===========================================================================


def _multi_cli_start(first_run, tmp_path, monkeypatch, *, app, record, primary_auth):
    """A container start of the registrar hook with *record* and *primary_auth*.

    ``record=None`` is a hand-written spec. No gateway values are exported, so a
    ``gateway`` policy is judged only by what the registrar writes.
    """
    _gateway_start(first_run, tmp_path, monkeypatch, app=app, record=record, gateway_values=False)
    if primary_auth is None:
        monkeypatch.delenv("BOOLEY_PRIMARY_AUTH", raising=False)
    else:
        monkeypatch.setenv("BOOLEY_PRIMARY_AUTH", primary_auth)


def _booley_mcp_entries(home):
    """Which CLIs carry the ``booley`` MCP entry in *home*'s user config."""

    def has_entry(read, key):
        try:
            return "booley" in read(home).get(key, {})
        except (OSError, ValueError):  # a missing or deliberately broken config has no entry
            return False

    return {
        "claude": has_entry(_claude_config, "mcpServers"),
        "codex": has_entry(_codex_config, "mcp_servers"),
    }


def test_issued_sandbox_registers_every_listed_cli(first_run, tmp_path, monkeypatch, capsys):
    require_symlinks(tmp_path)
    src = TestSkills._fake_skills(tmp_path, ["booley-a"])
    TestSkills._patch_src(monkeypatch, src)
    _multi_cli_start(
        first_run,
        tmp_path,
        monkeypatch,
        app="claude",
        record="claude=auto,codex=auto",
        primary_auth="auto",
    )

    entry.main()

    assert _booley_mcp_entries(first_run.home) == {"claude": True, "codex": True}
    assert (first_run.home / ".claude" / "skills" / "booley-a").exists()
    assert (first_run.home / ".agents" / "skills" / "booley-a").exists()
    err = capsys.readouterr().err
    assert "claude:written" in err
    assert "codex:written" in err


@pytest.mark.parametrize("default", ["claude-default", "codex-default"])
def test_each_listed_cli_applies_its_own_recorded_policy(
    first_run, tmp_path, monkeypatch, capsys, default
):
    if default == "claude-default":
        # Codex's API-key policy must not be judged under the default's subscription policy.
        _multi_cli_start(
            first_run,
            tmp_path,
            monkeypatch,
            app="claude",
            record="claude=subscription,codex=api_key",
            primary_auth="subscription",
        )
        monkeypatch.setenv("OPENAI_API_KEY", "SYNTHETIC_CODEX_KEY")
    else:
        # Claude's subscription login must be found although the default is on the gateway.
        _multi_cli_start(
            first_run,
            tmp_path,
            monkeypatch,
            app="codex",
            record="codex=gateway,claude=subscription",
            primary_auth="gateway",
        )
        login = first_run.home / ".claude" / ".credentials.json"
        login.parent.mkdir()
        login.write_text(
            json.dumps({"claudeAiOauth": {"accessToken": "SYNTHETIC_ACCESS"}}), encoding="utf-8"
        )

    entry.main()

    assert "refused" not in capsys.readouterr().err
    if default == "claude-default":
        auth_path = reg.codex_auth_path(first_run.home)
        assert auth_path.exists()
        assert json.loads(auth_path.read_text(encoding="utf-8")) == {
            "OPENAI_API_KEY": "SYNTHETIC_CODEX_KEY"
        }
    else:
        assert reg.claude_config_path(first_run.home).exists()
        assert _claude_config(first_run.home)["hasCompletedOnboarding"] is True


@pytest.mark.parametrize("broken", ["claude", "codex"])
def test_one_cli_refusal_leaves_the_other_registered(
    first_run, tmp_path, monkeypatch, capsys, broken
):
    _multi_cli_start(
        first_run,
        tmp_path,
        monkeypatch,
        app="claude",
        record="claude=auto,codex=auto",
        primary_auth="auto",
    )
    doctor_runs = []
    monkeypatch.setattr(entry, "launch_auto_doctor", lambda: doctor_runs.append(1) or "current")
    if broken == "claude":
        reg.claude_config_path(first_run.home).write_text("{not json", encoding="utf-8")
    else:
        codex_path = reg.codex_config_path(first_run.home)
        codex_path.parent.mkdir()
        codex_path.write_text("broken = [", encoding="utf-8")
    other = "codex" if broken == "claude" else "claude"

    with pytest.raises(SystemExit) as raised:
        entry.main()

    assert raised.value.code == 1
    assert doctor_runs == [1]
    assert _booley_mcp_entries(first_run.home)[other] is True
    err = capsys.readouterr().err
    assert f"registration refused: {broken}:" in err
    assert f"{broken}:refused" in err
    assert f"{other}:written" in err


@pytest.mark.parametrize(
    "spec",
    [
        ("claude", None, None),
        ("codex", None, None),
        ("codex", "codex=auto", "auto"),
    ],
    ids=["handwritten-claude", "handwritten-codex", "issued-codex"],
)
def test_single_cli_sandbox_registers_only_that_cli(
    first_run, tmp_path, monkeypatch, capsys, spec
):
    """Guard: a Sandbox allowing one CLI registers it alone and prints no set suffix."""
    app, record, primary_auth = spec
    _multi_cli_start(
        first_run, tmp_path, monkeypatch, app=app, record=record, primary_auth=primary_auth
    )

    entry.main()

    expected = {"claude": False, "codex": False}
    expected[app] = True
    assert _booley_mcp_entries(first_run.home) == expected
    err = capsys.readouterr().err
    assert re.search(rf"(?<![-\w]){app}:written", err)
    other = "codex" if app == "claude" else "claude"
    assert not re.search(rf"(?<![-\w]){other}:", err)
    assert "clis:" not in err
