"""Tests for booley.py."""

from __future__ import annotations

import os
import shutil
import sys
from argparse import Namespace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar
from unittest.mock import MagicMock, Mock, patch

import pytest

from booley.harness import booley as tlr
from booley.runtime import health
from booley.runtime.booley_dir import reset_cache

TICKETS_REL = Path(".booley") / "tickets"


# ===========================================================================
# ts()
# ===========================================================================


def test_doctor_parser_accepts_deep_flag():
    parser = tlr._build_parser()

    args = tlr._normalize_args(parser, parser.parse_args(["doctor", "--deep"]))

    assert args.command == "doctor"
    assert args.deep is True


def test_doctor_parser_accepts_concise_flag():
    parser = tlr._build_parser()

    args = tlr._normalize_args(parser, parser.parse_args(["doctor", "--concise"]))

    assert args.concise is True


def test_doctor_parser_rejects_concise_with_verbose(capsys):
    parser = tlr._build_parser()

    with pytest.raises(SystemExit) as exc:
        parser.parse_args(["doctor", "--concise", "--verbose"])

    assert exc.value.code == 2
    error = capsys.readouterr().err
    assert "--concise" in error
    assert "--verbose" in error


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


def test_project_command_rejects_source_but_feedback_is_allowed(tmp_path, capsys):
    (tmp_path / "pyproject.toml").write_text(
        "[tool.booley]\nsource_checkout = true\n",
        encoding="utf-8",
    )

    assert tlr._reject_source_project_command("doctor", tmp_path) == 2
    assert "cannot be initialized or used as a Project" in capsys.readouterr().err
    assert tlr._reject_source_project_command("feedback", tmp_path) is None


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
    help_text = parser.format_help()
    assert "[Sandbox] Open this Project's configured agent" in help_text


def test_bare_booley_defaults_to_chat(monkeypatch):
    monkeypatch.setattr(tlr.runtime_context, "inside_session_runtime", lambda: True)
    parser = tlr._build_parser()

    args = tlr._normalize_args(parser, parser.parse_args([]))

    assert args.command == "chat"
    assert "Run bare `booley`" in parser.format_help()


@pytest.mark.parametrize("update_flag", ["--update", "--upgrade"])
def test_bootstrap_parser_exposes_only_host_reconciliation_flags(update_flag):
    args = tlr._build_parser().parse_args(["bootstrap", update_flag, "--verbose"])
    assert args.command == "bootstrap"
    assert args.update is True
    assert args.verbose is True
    assert not hasattr(args, "project_root")


@pytest.mark.parametrize(
    "flags", [("--check-only", "--force"), ("--with-qa-skills", "--without-qa-skills")]
)
def test_bootstrap_parser_rejects_exclusive_flags(flags, capsys):
    with pytest.raises(SystemExit) as raised:
        tlr._build_parser().parse_args(["bootstrap", *flags])
    assert raised.value.code == 2
    assert "not allowed with argument" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("flag", "attribute"),
    [
        ("--with-qa-skills", "with_qa_skills"),
        ("--without-qa-skills", "without_qa_skills"),
    ],
)
def test_bootstrap_parser_exposes_qa_selection_flags(flag, attribute):
    args = tlr._build_parser().parse_args(["bootstrap", flag, "--force"])

    assert getattr(args, attribute) is True
    assert args.force is True


def test_bootstrap_dispatch_precedes_project_discovery(monkeypatch):
    args = tlr._build_parser().parse_args(["bootstrap"])
    monkeypatch.setattr(tlr, "_parse_cli", lambda: args)
    monkeypatch.setattr(tlr, "_enforce_runtime_location", lambda _command: None)
    monkeypatch.setattr(tlr, "run_bootstrap", lambda _args: 17)
    monkeypatch.setattr(
        tlr,
        "find_project_root",
        lambda: (_ for _ in ()).throw(AssertionError("Project discovery must not run")),
    )
    assert tlr.main() == 17


@pytest.mark.parametrize(
    ("argv", "command"),
    [
        (["bootstrap"], "bootstrap"),
        (["init"], "init"),
    ],
)
def test_main_renders_lifecycle_timeout_once_without_traceback(
    argv, command, tmp_path, monkeypatch, capsys
):
    from booley.runtime.lifecycle_lock import LifecycleLockError

    args = tlr._build_parser().parse_args(argv)
    failure = LifecycleLockError(
        "host Docker lifecycle is busy (pid=41 operation=project init); "
        "timed out after waiting 120s"
    )

    def raise_timeout(*_args, **_kwargs):
        raise failure

    monkeypatch.setattr(tlr, "_parse_cli", lambda: args)
    monkeypatch.setattr(tlr, "_enforce_runtime_location", lambda _command: None)
    monkeypatch.setattr(tlr, "_host_install_authority_error", lambda _command: None)
    monkeypatch.setattr(tlr, "find_project_root", lambda: tmp_path)
    monkeypatch.setattr(tlr, "_reject_source_project_command", lambda *_args: None)
    monkeypatch.setattr(tlr, "_require_command_project", lambda *_args: None)
    monkeypatch.setattr(tlr.runtime_context, "ensure_proxy_env", lambda: False)
    if command == "bootstrap":
        monkeypatch.setattr(tlr, "run_bootstrap", raise_timeout)
    else:
        monkeypatch.setitem(tlr._EARLY_COMMANDS, command, raise_timeout)

    assert tlr.main() == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == f"ERROR: {failure}\n"
    assert "Traceback" not in captured.err


def _configure_indirect_lifecycle_timeout(command, monkeypatch, failure) -> None:
    if command == "doctor":
        from booley.harness import doctor
        from booley.runtime import session_runtime

        def exercise_runtime(*_args, **_kwargs):
            runtime = doctor._DoctorFlowRuntime(Path("/project"), "docker")
            runtime.command(["true"])

        monkeypatch.setattr(doctor, "run_doctor_result", exercise_runtime)
        monkeypatch.setattr(session_runtime, "up", MagicMock(side_effect=failure))
    elif command == "auth":
        from booley.harness import auth_cmd

        monkeypatch.setattr(auth_cmd, "_preflight", lambda *_args: True)
        monkeypatch.setattr(auth_cmd, "_acquire", MagicMock(side_effect=failure))
    else:
        # Importing the coordinator first keeps modules that bind the lock by
        # name (session_refresh) from capturing the patched callable.
        from booley.harness import eda_grants

        monkeypatch.setattr(
            eda_grants.lifecycle_lock,
            "host_lifecycle_lock",
            MagicMock(side_effect=failure),
        )


@pytest.mark.parametrize(
    ("argv", "command"),
    [
        (["doctor"], "doctor"),
        (["auth", "--app", "claude"], "auth"),
        (["eda", "grant", "revoke", "/project", "--kind", "vivado"], "eda"),
    ],
)
def test_indirect_lifecycle_timeout_reaches_main(argv, command, tmp_path, monkeypatch, capsys):
    from booley.runtime.lifecycle_lock import LifecycleLockError

    failure = LifecycleLockError("host Docker lifecycle timed out after waiting 120s")
    args = tlr._build_parser().parse_args(argv)
    monkeypatch.setattr(tlr, "_parse_cli", lambda: args)
    monkeypatch.setattr(tlr, "_enforce_runtime_location", lambda _command: None)
    monkeypatch.setattr(tlr, "_host_install_authority_error", lambda _command: None)
    monkeypatch.setattr(tlr, "find_project_root", lambda: tmp_path)
    monkeypatch.setattr(tlr, "_reject_source_project_command", lambda *_args: None)
    monkeypatch.setattr(tlr, "_require_command_project", lambda *_args: None)
    monkeypatch.setattr(tlr.runtime_context, "ensure_proxy_env", lambda: False)
    _configure_indirect_lifecycle_timeout(command, monkeypatch, failure)

    assert tlr.main() == 2
    captured = capsys.readouterr()
    assert captured.err.splitlines().count(f"ERROR: {failure}") == 1
    assert "Traceback" not in captured.out + captured.err


def test_projects_dispatch_precedes_active_project_discovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from booley.projects import cli as project_inventory_cli

    args = tlr._build_parser().parse_args(["projects", "--json"])
    monkeypatch.setattr(tlr, "_parse_cli", lambda: args)
    monkeypatch.setattr(tlr, "_enforce_runtime_location", lambda _command: None)
    monkeypatch.setattr(tlr, "_host_install_authority_error", lambda _command: None)
    from booley.runtime import image_keepers as runtime_image_keepers

    def run(_args: Namespace, *, keeper_operations: project_inventory_cli.KeeperOperations) -> int:
        assert keeper_operations is runtime_image_keepers
        return 19

    monkeypatch.setattr(project_inventory_cli, "run", run)
    monkeypatch.setattr(
        tlr,
        "find_project_root",
        lambda: (_ for _ in ()).throw(AssertionError("Project discovery must not run")),
    )

    assert tlr.main() == 19


def test_noncanonical_host_command_stops_before_dispatch(monkeypatch, capsys):
    args = tlr._build_parser().parse_args(["projects", "--json"])
    monkeypatch.setattr(tlr, "_parse_cli", lambda: args)
    monkeypatch.setattr(tlr, "_enforce_runtime_location", lambda _command: None)
    monkeypatch.setattr(
        tlr,
        "_host_install_authority_error",
        lambda _command: "not the canonical host installation",
    )

    assert tlr.main() == 2
    assert "not the canonical" in capsys.readouterr().err


def test_host_authority_guard_resolves_packaged_skills(monkeypatch, capsys):
    from booley.runtime import host_install, paths

    args = tlr._build_parser().parse_args(["projects", "--json"])
    monkeypatch.setattr(tlr, "_parse_cli", lambda: args)
    monkeypatch.setattr(tlr, "_enforce_runtime_location", lambda _command: None)
    monkeypatch.setattr(paths, "skills_dir", lambda: Path("/installed/skills"))
    monkeypatch.setattr(
        host_install,
        "host_install_error",
        lambda _source: "not the canonical host installation",
    )

    assert tlr.main() == 2
    assert "not the canonical" in capsys.readouterr().err


def test_session_health_reports_scheduled_automatic_check(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(health, "consume_changed_snapshot", lambda *_a, **_kw: None)
    monkeypatch.setattr(health, "due_reason", lambda _root: "stale")

    tlr._report_session_health(tmp_path)

    assert "Automatic Doctor is running" in capsys.readouterr().err


def test_session_up_observes_upgrade_before_runtime_start(tmp_path, monkeypatch):
    from booley.runtime import session_runtime

    events: list[str] = []
    monkeypatch.setattr(
        tlr, "_report_upgrade_before_session", lambda _root: events.append("observe")
    )
    monkeypatch.setattr(
        session_runtime,
        "ensure",
        lambda *_args, **_kwargs: (
            events.append("up") or session_runtime.EnsureResult("session", False, True)
        ),
    )
    monkeypatch.setattr(health, "due_reason", lambda _root: None)
    monkeypatch.setattr(tlr, "_report_session_health", lambda *_args, **_kwargs: None)

    assert tlr._session_up(tlr._build_parser().parse_args(["session", "up"]), tmp_path) == 0
    assert events == ["observe", "up"]


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


def test_session_up_does_not_repeat_doctor_for_unchanged_mount_view(
    mounted_health_project, capsys
):
    host, sandbox, directory, calls, starts, run = mounted_health_project
    args = tlr._build_parser().parse_args(["session", "up"])
    assert tlr._session_up(args, host) == 0
    capsys.readouterr()
    upgrade_before = (directory / "runtime" / "upgrade_review.json").read_bytes()
    paths = (health.report_path(directory), health.transcript_path(directory))
    before = [(path.read_bytes(), path.stat().st_mtime_ns) for path in paths]
    assert tlr._session_up(args, host) == 0
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
    assert tlr._session_up(args, host) == 0
    assert "booley.toml" in capsys.readouterr().err
    assert run("config-change") is not None
    assert calls == ["probe", "probe"]
    assert health.due_reason(host) is None


@pytest.mark.parametrize("mounted_health_project", [True], indirect=True)
def test_session_up_with_copied_mount_view(mounted_health_project, capsys):
    test_session_up_does_not_repeat_doctor_for_unchanged_mount_view(mounted_health_project, capsys)


def test_due_session_start_does_not_report_persisted_doctor_findings(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setattr(
        health,
        "consume_changed_snapshot",
        lambda *_args, **_kwargs: pytest.fail("stale summary was consumed"),
    )

    tlr._report_session_health(tmp_path, startup_due_reason="Doctor inputs changed")

    output = capsys.readouterr().err
    assert "Automatic Doctor is running" in output
    assert "before startup" in output


def test_session_health_rechecks_freshness_before_consuming_summary(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(health, "due_reason", lambda _root: "Doctor inputs changed")
    monkeypatch.setattr(
        health,
        "consume_changed_snapshot",
        lambda *_args, **_kwargs: pytest.fail("stale summary was consumed"),
    )

    tlr._report_session_health(tmp_path)

    output = capsys.readouterr().err
    assert "Automatic Doctor is running" in output
    assert "Doctor inputs changed" in output


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
    monkeypatch.setattr(tlr, "_code_identity", lambda: ("abc1234", ""))
    assert "(abc1234)" in tlr._version_string()


def test_version_string_bare_without_commit(monkeypatch):
    monkeypatch.setattr(tlr, "_code_identity", lambda: ("", ""))
    assert "(" not in tlr._version_string()


class TestBakedBuildCommit:
    """F-5: a wheel install has no .git, so `--version` reported no commit.

    Every freshly built image printed a bare `booley 0.1.0`, leaving the
    prescribed freshness check — confirm the wheel matches the commit — with
    nothing to check. build.sh now stamps the commit into the package.
    """

    def test_absent_stamp_module_yields_none(self, monkeypatch):
        monkeypatch.setattr(
            tlr, "_baked_identity", tlr._baked_identity
        )  # exercise the real implementation
        # No _build_commit module is generated in a checkout, so nothing is known.
        import booley

        assert not hasattr(booley, "_build_commit")
        assert tlr._baked_identity() == ("", "")

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
        monkeypatch.setattr(tlr, "_baked_identity", lambda: ("cafe123", ""))
        assert tlr._code_identity()[0] == ""
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
        monkeypatch.setattr(tlr, "_baked_identity", lambda: ("cafe123", ""))

        assert tlr._code_identity() == ("cafe123", "")

    def test_live_checkout_wins_over_the_baked_commit(self, monkeypatch):
        """A checkout's live state is the truthful one — it can be +dirty."""
        from booley.runtime.version_attribution import VersionAttribution

        monkeypatch.setattr(
            VersionAttribution,
            "source_git_metadata",
            lambda _self: ("live999", "2026-08-30T10:39:40Z"),
        )
        monkeypatch.setattr(tlr, "_baked_identity", lambda: ("stale00", ""))
        assert tlr._code_identity() == ("live999", "2026-08-30T10:39:40Z")

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
        monkeypatch.setattr(tlr, "_baked_identity", lambda: ("stale00", ""))

        assert tlr._code_identity()[0] == ""

    def test_baked_commit_reads_the_generated_module(self, monkeypatch):
        import types

        import booley

        stamp = types.ModuleType("booley._build_commit")
        stamp.COMMIT = "abcd123+dirty"
        monkeypatch.setattr(booley, "_build_commit", stamp, raising=False)
        monkeypatch.setitem(sys.modules, "booley._build_commit", stamp)
        assert tlr._baked_identity() == ("abcd123+dirty", "")

    def test_empty_stamp_is_treated_as_unknown(self, monkeypatch):
        """build.sh writes an empty COMMIT when git is unavailable."""
        import types

        import booley

        stamp = types.ModuleType("booley._build_commit")
        stamp.COMMIT = ""
        monkeypatch.setattr(booley, "_build_commit", stamp, raising=False)
        monkeypatch.setitem(sys.modules, "booley._build_commit", stamp)
        assert tlr._baked_identity() == ("", "")


class TestVersionCommitDate:
    """`booley --version` shows the UTC commit date next to the commit (#2235)."""

    @staticmethod
    def _attribute(monkeypatch, origin, **fields):
        import booley
        from booley.runtime.version_attribution import VersionAttribution

        monkeypatch.setattr(
            booley,
            "version_attribution",
            VersionAttribution(version="4.5.6", origin=origin, **fields),
        )

    @classmethod
    def _source(cls, monkeypatch, commit, committed_at):
        from booley.runtime.version_attribution import VersionAttribution, VersionOrigin

        cls._attribute(monkeypatch, VersionOrigin.SOURCE, source_root=Path("/unused"))
        monkeypatch.setattr(
            VersionAttribution,
            "source_git_metadata",
            lambda _self: (commit, committed_at),
        )

    @classmethod
    def _stamped_wheel(cls, monkeypatch, **stamp_values):
        import types

        import booley
        from booley.runtime.version_attribution import VersionAttribution, VersionOrigin

        cls._attribute(monkeypatch, VersionOrigin.DISTRIBUTION, distribution_name="booley-rtl")
        monkeypatch.setattr(
            VersionAttribution,
            "source_git_metadata",
            lambda _self: pytest.fail("wheel attribution must not inspect Git"),
        )
        stamp = types.ModuleType("booley._build_commit")
        for name, value in stamp_values.items():
            setattr(stamp, name, value)
        monkeypatch.setattr(booley, "_build_commit", stamp, raising=False)
        monkeypatch.setitem(sys.modules, "booley._build_commit", stamp)

    def test_source_checkout_shows_head_commit_date_in_utc(self, monkeypatch):
        from booley import __version__

        self._source(monkeypatch, "abc1234", "2026-10-26T10:00:00Z")
        assert tlr._version_string() == f"booley {__version__} (abc1234, 2026-10-26)"

    def test_dirty_checkout_shows_dirty_commit_and_date(self, tmp_path, monkeypatch):
        import subprocess

        from booley import __version__
        from booley.runtime.version_attribution import VersionOrigin

        repo = tmp_path / "checkout"
        repo.mkdir()

        def git(*args, **env):
            return subprocess.run(
                ["git", "-C", str(repo), *args],
                check=True,
                capture_output=True,
                text=True,
                env={**os.environ, **env},
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
        (repo / "file.txt").write_text("two\n", encoding="utf-8")
        self._attribute(monkeypatch, VersionOrigin.SOURCE, source_root=repo)

        assert tlr._version_string() == f"booley {__version__} ({short}+dirty, 2026-10-26)"

    def test_source_offset_date_is_converted_to_utc_day(self, monkeypatch):
        from booley import __version__

        # 23:30 at UTC-7 is already the next day in UTC.
        self._source(monkeypatch, "abc1234", "2026-10-26T23:30:00-07:00")
        assert tlr._version_string() == f"booley {__version__} (abc1234, 2026-10-27)"

    def test_missing_commit_prints_bare_version(self, monkeypatch):
        from booley import __version__

        self._source(monkeypatch, "", "2026-10-26T10:00:00Z")
        assert tlr._version_string() == f"booley {__version__}"

    def test_missing_date_prints_commit_only(self, monkeypatch):
        from booley import __version__

        self._source(monkeypatch, "abc1234", "")
        assert tlr._version_string() == f"booley {__version__} (abc1234)"

    def test_stamp_without_date_field_prints_commit_only(self, monkeypatch):
        from booley import __version__

        self._stamped_wheel(monkeypatch, COMMIT="cafe123")
        assert tlr._version_string() == f"booley {__version__} (cafe123)"

    def test_stamp_with_empty_date_prints_commit_only(self, monkeypatch):
        from booley import __version__

        self._stamped_wheel(monkeypatch, COMMIT="cafe123", COMMIT_TIME="")
        assert tlr._version_string() == f"booley {__version__} (cafe123)"

    def test_fallback_version_prints_bare_version(self, monkeypatch):
        from booley import __version__
        from booley.runtime.version_attribution import VersionOrigin

        self._attribute(monkeypatch, VersionOrigin.FALLBACK)
        assert tlr._version_string() == f"booley {__version__}"

    def test_version_flag_stays_one_line_on_narrow_terminal(self, monkeypatch, capsys):
        monkeypatch.setenv("COLUMNS", "20")
        self._source(monkeypatch, "d4c94dc35+dirty", "2026-10-26T10:00:00Z")
        with pytest.raises(SystemExit) as exc:
            tlr._build_parser().parse_args(["--version"])
        assert exc.value.code == 0
        lines = capsys.readouterr().out.splitlines()
        assert len(lines) == 1
        assert lines[0].endswith("(d4c94dc35+dirty, 2026-10-26)")


# ===========================================================================
# _enforce_runtime_location() / _effective_command() -- container-only CLI
# ===========================================================================


def _host_venue(monkeypatch):
    """Simulate the host: no env marker, no /.dockerenv-style probe hits."""
    monkeypatch.delenv("BOOLEY_CONTAINER", raising=False)
    return patch.object(Path, "exists", lambda self: False)


class TestEnforceVenue:
    @pytest.mark.parametrize(
        "command", ["goal", "chat", "dashboard", "worktree", "flow", "specialist"]
    )
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

    @pytest.mark.parametrize(
        "command", ["goal", "chat", "dashboard", "worktree", "flow", "specialist"]
    )
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

        `session` joins `init` on the host side: it drives the Sandbox
        from outside it, and the sandbox has no Docker to do that with (ADR 0016).
        `auth` is host-only too: it drives the host's browser OAuth flow and
        writes the host's ~/.config, neither of which exists in the sandbox.
        """
        assert {
            "goal",
            "dashboard",
            "chat",
            "worktree",
            "flow",
            "specialist",
        } == tlr._CONTAINER_ONLY_COMMANDS
        assert {
            "bootstrap",
            "init",
            "session",
            "auth",
            "eda",
            "projects",
        } == tlr._HOST_ONLY_COMMANDS

    def test_top_level_help_labels_every_advertised_command_location(self):
        help_text = tlr._build_parser().format_help()

        assert "dashboard" in help_text and "[Sandbox]" in help_text
        assert "projects" in help_text and "[host]" in help_text
        assert "doctor" in help_text and "[either]" in help_text
        assert "flow" in help_text and "[Sandbox]" in help_text
        assert "targets" in help_text and "[either]" in help_text
        assert "[mixed]" not in help_text
        assert "Locations:" in help_text

    def test_runtime_guards_are_derived_from_command_location_catalog(self):
        assert (
            frozenset(
                command
                for command, location in tlr.COMMAND_LOCATIONS.items()
                if location is tlr.CommandLocation.SESSION_RUNTIME
            )
            == tlr._CONTAINER_ONLY_COMMANDS
        )
        assert (
            frozenset(
                command
                for command, location in tlr.COMMAND_LOCATIONS.items()
                if location is tlr.CommandLocation.HOST
            )
            == tlr._HOST_ONLY_COMMANDS
        )


class TestEffectiveCommand:
    def _parse(self, argv):
        return tlr._build_parser().parse_args(argv)

    def test_subcommand_passthrough(self):
        assert tlr._effective_command(self._parse(["goal", "status"])) == "goal"
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
        sandbox_cfg = type("S", (), {"image": "img", "memory": "8g"})()
        backend_cfg = type("C", (), {"sandbox": sandbox_cfg})()
        monkeypatch.setattr(cfgmod, "get_agent_settings", lambda: backend_cfg)
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

    def test_unknown_flow_name_lists_available_flows(self, capsys):
        rc = tlr._cmd_flow(self._args(["flow", "nosuchtool"]), Path("/work"))
        assert rc == 2
        streams = capsys.readouterr()
        assert streams.out == ""
        assert streams.err == (
            "ERROR: 'nosuchtool' is not a flow for booley flow.\n\nAvailable Flows:\n  fakeflow  A fake Flow\n"
        )
        assert not _FakeFlow.calls

    def test_missing_flow_name_lists_available_flows(self, capsys):
        rc = tlr._cmd_flow(self._args(["flow"]), Path("/work"))
        assert rc == 0
        streams = capsys.readouterr()
        assert "Available Flows:" in streams.out
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
        assert "Available Flows:" in streams.out
        assert "fakeflow" in streams.out
        assert streams.err == ""

    def test_empty_flow_name_is_unknown(self, capsys):
        rc = tlr._cmd_flow(self._args(["flow", ""]), Path("/work"))
        assert rc == 2
        streams = capsys.readouterr()
        assert streams.out == ""
        assert streams.err == (
            "ERROR: '' is not a flow for booley flow.\n\nAvailable Flows:\n  fakeflow  A fake Flow\n"
        )

    def test_listing_trims_the_llm_facing_description(self, capsys):
        tlr._cmd_flow(self._args(["flow"]), Path("/work"))
        out = capsys.readouterr().out
        assert "A fake Flow" in out
        assert "must not be shown" not in out

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

    def test_flow_is_sandbox_only(self):
        """Top-level dispatch refuses host execution before endpoint discovery."""
        assert "flow" in tlr._CONTAINER_ONLY_COMMANDS
        assert "flow" not in tlr._HOST_ONLY_COMMANDS


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
# _time_ampm_tz_to_wait() (formerly _parse_reset_time)
# ===========================================================================

_has_tzdata = True
try:
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    ZoneInfo("UTC")
except ZoneInfoNotFoundError:
    _has_tzdata = False


# ===========================================================================
# Constants
# ===========================================================================


class TestConstants:
    def test_venv_python_removed(self):
        """VENV_PYTHON removed — package is pip-installed, no embedded venv."""
        assert not hasattr(tlr, "VENV_PYTHON")

    def test_stale_threshold_removed(self):
        """STALE_THRESHOLD removed -- stale detection no longer needed."""
        assert not hasattr(tlr, "STALE_THRESHOLD")


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


def test_session_refresh_capacity_refusal_precedes_park_and_spec_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from booley.harness import init_cmd
    from booley.runtime import session_refresh as refresh_runtime
    from booley.runtime import session_runtime
    from booley.runtime.docker_capacity import DockerCapacityError

    spec = tmp_path / ".devcontainer/devcontainer.json"
    spec.parent.mkdir()
    spec.write_text('{"image":"sha256:running"}\n')
    before = spec.read_bytes()
    (tmp_path / ".booley").mkdir()
    monkeypatch.setattr(refresh_runtime, "_load_recovery_issuance", lambda *_: object())
    monkeypatch.setattr(refresh_runtime, "shared_recovery_blocks_command", lambda **_kw: False)
    monkeypatch.setattr(
        session_runtime, "_run", lambda *_args, **_kwargs: pytest.fail("must not invoke Docker")
    )
    monkeypatch.setattr(session_runtime, "strict_conflicting_vscode_session", lambda _root: None)
    monkeypatch.setattr(
        init_cmd,
        "inspect_refreshable_runtime_image",
        lambda *_args, **_kwargs: None,
    )
    preparations = []

    def refuse(project: Path, **_kwargs: object) -> None:
        preparations.append(project)
        raise DockerCapacityError("Project overlay needs 10 GiB; only 1 GiB available")

    monkeypatch.setattr(init_cmd, "prepare_runtime_image", refuse)
    monkeypatch.setattr(
        session_runtime,
        "plan_session_refresh",
        lambda *_args, **_kwargs: pytest.fail("capacity failure must precede parking"),
    )
    args = tlr._build_parser().parse_args(["session", "refresh"])
    assert tlr._cmd_session(args, tmp_path) == 2
    captured = capsys.readouterr()
    assert captured.err == (
        "[XX] image-capacity: Project overlay needs 10 GiB; only 1 GiB available\n"
    )
    assert "Refreshed Sandbox" not in captured.out
    assert preparations == [tmp_path.resolve()]
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


def test_project_binding_registry_covers_parser():
    import argparse

    parser = tlr._build_parser()
    commands = next(
        action for action in parser._actions if isinstance(action, argparse._SubParsersAction)
    )
    assert set(commands.choices) == set(tlr.COMMAND_PROJECT_BINDINGS)


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
    monkeypatch.setitem(tlr._EARLY_COMMANDS, "auth", lambda *_: 0)
    assert tlr._dispatch_main() == 0


@pytest.mark.parametrize("argv", [["goal", "status"], ["dashboard"], ["chat"]])
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
@pytest.mark.parametrize("view", ["--all", "--criteria"])
def test_source_project_binding_cheat_selected_imports(tmp_path, monkeypatch, source, view):
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
    criteria_calls = []
    original_catalog = tlr._live_criterion_endpoint_catalog

    def catalog(root):
        criteria_calls.append(root)
        return original_catalog(root)

    monkeypatch.setattr(tlr, "_live_criterion_endpoint_catalog", catalog)

    def render(**kwargs):
        calls.append(kwargs["project_mcp_tools_dir"])
        return ""

    monkeypatch.setattr(reference, "render_flow_reference", render)
    monkeypatch.setattr(reference, "render_specialists_reference", render)
    monkeypatch.setattr(reference, "splice_generated", lambda text, *_args, **_kwargs: text)
    assert tlr._dispatch_main() == 0
    assert calls == ([] if source else [project / ".booley" / "mcp_tools"] * 2)
    assert criteria_calls == ([] if source else [project])


@pytest.mark.parametrize("command", ["doctor", "goal", "session", "cleanup"])
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
    suffix = {"doctor": [], "goal": ["status"], "session": ["down"], "cleanup": ["prepare"]}[
        command
    ]
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
    monkeypatch.setattr(tlr, "_live_criterion_endpoint_catalog", calls.append)
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
def test_auth_status_cli_preserves_omitted_app_selection(
    tmp_path, monkeypatch, capsys, in_project, app, expected_apps, expected_rc
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
    argv = ["booley", "auth", "--status"]
    if in_project:
        data = project / ".booley"
        data.mkdir()
        (data / "booley.toml").write_text('[agent]\nprovider="codex"\nauth="api_key"\n')
        argv.extend(["--project", str(project)])
    if app:
        argv.extend(["--app", app])
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
        RefreshImage("booley-sandbox", "sha256:image"),
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
    assert "[XX]" in captured.out


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
    assert "session up" in output and "init --seed" in output
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
