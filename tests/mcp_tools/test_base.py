"""Tests for McpTool base class — argparse, set_criterion, git diff classification, exit codes."""

from __future__ import annotations

import argparse
import errno
import json
import os
from pathlib import Path, PurePosixPath
from unittest import mock

import pytest

from booley.flows import endpoint_admission
from booley.flows.endpoint_admission import _Claim
from booley.flows.endpoint_diff import read_source_dirs_from_toml
from booley.flows.endpoint_reporting import _StdoutWitness
from booley.flows.endpoint_session import _transcript_diagnostic_path
from booley.flows.source_fingerprint import as_str_list
from booley.mcp.base import EXIT_ERROR, EXIT_FAILURE, EXIT_SUCCESS, McpTool, McpToolResult
from booley.runtime import job_slots
from booley.runtime.endpoint_execution import EndpointOutcome
from tests.flows.report_headline import assert_paired_headline


class ConcreteMcpTool(McpTool):
    """Minimal concrete endpoint for testing."""

    name = "test_endpoint"
    description = "A test endpoint"

    def __init__(self, *, code_modifying: bool = False):
        self.code_modifying = code_modifying
        super().__init__()

    def _add_args(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument("--extra", default="")

    def _run(self) -> McpToolResult:
        return McpToolResult(exit_code=EXIT_SUCCESS)


def test_documented_mcp_result_name_remains_source_compatible() -> None:
    assert McpToolResult is EndpointOutcome


def test_mcp_adapter_preserves_structured_pre_state_rejection(capsys) -> None:
    class RejectingMcpTool(ConcreteMcpTool):
        def _pre_state_gate(self) -> McpToolResult:
            return McpToolResult(
                exit_code=EXIT_ERROR,
                detail={"acceptance_effect": "rejected"},
                report_text="blocked before state",
            )

    result = RejectingMcpTool().execute_cli([])

    assert result.exit_code == EXIT_ERROR
    assert result.outcome.detail == {"acceptance_effect": "rejected"}
    assert result.outcome.report_text == "blocked before state"
    assert capsys.readouterr().err == "blocked before state\n"


def test_mcp_adapter_passes_neutral_outcome_to_extension_hooks() -> None:
    class NeutralOutcomeMcpTool(ConcreteMcpTool):
        finalized_label: str | None = None

        def _run(self) -> EndpointOutcome:
            return EndpointOutcome(report_text="ok", display_label="target demo · 2 tests")

        def _finalize_result(self, result: McpToolResult) -> None:
            self.finalized_label = result.display_label

    endpoint = NeutralOutcomeMcpTool()

    assert endpoint.main([]) == EXIT_SUCCESS
    assert endpoint.finalized_label == "target demo · 2 tests"


def test_report_failure_is_returned_on_the_final_outcome(tmp_path: Path) -> None:
    class RecoveringMcpTool(ConcreteMcpTool):
        writes = 0

        def _run(self) -> EndpointOutcome:
            return EndpointOutcome(detail={"nested": {"fact": "kept"}}, report_text="PASS")

        def write_report(self, result: EndpointOutcome) -> Path | None:
            self.writes += 1
            if self.writes == 1:
                reserved = self.reserve_invocation_dir()
                assert reserved is not None
                path = reserved / "report.json"
                raise OSError(errno.EIO, "injected publication failure", path)
            return super().write_report(result)

    endpoint = RecoveringMcpTool()
    result = endpoint.execute_cli(["--report-dir", str(tmp_path / "reports")])

    assert result.exit_code == EXIT_ERROR
    assert result.outcome.detail["nested"] == {"fact": "kept"}
    completion_error = result.outcome.detail["completion_error"]
    assert {key: completion_error[key] for key in ("operation", "type", "path")} == {
        "operation": "publish report.json",
        "type": "OSError",
        "path": str(tmp_path / "reports/test_endpoint/1/report.json"),
    }
    assert "injected publication failure" in completion_error["message"]
    report = json.loads(
        (tmp_path / "reports/test_endpoint/1/report.json").read_text(encoding="utf-8")
    )
    assert report["detail"] == result.outcome.detail
    assert not (tmp_path / "reports/test_endpoint/2").exists()


def test_persistent_report_failure_preserves_primary_diagnosis(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    class FailingMcpTool(ConcreteMcpTool):
        writes = 0

        def _run(self) -> EndpointOutcome:
            return EndpointOutcome(detail={"fact": "kept"}, report_text="PASS")

        def write_report(self, result: EndpointOutcome) -> Path | None:
            self.writes += 1
            path = self.reserve_invocation_dir()
            assert path is not None
            raise OSError(errno.EIO, f"publication failure {self.writes}", path / "report.json")

    endpoint = FailingMcpTool()
    result = endpoint.execute_cli(["--report-dir", str(tmp_path / "reports")])

    assert result.exit_code == EXIT_ERROR
    assert endpoint.writes == 2
    assert result.outcome.detail["fact"] == "kept"
    assert "publication failure 1" in result.outcome.detail["completion_error"]["message"]
    assert "publication failure 2" in result.outcome.detail["completion_errors"][0]["message"]
    assert "Traceback" not in capsys.readouterr().err


def test_finalize_failure_propagates_without_persisting_replacement_result() -> None:
    class FinalizeFailingMcpTool(ConcreteMcpTool):
        def _finalize_result(self, result: McpToolResult) -> None:
            raise RuntimeError("finalization failed")

    with pytest.raises(RuntimeError, match="finalization failed"):
        FinalizeFailingMcpTool().main([])


def test_self_printed_verdict_gets_only_the_completion_suffix(capsys) -> None:
    class SelfPrintingPersistenceFailure(ConcreteMcpTool):
        def _run(self) -> McpToolResult:
            print("design failed")
            return McpToolResult(exit_code=EXIT_FAILURE, report_text="design failed")

        def write_report(self, result: EndpointOutcome) -> Path | None:
            raise OSError(errno.EIO, "persistence failed")

    result = SelfPrintingPersistenceFailure().execute_cli([])

    captured = capsys.readouterr()
    assert result.exit_code == EXIT_ERROR
    assert captured.out == "design failed\n"
    assert "design failed" not in captured.err
    assert captured.err.count("Completion failure (publish report.json)") == 1
    assert "completion_errors" not in result.outcome.detail


def test_self_printed_verdict_gets_only_the_requeue_note(capsys) -> None:
    """The requeue note follows a verdict the Flow already printed: the verdict is not shown twice."""
    from types import SimpleNamespace

    from booley.flows.endpoint_reporting import _publish_console_verdict
    from booley.runtime.endpoint_execution import OOM_REQUEUE_NOTE, EndpointOutcome

    note = f"{OOM_REQUEUE_NOTE}: the Job did not start again."
    endpoint = SimpleNamespace(
        _stdout_witness=SimpleNamespace(saw=lambda text: text == "design failed"),
        endpoint_kind="flow",
        announce_success_report=False,
    )

    _publish_console_verdict(
        endpoint, EndpointOutcome(exit_code=EXIT_FAILURE, report_text=f"design failed\n{note}")
    )

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == f"{note}\n"


def test_self_printed_verdict_gets_only_the_oom_kill_note(capsys) -> None:
    """The OOM-kill line follows a verdict the Flow already printed: the verdict is not shown twice."""
    from types import SimpleNamespace

    from booley.flows.endpoint_reporting import _publish_console_verdict
    from booley.runtime.endpoint_execution import EndpointOutcome

    note = (
        "Killed for out-of-memory: this Job may not be requeued after an OOM kill, "
        "so it is not retried."
    )
    endpoint = SimpleNamespace(
        _stdout_witness=SimpleNamespace(saw=lambda text: text == "design failed"),
        endpoint_kind="flow",
        announce_success_report=False,
    )

    _publish_console_verdict(
        endpoint, EndpointOutcome(exit_code=EXIT_FAILURE, report_text=f"design failed\n{note}")
    )

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == f"{note}\n"


def test_self_printed_verdict_gets_only_the_host_oom_kill_note(capsys) -> None:
    """The host line leads the result; a verdict the Flow already printed is not shown twice."""
    from types import SimpleNamespace

    from booley.flows.endpoint_reporting import _publish_console_verdict
    from booley.runtime.endpoint_execution import HOST_OOM_KILL_NOTE, EndpointOutcome

    host = f"{HOST_OOM_KILL_NOTE}: the host ran out of memory, so the Job is not retried."
    note = "Killed for out-of-memory: work item a's last attempt was OOM-killed, so no retry."

    def publish(report_text: str, seen: str) -> None:
        endpoint = SimpleNamespace(
            _stdout_witness=SimpleNamespace(saw=lambda text: text == seen),
            endpoint_kind="flow",
            announce_success_report=False,
        )
        _publish_console_verdict(
            endpoint, EndpointOutcome(exit_code=EXIT_FAILURE, report_text=report_text)
        )

    publish(f"{host}\ndesign failed", "design failed")
    captured = capsys.readouterr()
    assert (captured.out, captured.err) == ("", f"{host}\n")

    publish(f"{host}\ndesign failed\n{note}", "design failed")
    captured = capsys.readouterr()
    assert (captured.out, captured.err) == ("", f"{host}\n{note}\n")

    # The Flow printed nothing: the whole result, host line first, goes to stderr.
    publish(f"{host}\ndesign failed", "")
    assert capsys.readouterr().err == f"{host}\ndesign failed\n"


def test_console_failure_is_normalized_and_recovery_is_attempted() -> None:
    endpoint = ConcreteMcpTool()
    with (
        mock.patch.object(
            endpoint, "_publish_console_report", side_effect=OSError("console unavailable")
        ),
        mock.patch("booley.flows.endpoint_reporting._recover_report_publication") as recover,
    ):
        result = endpoint.execute_cli([])

    assert result.exit_code == EXIT_ERROR
    assert result.outcome.detail["completion_error"]["operation"] == ("publish console diagnosis")
    recover.assert_called_once_with(endpoint, result.outcome)


def test_dry_run_skips_admission_and_persistent_bookkeeping(tmp_path: Path) -> None:
    state_file = tmp_path / "state.json"
    state_file.write_bytes(b'{"opaque": true}\n')
    before = state_file.read_bytes()

    class DryRunMcpTool(ConcreteMcpTool):
        JOB_CLASS = job_slots.CLASS_MEMORY
        non_persisting_dry_run = True

        def _add_args(self, parser: argparse.ArgumentParser) -> None:
            parser.add_argument("--dry-run", action="store_true")

    endpoint = DryRunMcpTool()
    env = _env_with_state(state_file)
    with (
        mock.patch.dict(os.environ, env),
        mock.patch.object(endpoint_admission, "_acquire_memory_reservation") as acquire_slot,
    ):
        result = endpoint.execute_cli(["--dry-run"])

    assert result.exit_code == EXIT_SUCCESS
    assert state_file.read_bytes() == before
    acquire_slot.assert_not_called()


def test_dry_run_without_preview_opt_in_keeps_normal_lifecycle() -> None:
    class ExistingDryRunMcpTool(ConcreteMcpTool):
        JOB_CLASS = job_slots.CLASS_MEMORY

        def _add_args(self, parser: argparse.ArgumentParser) -> None:
            parser.add_argument("--dry-run", action="store_true")

    endpoint = ExistingDryRunMcpTool()
    with mock.patch.object(
        endpoint_admission, "_acquire_memory_reservation", return_value=_Claim(None, None)
    ) as acquire:
        result = endpoint.execute_cli(["--dry-run"])

    assert result.exit_code == EXIT_SUCCESS
    acquire.assert_called_once()


def test_dry_run_lifecycle_is_snapshotted_before_endpoint_mutates_args() -> None:
    class MutatingDryRunMcpTool(ConcreteMcpTool):
        non_persisting_dry_run = True

        def _add_args(self, parser: argparse.ArgumentParser) -> None:
            parser.add_argument("--dry-run", action="store_true")

        def _run(self) -> McpToolResult:
            self.args.dry_run = False
            return McpToolResult(exit_code=EXIT_SUCCESS)

    endpoint = MutatingDryRunMcpTool()
    with mock.patch.object(endpoint_admission, "_acquire_memory_reservation") as acquire_slot:
        result = endpoint.execute_cli(["--dry-run"])

    assert result.exit_code == EXIT_SUCCESS
    acquire_slot.assert_not_called()


class SimLikeMcpTool(ConcreteMcpTool):
    """Dummy simulate endpoint used to assert base guard behavior."""

    name = "sim"

    def _run(self) -> McpToolResult:
        self.set_criterion("sim_pass_default", True)
        return McpToolResult(exit_code=EXIT_SUCCESS)


class LintLikeMcpTool(ConcreteMcpTool):
    """Dummy lint endpoint used to exercise sealed invocation binding."""

    name = "lint"
    ran = False

    def _run(self) -> McpToolResult:
        self.ran = True
        self.set_criterion(f"lint_clean_{self.args.target}", True)
        return McpToolResult(exit_code=EXIT_SUCCESS)


def _env_with_state(state_file: Path, slug: str = "test") -> dict[str, str]:
    """Build env dict with BOOLEY_* vars pointing to a state file."""
    env = os.environ.copy()
    env["BOOLEY_SLUG"] = slug
    env["BOOLEY_STATE_FILE"] = str(state_file)
    return env


class TestMcpToolArgparse:
    def test_common_args_from_env(self, tmp_path: Path):
        state_file = tmp_path / "state.json"
        env = _env_with_state(state_file, slug="my-ticket")
        endpoint = ConcreteMcpTool()
        with mock.patch.dict(os.environ, env):
            args = endpoint.parse_args(["--target", "lite"])
        assert args.slug == "my-ticket"
        assert not hasattr(args, "state_file")
        assert args.target == "lite"

    def test_target_help_names_core_targets_not_configs_toml(self, tmp_path: Path):
        """SETUP-F-44: configs.toml was retired by ADR 0022, and this help text
        doubles as the MCP `target` schema description — a stale filename here
        sends agents hunting for a file that does not exist."""
        state_file = tmp_path / "state.json"
        env = _env_with_state(state_file)
        endpoint = ConcreteMcpTool()
        with mock.patch.dict(os.environ, env):
            endpoint.parse_args(["--target", "lite"])
        help_text = endpoint._parser.format_help()
        assert "configs.toml" not in help_text
        assert ".core Target name" in help_text

    def test_legacy_config_flag_is_rejected(self):
        endpoint = ConcreteMcpTool()
        with pytest.raises(SystemExit):
            endpoint.parse_args(["--config", "lite"])

    def test_custom_args(self, tmp_path: Path):
        state_file = tmp_path / "state.json"
        env = _env_with_state(state_file)
        endpoint = ConcreteMcpTool()
        with mock.patch.dict(os.environ, env):
            args = endpoint.parse_args(["--extra", "custom_value"])
        assert args.extra == "custom_value"

    def test_no_env_vars_human_mode(self):
        """Endpoints parse successfully without any BOOLEY_* env vars."""
        endpoint = ConcreteMcpTool()
        env = {k: v for k, v in os.environ.items() if not k.startswith("BOOLEY_")}
        with mock.patch.dict(os.environ, env, clear=True):
            args = endpoint.parse_args([])
        assert args.slug == ""
        assert args.report_dir is None

    def test_report_dir_from_env(self, tmp_path: Path):
        logs_dir = tmp_path / "logs"
        env = os.environ.copy()
        env["BOOLEY_LOGS_DIR"] = str(logs_dir)
        env["BOOLEY_RUNTIME_DIR"] = str(logs_dir / ".runtime")
        endpoint = ConcreteMcpTool()
        with mock.patch.dict(os.environ, env):
            args = endpoint.parse_args([])
        assert args.report_dir == logs_dir / ".runtime" / "mcp-tool-reports"

    def test_report_dir_cli_overrides_env(self, tmp_path: Path):
        cli_dir = tmp_path / "cli_reports"
        env = os.environ.copy()
        env["BOOLEY_LOGS_DIR"] = str(tmp_path / "env_reports")
        endpoint = ConcreteMcpTool()
        with mock.patch.dict(os.environ, env):
            args = endpoint.parse_args(["--report-dir", str(cli_dir)])
        assert args.report_dir == cli_dir

    def test_reserved_invocation_dir_reused_by_report(self, tmp_path: Path):
        report_dir = tmp_path / "reports"
        endpoint = ConcreteMcpTool()
        endpoint.parse_args(["--report-dir", str(report_dir)])

        reserved = endpoint.reserve_invocation_dir()
        assert reserved == report_dir / "test_endpoint" / "1"
        (reserved / "request.json").write_text("{}", encoding="utf-8")

        report_path = endpoint.write_report(McpToolResult(exit_code=EXIT_SUCCESS))
        assert report_path == reserved / "report.json"
        assert (reserved / "request.json").exists()

        next_path = endpoint.write_report(McpToolResult(exit_code=EXIT_SUCCESS))
        assert next_path == report_dir / "test_endpoint" / "2" / "report.json"

    def test_invocation_dir_retries_when_concurrent_writer_claims_number(self, tmp_path: Path):
        report_dir = tmp_path / "reports"
        endpoint = ConcreteMcpTool()
        endpoint.parse_args(["--report-dir", str(report_dir)])
        endpoint_dir = report_dir / endpoint.name
        endpoint_dir.mkdir(parents=True)
        (endpoint_dir / "1").mkdir()
        contested = endpoint_dir / "2"
        original_mkdir = Path.mkdir
        raced = False

        def racing_mkdir(path: Path, *args, **kwargs):
            nonlocal raced
            if path == contested and not raced:
                raced = True
                original_mkdir(path)
                raise FileExistsError(path)
            return original_mkdir(path, *args, **kwargs)

        with mock.patch.object(Path, "mkdir", racing_mkdir):
            reserved = endpoint.reserve_invocation_dir()

        assert raced
        assert reserved == endpoint_dir / "3"


def _git(repo: Path, *args: str) -> str:
    import subprocess

    return subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def test_code_modifying_tool_resets_nothing_and_keeps_change_statistics(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "rtl").mkdir()
    (repo / "rtl/top.v").write_bytes(b"module top;\nendmodule\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    state_file = tmp_path / "state.json"
    state_file.write_bytes(b'{"opaque": true}\n')
    before = state_file.read_bytes()

    class CommittingTool(ConcreteMcpTool):
        def _run(self) -> McpToolResult:
            (repo / "rtl/top.v").write_bytes(b"module top;\nwire a;\nwire b;\nendmodule\n")
            _git(repo, "commit", "-q", "-a", "-m", "change")
            return McpToolResult(exit_code=EXIT_SUCCESS)

    env = _env_with_state(state_file)
    env["BOOLEY_LOGS_DIR"] = str(tmp_path / "logs")
    env["BOOLEY_RUNTIME_DIR"] = str(tmp_path / "runtime")
    endpoint = CommittingTool(code_modifying=True)
    with mock.patch.dict(os.environ, env):
        result = endpoint.execute_cli(["--work-dir", str(repo)])

    assert result.exit_code == EXIT_SUCCESS
    assert state_file.read_bytes() == before
    assert (result.outcome.lines_added, result.outcome.lines_removed) == (2, 0)


def test_custom_endpoint_with_hand_set_state_file_runs_and_reports_no_criteria(
    tmp_path: Path,
) -> None:
    state_file = tmp_path / "state.json"
    state_file.write_bytes(b'{"opaque": true}\n')
    before = state_file.read_bytes()

    class DeclaringTool(ConcreteMcpTool):
        def _run(self) -> McpToolResult:
            self.set_criterion("lint_clean_demo", True)
            return McpToolResult(exit_code=EXIT_SUCCESS, report_text="ran")

    logs_dir = tmp_path / "logs"
    env = _env_with_state(state_file)
    env["BOOLEY_LOGS_DIR"] = str(logs_dir)
    env["BOOLEY_RUNTIME_DIR"] = str(logs_dir / ".runtime")
    endpoint = DeclaringTool()
    with mock.patch.dict(os.environ, env):
        exit_code = endpoint.main(["--target", "demo"])

    assert exit_code == EXIT_SUCCESS
    assert state_file.read_bytes() == before


class TestHumanModeStderr:
    """Standalone (no state file) failures surface report_text on stderr."""

    class _FailingMcpTool(ConcreteMcpTool):
        name = "failing_endpoint"

        def _run(self) -> McpToolResult:
            return McpToolResult(exit_code=EXIT_FAILURE, report_text="boom: the actual reason")

    def test_report_text_to_stderr_on_human_failure(self, capsys):
        endpoint = self._FailingMcpTool()
        env = {k: v for k, v in os.environ.items() if not k.startswith("BOOLEY_")}
        with mock.patch.dict(os.environ, env, clear=True):
            rc = endpoint.main([])
        assert rc == EXIT_FAILURE
        assert "boom: the actual reason" in capsys.readouterr().err

    def test_state_backed_failure_reaches_stderr_and_report(self, tmp_path: Path, capsys):
        state_file = tmp_path / "state.json"
        endpoint = self._FailingMcpTool()
        report_dir = tmp_path / "reports"
        with mock.patch.dict(os.environ, _env_with_state(state_file)):
            endpoint.main(["--report-dir", str(report_dir)])

        assert capsys.readouterr().err.count("boom: the actual reason") == 1
        report = json.loads((report_dir / "failing_endpoint.json").read_text())
        assert report["report_text"] == "boom: the actual reason"

    class _SilentPassMcpTool(ConcreteMcpTool):
        # A passing endpoint whose only verdict lives in report_text (not
        # display_lines) — like fpga_impl (F-14).
        name = "silent_pass_endpoint"

        def _run(self) -> McpToolResult:
            return McpToolResult(exit_code=EXIT_SUCCESS, report_text="RESULT: PASS")

    class _AnnouncingPassMcpTool(_SilentPassMcpTool):
        name = "announcing_pass_endpoint"
        announce_success_report = True

    def _run_human(self, endpoint):
        env = {k: v for k, v in os.environ.items() if not k.startswith("BOOLEY_")}
        with mock.patch.dict(os.environ, env, clear=True):
            return endpoint.main([])

    def test_pass_is_silent_by_default(self, capsys):
        """A passing run stays quiet unless the endpoint opts in — most endpoints have
        the harness UI render their PASS and must not double-print on the CLI."""
        rc = self._run_human(self._SilentPassMcpTool())
        out = capsys.readouterr()
        assert rc == EXIT_SUCCESS
        assert "RESULT: PASS" not in out.out
        assert "RESULT: PASS" not in out.err

    def test_announce_success_report_surfaces_pass_on_stdout(self, capsys):
        """F-14: an opted-in endpoint prints its PASS verdict on stdout, so success
        is never indistinguishable from a silent no-op."""
        rc = self._run_human(self._AnnouncingPassMcpTool())
        out = capsys.readouterr()
        assert rc == EXIT_SUCCESS
        assert "RESULT: PASS" in out.out

    class _SelfPrintingFailMcpTool(ConcreteMcpTool):
        """Like elaborate/simulate/asic_synthesize: prints its own verdict."""

        name = "self_printing_fail_endpoint"

        def _run(self) -> McpToolResult:
            text = "[elab] lite FAIL\n\nRESULT: FAIL (0/1)"
            print(text)
            return McpToolResult(exit_code=EXIT_FAILURE, report_text=text)

    def test_self_printed_verdict_is_not_echoed_again(self, capsys, monkeypatch):
        """F-28: a endpoint that already put its verdict block on stdout must not
        get the same block repeated on stderr — every FAIL path rendered its
        whole verdict twice on a merged console."""
        rc = self._run_human(self._SelfPrintingFailMcpTool())
        out = capsys.readouterr()
        assert rc == EXIT_FAILURE
        assert out.out.count("RESULT: FAIL (0/1)") == 1
        assert "RESULT: FAIL (0/1)" not in out.err

    def test_split_stream_capture_does_not_duplicate_the_report(self, capsys, monkeypatch):
        """Separate capture followed by merge must still contain one verdict."""
        rc = self._run_human(self._SelfPrintingFailMcpTool())
        out = capsys.readouterr()
        assert rc == EXIT_FAILURE
        assert "RESULT: FAIL (0/1)" in out.out
        assert "RESULT: FAIL (0/1)" not in out.err

    def test_state_backed_self_printed_verdict_is_not_echoed_again(
        self, tmp_path: Path, capsys
    ) -> None:
        state_file = tmp_path / "state.json"
        endpoint = self._SelfPrintingFailMcpTool()

        with mock.patch.dict(os.environ, _env_with_state(state_file)):
            rc = endpoint.main(["--report-dir", str(tmp_path / "reports")])

        out = capsys.readouterr()
        assert rc == EXIT_FAILURE
        assert out.out.count("RESULT: FAIL (0/1)") == 1
        assert "RESULT: FAIL (0/1)" not in out.err

    def test_state_backed_announcing_endpoint_still_prints_success(
        self, tmp_path: Path, capsys
    ) -> None:
        state_file = tmp_path / "state.json"

        with mock.patch.dict(os.environ, _env_with_state(state_file)):
            rc = self._AnnouncingPassMcpTool().main(["--report-dir", str(tmp_path / "reports")])

        out = capsys.readouterr()
        assert rc == EXIT_SUCCESS
        assert out.out.count("RESULT: PASS") == 1

    class _ChattyBlockedMcpTool(ConcreteMcpTool):
        """A Specialist whose streamed log merely *mentions* its verdict word."""

        name = "chatty_blocked_endpoint"

        def _run(self) -> McpToolResult:
            print("agent: the vendor flow is BLOCKED on a missing license, escalating")
            return McpToolResult(exit_code=EXIT_FAILURE, report_text="BLOCKED")

    def test_a_mention_inside_a_log_line_does_not_suppress_the_reason(self, capsys, monkeypatch):
        """The witness must match whole printed lines, not any substring: a
        short report_text like a bare "BLOCKED" otherwise matches any
        streamed agent line containing the word, and the only copy of the
        failure reason disappears."""
        rc = self._run_human(self._ChattyBlockedMcpTool())
        out = capsys.readouterr()
        assert rc == EXIT_FAILURE
        assert out.err.strip().splitlines()[-1] == "BLOCKED"

    class _PartialPrintFailMcpTool(ConcreteMcpTool):
        """Prints progress chatter only — the verdict lives in report_text."""

        name = "partial_print_fail_endpoint"

        def _run(self) -> McpToolResult:
            print("[lint] scanning 20 files")
            return McpToolResult(exit_code=EXIT_FAILURE, report_text="boom: the actual reason")

    def test_unprinted_verdict_still_reaches_stderr(self, capsys):
        """The de-duplication keys off the verdict text, not off 'the endpoint wrote
        something': a endpoint whose reason never hit stdout still gets it echoed."""
        rc = self._run_human(self._PartialPrintFailMcpTool())
        out = capsys.readouterr()
        assert rc == EXIT_FAILURE
        assert "boom: the actual reason" in out.err

    def test_stdout_is_restored_after_the_run(self, capsys):
        """The witness wraps stdout only for the duration of _run()."""
        import sys as _sys

        before = _sys.stdout
        self._run_human(self._SelfPrintingFailMcpTool())
        assert _sys.stdout is before

    def test_reused_endpoint_does_not_reuse_an_old_stdout_witness(self, capsys) -> None:
        class ReusedEndpoint(ConcreteMcpTool):
            name = "reused_endpoint"

            def __init__(self) -> None:
                super().__init__()
                self.reject_during_preparation = False

            def _pre_state_gate(self) -> McpToolResult | None:
                if self.reject_during_preparation:
                    return McpToolResult(exit_code=EXIT_ERROR, report_text="same verdict")
                return None

            def _run(self) -> McpToolResult:
                print("same verdict")
                return McpToolResult(exit_code=EXIT_FAILURE, report_text="same verdict")

        endpoint = ReusedEndpoint()
        assert self._run_human(endpoint) == EXIT_FAILURE
        capsys.readouterr()
        endpoint.reject_during_preparation = True

        assert self._run_human(endpoint) == EXIT_ERROR

        captured = capsys.readouterr()
        assert captured.err == "same verdict\n"


class TestStdoutWitness:
    """The stdout tee behind the F-28 de-duplication."""

    class _Sink:
        """A text stream with a byte-level `buffer`, like a real sys.stdout."""

        def __init__(self) -> None:
            self.text = ""
            self.encoding = "utf-8"
            self.buffer = self._Buffer(self)

        def write(self, text: str) -> int:
            self.text += text
            return len(text)

        def writelines(self, lines) -> None:
            for line in lines:
                self.write(line)

        class _Buffer:
            def __init__(self, parent) -> None:
                self.parent = parent
                self.data = b""

            def write(self, data: bytes) -> int:
                self.data += data
                self.parent.text += data.decode("utf-8")
                return len(data)

    def test_write_is_teed_and_witnessed(self):
        sink = self._Sink()
        witness = _StdoutWitness(sink)
        witness.write("RESULT: FAIL\n")
        assert sink.text == "RESULT: FAIL\n"
        assert witness.saw("RESULT: FAIL")

    def test_styled_print_of_a_plain_block_counts(self):
        """A coloured console copy of a plain report still counts as shown."""
        sink = self._Sink()
        witness = _StdoutWitness(sink)
        witness.write("\x1b[1mTarget\x1b[0m   Status\n\x1b[32mRESULT: PASS\x1b[0m\n")
        assert witness.saw("RESULT: PASS")
        assert witness.saw("Target   Status\nRESULT: PASS")

    def test_writelines_is_witnessed(self):
        """File objects implement writelines natively, so a plain __getattr__
        delegation would let a whole verdict reach the terminal unseen."""
        sink = self._Sink()
        witness = _StdoutWitness(sink)
        witness.writelines(["RESULT: ", "FAIL\n"])
        assert sink.text == "RESULT: FAIL\n"
        assert witness.saw("RESULT: FAIL")

    def test_buffer_writes_are_witnessed(self):
        """bwave._safe_print's legacy-Windows fallback writes encoded bytes
        straight to sys.stdout.buffer."""
        sink = self._Sink()
        witness = _StdoutWitness(sink)
        witness.buffer.write(b"RESULT: FAIL\n")
        assert sink.buffer.data == b"RESULT: FAIL\n"
        assert witness.saw("RESULT: FAIL")

    def test_buffer_stays_absent_when_the_stream_has_none(self):
        """`hasattr(sys.stdout, "buffer")` must keep telling the truth."""

        class _NoBuffer:
            def write(self, text: str) -> int:
                return len(text)

        assert not hasattr(_StdoutWitness(_NoBuffer()), "buffer")

    def test_substring_inside_a_line_does_not_count(self):
        sink = self._Sink()
        witness = _StdoutWitness(sink)
        witness.write("agent: the run is BLOCKED on a license\n")
        assert not witness.saw("BLOCKED")

    def test_a_whole_printed_line_counts(self):
        sink = self._Sink()
        witness = _StdoutWitness(sink)
        witness.write("preamble\nBLOCKED\n")
        assert witness.saw("BLOCKED")

    def test_block_starting_with_an_indented_line_counts(self):
        sink = self._Sink()
        witness = _StdoutWitness(sink)
        witness.write("noise\n  Target   Area\n  top      1.0 kGE\n\nRESULT: PASS\n")
        assert witness.saw("  Target   Area\n  top      1.0 kGE\n\nRESULT: PASS")

    def test_indented_echo_of_a_one_line_verdict_does_not_count(self):
        """Subprocess output indents lines; that echo is not the printed verdict."""
        sink = self._Sink()
        witness = _StdoutWitness(sink)
        witness.write("subprocess output:\n    RESULT: FAIL\n")
        assert not witness.saw("RESULT: FAIL")

    def test_multi_line_block_counts(self):
        sink = self._Sink()
        witness = _StdoutWitness(sink)
        witness.write("noise\n[elab] FAIL\n\nRESULT: FAIL (0/1)\n")
        assert witness.saw("[elab] FAIL\n\nRESULT: FAIL (0/1)")

    def test_truncated_head_is_not_trusted_as_a_line_start(self, monkeypatch):
        """Once the ring buffer drops a chunk the retained text no longer
        starts at a line boundary, so position 0 stops proving one."""
        monkeypatch.setattr(_StdoutWitness, "_MAX_CHARS", 8)
        sink = self._Sink()
        witness = _StdoutWitness(sink)
        witness.write("x" * 16)  # kept: the ring never drops its only chunk
        witness.write("BLOCKED")  # evicts the first chunk -> truncated tail
        # stdout really carried "xxxx...BLOCKED" — the word sits mid-line, and
        # the retained tail must not pretend otherwise just because the eviction
        # left it at index 0.
        assert not witness.saw("BLOCKED")


class TestMcpToolWriteReport:
    def test_report_written(self, tmp_path: Path):
        state_file = tmp_path / "state.json"
        report_dir = tmp_path / "reports"

        env = _env_with_state(state_file)
        endpoint = ConcreteMcpTool()
        with mock.patch.dict(os.environ, env):
            endpoint.parse_args(["--report-dir", str(report_dir)])
        result = McpToolResult(exit_code=0, detail={"warnings": 0})
        path = endpoint.write_report(result)
        assert path is not None
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["mcp_tool"] == "test_endpoint"
        assert "criterion_key" not in data and "criterion_met" not in data

    def test_no_report_when_no_dir(self, tmp_path: Path):
        state_file = tmp_path / "state.json"

        env = _env_with_state(state_file)
        endpoint = ConcreteMcpTool()
        with mock.patch.dict(os.environ, env):
            endpoint.parse_args([])
        result = McpToolResult()
        assert endpoint.write_report(result) is None

    def test_report_works_without_slug(self, tmp_path: Path):
        """Report written even when slug is empty (human mode)."""
        report_dir = tmp_path / "reports"
        env = {k: v for k, v in os.environ.items() if not k.startswith("BOOLEY_")}
        endpoint = ConcreteMcpTool()
        with mock.patch.dict(os.environ, env, clear=True):
            endpoint.parse_args(["--report-dir", str(report_dir)])
        result = McpToolResult(exit_code=0)
        path = endpoint.write_report(result)
        assert path is not None
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["slug"] == ""

    def test_report_retry_reuses_reserved_invocation_after_atomic_failure(
        self, tmp_path: Path
    ) -> None:
        report_dir = tmp_path / "reports"
        endpoint = ConcreteMcpTool()
        endpoint.parse_args(["--report-dir", str(report_dir)])
        reserved = endpoint.reserve_invocation_dir()
        assert reserved is not None
        report_path = reserved / "report.json"
        original_replace = Path.replace
        failed = False

        def fail_numbered_once(source: Path, destination: Path) -> Path:
            nonlocal failed
            if destination == report_path and not failed:
                failed = True
                raise OSError("injected report publication failure")
            return original_replace(source, destination)

        with (
            mock.patch.object(Path, "replace", fail_numbered_once),
            pytest.raises(OSError, match="injected report publication failure"),
        ):
            endpoint.write_report(McpToolResult(exit_code=EXIT_SUCCESS))

        path = endpoint.write_report(McpToolResult(exit_code=EXIT_ERROR))

        assert path == report_path
        assert json.loads(path.read_text(encoding="utf-8"))["exit_code"] == EXIT_ERROR
        assert not (report_dir / endpoint.name / "2").exists()


class TestMcpToolMain:
    def test_exception_returns_actionable_durable_error(self, tmp_path: Path, capsys, caplog):
        state_file = tmp_path / "state.json"
        report_dir = tmp_path / "reports"

        class FailingMcpTool(ConcreteMcpTool):
            def _run(self):
                raise ValueError("boom")

        env = _env_with_state(state_file)
        endpoint = FailingMcpTool()
        caplog.set_level("DEBUG")
        with mock.patch.dict(os.environ, env):
            exit_code = endpoint.main(["--report-dir", str(report_dir)])
        assert exit_code == EXIT_ERROR
        output = capsys.readouterr()
        assert output.out == ""
        assert "Traceback" not in output.err
        assert output.err.count("test_endpoint failed: ValueError: boom") == 1

        flat = json.loads((report_dir / "test_endpoint.json").read_text())
        numbered = json.loads((report_dir / "test_endpoint/1/report.json").read_text())
        assert flat["report_text"] == numbered["report_text"]
        assert "test_endpoint failed: ValueError: boom" in flat["report_text"]
        diagnostic = Path(flat["report_text"].rsplit(". Diagnostic: ", 1)[1])
        assert diagnostic.is_file()
        assert report_dir / "test_endpoint/1" not in diagnostic.parents
        assert "Traceback" in diagnostic.read_text(encoding="utf-8")
        assert any(record.exc_info for record in caplog.records if record.levelname == "DEBUG")

    def test_exception_report_is_bounded_and_single_line(self, tmp_path: Path):
        report_dir = tmp_path / "reports"
        long_message = "first line\nsecond line " + "x" * 2_000

        class FailingMcpTool(ConcreteMcpTool):
            def _run(self):
                raise ValueError(long_message)

        result = FailingMcpTool().execute_cli(["--report-dir", str(report_dir)])

        assert result.exit_code == EXIT_ERROR
        diagnosis = result.outcome.report_text.split(". Diagnostic: ", 1)[0]
        assert "ValueError: first line second line" in diagnosis
        assert "\n" not in diagnosis
        assert len(diagnosis) < 1_100
        diagnostic = Path(result.outcome.report_text.rsplit(". Diagnostic: ", 1)[1])
        assert long_message in diagnostic.read_text(encoding="utf-8")

    def test_exception_diagnostic_failure_does_not_mask_report(self):
        class FailingMcpTool(ConcreteMcpTool):
            def _run(self):
                raise ValueError("boom")

        with mock.patch(
            "booley.flows.endpoint_session.write_exception_diagnostic", return_value=None
        ):
            result = FailingMcpTool().execute_cli([])

        assert result.exit_code == EXIT_ERROR
        assert result.outcome.report_text == "test_endpoint failed: ValueError: boom"
        assert "Diagnostic:" not in result.outcome.report_text

    def test_non_persisting_dry_run_exception_creates_no_artifacts(self, tmp_path: Path):
        report_dir = tmp_path / "reports"
        transcript_dir = tmp_path / "transcripts"

        class FailingDryRunMcpTool(ConcreteMcpTool):
            non_persisting_dry_run = True

            def _add_args(self, parser: argparse.ArgumentParser) -> None:
                parser.add_argument("--dry-run", action="store_true")
                parser.add_argument("--transcript-dir", type=Path)

            def transcript_path(self):
                self.args.transcript_dir.mkdir(parents=True, exist_ok=True)
                return self.args.transcript_dir / "dry-run.jsonl"

            def _run(self):
                raise ValueError("boom")

        result = FailingDryRunMcpTool().execute_cli(
            ["--dry-run", "--report-dir", str(report_dir), "--transcript-dir", str(transcript_dir)]
        )

        assert result.exit_code == EXIT_ERROR
        assert result.outcome.report_text == "test_endpoint failed: ValueError: boom"
        assert not report_dir.exists()
        assert not transcript_dir.exists()

    def test_transcript_diagnostic_path_uses_endpoint_resolver(self, tmp_path: Path):
        expected = tmp_path / "endpoint.jsonl"

        class Endpoint:
            args = argparse.Namespace(transcript_dir=tmp_path)

            def transcript_path(self):
                return expected

        assert _transcript_diagnostic_path(Endpoint()) == expected

    def test_transcript_diagnostic_path_uses_declared_default(self, tmp_path: Path):
        endpoint = ConcreteMcpTool()
        endpoint._args = argparse.Namespace(transcript_dir=tmp_path)

        assert _transcript_diagnostic_path(endpoint) is None

    def test_transcript_diagnostic_path_ignores_resolver_failure(self, tmp_path: Path):
        class Endpoint:
            args = argparse.Namespace(transcript_dir=tmp_path)

            def transcript_path(self):
                raise OSError("unavailable")

        assert _transcript_diagnostic_path(Endpoint()) is None

    def test_main_works_in_human_mode(self):
        """main() completes without BOOLEY_* env vars (no state file)."""
        endpoint = ConcreteMcpTool()
        env = {k: v for k, v in os.environ.items() if not k.startswith("BOOLEY_")}
        with mock.patch.dict(os.environ, env, clear=True):
            exit_code = endpoint.main([])
        assert exit_code == EXIT_SUCCESS


class MemoryMcpTool(ConcreteMcpTool):
    """ConcreteMcpTool classed as memory class — participates in slot-store admission."""

    JOB_CLASS = job_slots.CLASS_MEMORY


class TestJobSlotAdmission:
    """ADR 0028: McpTool.main() claims a Job Class slot from the shared on-disk
    store. The SlotStore
    scheduling itself is covered by tests/test_job_slots.py; these tests pin
    the *wiring* in main(): who claims, when, with what role, and how a full
    queue surfaces.
    """

    @staticmethod
    def _runtime_env(monkeypatch, tmp_path: Path) -> Path:
        """Point the slot store at tmp_path; return the slots root dir.

        BOOLEY_SLOTS_DIR is the store's test override — the real store is
        project-scoped (.booley/runtime/jobs/slots), shared across
        every venue in the container.
        """
        slots = tmp_path / "jobs" / "slots"
        monkeypatch.setenv("BOOLEY_LOGS_DIR", str(tmp_path / "logs"))
        monkeypatch.setenv("BOOLEY_RUNTIME_DIR", str(tmp_path))
        monkeypatch.setenv("BOOLEY_SLOTS_DIR", str(slots))
        return slots

    def test_unclassed_endpoint_skips_slot_store(self, tmp_path: Path, monkeypatch):
        """JOB_CLASS=None (the default) means no admission: quick endpoints must
        never pay the slot-store round-trip or leave claim files behind."""
        slots = self._runtime_env(monkeypatch, tmp_path)
        endpoint = ConcreteMcpTool()  # McpTool.JOB_CLASS default is None
        assert endpoint.main(["--work-dir", str(tmp_path)]) == EXIT_SUCCESS
        assert not slots.exists()

    def test_no_runtime_skips_slot_store(self, monkeypatch, tmp_path: Path):
        """A classed endpoint with no resolvable project (bare human invocation
        outside a project) runs unguarded — admission needs a slot store."""
        monkeypatch.delenv("BOOLEY_SLOTS_DIR", raising=False)
        monkeypatch.delenv("BOOLEY_DIR", raising=False)
        monkeypatch.delenv("BOOLEY_LOGS_DIR", raising=False)
        monkeypatch.delenv("BOOLEY_RUNTIME_DIR", raising=False)
        # No .booley above tmp_path, so project discovery fails and
        # slots_dir() returns None.
        monkeypatch.chdir(tmp_path)
        from booley.runtime.booley_dir import reset_cache

        reset_cache()
        endpoint = MemoryMcpTool()
        assert endpoint.main(["--work-dir", str(tmp_path)]) == EXIT_SUCCESS
        reset_cache()  # don't leak a cached "no project" into other tests

    def test_classed_endpoint_holds_slot_during_run(self, tmp_path: Path, monkeypatch):
        """A memory-admitted endpoint holds exactly one holder entry while _run() executes
        and releases it after main() returns (finally-path release)."""
        slots = self._runtime_env(monkeypatch, tmp_path)
        seen: dict[str, list] = {}

        class ObservingMcpTool(MemoryMcpTool):
            def _run(inner) -> McpToolResult:  # noqa: N805 — closure style
                store = job_slots.SlotStore(slots)
                holders, waiters = store.snapshot(job_slots.CLASS_MEMORY)
                seen["holders"] = holders
                seen["waiters"] = waiters
                return McpToolResult(exit_code=EXIT_SUCCESS)

        endpoint = ObservingMcpTool()
        assert endpoint.main(["--work-dir", str(tmp_path)]) == EXIT_SUCCESS
        # During _run: our PID held the one memory slot, nothing queued.
        assert [t.pid for t in seen["holders"]] == [os.getpid()]
        assert seen["waiters"] == []
        # After main: the finally released the claim — no live entries remain.
        assert list((slots / job_slots.CLASS_MEMORY).glob("*.json")) == []


class TestReadSourceDirs:
    """read_source_dirs_from_toml (now .core-backed) / as_str_list contracts.

    ADR 0026 follow-through: ``read_source_dirs_from_toml`` derives (rtl, tb)
    source dirs from the project's ``.core`` ``tags:[tb]`` partition and returns
    ``None`` when no ``.core`` is authored (callers then use their own defaults).
    """

    def test_as_str_list_wraps_bare_string(self):
        # Classic bug: source_dirs = "rtl" would iterate char-by-char.
        assert as_str_list("rtl", ["rtl"]) == ["rtl"]

    def test_as_str_list_filters_and_defaults(self):
        assert as_str_list(["a", 1, "b"], ["rtl"]) == ["a", "b"]
        assert as_str_list([], ["rtl"]) == ["rtl"]
        assert as_str_list(42, ["rtl"]) == ["rtl"]

    def test_core_drives_source_dirs(self, tmp_path: Path):
        # A .core whose filesets place RTL under hdl/ and TB under verif/ drives
        # the (rtl_dirs, tb_dirs) result — parent dir of each declared file.
        (tmp_path / "design.core").write_text(
            "CAPI=2:\n"
            "name: ::demo\n"
            "filesets:\n"
            "  rtl: {files: [hdl/mod.sv]}\n"
            "  tb: {files: [verif/tb.sv], tags: [tb]}\n"
            "targets:\n"
            "  sim: {filesets: [rtl, tb], toplevel: tb}\n",
            encoding="utf-8",
        )
        rtl, tb = read_source_dirs_from_toml(tmp_path)
        assert rtl == ["hdl"]
        assert tb == ["verif"]

    def test_no_core_returns_none(self, tmp_path: Path):
        # No .core authored under work_dir → None (caller falls back to defaults).
        assert read_source_dirs_from_toml(tmp_path) is None


class TestReportDirRejectsMangledHostPath:
    """B1: `booley session enter -- ... --report-dir /tmp/rep` reached the endpoint
    inside the container as `C:/Users/.../Temp/rep` (Git Bash/MSYS rewrites
    POSIX argv when it spawns the native booley exe). POSIX pathlib reads that
    as a RELATIVE path whose first component is literally `C:`, so every
    `report_dir.mkdir(parents=True)` created `/work/C:/Users/...` — a junk `C:`
    directory inside the user's repo (172 KB of reports on taxi), while the
    "See <path>" summary echoed the Windows path back and looked plausible."""

    _MANGLED = "C:/Users/andre/AppData/Local/Temp/rep-lint"

    @staticmethod
    def _parse(argv: list[str]):
        return ConcreteMcpTool().parse_args(argv)

    @staticmethod
    def _parse_as_posix(argv: list[str]):
        """Parse with pathlib forced to its POSIX flavour.

        The bug only exists where the endpoint actually runs — inside the Linux
        container, where `C:/…` is a *relative* path. On a Windows host the same
        string is a legitimate absolute path and must pass through untouched
        (asserted separately). Pinning the flavour keeps this a real test of the
        guard on either host instead of a skip on Windows.

        The endpoint is constructed OUTSIDE the patch — ``--work-dir``'s default
        needs a concrete ``Path.cwd()``; only the parse is flavour-pinned.
        """
        endpoint = ConcreteMcpTool()
        with mock.patch("booley.flows.endpoint_cli.Path", PurePosixPath):
            return endpoint.parse_args(argv)

    def test_drive_lettered_relative_path_is_rejected(self):
        with pytest.raises(SystemExit):
            self._parse_as_posix(["--report-dir", self._MANGLED])

    def test_the_error_names_the_cause_and_the_fix(self, capsys):
        with pytest.raises(SystemExit):
            self._parse_as_posix(["--report-dir", self._MANGLED])
        err = capsys.readouterr().err
        assert "MSYS_NO_PATHCONV" in err
        assert "C:" in err

    def test_container_paths_survive_the_posix_reading(self):
        args = self._parse_as_posix(["--report-dir", "/tmp/rep-lint"])
        assert args.report_dir == PurePosixPath("/tmp/rep-lint")

    def test_a_real_windows_host_path_is_not_rejected_on_windows(self):
        # The guard must never fire on a genuine host path: there `C:/…` is
        # absolute, so the "relative path with a drive component" condition —
        # the only thing that can't be meant — is false.
        if not Path(self._MANGLED).is_absolute():
            pytest.skip("POSIX host: C:/… is the mangled form here, not a host path")
        assert self._parse(["--report-dir", self._MANGLED]).report_dir == Path(self._MANGLED)

    def test_an_ordinary_relative_path_still_works(self, tmp_path: Path):
        args = self._parse(["--report-dir", "reports/lint"])
        assert args.report_dir == Path("reports/lint")

    def test_an_absolute_container_path_still_works(self):
        args = self._parse(["--report-dir", "/tmp/rep-lint"])
        assert args.report_dir == Path("/tmp/rep-lint")

    def test_a_dir_merely_starting_with_a_letter_is_not_a_drive(self):
        # `C` is a directory name, not a drive: no colon, so no rewriting.
        args = self._parse(["--report-dir", "C/reports"])
        assert args.report_dir == Path("C/reports")


class TestReportRunIdStamp:
    def test_run_id_from_env_lands_in_report(self, tmp_path: Path):
        # The MCP dispatch layer exports BOOLEY_RUN_ID for async jobs; the
        # report must carry it so polls can match reports by identity.
        report_dir = tmp_path / "reports"
        endpoint = ConcreteMcpTool()
        env = os.environ.copy()
        env["BOOLEY_RUN_ID"] = "simulate-20260716T120000-1"
        with mock.patch.dict(os.environ, env):
            endpoint.parse_args(["--report-dir", str(report_dir)])
            path = endpoint.write_report(McpToolResult(exit_code=EXIT_SUCCESS))
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["run_id"] == "simulate-20260716T120000-1"

    def test_no_env_no_run_id_key(self, tmp_path: Path):
        report_dir = tmp_path / "reports"
        endpoint = ConcreteMcpTool()
        env = {k: v for k, v in os.environ.items() if k != "BOOLEY_RUN_ID"}
        with mock.patch.dict(os.environ, env, clear=True):
            endpoint.parse_args(["--report-dir", str(report_dir)])
            path = endpoint.write_report(McpToolResult(exit_code=EXIT_SUCCESS))
        data = json.loads(path.read_text(encoding="utf-8"))
        assert "run_id" not in data


class TestSlotClaimBudget:
    def test_slot_timeout_from_env_with_headroom(self, tmp_path: Path, monkeypatch):
        # BOOLEY_SLOT_TIMEOUT_S (the MCP watchdog budget) becomes the holder
        # deadline with 2x headroom — a strict upper bound of any legit run.
        from booley.runtime import job_slots

        monkeypatch.setenv("BOOLEY_SLOTS_DIR", str(tmp_path / "slots"))
        monkeypatch.setenv("BOOLEY_SLOT_TIMEOUT_S", "600")

        endpoint = ConcreteMcpTool()
        endpoint.parse_args([])
        monkeypatch.setattr(type(endpoint), "JOB_CLASS", job_slots.CLASS_MEMORY, raising=False)
        claim = endpoint_admission._acquire_memory_reservation(endpoint, None)
        store, token = claim.store, claim.token
        assert store is not None
        assert token is not None
        assert token.timeout_s == 1200.0
        store.release(token)

    def test_unparseable_budget_keeps_no_deadline(self, tmp_path: Path, monkeypatch):
        from booley.runtime import job_slots

        monkeypatch.setenv("BOOLEY_SLOTS_DIR", str(tmp_path / "slots"))
        monkeypatch.setenv("BOOLEY_SLOT_TIMEOUT_S", "soon")

        endpoint = ConcreteMcpTool()
        endpoint.parse_args([])
        monkeypatch.setattr(type(endpoint), "JOB_CLASS", job_slots.CLASS_MEMORY, raising=False)
        claim = endpoint_admission._acquire_memory_reservation(endpoint, None)
        store, token = claim.store, claim.token
        assert store is not None
        assert token is not None
        assert token.timeout_s is None
        store.release(token)


class TestTargetHelpIsMcpToolNeutral:
    """--target help is also the MCP schema description (SETUP-F-29a)."""

    def test_default_help_promises_no_simulation(self):
        endpoint = ConcreteMcpTool()
        help_text = endpoint._parser.format_help()
        assert "this run applies to" in help_text
        assert "to simulate" not in help_text
        assert "sim sub-loop" not in help_text

    def test_a_endpoint_can_override_it(self):
        class SimSubLoopMcpTool(ConcreteMcpTool):
            name = "sub_loop"
            target_help = "Target(s) the sim sub-loop drives. REQUIRED."

        assert "sim sub-loop drives" in SimSubLoopMcpTool()._parser.format_help()


class TestNoReportDirWarning:
    """Every endpoint — not just reviewer — says when it persists no verdict."""

    def _run_endpoint(self, argv: list[str], tmp_path: Path):
        state_file = tmp_path / "state.json"
        endpoint = ConcreteMcpTool()
        with mock.patch.dict(os.environ, _env_with_state(state_file)):
            return endpoint.main([*argv, "--work-dir", str(tmp_path)])

    def test_warns_when_no_report_dir(self, tmp_path: Path, capsys):
        assert self._run_endpoint([], tmp_path) == EXIT_SUCCESS
        err = capsys.readouterr().err
        assert "no --report-dir" in err
        assert "test_endpoint" in err

    def test_silent_when_a_report_is_written(self, tmp_path: Path, capsys):
        report_dir = tmp_path / "reports"
        assert self._run_endpoint(["--report-dir", str(report_dir)], tmp_path) == EXIT_SUCCESS
        assert "no --report-dir" not in capsys.readouterr().err
        assert (report_dir / "test_endpoint.json").exists()

    def test_warning_uses_one_channel(self, tmp_path: Path, capsys, caplog):
        """Emitting it through both `logger.warning` and `print` put the same
        sentence on stderr twice — the very doubling F-28 set out to remove."""
        import logging

        with caplog.at_level(logging.WARNING, logger="booley.mcp.base"):
            assert self._run_endpoint([], tmp_path) == EXIT_SUCCESS
        err = capsys.readouterr().err
        assert err.count("no --report-dir") == 1
        assert not [r for r in caplog.records if "report-dir" in r.getMessage()]


def test_custom_endpoint_report_and_completion_carry_no_criterion(tmp_path):
    class EvaluatingTool(ConcreteMcpTool):
        def _run(self):
            self.set_criterion("actual", True)
            return McpToolResult(exit_code=EXIT_SUCCESS)

    tool = EvaluatingTool()
    reports = tmp_path / "reports"
    result = tool.execute_cli(["--work-dir", str(tmp_path), "--report-dir", str(reports)])
    assert result.exit_code == 0
    assert_paired_headline(reports, tool.name)
    flat = json.loads((reports / f"{tool.name}.json").read_text())
    assert "criterion_key" not in flat and "criterion_met" not in flat


def test_project_local_sim_endpoint_writes_project_local_schema(tmp_path):
    """A Project-local endpoint named ``sim`` is not the Simulation Flow."""

    class OtherTool(ConcreteMcpTool):
        name = "other"

    documents = {}
    for tool in (ConcreteMcpTool(), OtherTool()):
        if tool.name == "test_endpoint":
            tool.name = "sim"
        tool.parse_args(["--report-dir", str(tmp_path / tool.name / "reports")])
        documents[tool.name] = json.loads(tool.write_report(McpToolResult()).read_text())
    sim, other = documents["sim"], documents["other"]
    assert "$schema" not in sim
    assert sim.keys() == other.keys()


def test_report_schema_follows_endpoint_kind(tmp_path):
    """Each endpoint kind writes its own schema, whatever the endpoint is named."""
    from booley.flows.lint.flow import LintFlow
    from booley.flows.lint.request import LintRequest
    from booley.specialists.reviewer import ReviewerSpecialist
    from tests.flows.test_flow_base import EchoFlow

    class SimNamedFlow(EchoFlow):
        name = "sim"

    lint = LintFlow()
    lint._args = LintRequest(
        target="chosen", work_dir=tmp_path, report_dir=tmp_path / "lint-reports"
    )
    custom = SimNamedFlow()
    custom.parse_args(["--target", "t", "--report-dir", str(tmp_path / "custom-reports")])
    specialist = ReviewerSpecialist()
    specialist.parse_args(
        ["--work-dir", str(tmp_path), "--report-dir", str(tmp_path / "specialist-reports")]
    )
    schemas = {
        endpoint_name: json.loads(endpoint.write_report(McpToolResult()).read_text())["$schema"]
        for endpoint_name, endpoint in {
            "specialist": specialist,
            "custom": custom,
            "lint": lint,
        }.items()
    }
    assert schemas == {
        "specialist": "booley.specialist-report/v1",
        "custom": "booley.flow-report/v1",
        "lint": "booley.flow-report/v1",
    }
