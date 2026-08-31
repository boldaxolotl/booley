"""Tests for booley.py."""

from __future__ import annotations

import json
import logging
import re
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import ClassVar
from unittest.mock import MagicMock, patch

import pytest

from booley.harness import booley as tlr
from booley.harness import subscription_limit as sl

# ===========================================================================
# ts()
# ===========================================================================


class TestTs:
    def test_format_hh_mm_ss(self):
        from booley.harness.terminal import ts

        result = ts()
        assert re.match(r"\d{2}:\d{2}:\d{2}$", result)

    def test_returns_current_time(self):
        from booley.harness.terminal import ts

        before = datetime.now().strftime("%H:%M")
        result = ts()
        assert result.startswith(before[:4])  # at least HH:M matches


def test_doctor_parser_accepts_deep_flag():
    parser = tlr._build_parser()

    args = tlr._normalize_args(parser, parser.parse_args(["doctor", "--deep"]))

    assert args.command == "doctor"
    assert args.deep is True


def test_doctor_parser_accepts_skip_agent_checks_flag():
    parser = tlr._build_parser()

    args = tlr._normalize_args(
        parser,
        parser.parse_args(["doctor", "--deep", "--skip-agent-checks"]),
    )

    assert args.skip_agent_checks is True


def test_doctor_parser_requires_deep_for_skip_agent_checks(capsys):
    parser = tlr._build_parser()

    with pytest.raises(SystemExit) as exc:
        tlr._normalize_args(parser, parser.parse_args(["doctor", "--skip-agent-checks"]))

    assert exc.value.code == 2
    assert "--skip-agent-checks requires --deep" in capsys.readouterr().err


def test_doctor_parser_accepts_explicit_project_root(tmp_path):
    parser = tlr._build_parser()

    args = parser.parse_args(["doctor", "--project-root", str(tmp_path)])

    assert args.project_root == str(tmp_path)


@pytest.mark.parametrize("command", ["mcp-tool", "link-guidance", "tool"])
def test_removed_top_level_commands_are_rejected(command):
    parser = tlr._build_parser()

    with pytest.raises(SystemExit) as exc:
        parser.parse_args([command])

    assert exc.value.code == 2


@pytest.mark.parametrize(
    ("retired", "replacement"),
    (("--sim-tool", "--sim-eda-tool"), ("--lint-tool", "--lint-eda-tool")),
)
def test_init_parser_rejects_retired_eda_tool_flags(retired, replacement, capsys):
    parser = tlr._build_parser()
    with pytest.raises(SystemExit) as exc:
        parser.parse_args(["init", "--scaffold", "demo", retired, "verilator"])
    assert exc.value.code == 2
    assert f"{retired} is retired; use {replacement}" in capsys.readouterr().err


def test_init_parser_accepts_explicit_agent_selection():
    args = tlr._build_parser().parse_args(["init", "--provider", "codex", "--auth", "api-key"])

    assert args.provider == "codex"
    assert args.auth == "api_key"


def test_init_parser_accepts_skip_credentials():
    args = tlr._build_parser().parse_args(["init", "--skip-credentials"])

    assert args.skip_credentials is True


def test_chat_parser_and_dispatch_are_registered():
    parser = tlr._build_parser()

    args = parser.parse_args(["chat"])

    assert args.command == "chat"
    assert tlr._EARLY_COMMANDS["chat"] is tlr.run_chat
    assert "Open this Project's configured agent CLI" in parser.format_help()


def test_bare_booley_defaults_to_chat():
    parser = tlr._build_parser()

    args = tlr._normalize_args(parser, parser.parse_args([]))

    assert args.command == "chat"
    assert "Run bare `booley`" in parser.format_help()


def test_session_health_reports_scheduled_automatic_check(tmp_path, monkeypatch, capsys):
    from booley.harness import auto_doctor

    monkeypatch.setattr(auto_doctor, "consume_changed_summary", lambda *_a, **_kw: None)
    monkeypatch.setattr(auto_doctor, "due_reason", lambda _root: "stale")

    tlr._report_session_health(tmp_path)

    assert "Automatic Doctor is running" in capsys.readouterr().err


def test_due_session_start_does_not_report_persisted_doctor_findings(
    tmp_path, monkeypatch, capsys
):
    from booley.harness import auto_doctor

    monkeypatch.setattr(
        auto_doctor,
        "consume_changed_summary",
        lambda *_args, **_kwargs: pytest.fail("stale summary was consumed"),
    )

    tlr._report_session_health(tmp_path, startup_due_reason="Doctor inputs changed")

    output = capsys.readouterr().err
    assert "Automatic Doctor is running" in output
    assert "before startup" in output


def test_session_health_rechecks_freshness_before_consuming_summary(tmp_path, monkeypatch, capsys):
    from booley.harness import auto_doctor

    monkeypatch.setattr(auto_doctor, "due_reason", lambda _root: "Doctor inputs changed")
    monkeypatch.setattr(
        auto_doctor,
        "consume_changed_summary",
        lambda *_args, **_kwargs: pytest.fail("stale summary was consumed"),
    )

    tlr._report_session_health(tmp_path)

    output = capsys.readouterr().err
    assert "Automatic Doctor is running" in output
    assert "Doctor inputs changed" in output


def test_console_startup_logs_automatic_doctor_progress(tmp_path, monkeypatch):
    from booley.harness import auto_doctor

    def run_if_due(_root, *, trigger, progress):
        assert trigger == "booley-run"
        progress("starting (automatic Doctor result expired)")
        progress("FuseSoC .core audit")
        return {}

    monkeypatch.setattr(auto_doctor, "run_if_due", run_if_due)
    monkeypatch.setattr(auto_doctor, "consume_changed_summary", lambda *_a, **_kw: None)
    monkeypatch.setattr(auto_doctor, "load_report", lambda _root: {})

    with patch.object(tlr.logger, "info") as log_info:
        tlr._run_automatic_doctor(tmp_path)

    assert ("Automatic Doctor — %s", "starting (automatic Doctor result expired)") in [
        call.args for call in log_info.call_args_list
    ]


def test_version_flag_prints_and_exits(capsys):
    # SETUP-1: `booley --version` should print a real version and exit 0.
    parser = tlr._build_parser()
    with pytest.raises(SystemExit) as exc:
        parser.parse_args(["--version"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert out.startswith("booley ")


def test_version_string_includes_commit_in_checkout(monkeypatch):
    # In a source checkout, the commit disambiguates the static packaged version.
    monkeypatch.setattr(tlr, "_source_commit", lambda: "abc1234")
    assert "(abc1234)" in tlr._version_string()


def test_version_string_bare_without_commit(monkeypatch):
    monkeypatch.setattr(tlr, "_source_commit", lambda: None)
    assert "(" not in tlr._version_string()


class TestBakedBuildCommit:
    """F-5: a wheel install has no .git, so `--version` reported no commit.

    Every freshly built image printed a bare `booley 0.1.0`, leaving the
    prescribed freshness check — confirm the wheel matches the commit — with
    nothing to check. build.sh now stamps the commit into the package.
    """

    def test_absent_stamp_module_yields_none(self, monkeypatch):
        monkeypatch.setattr(
            tlr, "_baked_commit", tlr._baked_commit
        )  # exercise the real implementation
        # No _build_commit module is generated in a checkout, so this is None.
        import booley

        assert not hasattr(booley, "_build_commit")
        assert tlr._baked_commit() is None

    def test_source_git_failure_does_not_borrow_baked_commit(self, tmp_path, monkeypatch):
        """A checkout must not combine its version with another build's stamp."""
        import booley
        from booley.runtime.version_attribution import VersionAttribution, VersionOrigin

        source_root = tmp_path / "checkout"
        source_root.mkdir()
        monkeypatch.setattr(
            booley,
            "version_attribution",
            VersionAttribution(
                version="4.5.6",
                origin=VersionOrigin.SOURCE,
                source_root=source_root,
            ),
        )
        monkeypatch.setattr(tlr, "_baked_commit", lambda: "cafe123")
        assert tlr._source_commit() is None
        assert "(cafe123)" not in tlr._version_string()

    def test_wheel_uses_baked_commit_without_live_git(self, monkeypatch):
        import booley
        from booley.runtime.version_attribution import VersionAttribution, VersionOrigin

        monkeypatch.setattr(
            booley,
            "version_attribution",
            VersionAttribution(
                version="4.5.6",
                origin=VersionOrigin.DISTRIBUTION,
                distribution_name="booley-rtl",
            ),
        )
        monkeypatch.setattr(
            VersionAttribution,
            "source_git_metadata",
            lambda _self: pytest.fail("wheel attribution must not inspect Git"),
        )
        monkeypatch.setattr(tlr, "_baked_commit", lambda: "cafe123")

        assert tlr._source_commit() == "cafe123"

    def test_live_checkout_wins_over_the_baked_commit(self, monkeypatch):
        """A checkout's live state is the truthful one — it can be +dirty."""
        from booley.runtime.version_attribution import VersionAttribution

        monkeypatch.setattr(
            VersionAttribution,
            "source_git_metadata",
            lambda _self: ("live999", "2026-08-30T10:39:40Z"),
        )
        monkeypatch.setattr(tlr, "_baked_commit", lambda: "stale00")
        assert tlr._source_commit() == "live999"

    def test_source_without_git_does_not_borrow_enclosing_repository(self, tmp_path, monkeypatch):
        import booley
        from booley.runtime.version_attribution import VersionAttribution, VersionOrigin

        outer = tmp_path / "outer"
        (outer / ".git").mkdir(parents=True)
        source_root = outer / "unpacked-booley"
        source_root.mkdir()
        monkeypatch.setattr(
            booley,
            "version_attribution",
            VersionAttribution(
                version="4.5.6",
                origin=VersionOrigin.SOURCE,
                source_root=source_root,
            ),
        )
        monkeypatch.setattr(tlr, "_baked_commit", lambda: "stale00")

        assert tlr._source_commit() is None

    def test_baked_commit_reads_the_generated_module(self, monkeypatch):
        import types

        import booley

        stamp = types.ModuleType("booley._build_commit")
        stamp.COMMIT = "abcd123+dirty"
        monkeypatch.setattr(booley, "_build_commit", stamp, raising=False)
        monkeypatch.setitem(sys.modules, "booley._build_commit", stamp)
        assert tlr._baked_commit() == "abcd123+dirty"

    def test_empty_stamp_is_treated_as_unknown(self, monkeypatch):
        """build.sh writes an empty COMMIT when git is unavailable."""
        import types

        import booley

        stamp = types.ModuleType("booley._build_commit")
        stamp.COMMIT = ""
        monkeypatch.setattr(booley, "_build_commit", stamp, raising=False)
        monkeypatch.setitem(sys.modules, "booley._build_commit", stamp)
        assert tlr._baked_commit() is None


# ===========================================================================
# _enforce_runtime_location() / _effective_command() -- container-only CLI
# ===========================================================================


def _host_venue(monkeypatch):
    """Simulate the host: no env marker, no /.dockerenv-style probe hits."""
    monkeypatch.delenv("BOOLEY_CONTAINER", raising=False)
    return patch.object(Path, "exists", lambda self: False)


class TestEnforceVenue:
    @pytest.mark.parametrize("command", ["run", "chat", "board"])
    def test_container_only_command_refused_on_host(self, command, monkeypatch, capsys):
        """Workflow commands on the host -> exit 2 with the actionable fix."""
        with _host_venue(monkeypatch), pytest.raises(SystemExit) as exc:
            tlr._enforce_runtime_location(command)
        assert exc.value.code == 2
        err = capsys.readouterr().err
        assert f"booley {command}" in err
        assert "Session Runtime" in err
        assert "Reopen in Container" in err  # names the fix, not just the rule

    @pytest.mark.parametrize("command", ["run", "chat", "board"])
    def test_container_only_command_allowed_in_container(self, command, monkeypatch):
        monkeypatch.setenv("BOOLEY_CONTAINER", "1")
        tlr._enforce_runtime_location(command)  # must not raise

    def test_host_only_init_refused_in_container(self, monkeypatch, capsys):
        """`booley init` builds the container boundary -> refused inside it."""
        monkeypatch.setenv("BOOLEY_CONTAINER", "1")
        with pytest.raises(SystemExit) as exc:
            tlr._enforce_runtime_location("init")
        assert exc.value.code == 2
        err = capsys.readouterr().err
        assert "booley init" in err
        assert "HOST terminal" in err

    def test_init_allowed_on_host(self, monkeypatch):
        with _host_venue(monkeypatch):
            tlr._enforce_runtime_location("init")  # must not raise

    @pytest.mark.parametrize("command", ["cheat", "doctor", None])
    def test_dual_venue_commands_never_guarded(self, command, monkeypatch):
        """cheat/doctor (and no command at all) run on either side."""
        with _host_venue(monkeypatch):
            tlr._enforce_runtime_location(command)  # host: must not raise
        monkeypatch.setenv("BOOLEY_CONTAINER", "1")
        tlr._enforce_runtime_location(command)  # container: must not raise

    def test_venue_table_contents(self):
        """Guard table matches ADR 0028 Decision 2.

        `session` joins `init` on the host side: it drives the Session Runtime
        from outside it, and the sandbox has no Docker to do that with (ADR 0016).
        `auth` is host-only too: it drives the host's browser OAuth flow and
        writes the host's ~/.config, neither of which exists in the sandbox.
        """
        assert {"run", "chat", "board"} == tlr._CONTAINER_ONLY_COMMANDS
        assert {"init", "session", "auth", "eda"} == tlr._HOST_ONLY_COMMANDS


class TestEffectiveCommand:
    def _parse(self, argv):
        return tlr._build_parser().parse_args(argv)

    def test_subcommand_passthrough(self):
        assert tlr._effective_command(self._parse(["run"])) == "run"
        assert tlr._effective_command(self._parse(["init"])) == "init"

    def test_legacy_board_flag_resolves_to_board(self):
        """Hidden flat `--board` flag must still hit the venue guard."""
        assert tlr._effective_command(self._parse(["--board"])) == "board"

    def test_legacy_doctor_and_cheat_flags(self):
        assert tlr._effective_command(self._parse(["--doctor"])) == "doctor"
        assert tlr._effective_command(self._parse(["--cheat"])) == "cheat"

    def test_no_command_defaults_to_chat(self):
        assert tlr._effective_command(self._parse([])) == "chat"


def test_prepare_review_board_parser():
    args = tlr._build_parser().parse_args(["board", "prepare-review", "demo-ticket", "--force"])
    assert args.board_command == "prepare-review"
    assert args.slug == "demo-ticket"
    assert args.force is True


def test_prepare_review_command_accepts_html_free_briefing(tmp_path, monkeypatch, capsys):
    from booley.harness import review_prep

    async def prepare(*_args, **_kwargs):
        return review_prep.ReviewPrepOutcome(
            "ready",
            "review briefing prepared; HTML explanation unavailable",
            package_path=tmp_path / "briefing.json",
        )

    monkeypatch.setattr(review_prep, "prepare_review_command", prepare)
    args = tlr._build_parser().parse_args(["board", "prepare-review", "demo-ticket"])

    assert tlr._cmd_board_prepare_review(args, tmp_path) == 0
    assert f"Review package ready: {tmp_path / 'briefing.json'}" in capsys.readouterr().out


def test_review_briefing_board_parser():
    args = tlr._build_parser().parse_args(
        ["board", "review-briefing", "demo-ticket", "--no-open-diffs"]
    )
    assert args.board_command == "review-briefing"
    assert args.slug == "demo-ticket"
    assert args.no_open_diffs is True


def test_blocked_briefing_board_parser():
    args = tlr._build_parser().parse_args(["board", "blocked-briefing", "demo-ticket"])
    assert args.board_command == "blocked-briefing"
    assert args.slug == "demo-ticket"


# ===========================================================================
# `booley shell` -- host-only fresh-sandbox shell (ADR 0016)
# ===========================================================================


class TestCmdShell:
    def test_shell_parser_accepts_net_and_command(self):
        parser = tlr._build_parser()
        args = tlr._normalize_args(
            parser,
            parser.parse_args(["shell", "--net", "--", "verilator", "-V"]),
        )
        assert args.command == "shell"
        assert args.net is True
        # REMAINDER keeps the leading `--`; the handler strips it.
        assert args.shell_cmd == ["--", "verilator", "-V"]

    def test_refuses_inside_container(self, monkeypatch, capsys):
        """Inside the sandbox -> exit code 2, no docker spawn attempted."""
        args = tlr._build_parser().parse_args(["shell"])
        monkeypatch.setenv("BOOLEY_CONTAINER", "1")  # venue.inside_session_runtime()
        rc = tlr._cmd_shell(args, Path("/work"))
        assert rc == 2
        assert "cannot run inside the Booley container" in capsys.readouterr().err

    def test_shell_warns_on_broken_project_config(self, monkeypatch, capsys):
        """An invalid [agent] provider raises BackendConfigError (a RuntimeError
        subclass); shell must warn-and-default, not traceback. Regression: the
        except set only caught (OSError, ValueError), so a fixable toml typo
        crashed `booley shell` with a raw traceback."""
        import booley.config.settings as cfgmod
        from booley.harness import sandbox as sandbox_mod

        monkeypatch.delenv("BOOLEY_CONTAINER", raising=False)

        def _boom(_root):
            raise RuntimeError("invalid provider 'nope'")

        monkeypatch.setattr(cfgmod, "load_models_config", _boom)
        sandbox = type("S", (), {"image": "img", "memory": ""})()
        monkeypatch.setattr(
            cfgmod,
            "get_backend_config",
            type("C", (), {"sandbox": sandbox}),
        )
        monkeypatch.setattr(
            sandbox_mod.DockerSandboxConfig,
            "verify",
            lambda self: "docker unavailable",
        )

        args = tlr._build_parser().parse_args(["shell"])
        rc = tlr._cmd_shell(args, Path("/work"))  # must not raise
        assert rc == 2
        err = capsys.readouterr().err
        assert "could not load project config" in err
        assert "invalid provider" in err

    def test_shell_honors_sandbox_memory_knob(self, monkeypatch):
        """[sandbox].memory reaches the shell container's --memory limit.

        The shell used to hardcode 4g, so a project whose EDA passes brush
        that ceiling (C910: sv2v over 485 files → flaky rc=137 OOM-kills)
        had no way to widen it. The shell must not be tighter than the
        Session Runtime the same Flows normally run in.
        """
        import booley.config.settings as cfgmod
        from booley.harness import sandbox as sandbox_mod

        monkeypatch.delenv("BOOLEY_CONTAINER", raising=False)
        monkeypatch.setattr(cfgmod, "load_models_config", lambda _root: None)
        sandbox_cfg = type("S", (), {"image": "img", "memory": "8g"})()
        backend_cfg = type("C", (), {"sandbox": sandbox_cfg})()
        monkeypatch.setattr(cfgmod, "get_backend_config", lambda: backend_cfg)
        captured: dict[str, str] = {}

        class _FakeRunner:
            def __init__(self, cfg, _root, label=""):
                captured["memory"] = cfg.memory_limit

            def ephemeral_argv(self, payload, tty=False):
                return ["true"]

            def cleanup_ephemeral(self):
                pass

        monkeypatch.setattr(sandbox_mod, "DockerRunner", _FakeRunner)
        monkeypatch.setattr(sandbox_mod.DockerSandboxConfig, "verify", lambda self: None)
        args = tlr._build_parser().parse_args(["shell", "--", "true"])
        rc = tlr._cmd_shell(args, Path("/work"))
        assert rc == 0
        assert captured["memory"] == "8g"

    def test_registered_as_early_command(self):
        assert tlr._EARLY_COMMANDS.get("shell") is tlr._cmd_shell

    def test_shell_hidden_from_help(self):
        """ADR 0028 Decision 2: `shell` keeps working but is stripped from
        `booley --help` (no help=, subparsers metavar hides the choice)."""
        parser = tlr._build_parser()
        assert "shell" not in parser.format_help()
        # ...while remaining fully functional.
        assert parser.parse_args(["shell"]).command == "shell"


# ===========================================================================
# `booley flow <name> [args...]` -- direct Booley Flow invocation
# ===========================================================================


class _FakeFlow:
    """Stand-in for a Booley Flow class: records the argv its main() was handed."""

    name = "fakeflow"
    description = "A fake Flow. Second sentence that must not be shown."
    calls: ClassVar[list[list[str]]] = []
    rc = 0

    def main(self, argv):
        type(self).calls.append(list(argv))
        return type(self).rc


class TestCmdFlow:
    @pytest.fixture(autouse=True)
    def _fake_registry(self, monkeypatch):
        """Point discovery + class loading at _FakeFlow, so no real Flow runs."""
        from booley.mcp.registry import McpToolInfo

        _FakeFlow.calls = []
        _FakeFlow.rc = 0
        info = McpToolInfo(
            name="fakeflow",
            path="flows/fakeflow.py",
            description=_FakeFlow.description,
            kind="flow",
        )
        monkeypatch.setattr(tlr, "_discover_project_mcp_tools", lambda _root: [info])
        monkeypatch.setattr(tlr, "_load_mcp_tool_class", lambda _info: _FakeFlow)

    def _args(self, argv):
        parser = tlr._build_parser()
        return tlr._normalize_args(parser, parser.parse_args(argv))

    def test_parser_captures_name_and_remainder(self):
        args = self._args(["flow", "lint", "--target", "lint"])
        assert args.command == "flow"
        assert args.endpoint_name == "lint"
        # REMAINDER hands the Flow's own flags through untouched.
        assert args.endpoint_args == ["--target", "lint"]

    def test_dispatch_calls_endpoint_with_its_argv(self):
        rc = tlr._cmd_flow(self._args(["flow", "fakeflow", "--target", "sim"]), Path("/work"))
        assert rc == 0
        assert _FakeFlow.calls == [["--target", "sim"]]

    def test_legacy_long_name_dispatches_to_canonical_flow(self, monkeypatch):
        from booley.mcp.registry import McpToolInfo

        info = McpToolInfo(
            name="sim", path="flows/sim/flow.py", description=_FakeFlow.description, kind="flow"
        )
        monkeypatch.setattr(tlr, "_discover_project_mcp_tools", lambda _root: [info])

        rc = tlr._cmd_flow(self._args(["flow", "simulate", "--target", "sim"]), Path("/work"))

        assert rc == 0
        assert _FakeFlow.calls == [["--target", "sim"]]

    def test_leading_double_dash_is_stripped(self):
        tlr._cmd_flow(self._args(["flow", "fakeflow", "--", "--target", "sim"]), Path("/work"))
        assert _FakeFlow.calls == [["--target", "sim"]]

    @pytest.mark.parametrize("rc", [0, 1, 2])
    def test_exit_code_passed_through_verbatim(self, rc):
        """lint exit 2 means "findings", not "crash" -- must not be collapsed."""
        _FakeFlow.rc = rc
        assert tlr._cmd_flow(self._args(["flow", "fakeflow"]), Path("/work")) == rc

    def test_unknown_endpoint_name_lists_available_endpoints(self, capsys):
        rc = tlr._cmd_flow(self._args(["flow", "nosuchtool"]), Path("/work"))
        assert rc == 2
        err = capsys.readouterr().err
        assert "not a flow" in err
        assert not _FakeFlow.calls

    def test_missing_flow_name_lists_available_flows(self, capsys):
        rc = tlr._cmd_flow(self._args(["flow"]), Path("/work"))
        assert rc == 2
        err = capsys.readouterr().err
        assert "needs a name" in err
        assert "fakeflow" in err

    def test_listing_trims_the_llm_facing_description(self, capsys):
        tlr._cmd_flow(self._args(["flow"]), Path("/work"))
        err = capsys.readouterr().err
        assert "A fake Flow" in err
        assert "must not be shown" not in err

    def test_unloadable_endpoint_errors_without_running(self, monkeypatch, capsys):
        monkeypatch.setattr(tlr, "_load_mcp_tool_class", lambda _info: None)
        rc = tlr._cmd_flow(self._args(["flow", "fakeflow"]), Path("/work"))
        assert rc == 2
        assert "could not load endpoint 'fakeflow'" in capsys.readouterr().err

    def test_registered_as_early_command(self):
        assert tlr._EARLY_COMMANDS.get("flow") is tlr._cmd_flow

    def test_advertised_in_help(self):
        help_text = tlr._build_parser().format_help()
        assert "flow" in help_text
        assert "Run a Booley Flow directly" in help_text

    def test_not_venue_guarded(self):
        """Booley Flows do their own venue/job-slot admission in ``main``;
        a second guard here would double-block them."""
        assert "flow" not in tlr._CONTAINER_ONLY_COMMANDS
        assert "flow" not in tlr._HOST_ONLY_COMMANDS


class TestEndpointResolution:
    """No mocks: the real registry -> real Flow class path."""

    def test_builtin_lint_resolves_to_its_class(self, tmp_path):
        infos = tlr._discover_project_mcp_tools(tmp_path)
        lint_info = next(i for i in infos if i.name == "lint")
        from booley.flows.lint.flow import LintFlow

        assert tlr._load_mcp_tool_class(lint_info) is LintFlow

    def test_project_custom_mcp_tool_is_discovered_and_loaded(self, tmp_path):
        mcp_tools_dir = tmp_path / ".booley_project" / "mcp_tools"
        mcp_tools_dir.mkdir(parents=True)
        (mcp_tools_dir / "custom_probe.py").write_text(
            "from booley.mcp.base import McpTool\n"
            "\n"
            "class CustomProbeMcpTool(McpTool):\n"
            '    name = "custom_probe"\n'
            '    description = "A project-local custom MCP tool."\n'
            "    def _add_args(self, parser):\n"
            "        pass\n"
            "    def run(self):\n"
            "        raise NotImplementedError\n",
            encoding="utf-8",
        )
        infos = tlr._discover_project_mcp_tools(tmp_path)
        info = next(i for i in infos if i.name == "custom_probe")
        cls = tlr._load_mcp_tool_class(info)
        assert cls is not None
        assert cls.name == "custom_probe"


# ===========================================================================
# _shutdown_requested()
# ===========================================================================


class TestShutdownRequested:
    def test_returns_false_when_event_is_none(self):
        original = tlr._shutdown_event
        try:
            tlr._shutdown_event = None
            assert tlr._shutdown_requested() is False
        finally:
            tlr._shutdown_event = original

    def test_returns_false_when_event_not_set(self):
        original = tlr._shutdown_event
        try:
            tlr._shutdown_event = threading.Event()
            assert tlr._shutdown_requested() is False
        finally:
            tlr._shutdown_event = original

    def test_returns_true_when_event_set(self):
        original = tlr._shutdown_event
        try:
            tlr._shutdown_event = threading.Event()
            tlr._shutdown_event.set()
            assert tlr._shutdown_requested() is True
        finally:
            tlr._shutdown_event = original


# ===========================================================================
# interruptible_sleep()
# ===========================================================================


class TestInterruptibleSleep:
    def test_completes_normally(self):
        """Short sleep completes, returns True."""
        original = tlr._shutdown_event
        try:
            tlr._shutdown_event = threading.Event()
            result = tlr.interruptible_sleep(0)
            assert result is True
        finally:
            tlr._shutdown_event = original

    def test_interrupted_returns_false(self):
        """Pre-set event causes immediate return with False."""
        original = tlr._shutdown_event
        try:
            tlr._shutdown_event = threading.Event()
            tlr._shutdown_event.set()
            result = tlr.interruptible_sleep(10)
            assert result is False
        finally:
            tlr._shutdown_event = original

    @patch("booley.harness.booley.time.sleep")
    def test_fallback_to_time_sleep_when_no_event(self, mock_sleep):
        """When _shutdown_event is None, falls back to time.sleep."""
        original = tlr._shutdown_event
        try:
            tlr._shutdown_event = None
            result = tlr.interruptible_sleep(5)
            mock_sleep.assert_called_once_with(5)
            assert result is True
        finally:
            tlr._shutdown_event = original

    def test_interrupted_by_thread(self):
        """Sleep interrupted mid-way by another thread setting the event."""
        original = tlr._shutdown_event
        try:
            tlr._shutdown_event = threading.Event()

            def set_after_delay():
                time.sleep(0.05)
                tlr._shutdown_event.set()

            t = threading.Thread(target=set_after_delay)
            t.start()
            t0 = time.monotonic()
            result = tlr.interruptible_sleep(10)
            elapsed = time.monotonic() - t0
            t.join()

            assert result is False
            assert elapsed < 2.0  # woke up well before 10s
        finally:
            tlr._shutdown_event = original


# ===========================================================================
# find_project_root()
# ===========================================================================


class TestFindProjectRoot:
    def test_finds_git_dir(self, tmp_path: Path):
        (tmp_path / ".git").mkdir()
        subdir = tmp_path / "a" / "b"
        subdir.mkdir(parents=True)
        with patch("booley.harness.booley.Path.cwd", return_value=subdir):
            result = tlr.find_project_root()
        assert result == tmp_path

    def test_skips_booley_dir(self, tmp_path: Path):
        """Should skip .booley even if it has .git."""
        rtl_dir = tmp_path / ".booley"
        rtl_dir.mkdir()
        (rtl_dir / ".git").mkdir()
        (tmp_path / ".git").mkdir()
        subdir = rtl_dir / "scripts"
        subdir.mkdir()
        with patch("booley.harness.booley.Path.cwd", return_value=subdir):
            result = tlr.find_project_root()
        assert result == tmp_path


# ===========================================================================
# find_venv_python()
# ===========================================================================


class TestFindVenvPython:
    def test_returns_sys_executable(self, tmp_path: Path):
        """With pip-installed booley, always returns sys.executable."""
        result = tlr.find_venv_python(tmp_path)
        import sys

        assert result == sys.executable


# ===========================================================================
# _run_board()
# ===========================================================================


class TestRunBoard:
    @patch("booley.harness.booley.subprocess.run")
    def test_passes_correct_command(self, mock_run, project_root: Path):
        mock_run.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="ok",
            stderr="",
        )
        tlr._run_board(project_root, ["classify", "--format", "counts"])
        call_args = mock_run.call_args
        cmd = call_args[0][0]
        assert "-m" in cmd
        assert tlr.BOARD_MODULE in cmd
        assert "classify" in cmd
        assert "--format" in cmd

    @patch("booley.harness.booley.subprocess.run")
    def test_cwd_is_project_root(self, mock_run, project_root: Path):
        mock_run.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="",
            stderr="",
        )
        tlr._run_board(project_root, ["classify"])
        call_kwargs = mock_run.call_args[1]
        assert call_kwargs["cwd"] == str(project_root)

    @patch("booley.harness.booley.subprocess.run")
    def test_logs_warning_on_failure(self, mock_run, project_root: Path):
        mock_run.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=1,
            stdout="",
            stderr="some error",
        )
        with patch.object(tlr.logger, "warning") as mock_warn:
            tlr._run_board(project_root, ["classify"])
            mock_warn.assert_called_once()


# ===========================================================================
# get_ticket_counts()
# ===========================================================================


class TestGetTicketCounts:
    @patch("booley.harness.booley._run_board")
    def test_parses_counts_output(self, mock_board):
        mock_board.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="executable=3\nblocked=1\nwaiting=0\nreview=2\norphaned=0\n",
            stderr="",
        )
        result = tlr.get_ticket_counts(Path("/fake"))
        assert result == {
            "executable": 3,
            "blocked": 1,
            "waiting": 0,
            "review": 2,
            "orphaned": 0,
        }

    @patch("booley.harness.booley._run_board")
    def test_returns_zeros_on_failure(self, mock_board):
        mock_board.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=1,
            stdout="",
            stderr="error",
        )
        result = tlr.get_ticket_counts(Path("/fake"))
        assert result["executable"] == 0

    @patch("booley.harness.booley._run_board")
    def test_returns_zeros_on_exception(self, mock_board):
        mock_board.side_effect = OSError("boom")
        result = tlr.get_ticket_counts(Path("/fake"))
        assert result == {
            "executable": 0,
            "active": 0,
            "blocked": 0,
            "waiting": 0,
            "review": 0,
            "orphaned": 0,
        }


# ===========================================================================
# get_active_slugs()
# ===========================================================================


class TestGetActiveSlugs:
    def test_returns_stems_of_md_files(self, project_root: Path):
        active_dir = project_root / ".booley" / "project" / "tickets" / "board" / "active"
        (active_dir / "fix-fsm.md").write_text("---\n---\n")
        (active_dir / "add-sha3.md").write_text("---\n---\n")
        result = tlr.get_active_slugs(project_root)
        assert sorted(result) == ["add-sha3", "fix-fsm"]

    def test_returns_empty_when_no_files(self, project_root: Path):
        assert tlr.get_active_slugs(project_root) == []

    def test_returns_empty_when_dir_missing(self, tmp_path: Path):
        assert tlr.get_active_slugs(tmp_path) == []


# ===========================================================================
# get_ticket_summary()
# ===========================================================================


class TestGetTicketSummary:
    def test_extracts_summary_from_frontmatter(self, project_root: Path):
        ticket = (
            project_root / ".booley" / "project" / "tickets" / "board" / "queue" / "fix-fsm.md"
        )
        ticket.write_text(
            "---\nsummary: Fix FSM counter overflow\ntype: bugfix\n---\n",
            encoding="utf-8",
        )
        assert tlr.get_ticket_summary(project_root, "fix-fsm") == "Fix FSM counter overflow"

    def test_strips_quotes(self, project_root: Path):
        ticket = (
            project_root / ".booley" / "project" / "tickets" / "board" / "queue" / "fix-fsm.md"
        )
        ticket.write_text(
            '---\nsummary: "Quoted summary"\n---\n',
            encoding="utf-8",
        )
        assert tlr.get_ticket_summary(project_root, "fix-fsm") == "Quoted summary"

    def test_searches_all_directories(self, project_root: Path):
        ticket = (
            project_root / ".booley" / "project" / "tickets" / "board" / "done" / "old-ticket.md"
        )
        ticket.write_text("---\nsummary: Old done ticket\n---\n", encoding="utf-8")
        assert tlr.get_ticket_summary(project_root, "old-ticket") == "Old done ticket"

    def test_returns_slug_when_not_found(self, project_root: Path):
        assert tlr.get_ticket_summary(project_root, "nonexistent") == "nonexistent"


# ===========================================================================
# handle_startup_orphans()
# ===========================================================================


class TestHandleStartupOrphans:
    @patch("booley.harness.orphan_handler.is_pid_alive", return_value=False)
    @patch("booley.harness.booley._run_board")
    def test_blocks_active_tickets(self, mock_board, _mock_alive, project_root: Path):
        mock_board.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="",
            stderr="",
        )
        # Create an orphaned ticket with a lock file containing a dead PID
        active = (
            project_root
            / ".booley"
            / "project"
            / "tickets"
            / "board"
            / "active"
            / "orphan-ticket.md"
        )
        active.write_text("---\nsummary: Orphan\n---\n", encoding="utf-8")
        lock_dir = project_root / ".booley" / "project" / "tickets" / "logs" / "orphan-ticket"
        lock_dir.mkdir(parents=True, exist_ok=True)
        (lock_dir / "ticket.lock").write_text("99999", encoding="utf-8")

        tlr.handle_startup_orphans(project_root)

        # Should have called _run_board with "block"
        calls = mock_board.call_args_list
        block_calls = [c for c in calls if "block" in c[0][1]]
        assert len(block_calls) == 1
        assert "orphan-ticket" in block_calls[0][0][1]

    @patch("booley.harness.booley._run_board")
    def test_noop_when_no_orphans(self, mock_board, project_root: Path):
        tlr.handle_startup_orphans(project_root)
        mock_board.assert_not_called()


# ===========================================================================
# handle_post_run_orphans()
# ===========================================================================


class TestHandlePostRunOrphans:
    def _create_orphan(self, project_root: Path, slug: str = "orphan") -> None:
        active = (
            project_root / ".booley" / "project" / "tickets" / "board" / "active" / f"{slug}.md"
        )
        active.write_text("---\nsummary: Orphan\n---\n", encoding="utf-8")

    @patch("booley.harness.booley._run_board")
    def test_requeue_on_limit_wait(self, mock_board, project_root: Path):
        """limit_wait > 0 -> requeue ticket."""
        mock_board.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="",
            stderr="",
        )
        self._create_orphan(project_root)
        tlr.handle_post_run_orphans(project_root, exit_code=1, limit_wait=3600)

        calls = mock_board.call_args_list
        requeue_calls = [c for c in calls if "requeue" in c[0][1]]
        assert len(requeue_calls) == 1

    @patch("booley.harness.booley._run_board")
    def test_block_on_clean_exit(self, mock_board, project_root: Path):
        """exit_code == 0 -> block for triage."""
        mock_board.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="",
            stderr="",
        )
        self._create_orphan(project_root)
        tlr.handle_post_run_orphans(project_root, exit_code=0, limit_wait=0)

        calls = mock_board.call_args_list
        block_calls = [c for c in calls if "block" in c[0][1]]
        assert len(block_calls) == 1

    @patch("booley.harness.booley._run_board")
    def test_fail_on_crash(self, mock_board, project_root: Path):
        """exit_code != 0, no limit -> fail ticket."""
        mock_board.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="",
            stderr="",
        )
        self._create_orphan(project_root)
        tlr.handle_post_run_orphans(project_root, exit_code=1, limit_wait=0)

        calls = mock_board.call_args_list
        fail_calls = [c for c in calls if "fail" in c[0][1]]
        assert len(fail_calls) == 1

    @patch("booley.harness.booley._run_board")
    def test_noop_when_no_orphans(self, mock_board, project_root: Path):
        tlr.handle_post_run_orphans(project_root, exit_code=0, limit_wait=0)
        mock_board.assert_not_called()


# ===========================================================================
# detect_subscription_limit()
# ===========================================================================


class TestDetectSubscriptionLimit:
    def _setup_failed_ticket(self, project_root: Path, slug: str, failure_text: str) -> None:
        """Create a blocked ticket + blocked.md with recent mtime."""
        blocked_dir = project_root / ".booley" / "project" / "tickets" / "board" / "blocked"
        blocked_dir.mkdir(parents=True, exist_ok=True)
        (blocked_dir / f"{slug}.md").write_text("---\n---\n")

        log_dir = project_root / ".booley" / "project" / "tickets" / "logs" / slug
        log_dir.mkdir(parents=True, exist_ok=True)
        blocked_md = log_dir / "blocked.md"
        blocked_md.write_text(failure_text, encoding="utf-8")

    def test_detects_usage_limit_pattern(self, project_root: Path):
        self._setup_failed_ticket(
            project_root,
            "fix-fsm",
            "Error: you've hit your usage limit for the day.",
        )
        result = tlr.detect_subscription_limit(project_root)
        assert result > 0

    def test_detects_subscription_limit_error(self, project_root: Path):
        self._setup_failed_ticket(
            project_root,
            "fix-fsm",
            "SubscriptionLimitError: rate limit exceeded",
        )
        result = tlr.detect_subscription_limit(project_root)
        assert result > 0

    def test_returns_zero_when_no_match(self, project_root: Path):
        self._setup_failed_ticket(
            project_root,
            "fix-fsm",
            "Error: syntax error in module my_adder.sv",
        )
        assert tlr.detect_subscription_limit(project_root) == 0

    def test_returns_zero_when_no_failed_dir(self, tmp_path: Path):
        assert tlr.detect_subscription_limit(tmp_path) == 0

    def test_ignores_old_failures(self, project_root: Path):
        """Failure.md older than 2 minutes should be ignored."""
        self._setup_failed_ticket(
            project_root,
            "fix-fsm",
            "Error: you've hit your usage limit",
        )
        # Backdate the blocked.md
        failure_md = (
            project_root / ".booley" / "project" / "tickets" / "logs" / "fix-fsm" / "blocked.md"
        )
        old_time = time.time() - 300  # 5 minutes ago
        import os

        os.utime(str(failure_md), (old_time, old_time))

        assert tlr.detect_subscription_limit(project_root) == 0

    def test_parses_reset_time_from_message(self, project_root: Path):
        self._setup_failed_ticket(
            project_root,
            "fix-fsm",
            "you've hit your limit, resets 8pm (UTC)",
        )
        result = tlr.detect_subscription_limit(project_root)
        assert result >= 60  # clamped to at least 60s

    def test_default_wait_when_no_reset_time(self, project_root: Path):
        self._setup_failed_ticket(
            project_root,
            "fix-fsm",
            "Error: usage limit reached",
        )
        assert tlr.detect_subscription_limit(project_root) == 3600

    def test_only_checks_last_entry(self, project_root: Path):
        """Old limit errors in append-only blocked.md should not trigger."""
        self._setup_failed_ticket(
            project_root,
            "fix-fsm",
            # Old entry with limit error, new entry without
            "## Run 1 -- Failed (2026-05-01T00:00:00Z)\n\n"
            "usage limit reached\n\n"
            "## Run 2 -- Blocked (2026-05-02T00:00:00Z)\n\n"
            "Need clarification on register width\n",
        )
        assert tlr.detect_subscription_limit(project_root) == 0


# ===========================================================================
# _time_ampm_tz_to_wait() (formerly _parse_reset_time)
# ===========================================================================

_has_tzdata = True
try:
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    ZoneInfo("UTC")
except ZoneInfoNotFoundError:
    _has_tzdata = False


@pytest.mark.skipif(not _has_tzdata, reason="tzdata package not installed")
class TestTimeAmpmTzToWait:
    def test_pm_conversion(self):
        """8pm -> hour 20."""
        result = sl._time_ampm_tz_to_wait("8", "pm", "UTC")
        assert result is not None
        assert 60 <= result <= 86400

    def test_am_conversion(self):
        result = sl._time_ampm_tz_to_wait("9", "am", "UTC")
        assert result is not None
        assert 60 <= result <= 86400

    def test_12am_edge_case(self):
        """12am -> hour 0 (midnight)."""
        result = sl._time_ampm_tz_to_wait("12", "am", "UTC")
        assert result is not None
        assert 60 <= result <= 86400

    def test_12pm_edge_case(self):
        """12pm -> hour 12 (noon)."""
        result = sl._time_ampm_tz_to_wait("12", "pm", "UTC")
        assert result is not None
        assert 60 <= result <= 86400

    def test_hour_minute_format(self):
        result = sl._time_ampm_tz_to_wait("8:30", "pm", "UTC")
        assert result is not None
        assert 60 <= result <= 86400

    def test_clamped_minimum(self):
        """Result should be at least 60s."""
        result = sl._time_ampm_tz_to_wait("8", "pm", "UTC")
        if result is not None:
            assert result >= 60

    def test_clamped_maximum(self):
        """Result should be at most 86400s."""
        result = sl._time_ampm_tz_to_wait("8", "pm", "UTC")
        if result is not None:
            assert result <= 86400

    def test_invalid_timezone_returns_none(self):
        result = sl._time_ampm_tz_to_wait("8", "pm", "Not/A/Timezone")
        assert result is None

    def test_non_numeric_time_returns_none(self):
        # Provider message format drift → non-numeric hour must not crash the
        # backoff path; the boundary guard returns None instead.
        assert sl._time_ampm_tz_to_wait("eight", "pm", "UTC") is None

    def test_out_of_range_hour_returns_none(self):
        # 25:00 → datetime.replace raises ValueError, caught at the boundary.
        assert sl._time_ampm_tz_to_wait("25", "am", "UTC") is None


# ===========================================================================
# _parse_codex_relative()
# ===========================================================================


class TestParseCodexRelative:
    def test_hours_and_minutes(self):
        result = sl._parse_codex_relative(
            "You've hit your usage limit. Try again in 3 hours 2 minutes."
        )
        assert result == 3 * 3600 + 2 * 60

    def test_hours_only(self):
        result = sl._parse_codex_relative("Try again in 5 hours.")
        assert result == 5 * 3600

    def test_minutes_only(self):
        result = sl._parse_codex_relative("Try again in 45 minutes.")
        assert result == 45 * 60

    def test_days_hours_minutes(self):
        result = sl._parse_codex_relative("Try again in 1 day 2 hours 30 minutes.")
        assert result == 86400  # clamped to 24h max

    def test_no_match_returns_none(self):
        result = sl._parse_codex_relative("Some unrelated error message")
        assert result is None

    def test_min_abbreviation(self):
        result = sl._parse_codex_relative("Try again in 10 min.")
        assert result == 600

    def test_clamped_minimum(self):
        result = sl._parse_codex_relative("Try again in 1 second.")
        assert result == 60  # clamped to 1min minimum


# ===========================================================================
# _parse_codex_absolute()
# ===========================================================================


class TestParseCodexAbsolute:
    def test_standard_format(self):
        result = sl._parse_codex_absolute("Try again at Jan 1st, 2099 12:00 AM")
        assert result is not None
        assert 60 <= result <= 86400

    def test_no_ordinal_suffix(self):
        result = sl._parse_codex_absolute("Try again at Apr 7, 2099 1:07 AM")
        assert result is not None

    def test_past_date_returns_none(self):
        result = sl._parse_codex_absolute("Try again at Jan 1st, 2020 12:00 AM")
        assert result is None

    def test_no_match_returns_none(self):
        result = sl._parse_codex_absolute("Some unrelated error message")
        assert result is None


# ===========================================================================
# _extract_reset_wait()
# ===========================================================================


class TestExtractResetWait:
    def test_codex_relative(self):
        result = sl._extract_reset_wait(
            "You've hit your usage limit. Try again in 2 hours 15 minutes."
        )
        assert result == 2 * 3600 + 15 * 60

    def test_codex_absolute_future(self):
        result = sl._extract_reset_wait(
            "You've hit your usage limit. Try again at Jan 1st, 2099 12:00 AM"
        )
        assert result is not None
        assert result > 0

    def test_no_reset_info(self):
        result = sl._extract_reset_wait("Quota exceeded. Check your plan.")
        assert result is None


# ===========================================================================
# _read_checkpoint_status()
# ===========================================================================


class TestReadCheckpointStatus:
    def test_reads_most_recent_status(self, project_root: Path):
        logs_dir = project_root / ".booley" / "project" / "tickets" / "logs"
        # Create two status files
        s1_dir = logs_dir / "old-ticket" / ".runtime"
        s1_dir.mkdir(parents=True)
        s1 = s1_dir / "status.json"
        s1.write_text(
            json.dumps(
                {
                    "slug": "old-ticket",
                    "step": "planning",
                    "last_updated": (datetime.now(UTC) - timedelta(hours=1)).strftime(
                        "%Y-%m-%dT%H:%M:%SZ"
                    ),
                }
            ),
            encoding="utf-8",
        )

        s2_dir = logs_dir / "new-ticket" / ".runtime"
        s2_dir.mkdir(parents=True)
        s2 = s2_dir / "status.json"
        now_iso = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        s2.write_text(
            json.dumps(
                {
                    "slug": "new-ticket",
                    "step": "sim-debug-loop",
                    "last_updated": now_iso,
                }
            ),
            encoding="utf-8",
        )

        # Touch s2 to ensure it's newer
        import os

        os.utime(str(s2), None)

        result = tlr._read_checkpoint_status(project_root)
        assert result is not None
        assert "new-ticket" in result
        assert "sim-debug-loop" in result

    def test_returns_none_when_no_status(self, project_root: Path):
        assert tlr._read_checkpoint_status(project_root) is None

    def test_returns_none_when_logs_dir_missing(self, tmp_path: Path):
        assert tlr._read_checkpoint_status(tmp_path) is None

    def test_includes_age_string(self, project_root: Path):
        logs_dir = project_root / ".booley" / "project" / "tickets" / "logs" / "my-ticket"
        runtime_dir = logs_dir / ".runtime"
        runtime_dir.mkdir(parents=True)
        sf = runtime_dir / "status.json"
        # 5 minutes ago
        ts = (datetime.now(UTC) - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
        sf.write_text(
            json.dumps(
                {
                    "slug": "my-ticket",
                    "step": "impl",
                    "last_updated": ts,
                }
            ),
            encoding="utf-8",
        )
        result = tlr._read_checkpoint_status(project_root)
        assert "5m ago" in result

    def test_stale_status_no_stuck_warning(self, project_root: Path):
        """Stale status no longer emit 'possibly stuck' -- timeout handles it."""
        logs_dir = project_root / ".booley" / "project" / "tickets" / "logs" / "stuck-ticket"
        runtime_dir = logs_dir / ".runtime"
        runtime_dir.mkdir(parents=True)
        sf = runtime_dir / "status.json"
        # 50 minutes ago
        ts = (datetime.now(UTC) - timedelta(minutes=50)).strftime("%Y-%m-%dT%H:%M:%SZ")
        sf.write_text(
            json.dumps(
                {
                    "slug": "stuck-ticket",
                    "step": "synthesis",
                    "last_updated": ts,
                }
            ),
            encoding="utf-8",
        )
        result = tlr._read_checkpoint_status(project_root)
        assert "possibly stuck" not in result
        assert "50m ago" in result

    def test_handles_corrupt_json(self, project_root: Path):
        logs_dir = project_root / ".booley" / "project" / "tickets" / "logs" / "bad"
        runtime_dir = logs_dir / ".runtime"
        runtime_dir.mkdir(parents=True)
        (runtime_dir / "status.json").write_text("{invalid json", encoding="utf-8")
        assert tlr._read_checkpoint_status(project_root) is None

    def test_developer_step_reads_active_endpoint_from_display(self, project_root: Path):
        """When status says 'developer', heartbeat checks display.jsonl for an active endpoint."""
        logs_dir = project_root / ".booley" / "project" / "tickets" / "logs" / "my-ticket"
        runtime_dir = logs_dir / ".runtime"
        runtime_dir.mkdir(parents=True)
        ts = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        (runtime_dir / "status.json").write_text(
            json.dumps(
                {
                    "slug": "my-ticket",
                    "step": "developer",
                    "last_updated": ts,
                }
            ),
            encoding="utf-8",
        )
        # Endpoint started but not ended → active
        (runtime_dir / "display.jsonl").write_text(
            json.dumps({"type": "endpoint_start", "endpoint": "tb_coder"}) + "\n",
            encoding="utf-8",
        )
        result = tlr._read_checkpoint_status(project_root)
        assert "implementing" in result
        assert "tb_coder" in result

    def test_developer_falls_back_when_no_display(self, project_root: Path):
        """Without display.jsonl, 'developer' step shown as-is."""
        logs_dir = project_root / ".booley" / "project" / "tickets" / "logs" / "my-ticket"
        runtime_dir = logs_dir / ".runtime"
        runtime_dir.mkdir(parents=True)
        ts = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        (runtime_dir / "status.json").write_text(
            json.dumps(
                {
                    "slug": "my-ticket",
                    "step": "developer",
                    "last_updated": ts,
                }
            ),
            encoding="utf-8",
        )
        result = tlr._read_checkpoint_status(project_root)
        assert "running developer" in result

    def test_developer_ignores_completed_endpoints(self, project_root: Path):
        """Endpoints that have both start and end events are not 'active'."""
        logs_dir = project_root / ".booley" / "project" / "tickets" / "logs" / "my-ticket"
        runtime_dir = logs_dir / ".runtime"
        runtime_dir.mkdir(parents=True)
        ts = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        (runtime_dir / "status.json").write_text(
            json.dumps(
                {
                    "slug": "my-ticket",
                    "step": "developer",
                    "last_updated": ts,
                }
            ),
            encoding="utf-8",
        )
        events = [
            json.dumps({"type": "endpoint_start", "endpoint": "lint"}),
            json.dumps({"type": "endpoint_end", "endpoint": "lint", "exit_code": 0}),
        ]
        (runtime_dir / "display.jsonl").write_text("\n".join(events) + "\n", encoding="utf-8")
        result = tlr._read_checkpoint_status(project_root)
        # No active endpoint → falls back to developer
        assert "running developer" in result

    def test_legacy_checkpoint_json_fallback(self, project_root: Path):
        """Old checkpoint.json files still work as fallback."""
        logs_dir = project_root / ".booley" / "project" / "tickets" / "logs" / "legacy"
        logs_dir.mkdir(parents=True)
        ts = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        (logs_dir / "checkpoint.json").write_text(
            json.dumps(
                {
                    "slug": "legacy",
                    "current_step": "setup",
                    "last_updated": ts,
                }
            ),
            encoding="utf-8",
        )
        result = tlr._read_checkpoint_status(project_root)
        assert result is not None
        assert "legacy" in result


class TestActiveEndpointFromDisplay:
    def test_returns_open_endpoint(self, tmp_path: Path):
        display = tmp_path / ".runtime" / "display.jsonl"
        display.parent.mkdir()
        display.write_text(
            json.dumps({"type": "endpoint_start", "endpoint": "sim"}) + "\n",
            encoding="utf-8",
        )
        assert tlr._active_endpoint_from_display(tmp_path) == ("sim", None)

    def test_returns_none_when_endpoint_closed(self, tmp_path: Path):
        display = tmp_path / ".runtime" / "display.jsonl"
        display.parent.mkdir()
        events = [
            json.dumps({"type": "endpoint_start", "endpoint": "lint"}),
            json.dumps({"type": "endpoint_end", "endpoint": "lint", "exit_code": 0}),
        ]
        display.write_text("\n".join(events) + "\n", encoding="utf-8")
        assert tlr._active_endpoint_from_display(tmp_path) is None

    def test_returns_latest_open_endpoint(self, tmp_path: Path):
        display = tmp_path / ".runtime" / "display.jsonl"
        display.parent.mkdir()
        events = [
            json.dumps({"type": "endpoint_start", "endpoint": "lint"}),
            json.dumps({"type": "endpoint_end", "endpoint": "lint", "exit_code": 0}),
            json.dumps({"type": "endpoint_start", "endpoint": "tb_coder"}),
        ]
        display.write_text("\n".join(events) + "\n", encoding="utf-8")
        assert tlr._active_endpoint_from_display(tmp_path) == ("tb_coder", None)

    def test_returns_none_when_no_file(self, tmp_path: Path):
        assert tlr._active_endpoint_from_display(tmp_path) is None

    def test_skips_malformed_json(self, tmp_path: Path):
        display = tmp_path / ".runtime" / "display.jsonl"
        display.parent.mkdir()
        display.write_text(
            "not json\n" + json.dumps({"type": "endpoint_start", "endpoint": "reviewer"}) + "\n",
            encoding="utf-8",
        )
        assert tlr._active_endpoint_from_display(tmp_path) == ("reviewer", None)


# ===========================================================================
# _run_with_heartbeat()
# ===========================================================================


class TestRunWithHeartbeat:
    @patch("booley.harness.booley.subprocess.Popen")
    def test_returns_exit_code_without_heartbeat(self, mock_popen, project_root: Path):
        """When heartbeat import fails, falls back to plain Popen."""
        proc_mock = MagicMock()
        proc_mock.returncode = 42
        proc_mock.wait.return_value = None
        mock_popen.return_value = proc_mock

        with (
            patch.dict("sys.modules", {"booley.runtime.heartbeat": None}),
            patch(
                "builtins.__import__",
                side_effect=lambda name, *a, **kw: (
                    (_ for _ in ()).throw(ImportError())
                    if name == "booley.runtime.heartbeat"
                    else __import__(name, *a, **kw)
                ),
            ),
        ):
            result = tlr._run_with_heartbeat(
                ["python", "-c", "pass"],
                str(project_root),
                project_root,
            )
        assert result == 42

    @patch("booley.harness.booley.subprocess.Popen")
    def test_heartbeat_start_stop(self, mock_popen, project_root: Path):
        """When heartbeat is available, start/stop are called."""
        proc_mock = MagicMock()
        proc_mock.returncode = 0
        proc_mock.wait.return_value = None
        mock_popen.return_value = proc_mock

        mock_hb_cls = MagicMock()
        mock_hb_inst = MagicMock()
        mock_hb_cls.return_value = mock_hb_inst

        mock_module = MagicMock()
        mock_module.Heartbeat = mock_hb_cls

        with patch.dict("sys.modules", {"booley.runtime.heartbeat": mock_module}):
            result = tlr._run_with_heartbeat(
                ["python", "-c", "pass"],
                str(project_root),
                project_root,
            )
        assert result == 0
        mock_hb_inst.start.assert_called_once()
        mock_hb_inst.stop.assert_called_once()


# ===========================================================================
# setup_logging()
# ===========================================================================


class TestSetupLogging:
    def setup_method(self):
        """Clear handlers before each test to avoid cross-contamination."""
        tlr.logger.handlers.clear()

    def test_creates_log_file(self, project_root: Path):
        tlr.setup_logging(project_root, verbose=False)
        from booley.ticket_board.helpers import tickets_dir_from_project_root

        log_path = tickets_dir_from_project_root(project_root) / tlr.LOOP_LOG_REL
        assert log_path.exists()

    def test_adds_console_and_file_handlers(self, project_root: Path):
        tlr.setup_logging(project_root, verbose=False)
        handler_types = [type(h) for h in tlr.logger.handlers]
        assert logging.StreamHandler in handler_types
        assert logging.FileHandler in handler_types

    def test_verbose_sets_debug_console(self, project_root: Path):
        tlr.setup_logging(project_root, verbose=True)
        console = next(
            h
            for h in tlr.logger.handlers
            if isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler)
        )
        assert console.level == logging.DEBUG

    def test_normal_sets_info_console(self, project_root: Path):
        tlr.setup_logging(project_root, verbose=False)
        console = next(
            h
            for h in tlr.logger.handlers
            if isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler)
        )
        assert console.level == logging.INFO

    def teardown_method(self):
        # Close file handlers to avoid ResourceWarning
        for h in tlr.logger.handlers[:]:
            if isinstance(h, logging.FileHandler):
                h.close()
        tlr.logger.handlers.clear()


# ===========================================================================
# TerseFormatter (now in logging_utils, canonical home after M5 dedup)
# ===========================================================================


class TestTerseFormatter:
    def test_info_no_level_prefix(self):
        from booley.harness.logging_utils import TerseFormatter

        fmt = TerseFormatter(datefmt="%H:%M:%S")
        record = logging.LogRecord(
            "test",
            logging.INFO,
            "",
            0,
            "hello world",
            (),
            None,
        )
        result = fmt.format(record)
        assert "hello world" in result
        assert "INFO" not in result

    def test_warning_includes_level(self):
        from booley.harness.logging_utils import TerseFormatter

        fmt = TerseFormatter(datefmt="%H:%M:%S")
        record = logging.LogRecord(
            "test",
            logging.WARNING,
            "",
            0,
            "bad thing",
            (),
            None,
        )
        result = fmt.format(record)
        assert "WARNING" in result
        assert "bad thing" in result

    def test_error_includes_level(self):
        from booley.harness.logging_utils import TerseFormatter

        fmt = TerseFormatter(datefmt="%H:%M:%S")
        record = logging.LogRecord(
            "test",
            logging.ERROR,
            "",
            0,
            "worse thing",
            (),
            None,
        )
        result = fmt.format(record)
        assert "ERROR" in result


# ===========================================================================
# _signal_handler()
# ===========================================================================


class TestSignalHandler:
    def test_sets_shutdown_event(self):
        original = tlr._shutdown_event
        try:
            tlr._shutdown_event = threading.Event()
            assert not tlr._shutdown_event.is_set()
            tlr._signal_handler(2, None)
            assert tlr._shutdown_event.is_set()
        finally:
            tlr._shutdown_event = original

    def test_noop_when_event_none(self):
        original = tlr._shutdown_event
        try:
            tlr._shutdown_event = None
            tlr._signal_handler(2, None)  # should not raise
        finally:
            tlr._shutdown_event = original


# ===========================================================================
# LIMIT_PATTERNS constant
# ===========================================================================


class TestLimitPatterns:
    @pytest.mark.parametrize(
        "text",
        [
            "you've hit your limit",
            "You've hit your limit for the day",
            "usage limit exceeded",
            "subscription limit reached",
            "SubscriptionLimitError: too many requests",
            "Codex error: You've hit your usage limit. Upgrade to Pro",
        ],
    )
    def test_matches_expected_strings(self, text):
        assert any(p.search(text) for p in sl.LIMIT_PATTERNS)

    @pytest.mark.parametrize(
        "text",
        [
            "syntax error in module",
            "compilation failed",
            "assertion failed at time 100ns",
        ],
    )
    def test_does_not_match_unrelated(self, text):
        assert not any(p.search(text) for p in sl.LIMIT_PATTERNS)


# ===========================================================================
# Constants
# ===========================================================================


class TestConstants:
    def test_venv_python_removed(self):
        """VENV_PYTHON removed — package is pip-installed, no embedded venv."""
        assert not hasattr(tlr, "VENV_PYTHON")

    def test_board_module(self):
        assert tlr.BOARD_MODULE == "booley.ticket_board"

    def test_heartbeat_interval(self):
        assert tlr.HEARTBEAT_INTERVAL == 300

    def test_stale_threshold_removed(self):
        """STALE_THRESHOLD removed -- stale detection no longer needed."""
        assert not hasattr(tlr, "STALE_THRESHOLD")


# ===========================================================================
# A0: board/ path correctness
# ===========================================================================


class TestBoardPathCorrectness:
    """Verify that get_active_slugs, get_ticket_summary, and
    detect_subscription_limit use the correct board/ paths."""

    def test_get_active_slugs_uses_board_path(self, project_root: Path):
        """Files in tickets/board/active/ are found."""
        active_dir = project_root / ".booley" / "project" / "tickets" / "board" / "active"
        (active_dir / "foo.md").write_text("---\n---\n")
        result = tlr.get_active_slugs(project_root)
        assert result == ["foo"]

    def test_get_active_slugs_ignores_old_path(self, project_root: Path):
        """Files in tickets/active/ (wrong, missing board/) are NOT found."""
        old_dir = project_root / ".booley" / "project" / "tickets" / "active"
        old_dir.mkdir(parents=True, exist_ok=True)
        (old_dir / "foo.md").write_text("---\n---\n")
        result = tlr.get_active_slugs(project_root)
        assert result == []

    def test_detect_subscription_limit_uses_board_path(self, project_root: Path):
        """detect_subscription_limit scans tickets/board/blocked/."""
        # Create in board/blocked/ (correct path)
        blocked_dir = project_root / ".booley" / "project" / "tickets" / "board" / "blocked"
        (blocked_dir / "test-slug.md").write_text("---\n---\n")
        log_dir = project_root / ".booley" / "project" / "tickets" / "logs" / "test-slug"
        log_dir.mkdir(parents=True, exist_ok=True)
        (log_dir / "blocked.md").write_text("usage limit exceeded", encoding="utf-8")
        result = tlr.detect_subscription_limit(project_root)
        assert result > 0


# ===========================================================================
# A4/A5: PID-aware orphan handling
# ===========================================================================


class TestStartupOrphansPIDAware:
    def _create_active_with_lock(self, project_root: Path, slug: str, pid: int):
        """Create an active ticket + lock file with given PID."""
        active = (
            project_root / ".booley" / "project" / "tickets" / "board" / "active" / f"{slug}.md"
        )
        active.write_text(f"---\nsummary: {slug}\n---\n", encoding="utf-8")
        lock_dir = project_root / ".booley" / "project" / "tickets" / "logs" / slug
        lock_dir.mkdir(parents=True, exist_ok=True)
        (lock_dir / "ticket.lock").write_text(str(pid), encoding="utf-8")

    @patch("booley.harness.booley._run_board")
    def test_skips_live_pid(self, mock_board, project_root: Path):
        """Tickets with a live PID should NOT be blocked."""
        import os

        self._create_active_with_lock(project_root, "live-ticket", os.getpid())
        mock_board.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="",
            stderr="",
        )
        tlr.handle_startup_orphans(project_root)
        # Should NOT have called block
        block_calls = [c for c in mock_board.call_args_list if "block" in c[0][1]]
        assert len(block_calls) == 0

    @patch("booley.harness.booley._run_board")
    def test_blocks_dead_pid(self, mock_board, project_root: Path):
        """Tickets with a dead PID should be blocked."""
        self._create_active_with_lock(project_root, "dead-ticket", 999999)
        mock_board.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="",
            stderr="",
        )
        tlr.handle_startup_orphans(project_root)
        block_calls = [c for c in mock_board.call_args_list if "block" in c[0][1]]
        assert len(block_calls) == 1
        assert "dead-ticket" in block_calls[0][0][1]


class TestPostRunOrphansPIDAware:
    def _create_active_with_lock(self, project_root: Path, slug: str, pid: int):
        active = (
            project_root / ".booley" / "project" / "tickets" / "board" / "active" / f"{slug}.md"
        )
        active.write_text(f"---\nsummary: {slug}\n---\n", encoding="utf-8")
        lock_dir = project_root / ".booley" / "project" / "tickets" / "logs" / slug
        lock_dir.mkdir(parents=True, exist_ok=True)
        (lock_dir / "ticket.lock").write_text(str(pid), encoding="utf-8")

    @patch("booley.harness.booley._run_board")
    def test_skips_other_live_pid(self, mock_board, project_root: Path):
        """Post-run: tickets owned by a *different* live process should be skipped."""
        import os

        # Use parent PID -- guaranteed alive and != os.getpid()
        other_pid = os.getppid()
        self._create_active_with_lock(project_root, "live-ticket", other_pid)
        mock_board.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="",
            stderr="",
        )
        tlr.handle_post_run_orphans(project_root, exit_code=1, limit_wait=0)
        mock_board.assert_not_called()

    @patch("booley.harness.booley._run_board")
    def test_handles_own_pid(self, mock_board, project_root: Path):
        """Post-run: tickets locked by our own PID should be handled, not skipped.

        When the loop runner calls orphan sweep after SIGINT, the lock file
        holds the runner's own PID (still alive). The ticket must still be
        handled -- otherwise it becomes a stale orphan once the runner exits.
        """
        import os

        self._create_active_with_lock(project_root, "own-ticket", os.getpid())
        mock_board.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="",
            stderr="",
        )
        tlr.handle_post_run_orphans(project_root, exit_code=130, limit_wait=0)
        fail_calls = [c for c in mock_board.call_args_list if "fail" in c[0][1]]
        assert len(fail_calls) == 1
        assert "own-ticket" in fail_calls[0][0][1]


# ===========================================================================
# _check_fast_failure()
# ===========================================================================


class TestCheckFastFailure:
    """Fast harness-failure classification: race-vs-infra.

    Regression for the slug-mode hang: when a specific --ticket is requested,
    _run_harness pre-activates that slug itself, so the post-failure
    "0 executable" recheck reflects our OWN activation, not another runner.
    The old code misread that as a race and polled forever, masking fatal
    preflight failures (e.g. Docker permission denied).
    """

    @staticmethod
    def _args(slug="", wait=5):
        from argparse import Namespace

        return Namespace(slug=slug, wait=wait)

    def test_success_returns_none(self):
        assert tlr._check_fast_failure(self._args(), Path(), exit_code=0, elapsed=0.1) is None

    def test_slow_failure_returns_none(self):
        # >=5s failures aren't "fast" -- normal post-run handling applies.
        assert tlr._check_fast_failure(self._args(), Path(), exit_code=2, elapsed=9.0) is None

    def test_slug_mode_fast_failure_aborts_without_race_recheck(self):
        # Slug mode: a fast failure is always infra -> abort, and we must NOT
        # consult ticket counts (our own pre-activation would read as a race).
        with (
            patch.object(tlr, "get_ticket_counts") as mock_counts,
            patch.object(tlr, "interruptible_sleep") as mock_sleep,
        ):
            action = tlr._check_fast_failure(
                self._args(slug="my-ticket"),
                Path(),
                exit_code=2,
                elapsed=0.6,
            )
        assert action == "abort"
        mock_counts.assert_not_called()
        mock_sleep.assert_not_called()

    def test_queue_mode_race_resumes_polling(self):
        # Queue-polling mode (no slug): 0 executable after a fast failure means
        # another runner grabbed it -> resume polling.
        with (
            patch.object(tlr, "get_ticket_counts", return_value={"executable": 0}),
            patch.object(tlr, "interruptible_sleep", return_value=True),
        ):
            action = tlr._check_fast_failure(
                self._args(),
                Path(),
                exit_code=2,
                elapsed=0.6,
            )
        assert action == "continue"

    def test_queue_mode_infra_error_aborts(self):
        # Queue mode but ticket still executable -> genuine infra error -> abort.
        with patch.object(tlr, "get_ticket_counts", return_value={"executable": 1}):
            action = tlr._check_fast_failure(
                self._args(),
                Path(),
                exit_code=2,
                elapsed=0.6,
            )
        assert action == "abort"

    def test_queue_mode_race_break_on_shutdown(self):
        # Shutdown during the race-poll sleep -> break.
        with (
            patch.object(tlr, "get_ticket_counts", return_value={"executable": 0}),
            patch.object(tlr, "interruptible_sleep", return_value=False),
        ):
            action = tlr._check_fast_failure(
                self._args(),
                Path(),
                exit_code=2,
                elapsed=0.6,
            )
        assert action == "break"


# ===========================================================================
# _handle_post_run() -- fast-failure orphan sweep
# ===========================================================================


class TestHandlePostRunFastFailure:
    """A fast-failure 'abort' must still sweep the pre-activated ticket.

    Regression: slug-mode pre-activation strands the ticket in active/ when
    the abort path returns before the orphan safety net. The race/shutdown
    paths must NOT sweep -- another runner owns the ticket.
    """

    @staticmethod
    def _args(slug="my-ticket", wait=5):
        from argparse import Namespace

        return Namespace(slug=slug, wait=wait)

    def test_abort_sweeps_orphans(self):
        with (
            patch.object(tlr, "_check_fast_failure", return_value="abort"),
            patch.object(tlr, "handle_post_run_orphans") as mock_orphans,
            patch.object(tlr, "detect_subscription_limit") as mock_limit,
        ):
            action = tlr._handle_post_run(self._args(), Path(), exit_code=2, elapsed=0.6)
        assert action == "abort"
        mock_orphans.assert_called_once()
        # exit_code is forwarded so the orphan is FAILED (not blocked).
        assert mock_orphans.call_args[0][1] == 2
        # Abort short-circuits before subscription-limit detection.
        mock_limit.assert_not_called()

    def test_race_continue_does_not_sweep(self):
        with (
            patch.object(tlr, "_check_fast_failure", return_value="continue"),
            patch.object(tlr, "handle_post_run_orphans") as mock_orphans,
        ):
            action = tlr._handle_post_run(self._args(slug=""), Path(), exit_code=2, elapsed=0.6)
        assert action == "continue"
        mock_orphans.assert_not_called()


# ===========================================================================
# --dry-run implications (F-12)
# ===========================================================================


class TestDryRunImplications:
    """`booley run --dry-run` used to block forever headlessly: the idle poll
    waited for a ticket it would never execute, and the TUI took the terminal."""

    def _parse(self, argv: list[str]):
        parser = tlr._build_parser()
        return tlr._normalize_args(parser, parser.parse_args(argv))

    def test_dry_run_implies_one_shot(self):
        assert self._parse(["run", "--dry-run"]).count == 1

    def test_dry_run_implies_no_console(self):
        assert self._parse(["run", "--dry-run"]).no_console is True

    def test_explicit_count_wins(self):
        assert self._parse(["run", "--dry-run", "-n", "3"]).count == 3

    def test_plain_run_still_polls_forever(self):
        args = self._parse(["run"])
        assert args.count == 0
        assert args.no_console is False

    def test_dry_run_never_starts_the_console(self):
        assert tlr._will_use_console(self._parse(["run", "--dry-run"])) is False

    def test_idle_exit_names_dry_run_not_a_phantom_n_flag(self):
        args = self._parse(["run", "--dry-run"])
        counts = {"active": 0, "waiting": 0, "blocked": 0, "review": 0, "executable": 0}
        with patch.object(tlr, "status") as status:
            action = tlr._handle_idle(args, counts, tlr._IdleState())
        assert action == "break"
        assert "--dry-run" in status.call_args.args[0]

    def test_idle_exit_still_names_n_when_user_passed_it(self):
        args = self._parse(["run", "-n", "2"])
        counts = {"active": 0, "waiting": 0, "blocked": 0, "review": 0, "executable": 0}
        with patch.object(tlr, "status") as status:
            action = tlr._handle_idle(args, counts, tlr._IdleState())
        assert action == "break"
        assert "-n 2" in status.call_args.args[0]

    def test_preview_only_reads_board_and_environment(self, tmp_path):
        args = self._parse(["run", "--dry-run"])
        counts = {
            "active": 0,
            "waiting": 0,
            "blocked": 0,
            "review": 0,
            "executable": 1,
        }
        with (
            patch.object(tlr, "get_ticket_counts", return_value=counts) as classify,
            patch.object(tlr, "find_venv_python", return_value="/venv/python") as find_python,
            patch.object(tlr, "_log_attempt") as log_attempt,
            patch.object(tlr, "_show_dry_run") as show,
        ):
            rc = tlr._preview_ticket_run(args, tmp_path)

        assert rc == 0
        classify.assert_called_once_with(tmp_path)
        find_python.assert_called_once_with(tmp_path)
        log_attempt.assert_called_once_with(args, 1, counts)
        show.assert_called_once_with("/venv/python")

    def test_main_bypasses_mutating_runtime_for_preview(self, tmp_path, monkeypatch):
        args = self._parse(["run", "--dry-run", "--project-root", str(tmp_path)])
        preview = MagicMock(return_value=0)
        monkeypatch.setattr(tlr, "_parse_cli", lambda: args)
        monkeypatch.setattr(tlr, "_enforce_runtime_location", lambda _command: None)
        monkeypatch.setattr(tlr.runtime_context, "ensure_proxy_env", lambda: False)
        monkeypatch.setattr(tlr, "_handle_early_exits", lambda *_args: None)
        monkeypatch.setattr(tlr, "_print_banner", lambda _args: None)
        monkeypatch.setattr(tlr, "_preview_ticket_run", preview)
        monkeypatch.setattr(
            tlr,
            "_setup_runtime",
            lambda *_args: pytest.fail("dry-run must not set up mutating runtime state"),
        )
        monkeypatch.setattr(
            tlr,
            "_ticket_loop",
            lambda *_args: pytest.fail("dry-run must not enter the mutating ticket loop"),
        )

        assert tlr.main() == 0
        preview.assert_called_once_with(args, tmp_path)


class TestNamedTicketImplications:
    """A named ticket must not turn into a long-lived queue runner."""

    def _parse(self, argv: list[str]):
        parser = tlr._build_parser()
        return tlr._normalize_args(parser, parser.parse_args(argv))

    def test_named_ticket_is_one_shot(self):
        assert self._parse(["run", "--ticket", "fix-crc"]).count == 1

    def test_named_ticket_overrides_count(self):
        assert self._parse(["run", "--ticket", "fix-crc", "-n", "2"]).count == 1


class TestCheckReady:
    def _parse(self, argv: list[str]):
        parser = tlr._build_parser()
        return tlr._normalize_args(parser, parser.parse_args(argv))

    def test_parser_exposes_no_agent_readiness_mode(self):
        args = self._parse(["run", "--ticket", "demo", "--check-ready"])
        assert args.check_ready is True
        assert args.ticket == "demo"

    def test_readiness_reports_validation_errors(self, tmp_path, monkeypatch, capsys):
        result = MagicMock(errors=("bad criterion",), warnings=())
        check = MagicMock(return_value=result)
        monkeypatch.setattr("booley.ticket_board.readiness.check_ticket_ready", check)
        args = self._parse(["run", "--ticket", "demo", "--check-ready"])

        assert tlr._check_ticket_readiness(args, tmp_path) == 2
        assert "bad criterion" in capsys.readouterr().err
        check.assert_called_once_with(tmp_path, "demo")


class TestIdleShutdown:
    """`booley run` must not outlive its queue (F-50)."""

    def _parse(self, argv: list[str]):
        parser = tlr._build_parser()
        return tlr._normalize_args(parser, parser.parse_args(argv))

    @staticmethod
    def _counts(**kw):
        base = {"active": 0, "waiting": 0, "blocked": 0, "review": 0, "executable": 0}
        base.update(kw)
        return base

    def test_idle_timeout_defaults_to_a_finite_value(self):
        assert self._parse(["run"]).idle_timeout == tlr.DEFAULT_IDLE_TIMEOUT_S
        assert tlr.DEFAULT_IDLE_TIMEOUT_S > 0

    def test_drained_board_exits_after_the_timeout(self, monkeypatch):
        args = self._parse(["run", "--idle-timeout", "60"])
        idle = tlr._IdleState()
        clock = [1000.0]
        monkeypatch.setattr(tlr.time, "monotonic", lambda: clock[0])
        monkeypatch.setattr(tlr, "interruptible_sleep", lambda _s: True)

        with patch.object(tlr, "status"):
            # First drained poll only arms the timer.
            assert tlr._handle_idle(args, self._counts(review=2), idle) == "continue"
            assert idle.drained_since == 1000.0
            clock[0] += 59
            assert tlr._handle_idle(args, self._counts(review=2), idle) == "continue"
            clock[0] += 2
            assert tlr._handle_idle(args, self._counts(review=2), idle) == "break"

    def test_active_or_waiting_tickets_keep_the_runner_alive(self, monkeypatch):
        """Work that can still become executable on its own must not trip the timer."""
        args = self._parse(["run", "--idle-timeout", "1"])
        idle = tlr._IdleState()
        clock = [1000.0]
        monkeypatch.setattr(tlr.time, "monotonic", lambda: clock[0])
        monkeypatch.setattr(tlr, "interruptible_sleep", lambda _s: True)

        with patch.object(tlr, "status"):
            for counts in (self._counts(active=1), self._counts(waiting=1)):
                clock[0] += 10_000
                assert tlr._handle_idle(args, counts, idle) == "continue"
                assert idle.drained_since is None

    def test_new_work_disarms_the_timer(self, monkeypatch):
        args = self._parse(["run", "--idle-timeout", "60"])
        idle = tlr._IdleState()
        clock = [1000.0]
        monkeypatch.setattr(tlr.time, "monotonic", lambda: clock[0])
        monkeypatch.setattr(tlr, "interruptible_sleep", lambda _s: True)

        with patch.object(tlr, "status"):
            tlr._handle_idle(args, self._counts(review=1), idle)
            idle.reset()  # the loop calls this when a ticket becomes executable
            clock[0] += 10_000
            assert tlr._handle_idle(args, self._counts(review=1), idle) == "continue"

    def test_zero_timeout_polls_forever(self, monkeypatch):
        args = self._parse(["run", "--idle-timeout", "0"])
        idle = tlr._IdleState()
        clock = [1000.0]
        monkeypatch.setattr(tlr.time, "monotonic", lambda: clock[0])
        monkeypatch.setattr(tlr, "interruptible_sleep", lambda _s: True)

        with patch.object(tlr, "status"):
            for _ in range(3):
                clock[0] += 10_000
                assert tlr._handle_idle(args, self._counts(), idle) == "continue"

    def test_drained_board_announces_the_pending_exit(self, monkeypatch):
        args = self._parse(["run", "--idle-timeout", "60"])
        idle = tlr._IdleState()
        monkeypatch.setattr(tlr, "interruptible_sleep", lambda _s: True)
        with patch.object(tlr, "status") as status:
            tlr._handle_idle(args, self._counts(review=1), idle)
        assert any("Queue drained" in c.args[0] for c in status.call_args_list)


class TestClaimTicketSlotShutdown:
    def test_queued_claim_aborts_on_shutdown_event(self, tmp_path, monkeypatch):
        # A Runner queued behind max_tickets busy Developers must stop
        # waiting when Ctrl+C sets the shutdown event (the SIGINT handler
        # never raises, so without the hook the wait was uninterruptible).
        import os

        from booley.runtime import job_slots

        monkeypatch.setenv("BOOLEY_SLOTS_DIR", str(tmp_path / "slots"))
        # Occupy every ticket slot so the claim queues.
        world_store = job_slots.SlotStore(tmp_path / "slots", job_slots.SlotCaps(max_tickets=1))
        holder = world_store.submit(job_slots.CLASS_TICKET, pid=os.getpid())
        assert world_store.refresh(holder).state == job_slots.HOLDING

        event = threading.Event()
        event.set()
        monkeypatch.setattr(tlr, "_shutdown_event", event)
        with pytest.raises(job_slots.ClaimAbortedError):
            tlr._claim_ticket_slot(tmp_path)
        # The withdrawn claim leaves no waiter behind.
        _holders, waiters = world_store.snapshot(job_slots.CLASS_TICKET)
        assert waiters == []
        world_store.release(holder)


class TestBoardProjectRoot:
    """fpu F-41: `booley run` took --project-root and `booley board` refused it
    (`unrecognized arguments`), so driving another checkout's board meant cd-ing
    or exporting BOOLEY_PROJECT_DIR."""

    @staticmethod
    def _parse(argv):
        parser = tlr._build_parser()
        return tlr._normalize_args(parser, parser.parse_args(argv))

    @pytest.mark.parametrize(
        "argv",
        [
            ["board", "--project-root", "/tmp/proj"],
            ["board", "-p", "/tmp/proj", "show"],
            ["board", "show", "--project-root", "/tmp/proj"],
            ["board", "move", "slug", "done", "-p", "/tmp/proj"],
            ["board", "create", "slug", "-p", "/tmp/proj"],
            ["board", "reset", "slug", "-p", "/tmp/proj"],
            ["board", "archive", "-p", "/tmp/proj"],
        ],
    )
    def test_accepted_on_both_sides_of_the_subcommand(self, argv):
        args = self._parse(argv)

        assert args.command == "board"
        assert args.project_root == "/tmp/proj"

    def test_subparser_does_not_clobber_the_parent_value(self):
        """default=SUPPRESS is load-bearing — subparsers share the namespace."""
        assert self._parse(["board", "-p", "/tmp/proj", "show"]).project_root == "/tmp/proj"

    def test_absent_leaves_discovery_to_find_project_root(self):
        """main() falls back to find_project_root() when the attr is unset."""
        args = self._parse(["board", "show"])

        assert not getattr(args, "project_root", "")

    def test_reset_accepts_correction_reason(self):
        args = self._parse(["board", "reset", "slug", "--reason", "review rejected it"])

        assert args.reason == "review rejected it"

    def test_run_still_takes_it(self):
        assert self._parse(["run", "--project-root", "/tmp/proj"]).project_root == "/tmp/proj"


def test_hidden_session_prepare_accepts_explicit_workspace_root():
    """Dev Containers need not run initializeCommand from the workspace cwd."""
    parser = tlr._build_parser()
    args = tlr._normalize_args(
        parser,
        parser.parse_args(["session", "prepare", "--project-root", "/tmp/project"]),
    )

    assert args.command == "session"
    assert args.session_command == "prepare"
    assert args.project_root == "/tmp/project"
