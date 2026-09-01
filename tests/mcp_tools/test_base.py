"""Tests for McpTool base class — argparse, set_criterion, git diff classification, exit codes."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path, PurePosixPath
from typing import ClassVar
from unittest import mock

import pytest

from booley.criteria.state import (
    CATEGORY_RTL,
    CATEGORY_TB,
    SOURCE_FINGERPRINT_DETAIL_KEY,
    DevelopmentState,
    as_str_list,
)
from booley.mcp.base import (
    EXIT_ERROR,
    EXIT_FAILURE,
    EXIT_SUCCESS,
    McpTool,
    McpToolResult,
    _as_pid,
    _classify_files,
    _scan_endpoint_events,
    _StdoutWitness,
    _write_display_event,
    read_source_dirs_from_toml,
)
from booley.runtime import job_slots


class ConcreteMcpTool(McpTool):
    """Minimal concrete endpoint for testing."""

    name = "test_endpoint"
    description = "A test endpoint"

    def __init__(self, *, code_modifying: bool = False, modifies_category: str | None = None):
        self.code_modifying = code_modifying
        self.modifies_category = modifies_category
        super().__init__()

    def _add_args(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument("--extra", default="")

    def _run(self) -> McpToolResult:
        return McpToolResult(
            exit_code=EXIT_SUCCESS,
            criterion_key="test_criterion",
            criterion_met=True,
        )


class SimLikeMcpTool(ConcreteMcpTool):
    """Dummy simulate endpoint used to assert base guard behavior."""

    name = "sim"
    satisfies: ClassVar[list[str]] = ["sim_pass"]

    def _run(self) -> McpToolResult:
        self.set_criterion("sim_pass_default", True)
        return McpToolResult(exit_code=EXIT_SUCCESS, criterion_key="sim_pass_default")


class LintLikeMcpTool(ConcreteMcpTool):
    """Dummy lint endpoint used to exercise sealed invocation binding."""

    name = "lint"
    satisfies: ClassVar[list[str]] = ["lint_clean"]
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


class TestClassifyFiles:
    """Test diff classification with known prefixes (mocked to be project-agnostic)."""

    # Patch module-level _RTL_DIRS/_TB_DIRS loaded from config at import time
    @mock.patch("booley.mcp.base._TB_DIRS", ("tb/",))
    @mock.patch("booley.mcp.base._RTL_DIRS", ("rtl/", "fw/"))
    def test_rtl_files(self):
        cats = _classify_files(["rtl/my_module.sv", "rtl/sub/other.v"])
        assert cats == {CATEGORY_RTL}

    @mock.patch("booley.mcp.base._TB_DIRS", ("tb/",))
    @mock.patch("booley.mcp.base._RTL_DIRS", ("rtl/", "fw/"))
    def test_tb_files(self):
        cats = _classify_files(["tb/my_tb.sv"])
        assert cats == {CATEGORY_TB}

    @mock.patch("booley.mcp.base._TB_DIRS", ("tb/",))
    @mock.patch("booley.mcp.base._RTL_DIRS", ("rtl/", "fw/"))
    def test_fw_files_are_rtl(self):
        cats = _classify_files(["fw/boot.s"])
        assert cats == {CATEGORY_RTL}

    @mock.patch("booley.mcp.base._TB_DIRS", ("tb/",))
    @mock.patch("booley.mcp.base._RTL_DIRS", ("rtl/", "fw/"))
    def test_mixed(self):
        cats = _classify_files(["rtl/a.sv", "tb/b.sv"])
        assert cats == {CATEGORY_RTL, CATEGORY_TB}

    @mock.patch("booley.mcp.base._TB_DIRS", ("tb/",))
    @mock.patch("booley.mcp.base._RTL_DIRS", ("rtl/", "fw/"))
    def test_unrelated_files(self):
        cats = _classify_files(["docs/readme.md", "util/script.py"])
        assert cats == set()

    @mock.patch("booley.mcp.base._TB_DIRS", ("tb/",))
    @mock.patch("booley.mcp.base._RTL_DIRS", ("rtl/", "fw/"))
    def test_empty(self):
        cats = _classify_files([])
        assert cats == set()


class TestClassifyFlatRepo:
    """ADR 0026: a flat single-file repo classifies by exact file path."""

    def _write_flat_project(self, tmp_path: Path) -> None:
        # ADR 0026: a flat single-file repo authors a root-level .core; its
        # tags:[tb] partition (not [sources.*]) is the classification truth.
        (tmp_path / "picorv32.core").write_text(
            "CAPI=2:\n"
            "name: ::picorv32\n"
            "filesets:\n"
            "  rtl: {files: [picorv32.v]}\n"
            "  tb: {files: [picorv32_tb.v], tags: [tb]}\n"
            "targets:\n"
            "  sim: {filesets: [rtl, tb], toplevel: picorv32_tb}\n",
            encoding="utf-8",
        )
        (tmp_path / "picorv32.v").write_text("// rtl\n", encoding="utf-8")
        (tmp_path / "picorv32_tb.v").write_text("// tb\n", encoding="utf-8")

    def test_file_entries_classify_by_exact_path(self, tmp_path: Path):
        self._write_flat_project(tmp_path)
        assert _classify_files(["picorv32.v"], tmp_path) == {CATEGORY_RTL}
        assert _classify_files(["picorv32_tb.v"], tmp_path) == {CATEGORY_TB}

    def test_file_prefix_does_not_leak_to_siblings(self, tmp_path: Path):
        """cpu.v as an RTL entry must not also claim a sibling cpu.vh."""
        self._write_flat_project(tmp_path)
        # A header sharing the stem is NOT the configured file -> unrelated.
        assert _classify_files(["picorv32.vh"], tmp_path) == set()

    def test_dir_and_file_entries_coexist(self, tmp_path: Path):
        # A nested RTL source and a root-level tb-tagged file coexist: the .core
        # partition classifies each by its exact declared path.
        (tmp_path / "design.core").write_text(
            "CAPI=2:\n"
            "name: ::demo\n"
            "filesets:\n"
            "  rtl: {files: [rtl/core.sv]}\n"
            "  tb: {files: [dut_tb.v], tags: [tb]}\n"
            "targets:\n"
            "  sim: {filesets: [rtl, tb], toplevel: dut_tb}\n",
            encoding="utf-8",
        )
        (tmp_path / "rtl").mkdir()
        (tmp_path / "dut_tb.v").write_text("// tb\n", encoding="utf-8")
        assert _classify_files(["rtl/core.sv"], tmp_path) == {CATEGORY_RTL}
        assert _classify_files(["dut_tb.v"], tmp_path) == {CATEGORY_TB}


class TestMcpToolArgparse:
    def test_common_args_from_env(self, tmp_path: Path):
        state_file = tmp_path / "state.json"
        env = _env_with_state(state_file, slug="my-ticket")
        endpoint = ConcreteMcpTool()
        with mock.patch.dict(os.environ, env):
            args = endpoint.parse_args(["--target", "lite"])
        assert args.slug == "my-ticket"
        assert args.state_file == state_file
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
        assert args.state_file is None
        assert args.report_dir is None

    def test_report_dir_from_env(self, tmp_path: Path):
        logs_dir = tmp_path / "logs"
        env = os.environ.copy()
        env["BOOLEY_LOGS_DIR"] = str(logs_dir)
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


class TestMcpToolGateBehavior:
    def test_simulate_not_blocked_by_unmet_review_criteria(self, tmp_path: Path):
        state_file = tmp_path / "state.json"
        state = DevelopmentState.load(state_file)
        state.init_criteria(
            {
                "review_tb_quality_done": True,
                "sim_pass_default": True,
            }
        )
        state.save()

        (tmp_path / "rtl").mkdir()
        (tmp_path / "tb").mkdir()
        (tmp_path / "rtl" / "dut.sv").write_text("module dut; endmodule\n", encoding="utf-8")
        (tmp_path / "tb" / "tb.sv").write_text("module tb; endmodule\n", encoding="utf-8")

        endpoint = SimLikeMcpTool()
        env = _env_with_state(state_file)
        with mock.patch.dict(os.environ, env):
            exit_code = endpoint.main(["--work-dir", str(tmp_path)])

        assert exit_code == EXIT_SUCCESS
        saved = DevelopmentState.load(state_file)
        assert saved.criteria["sim_pass_default"].met is True

    def test_wrong_target_is_rejected_before_run(self, tmp_path: Path, capsys):
        state_file = tmp_path / "state.json"
        state = DevelopmentState.load(state_file)
        state.init_criteria({"lint_clean_lint_uart": True}, strict=True)
        state.save()
        endpoint = LintLikeMcpTool()

        with mock.patch.dict(os.environ, _env_with_state(state_file)):
            exit_code = endpoint.main(["--target", "sim_uart"])

        assert exit_code == EXIT_ERROR
        assert endpoint.ran is False
        assert "lint --target lint_uart" in capsys.readouterr().err
        assert "lint_clean_sim_uart" not in DevelopmentState.load(state_file).criteria

    def test_qualified_selector_matches_bare_target_criterion_identity(self, tmp_path: Path):
        (tmp_path / "rtl").mkdir()
        (tmp_path / "rtl" / "uart.sv").write_text("module uart; endmodule\n")
        (tmp_path / "uart.core").write_text(
            "CAPI=2:\n"
            "name: acme:ip:uart:1.0\n"
            "filesets:\n"
            "  rtl: {files: [rtl/uart.sv]}\n"
            "targets:\n"
            "  lint_uart:\n"
            "    flow: lint\n"
            "    flow_options: {tool: verilator}\n"
            "    filesets: [rtl]\n"
            "    toplevel: uart\n",
            encoding="utf-8",
        )
        state_file = tmp_path / "state.json"
        state = DevelopmentState.load(state_file)
        state.init_criteria({"lint_clean_lint_uart": True}, strict=True)
        state.save()

        class BoundLint(LintLikeMcpTool):
            def _run(self) -> McpToolResult:
                self.ran = True
                self.set_criterion(
                    f"lint_clean_{self.args.target}",
                    True,
                    source_target=self.args.target,
                )
                return McpToolResult(exit_code=EXIT_SUCCESS)

        endpoint = BoundLint()
        with mock.patch.dict(os.environ, _env_with_state(state_file)):
            exit_code = endpoint.main(
                [
                    "--work-dir",
                    str(tmp_path),
                    "--target",
                    "acme:ip:uart:1.0#lint_uart",
                ]
            )

        assert exit_code == EXIT_SUCCESS
        assert endpoint.ran is True
        assert DevelopmentState.load(state_file).criteria["lint_clean_lint_uart"].met is True

    def test_diagnostic_wrong_target_runs_without_evidence(self, tmp_path: Path):
        state_file = tmp_path / "state.json"
        state = DevelopmentState.load(state_file)
        state.init_criteria({"lint_clean_lint_uart": True}, strict=True)
        state.save()
        endpoint = LintLikeMcpTool()

        with mock.patch.dict(os.environ, _env_with_state(state_file)):
            exit_code = endpoint.main(["--target", "sim_uart", "--diagnostic"])

        assert exit_code == EXIT_SUCCESS
        assert endpoint.ran is True
        saved = DevelopmentState.load(state_file)
        assert saved.criteria["lint_clean_lint_uart"].met is False
        assert "lint_clean_sim_uart" not in saved.criteria


class TestMcpToolSetCriterion:
    def test_set_criterion_persists(self, tmp_path: Path):
        state_file = tmp_path / "state.json"
        st = DevelopmentState.load(state_file)
        st.slug = "test"
        st.init_criteria({"lint_clean_lite": True})
        st.save()

        env = _env_with_state(state_file)
        endpoint = ConcreteMcpTool()
        with mock.patch.dict(os.environ, env):
            endpoint.parse_args([])
        endpoint.read_state()
        endpoint.set_criterion("lint_clean_lite", True, detail={"warnings": 0})

        # Verify persisted
        st2 = DevelopmentState.load(state_file)
        assert st2.is_met("lint_clean_lite") is True

    def test_set_criterion_records_normalized_acceptance_evidence(self, tmp_path: Path):
        logs_dir = tmp_path / "logs" / "test"
        state_file = logs_dir / ".runtime" / "booley_state.json"
        state = DevelopmentState.load(state_file)
        state.slug = "test"
        state.init_criteria(
            {"sim_pass_uart_default": True},
            strict=True,
            flow_key_aliases={"sim_pass_default": ["sim_pass_uart_default"]},
        )
        state.save()

        env = _env_with_state(state_file)
        env.update(
            {
                "BOOLEY_EXECUTION_ID": "execution-2",
                "BOOLEY_LOGS_DIR": str(logs_dir),
            }
        )
        endpoint = ConcreteMcpTool()
        with mock.patch.dict(os.environ, env):
            endpoint.parse_args([])
            endpoint.read_state()
            endpoint.set_criterion(
                "sim_pass_default",
                True,
                detail={"pending": [{"summary": "still failing"}]},
            )

        records = list((logs_dir / "acceptance" / "evidence").glob("*/record.json"))
        assert len(records) == 1
        record = json.loads(records[0].read_text(encoding="utf-8"))
        assert record["criterion"] == "sim_pass_uart_default"
        assert record["met"] is False
        assert record["execution_id"] == "execution-2"

    def test_set_criterion_noop_without_state_file(self):
        """In human mode, set_criterion works but doesn't persist."""
        endpoint = ConcreteMcpTool()
        env = {k: v for k, v in os.environ.items() if not k.startswith("BOOLEY_")}
        with mock.patch.dict(os.environ, env, clear=True):
            endpoint.parse_args([])
        endpoint.read_state()
        # Should not raise
        endpoint.set_criterion("lint_clean_lite", True)
        assert endpoint.state.is_met("lint_clean_lite") is True

    def test_diagnostic_set_criterion_skips_in_memory_evidence(self):
        """Diagnostic runs never normalize, fingerprint, or record Criteria."""
        endpoint = ConcreteMcpTool()
        endpoint.parse_args(["--diagnostic"])
        endpoint.read_state()

        with (
            mock.patch.object(
                endpoint,
                "_criterion_key_for_source",
                side_effect=AssertionError("diagnostic run normalized a Criterion"),
            ),
            mock.patch.object(
                endpoint,
                "_stamp_source_fingerprint",
                side_effect=AssertionError("diagnostic run fingerprinted evidence"),
            ),
        ):
            endpoint.set_criterion(
                "lint_clean_lite",
                True,
                source_target="lint_selftest_bad",
            )

        assert "lint_clean_lite" not in endpoint.state.criteria

    def test_verification_fingerprint_is_scoped_to_source_target(self, tmp_path: Path):
        (tmp_path / "rtl").mkdir()
        (tmp_path / "tb").mkdir()
        (tmp_path / "other").mkdir()
        (tmp_path / "rtl" / "dut.sv").write_text("module dut; endmodule\n")
        (tmp_path / "rtl" / "defs.svh").write_text("`define WIDTH 8\n")
        (tmp_path / "tb" / "tb.sv").write_text("module tb; endmodule\n")
        (tmp_path / "other" / "baseline.sv").write_text("module baseline; endmodule\n")
        (tmp_path / "design.core").write_text(
            "CAPI=2:\n"
            "name: ::demo\n"
            "filesets:\n"
            "  rtl:\n"
            "    files: [rtl/dut.sv, {rtl/defs.svh: {is_include_file: true}}]\n"
            "  tb: {files: [tb/tb.sv], tags: [tb]}\n"
            "  other: {files: [other/baseline.sv]}\n"
            "targets:\n"
            "  sim: {filesets: [rtl, tb], toplevel: tb}\n"
            "  baseline: {filesets: [other], toplevel: baseline}\n",
            encoding="utf-8",
        )
        state_file = tmp_path / "state.json"
        state = DevelopmentState.load(state_file)
        state.init_criteria({"sim_pass_sim": True})
        state.save()
        endpoint = ConcreteMcpTool()
        with mock.patch.dict(os.environ, _env_with_state(state_file)):
            endpoint.parse_args(["--work-dir", str(tmp_path)])
        endpoint.read_state()

        endpoint.set_criterion("sim_pass_sim", True, source_target="sim")

        detail = DevelopmentState.load(state_file).criteria["sim_pass_sim"].detail
        stamp = detail[SOURCE_FINGERPRINT_DETAIL_KEY]
        assert stamp["target"] == "sim"
        assert stamp["fingerprint"]["rtl"]["files"] == ["rtl/defs.svh", "rtl/dut.sv"]
        assert stamp["fingerprint"]["tb"]["files"] == ["tb/tb.sv"]


class TestHumanModeStderr:
    """Standalone (no state file) failures surface report_text on stderr."""

    class _FailingMcpTool(ConcreteMcpTool):
        name = "failing_endpoint"

        def _run(self) -> McpToolResult:
            return McpToolResult(
                exit_code=EXIT_FAILURE,
                report_text="boom: the actual reason",
            )

    def test_report_text_to_stderr_on_human_failure(self, capsys):
        endpoint = self._FailingMcpTool()
        env = {k: v for k, v in os.environ.items() if not k.startswith("BOOLEY_")}
        with mock.patch.dict(os.environ, env, clear=True):
            rc = endpoint.main([])
        assert rc == EXIT_FAILURE
        assert "boom: the actual reason" in capsys.readouterr().err

    def test_no_stderr_spam_in_state_mode(self, tmp_path: Path, capsys):
        """Agent/state mode keeps report_text in report.json, not on stderr."""
        state_file = tmp_path / "state.json"
        DevelopmentState.load(state_file).save()
        endpoint = self._FailingMcpTool()
        with mock.patch.dict(os.environ, _env_with_state(state_file)):
            endpoint.main(["--report-dir", str(tmp_path / "reports")])
        assert "boom: the actual reason" not in capsys.readouterr().err

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

    class _ChattyBlockedMcpTool(ConcreteMcpTool):
        """A Specialist whose streamed log merely *mentions* its verdict word."""

        name = "chatty_blocked_endpoint"

        def _run(self) -> McpToolResult:
            print("agent: the vendor flow is BLOCKED on a missing license, escalating")
            return McpToolResult(exit_code=EXIT_FAILURE, report_text="BLOCKED")

    def test_a_mention_inside_a_log_line_does_not_suppress_the_reason(self, capsys, monkeypatch):
        """The witness must match whole printed lines, not any substring: a
        short report_text like tb_coder's bare "BLOCKED" otherwise matches any
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
        DevelopmentState.load(state_file).save()
        report_dir = tmp_path / "reports"

        env = _env_with_state(state_file)
        endpoint = ConcreteMcpTool()
        with mock.patch.dict(os.environ, env):
            endpoint.parse_args(["--report-dir", str(report_dir)])
        result = McpToolResult(
            exit_code=0,
            criterion_key="lint_clean_lite",
            criterion_met=True,
            detail={"warnings": 0},
        )
        path = endpoint.write_report(result)
        assert path is not None
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["mcp_tool"] == "test_endpoint"
        assert data["criterion_met"] is True

    def test_no_report_when_no_dir(self, tmp_path: Path):
        state_file = tmp_path / "state.json"
        DevelopmentState.load(state_file).save()

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
        result = McpToolResult(exit_code=0, criterion_key="test_check", criterion_met=True)
        path = endpoint.write_report(result)
        assert path is not None
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["slug"] == ""


class TestMcpToolMain:
    def test_success_flow(self, tmp_path: Path):
        state_file = tmp_path / "state.json"
        st = DevelopmentState.load(state_file)
        st.slug = "test"
        st.init_criteria({"test_criterion": True})
        st.save()

        env = _env_with_state(state_file)
        endpoint = ConcreteMcpTool()
        with mock.patch.dict(os.environ, env):
            exit_code = endpoint.main([])
        assert exit_code == EXIT_SUCCESS
        # Timeline recorded
        st2 = DevelopmentState.load(state_file)
        assert len(st2.timeline) == 1
        assert st2.timeline[0]["mcp_tool"] == "test_endpoint"
        assert st2.timeline[0]["exit_code"] == 0

    def test_code_modifying_main_records_resets_in_timeline(self, tmp_path: Path):
        logs_dir = tmp_path / "logs" / "test"
        state_file = logs_dir / ".runtime" / "booley_state.json"
        st = DevelopmentState.load(state_file)
        st.slug = "test"
        st.init_criteria({"lint_clean_lite": True}, strict=True)
        st.set_criterion("lint_clean_lite", True)
        st.save()

        env = _env_with_state(state_file)
        env["BOOLEY_LOGS_DIR"] = str(logs_dir)
        endpoint = ConcreteMcpTool(code_modifying=True, modifies_category=CATEGORY_RTL)
        with mock.patch.dict(os.environ, env):
            exit_code = endpoint.main([])
        assert exit_code == EXIT_SUCCESS
        st2 = DevelopmentState.load(state_file)
        assert st2.is_met("lint_clean_lite") is False
        # Timeline records the reset with ~ prefix
        assert any("~lint_clean_lite" in (e.get("criteria_set") or []) for e in st2.timeline)
        records = [
            json.loads(path.read_text(encoding="utf-8"))
            for path in (logs_dir / "acceptance" / "evidence").glob("*/record.json")
        ]
        assert [(record["criterion"], record["reason"]) for record in records] == [
            ("lint_clean_lite", "source-invalidated")
        ]

    def test_exception_returns_error(self, tmp_path: Path):
        state_file = tmp_path / "state.json"
        DevelopmentState.load(state_file).save()

        class FailingMcpTool(ConcreteMcpTool):
            def _run(self):
                raise ValueError("boom")

        env = _env_with_state(state_file)
        endpoint = FailingMcpTool()
        with mock.patch.dict(os.environ, env):
            exit_code = endpoint.main([])
        assert exit_code == EXIT_ERROR

    def test_main_works_in_human_mode(self):
        """main() completes without BOOLEY_* env vars (no state file)."""
        endpoint = ConcreteMcpTool()
        env = {k: v for k, v in os.environ.items() if not k.startswith("BOOLEY_")}
        with mock.patch.dict(os.environ, env, clear=True):
            exit_code = endpoint.main([])
        assert exit_code == EXIT_SUCCESS


class TestCriteriaInvalidation:
    def test_code_modifying_resets_rtl_and_sim(self, tmp_path: Path):
        """RTL changes must reset RTL, sim, AND coverage criteria."""
        state_file = tmp_path / "state.json"
        st = DevelopmentState.load(state_file)
        st.init_criteria(
            {
                "lint_clean_lite": True,
                "sim_pass_lite": True,
                "coverage_toggle": True,
                "coverage_branch": True,
            }
        )
        st.set_criterion("lint_clean_lite", True)
        st.set_criterion("sim_pass_lite", True)
        st.set_criterion("coverage_toggle", True)
        st.set_criterion("coverage_branch", True)
        st.save()

        env = _env_with_state(state_file)
        endpoint = ConcreteMcpTool(code_modifying=True, modifies_category=CATEGORY_RTL)
        with mock.patch.dict(os.environ, env):
            endpoint.parse_args([])
        endpoint.read_state()
        reset = endpoint.invalidate_dependent_criteria()
        assert "lint_clean_lite" in reset
        assert "sim_pass_lite" in reset
        assert "coverage_toggle" in reset
        assert "coverage_branch" in reset

    def test_non_code_modifying_no_reset(self, tmp_path: Path):
        state_file = tmp_path / "state.json"
        st = DevelopmentState.load(state_file)
        st.init_criteria({"lint_clean_lite": True})
        st.set_criterion("lint_clean_lite", True)
        st.save()

        env = _env_with_state(state_file)
        endpoint = ConcreteMcpTool(code_modifying=False)
        with mock.patch.dict(os.environ, env):
            endpoint.parse_args([])
        endpoint.read_state()
        reset = endpoint.invalidate_dependent_criteria()
        assert reset == []
        assert endpoint.state.is_met("lint_clean_lite") is True


# ===========================================================================
# _write_display_event
# ===========================================================================


class TestWriteDisplayEvent:
    def test_writes_jsonl_when_env_set(self, tmp_path: Path, monkeypatch):
        """Event appended to $BOOLEY_RUNTIME_DIR/display.jsonl."""
        monkeypatch.setenv("BOOLEY_LOGS_DIR", str(tmp_path))
        event = {"type": "endpoint_start", "endpoint": "lint"}
        _write_display_event(event)

        jsonl = tmp_path / ".runtime" / "display.jsonl"
        assert jsonl.exists()
        lines = jsonl.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 1
        assert json.loads(lines[0]) == event

    def test_appends_multiple_events(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("BOOLEY_LOGS_DIR", str(tmp_path))
        _write_display_event({"type": "endpoint_start"})
        _write_display_event({"type": "endpoint_end"})

        lines = (
            (tmp_path / ".runtime" / "display.jsonl")
            .read_text(encoding="utf-8")
            .strip()
            .splitlines()
        )
        assert len(lines) == 2

    def test_noop_when_env_unset(self, tmp_path: Path, monkeypatch):
        """No file created when BOOLEY_LOGS_DIR is absent."""
        monkeypatch.delenv("BOOLEY_LOGS_DIR", raising=False)
        _write_display_event({"type": "endpoint_start"})

        assert not (tmp_path / ".runtime" / "display.jsonl").exists()

    def test_emit_completion_marks_repeated_final_line(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("BOOLEY_LOGS_DIR", str(tmp_path))
        endpoint = ConcreteMcpTool()

        endpoint.emit_completion("✓ target_a", repeats_at_end=True)

        event = json.loads((tmp_path / ".runtime" / "display.jsonl").read_text(encoding="utf-8"))
        assert event == {
            "type": "endpoint_progress",
            "endpoint": "test_endpoint",
            "line": "✓ target_a",
            "completion": True,
            "repeats_at_end": True,
            "timestamp": event["timestamp"],
        }


# ===========================================================================
# McpTool.main() display events
# ===========================================================================


class TestMainDisplayEvents:
    def test_main_emits_endpoint_start_and_endpoint_end(self, tmp_path: Path):
        """main() brackets _run() with endpoint_start and endpoint_end display events."""
        state_file = tmp_path / "state.json"
        DevelopmentState.load(state_file).save()
        logs_dir = tmp_path / "logs"
        logs_dir.mkdir()

        env = _env_with_state(state_file)
        env["BOOLEY_LOGS_DIR"] = str(logs_dir)
        endpoint = ConcreteMcpTool()
        with mock.patch.dict(os.environ, env):
            exit_code = endpoint.main([])
        assert exit_code == EXIT_SUCCESS

        jsonl = logs_dir / ".runtime" / "display.jsonl"
        assert jsonl.exists()
        all_events = [
            json.loads(l) for l in jsonl.read_text(encoding="utf-8").strip().splitlines()
        ]
        events = [e for e in all_events if e["type"] in ("endpoint_start", "endpoint_end")]
        assert len(events) == 2
        assert events[0]["type"] == "endpoint_start"
        assert events[0]["endpoint"] == "test_endpoint"
        assert events[1]["type"] == "endpoint_end"
        assert events[1]["endpoint"] == "test_endpoint"
        assert events[1]["exit_code"] == EXIT_SUCCESS

    def test_endpoint_end_reflects_pre_save_hook_mutation(self, tmp_path: Path):
        """endpoint_end reports final result after promotion/pre-save hooks run."""
        state_file = tmp_path / "state.json"
        DevelopmentState.load(state_file).save()
        logs_dir = tmp_path / "logs"
        logs_dir.mkdir()

        class PromotionRejectingMcpTool(ConcreteMcpTool):
            def _pre_save_hook(self, result: McpToolResult) -> None:
                result.exit_code = EXIT_FAILURE
                result.criterion_met = False
                result.report_text = "promotion failed"

        env = _env_with_state(state_file)
        env["BOOLEY_LOGS_DIR"] = str(logs_dir)
        endpoint = PromotionRejectingMcpTool()
        with mock.patch.dict(os.environ, env):
            exit_code = endpoint.main([])
        assert exit_code == EXIT_FAILURE

        all_events = [
            json.loads(l)
            for l in (logs_dir / ".runtime" / "display.jsonl")
            .read_text(
                encoding="utf-8",
            )
            .strip()
            .splitlines()
        ]
        end_evt = next(e for e in all_events if e["type"] == "endpoint_end")
        assert end_evt["exit_code"] == EXIT_FAILURE
        assert end_evt["criterion_met"] is False
        assert end_evt["report_text"] == "promotion failed"

    def test_config_aware_false_suppresses_config(self, tmp_path: Path):
        """config_aware=False suppresses config in display events."""
        state_file = tmp_path / "state.json"
        DevelopmentState.load(state_file).save()
        logs_dir = tmp_path / "logs"
        logs_dir.mkdir()

        class NoConfigMcpTool(ConcreteMcpTool):
            config_aware = False

        env = _env_with_state(state_file)
        env["BOOLEY_LOGS_DIR"] = str(logs_dir)
        endpoint = NoConfigMcpTool()
        with mock.patch.dict(os.environ, env):
            endpoint.main(["--target", "default"])

        all_events = [
            json.loads(l)
            for l in (logs_dir / ".runtime" / "display.jsonl")
            .read_text(encoding="utf-8")
            .strip()
            .splitlines()
        ]
        events = [e for e in all_events if e["type"] in ("endpoint_start", "endpoint_end")]
        for evt in events:
            assert evt["target"] is None, f"{evt['type']} should have target=None"

    def test_endpoint_end_emitted_on_exception(self, tmp_path: Path):
        """endpoint_end is still written when _run() raises."""
        state_file = tmp_path / "state.json"
        DevelopmentState.load(state_file).save()
        logs_dir = tmp_path / "logs"
        logs_dir.mkdir()

        class FailingMcpTool(ConcreteMcpTool):
            def _run(self):
                raise RuntimeError("kaboom")

        env = _env_with_state(state_file)
        env["BOOLEY_LOGS_DIR"] = str(logs_dir)
        endpoint = FailingMcpTool()
        with mock.patch.dict(os.environ, env):
            exit_code = endpoint.main([])
        assert exit_code == EXIT_ERROR

        events = [
            json.loads(l)
            for l in (logs_dir / ".runtime" / "display.jsonl")
            .read_text(encoding="utf-8")
            .strip()
            .splitlines()
        ]
        types = [e["type"] for e in events]
        assert "endpoint_start" in types
        assert "endpoint_end" in types
        # endpoint_end should report the error exit code
        end_evt = next(e for e in events if e["type"] == "endpoint_end")
        assert end_evt["exit_code"] == EXIT_ERROR

    def test_display_tag_overrides_config(self, tmp_path: Path):
        """display_tag property overrides config_aware in display events."""
        state_file = tmp_path / "state.json"
        DevelopmentState.load(state_file).save()
        logs_dir = tmp_path / "logs"
        logs_dir.mkdir()

        class TaggedMcpTool(ConcreteMcpTool):
            config_aware = False

            @property
            def display_tag(self):
                return "rtl"

        env = _env_with_state(state_file)
        env["BOOLEY_LOGS_DIR"] = str(logs_dir)
        endpoint = TaggedMcpTool()
        with mock.patch.dict(os.environ, env):
            endpoint.main([])

        all_events = [
            json.loads(l)
            for l in (logs_dir / ".runtime" / "display.jsonl")
            .read_text(encoding="utf-8")
            .strip()
            .splitlines()
        ]
        events = [e for e in all_events if e["type"] in ("endpoint_start", "endpoint_end")]
        for evt in events:
            assert evt["target"] == "rtl", f"{evt['type']} should have target='rtl'"


class HeavyMcpTool(ConcreteMcpTool):
    """ConcreteMcpTool classed as HEAVY — participates in slot-store admission."""

    JOB_CLASS = job_slots.CLASS_HEAVY


class TestJobSlotAdmission:
    """ADR 0028: McpTool.main() claims a Job Class slot from the shared on-disk
    store instead of the deleted display.jsonl blocking scan. The SlotStore
    scheduling itself is covered by tests/test_job_slots.py; these tests pin
    the *wiring* in main(): who claims, when, with what role, and how a full
    queue surfaces.
    """

    @staticmethod
    def _runtime_env(monkeypatch, tmp_path: Path) -> Path:
        """Point the slot store at tmp_path; return the slots root dir.

        BOOLEY_SLOTS_DIR is the store's test override — the real store is
        project-scoped (.booley_project/runtime/jobs/slots), shared across
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
        monkeypatch.delenv("BOOLEY_PROJECT_DIR", raising=False)
        monkeypatch.delenv("BOOLEY_LOGS_DIR", raising=False)
        monkeypatch.delenv("BOOLEY_RUNTIME_DIR", raising=False)
        # No .booley_project above tmp_path, so project discovery fails and
        # slots_dir() returns None.
        monkeypatch.chdir(tmp_path)
        from booley.runtime.project_dir import reset_cache

        reset_cache()
        endpoint = HeavyMcpTool()
        assert endpoint.main(["--work-dir", str(tmp_path)]) == EXIT_SUCCESS
        reset_cache()  # don't leak a cached "no project" into other tests

    def test_classed_endpoint_holds_slot_during_run(self, tmp_path: Path, monkeypatch):
        """A HEAVY endpoint holds exactly one holder entry while _run() executes
        and releases it after main() returns (finally-path release)."""
        slots = self._runtime_env(monkeypatch, tmp_path)
        seen: dict[str, list] = {}

        class ObservingMcpTool(HeavyMcpTool):
            def _run(inner) -> McpToolResult:  # noqa: N805 — closure style
                store = job_slots.SlotStore(slots)
                holders, waiters = store.snapshot(job_slots.CLASS_HEAVY)
                seen["holders"] = holders
                seen["waiters"] = waiters
                return McpToolResult(exit_code=EXIT_SUCCESS)

        endpoint = ObservingMcpTool()
        assert endpoint.main(["--work-dir", str(tmp_path)]) == EXIT_SUCCESS
        # During _run: our PID held the one heavy slot, nothing queued.
        assert [t.pid for t in seen["holders"]] == [os.getpid()]
        assert seen["waiters"] == []
        # After main: the finally released the claim — no live entries remain.
        assert list((slots / job_slots.CLASS_HEAVY).glob("*.json")) == []

    def test_queue_full_returns_blocked_error(
        self,
        tmp_path: Path,
        monkeypatch,
        capsys,
    ):
        """cap + queue_max live entries already present → main() is refused
        with EXIT_ERROR and a BLOCKED report_text (ADR 0028 Decision 8: queue
        overflow is the only admission outcome surfaced as BLOCKED)."""
        slots = self._runtime_env(monkeypatch, tmp_path)
        # Fill the heavy class to its bound with default caps (cap 1 +
        # queue_max 8 = 9 live entries). pid=our own → provably alive;
        # argv=[] → the /proc identity check can't judge, so no reap.
        store = job_slots.SlotStore(slots)
        caps = job_slots.SlotCaps()
        for _ in range(caps.max_heavy + caps.queue_max):
            store.submit(job_slots.CLASS_HEAVY, pid=os.getpid(), argv=[])

        endpoint = HeavyMcpTool()  # human mode: report_text surfaces on stderr
        exit_code = endpoint.main(["--work-dir", str(tmp_path)])
        assert exit_code == EXIT_ERROR
        err = capsys.readouterr().err
        assert "BLOCKED" in err
        assert "queue is full" in err
        # The refused run withdrew its own entry — the queue did not grow.
        live = list((slots / job_slots.CLASS_HEAVY).glob("*.json"))
        assert len(live) == caps.max_heavy + caps.queue_max

    def test_ticket_role_recorded_on_claim(self, tmp_path: Path, monkeypatch):
        """BOOLEY_AGENT_ROLE=ticket claims at ticket priority — that role must
        land in the entry so a later interactive request can overtake it."""
        slots = self._runtime_env(monkeypatch, tmp_path)
        monkeypatch.setenv("BOOLEY_AGENT_ROLE", "ticket")
        seen: dict[str, list] = {}

        class ObservingMcpTool(HeavyMcpTool):
            def _run(inner) -> McpToolResult:  # noqa: N805 — closure style
                store = job_slots.SlotStore(slots)
                holders, _ = store.snapshot(job_slots.CLASS_HEAVY)
                seen["holders"] = holders
                return McpToolResult(exit_code=EXIT_SUCCESS)

        endpoint = ObservingMcpTool()
        assert endpoint.main(["--work-dir", str(tmp_path)]) == EXIT_SUCCESS
        assert len(seen["holders"]) == 1
        assert seen["holders"][0].role == job_slots.ROLE_TICKET
        assert seen["holders"][0].priority == 1  # ticket sorts after interactive


class TestMcpToolEventScan:
    """_scan_endpoint_events parses display.jsonl into unmatched starts + last
    ends. Post-ADR-0028 this is bookkeeping only (Console/telemetry) — it no
    longer gates admission — but the parsing contract still matters."""

    def test_matched_start_end_leaves_no_unmatched(self):
        lines = [
            '{"type":"endpoint_start","endpoint":"review","timestamp":"t0"}',
            '{"type":"endpoint_end","endpoint":"review","timestamp":"t1"}',
        ]
        unmatched, last_end = _scan_endpoint_events(lines)
        assert unmatched["review"] == []
        assert last_end["review"] == "t1"

    def test_unmatched_start_survives(self):
        lines = ['{"type":"endpoint_start","endpoint":"review","timestamp":"t0","pid":42}']
        unmatched, last_end = _scan_endpoint_events(lines)
        assert unmatched["review"] == [("t0", 42)]
        assert last_end == {}

    def test_garbage_lines_skipped(self):
        lines = [
            "not json at all",
            '{"type":"endpoint_start","timestamp":"t0"}',  # no endpoint → ignored
            '{"type":"endpoint_start","endpoint":"lint","timestamp":"t1"}',
        ]
        unmatched, _ = _scan_endpoint_events(lines)
        assert list(unmatched) == ["lint"]


class TestPidCoercion:
    """_as_pid validates the untrusted pid field of display.jsonl events."""

    def test_int_passthrough(self):
        assert _as_pid(4321) == 4321

    def test_numeric_string_coerced(self):
        assert _as_pid("4321") == 4321

    def test_none_and_missing(self):
        assert _as_pid(None) is None

    def test_garbage_string_is_none(self):
        assert _as_pid("not-a-pid") is None

    def test_bool_rejected(self):
        # bool is an int subclass; a JSON `true` pid must not become PID 1.
        assert _as_pid(True) is None

    def test_scan_events_coerces_string_pid(self, tmp_path: Path):
        # Boundary: a string pid from JSON must reach PID consumers as an int.
        lines = [
            '{"type":"endpoint_start","endpoint":"review","timestamp":"t0","pid":"4321"}',
            '{"type":"endpoint_start","endpoint":"lint","timestamp":"t1","pid":"bogus"}',
        ]
        unmatched, _ = _scan_endpoint_events(lines)
        assert unmatched["review"] == [("t0", 4321)]
        assert unmatched["lint"] == [("t1", None)]


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
        with mock.patch("booley.mcp.base.Path", PurePosixPath):
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
        assert self._parse(["--report-dir", self._MANGLED]).report_dir == Path(
            self._MANGLED,
        )

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
        monkeypatch.setattr(type(endpoint), "JOB_CLASS", job_slots.CLASS_HEAVY, raising=False)
        store, token = endpoint._acquire_job_slot()
        assert token is not None
        assert token.timeout_s == 1200.0
        store.release(token)

    def test_unparseable_budget_keeps_no_deadline(self, tmp_path: Path, monkeypatch):
        from booley.runtime import job_slots

        monkeypatch.setenv("BOOLEY_SLOTS_DIR", str(tmp_path / "slots"))
        monkeypatch.setenv("BOOLEY_SLOT_TIMEOUT_S", "soon")

        endpoint = ConcreteMcpTool()
        endpoint.parse_args([])
        monkeypatch.setattr(type(endpoint), "JOB_CLASS", job_slots.CLASS_HEAVY, raising=False)
        store, token = endpoint._acquire_job_slot()
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
        DevelopmentState.load(state_file).save()
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
