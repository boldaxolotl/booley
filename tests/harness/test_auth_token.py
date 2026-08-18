"""Tests for the long-lived agent token: storage, resolution, and injection.

The invariant under test throughout: an absent token must resolve to ``None`` —
never ``""`` — because an empty value forwarded into a container SHADOWS the
mounted subscription credentials and logs the agent out.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from booley.harness import auth_token, session_runtime
from booley.harness import booley as tlr

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

    def test_lives_outside_any_repo_or_mount(self, home):
        # A one-year credential inside the project dir is one `git add -f` from
        # being published, so it must not land under the workspace.
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
        from booley.harness import devcontainer as dc

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

        assert rc == 1  # codex has nothing either → every app at rotation risk
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

        monkeypatch.setattr(doctor, "_detect_claude_code", lambda: claude)
        monkeypatch.setattr(doctor, "_detect_codex", lambda: codex)
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
            project_dir=tmp_path / ".booley_project",
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
        """Third resolution step, same as ``_backend_config._lazy_backend_config``:
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
        monkeypatch.setattr(doctor, "_detect_claude_code", lambda: True)
        monkeypatch.setattr(doctor, "_detect_codex", lambda: True)  # stale login
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
