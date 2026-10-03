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
    monkeypatch.setattr(reg, "register", lambda _app: "claude:current")
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

    def test_survives_corrupt_json(self, tmp_path):
        path = reg.claude_config_path(tmp_path)
        path.write_text("{not json", encoding="utf-8")
        assert reg.upsert_claude(path) is True
        assert json.loads(path.read_text(encoding="utf-8"))["mcpServers"]["booley"]


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
        from booley.mcp.server import _mcp_tool_visible, _status_mcp_tool_visible

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
            assert not _mcp_tool_visible("submit_run_report")
            assert _status_mcp_tool_visible()

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

    def test_claude_survives_corrupt_settings(self, tmp_path, monkeypatch):
        self._clear_env(monkeypatch)
        self._seed(tmp_path, "claude", "sk-ant-oat01-stored")
        path = reg.claude_settings_path(tmp_path)
        path.parent.mkdir(parents=True)
        path.write_text("{not json")

        assert reg.apply_stored_credential("claude", tmp_path) == "written"
        assert json.loads(path.read_text())["env"]["CLAUDE_CODE_OAUTH_TOKEN"]

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

    def test_survives_corrupt_settings(self, tmp_path):
        path = reg.claude_settings_path(tmp_path)
        path.parent.mkdir(parents=True)
        path.write_text("{ not json")

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
    with pytest.raises(ValueError, match=str(path)):
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
    with pytest.raises(ValueError, match=str(path)):
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
    with pytest.raises(ValueError, match=str(path)) as error:
        reg.upsert_codex(path)
    assert "repair this file and retry" in str(error.value)
    assert path.read_text() == existing


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
        key = "mcp_2026_07_28" if table == "features" else "hide_full_access_warning"
        expected = re.sub(
            rf'^(\s*"?{key}"?\s*=\s*)false\b', r"\1true", original, flags=re.MULTILINE
        )
        assert expected[1:-1] in body
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
    expected = notice.replace(
        "\n hide_full_access_warning=false", "\n hide_full_access_warning=true"
    )
    assert expected[1:-1] in body


def test_codex_permission_writer_validates_notice_before_publication(tmp_path, monkeypatch):
    path = reg.codex_config_path(tmp_path)
    path.parent.mkdir(parents=True)
    source = "suppress_unstable_features_warning=false\nnotice={}\n"
    path.write_text(source)
    monkeypatch.setattr(reg, "_upsert_codex_full_access_notice", lambda source: source)
    with pytest.raises(ValueError, match=str(path)):
        reg._apply_codex_permission_mode(tmp_path)
    assert path.read_text() == source
