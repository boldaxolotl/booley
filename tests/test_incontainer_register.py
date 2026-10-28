"""Tests for the in-container MCP registrar (ADR 0018 WS4, ADR 0023 HTTP)."""

from __future__ import annotations

import builtins
import json
import os
import re
import sys
import tomllib
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from booley.harness import incontainer_register as entry
from booley.mcp import (
    catalog,
)
from booley.runtime import incontainer_setup as reg
from tests.conftest import require_symlinks


@pytest.mark.parametrize(
    "existing", [None, "suppress_unstable_features_warning=false\n", "broken=["]
)
@pytest.mark.parametrize("failure", ["missing", "stale", "broken", "broken-import"])
def test_codex_registration_installation_failure_precedes_all_writes(
    tmp_path, monkeypatch, existing, failure
):
    from booley.runtime import mcp_config

    def loads(source):
        if failure == "broken":
            raise RuntimeError("broken probe")
        raise ValueError("TOML1.0-only parser rejected capability probe")

    parser = None if failure == "missing" else SimpleNamespace(loads=loads)
    path = reg.codex_config_path(tmp_path)
    if existing is not None:
        path.parent.mkdir(parents=True)
        path.write_text(existing)
    calls = []
    for name in (
        "deploy_skills",
        "deploy_host_skills",
        "apply_stored_credential",
        "_publish_codex_config",
        "_apply_codex_permission_mode",
    ):
        monkeypatch.setattr(reg, name, lambda *args: calls.append(args))
    mcp_config._codex_toml_parser.cache_clear()
    real_import = builtins.__import__

    def import_parser(name, *args, **kwargs):
        if name == "tomli" and failure == "broken-import":
            raise ImportError("partial parser installation")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", import_parser)
    monkeypatch.setitem(sys.modules, "tomli", parser)
    try:
        with pytest.raises(RuntimeError, match=r"installation.*tomli>=2\.4\.0") as error:
            reg.register("codex", home=tmp_path)
        assert "repair this file" not in str(error.value)
        assert calls == []
        assert path.read_text() == existing if existing is not None else not path.exists()
    finally:
        mcp_config._codex_toml_parser.cache_clear()


def test_legacy_runtime_module_keeps_entrypoint_compatibility():
    from booley.runtime import incontainer_register as compatibility

    assert compatibility.main is entry.main


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
# Codex — ~/.codex/config.toml [mcp_servers.booley]
# ===========================================================================


class TestCodex:
    def test_appends_section(self, tmp_path):
        path = reg.codex_config_path(tmp_path)
        assert reg.upsert_codex(path) is True
        body = path.read_text(encoding="utf-8")
        assert "[mcp_servers.booley]" in body
        assert reg.http_url() in body
        assert "tool_timeout_sec = 7200" in body

        import tomllib

        assert tomllib.loads(body)["features"]["mcp_2026_07_28"] is True

    def test_idempotent(self, tmp_path):
        path = reg.codex_config_path(tmp_path)
        assert reg.upsert_codex(path) is True
        assert reg.upsert_codex(path) is False

    def test_migrates_stale_stdio_table(self, tmp_path):
        # A stale stdio table would keep Codex spawning a doomed per-session
        # child; it must be replaced in place, preserving everything else.
        path = reg.codex_config_path(tmp_path)
        path.parent.mkdir(parents=True)
        path.write_text(
            "# my comment\n"
            '[user]\nname = "a"\n\n'
            "[mcp_servers.booley]\n"
            'command = "python"\n'
            'args = ["-m", "booley.mcp.server"]\n'
            "tool_timeout_sec = 7200\n\n"
            "[other]\nkey = 1\n",
            encoding="utf-8",
        )
        assert reg.upsert_codex(path) is True
        body = path.read_text(encoding="utf-8")
        assert "# my comment" in body and "[user]" in body and "[other]" in body
        assert "command = " not in body

        import tomllib

        parsed = tomllib.loads(body)
        assert parsed["mcp_servers"]["booley"] == {
            "url": reg.http_url(),
            "tool_timeout_sec": 7200,
        }
        assert parsed["features"]["mcp_2026_07_28"] is True

    def test_preserves_other_codex_features(self, tmp_path):
        path = reg.codex_config_path(tmp_path)
        path.parent.mkdir(parents=True)
        path.write_text("[features]\nother_feature = true\n", encoding="utf-8")

        assert reg.upsert_codex(path) is True

        import tomllib

        features = tomllib.loads(path.read_text(encoding="utf-8"))["features"]
        assert features == {"other_feature": True, "mcp_2026_07_28": True}

    def test_replaces_disabled_codex_feature(self, tmp_path):
        path = reg.codex_config_path(tmp_path)
        path.parent.mkdir(parents=True)
        path.write_text("[features]\nmcp_2026_07_28 = false\n", encoding="utf-8")

        assert reg.upsert_codex(path) is True

        import tomllib

        assert tomllib.loads(path.read_text(encoding="utf-8"))["features"] == {
            "mcp_2026_07_28": True
        }

    def test_preserves_existing_content(self, tmp_path):
        path = reg.codex_config_path(tmp_path)
        path.parent.mkdir(parents=True)
        path.write_text('[user]\nname = "a"\n', encoding="utf-8")
        reg.upsert_codex(path)
        body = path.read_text(encoding="utf-8")
        assert "[user]" in body and "[mcp_servers.booley]" in body

    def test_valid_toml(self, tmp_path):
        import tomllib

        path = reg.codex_config_path(tmp_path)
        reg.upsert_codex(path)
        parsed = tomllib.loads(path.read_text(encoding="utf-8"))
        assert parsed["mcp_servers"]["booley"]["url"] == reg.http_url()


class TestCodexPermissionMode:
    """Codex should trust the hardened container as its outer sandbox."""

    def test_writes_no_approval_full_access_mode(self, tmp_path):
        assert reg._apply_codex_permission_mode(tmp_path) == "written"

        import tomllib

        parsed = tomllib.loads(reg.codex_config_path(tmp_path).read_text())
        assert parsed["approval_policy"] == "never"
        assert parsed["sandbox_mode"] == "danger-full-access"
        assert parsed["web_search"] == "disabled"
        assert parsed["notice"]["hide_full_access_warning"] is True

    def test_idempotent(self, tmp_path):
        assert reg._apply_codex_permission_mode(tmp_path) == "written"
        assert reg._apply_codex_permission_mode(tmp_path) == "current"

    def test_preserves_other_config_and_notice_settings(self, tmp_path):
        path = reg.codex_config_path(tmp_path)
        path.parent.mkdir(parents=True)
        path.write_text(
            'model = "gpt-existing"\n'
            "# keep this comment\n\n"
            "[notice]\n"
            "hide_full_access_warning = false\n"
            "hide_rate_limit_model_nudge = true\n\n"
            "[other]\n"
            "key = 1\n",
            encoding="utf-8",
        )

        assert reg._apply_codex_permission_mode(tmp_path) == "written"

        import tomllib

        body = path.read_text(encoding="utf-8")
        parsed = tomllib.loads(body)
        assert parsed["model"] == "gpt-existing"
        assert parsed["notice"]["hide_rate_limit_model_nudge"] is True
        assert parsed["other"]["key"] == 1
        assert "# keep this comment" in body

    def test_reasserted_over_a_downgrade(self, tmp_path):
        path = reg.codex_config_path(tmp_path)
        path.parent.mkdir(parents=True)
        path.write_text(
            'approval_policy="on-request"\n'
            'sandbox_mode="workspace-write"\n'
            "notice.hide_full_access_warning=false\n",
            encoding="utf-8",
        )

        assert reg._apply_codex_permission_mode(tmp_path) == "written"

        import tomllib

        parsed = tomllib.loads(path.read_text(encoding="utf-8"))
        assert parsed["approval_policy"] == "never"
        assert parsed["sandbox_mode"] == "danger-full-access"
        assert parsed["web_search"] == "disabled"
        assert parsed["notice"]["hide_full_access_warning"] is True


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

    def test_main_skips_server_without_app(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("BOOLEY_AGENT_APP", "none")

        def fail(**kw):
            raise AssertionError("must not start a server with no client app")

        monkeypatch.setattr(reg, "ensure_http_server", fail)
        monkeypatch.setattr(entry, "launch_auto_doctor", lambda: "current")
        monkeypatch.setattr(entry, "observe_upgrade", lambda: "current")
        entry.main()
        assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("preference", [None, True, False])
def test_codex_migrates_old_current_entry_once(tmp_path, preference):
    path = reg.codex_config_path(tmp_path)
    path.parent.mkdir(parents=True)
    choice = (
        ""
        if preference is None
        else f'"suppress_unstable_features_warning"={str(preference).lower()} # keep\n'
    )
    existing = choice + "features.mcp_2026_07_28=true\n" + reg.codex_section()
    path.write_text(existing)
    assert reg.upsert_codex(path) is (preference is None)
    body = path.read_text()
    assert tomllib.loads(body)["suppress_unstable_features_warning"] is (
        True if preference is None else preference
    )
    assert body.endswith(existing)
    assert reg.upsert_codex(path) is False
    assert path.read_text() == body


@pytest.mark.parametrize("existing", ['bad = "', 'suppress_unstable_features_warning="bad"'])
def test_codex_invalid_interactive_config_never_writes(tmp_path, existing):
    path = reg.codex_config_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(existing)
    for _ in range(2):
        with pytest.raises(ValueError):
            reg.upsert_codex(path)
        assert path.read_text() == existing


@pytest.mark.parametrize(
    "existing",
    [
        "items = [\n[1, 2],\n[3]\n]\n# retain array\n",
        'description = """\n[features]\nmcp_2026_07_28 = false\n"""\n',
        "features.other=true\nfeatures.mcp_2026_07_28=false\n",
        '["features"] # flags\n"mcp_2026_07_28"=false # deliberate override\n',
        '["mcp_servers"."booley"] # stale\ncommand="old"\n["other"] # retain header\nx=1',
        'model="existing"',
    ],
)
def test_codex_full_registration_preserves_toml_forms(tmp_path, monkeypatch, existing):
    path = reg.codex_config_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(existing)
    monkeypatch.setattr(reg, "deploy_skills", lambda *args: 0)
    monkeypatch.setattr(reg, "deploy_host_skills", lambda *args: 0)
    monkeypatch.setattr(reg, "apply_stored_credential", lambda *args: "none")
    assert "codex:written" in reg.register("codex", home=tmp_path)
    body = path.read_text()
    parsed = tomllib.loads(body)
    assert parsed["suppress_unstable_features_warning"] is True
    assert parsed["features"]["mcp_2026_07_28"] is True
    assert parsed["approval_policy"] == "never"
    assert parsed["notice"]["hide_full_access_warning"] is True
    original = tomllib.loads(existing)
    for key in ("items", "description", "other", "model"):
        if key in original:
            assert parsed[key] == original[key]
    if "# retain" in existing:
        assert "# retain" in body
    assert "codex:current" in reg.register("codex", home=tmp_path)
    assert path.read_text() == body


@pytest.mark.parametrize("preference", [True, False])
def test_full_codex_registration_preserves_explicit_choice(tmp_path, monkeypatch, preference):
    path = reg.codex_config_path(tmp_path)
    path.parent.mkdir(parents=True)
    choice = f'"suppress_unstable_features_warning" = {str(preference).lower()} # retain choice\n'
    path.write_text(choice + "[features] # flags\nsuppress_unstable_features_warning=false\n")
    monkeypatch.setattr(reg, "deploy_skills", lambda *args: 0)
    monkeypatch.setattr(reg, "deploy_host_skills", lambda *args: 0)
    monkeypatch.setattr(reg, "apply_stored_credential", lambda *args: "none")
    assert "codex:written" in reg.register("codex", home=tmp_path)
    body = path.read_text()
    assert choice in body
    assert tomllib.loads(body)["suppress_unstable_features_warning"] is preference
    assert "codex:current" in reg.register("codex", home=tmp_path)
    assert path.read_text() == body


@pytest.mark.parametrize(
    "existing",
    [
        "features = {mcp_2026_07_28 = false} # preserve header\n",
        "features = {other = true, mcp_2026_07_28 = false}\n",
        'features = {other = {text = "a,b=}", values = [1, 2]}}\n',
        "notice = {hide_full_access_warning = false} # preserve header\n",
        "notice = {other = true, hide_full_access_warning = false}\n",
        'notice = {other = {text = "a,b=}", values = [1, 2]}}\n',
        '# preserve\u2028approval_policy="on-request"\nmodel="existing"\n',
        '# preserve\u2029sandbox_mode="restricted"\n',
        '# preserve\u0085web_search="enabled"\n',
    ],
)
def test_full_codex_registration_preserves_inline_tables_and_unicode_comments(
    tmp_path, monkeypatch, existing
):
    path = reg.codex_config_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(existing, encoding="utf-8")
    monkeypatch.setattr(reg, "deploy_skills", lambda *args: 0)
    monkeypatch.setattr(reg, "deploy_host_skills", lambda *args: 0)
    monkeypatch.setattr(reg, "apply_stored_credential", lambda *args: "none")
    assert "codex:written" in reg.register("codex", home=tmp_path)
    body = path.read_text(encoding="utf-8")
    parsed = tomllib.loads(body)
    assert parsed["features"]["mcp_2026_07_28"] is True
    assert parsed["notice"]["hide_full_access_warning"] is True
    for table in ("features", "notice"):
        original = tomllib.loads(existing).get(table, {})
        if "other" in original:
            assert parsed[table]["other"] == original["other"]
            assert "other = " in body
    if existing.startswith("# preserve"):
        assert existing in body
    if "# preserve header" in existing:
        assert "# preserve header" in body
    assert "codex:current" in reg.register("codex", home=tmp_path)
    assert path.read_text(encoding="utf-8") == body


@pytest.mark.parametrize("writer", ["mcp", "permission"])
def test_codex_publication_failure_preserves_destination(tmp_path, monkeypatch, writer):
    path = reg.codex_config_path(tmp_path)
    path.parent.mkdir(parents=True)
    existing = 'model="existing"\n'
    path.write_text(existing, encoding="utf-8")
    path.chmod(0o640)

    def fail_replace(source, destination):
        if destination == path:
            raise OSError("interrupted publication")
        raise AssertionError("unexpected destination")

    monkeypatch.setattr(type(path), "replace", fail_replace)
    with pytest.raises(OSError, match="interrupted publication"):
        if writer == "mcp":
            reg.upsert_codex(path)
        else:
            reg._apply_codex_permission_mode(tmp_path)
    assert path.read_text(encoding="utf-8") == existing
    if os.name != "nt":
        assert path.stat().st_mode & 0o777 == 0o640
    assert list(path.parent.iterdir()) == [path]


@pytest.mark.parametrize("existing", ['broken="', 'suppress_unstable_features_warning="bad"'])
def test_invalid_codex_diagnostic_names_destination(tmp_path, existing):
    path = reg.codex_config_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(existing)
    with pytest.raises(ValueError, match=re.escape(str(path))):
        reg.upsert_codex(path)
    assert path.read_text() == existing


@pytest.mark.parametrize("writer", ["mcp", "permission"])
def test_codex_atomic_publication_preserves_symlink_and_permissions(tmp_path, writer):
    require_symlinks(tmp_path)
    path = reg.codex_config_path(tmp_path)
    path.parent.mkdir(parents=True)
    target = tmp_path / "shared-config.toml"
    target.write_text('model="existing"\n', encoding="utf-8")
    target.chmod(0o640)
    path.symlink_to(target)
    if writer == "mcp":
        reg.upsert_codex(path)
        assert tomllib.loads(target.read_text())["suppress_unstable_features_warning"] is True
    else:
        reg._apply_codex_permission_mode(tmp_path)
        assert tomllib.loads(target.read_text())["approval_policy"] == "never"
    assert path.is_symlink()
    if os.name != "nt":
        assert target.stat().st_mode & 0o777 == 0o640
    assert tomllib.loads(target.read_text())["model"] == "existing"


@pytest.mark.parametrize("writer", ["mcp", "permission"])
def test_codex_migration_preserves_unrelated_crlf_bytes(tmp_path, writer):
    path = reg.codex_config_path(tmp_path)
    path.parent.mkdir(parents=True)
    existing = b'# retain CRLF\r\nmodel="existing"\r\n'
    path.write_bytes(existing)
    if writer == "mcp":
        reg.upsert_codex(path)
    else:
        reg._apply_codex_permission_mode(tmp_path)
    assert existing in path.read_bytes()
    tomllib.loads(path.read_bytes().decode("utf-8"))


@pytest.mark.parametrize("existing", ["features=true\n", "notice=true\n", 'notice="x"\n'])
def test_codex_invalid_table_shape_fails_before_registration_mutations(
    tmp_path, monkeypatch, existing
):
    path = reg.codex_config_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(existing)
    calls = []
    monkeypatch.setattr(reg, "deploy_skills", lambda *args: calls.append("skills"))
    monkeypatch.setattr(reg, "deploy_host_skills", lambda *args: calls.append("host skills"))
    monkeypatch.setattr(reg, "apply_stored_credential", lambda *args: calls.append("credential"))
    with pytest.raises(ValueError, match=re.escape(str(path))):
        reg.register("codex", home=tmp_path)
    assert calls == []
    assert path.read_text() == existing


@pytest.mark.parametrize(
    "existing",
    [
        'mcp_servers = {other = {url="http://other"}}\n',
        'mcp_servers = {booley = {command="old"}, other = {url="http://other"}}\n',
    ],
)
def test_codex_unsupported_inline_servers_fail_with_named_repair_diagnostic(tmp_path, existing):
    path = reg.codex_config_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(existing)
    with pytest.raises(ValueError, match=re.escape(str(path))) as error:
        reg.upsert_codex(path)
    assert "repair this file and retry" in str(error.value)
    assert path.read_text() == existing


def _assert_inline_owned_body(body, table, original):
    import tomli

    key = "mcp_2026_07_28" if table == "features" else "hide_full_access_warning"
    old = tomli.loads("value=" + original)["value"]
    expected = re.sub(rf'^(\s*"?{key}"?\s*=\s*)false\b', r"\1true", original, flags=re.MULTILINE)
    if key in old:
        assert f"{table}={expected}" in body
    else:
        separator = "," if old else ""
        pattern = (
            rf"(?m)^{table}=\{{\s*{key}\s*=\s*true" + re.escape(separator + original[1:-1]) + r"\}"
        )
        assert re.search(pattern, body), body


@pytest.mark.parametrize("preference", [None, True, False])
@pytest.mark.parametrize("ending", ["\n", "\r\n"])
@pytest.mark.parametrize(
    "features,notice",
    [
        (
            '{\n # before\n "mcp_2026_07_28"=false, # after\n other=true,\n}',
            "{\n # before\n hide_full_access_warning=false, # after\n other=true,\n}",
        ),
        ("{\n other=true, # trailing\n}", "{\n other=true, # trailing\n}"),
        ("{\n # comment-only\n}", "{\n # comment-only\n}"),
        ("{\n}", "{\n}"),
        ('{\n other = {text="a,b=}", values=[1,2]}\n}', "{\n other=true\n}"),
        (
            '{\n "other".flag=true, # between\n nested={text="{a,b}", values=[1,2]},\n}',
            '{\n "other".flag=true,\n}',
        ),
        (
            '{\n "mcp_2026_07_28"=false # no comma\n}',
            '{\n "hide_full_access_warning"=false # no comma\n}',
        ),
        ("{\n # before, keep this\n other=true,\n}", "{\n # before, keep this\n other=true,\n}"),
        (
            "{\n # before, keep this\n mcp_2026_07_28=false,\n}",
            "{\n # before, keep this\n hide_full_access_warning=false,\n}",
        ),
        (
            "{\n mcp_2026_07_28=false, # after, keep this\n}",
            "{\n hide_full_access_warning=false, # after, keep this\n}",
        ),
        ("{\n # x, mcp_2026_07_28=false\n}", "{\n # x, hide_full_access_warning=false\n}"),
        ("{\n other=true, # trailing, x\n}", "{\n other=true, # trailing, x\n}"),
    ],
)
def test_toml11_full_registration_preserves_choices_and_body_bytes(
    tmp_path, monkeypatch, preference, ending, features, notice
):
    choice = (
        ""
        if preference is None
        else f'"suppress_unstable_features_warning"={str(preference).lower()} # choice\n'
    )
    unrelated = 'other = {\n # untouched\n text="comma, brace}",\n values=[1,2],\n}\n'
    source = (choice + unrelated + f"features={features}\nnotice={notice}").replace("\n", ending)
    choice = choice.replace("\n", ending)
    unrelated = unrelated.replace("\n", ending)
    features = features.replace("\n", ending)
    notice = notice.replace("\n", ending)
    path = reg.codex_config_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_bytes(source.encode())
    monkeypatch.setattr(reg, "deploy_skills", lambda *args: 0)
    monkeypatch.setattr(reg, "deploy_host_skills", lambda *args: 0)
    monkeypatch.setattr(reg, "apply_stored_credential", lambda *args: "none")
    assert "codex:written" in reg.register("codex", home=tmp_path)
    body = path.read_bytes().decode()
    assert choice in body and unrelated in body
    import tomli

    parsed = tomli.loads(body)
    assert parsed["suppress_unstable_features_warning"] is (
        True if preference is None else preference
    )
    assert parsed["features"]["mcp_2026_07_28"] is True
    assert parsed["notice"]["hide_full_access_warning"] is True
    for table, original in (("features", features), ("notice", notice)):
        old = tomli.loads("value=" + original)["value"]
        if "other" in old:
            assert parsed[table]["other"] == old["other"]
        _assert_inline_owned_body(body, table, original)
    for fragment in ("# before", "# after", "# trailing", "# comment-only"):
        if fragment in source:
            assert fragment in body
    before = path.read_bytes()
    assert "codex:current" in reg.register("codex", home=tmp_path)
    assert path.read_bytes() == before


@pytest.mark.parametrize("ending", ["\n", "\r\n"])
def test_toml11_already_current_is_byte_identical(tmp_path, monkeypatch, ending):
    source = (
        "suppress_unstable_features_warning=false\nfeatures={\n mcp_2026_07_28=true,\n}\n"
        'approval_policy="never"\nsandbox_mode="danger-full-access"\nweb_search="disabled"\n'
        "notice={\n hide_full_access_warning=true,\n}\nother={\n x=true,\n}\n"
        + reg.codex_section()
    ).replace("\n", ending)
    path = reg.codex_config_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_bytes(source.encode())
    monkeypatch.setattr(reg, "deploy_skills", lambda *args: 0)
    monkeypatch.setattr(reg, "deploy_host_skills", lambda *args: 0)
    monkeypatch.setattr(reg, "apply_stored_credential", lambda *args: "none")
    assert reg.upsert_codex(path) is False
    assert "codex:current" in reg.register("codex", home=tmp_path)
    assert path.read_bytes() == source.encode()


@pytest.mark.parametrize(
    "notice",
    [
        "{\n # before, keep this\n other=true,\n}",
        "{\n # before, keep this\n hide_full_access_warning=false,\n}",
        "{\n # x, hide_full_access_warning=false\n}",
    ],
)
def test_toml11_notice_comment_comma_completes_full_registration(tmp_path, monkeypatch, notice):
    import tomli

    path = reg.codex_config_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(
        "suppress_unstable_features_warning=false\nfeatures={\n mcp_2026_07_28=true,\n}\nnotice="
        + notice
    )
    calls = []
    monkeypatch.setattr(reg, "deploy_skills", lambda *args: calls.append("skills") or 0)
    monkeypatch.setattr(reg, "deploy_host_skills", lambda *args: calls.append("host") or 0)
    monkeypatch.setattr(
        reg, "apply_stored_credential", lambda *args: calls.append("credential") or "none"
    )
    try:
        result = reg.register("codex", home=tmp_path)
    except ValueError:
        assert calls == [], "valid comment caused partial registration: " + str(calls)
        raise
    assert result.startswith("codex:written")
    assert calls == ["skills", "host", "credential"]
    body = path.read_text()
    assert tomli.loads(body)["notice"]["hide_full_access_warning"] is True
    _assert_inline_owned_body(body, "notice", notice)
    before = path.read_bytes()
    assert reg.register("codex", home=tmp_path).startswith("codex:current")
    assert path.read_bytes() == before


def test_codex_permission_writer_validates_notice_before_publication(tmp_path, monkeypatch):
    path = reg.codex_config_path(tmp_path)
    path.parent.mkdir(parents=True)
    source = "suppress_unstable_features_warning=false\nnotice={}\n"
    path.write_text(source)
    monkeypatch.setattr(reg, "_upsert_codex_full_access_notice", lambda source: source)
    with pytest.raises(ValueError, match=re.escape(str(path))):
        reg._apply_codex_permission_mode(tmp_path)
    assert path.read_text() == source


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
        "autoUpdates": False,
        "cachedGrowthBookFeatures": {"gate": 1},
        "projects": {"/host/checkout": host_entry},
    }
    _write_claude_config(first_run.home, seed)

    reg.register("claude", home=first_run.home, trust_paths=first_run.keys)

    config = _claude_config(first_run.home)
    for kept in ("hasCompletedOnboarding", "autoUpdates", "cachedGrowthBookFeatures"):
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
        f'[projects."{first_run.root}"]\n'
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


@pytest.mark.parametrize(
    ("existing", "root_gets_trust"),
    [
        ('[projects."{root}"]\nother = 1\n', True),
        ('projects = { "/elsewhere" = { trust_level = "trusted" } }\n', False),
        ('[projects]\n"{root}".other = 1\n', False),
        ('projects."{root}".other = 1\n', True),
    ],
    ids=["table-with-other-key", "inline-table", "dotted-key-in-table", "dotted-root-key"],
)
def test_codex_valid_projects_layouts_never_break_registration(
    first_run, existing, root_gets_trust
):
    path = reg.codex_config_path(first_run.home)
    path.parent.mkdir()
    path.write_text(existing.replace("{root}", first_run.root), encoding="utf-8")

    reg.register("codex", home=first_run.home, trust_paths=first_run.keys)

    projects = _codex_config(first_run.home)["projects"]
    assert ("trust_level" in projects.get(first_run.root, {})) is root_gets_trust


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
