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

from booley.core.host_shell import quote_arg
from booley.harness import auth_cmd
from booley.harness import booley as tlr
from booley.runtime import auth_token, issuance_invalidation, runtime_context, session_runtime
from booley.runtime import devcontainer as dc
from booley.runtime.platform_paths import docker_mount_path
from booley.runtime.sandbox_apply import UNKNOWN_SANDBOX_STEPS
from tests.devcontainer_spec_support import build_spec
from tests.subprocess_budget import run_bounded_child

_TOKEN = "sk-ant-oat01-abcdef123456"  # claude: one-year setup-token
_API_KEY = "sk-proj-codex-abcdef123456"  # codex: API key (no setup-token exists)


@pytest.fixture
def home(tmp_path, monkeypatch):
    """Redirect the per-user config dir into a tmp tree, with no auth env leaking in."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.delenv(auth_token.ENV_VAR, raising=False)
    for var in ("BOOLEY_PRIMARY_AUTH", "BOOLEY_AGENT_APP"):
        monkeypatch.delenv(var, raising=False)
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

    @pytest.mark.parametrize("bad", ["", "   ", "\n"])
    def test_refuses_empty(self, home, bad):
        with pytest.raises(ValueError):
            auth_token.store_token(bad)

    def test_refuses_embedded_whitespace(self, home):
        # A newline inside the value would corrupt every following line of the
        # docker --env-file it gets written into.
        with pytest.raises(ValueError, match="whitespace"):
            auth_token.store_token("sk-ant-oat01-aaa\nBAR=evil")


class TestCodex:
    """Codex needs the same mechanism: its auth.json also holds a REFRESHING
    credential when signed in with a ChatGPT subscription, and Codex rewrites it."""

    def test_apps_have_separate_stores(self, home):
        auth_token.store_token(_TOKEN, auth_token.APP_CLAUDE)
        auth_token.store_token(_API_KEY, auth_token.APP_CODEX)

        assert auth_token.resolve_token(auth_token.APP_CODEX) == _API_KEY
        auth_token.clear_stored_token(auth_token.APP_CODEX)

        assert auth_token.resolve_token(auth_token.APP_CLAUDE) == _TOKEN
        assert auth_token.resolve_token(auth_token.APP_CODEX) is None

    def test_spec_seeds_codex_auth_onto_writable_volume(self, home):
        # The regression: a read-only bind at ~/.codex/auth.json makes Codex's
        # own refresh write FAIL, 401ing the session once the token expires.
        spec = build_spec(dc.APP_CODEX, login_source="/home/u/.codex/auth.json")

        assert all(f"target={dc.AGENT_HOME}/.codex/auth.json" not in m for m in spec["mounts"])
        assert f"{dc.AGENT_HOME}/.codex-auth-seed.json" in spec["postStartCommand"]


@pytest.mark.parametrize(
    ("stored", "exported", "seed", "expected"),
    [
        pytest.param(None, None, None, None, id="absent-is-none-not-empty"),
        pytest.param(_TOKEN, None, None, _TOKEN, id="stored-without-export"),
        pytest.param(_TOKEN, "sk-ant-oat01-exported", None, "sk-ant-oat01-exported", id="export"),
        pytest.param(_TOKEN, "   ", None, _TOKEN, id="blank-export-does-not-shadow"),
        # In-container storage lives on the HOST, mounted read-only at a home sidecar.
        pytest.param(None, None, _TOKEN, _TOKEN, id="seed-sidecar-fills-the-gap"),
        pytest.param(_TOKEN, None, "sk-ant-oat01-seed", _TOKEN, id="store-wins-over-seed"),
    ],
)
def test_token_resolution(isolated_home, monkeypatch, stored, exported, seed, expected):
    # "" instead of None would shadow the mounted subscription creds.
    if stored:
        auth_token.store_token(stored)
    if exported:
        monkeypatch.setenv(auth_token.ENV_VAR, exported)
    if seed:
        (isolated_home / auth_token.TOKEN_SEED_BASENAME[auth_token.APP_CLAUDE]).write_text(seed)

    assert auth_token.resolve_token(auth_token.APP_CLAUDE) == expected


@pytest.mark.parametrize(
    ("app", "login", "key", "stored", "mode", "loser"),
    [
        pytest.param("claude", False, False, None, None, None, id="nothing"),
        pytest.param("not-an-app", False, False, None, None, None, id="unknown-app"),
        pytest.param("claude", True, False, None, "subscription", None, id="login-alone"),
        # The original misreport: login and key both present used to read "subscription".
        pytest.param("claude", True, True, None, "api_key", ".credentials.json", id="key>login"),
        pytest.param("claude", False, True, _TOKEN, "api_key", "stored", id="key>stored"),
        pytest.param(
            "claude", True, False, _TOKEN, "oauth_token", ".credentials.json", id="token"
        ),
        # For Codex the API key IS the rotation-free credential: one ladder rung.
        pytest.param("codex", False, False, _API_KEY, "api_key", None, id="codex-stored-key"),
    ],
)
def test_effective_credential_follows_the_cli_precedence(
    isolated_home, monkeypatch, app, login, key, stored, mode, loser
):
    """An exported API key outranks the rotation-free token, which outranks the login file."""
    if login:
        creds = isolated_home / ".claude" / ".credentials.json"
        creds.parent.mkdir(parents=True)
        creds.write_text("{}", encoding="utf-8")
    if key:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-x")
    if stored:
        auth_token.store_token(stored, app)

    eff = auth_token.effective_credential(app, auth_token.VISITOR_AUTH)

    assert (eff and eff.mode) == mode
    if eff is not None:
        assert eff.rotation_free == (mode != "subscription")
        assert ("ANTHROPIC_API_KEY" in eff.source) == key
        assert [loser in x for x in eff.overridden] == ([True] if loser else [])


class TestAuthStatusReport:
    """`booley auth --status` must describe what actually runs. With an API key
    exported it used to say "no rotation-free credential — falls back to a
    refreshing one": doubly wrong (the key is what runs, and keys never refresh)."""

    @staticmethod
    def _status(monkeypatch, capsys) -> tuple[int, list[str]]:
        from booley.presentation import colors

        monkeypatch.setattr(colors, "COLORS_ENABLED", False)
        rc = auth_cmd.run_auth(SimpleNamespace(status=True), None)
        return rc, capsys.readouterr().out.splitlines()

    def test_subscription_only_still_warns_about_rotation(
        self, isolated_home, monkeypatch, capsys
    ):
        creds = isolated_home / ".claude" / ".credentials.json"
        creds.parent.mkdir(parents=True)
        creds.write_text('{"claudeAiOauth": {"accessToken": "SYNTHETIC"}}', encoding="utf-8")

        rc, lines = self._status(monkeypatch, capsys)

        assert rc == 0  # a refreshing login is usable despite its rotation warning
        row = next(i for i, line in enumerate(lines) if line.startswith("  !  claude:"))
        assert "refreshing" in lines[row]
        assert "can revoke a running container" in lines[row + 1]

    def test_api_key_runs_and_overridden_login_is_a_hint(self, isolated_home, monkeypatch, capsys):
        from booley.config import agent as agent_config

        creds = isolated_home / ".claude" / ".credentials.json"
        creds.parent.mkdir(parents=True)
        creds.write_text("{}", encoding="utf-8")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-x")
        # The visitor mode needs a hand-written Sandbox: /.dockerenv, which a test cannot create.
        monkeypatch.setattr(agent_config, "_inside_container", lambda: True)
        monkeypatch.setenv("BOOLEY_PRIMARY_AUTH", "auto")

        _rc, lines = self._status(monkeypatch, capsys)

        row = next(i for i, line in enumerate(lines) if line.startswith("  \u2713  claude:"))
        assert "ANTHROPIC_API_KEY" in lines[row]
        assert not [line for line in lines if line.startswith("  !  claude:")]
        assert lines[row + 1].startswith("     \u2192 ")
        assert lines[row + 1].endswith(
            ".credentials.json is present but not used; the line above wins"
        )


class TestInjection:
    @pytest.mark.parametrize(
        ("var", "app", "value", "expected"),
        [
            ("CLAUDE_CODE_OAUTH_TOKEN", "claude", _TOKEN, _TOKEN),
            ("OPENAI_API_KEY", "codex", _API_KEY, _API_KEY),
            ("CLAUDE_CODE_OAUTH_TOKEN", None, None, ""),
            # Only token vars get the file fallback; others keep the CLI's empty string.
            ("NOT_A_TOKEN", "claude", _TOKEN, ""),
        ],
    )
    def test_session_runtime_resolves_localenv_from_store(self, home, var, app, value, expected):
        if app:
            auth_token.store_token(value, app)

        assert session_runtime.substitute(f"${{localEnv:{var}}}", home) == expected

    def test_sandbox_injects_stored_token(self, home, monkeypatch):
        from booley.harness import sandbox

        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        auth_token.store_token(_TOKEN)

        cmd: list[str] = []
        runner = sandbox.DockerRunner.__new__(sandbox.DockerRunner)
        runner.worktree = home / "project"
        runner.worktree.mkdir()
        # DockerRunner's public run() starts Docker; writing the env-file is its last step before.
        runner._append_api_key_env_file(cmd)

        assert "--env-file" in cmd
        env_file = Path(cmd[cmd.index("--env-file") + 1])
        body = env_file.read_text(encoding="utf-8")
        assert f"CLAUDE_CODE_OAUTH_TOKEN={_TOKEN}" in body


class TestRotationRiskVenue:
    """A host login shares the refresh token only on the host or in a seeded Sandbox."""

    _NOTE = "login made inside the Sandbox"

    @pytest.mark.parametrize(
        ("app", "policy", "in_sandbox", "seed_app", "expected"),
        [
            pytest.param(
                "claude", auth_token.VISITOR_AUTH, True, None, "sandbox-note", id="unseeded-claude"
            ),
            pytest.param(
                "codex", auth_token.VISITOR_AUTH, True, None, "sandbox-note", id="unseeded-codex"
            ),
            pytest.param(
                "codex",
                auth_token.VISITOR_AUTH,
                True,
                "claude",
                "sandbox-note",
                id="other-app-seed",
            ),
            pytest.param(
                "claude", "subscription", True, None, "sandbox-note", id="unseeded-subscription"
            ),
            pytest.param(
                "claude", auth_token.VISITOR_AUTH, True, "claude", "warn", id="seeded-claude"
            ),
            pytest.param(
                "claude", "subscription", True, "claude", "accepted-note", id="seeded-subscription"
            ),
            pytest.param(
                "codex", auth_token.VISITOR_AUTH, True, "codex", "warn", id="seeded-codex"
            ),
            pytest.param("claude", auth_token.VISITOR_AUTH, False, None, "warn", id="host-claude"),
            pytest.param("codex", auth_token.VISITOR_AUTH, False, None, "warn", id="host-codex"),
        ],
    )
    def test_rotation_risk_by_venue_and_seed(
        self, isolated_home, monkeypatch, tmp_path, app, policy, in_sandbox, seed_app, expected
    ):
        from booley.harness import doctor

        monkeypatch.setattr(runtime_context, "inside_session_runtime", lambda: in_sandbox)
        monkeypatch.setattr(
            doctor.host_environment,
            "inspect_agent_installation",
            lambda _app: doctor.host_environment.AgentInstallation(True, None),
        )
        # Doctor reaches this row publicly only through run_doctor_result, which runs every
        # phase (Docker, image, Project); the seed targets are Sandbox paths a test cannot own.
        for name in (auth_token.APP_CLAUDE, auth_token.APP_CODEX):
            seed = tmp_path / "seed" / name
            monkeypatch.setitem(dc._APP_CREDS_SEED_TARGET, name, str(seed))
            if name == seed_app:
                seed.parent.mkdir(exist_ok=True)
                seed.write_text("{}")
        reporter = doctor._Reporter.create()

        doctor._check_oauth_token(
            app,
            reporter.pass_,
            reporter.warn_,
            reporter.skip_,
            policy,
            _note=reporter.note_,
        )

        warns = [f for f in reporter.findings if f.check_id == "agent.rotation-risk"]
        sandbox_notes = [f for f in reporter.findings if self._NOTE in f.message]
        label = {"claude": "Claude Code", "codex": "Codex"}[app]
        if expected == "warn":
            assert [f.fix for f in warns] == [f"booley auth --cli {app}"]
            assert "host-side login can revoke mid-run" in warns[0].message
            assert sandbox_notes == []
            return
        assert warns == []
        if expected == "accepted-note":
            assert sandbox_notes == []
            assert any("accepted by explicit auth" in f.message for f in reporter.findings)
            return
        assert [(f.severity, f.message.startswith(f"{label}:")) for f in sandbox_notes] == [
            (doctor.Severity.NOTE, True)
        ]


class TestDoctorCheck:
    """The check that would have caught the 40-task void run before it started."""

    @staticmethod
    def _run(monkeypatch, *, claude=True, codex=False, provider="claude"):
        from booley.harness import doctor

        # Host venue, pinned: in a Sandbox the rotation WARN depends on the seed sidecar.
        monkeypatch.setattr(runtime_context, "inside_session_runtime", lambda: False)
        monkeypatch.setattr(
            doctor.host_environment,
            "inspect_agent_installation",
            lambda provider: doctor.host_environment.AgentInstallation(
                claude if provider == "claude" else codex, None
            ),
        )
        sink: dict[str, list[str]] = {"pass": [], "warn": [], "skip": [], "fix": []}
        # run_doctor_result reaches this row only through its Docker and image phases.
        doctor._check_oauth_token(
            provider,
            sink["pass"].append,
            lambda msg, fix=None: (sink["warn"].append(msg), sink["fix"].append(fix or "")),
            sink["skip"].append,
            auth_token.VISITOR_AUTH,
        )
        return sink

    @pytest.mark.parametrize(
        ("provider", "installed", "stored", "key", "kind", "words"),
        [
            pytest.param(
                "claude", "both", None, False, "warn", ("Claude", "revoke"), id="only-rotating"
            ),
            pytest.param(
                "claude", "both", "claude", False, "pass", ("Claude", "rotation-free"), id="stored"
            ),
            # A key never rotates and outranks everything: warning here was doubly wrong.
            pytest.param(
                "claude", "claude", None, True, "pass", ("API key", "rotation-free"), id="key"
            ),
            pytest.param(
                "claude", "claude", "claude", True, "pass", ("outranks",), id="key>stored"
            ),
            # Claude covered, Codex not: on a codex project the warning is Codex's alone.
            pytest.param("codex", "both", "claude", False, "warn", ("Codex",), id="codex-warns"),
            pytest.param(
                "codex", "both", "codex", False, "pass", ("Codex",), id="codex-stored-key"
            ),
            pytest.param("claude", "none", None, False, "skip", ("skipped",), id="absent"),
            # Codex configured but never installed: say so, don't claim "no agent app".
            pytest.param(
                "codex",
                "claude",
                None,
                False,
                "skip",
                ("codex", "not installed"),
                id="codex-absent",
            ),
        ],
    )
    def test_audits_only_the_configured_provider(
        self, isolated_home, monkeypatch, provider, installed, stored, key, kind, words
    ):
        if stored:
            auth_token.store_token(_TOKEN if stored == "claude" else _API_KEY, stored)
        if key:
            monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-x")

        sink = self._run(
            monkeypatch, claude=installed != "none", codex=installed == "both", provider=provider
        )

        assert [k for k in ("pass", "warn", "skip") if sink[k]] == [kind]
        assert len(sink[kind]) == 1 and all(w in sink[kind][0] for w in words)

    @pytest.mark.skipif(os.name == "nt", reason="Windows ACLs are not POSIX mode bits")
    def test_warns_when_stored_token_is_world_readable(self, isolated_home, home, monkeypatch):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(home / "it's cfg"))
        path = auth_token.store_token(_TOKEN)
        path.chmod(0o644)

        sink = self._run(monkeypatch)

        assert any("readable by others" in m for m in sink["warn"])
        assert sink["fix"] == [f"chmod 600 {quote_arg(str(path))}"]


@pytest.mark.parametrize(
    ("primary", "app_env", "section", "configured", "declared"),
    [
        ("codex", None, {"provider": "claude"}, "codex", "claude"),  # run env wins
        (None, "codex", {}, "codex", None),  # the devcontainer's app beats the default
        (None, "codex", {"provider": "claude"}, "claude", "claude"),  # toml beats it
        ("claude", "claude", {"provider": "codex"}, "claude", "codex"),
        (None, None, {"provider": "bogus"}, "claude", None),  # invalid [agent] degrades softly
    ],
)
def test_doctor_provider_answers(
    tmp_path, monkeypatch, primary, app_env, section, configured, declared
):
    """The configured provider follows the runtime's order: BOOLEY_PRIMARY_PROVIDER, booley.toml,
    BOOLEY_AGENT_APP. The declared one backs the spec drift check, so it is booley.toml only:
    env vars steer which backend runs, never what `booley init --seed` writes."""
    from booley.harness import doctor

    for name, value in (("BOOLEY_PRIMARY_PROVIDER", primary), ("BOOLEY_AGENT_APP", app_env)):
        if value:
            monkeypatch.setenv(name, value)
        else:
            monkeypatch.delenv(name, raising=False)
    audit = doctor.ProjectAudit(tmp_path, tmp_path / ".booley", {"agent": section}, {}, "")

    # run_doctor_result reaches these only through its Docker and image phases.
    assert (doctor._configured_provider(audit), doctor._declared_provider(audit)) == (
        configured,
        declared,
    )


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
        audit = doctor.ProjectAudit(tmp_path, tmp_path / ".booley", {"agent": {}}, {}, "")
        # run_doctor_result reaches these only through its Docker and image phases.
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
        # Driving run_auth here would need a running Project Sandbox; no Docker fake starts one.
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
        # Driving run_auth here would need a running Project Sandbox; no Docker fake starts one.
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
        # A Sandbox start refusal needs the Project Sandbox runtime, which no Docker fake drives.
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

    @pytest.fixture
    def host_claude(self, isolated_home, monkeypatch):
        """No Project: `claude setup-token` runs from PATH; getpass reads the pasted token."""
        from tests.path_tool_fake import PathToolFake

        prompts: list[str] = []
        monkeypatch.setattr(auth_cmd.getpass, "getpass", lambda p: prompts.append(p) or _TOKEN)
        bin_dir = isolated_home / "bin"
        bin_dir.mkdir()
        monkeypatch.setenv("PATH", str(bin_dir))
        return SimpleNamespace(prompts=prompts, install=lambda: PathToolFake(bin_dir, "claude"))

    def _mint(self, capsys):
        args = SimpleNamespace(app=auth_token.APP_CLAUDE, token_stdin=False)
        return auth_cmd.run_auth(args, None), " ".join(capsys.readouterr().out.split())

    # Windows runs a bare command name only as an .exe; the branches here are platform-neutral.
    @pytest.mark.skipif(os.name == "nt", reason="subprocess cannot start a .cmd fake by bare name")
    @pytest.mark.parametrize(
        ("exit_code", "rc", "stored", "message"),
        [(0, 0, _TOKEN, "stored"), (7, 1, None, "`claude setup-token` failed (exit 7)")],
    )
    def test_non_project_mint_runs_the_host_cli(
        self, host_claude, capsys, exit_code, rc, stored, message
    ):
        claude = host_claude.install()
        claude.answer("setup-token", returncode=exit_code)

        code, output = self._mint(capsys)

        assert code == rc
        assert message in output
        assert [call.args for call in claude.calls()] == [["setup-token"]]
        assert auth_token.read_stored_token() == stored
        # A failed mint names its exit and never asks for a token it did not print.
        assert len(host_claude.prompts) == (1 if stored else 0)

    def test_missing_host_cli_names_project_runtime_option(self, host_claude, capsys):
        rc, output = self._mint(capsys)

        assert rc == 1
        assert "CLI not found on PATH — run this from an initialized Booley Project" in output
        assert host_claude.prompts == []

    def test_store_rejects_invalid_credential(self, home, monkeypatch, capsys):
        assert _auth_stdin(monkeypatch, None, token="sk-ant-oat01-a b") == 1
        assert "whitespace" in capsys.readouterr().out
        assert auth_token.read_stored_token() is None

    @pytest.mark.parametrize(
        ("app", "policy"),
        [(auth_token.APP_CLAUDE, "subscription"), (auth_token.APP_CODEX, "api_key")],
    )
    def test_store_outside_a_project_names_policy_and_future_pickup(
        self, home, monkeypatch, capsys, app, policy
    ):
        assert _auth_stdin(monkeypatch, None, app=app) == 0

        output = capsys.readouterr().out
        assert " ".join(output.split()).count(UNKNOWN_SANDBOX_STEPS) == 1
        assert f"{app} Projects with auth={policy} use this stored credential." in output
        assert [line.strip() for line in output.splitlines() if "revoke" in line] == [
            "In those Projects it does not refresh, so a host-side login can no longer "
            "revoke a running agent."
        ]

    @pytest.mark.parametrize("clear", [False, True])
    def test_reseed_failure_keeps_the_store_change_but_fails(
        self, auth_project, monkeypatch, capsys, clear
    ):
        _auth_reseed_boundaries(auth_project, monkeypatch, "synthetic issuance failure")
        if clear:
            auth_token.store_token(_TOKEN)
            rc = auth_cmd.run_auth(SimpleNamespace(clear=True), auth_project)
        else:
            rc = _auth_stdin(monkeypatch, auth_project)

        output = " ".join(capsys.readouterr().out.split())
        assert rc == 1
        assert auth_token.read_stored_token() == (None if clear else _TOKEN + "-replacement")
        assert "synthetic issuance failure" in output
        assert ("credential was removed" if clear else "credential was stored") in output


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


def test_codex_auth_link_replaces_legacy_copy_atomically(tmp_path, monkeypatch):
    from tests.conftest import require_symlinks

    require_symlinks(tmp_path)
    source = tmp_path / "source.json"
    destination = tmp_path / "auth.json"
    source.write_text('{"OPENAI_API_KEY":"NEW_SYNTHETIC"}')
    previous = b'{"OPENAI_API_KEY":"OLD_SYNTHETIC"}'
    destination.write_bytes(previous)

    def interrupted_replace(self, target):
        raise OSError("synthetic interrupted replace")

    with monkeypatch.context() as patched:
        patched.setattr(Path, "replace", interrupted_replace)
        with pytest.raises(OSError, match="interrupted"):
            auth_token.link_codex_auth(source, destination, auth_token.VISITOR_AUTH)
    # An interrupted relink keeps the previous entry and leaves no stray link.
    assert destination.read_bytes() == previous
    assert sorted(path.name for path in tmp_path.iterdir()) == ["auth.json", "source.json"]

    auth_token.link_codex_auth(source, destination, auth_token.VISITOR_AUTH)
    assert destination.is_symlink()
    assert destination.resolve() == source.resolve()
    # A second call with the link in place changes nothing.
    auth_token.link_codex_auth(source, destination, auth_token.VISITOR_AUTH)
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

    # Tripwires: past the linked-checkout guard auth would mint on the host or reseed a
    # Sandbox, effects no external boundary of a linked worktree can stand in for.
    for name, value in [("_acquire", _TOKEN), ("_reseed_spec", True)]:
        monkeypatch.setattr(auth_cmd, name, effect(name, value))
    for name, value in [("store_token", tmp_path / "token"), ("clear_stored_token", True)]:
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
    import sys

    _linked_auth(tmp_path, monkeypatch)
    main = tmp_path / "plain"
    git = ["git", "-c", "user.name=Test", "-c", "user.email=test@example.com"]
    run_bounded_child([*git, "init", str(main)], check=True)
    if repo == "source-checkout":
        (main / "pyproject.toml").write_text("[tool.booley]\nsource_checkout = true\n")
        run_bounded_child([*git, "-C", str(main), "add", "-A"], check=True)
    run_bounded_child(
        [*git, "-C", str(main), "commit", "--allow-empty", "-m", "initial"], check=True
    )
    linked = tmp_path / "plain-linked"
    run_bounded_child(
        [*git, "-C", str(main), "worktree", "add", "--detach", str(linked)], check=True
    )
    monkeypatch.chdir(linked)
    monkeypatch.setattr(sys, "argv", ["booley", "auth", "--clear"])
    result = tlr.main()
    assert result == 0, capsys.readouterr().err


def test_linked_auth_status_preserves_selected_checkout(tmp_path, monkeypatch):
    _, linked, effects = _linked_auth(tmp_path, monkeypatch)
    selected = []
    # The report itself prints no root; recording the argument is the only witness.
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
    assert effects == ["clear_stored_token", "_reseed_spec"]  # the tripwires of _linked_auth


def test_uninitialized_nested_checkout_is_not_adopted_as_ancestor_project(tmp_path, monkeypatch):
    from booley.runtime.project_discovery import discover_project_root

    root, _, _ = _linked_auth(tmp_path, monkeypatch)
    nested = root / "unrelated"
    nested.mkdir()
    run_bounded_child(["git", "init", str(nested)], check=True)
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


def _auth_stdin(monkeypatch, project, app=auth_token.APP_CLAUDE, token=_TOKEN + "-replacement"):
    from io import StringIO

    monkeypatch.setattr(auth_cmd.sys, "stdin", StringIO(token + "\n"))
    return auth_cmd.run_auth(SimpleNamespace(token_stdin=True, app=app), project)


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
        rc = _auth_stdin(monkeypatch, auth_project)
        assert token.read_bytes() == previous, "credential changed before permission preflight"
        assert rc == 2
        assert token.parent.parent.stat().st_mode & 0o777 == 0o775
        assert "chmod 700" in capsys.readouterr().out

    @pytest.mark.parametrize("kind", ["mode", "foreign"])
    def test_unsafe_booley_directory_stores_nothing(self, auth_project, monkeypatch, capsys, kind):
        token = auth_token.token_path()
        token.parent.mkdir(parents=True, mode=0o700)
        if kind == "mode":
            token.parent.chmod(0o755)
        else:
            _foreign_owner(monkeypatch, token.parent)
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
        rc = _auth_stdin(monkeypatch, auth_project)
        assert token.read_bytes() == previous, (
            "credential changed before Project data ownership preflight"
        )
        assert rc == 2
        assert "owned" in capsys.readouterr().out


# The reseed reaches Docker and the image store through these Booley internals; no shared
# fake covers that boundary yet, so the private ones below stand in for it.
def _auth_sandbox_ports(monkeypatch):
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


def _auth_reseed_boundaries(project, monkeypatch, issue_error=None):
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
    # Image and keeper lookups run Docker; no shared fake covers issuance's Docker calls yet.
    monkeypatch.setattr(session_issuance, "_resolve_image_id", lambda _: "sha256:" + "a" * 64)
    monkeypatch.setattr(session_issuance, "_keeper_image_id", lambda _: "sha256:" + "a" * 64)
    monkeypatch.setattr(session_issuance, "_project_data_alias_capable", lambda *a: False)
    monkeypatch.setattr(session_issuance, "_pin_initialize_command", lambda *a: None)
    monkeypatch.setattr(session_issuance, "_seal_with_requirements", lambda *a: "synthetic-digest")
    issued = SimpleNamespace(license_profile=None, project_data_layout="legacy")
    monkeypatch.setattr(session_issuance, "_issuance", lambda *a, **k: issued)
    monkeypatch.setattr(
        session_issuance,
        "inspect_prepared",
        Mock(side_effect=session_issuance.RuntimeSpecError("not issued")),
    )

    def issue(_root, prepared, **kwargs):
        if issue_error:
            raise session_issuance.RuntimeSpecError(issue_error)
        captured.append(prepared)
        return issued

    monkeypatch.setattr(session_issuance, "issue_prepared", issue)
    _auth_sandbox_ports(monkeypatch)
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

    @pytest.mark.parametrize(
        ("agent", "revokes", "expected"),
        [
            pytest.param(
                'default = "codex"\nauth = "subscription"',
                False,
                [
                    "This Project's codex auth policy 'subscription' does not use the stored "
                    "credential, so its agents will not use it.",
                    'to use it here: set [agent.auth] codex = "api_key" in booley.toml, '
                    "then run booley session refresh",
                ],
                id="subscription",
            ),
            pytest.param(
                'default = "codex"\nauth = "api_key"',
                True,
                [
                    "This Project's codex auth policy 'api_key' uses the stored credential.",
                    "It does not refresh, so a host-side login can no longer revoke a running agent.",
                ],
                id="api_key",
            ),
            pytest.param(
                'default = "claude"\nauth = "subscription"',
                False,
                [
                    "This Project does not list codex in [agent] clis, so its agents will not "
                    "use the stored credential.",
                    "to use it here: add codex to [agent] clis, set [agent.auth] codex = "
                    '"api_key", then run booley session refresh',
                ],
                id="unlisted",
            ),
        ],
    )
    def test_codex_store_says_whether_the_project_uses_it(
        self, auth_project, isolated_home, monkeypatch, capsys, agent, revokes, expected
    ):
        (auth_project / ".booley" / "booley.toml").write_text(f"[agent]\n{agent}\n")
        auth_token.store_token(_TOKEN)
        codex_home = isolated_home / ".codex"
        codex_home.mkdir()
        (codex_home / "auth.json").write_bytes(b'{"tokens": {"access_token": "SYNTHETIC"}}')
        _auth_reseed_boundaries(auth_project, monkeypatch)

        rc = _auth_stdin(monkeypatch, auth_project, app=auth_token.APP_CODEX)

        output = " ".join(capsys.readouterr().out.split())
        assert rc == 0
        for text in expected:
            assert text in output
        assert ("can no longer revoke" in output) is revokes

    def test_codex_store_reads_the_policy_of_the_trusted_booley_dir(
        self, auth_project, isolated_home, monkeypatch, capsys
    ):
        (auth_project / ".booley" / "booley.toml").write_text(
            '[agent]\ndefault = "codex"\nauth = "api_key"\n'
        )
        trusted = isolated_home / "trusted-data"
        trusted.mkdir()
        (trusted / "booley.toml").write_text('[agent]\ndefault = "codex"\nauth = "subscription"\n')
        monkeypatch.setenv("BOOLEY_DIR", str(trusted))
        _auth_reseed_boundaries(auth_project, monkeypatch)

        rc = _auth_stdin(monkeypatch, auth_project, app=auth_token.APP_CODEX)

        output = " ".join(capsys.readouterr().out.split())
        assert rc == 0
        assert "auth policy 'subscription' does not use the stored credential" in output
        assert "can no longer revoke" not in output

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

    @pytest.mark.parametrize("relative", ["runtime", "runtime/session-specs"])
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

    @pytest.mark.parametrize("present", [False, True])
    def test_clear_outside_project(self, home, capsys, present):
        if present:
            auth_token.store_token(_TOKEN)
        assert auth_cmd.run_auth(SimpleNamespace(clear=True), None) == 0
        assert not auth_token.token_path().exists()
        removed = "removed" if present else "no stored claude credential to remove"
        assert removed in capsys.readouterr().out


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
    # Storage admission is under test; the reseed past it is stubbed (TestAuthReseed runs it).
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
    # Storage admission is under test; the reseed past it is stubbed (TestAuthReseed runs it).
    monkeypatch.setattr(auth_cmd, "_reseed_spec", Mock(return_value=True))
    assert _auth_stdin(monkeypatch, auth_project) == 0
    assert lock.is_dir()
