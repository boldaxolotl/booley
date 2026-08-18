"""Tests for submit_run_report -- end-of-run review report endpoint."""

from __future__ import annotations

import json
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from booley.dev_support.development_state import (
    CATEGORY_RTL,
    SOURCE_FINGERPRINT_DETAIL_KEY,
    DevelopmentState,
    compute_source_fingerprint,
)
from booley.mcp.base import EXIT_ERROR, EXIT_SUCCESS
from booley.mcp.submit_run_report import SubmitRunReportMcpTool
from booley.runtime import job_records as jobrec


@pytest.fixture
def state_file(tmp_path: Path) -> Path:
    """Create a state file with _report_submitted pre-injected (mandatory, unmet)."""
    sf = tmp_path / "state.json"
    st = DevelopmentState.load(sf)
    st.slug = "report-test"
    st.init_criteria({"_report_submitted": True, "implementation_done": True})
    st.set_criterion("implementation_done", True)
    st.save()
    return sf


def _run_endpoint(
    state_file: Path,
    report_dir: Path,
    ticket_type: str,
    argv: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[int, DevelopmentState]:
    """Invoke the endpoint via main(), returning (exit_code, reloaded_state)."""
    monkeypatch.setenv("BOOLEY_STATE_FILE", str(state_file))
    monkeypatch.setenv("BOOLEY_LOGS_DIR", str(report_dir))
    monkeypatch.setenv("BOOLEY_SLUG", "report-test")
    monkeypatch.setenv("BOOLEY_TICKET_TYPE", ticket_type)
    endpoint = SubmitRunReportMcpTool()
    exit_code = endpoint.main(argv)
    return exit_code, DevelopmentState.load(state_file)


def test_rejects_final_report_while_ticket_job_is_active(
    tmp_path: Path,
    state_file: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    runtime_dir = tmp_path / ".runtime"
    monkeypatch.setenv("BOOLEY_LOGS_DIR", str(tmp_path))
    monkeypatch.setenv("BOOLEY_RUNTIME_DIR", str(runtime_dir))
    jobrec.write_record(
        jobrec.JobRecord(
            run_id="mutation_tester-now-1",
            endpoint="mutation_tester",
            started_at=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            timeout_s=60,
            pid=os.getpid(),
        )
    )

    exit_code, state = _run_endpoint(
        state_file,
        tmp_path,
        "bugfix",
        [
            "--summary",
            "Fixed the bug.",
            "--root-cause",
            "Concurrent finalization.",
            "--uncertainties",
            "None beyond the running job.",
        ],
        monkeypatch,
    )

    assert exit_code == EXIT_ERROR
    assert not state.is_met("_report_submitted")
    assert not (tmp_path / "REPORT.md").exists()
    assert "outstanding ticket jobs" in capsys.readouterr().err


def test_rejects_report_and_updates_ui_when_verification_became_stale(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "rtl").mkdir()
    (tmp_path / "tb").mkdir()
    (tmp_path / "rtl" / "dut.sv").write_text("module dut; endmodule\n")
    (tmp_path / "tb" / "tb.sv").write_text("module tb; endmodule\n")
    state_path = tmp_path / "state.json"
    state = DevelopmentState.load(state_path)
    state.slug = "stale-report"
    state.init_criteria({"sim_pass_default": True, "_report_submitted": True})
    state.set_criterion(
        "sim_pass_default",
        True,
        detail={
            SOURCE_FINGERPRINT_DETAIL_KEY: {
                "categories": ["rtl", "tb"],
                "fingerprint": compute_source_fingerprint(tmp_path),
            }
        },
    )
    state.save()
    (tmp_path / "rtl" / "dut.sv").write_text("module dut; wire changed; endmodule\n")

    exit_code, state = _run_endpoint(
        state_path,
        tmp_path,
        "bugfix",
        [
            "--work-dir",
            str(tmp_path),
            "--summary",
            "Fixed the bug.",
            "--root-cause",
            "A stale simulation result.",
            "--uncertainties",
            "The simulation must be rerun.",
        ],
        monkeypatch,
    )

    assert exit_code == EXIT_ERROR
    assert state.criteria["sim_pass_default"].met is False
    assert state.criteria["sim_pass_default"].stale is True
    assert not (tmp_path / "REPORT.md").exists()
    events = [
        json.loads(line)
        for line in (tmp_path / ".runtime" / "display.jsonl").read_text().splitlines()
    ]
    update = next(event for event in events if event["type"] == "criteria_update")
    assert update["criteria"]["sim_pass_default"]["met"] is False
    assert update["criteria"]["sim_pass_default"]["stale"] is True


# ---------------------------------------------------------------------------
# Happy path per ticket type
# ---------------------------------------------------------------------------


class TestPerTypeHappyPath:
    """Each ticket type accepts its matching --<type-specific> arg."""

    def test_bugfix_with_root_cause(
        self,
        tmp_path: Path,
        state_file: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        exit_code, st = _run_endpoint(
            state_file,
            tmp_path,
            "bugfix",
            [
                "--summary",
                "Fixed off-by-one in counter wrap.",
                "--root-cause",
                "wrap condition used >= instead of >.",
                "--uncertainties",
                "no test exercises wrap at MAX_VAL exactly.",
            ],
            monkeypatch,
        )
        assert exit_code == EXIT_SUCCESS
        assert st.is_met("_report_submitted")

        report_text = (tmp_path / "REPORT.md").read_text(encoding="utf-8")
        assert "report-test" in report_text
        assert "`bugfix`" in report_text
        assert "## Root cause" in report_text
        assert "wrap condition used >= instead of >" in report_text
        assert "## Uncertainties" in report_text

    def test_feature_with_design_decisions(
        self,
        tmp_path: Path,
        state_file: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        exit_code, st = _run_endpoint(
            state_file,
            tmp_path,
            "feature",
            [
                "--summary",
                "Added APB slave.",
                "--design-decisions",
                "Picked little-endian byte order, PREADY always 1.",
                "--uncertainties",
                "Burst access not exercised.",
            ],
            monkeypatch,
        )
        assert exit_code == EXIT_SUCCESS
        assert st.is_met("_report_submitted")
        text = (tmp_path / "REPORT.md").read_text(encoding="utf-8")
        assert "## Design decisions" in text
        assert "little-endian" in text

    def test_feature_with_type_specific_detail(
        self,
        tmp_path: Path,
        state_file: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        exit_code, st = _run_endpoint(
            state_file,
            tmp_path,
            "feature",
            [
                "--summary",
                "Added APB slave.",
                "--type-specific-detail",
                "Picked little-endian byte order.",
                "--uncertainties",
                "Burst access not exercised.",
            ],
            monkeypatch,
        )
        assert exit_code == EXIT_SUCCESS
        assert st.is_met("_report_submitted")
        text = (tmp_path / "REPORT.md").read_text(encoding="utf-8")
        assert "## Design decisions" in text
        assert "little-endian" in text

    def test_refactor_with_behavior_preservation(
        self,
        tmp_path: Path,
        state_file: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        exit_code, st = _run_endpoint(
            state_file,
            tmp_path,
            "refactor",
            [
                "--summary",
                "Split monolithic FSM into submodules.",
                "--behavior-preservation",
                "All 12 regression tests still pass; equivalence checked via cone-of-influence.",
                "--uncertainties",
                "Synthesis area may differ marginally.",
            ],
            monkeypatch,
        )
        assert exit_code == EXIT_SUCCESS
        assert st.is_met("_report_submitted")
        text = (tmp_path / "REPORT.md").read_text(encoding="utf-8")
        assert "## Behavior preservation" in text

    def test_verification_with_coverage_added(
        self,
        tmp_path: Path,
        state_file: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        exit_code, st = _run_endpoint(
            state_file,
            tmp_path,
            "verification",
            [
                "--summary",
                "Added directed tests for reset edge cases.",
                "--coverage-added",
                "Async reset during burst, reset at clock edge.",
                "--uncertainties",
                "Bit-flip injection not yet covered.",
            ],
            monkeypatch,
        )
        assert exit_code == EXIT_SUCCESS
        assert st.is_met("_report_submitted")
        text = (tmp_path / "REPORT.md").read_text(encoding="utf-8")
        assert "## Coverage added" in text


# ---------------------------------------------------------------------------
# Validation -- wrong / missing type-specific args
# ---------------------------------------------------------------------------


class TestTypeMismatchRejected:
    """Endpoint exits 2 when the wrong type-specific arg is used."""

    def test_bugfix_missing_root_cause(
        self,
        tmp_path: Path,
        state_file: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        exit_code, st = _run_endpoint(
            state_file,
            tmp_path,
            "bugfix",
            ["--summary", "x", "--uncertainties", "y"],
            monkeypatch,
        )
        assert exit_code == EXIT_ERROR
        assert not st.is_met("_report_submitted")

    def test_bugfix_passes_feature_arg(
        self,
        tmp_path: Path,
        state_file: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Passing a non-matching type-specific arg should reject, not silently ignore."""
        exit_code, st = _run_endpoint(
            state_file,
            tmp_path,
            "bugfix",
            [
                "--summary",
                "x",
                "--root-cause",
                "real reason",
                "--design-decisions",
                "should not be here for bugfix",
                "--uncertainties",
                "y",
            ],
            monkeypatch,
        )
        assert exit_code == EXIT_ERROR
        assert not st.is_met("_report_submitted")

    def test_type_specific_detail_rejects_legacy_arg_mix(
        self,
        tmp_path: Path,
        state_file: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        exit_code, st = _run_endpoint(
            state_file,
            tmp_path,
            "feature",
            [
                "--summary",
                "x",
                "--type-specific-detail",
                "generic detail",
                "--design-decisions",
                "legacy detail",
                "--uncertainties",
                "y",
            ],
            monkeypatch,
        )
        assert exit_code == EXIT_ERROR
        assert not st.is_met("_report_submitted")

    def test_unknown_ticket_type(
        self,
        tmp_path: Path,
        state_file: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        exit_code, st = _run_endpoint(
            state_file,
            tmp_path,
            "made_up_type",
            [
                "--summary",
                "x",
                "--root-cause",
                "y",
                "--uncertainties",
                "z",
            ],
            monkeypatch,
        )
        assert exit_code == EXIT_ERROR
        assert not st.is_met("_report_submitted")

    def test_empty_type_specific_value_rejected(
        self,
        tmp_path: Path,
        state_file: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Whitespace-only --root-cause should not satisfy the requirement."""
        exit_code, st = _run_endpoint(
            state_file,
            tmp_path,
            "bugfix",
            [
                "--summary",
                "x",
                "--root-cause",
                "   ",
                "--uncertainties",
                "y",
            ],
            monkeypatch,
        )
        assert exit_code == EXIT_ERROR
        assert not st.is_met("_report_submitted")


# ---------------------------------------------------------------------------
# Universal args -- argparse-level requirements
# ---------------------------------------------------------------------------


class TestUniversalArgs:
    def test_summary_required(self) -> None:
        endpoint = SubmitRunReportMcpTool()
        with pytest.raises(SystemExit):
            endpoint.parse_args(["--uncertainties", "y", "--root-cause", "z"])

    def test_uncertainties_required(self) -> None:
        endpoint = SubmitRunReportMcpTool()
        with pytest.raises(SystemExit):
            endpoint.parse_args(["--summary", "x", "--root-cause", "z"])

    def test_help_tells_feature_tickets_not_to_use_coverage_added(self) -> None:
        endpoint = SubmitRunReportMcpTool()
        help_text = endpoint._parser.format_help()

        assert "feature only" in help_text
        assert "not" in help_text
        assert "--coverage-added" in help_text
        assert "Do not use for feature tickets" in help_text
        assert "--coverage-added" in SubmitRunReportMcpTool.description

    def test_mcp_schema_exposes_one_type_specific_detail_field(self) -> None:
        endpoint = SubmitRunReportMcpTool()
        schema = endpoint.mcp_schema()
        properties = schema["properties"]

        assert "type_specific_detail" in properties
        assert "type_specific_detail" in schema["required"]
        assert "design_decisions" not in properties
        assert "coverage_added" not in properties

    def test_mcp_schema_exposes_conditional_optional_criteria_justification(self) -> None:
        endpoint = SubmitRunReportMcpTool()
        schema = endpoint.mcp_schema()

        assert "optional_criteria_justification" in schema["properties"]
        assert "optional_criteria_justification" not in schema["required"]


class TestOptionalCriteriaJustification:
    """Unmet optional criteria are allowed only with a report explanation."""

    @staticmethod
    def _add_optional(state_file: Path, *, met: bool) -> None:
        state = DevelopmentState.load(state_file)
        state.set_criterion("mutation_score", met)
        state.save()

    def test_unmet_optional_rejected_without_justification(
        self,
        tmp_path: Path,
        state_file: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        self._add_optional(state_file, met=False)

        exit_code, state = _run_endpoint(
            state_file,
            tmp_path,
            "feature",
            [
                "--summary",
                "Added APB slave.",
                "--design-decisions",
                "Used a single-cycle response.",
                "--uncertainties",
                "No burst coverage.",
            ],
            monkeypatch,
        )

        assert exit_code == EXIT_ERROR
        assert not state.is_met("_report_submitted")
        assert not (tmp_path / "REPORT.md").exists()

    def test_unmet_optional_rejects_blank_justification(
        self,
        tmp_path: Path,
        state_file: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        self._add_optional(state_file, met=False)

        exit_code, state = _run_endpoint(
            state_file,
            tmp_path,
            "feature",
            [
                "--summary",
                "Added APB slave.",
                "--design-decisions",
                "Used a single-cycle response.",
                "--uncertainties",
                "No burst coverage.",
                "--optional-criteria-justification",
                "   ",
            ],
            monkeypatch,
        )

        assert exit_code == EXIT_ERROR
        assert not state.is_met("_report_submitted")

    def test_unmet_optional_justification_is_written_to_report(
        self,
        tmp_path: Path,
        state_file: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        self._add_optional(state_file, met=False)

        exit_code, state = _run_endpoint(
            state_file,
            tmp_path,
            "feature",
            [
                "--summary",
                "Added APB slave.",
                "--design-decisions",
                "Used a single-cycle response.",
                "--uncertainties",
                "No burst coverage.",
                "--optional-criteria-justification",
                "mutation_score could not run because no mutation backend is configured.",
            ],
            monkeypatch,
        )

        assert exit_code == EXIT_SUCCESS
        assert state.is_met("_report_submitted")
        assert state.criteria["_report_submitted"].detail["unmet_optional_criteria"] == [
            "mutation_score"
        ]
        report = (tmp_path / "REPORT.md").read_text(encoding="utf-8")
        assert "## Unmet optional criteria" in report
        assert "- `mutation_score`" in report
        assert "no mutation backend is configured" in report

    def test_met_optional_needs_no_justification(
        self,
        tmp_path: Path,
        state_file: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        self._add_optional(state_file, met=True)

        exit_code, _state = _run_endpoint(
            state_file,
            tmp_path,
            "feature",
            [
                "--summary",
                "Added APB slave.",
                "--design-decisions",
                "Used a single-cycle response.",
                "--uncertainties",
                "No burst coverage.",
            ],
            monkeypatch,
        )

        assert exit_code == EXIT_SUCCESS
        assert "## Unmet optional criteria" not in (tmp_path / "REPORT.md").read_text(
            encoding="utf-8"
        )


# ---------------------------------------------------------------------------
# Reset-on-code-change -- _report_submitted goes stale when categories reset
# ---------------------------------------------------------------------------


class TestReportResetsOnCodeChange:
    def _populate_state(self, tmp_path: Path) -> Path:
        sf = tmp_path / "state.json"
        st = DevelopmentState.load(sf)
        st.slug = "reset-test"
        st.init_criteria(
            {
                "_report_submitted": True,
                "sim_pass": True,
                "lint_clean": True,
            }
        )
        # Mark report and a real RTL-category criterion as met.
        st.set_criterion("_report_submitted", True)
        st.set_criterion("lint_clean", True)
        st.save()
        return sf

    def test_rtl_reset_clears_report(self, tmp_path: Path) -> None:
        sf = self._populate_state(tmp_path)
        st = DevelopmentState.load(sf)
        assert st.is_met("_report_submitted")
        reset_keys = st.reset_category(CATEGORY_RTL)
        assert "_report_submitted" in reset_keys
        assert not st.is_met("_report_submitted")
        assert st.criteria["_report_submitted"].stale

    def test_no_reset_when_nothing_else_reset(self, tmp_path: Path) -> None:
        """If category reset finds no real criteria to clear, leave report alone."""
        sf = tmp_path / "state.json"
        st = DevelopmentState.load(sf)
        st.slug = "no-op-reset"
        # Only the report criterion is set -- no rtl/tb criteria exist.
        st.init_criteria({"_report_submitted": True})
        st.set_criterion("_report_submitted", True)
        st.save()

        reset_keys = st.reset_category(CATEGORY_RTL)
        assert reset_keys == []
        assert st.is_met("_report_submitted")


# ---------------------------------------------------------------------------
# Submission receipt -- diff-stat + last-simulate echo in report_text
# ---------------------------------------------------------------------------


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.email=t@test", "-c", "user.name=t", *args],
        cwd=repo,
        check=True,
        capture_output=True,
    )


class TestSubmissionEcho:
    """report_text is a receipt: what was written, worktree diff-stat, and
    the last simulation verdict -- so the agent skips the manual pre-submit
    sed/git-diff ritual. All echoes are best-effort."""

    @pytest.fixture
    def git_repo(self, tmp_path: Path) -> Path:
        repo = tmp_path / "repo"
        repo.mkdir()
        _git(repo, "init", "-q")
        (repo / "rtl.sv").write_text("module m; endmodule\n", encoding="utf-8")
        _git(repo, "add", "rtl.sv")
        _git(repo, "commit", "-qm", "init")
        return repo

    def _submit(
        self,
        tmp_path: Path,
        state_file: Path,
        work_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> str:
        """Run the endpoint via main() and return the report_text it recorded."""
        runtime = tmp_path / "runtime"
        monkeypatch.setenv("BOOLEY_STATE_FILE", str(state_file))
        monkeypatch.setenv("BOOLEY_LOGS_DIR", str(tmp_path / "logs"))
        monkeypatch.setenv("BOOLEY_RUNTIME_DIR", str(runtime))
        monkeypatch.setenv("BOOLEY_SLUG", "report-test")
        monkeypatch.setenv("BOOLEY_TICKET_TYPE", "bugfix")
        endpoint = SubmitRunReportMcpTool()
        exit_code = endpoint.main(
            [
                "--work-dir",
                str(work_dir),
                "--summary",
                "Fixed it.",
                "--root-cause",
                "Wrong wrap condition.",
                "--uncertainties",
                "None worth noting.",
            ]
        )
        assert exit_code == EXIT_SUCCESS
        flat = json.loads(
            (runtime / "mcp-tool-reports" / "submit_run_report.json").read_text(encoding="utf-8")
        )
        return flat["report_text"]

    def test_receipt_contains_diff_stat(
        self,
        tmp_path: Path,
        state_file: Path,
        git_repo: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        (git_repo / "rtl.sv").write_text("module m2; endmodule\n", encoding="utf-8")
        text = self._submit(tmp_path, state_file, git_repo, monkeypatch)
        assert text.startswith("Wrote ")
        assert "Captured with this submission:" in text
        assert "worktree diff (git diff --stat):" in text
        assert "rtl.sv" in text

    def test_receipt_reports_clean_worktree(
        self,
        tmp_path: Path,
        state_file: Path,
        git_repo: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        text = self._submit(tmp_path, state_file, git_repo, monkeypatch)
        assert "worktree diff: clean (no uncommitted changes)" in text

    def test_receipt_diff_unavailable_outside_repo(
        self,
        tmp_path: Path,
        state_file: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        plain = tmp_path / "plain"
        plain.mkdir()
        text = self._submit(tmp_path, state_file, plain, monkeypatch)
        assert "worktree diff: unavailable" in text

    def test_receipt_includes_last_simulate_fingerprint(
        self,
        tmp_path: Path,
        state_file: Path,
        git_repo: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("BOOLEY_LOCAL_TIMEZONE", "+04:00")
        reports = tmp_path / "runtime" / "flow-reports"
        reports.mkdir(parents=True)
        (reports / "sim_default.json").write_text(
            json.dumps(
                {
                    "flow": "sim",
                    "target": "default",
                    "passed": True,
                    "timestamp": "2026-07-25T12:00:00Z",
                }
            ),
            encoding="utf-8",
        )
        text = self._submit(tmp_path, state_file, git_repo, monkeypatch)
        assert "last sim: default passed=True at 16:00:00 · 25 JUL 2026" in text

    def test_receipt_omits_simulate_line_when_absent(
        self,
        tmp_path: Path,
        state_file: Path,
        git_repo: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        text = self._submit(tmp_path, state_file, git_repo, monkeypatch)
        assert "last simulate" not in text

    def test_diff_stat_elides_long_file_lists(
        self,
        git_repo: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # 10 modified tracked files -> head + "... (N more)" + git's summary.
        for i in range(10):
            f = git_repo / f"mod_{i}.sv"
            f.write_text("module a; endmodule\n", encoding="utf-8")
        _git(git_repo, "add", "-A")
        _git(git_repo, "commit", "-qm", "more files")
        for i in range(10):
            (git_repo / f"mod_{i}.sv").write_text("module b; endmodule\n", encoding="utf-8")

        monkeypatch.setenv("BOOLEY_TICKET_TYPE", "bugfix")
        endpoint = SubmitRunReportMcpTool()
        endpoint.parse_args(
            ["--work-dir", str(git_repo), "--summary", "s", "--uncertainties", "u"]
        )
        lines = endpoint._diff_stat_lines()
        assert lines[0] == "worktree diff (git diff --stat):"
        assert len(lines) <= endpoint._DIFF_STAT_MAX_LINES + 2  # header + elision marker
        assert any("more)" in line for line in lines)
        assert "files changed" in lines[-1]  # git's summary survives elision
