"""Tests for booley.py."""

from __future__ import annotations

import os
import re
import shutil
import sys
import types
from argparse import Namespace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar
from unittest.mock import MagicMock, Mock, patch

import pytest

import booley
from booley import __version__
from booley.harness import booley as tlr
from booley.harness import bootstrap_cli
from booley.runtime import health, host_install
from booley.runtime import project_image as pi
from booley.runtime.booley_dir import reset_cache
from booley.runtime.lifecycle_lock import LifecycleLockError
from booley.runtime.sandbox_apply import UNKNOWN_SANDBOX_STEPS
from booley.runtime.version_attribution import VersionAttribution, VersionOrigin
from tests.docker_host_support import fake_docker_host
from tests.subprocess_budget import run_bounded_child

TICKETS_REL = Path(".booley") / "tickets"


@pytest.fixture(autouse=True)
def _outside_any_job(monkeypatch):
    """CLI dispatch tests never record a Standalone Job into a real Project (e.g. inside a Sandbox)."""
    for name in ("BOOLEY_DIR", "BOOLEY_RUN_ID", "_BOOLEY_CLI_INVOCATION_ORIGIN"):
        monkeypatch.delenv(name, raising=False)


# ===========================================================================
# Parser flags
# ===========================================================================


def _parse_argv(monkeypatch, *argv):
    # Kept private: the parser's only public entry point, main(), also dispatches the command.
    monkeypatch.setattr(sys, "argv", ["booley", *argv])
    return tlr._parse_cli()


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["doctor", "--deep"], {"command": "doctor", "deep": True}),
        (["doctor", "--concise"], {"concise": True}),
        (["doctor", "--deep", "--skip-agent-checks"], {"skip_agent_checks": True}),
        (
            ["init", "--provider", "codex", "--auth", "api-key"],
            {"provider": "codex", "auth": "api_key"},
        ),
        (["init", "--scaffold", "demo"], {"stealth": False, "style_lint": None}),
        (
            ["init", "--scaffold", "demo", "--stealth", "--no-style-lint"],
            {"stealth": True, "style_lint": False},
        ),
        (["init", "--scaffold", "demo", "--style-lint"], {"style_lint": True}),
        (["init", "--skip-credentials"], {"skip_credentials": True}),
        (
            ["bootstrap", "--update", "--verbose"],
            {"command": "bootstrap", "update": True, "verbose": True, "project_root": "absent"},
        ),
        (["bootstrap", "--upgrade"], {"update": True}),
        (["bootstrap", "--with-qa-skills", "--force"], {"with_qa_skills": True, "force": True}),
        (["bootstrap", "--without-qa-skills"], {"without_qa_skills": True}),
        (["bootstrap"], {"egress": None}),
        (["bootstrap", "--egress", "unproxied"], {"egress": "unproxied"}),
        (["bootstrap", "--check-only", "--egress", "proxy"], {"egress": "proxy"}),
    ],
)
def test_parser_reads_command_flags(argv, expected, monkeypatch):
    args = vars(_parse_argv(monkeypatch, *argv))
    assert {key: args.get(key, "absent") for key in expected} == expected


def test_bare_booley_in_the_sandbox_opens_chat(monkeypatch):
    monkeypatch.setattr(tlr.runtime_context, "inside_session_runtime", lambda: True)
    args = _parse_argv(monkeypatch)
    assert (args.command, args.agent_args) == ("chat", [])


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        (
            ["doctor", "--concise", "--verbose"],
            "--verbose/-v: not allowed with argument --concise",
        ),
        (["doctor", "--skip-agent-checks"], "--skip-agent-checks requires --deep"),
        (["init", "--sim-tool", "verilator"], "--sim-tool is retired; use --sim-eda-tool"),
        (["init", "--lint-tool", "verilator"], "--lint-tool is retired; use --lint-eda-tool"),
        (["init", "--provider", "codex", "--auth", "auto"], "choose subscription or api-key"),
        (["init", "--provider", "codex", "--auth", "visitor"], "choose subscription or api-key"),
        (["bootstrap", "--check-only", "--force"], "not allowed with argument"),
        (["bootstrap", "--with-qa-skills", "--without-qa-skills"], "not allowed with argument"),
        (["bootstrap", "--egress", "open"], "invalid choice"),
        (["chat", "--cli", "bogus"], "invalid choice: 'bogus'"),
        (["chat", "--name"], "--name: expected one argument"),
        *(
            (["chat", "--name", label], "argument --name: use 1-32 letters")
            for label in ["", "   ", "a/b", "x" * 33, ".hidden", "café", "tab\there"]
        ),
        *(([removed], "invalid choice") for removed in ["mcp-tool", "link-guidance", "tool"]),
    ],
)
def test_usage_errors_exit_two_naming_the_problem(argv, message, monkeypatch, capsys):
    with pytest.raises(SystemExit) as raised:
        _parse_argv(monkeypatch, *argv)
    assert raised.value.code == 2
    assert message in capsys.readouterr().err


def test_chat_forwards_arguments_after_chat(monkeypatch):
    assert _parse_argv(monkeypatch, "chat", "resume", "--last").agent_args == ["resume", "--last"]
    assert _parse_argv(monkeypatch, "chat", "--resume", "abc").agent_args == ["--resume", "abc"]
    assert _parse_argv(monkeypatch, "chat", "--resume", "abc").cli is None
    # The first `chat` is the value of --project; only the second is the command.
    # `_cli_selection` stays private: -C is resolved to a Project only at dispatch.
    args = _parse_argv(monkeypatch, "--project", "chat", "chat", "--resume", "x", "--", "y")
    assert args.agent_args == ["--resume", "x", "--", "y"]
    assert args._cli_selection.value == "chat"
    # Leading Booley options stay Booley's; the rest is forwarded.
    args = _parse_argv(monkeypatch, "chat", "--project", "P", "--continue")
    assert args.agent_args == ["--continue"]
    assert args._cli_selection.value == "P"


def test_chat_cli_option_is_booleys_only_when_leading(monkeypatch):
    args = _parse_argv(monkeypatch, "chat", "--cli", "codex", "--resume", "x")
    assert (args.cli, args.agent_args) == ("codex", ["--resume", "x"])
    assert _parse_argv(monkeypatch, "chat", "--cli=codex").cli == "codex"
    # `_cli_selection` stays private: -C is resolved to a Project only at dispatch.
    args = _parse_argv(monkeypatch, "chat", "-C", "P", "--cli", "codex")
    assert (args.cli, args._cli_selection.value) == ("codex", "P")
    # After the agent's own arguments, `--cli` belongs to the agent CLI.
    args = _parse_argv(monkeypatch, "chat", "--resume", "x", "--cli", "codex")
    assert (args.cli, args.agent_args) == (None, ["--resume", "x", "--cli", "codex"])


def test_chat_name_option_is_booleys_only_when_leading(monkeypatch):
    # Bug caught: a leading `--name X` / `--name=X` not parsed into `session_name`, or `--name`
    # after the agent's own arguments swallowed instead of forwarded.
    args = _parse_argv(monkeypatch, "chat", "--name", "uart-fix", "--resume", "x")
    assert (args.session_name, args.agent_args) == ("uart-fix", ["--resume", "x"])
    args = _parse_argv(monkeypatch, "chat", "--cli", "codex", "--name=uart-fix", "resume")
    assert (args.cli, args.session_name, args.agent_args) == ("codex", "uart-fix", ["resume"])
    assert _parse_argv(monkeypatch, "chat").session_name is None
    args = _parse_argv(monkeypatch, "chat", "--resume", "x", "--name", "y")
    assert (args.session_name, args.agent_args) == (None, ["--resume", "x", "--name", "y"])
    # `--` hands even a leading `--name` to the agent (Claude's own session name).
    args = _parse_argv(monkeypatch, "chat", "--", "--name", "y")
    assert (args.session_name, args.agent_args) == (None, ["--name", "y"])


def test_chat_name_is_trimmed_and_length_bounded(monkeypatch):
    assert _parse_argv(monkeypatch, "chat", "--name", "  uart fix  ").session_name == "uart fix"
    assert _parse_argv(monkeypatch, "chat", "--name", "x" * 32).session_name == "x" * 32


def test_chat_later_booley_options_are_forwarded(monkeypatch, capsys):
    """Only leading -h/-C/--project are Booley's; later ones belong to the agent."""
    for tail in (["--resume", "x", "--project", "P"], ["--resume", "x", "-C", "P"]):
        args = _parse_argv(monkeypatch, "chat", *tail)
        assert args.agent_args == tail
        # `_cli_selection` stays private: -C is resolved to a Project only at dispatch.
        assert not hasattr(args, "_cli_selection")
    assert _parse_argv(monkeypatch, "chat", "--resume", "x", "--help").agent_args == [
        "--resume",
        "x",
        "--help",
    ]
    assert _parse_argv(monkeypatch, "chat", "--", "--help").agent_args == ["--help"]
    assert capsys.readouterr().out == ""


def test_chat_help_documents_passthrough_and_options(monkeypatch, capsys):
    with pytest.raises(SystemExit) as raised:
        _parse_argv(monkeypatch, "chat", "--help")
    assert raised.value.code == 0
    help_text = " ".join(capsys.readouterr().out.split())
    for text in (
        "passed unchanged to the Agent CLI",
        "booley chat -- --help",
        "overrides [worktree] chat for this launch",
        "--cli {claude,codex}",
        "must be listed in booley.toml [agent] clis",
        "[--name NAME]",
        "S3 · NAME",
        "resumed in its worktree keeps its label unless --name sets a new one",
        "--here and --name only before any other argument",
    ):
        assert text in help_text, text


def test_auth_cli_and_hidden_app_alias_parse_identically(monkeypatch, capsys):
    by_cli = _parse_argv(monkeypatch, "auth", "--cli", "codex", "--status")
    by_app = _parse_argv(monkeypatch, "auth", "--app", "codex", "--status")

    assert vars(by_cli) == vars(by_app)
    assert by_cli.app == "codex"
    with pytest.raises(SystemExit):
        _parse_argv(monkeypatch, "auth", "--help")
    auth_help = capsys.readouterr().out
    assert "--cli" in auth_help
    assert "--app" not in auth_help
    words = " ".join(auth_help.split())
    assert (
        "status reports this CLI only, else every Agent CLI the Project allows, default first"
        in words
    )


# ===========================================================================
# Dispatch before Project discovery
# ===========================================================================


@pytest.fixture
def host_main(tmp_path, monkeypatch):
    """Run `booley <argv>` through main() on the host, from a fresh Project checkout."""
    (tmp_path / ".git").mkdir()
    (tmp_path / ".booley").mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("RTL_PROJECT_ROOT", raising=False)
    reset_cache()
    monkeypatch.setattr(tlr.runtime_context, "inside_session_runtime", lambda: False)
    monkeypatch.setattr(tlr.runtime_context, "ensure_proxy_env", lambda: False)
    monkeypatch.setattr(host_install, "host_install_error", lambda _source: None)

    def run(*argv):
        monkeypatch.setattr(sys, "argv", ["booley", *argv])
        return tlr.main()

    return run


def _no_project_discovery():
    raise AssertionError("Project discovery must not run")


def test_source_checkout_refuses_project_commands_but_not_feedback(
    host_main, tmp_path, monkeypatch, capsys
):
    from booley.feedback import cli as feedback_cli

    shutil.rmtree(tmp_path / ".booley")
    (tmp_path / "pyproject.toml").write_text("[tool.booley]\nsource_checkout = true\n")
    roots = []
    monkeypatch.setattr(feedback_cli, "run", lambda _args, root, **_kw: roots.append(root) or 0)

    assert host_main("doctor") == 2
    assert "cannot be initialized or used as a Project" in capsys.readouterr().err
    assert host_main("feedback", "list") == 0
    assert roots == [tmp_path]


def test_bootstrap_dispatch_precedes_project_discovery(host_main, monkeypatch):
    monkeypatch.setattr(bootstrap_cli, "run_bootstrap", lambda _args: 17)
    monkeypatch.setattr(tlr, "find_project_root", _no_project_discovery)
    assert host_main("bootstrap", "--check-only") == 17


def test_projects_dispatch_precedes_active_project_discovery(host_main, monkeypatch):
    from booley.harness import projects_view
    from booley.projects import cli as project_inventory_cli
    from booley.runtime import image_keepers

    seen = {}
    monkeypatch.setattr(project_inventory_cli, "run", lambda _args, **kw: seen.update(kw) or 19)
    monkeypatch.setattr(tlr, "find_project_root", _no_project_discovery)

    assert host_main("projects", "--json") == 19
    assert seen == {
        "keeper_operations": image_keepers,
        "leftovers_view": projects_view.render_leftovers,
    }


def test_noncanonical_host_installation_stops_before_dispatch(host_main, monkeypatch, capsys):
    from booley.projects import cli as project_inventory_cli
    from booley.runtime import paths

    sources = []
    monkeypatch.setattr(paths, "skills_dir", lambda: Path("/installed/skills"))
    monkeypatch.setattr(
        host_install,
        "host_install_error",
        lambda source: sources.append(source) or "not the canonical host installation",
    )
    monkeypatch.setattr(project_inventory_cli, "run", lambda *_a, **_kw: pytest.fail("dispatched"))

    assert host_main("projects", "--json") == 2
    assert sources == [Path("/installed/skills")]
    assert "not the canonical" in capsys.readouterr().err


def _fail_bootstrap(monkeypatch, failure):
    monkeypatch.setattr(bootstrap_cli, "run_bootstrap", MagicMock(side_effect=failure))


def _fail_init(monkeypatch, failure):
    from booley.harness import init_cmd

    monkeypatch.setattr(init_cmd, "run_init", MagicMock(side_effect=failure))


def _fail_doctor(monkeypatch, failure):
    from booley.harness import doctor
    from booley.runtime import session_runtime

    def exercise_runtime(*_args, **_kwargs):
        # Kept private: only Doctor's own flow runtime starts the Sandbox, and through Docker.
        doctor._DoctorFlowRuntime(Path("/project"), "docker").command(["true"])

    monkeypatch.setattr(doctor, "run_doctor_result", exercise_runtime)
    monkeypatch.setattr(session_runtime, "up_without_recovery", MagicMock(side_effect=failure))


def _fail_auth(monkeypatch, failure):
    from booley.harness import auth_cmd

    # Kept private: the public run_auth probes Docker first and mints through a live Agent CLI.
    monkeypatch.setattr(auth_cmd, "_preflight", lambda *_args: True)
    monkeypatch.setattr(auth_cmd, "_acquire", MagicMock(side_effect=failure))


def _fail_eda(monkeypatch, failure):
    # Importing the coordinator first keeps modules that bind the lock by
    # name (session_refresh) from capturing the patched callable.
    from booley.harness import eda_grants

    monkeypatch.setattr(
        eda_grants.lifecycle_lock, "host_lifecycle_lock", MagicMock(side_effect=failure)
    )


@pytest.mark.parametrize(
    ("argv", "fail"),
    [
        (["bootstrap"], _fail_bootstrap),
        (["init"], _fail_init),
        (["doctor"], _fail_doctor),
        (["auth", "--cli", "claude"], _fail_auth),
        (["eda", "grant", "revoke", "/project", "--kind", "vivado"], _fail_eda),
    ],
    ids=["bootstrap", "init", "doctor", "auth", "eda"],
)
def test_lifecycle_timeout_reaches_main_once_without_traceback(
    argv, fail, host_main, monkeypatch, capsys
):
    failure = LifecycleLockError("host Docker lifecycle timed out after waiting 120s")
    fail(monkeypatch, failure)

    assert host_main(*argv) == 2
    captured = capsys.readouterr()
    assert captured.err == f"ERROR: {failure}\n"
    assert str(failure) not in captured.out  # `auth` prints its own heading first


# ===========================================================================
# Session health and upgrade reports at `booley session up`
# ===========================================================================


@pytest.fixture
def stub_session(tmp_path, monkeypatch):
    """`session up` against a stub Sandbox; records upgrade observation and Sandbox start."""
    from booley.harness import doctor, upgrade_review
    from booley.runtime import session_runtime

    (tmp_path / ".booley").mkdir()
    events = []
    current = SimpleNamespace(condition=upgrade_review.ReviewCondition.CURRENT)
    monkeypatch.setattr(
        upgrade_review, "observe", lambda _dir: events.append("observe") or current
    )
    monkeypatch.setattr(
        session_runtime,
        "ensure",
        lambda *_a, **_kw: events.append("up") or session_runtime.EnsureResult("s", False, True),
    )
    monkeypatch.setattr(
        doctor, "observe_deep_status", lambda *_a, **_kw: MagicMock(render=lambda: "deep")
    )
    monkeypatch.setattr(
        health, "consume_changed_snapshot", lambda *_a, **_kw: pytest.fail("summary consumed")
    )
    # Kept private: main() would resolve the host Session context through Docker first.
    return SimpleNamespace(events=events, up=lambda: tlr._session_up(Namespace(), tmp_path))


def test_session_up_observes_upgrade_before_runtime_start(stub_session, monkeypatch):
    monkeypatch.setattr(health, "due_reason", lambda _root: None)
    monkeypatch.setattr(health, "consume_changed_snapshot", lambda *_a, **_kw: None)

    assert stub_session.up() == 0
    assert stub_session.events == ["observe", "up"]


@pytest.mark.parametrize(
    "due_reasons",
    [("Doctor inputs changed", None), (None, "Doctor inputs changed")],
    ids=["due-at-startup", "due-after-startup"],
)
def test_due_doctor_is_announced_instead_of_persisted_findings(
    due_reasons, stub_session, monkeypatch, capsys
):
    reasons = iter(due_reasons)
    monkeypatch.setattr(health, "due_reason", lambda _root: next(reasons))

    assert stub_session.up() == 0
    assert (
        "Automatic Doctor is running in the Sandbox (Doctor inputs changed); "
        "persisted findings from before startup will not be reported as current."
    ) in capsys.readouterr().err


def _health_project_views(tmp_path: Path, *, force_copy: bool) -> tuple[Path, Path, Path]:
    host = tmp_path / "host-project"
    directory = host / ".booley"
    directory.mkdir(parents=True)
    (directory / "booley.toml").write_text("[project]\nname = 'unit'\n")
    (host / "design.core").write_text("authored core")
    sandbox = tmp_path / "work"
    try:
        if force_copy:
            raise PermissionError("exercise hosts without symlink privileges")
        sandbox.symlink_to(host, target_is_directory=True)
    except OSError:
        shutil.copytree(host, sandbox)
    return host, sandbox, directory


def _healthy_session_probe(calls):
    from booley.audit.diagnostic_results import DiagnosticFinding, Severity
    from booley.harness import doctor

    calls.append("probe")
    print("healthy probe")
    return doctor.DoctorRunResult(
        counts={"pass": 1, "fail": 0, "warn": 0, "waived": 0, "note": 0, "skip": 0},
        findings=(DiagnosticFinding(Severity.PASS, "healthy"),),
        exit_code=0,
    )


def _fix_session_health_clock(monkeypatch):
    import booley

    class FixedClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 10, 16, 12, tzinfo=UTC)

    monkeypatch.setattr(booley, "__version__", "1.2.3")
    monkeypatch.setattr(health, "datetime", FixedClock)
    monkeypatch.setattr(health, "utc_now_rfc3339", lambda: "2026-10-16T12:00:00Z")


@pytest.fixture
def mounted_health_project(tmp_path, monkeypatch, request):
    from booley.harness import auto_doctor, doctor
    from booley.runtime import session_runtime

    monkeypatch.delenv("BOOLEY_DIR", raising=False)
    reset_cache()
    host, sandbox, directory = _health_project_views(
        tmp_path, force_copy=getattr(request, "param", False)
    )
    _fix_session_health_clock(monkeypatch)
    calls, starts = [], []

    def run(trigger):
        if not sandbox.is_symlink():
            shutil.copytree(
                directory,
                sandbox / ".booley",
                dirs_exist_ok=True,
                ignore=shutil.ignore_patterns("doctor"),
            )
        reset_cache()
        result = auto_doctor.run_if_due(sandbox, trigger=trigger)
        if result is not None and not sandbox.is_symlink():
            for path_fn in (health.report_path, health.transcript_path):
                target = path_fn(directory)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path_fn(sandbox / ".booley"), target)
        reset_cache()
        return result

    def ensure(*_args, **_kwargs):
        started = not starts
        starts.append(started)
        if started:
            assert run("session-start") is not None
        return session_runtime.EnsureResult("session", False, started)

    monkeypatch.setattr(
        doctor, "run_doctor_result", lambda *_a, **_kw: _healthy_session_probe(calls)
    )
    monkeypatch.setattr(
        doctor, "observe_deep_status", lambda *_a, **_kw: MagicMock(render=lambda: "deep")
    )
    monkeypatch.setattr(session_runtime, "ensure", ensure)
    try:
        yield host, sandbox, directory, calls, starts, run
    finally:
        reset_cache()


@pytest.mark.parametrize(
    "mounted_health_project", [False, True], ids=["symlinked", "copied"], indirect=True
)
def test_session_up_does_not_repeat_doctor_for_unchanged_mount_view(
    mounted_health_project, capsys
):
    host, sandbox, directory, calls, starts, run = mounted_health_project

    def session_up():
        # Kept private: main() would resolve the host Session context through Docker first.
        return tlr._session_up(Namespace(), host)

    assert session_up() == 0
    capsys.readouterr()
    upgrade_before = (directory / "runtime" / "upgrade_review.json").read_bytes()
    paths = (health.report_path(directory), health.transcript_path(directory))
    before = [(path.read_bytes(), path.stat().st_mtime_ns) for path in paths]
    assert session_up() == 0
    assert "Doctor inputs changed" not in capsys.readouterr().err, (
        "unchanged second session up must not report input drift"
    )
    assert calls == ["probe"]
    assert starts == [True, False]
    assert [(path.read_bytes(), path.stat().st_mtime_ns) for path in paths] == before
    assert (directory / "runtime" / "upgrade_review.json").read_bytes() == upgrade_before
    assert run("adapter-current") is None
    assert calls == ["probe"]
    for view in (host, sandbox):
        (view / ".booley" / "booley.toml").write_text("[project]\nname = 'edited'\n")
    assert "booley.toml" in health.due_reason(host)
    assert "booley.toml" in health.due_reason(sandbox)
    assert session_up() == 0
    assert "booley.toml" in capsys.readouterr().err
    assert run("config-change") is not None
    assert calls == ["probe", "probe"]
    assert health.due_reason(host) is None


# ===========================================================================
# `booley --version` build identity
# ===========================================================================


def _version_line(monkeypatch, capsys):
    monkeypatch.setenv("COLUMNS", "20")
    with pytest.raises(SystemExit) as exc:
        _parse_argv(monkeypatch, "--version")
    assert exc.value.code == 0
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 1, "the version line is never wrapped to the terminal width"
    return lines[0]


def _attribute(monkeypatch, origin, **fields):
    attribution = VersionAttribution(version="4.5.6", origin=origin, **fields)
    monkeypatch.setattr(booley, "version_attribution", attribution)


def _stamp(monkeypatch, **values):
    """Install the generated build-stamp module with *values*.

    Kept private: build.sh generates `booley._build_commit` only inside a wheel build.
    """
    stamp = types.ModuleType("booley._build_commit")
    stamp.__dict__.update(values)
    monkeypatch.setattr(booley, "_build_commit", stamp, raising=False)
    monkeypatch.setitem(sys.modules, "booley._build_commit", stamp)


def test_absent_stamp_module_yields_none(monkeypatch, capsys):
    # build.sh and build_stamp name this test: a build must not leave the stamp in the
    # checkout, where a wheel-attributed import would read it.
    assert not hasattr(booley, "_build_commit")
    _attribute(monkeypatch, VersionOrigin.DISTRIBUTION, distribution_name="booley-rtl")
    assert _version_line(monkeypatch, capsys) == f"booley {__version__}"


class TestVersionCommitDate:
    """`booley --version` shows the commit and its UTC commit date when known (#2235)."""

    @pytest.mark.parametrize(
        ("metadata", "suffix"),
        [
            (("abc1234", "2026-10-26T10:00:00Z"), " (abc1234, 2026-10-26)"),
            # 23:30 at UTC-7 is already the next day in UTC.
            (("abc1234", "2026-10-26T23:30:00-07:00"), " (abc1234, 2026-10-27)"),
            (("abc1234", ""), " (abc1234)"),
            (("", "2026-10-26T10:00:00Z"), ""),
        ],
    )
    def test_source_version_shows_only_the_live_commit(
        self, metadata, suffix, monkeypatch, capsys
    ):
        """A checkout never borrows a baked stamp, even when its own Git cannot answer (#177)."""
        _attribute(monkeypatch, VersionOrigin.SOURCE, source_root=Path("/unused"))
        monkeypatch.setattr(VersionAttribution, "source_git_metadata", lambda _self: metadata)
        _stamp(monkeypatch, COMMIT="stale00", COMMIT_TIME="2026-01-01T00:00:00Z")
        assert _version_line(monkeypatch, capsys) == f"booley {__version__}{suffix}"

    @pytest.mark.parametrize(
        ("stamp", "suffix"),
        [
            ({"COMMIT": "cafe123"}, " (cafe123)"),
            ({"COMMIT": "cafe123", "COMMIT_TIME": ""}, " (cafe123)"),
            (
                {"COMMIT": "abcd123+dirty", "COMMIT_TIME": "2026-10-26T12:00:00Z"},
                " (abcd123+dirty, 2026-10-26)",
            ),
            ({"COMMIT": ""}, ""),
        ],
    )
    def test_wheel_version_shows_the_baked_commit_without_git(
        self, stamp, suffix, monkeypatch, capsys
    ):
        _attribute(monkeypatch, VersionOrigin.DISTRIBUTION, distribution_name="booley-rtl")
        monkeypatch.setattr(
            VersionAttribution,
            "source_git_metadata",
            lambda _self: pytest.fail("wheel attribution must not inspect Git"),
        )
        _stamp(monkeypatch, **stamp)
        assert _version_line(monkeypatch, capsys) == f"booley {__version__}{suffix}"

    def test_fallback_version_prints_bare_version(self, monkeypatch, capsys):
        _attribute(monkeypatch, VersionOrigin.FALLBACK)
        _stamp(monkeypatch, COMMIT="stale00")
        assert _version_line(monkeypatch, capsys) == f"booley {__version__}"

    def test_dirty_checkout_shows_dirty_commit_and_date(self, tmp_path, monkeypatch, capsys):
        repo = tmp_path / "checkout"
        repo.mkdir()

        def git(*args, **env):
            return run_bounded_child(
                ["git", "-C", str(repo), *args],
                check=True,
                # Ignore the developer's global git config (signing, hooks).
                env={
                    **os.environ,
                    "GIT_CONFIG_GLOBAL": os.devnull,
                    "GIT_CONFIG_NOSYSTEM": "1",
                    **env,
                },
            ).stdout.strip()

        git("init")
        git("config", "user.email", "t@example.com")
        git("config", "user.name", "T")
        (repo / "file.txt").write_text("one\n", encoding="utf-8")
        git("add", ".")
        # Distinct author and committer days prove the committer date is shown.
        git(
            "commit",
            "-m",
            "init",
            GIT_COMMITTER_DATE="2026-10-26T12:00:00+00:00",
            GIT_AUTHOR_DATE="2026-09-01T12:00:00+00:00",
        )
        short = git("rev-parse", "--short", "HEAD")
        _attribute(monkeypatch, VersionOrigin.SOURCE, source_root=repo)
        assert _version_line(monkeypatch, capsys) == f"booley {__version__} ({short}, 2026-10-26)"
        (repo / "file.txt").write_text("two\n", encoding="utf-8")

        assert (
            _version_line(monkeypatch, capsys)
            == f"booley {__version__} ({short}+dirty, 2026-10-26)"
        )


# ===========================================================================
# _enforce_runtime_location() / _effective_command() -- container-only CLI
# ===========================================================================


def _host_venue(monkeypatch):
    """Simulate the host: no env marker, no /.dockerenv-style probe hits."""
    monkeypatch.delenv("BOOLEY_CONTAINER", raising=False)
    return patch.object(Path, "exists", lambda self: False)


class TestEnforceVenue:
    @pytest.mark.parametrize("command", ["chat", "dashboard", "worktree", "flow", "specialist"])
    def test_container_only_command_refused_on_host(self, command, monkeypatch, capsys):
        """Workflow commands on the host -> exit 2 with the actionable fix."""
        monkeypatch.setattr(sys, "argv", ["booley", command])
        with _host_venue(monkeypatch), pytest.raises(SystemExit) as exc:
            tlr._enforce_runtime_location(command)
        assert exc.value.code == 2
        err = capsys.readouterr().err
        assert f"booley {command}" in err
        assert "Sandbox" in err
        assert "Reopen in Container" in err  # names the fix, not just the rule

    @pytest.mark.parametrize("command", ["chat", "dashboard", "worktree", "flow", "specialist"])
    def test_container_only_command_allowed_in_container(self, command, monkeypatch):
        monkeypatch.setenv("BOOLEY_CONTAINER", "1")
        tlr._enforce_runtime_location(command)  # must not raise

    def test_init_in_container_follows_sandbox_kind(self, tmp_path, monkeypatch, capsys):
        """`init` is never venue-refused; a Booley-issued Sandbox refuses it itself."""
        import argparse

        from booley.harness import init_cmd
        from booley.runtime import runtime_context

        cases = (
            ("/work/.devcontainer", "cannot run inside a Booley-issued Sandbox"),
            ("/mnt", "`booley init --seed` is host-side setup"),
        )
        monkeypatch.setenv("BOOLEY_CONTAINER", "1")
        for mount_point, message in cases:
            mountinfo = tmp_path / "mountinfo"
            mountinfo.write_text(f"1 0 0:1 / {mount_point} rw - ext4 /dev/x rw\n")
            monkeypatch.setattr(runtime_context, "_MOUNTINFO", mountinfo, raising=False)
            tlr._enforce_runtime_location("init")  # must not raise
            assert init_cmd.run_init(argparse.Namespace(seed=True), tmp_path) == 2
            assert message in capsys.readouterr().err

    @pytest.mark.parametrize(
        ("mounts", "refused", "init_message"),
        [
            ("/", False, "host init reached"),
            ("/work/.devcontainer", True, "cannot run inside a Booley-issued Sandbox"),
        ],
        ids=["container-host", "issued-sandbox"],
    )
    def test_container_host_override_reaches_host_commands(
        self, tmp_path, monkeypatch, capsys, mounts, refused, init_message
    ):
        """BOOLEY_CONTAINER=0 in a container runs host commands, except in an issued Sandbox."""
        from booley.harness import init_cmd
        from booley.runtime import runtime_context

        def reach_host_init(*_args):
            raise OSError("host init reached")

        mountinfo = tmp_path / "mountinfo"
        mountinfo.write_bytes(f"1 0 0:1 / {mounts} rw - ext4 /dev/x rw\n".encode())
        monkeypatch.setattr(runtime_context, "_MOUNTINFO", mountinfo)
        monkeypatch.setenv("BOOLEY_CONTAINER", "0")
        monkeypatch.setattr(Path, "exists", lambda self: self.as_posix() == "/.dockerenv")
        # private: the first host-only step of run_init; reaching it proves the host dispatch.
        monkeypatch.setattr(init_cmd, "_check_init_layout", reach_host_init)
        for command in ("bootstrap", "session"):
            if refused:
                with pytest.raises(SystemExit) as exc:
                    tlr._enforce_runtime_location(command)
                assert exc.value.code == 2
            else:
                tlr._enforce_runtime_location(command)  # must not raise
        capsys.readouterr()
        assert init_cmd.run_init(Namespace(seed=True), tmp_path) == 2
        assert init_message in capsys.readouterr().err

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

    def test_top_level_help_drops_per_command_location_tags(self):
        """The group header carries the location; command rows carry none."""
        help_text = tlr._build_parser().format_help()

        assert not re.search(r"\[(Sandbox|either|host|mixed)\]", help_text)
        assert "Locations:" not in help_text
        assert "Open this Project's default (or chosen) Agent CLI" in " ".join(help_text.split())


class TestEffectiveCommand:
    def _parse(self, argv):
        return tlr._build_parser().parse_args(argv)

    def test_subcommand_passthrough(self):
        assert tlr._effective_command(self._parse(["worktree", "list"])) == "worktree"
        assert tlr._effective_command(self._parse(["init"])) == "init"

    def test_unnormalized_no_command_has_no_effective_command(self):
        assert tlr._effective_command(self._parse([])) is None

    def test_normalized_sandbox_default_is_chat(self, monkeypatch):
        monkeypatch.setattr(tlr.runtime_context, "inside_session_runtime", lambda: True)
        parser = tlr._build_parser()
        args = tlr._normalize_args(parser, parser.parse_args([]))
        assert tlr._effective_command(args) == "chat"


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

        monkeypatch.setattr(cfgmod, "load_agent_settings", _boom)
        sandbox = type("S", (), {"image": "img", "memory": ""})()
        monkeypatch.setattr(
            cfgmod,
            "get_agent_settings",
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
        Sandbox the same Flows normally run in.
        """
        import booley.config.settings as cfgmod
        from booley.harness import sandbox as sandbox_mod

        monkeypatch.delenv("BOOLEY_CONTAINER", raising=False)
        monkeypatch.setattr(cfgmod, "load_agent_settings", lambda _root: None)
        sandbox_cfg = type("S", (), {"image": "img", "memory": "8g", "swap": "0"})()
        backend_cfg = type("C", (), {"sandbox": sandbox_cfg})()
        monkeypatch.setattr(cfgmod, "get_agent_settings", lambda: backend_cfg)
        fake_docker_host(monkeypatch, memory_gib=16, swap_gib=2)
        captured: dict[str, str] = {}

        class _FakeRunner:
            def __init__(self, cfg, _root, label=""):
                captured["memory"] = cfg.memory_limit
                captured["memory_swap"] = cfg.memory_swap

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
        assert captured["memory_swap"] == "8g"

    def test_shell_stops_when_the_docker_hosts_swap_is_unreadable(self, monkeypatch, capsys):
        import booley.config.settings as cfgmod
        from booley.harness import sandbox as sandbox_mod

        monkeypatch.delenv("BOOLEY_CONTAINER", raising=False)
        monkeypatch.setattr(cfgmod, "load_agent_settings", lambda _root: None)
        sandbox_cfg = type("S", (), {"image": "img", "memory": "", "swap": ""})()
        backend_cfg = type("C", (), {"sandbox": sandbox_cfg})()
        monkeypatch.setattr(cfgmod, "get_agent_settings", lambda: backend_cfg)
        monkeypatch.setattr(sandbox_mod.DockerSandboxConfig, "verify", lambda self: None)

        def _unreadable(_image):
            raise pi.DockerImageError("probe failed")

        monkeypatch.setattr(pi, "probe_docker_host_memory", _unreadable, raising=False)
        args = tlr._build_parser().parse_args(["shell", "--", "true"])
        assert tlr._cmd_shell(args, Path("/work")) == 2
        assert "probe failed" in capsys.readouterr().err

    def test_registered_as_early_command(self):
        assert tlr._EARLY_COMMANDS.get("shell") is tlr._cmd_shell


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


def _unknown_flow_error(name: str) -> str:
    """The stderr text for an unknown Flow: plain heading and table, never styled."""
    from booley.presentation.terminal_table import render_table

    table = "\n".join(render_table(["Name", "Description"], [["fakeflow", "A fake Flow"]]))
    return f"ERROR: {name!r} is not a flow for booley flow.\n\nAvailable Flows\n{table}\n"


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

    @pytest.mark.parametrize("kind", ["flow", "specialist"])
    def test_bare_listing_is_a_header_table(self, kind, monkeypatch, capsys):
        from booley.mcp.registry import McpToolInfo
        from booley.presentation import colors
        from booley.presentation.terminal_table import render_table

        monkeypatch.setattr(colors, "COLORS_ENABLED", False)
        infos = [
            McpToolInfo(name=name, path=f"{kind}s/{name}.py", description=text, kind=kind)
            for name, text in (("zeta", "Last one. Hidden."), ("alpha", "First\n   one"))
        ]
        args = Namespace(command=kind, endpoint_name=None, endpoint_args=[])

        assert tlr._select_endpoint(args, infos, kind) == 0

        heading = "Available Flows" if kind == "flow" else "Available Specialists"
        table = render_table(
            ["Name", "Description"], [["alpha", "First one"], ["zeta", "Last one"]]
        )
        assert capsys.readouterr().out.splitlines() == [heading, *table]

    def test_unknown_flow_name_lists_available_flows(self, capsys):
        rc = tlr._cmd_flow(self._args(["flow", "nosuchtool"]), Path("/work"))
        assert rc == 2
        streams = capsys.readouterr()
        assert streams.out == ""
        assert streams.err == (_unknown_flow_error("nosuchtool"))
        assert not _FakeFlow.calls

    def test_missing_flow_name_lists_available_flows(self, capsys):
        rc = tlr._cmd_flow(self._args(["flow"]), Path("/work"))
        assert rc == 0
        streams = capsys.readouterr()
        assert "Available Flows" in streams.out
        assert "fakeflow" in streams.out
        assert streams.err == ""

    def test_bare_flow_main_lists_on_stdout(self, tmp_path, monkeypatch, capsys):
        args = self._args(["flow"])
        monkeypatch.setattr(tlr.runtime_context, "inside_session_runtime", lambda: True)
        monkeypatch.setattr(tlr, "_parse_cli", lambda: args)
        monkeypatch.setattr(tlr, "find_project_root", lambda: tmp_path)
        monkeypatch.setattr(tlr, "_reject_source_project_command", lambda *_args: None)
        monkeypatch.setattr(tlr, "_require_command_project", lambda *_args: None)
        monkeypatch.setattr(tlr.runtime_context, "ensure_proxy_env", lambda: False)

        assert tlr.main() == 0
        streams = capsys.readouterr()
        assert "Available Flows" in streams.out
        assert "fakeflow" in streams.out
        assert streams.err == ""

    def test_empty_flow_name_is_unknown(self, capsys):
        rc = tlr._cmd_flow(self._args(["flow", ""]), Path("/work"))
        assert rc == 2
        streams = capsys.readouterr()
        assert streams.out == ""
        assert streams.err == (_unknown_flow_error(""))

    def test_listing_trims_the_llm_facing_description(self, capsys):
        tlr._cmd_flow(self._args(["flow"]), Path("/work"))
        out = capsys.readouterr().out
        assert "A fake Flow" in out
        assert "must not be shown" not in out

    def test_piped_listing_keeps_a_long_description_whole(self, monkeypatch, capsys):
        """Piped output is never cut or ellipsized (maintainer rule from #2433)."""
        from booley.mcp.registry import McpToolInfo

        long_lead = "Lead sentence " + "that keeps going " * 12 + "to the very end"
        info = McpToolInfo(
            name="fakeflow", path="flows/fakeflow.py", description=long_lead, kind="flow"
        )
        monkeypatch.setattr(tlr, "_discover_project_mcp_tools", lambda _root: [info])

        tlr._cmd_flow(self._args(["flow"]), Path("/work"))
        out = capsys.readouterr().out
        assert long_lead in out
        assert "…" not in out

    def test_unloadable_endpoint_errors_without_running(self, monkeypatch, capsys):
        monkeypatch.setattr(tlr, "_load_mcp_tool_class", lambda _info: None)
        rc = tlr._cmd_flow(self._args(["flow", "fakeflow"]), Path("/work"))
        assert rc == 2
        assert "could not load endpoint 'fakeflow'" in capsys.readouterr().err

    def test_advertised_in_help(self):
        help_text = tlr._build_parser().format_help()
        assert "flow" in help_text
        assert "Run a Booley Flow directly" in help_text
        # Collapse whitespace so argparse wrapping cannot split the example.
        assert "--target lint_soc" in " ".join(help_text.split())


class TestEndpointResolution:
    """No mocks: the real registry -> real Flow class path."""

    def test_builtin_lint_resolves_to_its_class(self, tmp_path):
        infos = tlr._discover_project_mcp_tools(tmp_path)
        lint_info = next(i for i in infos if i.name == "lint")
        from booley.flows.lint.flow import LintFlow

        assert tlr._load_mcp_tool_class(lint_info) is LintFlow

    def test_project_custom_mcp_tool_is_discovered_and_loaded(self, tmp_path):
        mcp_tools_dir = tmp_path / ".booley" / "mcp_tools"
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
# find_project_root()
# ===========================================================================


class TestFindProjectRoot:
    def test_finds_git_dir(self, tmp_path: Path):
        (tmp_path / ".git").mkdir()
        (tmp_path / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
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
        (rtl_dir / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
        (tmp_path / ".git").mkdir()
        (tmp_path / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
        subdir = rtl_dir / "scripts"
        subdir.mkdir()
        with patch("booley.harness.booley.Path.cwd", return_value=subdir):
            result = tlr.find_project_root()
        assert result == tmp_path


# ===========================================================================
# --dry-run implications (F-12)
# ===========================================================================


@pytest.mark.parametrize("command", ["up", "prepare"])
def test_at_cap_session_start_reports_status_two_without_success(
    command, tmp_path, monkeypatch, capsys
):
    from booley.runtime import session_runtime as sr

    args = tlr._build_parser().parse_args(["session", command])
    monkeypatch.setattr(tlr, "_report_upgrade_before_session", lambda _root: None)
    monkeypatch.setattr(health, "due_reason", lambda _root: None)
    monkeypatch.setattr(
        sr,
        "ensure" if command == "up" else command,
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            sr.SessionError("Sandbox start refused: host is at sandbox.max_sessions=1")
        ),
    )

    assert tlr._cmd_session(args, tmp_path) == 2
    captured = capsys.readouterr()
    assert "sandbox.max_sessions=1" in captured.err
    assert "Sandbox ready" not in captured.out


def test_session_lifecycle_timeout_reaches_main_boundary(tmp_path, monkeypatch):
    from booley.runtime.lifecycle_lock import LifecycleLockError

    args = tlr._build_parser().parse_args(["session", "up"])

    def raise_timeout(*_args, **_kwargs):
        raise LifecycleLockError("host Docker lifecycle timed out")

    monkeypatch.setattr(tlr, "_session_up", raise_timeout)

    with pytest.raises(LifecycleLockError, match="lifecycle timed out"):
        tlr._cmd_session(args, tmp_path)


def test_session_plain_runtime_error_remains_one_line(tmp_path, monkeypatch, capsys):
    args = tlr._build_parser().parse_args(["session", "up"])

    def raise_runtime_error(*_args, **_kwargs):
        raise RuntimeError("image refresh failed")

    monkeypatch.setattr(tlr, "_session_up", raise_runtime_error)

    assert tlr._cmd_session(args, tmp_path) == 2
    assert capsys.readouterr().err == "ERROR: image refresh failed\n"


@pytest.mark.parametrize("command", ["up", "enter", "status", "validate", "refresh"])
def test_project_root_is_not_exposed_on_unrelated_session_commands(command):
    parser = tlr._build_parser()

    with pytest.raises(SystemExit):
        parser.parse_args(["session", command, "--project-root", "/tmp/project"])


def test_session_refresh_capacity_refusal_precedes_spec_and_sandbox_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from booley.harness import init_cmd
    from booley.harness import session_refresh as harness_refresh
    from booley.runtime import session_runtime
    from booley.runtime.docker_capacity import DockerCapacityError

    spec = tmp_path / ".devcontainer/devcontainer.json"
    spec.parent.mkdir()
    spec.write_text('{"image":"sha256:running"}\n')
    before = spec.read_bytes()
    (tmp_path / ".booley").mkdir()
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setattr(
        session_runtime, "_run", lambda *_args, **_kwargs: pytest.fail("must not invoke Docker")
    )
    monkeypatch.setattr(session_runtime, "vscode_session_container", lambda _root: None)
    monkeypatch.setattr(
        init_cmd, "inspect_refreshable_runtime_image", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(init_cmd, "_report_project_requirements", lambda *_a, **_k: None)
    reconciles = []

    def refuse(scope: object, *_args: object, **_kwargs: object) -> None:
        reconciles.append(scope.project_root)
        raise DockerCapacityError("Project overlay needs 10 GiB; only 1 GiB available")

    monkeypatch.setattr(harness_refresh.image_lifecycle, "reconcile_planned", refuse)
    args = tlr._build_parser().parse_args(["session", "refresh"])
    assert tlr._cmd_session(args, tmp_path) == 2
    captured = capsys.readouterr()
    assert captured.err == (
        "ERROR: image-capacity: Project overlay needs 10 GiB; only 1 GiB available\n"
    )
    assert "Refreshed Sandbox" not in captured.out
    assert reconciles == [tmp_path.resolve()]
    assert spec.read_bytes() == before


@pytest.mark.parametrize(
    "argv",
    [
        ["eda", "installation", "list"],
        ["eda", "grant", "add", "TARGET", "--kind", "vivado", "--installation", "registered"],
    ],
)
def test_source_project_binding_independent_dispatch(tmp_path, monkeypatch, argv):
    source = tmp_path / "source"
    source.mkdir()
    (source / "pyproject.toml").write_text("[tool.booley]\nsource_checkout = true\n")
    (source / ".git").mkdir()
    (source / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    project = tmp_path / "project"
    (project / ".booley").mkdir(parents=True)
    monkeypatch.chdir(source)
    monkeypatch.delenv("RTL_PROJECT_ROOT", raising=False)
    monkeypatch.setattr(
        sys, "argv", ["booley", *[str(project) if item == "TARGET" else item for item in argv]]
    )
    monkeypatch.setattr(tlr, "_enforce_runtime_location", lambda *_: None)
    monkeypatch.setattr(tlr, "_host_install_authority_error", lambda *_: None)
    from booley.eda.provisioning import authority
    from booley.harness.eda_grants import COORDINATOR

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    calls = []

    def add(target, kind, **kwargs):
        calls.append(target)
        return authority.ProjectGrant(str(target), kind, kwargs["installation"], None)

    monkeypatch.setattr(COORDINATOR, "add", add)
    assert tlr._dispatch_main() == 0
    if "add" in argv:
        assert calls == [project]
    else:
        assert authority.load_state().installations == {}


@pytest.mark.parametrize(
    "argv",
    [["cheat"], ["cheat", "--list"]],
)
def test_project_binding_independent_never_discovers(tmp_path, monkeypatch, argv):
    (tmp_path / "pyproject.toml").write_text("[tool.booley]\nsource_checkout = true\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["booley", *argv])
    monkeypatch.setattr(tlr, "_enforce_runtime_location", lambda *_: None)
    monkeypatch.setattr(tlr, "_host_install_authority_error", lambda *_: None)

    def discover():
        raise AssertionError("independent command discovered a Project")

    monkeypatch.setattr(tlr, "find_project_root", discover)
    assert tlr._dispatch_main() == 0


@pytest.mark.parametrize("argv", [["dashboard"], ["chat"], ["worktree", "list"]])
def test_source_project_binding_required_dispatch(tmp_path, monkeypatch, argv):
    (tmp_path / "pyproject.toml").write_text("[tool.booley]\nsource_checkout = true\n")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    monkeypatch.setattr(tlr.runtime_context, "inside_session_runtime", lambda: True)
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("RTL_PROJECT_ROOT", raising=False)
    monkeypatch.setattr(sys, "argv", ["booley", *argv])
    monkeypatch.setattr(tlr, "_enforce_runtime_location", lambda *_: None)
    monkeypatch.setattr(tlr, "_host_install_authority_error", lambda *_: None)
    assert tlr._dispatch_main() == 2


@pytest.mark.parametrize("source", [True, False])
@pytest.mark.parametrize(("view", "renders"), [("--all", 2), ("--flows", 2), ("--specialists", 2)])
def test_source_project_binding_cheat_selected_imports(
    tmp_path, monkeypatch, source, view, renders
):
    from booley.dev_support import flow_specialist_reference as reference

    project = tmp_path / "project"
    project.mkdir()
    (project / ".git").mkdir()
    (project / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    (project / ".booley").mkdir()
    if source:
        (project / "pyproject.toml").write_text("[tool.booley]\nsource_checkout = true\n")
    subdir = project / "nested"
    subdir.mkdir()
    monkeypatch.chdir(subdir)
    monkeypatch.setenv("BOOLEY_DIR", str(project / ".booley"))
    monkeypatch.delenv("RTL_PROJECT_ROOT", raising=False)
    monkeypatch.setattr(sys, "argv", ["booley", "cheat", view])
    monkeypatch.setattr(tlr, "_enforce_runtime_location", lambda *_: None)
    calls = []

    def render(**kwargs):
        calls.append(kwargs["project_mcp_tools_dir"])
        return ""

    monkeypatch.setattr(reference, "render_flow_reference", render)
    monkeypatch.setattr(reference, "render_specialists_reference", render)
    monkeypatch.setattr(reference, "splice_generated", lambda text, *_args, **_kwargs: text)
    assert tlr._dispatch_main() == 0
    assert calls == ([] if source else [project / ".booley" / "mcp_tools"] * renders)


@pytest.mark.parametrize("command", ["doctor", "session", "cleanup"])
@pytest.mark.parametrize("selected_source", [True, False])
def test_source_project_binding_selected_root(tmp_path, monkeypatch, command, selected_source):
    source = tmp_path / "source"
    source.mkdir()
    (source / "pyproject.toml").write_text("[tool.booley]\nsource_checkout = true\n")
    project = tmp_path / "project"
    project.mkdir()
    (project / ".booley").mkdir()
    selected, cwd = (source, project) if selected_source else (project, source)
    monkeypatch.chdir(cwd)
    suffix = {"doctor": [], "session": ["down"], "cleanup": ["prepare"]}[command]
    argv = ["booley", "--project", str(selected), command, *suffix]
    monkeypatch.setattr(sys, "argv", argv)
    monkeypatch.setattr(tlr, "_enforce_runtime_location", lambda *_: None)
    monkeypatch.setattr(tlr, "_host_install_authority_error", lambda *_: None)
    calls = []

    def handler(_args, root):
        calls.append(root)
        return 0

    monkeypatch.setitem(tlr._EARLY_COMMANDS, command, handler)
    assert tlr._dispatch_main() == (2 if selected_source else 0)
    assert calls == ([] if selected_source else [project])


def test_source_project_binding_optional_discovery_failure(monkeypatch):
    from booley.runtime.project_discovery import ProjectRootDiscoveryError

    calls = []

    def discover():
        calls.append(True)
        raise ProjectRootDiscoveryError("ambiguous stale Project")

    monkeypatch.setattr(tlr, "find_project_root", discover)
    assert tlr._optional_project_root(Namespace(list=False, all=True)) is None
    assert calls == [True]


def test_source_project_binding_feedback_discovery_fallback(tmp_path, monkeypatch):
    from booley.runtime.project_discovery import ProjectRootDiscoveryError

    (tmp_path / "pyproject.toml").write_text("[tool.booley]\nsource_checkout = true\n")
    monkeypatch.chdir(tmp_path)

    def discover():
        raise ProjectRootDiscoveryError("stale Project")

    monkeypatch.setattr(tlr, "find_project_root", discover)
    assert tlr._command_project_root("feedback") == tmp_path


@pytest.mark.parametrize("unreadable_project", [False, True])
def test_project_binding_cheat_uninitialized_directory(
    tmp_path, monkeypatch, capsys, unreadable_project
):
    from booley.dev_support import flow_specialist_reference as reference

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("BOOLEY_DIR", raising=False)
    monkeypatch.delenv("RTL_PROJECT_ROOT", raising=False)
    monkeypatch.setattr(sys, "argv", ["booley", "cheat", "--all"])
    committed = tmp_path / "reference.md"
    committed.write_text("Committed optional reference\n")
    monkeypatch.setattr(tlr, "cheatsheet_path", lambda: committed)
    calls = []
    monkeypatch.setattr(reference, "render_flow_reference", lambda **kwargs: calls.append(kwargs))
    if unreadable_project:

        def discover():
            raise PermissionError("unreadable Project metadata")

        monkeypatch.setattr(tlr, "find_project_root", discover)
    reset_cache()
    assert tlr._dispatch_main() == 0
    assert "Committed optional reference" in capsys.readouterr().out
    assert calls == []


def test_source_project_binding_cheat_stale_custom_sentinel(tmp_path, monkeypatch, capsys):
    source = tmp_path / "source"
    custom = source / ".booley" / "mcp_tools"
    custom.mkdir(parents=True)
    (source / "pyproject.toml").write_text("[tool.booley]\nsource_checkout = true\n")
    (source / ".git").mkdir()
    (source / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    sentinel = tmp_path / "source-endpoint-imported"
    (custom / "source_sentinel.py").write_text(
        "from pathlib import Path\n"
        f"Path({str(sentinel)!r}).write_text('imported')\n"
        "class SourceSentinelFlow(Flow):\n"
        "    name = 'source_sentinel'\n"
        "    description = 'Source custom endpoint sentinel'\n"
    )
    monkeypatch.chdir(source)
    monkeypatch.setenv("BOOLEY_DIR", str(source / ".booley"))
    monkeypatch.delenv("RTL_PROJECT_ROOT", raising=False)
    monkeypatch.setattr(sys, "argv", ["booley", "cheat", "--all"])
    assert tlr._dispatch_main() == 0
    assert "source_sentinel" not in capsys.readouterr().out
    assert not sentinel.exists()


@pytest.mark.parametrize("command", ["init", "eda", "auth", "session", "projects"])
def test_host_commands_use_persistent_venv_identity(tmp_path, monkeypatch, command):
    from dataclasses import replace

    from booley.runtime import host_install, paths
    from booley.runtime.host_install import HostInstallationIdentity

    source = Path("/home/user/venv/lib/site-packages/booley/data/skills")
    identity = HostInstallationIdentity(
        1,
        "/home/user/venv/bin/booley",
        "/usr/bin/python3",
        str(source.parents[2]),
        "1.2.3",
        "abc123",
        "f" * 64,
    )
    # `init` is dual: its installation check applies on the host only.
    monkeypatch.setattr(tlr.runtime_context, "inside_session_runtime", lambda: False)
    monkeypatch.setattr(host_install.sys, "prefix", "/home/user/venv")
    monkeypatch.setattr(host_install.sys, "base_prefix", "/usr")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setattr(paths, "skills_dir", lambda: source)
    monkeypatch.setattr(host_install, "current_host_installation", lambda _source: identity)
    host_install.register_host_installation(source)
    assert tlr._host_install_authority_error(command) is None
    monkeypatch.setattr(
        host_install,
        "current_host_installation",
        lambda _source: replace(identity, revision="different"),
    )
    assert "--update" in tlr._host_install_authority_error(command)


@pytest.mark.parametrize("option", ["--project-root", "-p"])
def test_removed_alias_docs_still_have_project_replacement_notice(tmp_path, capsys, option):
    parser = tlr._build_parser()
    args = tlr._normalize_args(parser, parser.parse_args(["doctor", option, str(tmp_path)]))
    assert tlr._resolve_cli_selection(args) == tmp_path
    notice = capsys.readouterr().err
    assert option in notice
    assert "--project" in notice


def test_auth_status_optional_context_preserves_source_checkout(tmp_path, monkeypatch):
    (tmp_path / "pyproject.toml").write_text("[tool.booley]\nsource_checkout=true\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["booley", "auth", "--status"])
    monkeypatch.setattr(tlr, "find_project_root", lambda: tmp_path)
    monkeypatch.setattr(tlr, "_host_install_authority_error", lambda *_: None)
    roots = []
    monkeypatch.setitem(tlr._EARLY_COMMANDS, "auth", lambda args, root: roots.append(root) or 0)
    assert tlr._dispatch_main() == 0
    assert roots == [None]


@pytest.mark.parametrize("flag", ["--cli", "--app"])
@pytest.mark.parametrize(
    "in_project,app,expected_apps,expected_rc",
    [
        (False, None, ("claude", "codex"), 0),
        (True, None, ("codex",), 0),
        (True, "claude", ("claude",), 1),
        (False, "claude", ("claude",), 1),
        (False, "codex", ("codex",), 0),
    ],
)
def test_auth_status_selection_by_flag(
    tmp_path, monkeypatch, capsys, flag, in_project, app, expected_apps, expected_rc
):
    _assert_auth_status(
        tmp_path, monkeypatch, capsys, flag, in_project, app, expected_apps, expected_rc
    )


def _assert_auth_status(
    tmp_path, monkeypatch, capsys, flag, in_project, app, expected_apps, expected_rc
):

    from booley.runtime import auth_token

    home = tmp_path / "home"
    home.mkdir()
    ancestor = tmp_path / "ancestor"
    (ancestor / ".booley").mkdir(parents=True)
    project = ancestor / "project"
    project.mkdir()
    (project / ".git").mkdir()
    (project / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    monkeypatch.chdir(project)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    for key in (*auth_token.CREDENTIAL_ENV_KEYS, "BOOLEY_DIR", "RTL_PROJECT_ROOT", "CODEX_HOME"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "SYNTHETIC_CODEX_KEY")
    if not in_project:
        # With no Project the policy is ``subscription``: Codex runs on its login, not the key.
        (home / ".codex").mkdir()
        (home / ".codex" / "auth.json").write_bytes(b'{"tokens": {"access_token": "SYNTHETIC"}}')
    argv = ["booley", "auth", "--status"]
    if in_project:
        data = project / ".booley"
        data.mkdir()
        (data / "booley.toml").write_text('[agent]\nprovider="codex"\nauth="api_key"\n')
        argv.extend(["--project", str(project)])
    if app:
        argv.extend([flag, app])
    monkeypatch.setattr(sys, "argv", argv)
    monkeypatch.setattr(tlr, "_enforce_runtime_location", lambda *_: None)
    monkeypatch.setattr(tlr, "_host_install_authority_error", lambda *_: None)
    reset_cache()
    assert tlr.main() == expected_rc
    output = capsys.readouterr().out
    for client in ("claude", "codex"):
        assert (f"{client}:" in output) == (client in expected_apps)
    assert "SYNTHETIC_CODEX_KEY" not in output


@pytest.mark.parametrize("app,expected", [(None, "claude"), ("codex", "codex")])
def test_auth_clear_cli_keeps_historical_app_default(tmp_path, monkeypatch, capsys, app, expected):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("BOOLEY_DIR", raising=False)
    monkeypatch.delenv("RTL_PROJECT_ROOT", raising=False)
    data = tmp_path / ".booley"
    data.mkdir()
    (data / "booley.toml").write_text('[agent]\nprovider="codex"\nauth="api_key"\n')
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    argv = ["booley", "auth", "--clear", "--project", str(tmp_path)]
    if app:
        argv.extend(["--app", app])
    monkeypatch.setattr(sys, "argv", argv)
    monkeypatch.setattr(tlr, "_enforce_runtime_location", lambda *_: None)
    monkeypatch.setattr(tlr, "_host_install_authority_error", lambda *_: None)
    assert tlr.main() == 0
    assert f"no stored {expected} credential" in capsys.readouterr().out


@pytest.mark.parametrize(
    "outcome,expected",
    [
        ("kept", "kept: already current"),
        ("replaced", "replaced old → new"),
        ("created", "created new"),
    ],
)
def test_refresh_cli_reports_verified_container_outcome(
    tmp_path, monkeypatch, capsys, outcome, expected
):
    from booley.harness import session_refresh
    from booley.runtime.session_refresh import RefreshImage, RefreshResult

    result = RefreshResult(
        RefreshImage("sha256:image", None, None, False),
        outcome,
        None if outcome == "created" else "old",
        "new",
    )
    monkeypatch.setattr(session_refresh, "refresh", lambda *_a, **_kw: result)
    monkeypatch.setattr(tlr, "_report_session_health", lambda *_a, **_kw: None)
    monkeypatch.setattr(health, "due_reason", lambda _root: None)
    assert (
        tlr._session_refresh(tlr._build_parser().parse_args(["session", "refresh"]), tmp_path) == 0
    )
    assert capsys.readouterr().out.strip() == expected


@pytest.mark.parametrize(
    "configuration",
    ['[agent]\nauth="invalid"\n', '[agent]\nprovider="unknown"\n', "[agent\n"],
)
def test_auth_status_cli_refuses_corrupt_owned_project(
    tmp_path, monkeypatch, capsys, configuration
):
    data = tmp_path / ".booley"
    data.mkdir()
    (data / "booley.toml").write_text(configuration)
    monkeypatch.delenv("BOOLEY_DIR", raising=False)
    monkeypatch.setattr(sys, "argv", ["booley", "auth", "--status", "--project", str(tmp_path)])
    monkeypatch.setattr(tlr, "_enforce_runtime_location", lambda *_: None)
    monkeypatch.setattr(tlr, "_host_install_authority_error", lambda *_: None)
    reset_cache()
    assert tlr.main() == 1
    captured = capsys.readouterr()
    assert "agents run on" not in captured.out
    assert "✗" in captured.out


def _auth_cli_environment(tmp_path, monkeypatch, argv):
    from io import StringIO

    monkeypatch.chdir(tmp_path)
    for name in ("BOOLEY_DIR", "RTL_PROJECT_ROOT", "BOOLEY_IN_CONTAINER"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "private-config"))
    monkeypatch.setattr(sys, "argv", ["booley", "auth", *argv])
    monkeypatch.setattr(sys, "stdin", StringIO("sk-ant-oat01-synthetic-cli\n"))
    monkeypatch.setattr(tlr, "_enforce_runtime_location", lambda *_: None)
    monkeypatch.setattr(tlr, "_host_install_authority_error", lambda *_: None)
    reset_cache()


def test_auth_token_stdin_outside_project_stores_and_explains_pickup(
    tmp_path, monkeypatch, capsys
):
    from booley.harness import init_cmd
    from booley.runtime import auth_token

    _auth_cli_environment(tmp_path, monkeypatch, ["--token-stdin"])
    init = Mock(side_effect=AssertionError("outside auth must not initialize"))
    monkeypatch.setattr(init_cmd, "run_init", init)
    try:
        result = tlr._dispatch_main()
    except (SystemExit, RuntimeError, FileNotFoundError) as exc:
        result = exc
    assert result == 0, "auth incorrectly requires a Project"
    assert auth_token.token_path().read_text().strip() == "sk-ant-oat01-synthetic-cli"
    output = capsys.readouterr().out
    assert " ".join(output.split()).count(UNKNOWN_SANDBOX_STEPS) == 1
    init.assert_not_called()


def test_auth_nested_checkout_does_not_inherit_ancestor_project(tmp_path, monkeypatch):
    from booley.harness import auth_cmd
    from booley.runtime import session_runtime

    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    data = tmp_path / ".booley"
    data.mkdir()
    (data / "booley.toml").write_text('[agent]\nprovider="claude"\nauth="subscription"\n')
    child = tmp_path / "nested"
    (child / ".git").mkdir(parents=True)
    (child / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    _auth_cli_environment(child, monkeypatch, [])
    runtime = Mock(return_value=0)
    host = Mock(return_value=SimpleNamespace(returncode=0))
    reseed = Mock(return_value=True)
    monkeypatch.setattr(session_runtime, "run_project_command", runtime)
    monkeypatch.setattr(auth_cmd.shutil, "which", lambda _: "/synthetic/claude")
    original_run = auth_cmd.subprocess.run

    def run(command, *args, **kwargs):
        if tuple(command) == ("claude", "setup-token"):
            return host(command, *args, **kwargs)
        return original_run(command, *args, **kwargs)

    monkeypatch.setattr(auth_cmd.subprocess, "run", run)
    monkeypatch.setattr(auth_cmd, "_prompt_secret", lambda: "sk-ant-oat01-synthetic-nested")
    monkeypatch.setattr(auth_cmd, "_reseed_spec", reseed)
    result = tlr._dispatch_main()
    assert not runtime.called, "uninitialized nested checkout inherited ancestor Sandbox"
    assert result == 0
    host.assert_called_once()
    assert not reseed.called or reseed.call_args.args == (None,)


@pytest.mark.parametrize("external", [False, True])
def test_auth_ordinary_subdirectory_uses_owning_project(tmp_path, monkeypatch, external):
    project = tmp_path / "project"
    (project / ".git").mkdir(parents=True)
    (project / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    data = tmp_path / "external" if external else project / ".booley"
    data.mkdir()
    if external:
        (project / "booley.toml").write_text('[project]\ndir="../external"\n')
    child = project / "src"
    child.mkdir()
    _auth_cli_environment(child, monkeypatch, ["--token-stdin"])
    if external:
        monkeypatch.setenv("BOOLEY_DIR", str(data))
    roots = []
    monkeypatch.setitem(tlr._EARLY_COMMANDS, "auth", lambda args, root: roots.append(root) or 0)
    assert tlr._dispatch_main() == 0
    assert roots == [project]


def test_auth_nested_checkout_ignores_ancestor_config_override(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    data = tmp_path / "external"
    data.mkdir()
    (tmp_path / "booley.toml").write_text('[project]\ndir="external"\n')
    child = tmp_path / "nested"
    (child / ".git").mkdir(parents=True)
    (child / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    _auth_cli_environment(child, monkeypatch, ["--token-stdin"])
    monkeypatch.setenv("BOOLEY_DIR", str(data))
    roots = []
    monkeypatch.setitem(tlr._EARLY_COMMANDS, "auth", lambda args, root: roots.append(root) or 0)
    assert tlr._dispatch_main() == 0
    assert roots == [None]
