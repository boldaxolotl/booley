"""Tests for the long-lived agent token: storage, resolution, and injection.

The invariant under test throughout: an absent token must resolve to ``None`` —
never ``""`` — because an empty value forwarded into a container SHADOWS the
mounted subscription credentials and logs the agent out.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from booley.harness import auth_cmd
from booley.harness import booley as tlr
from booley.runtime import auth_token, issuance_invalidation, session_runtime
from booley.runtime.platform_paths import docker_mount_path

_TOKEN = "sk-ant-oat01-abcdef123456"  # claude: one-year setup-token
_API_KEY = "sk-proj-codex-abcdef123456"  # codex: API key (no setup-token exists)


@pytest.fixture
def home(tmp_path, monkeypatch):
    """Redirect the per-user config dir into a tmp tree, with no auth env leaking in."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.delenv(auth_token.ENV_VAR, raising=False)
    # The credential-resolution code ranks these above everything else, so a
    # developer's real key leaking into the test env would flip every verdict.
    for var in auth_token.API_KEY_ENV.values():
        monkeypatch.delenv(var, raising=False)
    return tmp_path


@pytest.fixture
def isolated_home(home, monkeypatch):
    """Also redirect ``Path.home()`` so the real ``~/.claude`` login can't leak in."""
    monkeypatch.setenv("HOME", str(home / "userhome"))
    monkeypatch.setenv("USERPROFILE", str(home / "userhome"))
    (home / "userhome").mkdir(exist_ok=True)
    return home / "userhome"


class TestStorage:
    @pytest.mark.skipif(os.name == "nt", reason="Windows ACLs are not POSIX mode bits")
    def test_stores_token_private_to_owner(self, home):
        path = auth_token.store_token(_TOKEN)

        assert path.read_text().strip() == _TOKEN
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
        assert auth_token.stored_token_is_private()

    def test_lives_outside_any_repo(self, home):
        # A one-year credential inside the Project Root is one `git add -f` from
        # being published, so its host-side source must not land under the workspace.
        assert "booley" in auth_token.token_path().parts[-2]
        assert auth_token.token_path().name == "claude-oauth-token"

    @pytest.mark.skipif(os.name == "nt", reason="Windows ACLs are not POSIX mode bits")
    def test_rewrite_tightens_a_loose_mode(self, home):
        path = auth_token.store_token(_TOKEN)
        path.chmod(0o644)
        assert not auth_token.stored_token_is_private()

        auth_token.store_token(_TOKEN)

        assert stat.S_IMODE(path.stat().st_mode) == 0o600

    @pytest.mark.parametrize("bad", ["", "   ", "\n"])
    def test_refuses_empty(self, home, bad):
        with pytest.raises(ValueError):
            auth_token.store_token(bad)

    def test_refuses_embedded_whitespace(self, home):
        # A newline inside the value would corrupt every following line of the
        # docker --env-file it gets written into.
        with pytest.raises(ValueError, match="whitespace"):
            auth_token.store_token("sk-ant-oat01-aaa\nBAR=evil")

    def test_clear_removes_it(self, home):
        auth_token.store_token(_TOKEN)

        assert auth_token.clear_stored_token() is True
        assert auth_token.read_stored_token() is None
        assert auth_token.clear_stored_token() is False  # idempotent


class TestCodex:
    """Codex needs the same mechanism: its auth.json also holds a REFRESHING
    credential when signed in with a ChatGPT subscription, and Codex rewrites it."""

    def test_codex_credential_is_the_openai_api_key(self):
        cred = auth_token.CREDENTIALS[auth_token.APP_CODEX]

        assert cred.env_var == "OPENAI_API_KEY"
        # Codex has no `setup-token` equivalent: `codex login` writes the very
        # refreshing credential we are avoiding, so there is nothing to mint.
        assert cred.mint_cmd is None

    def test_apps_have_separate_stores(self, home):
        auth_token.store_token(_TOKEN, auth_token.APP_CLAUDE)
        auth_token.store_token(_API_KEY, auth_token.APP_CODEX)

        assert auth_token.resolve_token(auth_token.APP_CLAUDE) == _TOKEN
        assert auth_token.resolve_token(auth_token.APP_CODEX) == _API_KEY
        assert auth_token.token_path(auth_token.APP_CLAUDE) != auth_token.token_path(
            auth_token.APP_CODEX
        )

    def test_clearing_one_leaves_the_other(self, home):
        auth_token.store_token(_TOKEN, auth_token.APP_CLAUDE)
        auth_token.store_token(_API_KEY, auth_token.APP_CODEX)

        auth_token.clear_stored_token(auth_token.APP_CODEX)

        assert auth_token.resolve_token(auth_token.APP_CLAUDE) == _TOKEN
        assert auth_token.resolve_token(auth_token.APP_CODEX) is None

    def test_env_var_reverse_lookup(self):
        assert auth_token.credential_for_env_var("OPENAI_API_KEY").app == auth_token.APP_CODEX
        assert (
            auth_token.credential_for_env_var("CLAUDE_CODE_OAUTH_TOKEN").app
            == auth_token.APP_CLAUDE
        )
        assert auth_token.credential_for_env_var("PATH") is None

    def test_spec_seeds_codex_auth_onto_writable_volume(self, home):
        # The regression: a read-only bind at ~/.codex/auth.json makes Codex's
        # own refresh write FAIL, 401ing the session once the token expires.
        from booley.runtime import devcontainer as dc

        spec = dc.build_devcontainer_spec(
            dc.APP_CODEX, auth_token_source="/home/u/.codex/auth.json"
        )

        assert all(f"target={dc.AGENT_HOME}/.codex/auth.json" not in m for m in spec["mounts"])
        assert f"{dc.AGENT_HOME}/.codex-auth-seed.json" in spec["postStartCommand"]

    def test_session_runtime_resolves_codex_key_from_store(self, home):
        auth_token.store_token(_API_KEY, auth_token.APP_CODEX)

        assert session_runtime.substitute("${localEnv:OPENAI_API_KEY}", home) == _API_KEY


class TestResolution:
    def test_absent_resolves_to_none_not_empty_string(self, home):
        # The whole point: "" would shadow the mounted subscription creds.
        assert auth_token.resolve_oauth_token() is None

    def test_stored_token_resolves_without_any_export(self, home):
        auth_token.store_token(_TOKEN)

        assert auth_token.resolve_oauth_token() == _TOKEN

    def test_exported_token_wins_over_stored(self, home, monkeypatch):
        auth_token.store_token(_TOKEN)
        monkeypatch.setenv(auth_token.ENV_VAR, "sk-ant-oat01-exported")

        assert auth_token.resolve_oauth_token() == "sk-ant-oat01-exported"

    def test_blank_export_does_not_shadow_stored(self, home, monkeypatch):
        auth_token.store_token(_TOKEN)
        monkeypatch.setenv(auth_token.ENV_VAR, "   ")

        assert auth_token.resolve_oauth_token() == _TOKEN

    def test_container_seed_sidecar_fills_the_gap(self, isolated_home):
        # In-container `booley auth` storage lives on the HOST; the spec mounts
        # it read-only at a home sidecar. read_stored_token must see it there.
        seed = isolated_home / auth_token.TOKEN_SEED_BASENAME[auth_token.APP_CLAUDE]
        seed.write_text(f"{_TOKEN}\n")

        assert auth_token.resolve_oauth_token() == _TOKEN

    def test_config_store_wins_over_seed_sidecar(self, isolated_home):
        seed = isolated_home / auth_token.TOKEN_SEED_BASENAME[auth_token.APP_CLAUDE]
        seed.write_text("sk-ant-oat01-seed\n")
        auth_token.store_token(_TOKEN)

        assert auth_token.resolve_oauth_token() == _TOKEN


class TestEffectiveCredential:
    """`effective_credential` models the agent CLI's real precedence: an exported
    API key outranks the rotation-free token, which outranks the refreshing
    login file. `booley init` used to answer from the login file alone and
    reported "subscription" while an exported ANTHROPIC_API_KEY was what billed."""

    @staticmethod
    def _write_claude_login(userhome: Path) -> Path:
        creds = userhome / ".claude" / ".credentials.json"
        creds.parent.mkdir(parents=True, exist_ok=True)
        creds.write_text("{}", encoding="utf-8")
        return creds

    def test_nothing_at_all_resolves_to_none(self, isolated_home):
        assert auth_token.effective_credential(auth_token.APP_CLAUDE) is None

    def test_login_file_alone_is_subscription_and_not_rotation_free(self, isolated_home):
        self._write_claude_login(isolated_home)

        eff = auth_token.effective_credential(auth_token.APP_CLAUDE)

        assert eff.mode == auth_token.MODE_SUBSCRIPTION
        assert not eff.rotation_free
        assert eff.overridden == ()

    def test_api_key_outranks_the_subscription_login(self, isolated_home, monkeypatch):
        # The original misreport: both present used to be called "subscription".
        self._write_claude_login(isolated_home)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-x")

        eff = auth_token.effective_credential(auth_token.APP_CLAUDE)

        assert eff.mode == auth_token.MODE_API_KEY
        assert eff.rotation_free
        assert "ANTHROPIC_API_KEY" in eff.source
        assert any(".credentials.json" in loser for loser in eff.overridden)

    def test_api_key_outranks_the_stored_oauth_token(self, isolated_home, monkeypatch):
        auth_token.store_token(_TOKEN)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-x")

        eff = auth_token.effective_credential(auth_token.APP_CLAUDE)

        assert eff.mode == auth_token.MODE_API_KEY
        assert any("stored" in loser for loser in eff.overridden)

    def test_oauth_token_outranks_the_subscription_login(self, isolated_home):
        self._write_claude_login(isolated_home)
        auth_token.store_token(_TOKEN)

        eff = auth_token.effective_credential(auth_token.APP_CLAUDE)

        assert eff.mode == auth_token.MODE_OAUTH_TOKEN
        assert eff.rotation_free
        assert any(".credentials.json" in loser for loser in eff.overridden)

    def test_codex_stored_key_is_api_key_mode(self, isolated_home):
        # For Codex the API key IS the rotation-free credential — one ladder rung.
        auth_token.store_token(_API_KEY, auth_token.APP_CODEX)

        eff = auth_token.effective_credential(auth_token.APP_CODEX)

        assert eff.mode == auth_token.MODE_API_KEY
        assert eff.rotation_free

    def test_unknown_app_resolves_to_none(self, isolated_home):
        assert auth_token.effective_credential("not-an-app") is None


class TestAuthStatusReport:
    """`booley auth --status` must describe what actually runs. With an API key
    exported it used to say "no rotation-free credential — falls back to a
    refreshing one": doubly wrong (the key is what runs, and keys never refresh)."""

    @staticmethod
    def _run(monkeypatch):
        from booley.harness import auth_cmd

        sink: dict[str, list[str]] = {"ok": [], "info": [], "warn": []}
        monkeypatch.setattr(auth_cmd, "banner", lambda *a, **k: None)
        for level, messages in sink.items():
            monkeypatch.setattr(auth_cmd, level, messages.append)
        rc = auth_cmd._report_status()
        return rc, sink

    def test_api_key_alone_is_reported_as_what_runs(self, isolated_home, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-x")

        _rc, sink = self._run(monkeypatch)

        assert any("claude" in m and "ANTHROPIC_API_KEY" in m for m in sink["ok"])
        assert not any("claude" in m for m in sink["warn"])

    def test_subscription_only_still_warns_about_rotation(self, isolated_home, monkeypatch):
        creds = isolated_home / ".claude" / ".credentials.json"
        creds.parent.mkdir(parents=True)
        creds.write_text("{}", encoding="utf-8")

        rc, sink = self._run(monkeypatch)

        assert rc == 0  # a refreshing login is usable despite its rotation warning
        assert any("claude" in m and "refreshing" in m for m in sink["warn"])

    def test_overridden_subscription_is_named(self, isolated_home, monkeypatch):
        creds = isolated_home / ".claude" / ".credentials.json"
        creds.parent.mkdir(parents=True)
        creds.write_text("{}", encoding="utf-8")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-x")

        _rc, sink = self._run(monkeypatch)

        assert any(".credentials.json" in m and "NOT used" in m for m in sink["info"])


class TestInjection:
    def test_session_runtime_resolves_localenv_from_stored_token(self, home):
        auth_token.store_token(_TOKEN)

        resolved = session_runtime.substitute("${localEnv:CLAUDE_CODE_OAUTH_TOKEN}", home)

        assert resolved == _TOKEN

    def test_session_runtime_leaves_other_vars_empty_when_unset(self, home):
        # Only the token var gets the file fallback; everything else keeps the
        # Dev Containers CLI's empty-string semantics.
        assert session_runtime.substitute("${localEnv:NOT_A_TOKEN}", home) == ""

    def test_session_runtime_token_empty_when_nothing_stored(self, home):
        assert session_runtime.substitute("${localEnv:CLAUDE_CODE_OAUTH_TOKEN}", home) == ""

    def test_sandbox_injects_stored_token(self, home, monkeypatch):
        from booley.harness import sandbox

        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        auth_token.store_token(_TOKEN)

        cmd: list[str] = []
        runner = sandbox.DockerRunner.__new__(sandbox.DockerRunner)
        runner.worktree = home / "project"
        runner.worktree.mkdir()
        runner._append_api_key_env_file(cmd)

        assert "--env-file" in cmd
        env_file = Path(cmd[cmd.index("--env-file") + 1])
        body = env_file.read_text(encoding="utf-8")
        assert f"CLAUDE_CODE_OAUTH_TOKEN={_TOKEN}" in body


class TestDoctorCheck:
    """The check that would have caught the 40-task void run before it started."""

    @staticmethod
    def _run(monkeypatch, *, claude=True, codex=False, provider="claude"):
        from booley.harness import doctor

        monkeypatch.setattr(
            doctor.host_environment,
            "inspect_agent_installation",
            lambda provider: doctor.host_environment.AgentInstallation(
                claude if provider == "claude" else codex, None
            ),
        )
        sink: dict[str, list[str]] = {"pass": [], "warn": [], "skip": []}
        doctor._check_oauth_token(
            provider,
            sink["pass"].append,
            lambda msg, fix=None: sink["warn"].append(msg),
            sink["skip"].append,
        )
        return sink

    def test_warns_when_only_rotating_creds(self, home, monkeypatch):
        sink = self._run(monkeypatch)

        assert sink["pass"] == []
        assert any("revoke" in m for m in sink["warn"])

    def test_passes_with_stored_token(self, home, monkeypatch):
        auth_token.store_token(_TOKEN)

        sink = self._run(monkeypatch)

        assert sink["warn"] == []
        assert any("rotation-free" in m for m in sink["pass"])

    def test_exported_api_key_satisfies_the_check(self, home, monkeypatch):
        # An API key never rotates AND outranks everything else, so warning
        # "no rotation-free credential" (and steering to `booley auth`, whose
        # token the key would override) was doubly wrong.
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-x")

        sink = self._run(monkeypatch)

        assert sink["warn"] == []
        assert any("API key" in m and "rotation-free" in m for m in sink["pass"])

    def test_api_key_pass_names_the_outranked_stored_token(self, home, monkeypatch):
        auth_token.store_token(_TOKEN)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-x")

        sink = self._run(monkeypatch)

        assert any("outranks" in m for m in sink["pass"])

    @pytest.mark.skipif(os.name == "nt", reason="Windows ACLs are not POSIX mode bits")
    def test_warns_when_stored_token_is_world_readable(self, home, monkeypatch):
        auth_token.store_token(_TOKEN).chmod(0o644)

        sink = self._run(monkeypatch)

        assert any("readable by others" in m for m in sink["warn"])

    def test_codex_is_checked_on_a_codex_project(self, home, monkeypatch):
        # Claude covered, Codex not: on a codex project the warning is Codex's.
        auth_token.store_token(_TOKEN, auth_token.APP_CLAUDE)

        sink = self._run(monkeypatch, claude=True, codex=True, provider="codex")

        assert any("Codex" in m for m in sink["warn"])
        assert not any("Claude" in m for m in sink["warn"])

    def test_codex_passes_with_stored_api_key(self, home, monkeypatch):
        auth_token.store_token(_API_KEY, auth_token.APP_CODEX)

        sink = self._run(monkeypatch, claude=True, codex=True, provider="codex")

        assert sink["warn"] == []
        assert len(sink["pass"]) == 1

    def test_skips_when_the_selected_app_is_absent(self, home, monkeypatch):
        sink = self._run(monkeypatch, claude=False, codex=False)

        assert sink["skip"] and not sink["warn"]


class TestDoctorCheckIsProviderGated:
    """Booley runs ONE provider. Merely having the other app's config dir on the
    host (`~/.codex` from an unrelated experiment) used to buy a warning about
    credentials no Booley run would ever read -- unactionable noise that erodes
    trust in the warning that IS real."""

    def test_unused_provider_is_not_audited(self, home, monkeypatch):
        # Both apps installed; only Claude is configured and covered.
        auth_token.store_token(_TOKEN, auth_token.APP_CLAUDE)

        sink = TestDoctorCheck._run(monkeypatch, claude=True, codex=True, provider="claude")

        assert sink["warn"] == []
        assert len(sink["pass"]) == 1
        assert "Claude" in sink["pass"][0]

    def test_configured_provider_still_warns(self, home, monkeypatch):
        # The kept warning: it names a real failure mode with a real remedy.
        sink = TestDoctorCheck._run(monkeypatch, claude=True, codex=True, provider="claude")

        assert any("Claude" in m and "revoke" in m for m in sink["warn"])
        assert not any("Codex" in m for m in sink["warn"])

    def test_skip_names_the_configured_provider(self, home, monkeypatch):
        # Codex configured but never installed: say so, don't claim "no agent app".
        sink = TestDoctorCheck._run(monkeypatch, claude=True, codex=False, provider="codex")

        assert sink["warn"] == [] and sink["pass"] == []
        assert any("codex" in m and "not installed" in m for m in sink["skip"])


class TestConfiguredProvider:
    """Where the provider gate reads its answer from."""

    @staticmethod
    def _audit(tmp_path, agent_section):
        from booley.harness import doctor

        return doctor.ProjectAudit(
            project_root=tmp_path,
            booley_dir=tmp_path / ".booley",
            booley_toml={"agent": agent_section} if agent_section is not None else {},
            configs_toml={},
            first_target="",
        )

    def test_reads_booley_toml(self, tmp_path, monkeypatch):
        from booley.harness import doctor

        monkeypatch.delenv("BOOLEY_PRIMARY_PROVIDER", raising=False)
        assert doctor._configured_provider(self._audit(tmp_path, {"provider": "codex"})) == "codex"

    def test_env_wins_over_toml(self, tmp_path, monkeypatch):
        """Mirrors the backend's own resolution order: env first."""
        from booley.harness import doctor

        monkeypatch.setenv("BOOLEY_PRIMARY_PROVIDER", "codex")
        assert (
            doctor._configured_provider(self._audit(tmp_path, {"provider": "claude"})) == "codex"
        )

    @pytest.mark.parametrize("section", [None, {}, {"provider": "bogus"}, "not-a-table"])
    def test_undeclared_provider_is_the_backend_default(self, tmp_path, monkeypatch, section):
        # F-23: an absent, empty, or invalid [agent] does NOT mean "audit every
        # app" — the backend's own host fallback is claude, so a stale ~/.codex
        # login must not buy the project permanent codex warnings.
        from booley.harness import doctor

        monkeypatch.delenv("BOOLEY_PRIMARY_PROVIDER", raising=False)
        monkeypatch.delenv("BOOLEY_AGENT_APP", raising=False)
        assert doctor._configured_provider(self._audit(tmp_path, section)) == "claude"

    def test_no_project_is_the_backend_default(self, monkeypatch):
        from booley.harness import doctor

        monkeypatch.delenv("BOOLEY_PRIMARY_PROVIDER", raising=False)
        monkeypatch.delenv("BOOLEY_AGENT_APP", raising=False)
        assert doctor._configured_provider(None) == "claude"

    def test_devcontainer_app_env_beats_the_default(self, tmp_path, monkeypatch):
        """Third resolution step, same as ``config.agent._lazy_agent_settings``:
        in-container the devcontainer exports which app runs the session."""
        from booley.harness import doctor

        monkeypatch.delenv("BOOLEY_PRIMARY_PROVIDER", raising=False)
        monkeypatch.setenv("BOOLEY_AGENT_APP", "codex")
        assert doctor._configured_provider(self._audit(tmp_path, {})) == "codex"

    def test_toml_beats_the_devcontainer_app_env(self, tmp_path, monkeypatch):
        from booley.harness import doctor

        monkeypatch.delenv("BOOLEY_PRIMARY_PROVIDER", raising=False)
        monkeypatch.setenv("BOOLEY_AGENT_APP", "codex")
        audit = self._audit(tmp_path, {"provider": "claude"})
        assert doctor._configured_provider(audit) == "claude"


class TestDeclaredProvider:
    """``_declared_provider`` backs the devcontainer app-drift check, so unlike
    ``_configured_provider`` it must report *only* what booley.toml declares —
    that is the sole input ``init_cmd._select_interactive_app`` re-seeds from."""

    _audit = staticmethod(TestConfiguredProvider._audit)

    def test_reads_booley_toml(self, tmp_path):
        from booley.harness import doctor

        assert doctor._declared_provider(self._audit(tmp_path, {"provider": "codex"})) == "codex"

    def test_ignores_the_retired_primary_alias(self, tmp_path):
        from booley.harness import doctor

        assert doctor._declared_provider(self._audit(tmp_path, {"primary": "codex"})) is None

    @pytest.mark.parametrize("section", [None, {}, "not-a-table"])
    def test_undeclared_is_none_not_a_default(self, tmp_path, section):
        # A default here would drift-check the spec against a provider the
        # project never chose, warning on every correctly-seeded claude repo.
        from booley.harness import doctor

        assert doctor._declared_provider(self._audit(tmp_path, section)) is None

    def test_invalid_value_degrades_to_none(self, tmp_path):
        # Fail-soft (Decision 12): _validate_agent_table already FAILs on this.
        from booley.harness import doctor

        assert doctor._declared_provider(self._audit(tmp_path, {"provider": "bogus"})) is None

    def test_ignores_env(self, tmp_path, monkeypatch):
        # Env vars steer which backend *runs*; they never steer what `booley
        # init --seed` writes, so they must not move this answer.
        from booley.harness import doctor

        monkeypatch.setenv("BOOLEY_PRIMARY_PROVIDER", "claude")
        monkeypatch.setenv("BOOLEY_AGENT_APP", "claude")
        assert doctor._declared_provider(self._audit(tmp_path, {"provider": "codex"})) == "codex"
        assert doctor._declared_provider(self._audit(tmp_path, {})) is None

    def test_no_project_is_none(self):
        from booley.harness import doctor

        assert doctor._declared_provider(None) is None


class TestUnusedProviderIsSilent:
    """F-23: a claude project with a stale ~/.codex login used to carry two
    permanent codex WARNs — noise for credentials no Booley run would read."""

    def test_no_codex_warnings_when_provider_is_undeclared(self, home, monkeypatch, tmp_path):
        from booley.harness import doctor

        monkeypatch.delenv("BOOLEY_PRIMARY_PROVIDER", raising=False)
        monkeypatch.delenv("BOOLEY_AGENT_APP", raising=False)
        monkeypatch.setattr(
            doctor.host_environment,
            "inspect_agent_installation",
            lambda _provider: doctor.host_environment.AgentInstallation(True, None),
        )  # Both providers have installation state, but only the selected one is audited.
        auth_token.store_token(_TOKEN, auth_token.APP_CLAUDE)
        audit = TestConfiguredProvider._audit(tmp_path, {})  # [agent] without provider
        provider = doctor._configured_provider(audit)

        warns: list[str] = []
        for check in (doctor._check_agent_auth_token, doctor._check_oauth_token):
            check(
                provider,
                lambda _msg: None,
                lambda msg, fix=None: warns.append(msg),
                lambda _msg: None,
            )

        assert warns == []


class TestCli:
    def test_auth_is_host_only(self):
        # It drives the host browser flow and writes the host's ~/.config.
        assert "auth" in tlr._HOST_ONLY_COMMANDS

    @pytest.mark.parametrize(
        "argv,flag",
        [
            (["auth"], None),
            (["auth", "--status"], "status"),
            (["auth", "--clear"], "clear"),
            (["auth", "--token-stdin"], "token_stdin"),
        ],
    )
    def test_parses_flags(self, argv, flag):
        parser = tlr._build_parser()

        args = tlr._normalize_args(parser, parser.parse_args(argv))

        assert args.command == "auth"
        if flag:
            assert getattr(args, flag) is True

    def test_dispatches_to_run_auth(self):
        from booley.harness.auth_cmd import run_auth

        assert tlr._EARLY_COMMANDS["auth"] is run_auth


class TestClaudeMintLocation:
    def test_project_mint_uses_runtime_without_host_cli(self, tmp_path, monkeypatch):
        project = tmp_path / "rtl"
        project.mkdir()
        runtime = Mock(return_value=0)
        monkeypatch.setattr(session_runtime, "run_project_command", runtime)
        monkeypatch.setattr(
            auth_cmd.shutil,
            "which",
            Mock(side_effect=AssertionError("host PATH must not decide Project auth")),
        )
        monkeypatch.setattr(auth_cmd, "_prompt_secret", lambda: _TOKEN)

        with (
            patch.object(auth_cmd.sys.stdin, "isatty", return_value=True),
            patch.object(auth_cmd.sys.stdout, "isatty", return_value=True),
        ):
            token = auth_cmd._mint_claude_token(
                auth_token.CREDENTIALS[auth_token.APP_CLAUDE], project
            )

        assert token == _TOKEN
        runtime.assert_called_once_with(project, ["claude", "setup-token"], tty=True)

    def test_runtime_failure_is_actionable_and_does_not_prompt(self, tmp_path, monkeypatch):
        project = tmp_path / "rtl"
        project.mkdir()
        prompt = Mock()
        errors: list[str] = []
        monkeypatch.setattr(
            session_runtime,
            "run_project_command",
            Mock(side_effect=session_runtime.SessionError("cannot inventory runtimes")),
        )
        monkeypatch.setattr(auth_cmd, "_prompt_secret", prompt)
        monkeypatch.setattr(auth_cmd, "err", errors.append)

        with pytest.raises(session_runtime.SessionError, match="cannot inventory runtimes"):
            auth_cmd._mint_claude_token(auth_token.CREDENTIALS[auth_token.APP_CLAUDE], project)
        prompt.assert_not_called()
        assert errors == []

    def test_run_auth_returns_two_for_sandbox_start_refusal(self, auth_project, monkeypatch):
        errors: list[str] = []
        monkeypatch.setattr(auth_cmd, "banner", lambda *_args: None)
        monkeypatch.setattr(auth_cmd, "err", errors.append)
        monkeypatch.setattr(
            auth_cmd,
            "_acquire",
            Mock(side_effect=session_runtime.SessionError("Sandbox start refused: at cap")),
        )
        args = SimpleNamespace(
            status=False,
            clear=False,
            app=auth_token.APP_CLAUDE,
            token_stdin=False,
        )

        assert auth_cmd.run_auth(args, auth_project) == 2
        assert errors == [
            "could not run `claude setup-token` in the Sandbox: Sandbox start refused: at cap"
        ]

    @pytest.mark.skipif(os.name != "posix", reason="POSIX mode validation")
    def test_run_auth_reports_unsafe_private_store(
        self,
        home: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        project = home / "rtl"
        project.mkdir()
        issuance_invalidation.prepare(str(project.resolve()))
        issuance_invalidation.cancel(str(project.resolve()))
        booley_config = home / "config" / "booley"
        booley_config.chmod(0o755)
        args = SimpleNamespace(
            status=False,
            clear=False,
            app=auth_token.APP_CLAUDE,
            token_stdin=False,
        )

        assert auth_cmd.run_auth(args, project) == 2

        captured = capsys.readouterr()
        assert "credential store" in captured.out
        assert str(booley_config) in captured.out
        assert "0755" in captured.out
        assert f"chmod 700 {booley_config}" in captured.out
        assert "Traceback" not in captured.out
        assert "Traceback" not in captured.err

    def test_non_project_mint_keeps_host_cli_fallback(self, tmp_path, monkeypatch):
        run = Mock(return_value=SimpleNamespace(returncode=0))
        runtime = Mock()
        monkeypatch.setattr(auth_cmd.shutil, "which", lambda _command: "/host/bin/claude")
        monkeypatch.setattr(auth_cmd.subprocess, "run", run)
        monkeypatch.setattr(session_runtime, "run_project_command", runtime)
        monkeypatch.setattr(auth_cmd, "_prompt_secret", lambda: _TOKEN)

        token = auth_cmd._mint_claude_token(auth_token.CREDENTIALS[auth_token.APP_CLAUDE], None)

        assert token == _TOKEN
        run.assert_called_once_with(("claude", "setup-token"), check=False)
        runtime.assert_not_called()

    def test_missing_host_cli_names_project_runtime_option(self, tmp_path, monkeypatch):
        errors: list[str] = []
        monkeypatch.setattr(auth_cmd.shutil, "which", lambda _command: None)
        monkeypatch.setattr(auth_cmd, "err", errors.append)

        token = auth_cmd._mint_claude_token(auth_token.CREDENTIALS[auth_token.APP_CLAUDE], None)

        assert token is None
        assert "initialized Booley Project" in errors[0]
        assert "Sandbox" in errors[0]

    def test_failed_host_mint_reports_exit_and_does_not_prompt(self, tmp_path, monkeypatch):
        prompt = Mock()
        errors: list[str] = []
        monkeypatch.setattr(auth_cmd.shutil, "which", lambda _command: "/host/bin/claude")
        monkeypatch.setattr(
            auth_cmd.subprocess,
            "run",
            Mock(return_value=SimpleNamespace(returncode=7)),
        )
        monkeypatch.setattr(auth_cmd, "_prompt_secret", prompt)
        monkeypatch.setattr(auth_cmd, "err", errors.append)

        token = auth_cmd._mint_claude_token(auth_token.CREDENTIALS[auth_token.APP_CLAUDE], None)

        assert token is None
        prompt.assert_not_called()
        assert errors == ["`claude setup-token` failed (exit 7)"]

    def test_token_stdin_never_inspects_or_starts_a_runtime(self, tmp_path, monkeypatch):
        monkeypatch.setattr(auth_cmd.sys, "stdin", SimpleNamespace(read=lambda: f"{_TOKEN}\n"))
        project_probe = Mock(side_effect=AssertionError("runtime must not be inspected"))
        monkeypatch.setattr(session_runtime, "run_project_command", project_probe)

        token = auth_cmd._acquire(auth_token.CREDENTIALS[auth_token.APP_CLAUDE], True, tmp_path)

        assert token == _TOKEN
        project_probe.assert_not_called()

    def test_interactive_acquire_uses_mint_path(self, tmp_path, monkeypatch):
        mint = Mock(return_value=_TOKEN)
        monkeypatch.setattr(auth_cmd, "_mint_claude_token", mint)
        credential = auth_token.CREDENTIALS[auth_token.APP_CLAUDE]

        assert auth_cmd._acquire(credential, False, tmp_path) == _TOKEN
        mint.assert_called_once_with(credential, tmp_path)

    def test_non_project_reseed_is_a_successful_noop(self, tmp_path, monkeypatch):

        assert auth_cmd._reseed_spec(None)

    def test_project_reseed_reports_narrow_reseed_result(self, tmp_path, monkeypatch):
        from booley.harness import init_cmd

        reseed = Mock(return_value=0)
        monkeypatch.setattr(init_cmd, "reseed_auth_spec", reseed)
        assert auth_cmd._reseed_spec(tmp_path)
        reseed.assert_called_once_with(tmp_path)

    def test_store_rejects_invalid_credential(self, tmp_path, monkeypatch):
        errors: list[str] = []
        monkeypatch.setattr(
            auth_token,
            "store_token",
            Mock(side_effect=ValueError("credential is empty")),
        )
        monkeypatch.setattr(auth_cmd, "err", errors.append)

        stored = auth_cmd._store_and_reseed(
            "invalid", auth_token.CREDENTIALS[auth_token.APP_CLAUDE], None
        )

        assert stored is None
        assert errors == ["credential is empty"]

    def test_store_returns_path_after_successful_reseed(self, tmp_path, monkeypatch):
        stored = tmp_path / "claude-oauth-token"
        monkeypatch.setattr(auth_token, "store_token", Mock(return_value=stored))
        monkeypatch.setattr(auth_cmd, "_reseed_spec", Mock(return_value=True))

        assert (
            auth_cmd._store_and_reseed(
                _TOKEN, auth_token.CREDENTIALS[auth_token.APP_CLAUDE], tmp_path
            )
            == stored
        )

    def test_reseed_failure_keeps_stored_token_but_returns_failure(self, home, monkeypatch):
        tmp_path = home
        stored = tmp_path / "config" / "booley" / "claude-oauth-token"
        errors: list[str] = []
        monkeypatch.setattr(auth_cmd, "banner", lambda *_args: None)
        monkeypatch.setattr(auth_cmd, "ok", lambda *_args: None)
        monkeypatch.setattr(auth_cmd, "info", lambda *_args: None)
        monkeypatch.setattr(auth_cmd, "err", errors.append)
        monkeypatch.setattr(auth_cmd, "_acquire", lambda *_args: _TOKEN)
        monkeypatch.setattr(auth_token, "store_token", Mock(return_value=stored))
        monkeypatch.setattr(auth_cmd, "_reseed_spec", Mock(return_value=False))
        args = SimpleNamespace(
            status=False,
            clear=False,
            app=auth_token.APP_CLAUDE,
            token_stdin=False,
        )

        rc = auth_cmd.run_auth(args, None)

        assert rc == 1
        auth_token.store_token.assert_called_once_with(_TOKEN, auth_token.APP_CLAUDE)
        assert errors == [
            "credential was stored, but the Project Sandbox spec could not be re-seeded; "
            "resolve the condition reported above before retrying"
        ]

    def test_clear_reseed_failure_returns_failure(self, tmp_path, monkeypatch):
        errors: list[str] = []
        monkeypatch.setattr(auth_token, "clear_stored_token", lambda _app: True)
        monkeypatch.setattr(auth_token, "token_path", lambda _app: tmp_path / "stored-token")
        monkeypatch.setattr(auth_cmd, "banner", lambda *_args: None)
        monkeypatch.setattr(auth_cmd, "ok", lambda *_args: None)
        monkeypatch.setattr(auth_cmd, "info", lambda *_args: None)
        monkeypatch.setattr(auth_cmd, "err", errors.append)
        monkeypatch.setattr(auth_cmd, "_reseed_spec", Mock(return_value=False))

        rc = auth_cmd._clear(auth_token.APP_CLAUDE, tmp_path)

        assert rc == 1
        assert "credential was removed" in errors[0]


@pytest.mark.parametrize(
    "payload,valid",
    [
        (
            {
                "tokens": {"access_token": "SYNTHETIC"},
                "OPENAI_API_KEY": None,
                "last_refresh": "ordinary metadata",
            },
            True,
        ),
        ({"OPENAI_API_KEY": "SYNTHETIC"}, False),
        ({"tokens": {"access_token": "SYNTHETIC"}, "OPENAI_API_KEY": "SYNTHETIC"}, False),
        ({}, False),
        ([], False),
    ],
)
def test_codex_explicit_subscription_classifies_native_shape(isolated_home, payload, valid):
    import json

    path = auth_token.subscription_creds_path("codex")
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(payload))
    selected = auth_token.effective_credential("codex", "subscription")
    assert (selected is not None) == valid


def test_plan_repr_never_contains_credential(isolated_home, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "SYNTHETIC_SECRET")
    plan = auth_token.credential_plan("claude", "api_key")
    assert "SYNTHETIC_SECRET" not in repr(plan)
    assert plan.child_environment({"CLAUDE_CODE_OAUTH_TOKEN": "FORBIDDEN"}) == {
        "ANTHROPIC_API_KEY": "SYNTHETIC_SECRET"
    }


def test_codex_auth_refresh_preserves_complete_copy_on_interruption(tmp_path, monkeypatch):
    import shutil

    source = tmp_path / "source.json"
    destination = tmp_path / "auth.json"
    source.write_text('{"OPENAI_API_KEY":"NEW_SYNTHETIC"}')
    previous = b'{"OPENAI_API_KEY":"OLD_SYNTHETIC"}'
    destination.write_bytes(previous)

    def interrupted_copy(source, target, *args, **kwargs):
        Path(target).write_text('{"OPENAI_API_KEY":')
        raise OSError("synthetic interrupted copy")

    monkeypatch.setattr(shutil, "copyfile", interrupted_copy)
    with pytest.raises(OSError, match="interrupted"):
        auth_token.copy_codex_auth(source, destination)
    assert destination.read_bytes() == previous
    assert sorted(path.name for path in tmp_path.iterdir()) == ["auth.json", "source.json"]


def test_codex_subscription_plan_uses_cli_home(isolated_home, monkeypatch, tmp_path):
    default = auth_token.subscription_creds_path("codex")
    default.parent.mkdir(parents=True)
    default.write_text('{"tokens":{"access_token":"SYNTHETIC_LOGIN"}}')
    alternate = tmp_path / "alternate"
    alternate.mkdir()
    (alternate / "auth.json").write_text('{"OPENAI_API_KEY":"SYNTHETIC_KEY"}')
    monkeypatch.setenv("CODEX_HOME", str(alternate))
    assert auth_token.credential_plan("codex", "subscription").selected is None
    (alternate / "auth.json").write_text(default.read_text())
    plan = auth_token.credential_plan("codex", "subscription")
    assert plan.subscription_seed == alternate / "auth.json"
    assert plan.selected is not None
    assert (
        auth_token.subscription_creds_path("codex", home=tmp_path)
        == tmp_path / ".codex" / "auth.json"
    )


def _linked_auth(tmp_path, monkeypatch, *, data=True):
    from tests.harness.test_session_runtime import _linked_project

    root, linked = _linked_project(tmp_path, monkeypatch)
    if data:
        (linked / ".booley").mkdir()
    effects = []

    def effect(name, value=None):
        def call(*args, **kwargs):
            effects.append(name)
            return value

        return call

    for name, value in [
        ("_acquire", _TOKEN),
        ("_read_token_from_stdin", _TOKEN),
        ("_prompt_secret", _TOKEN),
        ("_mint_claude_token", _TOKEN),
        ("_reseed_spec", True),
    ]:
        monkeypatch.setattr(auth_cmd, name, effect(name, value))
    for name, value in [
        ("store_token", tmp_path / "token"),
        ("clear_stored_token", True),
        ("read_stored_token", None),
    ]:
        monkeypatch.setattr(auth_token, name, effect(name, value))
    return root, linked, effects


@pytest.mark.parametrize("mode", ["mint", "token-stdin", "clear"])
@pytest.mark.parametrize(
    "selection",
    ["implicit-with-data", "implicit-without-data", "explicit-with-data", "explicit-without-data"],
)
def test_cli_linked_auth_refuses_before_required_guard(
    tmp_path, monkeypatch, capsys, selection, mode
):
    import sys

    _, linked, effects = _linked_auth(tmp_path, monkeypatch, data="without" not in selection)
    flags = {"mint": [], "token-stdin": ["--token-stdin"], "clear": ["--clear"]}[mode]
    argv = ["booley", "auth", *flags]
    if selection.startswith("explicit"):
        monkeypatch.chdir(tmp_path)
        argv = ["booley", "-C", str(linked), "auth", *flags]
    monkeypatch.setattr(sys, "argv", argv)
    result = tlr.main()
    text = capsys.readouterr().err
    assert (
        result == 2
        and not effects
        and "run from the Project Root" in text
        and "booley init" not in text
    ), "linked CLI auth must require Project Root before generic guards"


@pytest.mark.parametrize("repo", ["unrelated", "source-checkout"])
def test_cli_auth_from_non_project_linked_worktree_is_not_refused(
    tmp_path, monkeypatch, capsys, repo
):
    """Only a proven linked Project checkout refuses; other linked worktrees keep auth."""
    import subprocess
    import sys

    _linked_auth(tmp_path, monkeypatch)
    main = tmp_path / "plain"
    git = ["git", "-c", "user.name=Test", "-c", "user.email=test@example.com"]
    subprocess.run([*git, "init", str(main)], check=True, capture_output=True, timeout=30)
    if repo == "source-checkout":
        (main / "pyproject.toml").write_text("[tool.booley]\nsource_checkout = true\n")
        subprocess.run([*git, "-C", str(main), "add", "-A"], check=True, capture_output=True)
    subprocess.run(
        [*git, "-C", str(main), "commit", "--allow-empty", "-m", "initial"],
        check=True,
        capture_output=True,
        timeout=30,
    )
    linked = tmp_path / "plain-linked"
    subprocess.run(
        [*git, "-C", str(main), "worktree", "add", "--detach", str(linked)],
        check=True,
        capture_output=True,
        timeout=30,
    )
    monkeypatch.chdir(linked)
    monkeypatch.setattr(sys, "argv", ["booley", "auth", "--clear"])
    result = tlr.main()
    assert result == 0, capsys.readouterr().err


def test_linked_auth_status_preserves_selected_checkout(tmp_path, monkeypatch):
    _, linked, effects = _linked_auth(tmp_path, monkeypatch)
    selected = []
    monkeypatch.setattr(
        auth_cmd, "_safe_report_status", lambda app, root: selected.append(root) or 0
    )
    assert auth_cmd.run_auth(SimpleNamespace(status=True), linked) == 0
    assert selected == [linked]
    assert not effects


def test_auth_from_project_subdirectory_retains_behavior(tmp_path, monkeypatch):
    root, _, effects = _linked_auth(tmp_path, monkeypatch)
    child = root / "rtl"
    child.mkdir()
    assert auth_cmd.run_auth(SimpleNamespace(status=False, clear=True, app="claude"), child) == 0
    assert effects == ["clear_stored_token", "_reseed_spec"]


def test_uninitialized_nested_checkout_is_not_adopted_as_ancestor_project(tmp_path, monkeypatch):
    from booley.runtime.project_discovery import discover_project_root

    root, _, _ = _linked_auth(tmp_path, monkeypatch)
    nested = root / "unrelated"
    nested.mkdir()
    import subprocess

    subprocess.run(["git", "init", str(nested)], check=True, capture_output=True, timeout=30)
    assert discover_project_root(nested) == nested


@pytest.fixture
def auth_project(home, monkeypatch):
    from booley.runtime.booley_dir import reset_cache

    (home / "config").mkdir(mode=0o700)
    project = home / "project"
    data = project / ".booley"
    data.mkdir(parents=True)
    (data / "booley.toml").write_text('[agent]\nprovider="claude"\nauth="subscription"\n')
    monkeypatch.delenv("BOOLEY_DIR", raising=False)
    monkeypatch.delenv("RTL_PROJECT_ROOT", raising=False)
    reset_cache()
    return project


def _auth_stdin(monkeypatch, project):
    from io import StringIO

    monkeypatch.setattr(auth_cmd.sys, "stdin", StringIO(_TOKEN + "-replacement\n"))
    return auth_cmd.run_auth(SimpleNamespace(token_stdin=True), project)


def _foreign_owner(monkeypatch, target):
    original = Path.stat

    def stat_path(path, *args, **kwargs):
        result = original(path, *args, **kwargs)
        if path == target:
            values = list(result)
            values[4] = os.getuid() + 1
            return os.stat_result(values)
        return result

    monkeypatch.setattr(Path, "stat", stat_path)


@pytest.mark.skipif(os.name != "posix", reason="POSIX ownership and mode admission")
class TestAuthStorePreflight:
    def test_unsafe_xdg_parent_preserves_credentials(self, auth_project, monkeypatch, capsys):
        token = auth_token.store_token(_TOKEN)
        previous = token.read_bytes()
        token.parent.parent.chmod(0o775)
        reseed = Mock(return_value=True)
        monkeypatch.setattr(auth_cmd, "_reseed_spec", reseed)
        rc = _auth_stdin(monkeypatch, auth_project)
        assert token.read_bytes() == previous, "credential changed before permission preflight"
        assert rc == 2
        assert token.parent.parent.stat().st_mode & 0o777 == 0o775
        assert "chmod 700" in capsys.readouterr().out
        reseed.assert_not_called()

    @pytest.mark.parametrize("kind", ["mode", "foreign"])
    def test_unsafe_booley_directory_stores_nothing(self, auth_project, monkeypatch, capsys, kind):
        token = auth_token.token_path()
        token.parent.mkdir(parents=True, mode=0o700)
        if kind == "mode":
            token.parent.chmod(0o755)
        else:
            _foreign_owner(monkeypatch, token.parent)
        monkeypatch.setattr(auth_cmd, "_reseed_spec", Mock(return_value=True))
        rc = _auth_stdin(monkeypatch, auth_project)
        assert not token.exists(), "credential written into an unsafe directory"
        assert rc == 2
        if kind == "mode":
            assert token.parent.stat().st_mode & 0o777 == 0o755
        assert ("chmod 700" if kind == "mode" else "owned") in capsys.readouterr().out

    def test_foreign_project_data_preserves_credentials(self, auth_project, monkeypatch, capsys):
        token = auth_token.store_token(_TOKEN)
        previous = token.read_bytes()
        _foreign_owner(monkeypatch, auth_project / ".booley")
        monkeypatch.setattr(auth_cmd, "_reseed_spec", Mock(return_value=True))
        rc = _auth_stdin(monkeypatch, auth_project)
        assert token.read_bytes() == previous, (
            "credential changed before Project data ownership preflight"
        )
        assert rc == 2
        assert "owned" in capsys.readouterr().out


def _auth_interactive_ports(monkeypatch):
    from booley.harness.setup import interactive
    from booley.runtime import dashboard_tasks

    monkeypatch.setattr(interactive, "_unlicensed_relay_pending", lambda _: False)
    monkeypatch.setattr(
        session_runtime,
        "plan_stopped_headless_runtime_reconciliation",
        lambda *a: SimpleNamespace(pending=False),
    )
    monkeypatch.setattr(session_runtime, "reconcile_stopped_headless_runtime", lambda *a: False)
    monkeypatch.setattr(interactive, "git_excludes_pending", lambda *a: False)
    monkeypatch.setattr(interactive, "add_git_excludes", lambda *a: False)
    monkeypatch.setattr(dashboard_tasks, "inspect", lambda *a: dashboard_tasks.TaskPlan())


def _auth_reseed_boundaries(project, monkeypatch):
    from booley.harness import bootstrap, host_sidecars, image_lifecycle, init_cmd
    from booley.runtime import session_issuance
    from booley.runtime.image_lifecycle import Intent, LifecycleResult, Status

    bootstrap_calls = []
    captured = []

    def full_bootstrap(*args, **kwargs):
        bootstrap_calls.append(True)
        return bootstrap.BootstrapResult(Intent.CHECK, ())

    monkeypatch.setattr(bootstrap, "reconcile_bootstrap", full_bootstrap)
    monkeypatch.setattr(init_cmd, "reconcile_bootstrap", full_bootstrap)
    monkeypatch.setattr(
        host_sidecars, "reconcile_sidecars", lambda *a, **k: SimpleNamespace(findings=())
    )
    monkeypatch.setattr(image_lifecycle, "plan", lambda *a, **k: SimpleNamespace(steps=()))
    monkeypatch.setattr(
        image_lifecycle,
        "reconcile_planned",
        lambda *a, **k: LifecycleResult("synthetic-image", "sha256:" + "a" * 64, Status.CURRENT),
    )
    pdk = project.parent / "pdk"
    pdk.mkdir()
    monkeypatch.setattr(init_cmd.nangate_pdk, "is_ready", lambda: True)
    monkeypatch.setattr(init_cmd.nangate_pdk, "cache_root", lambda: pdk)
    monkeypatch.setattr(session_issuance, "_resolve_image_id", lambda _: "sha256:" + "a" * 64)
    monkeypatch.setattr(session_issuance, "_project_data_alias_capable", lambda *a: False)
    monkeypatch.setattr(session_issuance, "_pin_initialize_command", lambda *a: None)
    monkeypatch.setattr(session_issuance, "_seal_with_requirements", lambda *a: "synthetic-digest")
    issued = SimpleNamespace(license_profile=None, project_data_layout="legacy")
    monkeypatch.setattr(session_issuance, "_prospective_issuance", lambda *a: issued)
    monkeypatch.setattr(
        session_issuance,
        "inspect_prepared",
        Mock(side_effect=session_issuance.RuntimeSpecError("not issued")),
    )

    def issue(_root, prepared, **kwargs):
        captured.append(prepared)
        return issued

    monkeypatch.setattr(session_issuance, "issue_prepared", issue)
    _auth_interactive_ports(monkeypatch)
    return bootstrap_calls, captured


class TestAuthReseed:
    def test_auth_never_runs_full_bootstrap(self, auth_project, monkeypatch):
        calls, captured = _auth_reseed_boundaries(auth_project, monkeypatch)
        rc = _auth_stdin(monkeypatch, auth_project)
        assert not calls, "auth invoked full Host Bootstrap"
        assert rc == 0
        assert len(captured) == 1
        assert any(
            docker_mount_path(auth_token.token_path()) in mount
            for mount in captured[0].spec["mounts"]
        )
        assert captured[0].spec["remoteEnv"]["BOOLEY_PRIMARY_AUTH"] == "subscription"

    def test_clear_never_runs_full_bootstrap(self, auth_project, monkeypatch, capsys):
        auth_token.store_token(_TOKEN)
        calls, captured = _auth_reseed_boundaries(auth_project, monkeypatch)
        rc = auth_cmd.run_auth(SimpleNamespace(clear=True), auth_project)
        assert not calls, "clear invoked full Host Bootstrap"
        assert rc == 0
        assert not auth_token.token_path().exists()
        assert len(captured) == 1
        assert all(
            docker_mount_path(auth_token.token_path()) not in mount
            for mount in captured[0].spec["mounts"]
        )
        assert "rebuild" in capsys.readouterr().out


class TestAuthStorageAdmission:
    def test_rewrite_keeps_bound_inode(self, home):
        path = auth_token.store_token(_TOKEN)
        inode = path.stat().st_ino
        path.chmod(0o644)
        auth_token.store_token(_TOKEN + "-new")
        assert path.stat().st_ino == inode
        assert path.read_text().strip() == _TOKEN + "-new"
        if os.name != "nt":
            assert path.stat().st_mode & 0o777 == 0o600

    @pytest.mark.parametrize("kind", ["symlink", "directory", "foreign"])
    def test_unsafe_token_target_preserves_existing_bytes(self, home, monkeypatch, kind):
        path = auth_token.store_token(_TOKEN)
        previous = path.read_bytes()
        if kind == "symlink":
            target = path.with_name("target")
            path.rename(target)
            path.symlink_to(target)
        elif kind == "directory":
            path.unlink()
            path.mkdir()
            target = path / "unmodified"
            target.write_bytes(previous)
        else:
            if os.name == "nt":
                pytest.skip("POSIX owner admission")
            target = path
            _foreign_owner(monkeypatch, path)
        with pytest.raises(auth_token.CredentialStoreError):
            auth_token.store_token(_TOKEN + "-new")
        assert target.read_bytes() == previous

    @pytest.mark.parametrize(
        "relative", ["runtime", "runtime/session-specs", "eda/session-refresh"]
    )
    def test_unsafe_project_storage_refuses_before_store(
        self, auth_project, monkeypatch, relative
    ):
        if os.name == "nt":
            pytest.skip("POSIX mode admission")
        token = auth_token.store_token(_TOKEN)
        previous = token.read_bytes()
        directory = token.parent / relative
        directory.mkdir(parents=True, mode=0o700)
        directory.chmod(0o755)
        monkeypatch.setattr(auth_cmd, "_reseed_spec", Mock(return_value=True))
        assert _auth_stdin(monkeypatch, auth_project) == 2
        assert token.read_bytes() == previous

    def test_write_failure_is_plain_and_does_not_report_success(
        self, auth_project, monkeypatch, capsys
    ):
        original = auth_token.os.open

        def open_path(path, *args, **kwargs):
            if path == auth_token.token_path():
                raise PermissionError("synthetic credential write denied")
            return original(path, *args, **kwargs)

        monkeypatch.setattr(auth_token.os, "open", open_path)
        assert _auth_stdin(monkeypatch, auth_project) != 0
        output = capsys.readouterr()
        assert "synthetic credential write denied" in output.out
        assert "[OK] stored" not in output.out
        assert "Traceback" not in output.out + output.err
        assert _TOKEN not in output.out + output.err

    def test_store_only_names_policy_and_future_pickup(self, home, monkeypatch, capsys):
        assert _auth_stdin(monkeypatch, None) == 0
        output = capsys.readouterr().out
        assert "session up" in output and "init --seed" in output
        assert "claude" in output and "auto/subscription" in output

    @pytest.mark.parametrize("present", [False, True])
    def test_clear_outside_project(self, home, present):
        if present:
            auth_token.store_token(_TOKEN)
        assert auth_cmd.run_auth(SimpleNamespace(clear=True), None) == 0
        assert not auth_token.token_path().exists()


@pytest.mark.parametrize("kind", ["directory", "foreign", "absent", "symlink"])
def test_auth_conditional_eda_lock_admission(auth_project, monkeypatch, kind):
    from booley.eda.provisioning import authority

    if kind == "foreign" and os.name == "nt":
        pytest.skip("POSIX ownership admission")
    (auth_project / ".booley" / "booley.toml").write_text(
        '[flows.fpga]\nenabled=true\n[eda.vivado]\nprovisioning="image"\n'
    )
    token = auth_token.store_token(_TOKEN)
    previous = token.read_bytes()
    authority.ensure_state_dir()
    lock = authority.state_dir() / "authority.lock"
    if kind == "symlink":
        target = lock.with_name("unchanged")
        target.write_text("untouched")
        lock.symlink_to(target)
        before = lock.lstat()
    elif kind == "directory":
        lock.mkdir()
    elif kind == "foreign":
        lock.write_text("unchanged")
        _foreign_owner(monkeypatch, lock)
    monkeypatch.setattr(auth_cmd, "_reseed_spec", Mock(return_value=True))
    rc = _auth_stdin(monkeypatch, auth_project)
    if kind == "absent":
        assert rc == 0
        assert not lock.exists()
    else:
        assert rc == 2
        assert token.read_bytes() == previous
        if kind == "foreign":
            assert lock.read_text() == "unchanged"
        elif kind == "symlink":
            assert lock.lstat() == before
            assert target.read_text() == "untouched"


def test_auth_no_eda_consumer_ignores_unrelated_authority(auth_project, monkeypatch):
    from booley.eda.provisioning import authority

    token = auth_token.store_token(_TOKEN)
    root = token.parent / "eda"
    root.mkdir(mode=0o700)
    lock = root / "authority.lock"
    lock.mkdir()
    monkeypatch.setattr(
        authority, "load_state", Mock(side_effect=AssertionError("no EDA consumer"))
    )
    monkeypatch.setattr(auth_cmd, "_reseed_spec", Mock(return_value=True))
    assert _auth_stdin(monkeypatch, auth_project) == 0
    assert lock.is_dir()
