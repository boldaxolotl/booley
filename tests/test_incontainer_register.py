"""Tests for the in-container MCP registrar (ADR 0018 WS4, ADR 0023 HTTP)."""

from __future__ import annotations

import json
import os

from booley.runtime import incontainer_register as reg
from tests.conftest import require_symlinks


def test_main_launches_automatic_doctor_after_server(monkeypatch, capsys):
    monkeypatch.setenv("BOOLEY_AGENT_APP", "claude")
    monkeypatch.setattr(reg, "ensure_http_server", lambda: "started")
    monkeypatch.setattr(reg, "register", lambda _app: "claude:current")
    monkeypatch.setattr(reg, "launch_auto_doctor", lambda: "started")

    reg.main()

    assert "server:started health:started claude:current" in capsys.readouterr().err


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
        assert reg.ensure_http_server() == "running"

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
        assert reg.ensure_http_server(log_path=str(log)) == "started"
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
        assert reg.ensure_http_server(log_path=str(log)) == "failed"

    def test_spawn_oserror_is_failed_not_raised(self, tmp_path, monkeypatch):
        monkeypatch.setattr(reg, "_port_is_serving", lambda port, **kw: False)

        def boom(cmd, **kw):
            raise OSError("no exec")

        monkeypatch.setattr(reg.subprocess, "Popen", boom)
        log = tmp_path / "server.log"
        assert reg.ensure_http_server(log_path=str(log)) == "failed"


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
        monkeypatch.setattr(reg, "ensure_http_server", lambda: "running")
        monkeypatch.setattr(reg, "launch_auto_doctor", lambda: "current")
        reg.main()
        assert reg.claude_config_path(tmp_path).exists()

    def test_main_skips_server_without_app(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("BOOLEY_AGENT_APP", "none")

        def fail(**kw):
            raise AssertionError("must not start a server with no client app")

        monkeypatch.setattr(reg, "ensure_http_server", fail)
        monkeypatch.setattr(reg, "launch_auto_doctor", lambda: "current")
        reg.main()
        assert not list(tmp_path.iterdir())
