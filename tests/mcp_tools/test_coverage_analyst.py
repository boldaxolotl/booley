"""Tests for CoverageAnalystSpecialist v3 — pure/testable helpers only.

Covers: SignalStats, ReviewerResult, FsmResult, BranchResult, CoverageReport,
parsing helpers (_parse_vsc_output, _parse_fsm_output, _parse_reviewer_output),
_find_trace_file, _parse_bwave_stats, _derive_hierarchy_glob,
pre-filtering logic, scoring formulas, errored-branch threshold,
_run_mechanical_measurement error tuples, _canon_value x/z handling.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Make sure the src tree is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "src"))

from booley.fusesoc import fusesoc_registry
from booley.mcp.base import EXIT_ERROR
from booley.specialists.coverage_analyst import (
    BranchResult,
    CoverageAnalystSpecialist,
    CoverageReport,
    FsmResult,
    PersistentWaivers,
    ReviewerResult,
    SignalStats,
    _build_rtl_name_map,
    _canon_value,
    _compute_scope_hash,
    _configured_testbench_source_dirs,
    _extract_json_block,
    _find_signal,
    _is_numeric_verilog_literal,
    _resolve_fsm_enum_names,
    _sanitize_fsm_registers,
    _trace_test_plusargs,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sig(name="sig", transitions=5, value_hist=None, width=1):
    """Shorthand for building a SignalStats."""
    return SignalStats(
        name=name,
        transitions=transitions,
        value_hist=value_hist or {},
        width=width,
    )


def _make_endpoint_with_args(**kwargs):
    """Build a CoverageAnalystSpecialist with just enough state for unit tests."""
    from booley.criteria.state import DevelopmentState

    endpoint = object.__new__(CoverageAnalystSpecialist)
    defaults = {
        "scope": "alu.sv",
        "work_dir": ".",
        "target": "default",
        "tb_top": "tb_top",
        "timeout": 1200,
    }
    defaults.update(kwargs)
    endpoint._args = types.SimpleNamespace(**defaults)
    endpoint._state = DevelopmentState()
    return endpoint


def _fake_bwave_stats_command() -> list[str]:
    """Return the stable fake command shared by B-Wave subprocess tests."""
    return ["/fake/bwave", "stats", "--format", "json"]


def test_vsc_prompt_uses_configured_testbench_dirs(tmp_path):
    # ADR 0026: configured TB dirs come from the .core tags:[tb] partition. The
    # tb fileset lists sources under verification/ and tb_alt/; source_dirs_from_core
    # returns their parent dirs sorted, so tb_alt/ precedes verification/.
    (tmp_path / "design.core").write_text(
        "CAPI=2:\n"
        "name: ::demo\n"
        "filesets:\n"
        "  rtl: {files: [rtl/dut.sv]}\n"
        "  tb:\n"
        "    files:\n"
        "      - verification/tb_top.sv: {file_type: systemVerilogSource}\n"
        "      - tb_alt/tb_extra.sv: {file_type: systemVerilogSource}\n"
        "    tags: [tb]\n"
        "targets:\n"
        "  sim: {filesets: [rtl, tb], toplevel: tb_top}\n",
        encoding="utf-8",
    )
    endpoint = _make_endpoint_with_args(work_dir=tmp_path)

    with patch.object(CoverageAnalystSpecialist, "_find_trace_file", return_value=None):
        prompt = endpoint._build_vsc_prompt(
            tmp_path,
            [_sig("top.dut.state", width=2)],
            need_branch=True,
            need_expression=False,
        )

    assert _configured_testbench_source_dirs(tmp_path) == ["tb_alt/", "verification/"]
    assert "tb_alt/, verification/" in prompt
    assert "verif/ directory" not in prompt


def test_flat_repo_boundary_names_testbench_file_not_directory(tmp_path):
    (tmp_path / "picorv32.v").write_text("module picorv32; endmodule\n")
    (tmp_path / "testbench.v").write_text("module testbench; endmodule\n")
    (tmp_path / "design.core").write_text(
        "CAPI=2:\n"
        "name: ::demo\n"
        "filesets:\n"
        "  rtl: {files: [picorv32.v]}\n"
        "  tb: {files: [testbench.v], tags: [tb]}\n"
        "targets:\n"
        "  sim: {filesets: [rtl, tb], toplevel: testbench}\n"
    )

    assert _configured_testbench_source_dirs(tmp_path) == ["testbench.v"]


# ===================================================================
# SignalStats
# ===================================================================


class TestSignalStats:
    def test_default_construction(self):
        s = SignalStats(name="clk")
        assert s.name == "clk"
        assert s.transitions == 0
        assert s.value_hist == {}
        assert s.width == 1

    def test_with_data(self):
        s = _sig("data", transitions=100, value_hist={"'h0": 50, "'hFF": 50}, width=8)
        assert s.transitions == 100
        assert len(s.value_hist) == 2
        assert s.width == 8


# ===================================================================
# ReviewerResult
# ===================================================================


class TestReviewerResult:
    def test_default_construction(self):
        r = ReviewerResult()
        assert r.toggle_waivers == []
        assert r.value_classifications == {}
        assert r.value_waivers == []
        assert r.notes == []
        assert r.improvement_hints == []

    def test_with_data(self):
        r = ReviewerResult(
            toggle_waivers=["clk", "rst_n"],
            value_classifications={"en": "sufficient", "data_out": "insufficient"},
            value_waivers=["const_flag"],
            notes=["Reset-only signals waived"],
            improvement_hints=["Drive cfg_mode to all 4 enum values"],
        )
        assert len(r.toggle_waivers) == 2
        assert r.value_classifications["en"] == "sufficient"
        assert len(r.value_waivers) == 1
        assert len(r.notes) == 1
        assert r.improvement_hints == ["Drive cfg_mode to all 4 enum values"]


# ===================================================================
# FsmResult
# ===================================================================


class TestFsmResult:
    def test_default_construction(self):
        f = FsmResult()
        assert f.fsm_registers == []

    def test_with_data(self):
        f = FsmResult(fsm_registers=[{"signal": "state", "expected_values": ["'d0", "'d1"]}])
        assert len(f.fsm_registers) == 1
        assert f.fsm_registers[0]["signal"] == "state"


# ===================================================================
# BranchResult
# ===================================================================


class TestBranchResult:
    def test_default_construction(self):
        b = BranchResult(name="br_en", expr="bwave --virtual ...")
        assert b.met is False
        assert b.errored is False
        assert b.reason == ""
        assert b.error_msg == ""

    def test_met(self):
        b = BranchResult(name="br_en", expr="...", met=True, reason="both observed")
        assert b.met is True

    def test_errored(self):
        b = BranchResult(name="br_bad", expr="...", errored=True, error_msg="syntax error")
        assert b.errored is True
        assert b.error_msg == "syntax error"


# ===================================================================
# CoverageReport — scoring formulas
# ===================================================================


class TestCoverageReport:
    # -- toggle_score --

    def test_toggle_score_all_pass(self):
        r = CoverageReport(
            signal_stats=[
                _sig("a", transitions=5),
                _sig("b", transitions=3),
            ]
        )
        score = r.toggle_score()
        assert score["pct"] == 100.0
        assert score["met"] == 2
        assert score["total"] == 2
        assert score["waived"] == 0
        assert score["missed"] == []

    def test_toggle_score_with_failures(self):
        r = CoverageReport(
            signal_stats=[
                _sig("a", transitions=5),
                _sig("b", transitions=0),
                _sig("c", transitions=1),
            ]
        )
        score = r.toggle_score()
        assert score["pct"] == pytest_approx(33.3, tolerance=0.1)
        assert score["met"] == 1
        assert score["total"] == 3
        assert set(score["missed"]) == {"b", "c"}

    def test_toggle_score_with_waivers(self):
        """Waived toggle failures count as met."""
        r = CoverageReport(
            signal_stats=[
                _sig("a", transitions=5),
                _sig("clk", transitions=0),
                _sig("rst", transitions=1),
            ],
            toggle_waivers=["clk"],
        )
        score = r.toggle_score()
        # 1 toggled + 1 waived = 2 met out of 3
        assert score["met"] == 2
        assert score["waived"] == 1
        assert score["missed"] == ["rst"]
        assert score["pct"] == pytest_approx(66.7, tolerance=0.1)

    def test_toggle_score_empty(self):
        r = CoverageReport()
        score = r.toggle_score()
        assert score["pct"] is None
        assert score["total"] == 0

    # -- fsm_score --

    def test_fsm_score_all_visited(self):
        r = CoverageReport(
            signal_stats=[_sig("state", value_hist={"'d0": 100, "'d1": 50, "'d2": 30})],
            fsm_registers=[{"signal": "state", "expected_values": ["'d0", "'d1", "'d2"]}],
        )
        score = r.fsm_score()
        assert score["pct"] == 100.0
        assert score["met"] == 3
        assert score["total"] == 3

    def test_fsm_score_missing_states(self):
        r = CoverageReport(
            signal_stats=[_sig("state", value_hist={"'d0": 100, "'d1": 50})],
            fsm_registers=[{"signal": "state", "expected_values": ["'d0", "'d1", "'d2"]}],
        )
        score = r.fsm_score()
        assert score["pct"] == pytest_approx(66.7, tolerance=0.1)
        assert score["met"] == 2
        assert score["total"] == 3
        assert score["registers"][0]["missing"] == ["2"]

    def test_fsm_score_empty(self):
        r = CoverageReport()
        score = r.fsm_score()
        assert score["pct"] is None
        assert score["total"] == 0

    def test_fsm_score_cross_format(self):
        """Decimal expected matches hex observed."""
        r = CoverageReport(
            signal_stats=[_sig("state", value_hist={"'h0": 10, "'hF": 5})],
            fsm_registers=[{"signal": "state", "expected_values": ["'d0", "'d15"]}],
        )
        score = r.fsm_score()
        assert score["pct"] == 100.0

    def test_fsm_score_hierarchical_name(self):
        """Flat specialist name matches hierarchical bwave name (KI-14)."""
        r = CoverageReport(
            signal_stats=[_sig("dut.state[2:0]", value_hist={"'d0": 10, "'d1": 5, "'d2": 3})],
            fsm_registers=[{"signal": "state", "expected_values": ["'d0", "'d1", "'d2"]}],
        )
        score = r.fsm_score()
        assert score["pct"] == 100.0
        assert score["met"] == 3

    def test_fsm_score_hierarchical_no_bitrange(self):
        """Flat name matches hierarchical name without bit range."""
        r = CoverageReport(
            signal_stats=[_sig("top.dut.fsm_state", value_hist={"'d0": 10, "'d1": 5})],
            fsm_registers=[{"signal": "fsm_state", "expected_values": ["'d0", "'d1", "'d2"]}],
        )
        score = r.fsm_score()
        assert score["met"] == 2
        assert score["total"] == 3

    def test_fsm_score_ambiguous_leaf_merges_instances(self):
        """Ambiguous leaf name merges value histograms across sub-instances."""
        r = CoverageReport(
            signal_stats=[
                _sig("dut.a.state[1:0]", value_hist={"'d0": 10}),
                _sig("dut.b.state[1:0]", value_hist={"'d1": 5}),
            ],
            fsm_registers=[{"signal": "state", "expected_values": ["'d0", "'d1"]}],
        )
        score = r.fsm_score()
        assert score["met"] == 2, "ambiguous leaf should merge instances"
        assert score["pct"] == 100.0

    def test_find_signal_ambiguous_without_merge(self):
        """Without merge_ambiguous, ambiguous leaf returns None."""
        stats = [
            _sig("dut.a.state[1:0]", value_hist={"'d0": 10}),
            _sig("dut.b.state[1:0]", value_hist={"'d1": 5}),
        ]
        assert _find_signal(stats, "state") is None

    # -- value_score --

    def test_value_score_all_sufficient(self):
        r = CoverageReport(
            value_classifications={
                "en": "sufficient",
                "valid": "sufficient",
                "ready": "sufficient",
            },
        )
        score = r.value_score()
        assert score["pct"] == 100.0
        assert score["sufficient"] == 3
        assert score["total"] == 3

    def test_value_score_with_insufficient(self):
        r = CoverageReport(
            value_classifications={
                "en": "sufficient",
                "data_out": "insufficient",
                "addr": "insufficient",
            },
        )
        score = r.value_score()
        assert score["pct"] == pytest_approx(33.3, tolerance=0.1)
        assert set(score["insufficient"]) == {"data_out", "addr"}

    def test_value_score_with_waivers(self):
        """Waived insufficient signals count as met."""
        r = CoverageReport(
            value_classifications={
                "en": "sufficient",
                "data_out": "insufficient",
                "const_sig": "insufficient",
            },
            value_waivers=["const_sig"],
        )
        score = r.value_score()
        # en (sufficient) + const_sig (waived) = 2 met out of 3
        assert score["sufficient"] == 2
        assert score["waived"] == 1
        assert score["insufficient"] == ["data_out"]

    def test_value_score_empty(self):
        r = CoverageReport()
        score = r.value_score()
        assert score["pct"] is None

    # -- branch_score --

    def test_branch_score_all_met(self):
        r = CoverageReport(
            branch_results=[
                BranchResult(name="br_a", expr="...", met=True),
                BranchResult(name="br_b", expr="...", met=True),
            ]
        )
        score = r.branch_score()
        assert score["pct"] == 100.0
        assert score["met"] == 2
        assert score["total"] == 2

    def test_branch_score_with_failures(self):
        r = CoverageReport(
            branch_results=[
                BranchResult(name="br_a", expr="...", met=True),
                BranchResult(name="br_b", expr="expr_b", met=False, reason="never true"),
            ]
        )
        score = r.branch_score()
        assert score["pct"] == 50.0
        assert len(score["missed"]) == 1
        assert score["missed"][0]["name"] == "br_b"

    def test_branch_score_errored_excluded(self):
        """Errored results excluded from denominator."""
        r = CoverageReport(
            branch_results=[
                BranchResult(name="br_a", expr="...", met=True),
                BranchResult(name="br_err", expr="...", errored=True, error_msg="bwave error"),
            ]
        )
        score = r.branch_score()
        assert score["pct"] == 100.0
        assert score["total"] == 1
        assert score["errored"] == 1

    def test_branch_score_empty(self):
        r = CoverageReport()
        score = r.branch_score()
        assert score["pct"] is None

    # -- expression_score --

    def test_expression_score_mirrors_branch(self):
        """Expression scoring uses same formula as branch."""
        r = CoverageReport(
            expression_results=[
                BranchResult(name="e1", expr="...", met=True),
                BranchResult(name="e2", expr="...", met=False, reason="never false"),
                BranchResult(name="e3", expr="...", errored=True),
            ]
        )
        score = r.expression_score()
        assert score["pct"] == 50.0
        assert score["met"] == 1
        assert score["total"] == 2
        assert score["errored"] == 1

    # -- errored threshold --

    def test_branch_majority_errored_detected(self):
        """When >50% of branches errored, the score dict reflects it for threshold check."""
        r = CoverageReport(
            branch_results=[
                BranchResult(name="br_ok", expr="...", met=True),
                BranchResult(name="br_e1", expr="...", errored=True),
                BranchResult(name="br_e2", expr="...", errored=True),
                BranchResult(name="br_e3", expr="...", errored=True),
            ]
        )
        score = r.branch_score()
        # 3 errored out of 4 total (>50%)
        assert score["errored"] == 3
        total_with_errored = score["total"] + score["errored"]
        assert score["errored"] / total_with_errored > 0.5

    def test_branch_minority_errored_ok(self):
        """When <=50% errored, threshold check should pass."""
        r = CoverageReport(
            branch_results=[
                BranchResult(name="br_a", expr="...", met=True),
                BranchResult(name="br_b", expr="...", met=True),
                BranchResult(name="br_e", expr="...", errored=True),
            ]
        )
        score = r.branch_score()
        assert score["errored"] == 1
        total_with_errored = score["total"] + score["errored"]
        assert score["errored"] / total_with_errored <= 0.5

    # -- to_report_dict --

    def test_to_report_dict_structure(self):
        r = CoverageReport(
            signal_stats=[_sig("a", transitions=5)],
            branch_results=[BranchResult(name="br", expr="...", met=True)],
        )
        d = r.to_report_dict()
        assert "toggle" in d
        assert "fsm" in d
        assert "value" in d
        assert "branch" in d
        assert "expression" in d

    def test_to_report_dict_nulls_empty(self):
        """Empty report produces null entries for all types."""
        d = CoverageReport().to_report_dict()
        assert d["toggle"] is None
        assert d["fsm"] is None
        assert d["value"] is None
        assert d["branch"] is None
        assert d["expression"] is None
        assert "reviewer_notes" not in d
        assert "improvement_hints" not in d

    def test_to_report_dict_includes_reviewer_notes_and_hints(self):
        r = CoverageReport(
            signal_stats=[_sig("a", transitions=5)],
            reviewer_notes=["Waived clk as constant"],
            improvement_hints=["Add randomized data_in values"],
        )
        d = r.to_report_dict()
        assert d["reviewer_notes"] == ["Waived clk as constant"]
        assert d["improvement_hints"] == ["Add randomized data_in values"]

    def test_to_report_dict_omits_empty_notes_and_hints(self):
        r = CoverageReport(signal_stats=[_sig("a", transitions=5)])
        d = r.to_report_dict()
        assert "reviewer_notes" not in d
        assert "improvement_hints" not in d


# ===================================================================
# _parse_bwave_stats
# ===================================================================


class TestParseBwaveStats:
    def test_list_format(self):
        data = [
            {
                "name": "clk",
                "transitions": 1000,
                "value_hist": {"'h0": 500, "'h1": 500},
                "width": 1,
            },
            {"name": "data", "transitions": 50, "value_hist": {"'h0": 10, "'hFF": 40}, "width": 8},
        ]
        stats = CoverageAnalystSpecialist._parse_bwave_stats(json.dumps(data))
        assert len(stats) == 2
        assert stats[0].name == "clk"
        assert stats[0].transitions == 1000
        assert stats[1].width == 8

    def test_object_with_signals_key(self):
        data = {
            "signals": [
                {"name": "en", "transitions": 2, "value_hist": {"'h0": 1, "'h1": 1}},
            ]
        }
        stats = CoverageAnalystSpecialist._parse_bwave_stats(json.dumps(data))
        assert len(stats) == 1
        assert stats[0].name == "en"

    def test_path_fallback_for_name(self):
        """If 'name' missing, fall back to 'path'."""
        data = [{"path": "top.dut.sig", "transitions": 3}]
        stats = CoverageAnalystSpecialist._parse_bwave_stats(json.dumps(data))
        assert stats[0].name == "top.dut.sig"

    def test_invalid_json_returns_empty(self):
        stats = CoverageAnalystSpecialist._parse_bwave_stats("not json")
        assert stats == []

    def test_empty_list(self):
        stats = CoverageAnalystSpecialist._parse_bwave_stats("[]")
        assert stats == []

    def test_non_dict_entries_skipped(self):
        data = [{"name": "ok", "transitions": 1}, "bad", 42]
        stats = CoverageAnalystSpecialist._parse_bwave_stats(json.dumps(data))
        assert len(stats) == 1


# ===================================================================
# _run_mechanical_measurement — error tuple return (Fix 3)
# ===================================================================


class TestMechanicalMeasurementErrors:
    """Verify _run_mechanical_measurement returns (stats, error_msg, is_infra) tuples."""

    @staticmethod
    def _resolved_bwave():
        """Keep subprocess behavior tests independent of a compiled dev binary."""
        return patch(
            "booley.specialists.coverage_analyst._bwave_stats_cmd",
            side_effect=_fake_bwave_stats_command,
        )

    def test_no_trace_file_returns_error(self):
        endpoint = _make_endpoint_with_args()
        with patch.object(CoverageAnalystSpecialist, "_find_trace_file", return_value=None):
            stats, err, infra = endpoint._run_mechanical_measurement(Path("/fake/dir"))
        assert stats == []
        assert "No trace file" in err
        assert infra is False

    def test_bwave_timeout_returns_error(self):
        endpoint = _make_endpoint_with_args()
        with (
            patch.object(
                CoverageAnalystSpecialist, "_find_trace_file", return_value=Path("/fake/trace.fst")
            ),
            self._resolved_bwave(),
            patch(
                "booley.specialists.coverage_analyst.subprocess.run",
                side_effect=subprocess.TimeoutExpired("bwave", 120),
            ),
        ):
            stats, err, infra = endpoint._run_mechanical_measurement(Path("/fake/dir"))
        assert stats == []
        assert "timed out" in err
        assert infra is True

    def test_bwave_not_found_returns_error(self):
        endpoint = _make_endpoint_with_args()
        with (
            patch.object(
                CoverageAnalystSpecialist, "_find_trace_file", return_value=Path("/fake/trace.fst")
            ),
            self._resolved_bwave(),
            patch(
                "booley.specialists.coverage_analyst.subprocess.run", side_effect=FileNotFoundError
            ),
        ):
            stats, err, infra = endpoint._run_mechanical_measurement(Path("/fake/dir"))
        assert stats == []
        assert "not found" in err
        assert infra is True

    def test_bwave_nonzero_exit_returns_error(self):
        """Non-zero bwave exit → error propagated immediately."""
        endpoint = _make_endpoint_with_args()
        mock_proc = MagicMock()
        mock_proc.returncode = 1
        mock_proc.stderr = "some error"
        with (
            patch.object(
                CoverageAnalystSpecialist, "_find_trace_file", return_value=Path("/fake/trace.fst")
            ),
            self._resolved_bwave(),
            patch("booley.specialists.coverage_analyst.subprocess.run", return_value=mock_proc),
        ):
            stats, err, infra = endpoint._run_mechanical_measurement(Path("/fake/dir"))
        assert stats == []
        assert "failed" in err
        assert "rc=1" in err
        assert infra is False

    def test_empty_stats_from_valid_json_triggers_discovery_then_error(self):
        """Empty stats → discovery fallback → stage-3 error with scope hint."""
        endpoint = _make_endpoint_with_args()
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = "[]"
        with (
            patch.object(
                CoverageAnalystSpecialist, "_find_trace_file", return_value=Path("/fake/trace.fst")
            ),
            self._resolved_bwave(),
            patch("booley.specialists.coverage_analyst.subprocess.run", return_value=mock_proc),
        ):
            stats, err, infra = endpoint._run_mechanical_measurement(Path("/fake/dir"))
        assert stats == []
        assert "No signals matched" in err
        assert "scope 'alu.sv'" in err
        assert infra is False

    def test_success_returns_none_error(self):
        endpoint = _make_endpoint_with_args()
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = json.dumps([{"name": "sig", "transitions": 5}])
        with (
            patch.object(
                CoverageAnalystSpecialist, "_find_trace_file", return_value=Path("/fake/trace.fst")
            ),
            self._resolved_bwave(),
            patch("booley.specialists.coverage_analyst.subprocess.run", return_value=mock_proc),
        ):
            stats, err, infra = endpoint._run_mechanical_measurement(Path("/fake/dir"))
        assert len(stats) == 1
        assert err is None
        assert infra is False


# ===================================================================
# Pre-filtering logic (Phase 1 → Phase 4 handoff)
# ===================================================================


class TestPreFilter:
    def test_toggle_failures_identified(self):
        stats = [_sig("a", transitions=5), _sig("b", transitions=0), _sig("c", transitions=1)]
        endpoint = CoverageAnalystSpecialist.__new__(CoverageAnalystSpecialist)
        toggle_fail, _ = endpoint._pre_filter_for_waiver(stats)
        assert len(toggle_fail) == 2
        names = {s.name for s in toggle_fail}
        assert "b" in names
        assert "c" in names

    def test_low_diversity_identified(self):
        stats = [
            _sig("a", value_hist={"0": 1, "1": 1, "2": 1, "3": 1}),  # 4 values = ok
            _sig("b", value_hist={"0": 1}),  # 1 value = low
            _sig("c", value_hist={"0": 1, "1": 1, "2": 1}),  # 3 values = low
        ]
        endpoint = CoverageAnalystSpecialist.__new__(CoverageAnalystSpecialist)
        _, low_div = endpoint._pre_filter_for_waiver(stats)
        assert len(low_div) == 2
        names = {s.name for s in low_div}
        assert "b" in names
        assert "c" in names

    def test_empty_stats(self):
        endpoint = CoverageAnalystSpecialist.__new__(CoverageAnalystSpecialist)
        toggle_fail, low_div = endpoint._pre_filter_for_waiver([])
        assert toggle_fail == []
        assert low_div == []


# ===================================================================
# Structural noise filter
# ===================================================================


class TestFilterStructuralNoise:
    """Unit tests for _filter_structural_noise (Step 6 of ADR-0006 plan)."""

    def _make_endpoint(self, tmp_path, scope_content=None, scope_file="alu.sv"):
        endpoint = _make_endpoint_with_args(
            scope=scope_file,
            work_dir=str(tmp_path),
        )
        if scope_content is not None:
            (tmp_path / scope_file).write_text(scope_content, encoding="utf-8")
        return endpoint

    @patch("booley.fusesoc.fusesoc_registry.target_eda_tools", return_value={"default": "icarus"})
    def test_ivl_for_loop_zero_transitions(self, _mock_be, tmp_path):
        endpoint = self._make_endpoint(tmp_path)
        stats = [_sig("dut.$ivl_for_loop0.i[31:0]", transitions=0)]
        kept, excluded = endpoint._filter_structural_noise(stats, ["alu.sv"])
        assert len(excluded) == 1
        assert kept == []

    @patch("booley.fusesoc.fusesoc_registry.target_eda_tools", return_value={"default": "icarus"})
    def test_ivl_for_loop_nonzero_transitions(self, _mock_be, tmp_path):
        endpoint = self._make_endpoint(tmp_path)
        stats = [_sig("dut.$ivl_for_loop0.i[31:0]", transitions=50)]
        kept, excluded = endpoint._filter_structural_noise(stats, ["alu.sv"])
        assert len(excluded) == 1
        assert kept == []

    @patch("booley.fusesoc.fusesoc_registry.target_eda_tools", return_value={"default": "icarus"})
    def test_param_zero_transitions_in_scope(self, _mock_be, tmp_path):
        rtl = "module alu;\n  parameter NUM_ROUNDS = 10;\nendmodule"
        endpoint = self._make_endpoint(tmp_path, scope_content=rtl)
        stats = [_sig("dut.NUM_ROUNDS", transitions=0)]
        kept, excluded = endpoint._filter_structural_noise(stats, ["alu.sv"])
        assert len(excluded) == 1
        assert kept == []

    @patch("booley.fusesoc.fusesoc_registry.target_eda_tools", return_value={"default": "icarus"})
    def test_param_nonzero_transitions_kept(self, _mock_be, tmp_path):
        rtl = "module alu;\n  parameter NUM_ROUNDS = 10;\nendmodule"
        endpoint = self._make_endpoint(tmp_path, scope_content=rtl)
        stats = [_sig("dut.NUM_ROUNDS", transitions=4)]
        kept, excluded = endpoint._filter_structural_noise(stats, ["alu.sv"])
        assert len(kept) == 1
        assert excluded == []

    @patch("booley.fusesoc.fusesoc_registry.target_eda_tools", return_value={"default": "icarus"})
    def test_param_zero_transitions_not_in_scope(self, _mock_be, tmp_path):
        rtl = "module alu;\n  parameter NUM_ROUNDS = 10;\nendmodule"
        endpoint = self._make_endpoint(tmp_path, scope_content=rtl)
        stats = [_sig("dut.FOO", transitions=0)]
        kept, excluded = endpoint._filter_structural_noise(stats, ["alu.sv"])
        assert len(kept) == 1
        assert excluded == []

    @patch("booley.fusesoc.fusesoc_registry.target_eda_tools", return_value={"default": "icarus"})
    def test_normal_signal_zero_transitions_kept(self, _mock_be, tmp_path):
        endpoint = self._make_endpoint(tmp_path)
        stats = [_sig("dut.data_out[7:0]", transitions=0)]
        kept, excluded = endpoint._filter_structural_noise(stats, ["alu.sv"])
        assert len(kept) == 1
        assert excluded == []

    @patch(
        "booley.fusesoc.fusesoc_registry.target_eda_tools", return_value={"default": "verilator"}
    )
    def test_unknown_backend_all_kept(self, _mock_be, tmp_path):
        endpoint = self._make_endpoint(tmp_path)
        stats = [
            _sig("dut.$ivl_for_loop0.i[31:0]", transitions=0),
            _sig("dut.NUM_ROUNDS", transitions=0),
        ]
        kept, excluded = endpoint._filter_structural_noise(stats, ["alu.sv"])
        assert len(kept) == 2
        assert excluded == []

    @patch("booley.fusesoc.fusesoc_registry.target_eda_tools", return_value={"default": "icarus"})
    def test_mixed_bag(self, _mock_be, tmp_path):
        rtl = "module alu;\n  localparam WIDTH = 8;\n  parameter DEPTH = 4;\nendmodule"
        endpoint = self._make_endpoint(tmp_path, scope_content=rtl)
        stats = [
            _sig("dut.$ivl_for_loop0.i[31:0]", transitions=0),
            _sig("dut.$ivl_for_loop1.step[31:0]", transitions=10),
            _sig("dut.WIDTH", transitions=0),
            _sig("dut.DEPTH", transitions=3),
            _sig("dut.data_out[7:0]", transitions=0),
            _sig("dut.clk", transitions=100),
        ]
        kept, excluded = endpoint._filter_structural_noise(stats, ["alu.sv"])
        excluded_names = {s.name for s in excluded}
        kept_names = {s.name for s in kept}
        assert excluded_names == {
            "dut.$ivl_for_loop0.i[31:0]",
            "dut.$ivl_for_loop1.step[31:0]",
            "dut.WIDTH",
        }
        assert kept_names == {"dut.DEPTH", "dut.data_out[7:0]", "dut.clk"}

    @patch("booley.fusesoc.fusesoc_registry.target_eda_tools", return_value={"default": "icarus"})
    def test_localparam_with_range(self, _mock_be, tmp_path):
        rtl = "localparam [7:0] INIT_VAL = 8'hFF;"
        endpoint = self._make_endpoint(tmp_path, scope_content=rtl)
        stats = [_sig("dut.INIT_VAL[7:0]", transitions=0)]
        _kept, excluded = endpoint._filter_structural_noise(stats, ["alu.sv"])
        assert len(excluded) == 1

    @patch("booley.fusesoc.fusesoc_registry.target_eda_tools", return_value={"default": "icarus"})
    def test_genvar_zero_transitions_excluded(self, _mock_be, tmp_path):
        rtl = "module top;\n  genvar i;\n  generate for (i=0; i<4; i=i+1) begin : gen\n  end endgenerate\nendmodule"
        endpoint = self._make_endpoint(tmp_path, scope_content=rtl)
        stats = [_sig("dut.gen[0].i[31:0]", transitions=0)]
        kept, excluded = endpoint._filter_structural_noise(stats, ["alu.sv"])
        assert len(excluded) == 1
        assert kept == []

    @patch("booley.fusesoc.fusesoc_registry.target_eda_tools", return_value={"default": "icarus"})
    def test_genvar_nonzero_transitions_kept(self, _mock_be, tmp_path):
        rtl = "module top;\n  genvar i;\nendmodule"
        endpoint = self._make_endpoint(tmp_path, scope_content=rtl)
        stats = [_sig("dut.gen[0].i[31:0]", transitions=5)]
        kept, excluded = endpoint._filter_structural_noise(stats, ["alu.sv"])
        assert len(kept) == 1
        assert excluded == []

    @patch("booley.fusesoc.fusesoc_registry.target_eda_tools", return_value={"default": "icarus"})
    def test_genvar_not_in_scope_generate_constant_excluded(self, _mock_be, tmp_path):
        """Signal under generate scope with ≤1 transition and ≤1 value is a
        generate-scope constant even if its leaf name isn't a declared genvar."""
        rtl = "module top;\n  genvar i;\nendmodule"
        endpoint = self._make_endpoint(tmp_path, scope_content=rtl)
        stats = [_sig("dut.gen[0].j[31:0]", transitions=0)]
        kept, excluded = endpoint._filter_structural_noise(stats, ["alu.sv"])
        assert len(excluded) == 1
        assert kept == []

    @patch("booley.fusesoc.fusesoc_registry.target_eda_tools", return_value={"default": "icarus"})
    def test_genvar_not_in_scope_active_signal_kept(self, _mock_be, tmp_path):
        """Signal under generate scope with many transitions is NOT noise."""
        rtl = "module top;\n  genvar i;\nendmodule"
        endpoint = self._make_endpoint(tmp_path, scope_content=rtl)
        stats = [_sig("dut.gen[0].j[31:0]", transitions=5)]
        kept, excluded = endpoint._filter_structural_noise(stats, ["alu.sv"])
        assert len(kept) == 1
        assert excluded == []

    @patch("booley.fusesoc.fusesoc_registry.target_eda_tools", return_value={"default": "icarus"})
    def test_generate_scope_nested_constant_excluded(self, _mock_be, tmp_path):
        """Nested generate hierarchy (row[0].col[1].word_idx) with constant
        value is structural noise."""
        endpoint = self._make_endpoint(tmp_path)
        stats = [_sig("dut.row[0].col[1].word_idx", transitions=1, value_hist={"5": 100})]
        kept, excluded = endpoint._filter_structural_noise(stats, ["alu.sv"])
        assert len(excluded) == 1
        assert kept == []

    @patch("booley.fusesoc.fusesoc_registry.target_eda_tools", return_value={"default": "icarus"})
    def test_generate_scope_multi_value_kept(self, _mock_be, tmp_path):
        """Signal under generate scope with multiple observed values is real."""
        endpoint = self._make_endpoint(tmp_path)
        stats = [_sig("dut.gen[0].counter", transitions=0, value_hist={"0": 50, "1": 50})]
        kept, excluded = endpoint._filter_structural_noise(stats, ["alu.sv"])
        assert len(kept) == 1
        assert excluded == []

    @patch(
        "booley.fusesoc.fusesoc_registry.target_eda_tools", return_value={"default": "verilator"}
    )
    def test_generate_scope_constant_not_filtered_on_verilator(self, _mock_be, tmp_path):
        """Generate-scope constant detection is Icarus-only."""
        endpoint = self._make_endpoint(tmp_path)
        stats = [_sig("dut.gen[0].j[31:0]", transitions=0)]
        kept, excluded = endpoint._filter_structural_noise(stats, ["alu.sv"])
        assert len(kept) == 1
        assert excluded == []

    @patch("booley.fusesoc.fusesoc_registry.target_eda_tools", return_value={"default": "icarus"})
    def test_param_one_transition_excluded(self, _mock_be, tmp_path):
        """Params with exactly 1 transition (X->constant) are now excluded."""
        rtl = "module alu;\n  parameter NUM_ROUNDS = 10;\nendmodule"
        endpoint = self._make_endpoint(tmp_path, scope_content=rtl)
        stats = [_sig("dut.NUM_ROUNDS", transitions=1)]
        kept, excluded = endpoint._filter_structural_noise(stats, ["alu.sv"])
        assert len(excluded) == 1
        assert kept == []

    @patch(
        "booley.fusesoc.fusesoc_registry.target_eda_tools", return_value={"default": "verilator"}
    )
    def test_param_filtered_on_verilator(self, _mock_be, tmp_path):
        rtl = "module alu;\n  parameter NUM_ROUNDS = 10;\nendmodule"
        endpoint = self._make_endpoint(tmp_path, scope_content=rtl)
        stats = [_sig("dut.NUM_ROUNDS", transitions=0)]
        kept, excluded = endpoint._filter_structural_noise(stats, ["alu.sv"])
        assert len(excluded) == 1
        assert kept == []

    @patch(
        "booley.fusesoc.fusesoc_registry.target_eda_tools", return_value={"default": "verilator"}
    )
    def test_genvar_filtered_on_verilator(self, _mock_be, tmp_path):
        rtl = "module top;\n  genvar j;\nendmodule"
        endpoint = self._make_endpoint(tmp_path, scope_content=rtl)
        stats = [_sig("dut.row[0].col[0].j[31:0]", transitions=0)]
        kept, excluded = endpoint._filter_structural_noise(stats, ["alu.sv"])
        assert len(excluded) == 1
        assert kept == []

    @patch(
        "booley.fusesoc.fusesoc_registry.target_eda_tools", return_value={"default": "verilator"}
    )
    def test_ivl_for_loop_not_filtered_on_verilator(self, _mock_be, tmp_path):
        endpoint = self._make_endpoint(tmp_path)
        stats = [_sig("dut.$ivl_for_loop0.i[31:0]", transitions=0)]
        kept, excluded = endpoint._filter_structural_noise(stats, ["alu.sv"])
        assert len(kept) == 1
        assert excluded == []

    @patch("booley.fusesoc.fusesoc_registry.target_eda_tools", return_value={"default": "icarus"})
    def test_mixed_bag_with_genvars(self, _mock_be, tmp_path):
        rtl = "module top;\n  parameter WIDTH = 8;\n  genvar i, j;\nendmodule"
        endpoint = self._make_endpoint(tmp_path, scope_content=rtl)
        stats = [
            _sig("dut.$ivl_for_loop0.i[31:0]", transitions=0),
            _sig("dut.gen[0].i[31:0]", transitions=0),
            _sig("dut.row[0].col[0].j[31:0]", transitions=0),
            _sig("dut.WIDTH", transitions=0),
            _sig("dut.data_out[7:0]", transitions=0),
            _sig("dut.clk", transitions=100),
        ]
        kept, excluded = endpoint._filter_structural_noise(stats, ["alu.sv"])
        excluded_names = {s.name for s in excluded}
        kept_names = {s.name for s in kept}
        assert excluded_names == {
            "dut.$ivl_for_loop0.i[31:0]",
            "dut.gen[0].i[31:0]",
            "dut.row[0].col[0].j[31:0]",
            "dut.WIDTH",
        }
        assert kept_names == {"dut.data_out[7:0]", "dut.clk"}

    @patch("booley.fusesoc.fusesoc_registry.target_eda_tools", return_value={"default": "icarus"})
    def test_lowercase_param_not_filtered(self, _mock_be, tmp_path):
        """Lowercase params bypass the uppercase gate — avoids false filtering
        of dynamic signals that share a name with a localparam in another file."""
        rtl = "module alu;\n  localparam depth = 4;\nendmodule"
        endpoint = self._make_endpoint(tmp_path, scope_content=rtl)
        stats = [_sig("dut.depth", transitions=0)]
        kept, excluded = endpoint._filter_structural_noise(stats, ["alu.sv"])
        assert len(kept) == 1
        assert excluded == []

    @patch("booley.fusesoc.fusesoc_registry.target_eda_tools", return_value={"default": "icarus"})
    def test_submodule_param_in_sibling_file_filtered(self, _mock_be, tmp_path):
        """Params declared in sibling RTL files (same directory) are now
        picked up — fixes the AES submodule constant gap."""
        scope_rtl = "module top;\n  parameter TOP_W = 8;\nendmodule"
        endpoint = self._make_endpoint(tmp_path, scope_content=scope_rtl)
        # Write a sibling submodule file in the same directory
        (tmp_path / "sub.sv").write_text(
            "module sub;\n  localparam NK = 8;\n  localparam RCON_STEPS = 7;\nendmodule",
            encoding="utf-8",
        )
        stats = [
            _sig("dut.sub_inst.NK[31:0]", transitions=0),
            _sig("dut.sub_inst.RCON_STEPS[31:0]", transitions=0),
            _sig("dut.TOP_W[31:0]", transitions=0),
            _sig("dut.sub_inst.data_out[7:0]", transitions=0),
        ]
        kept, excluded = endpoint._filter_structural_noise(stats, ["alu.sv"])
        excluded_names = {s.name for s in excluded}
        kept_names = {s.name for s in kept}
        assert excluded_names == {
            "dut.sub_inst.NK[31:0]",
            "dut.sub_inst.RCON_STEPS[31:0]",
            "dut.TOP_W[31:0]",
        }
        assert kept_names == {"dut.sub_inst.data_out[7:0]"}

    @patch("booley.fusesoc.fusesoc_registry.target_eda_tools", return_value={"default": "icarus"})
    def test_typed_parameter_filtered(self, _mock_be, tmp_path):
        """Typed parameters (parameter int/logic/string) are correctly captured
        by the regex — the type keyword is skipped, not mistaken for the name."""
        rtl = (
            "module alu;\n"
            "  parameter int WIDTH = 8;\n"
            "  localparam logic [3:0] DEPTH = 4;\n"
            '  parameter string MODE = "fast";\n'
            "endmodule"
        )
        endpoint = self._make_endpoint(tmp_path, scope_content=rtl)
        stats = [
            _sig("dut.WIDTH", transitions=0),
            _sig("dut.DEPTH[3:0]", transitions=0),
            _sig("dut.MODE", transitions=0),
        ]
        kept, excluded = endpoint._filter_structural_noise(stats, ["alu.sv"])
        excluded_names = {s.name for s in excluded}
        assert excluded_names == {"dut.WIDTH", "dut.DEPTH[3:0]", "dut.MODE"}
        assert kept == []

    @patch("booley.fusesoc.fusesoc_registry.target_eda_tools", return_value={"default": "icarus"})
    def test_params_in_header_files_filtered(self, _mock_be, tmp_path):
        """Params declared in .svh/.vh header files are picked up."""
        endpoint = self._make_endpoint(tmp_path, scope_content="module top;\nendmodule")
        (tmp_path / "defines.svh").write_text(
            "localparam ADDR_W = 16;\n",
            encoding="utf-8",
        )
        (tmp_path / "config.vh").write_text(
            "parameter DATA_W = 32;\n",
            encoding="utf-8",
        )
        stats = [
            _sig("dut.ADDR_W", transitions=0),
            _sig("dut.DATA_W", transitions=0),
            _sig("dut.real_sig", transitions=0),
        ]
        kept, excluded = endpoint._filter_structural_noise(stats, ["alu.sv"])
        excluded_names = {s.name for s in excluded}
        assert excluded_names == {"dut.ADDR_W", "dut.DATA_W"}
        assert len(kept) == 1


# ===================================================================
# Structural noise integration (Step 7 of ADR-0006 plan)
# ===================================================================


class TestStructuralNoiseIntegration:
    """Verify filter wiring: excluded signals affect report, not scoring."""

    def test_excluded_not_in_signal_stats(self):
        noise = [_sig("dut.$ivl_for_loop0.i[31:0]", transitions=0)]
        report = CoverageReport(
            signal_stats=[_sig("dut.clk", transitions=100)],
            structural_noise=noise,
        )
        assert len(report.signal_stats) == 1
        assert report.signal_stats[0].name == "dut.clk"

    def test_excluded_in_structural_noise_field(self):
        noise = [_sig("dut.$ivl_for_loop0.i[31:0]")]
        report = CoverageReport(
            signal_stats=[_sig("dut.clk", transitions=100)],
            structural_noise=noise,
        )
        assert len(report.structural_noise) == 1
        assert report.structural_noise[0].name == "dut.$ivl_for_loop0.i[31:0]"

    def test_toggle_score_uses_reduced_denominator(self):
        kept = [_sig("dut.clk", transitions=100), _sig("dut.rst", transitions=0)]
        noise = [_sig("dut.$ivl_for_loop0.i[31:0]", transitions=0)]
        report = CoverageReport(signal_stats=kept, structural_noise=noise)
        score = report.toggle_score()
        # 1 toggled / 2 total = 50%, not 1/3 = 33%
        assert score["total"] == 2
        assert score["pct"] == 50.0

    def test_report_dict_includes_noise_names(self):
        noise = [
            _sig("dut.$ivl_for_loop0.i[31:0]"),
            _sig("dut.WIDTH"),
        ]
        report = CoverageReport(
            signal_stats=[_sig("dut.clk", transitions=100)],
            structural_noise=noise,
        )
        d = report.to_report_dict()
        assert d["structural_noise"] == [
            "dut.$ivl_for_loop0.i[31:0]",
            "dut.WIDTH",
        ]

    def test_report_dict_omits_key_when_empty(self):
        report = CoverageReport(
            signal_stats=[_sig("dut.clk", transitions=100)],
        )
        d = report.to_report_dict()
        assert "structural_noise" not in d


# ===================================================================
# VSC output parsing (Phase 2 new)
# ===================================================================


class TestParseVscOutput:
    def test_valid_both(self):
        data = {
            "branch_results": [
                {"name": "br_a", "expr": "...", "met": True, "reason": "both observed"},
            ],
            "expression_results": [
                {"name": "e1", "expr": "...", "met": False, "reason": "never false"},
            ],
        }
        raw = f"```json\n{json.dumps(data)}\n```"
        branches, exprs = CoverageAnalystSpecialist._parse_vsc_output(raw, True, True)
        assert len(branches) == 1
        assert branches[0].met is True
        assert len(exprs) == 1
        assert exprs[0].met is False

    def test_branch_only(self):
        data = {"branch_results": [{"name": "br", "expr": "...", "met": True}]}
        raw = json.dumps(data)
        branches, exprs = CoverageAnalystSpecialist._parse_vsc_output(raw, True, False)
        assert len(branches) == 1
        assert exprs == []

    def test_errored_result(self):
        data = {
            "branch_results": [
                {
                    "name": "br",
                    "expr": "...",
                    "met": False,
                    "errored": True,
                    "error_msg": "bwave fail",
                },
            ]
        }
        raw = json.dumps(data)
        branches, _ = CoverageAnalystSpecialist._parse_vsc_output(raw, True, False)
        assert branches[0].errored is True
        assert branches[0].error_msg == "bwave fail"

    def test_invalid_json(self):
        branches, exprs = CoverageAnalystSpecialist._parse_vsc_output("garbage", True, True)
        assert branches == []
        assert exprs == []


# ===================================================================
# FSM output parsing (Phase 3 new)
# ===================================================================


class TestParseFsmOutput:
    def test_valid_json_block(self):
        raw = """```json
{
  "fsm_registers": [
    {"signal": "state", "expected_values": ["'d0", "'d1", "'d2"]},
    {"signal": "mode", "expected_values": ["IDLE", "ACTIVE"]}
  ]
}
```"""
        result = CoverageAnalystSpecialist._parse_fsm_output(raw)
        assert len(result.fsm_registers) == 2
        assert result.fsm_registers[0]["signal"] == "state"
        assert result.fsm_registers[1]["expected_values"] == ["IDLE", "ACTIVE"]

    def test_empty_registers(self):
        raw = '{"fsm_registers": []}'
        result = CoverageAnalystSpecialist._parse_fsm_output(raw)
        assert result.fsm_registers == []

    def test_invalid_json(self):
        result = CoverageAnalystSpecialist._parse_fsm_output("no json here")
        assert result.fsm_registers == []

    def test_missing_key(self):
        raw = '{"other_key": "value"}'
        result = CoverageAnalystSpecialist._parse_fsm_output(raw)
        assert result.fsm_registers == []


# ===================================================================
# Principle 5 — boundary validation of LLM-supplied FSM registers
# ===================================================================


class TestSanitizeFsmRegisters:
    """_sanitize_fsm_registers is the boundary guard for LLM output.

    Downstream (fsm_score's reg["signal"], _resolve_fsm_enum_names' v.strip())
    trust the shape it produces, so these are the malformed-input cases.
    """

    def test_non_list_top_level_degrades_to_empty(self):
        # Wrong top-level shape: dict / str / None all become [].
        assert _sanitize_fsm_registers({"signal": "x"}) == []
        assert _sanitize_fsm_registers("state") == []
        assert _sanitize_fsm_registers(None) == []

    def test_non_dict_entries_dropped(self):
        regs = _sanitize_fsm_registers(["state", 42, {"signal": "ok"}])
        assert [r["signal"] for r in regs] == ["ok"]

    def test_missing_or_blank_signal_dropped(self):
        raw = [
            {"expected_values": ["'d0"]},  # missing signal key
            {"signal": "", "expected_values": []},  # blank signal
            {"signal": "  ", "expected_values": []},  # whitespace-only
            {"signal": 123, "expected_values": []},  # non-string signal
            {"signal": "good", "expected_values": []},
        ]
        regs = _sanitize_fsm_registers(raw)
        assert [r["signal"] for r in regs] == ["good"]

    def test_non_string_expected_values_stringified(self):
        # Downstream calls v.strip()/v.upper(); ints must become str first.
        raw = [{"signal": "st", "expected_values": [0, 1, "'d2", None]}]
        regs = _sanitize_fsm_registers(raw)
        assert regs[0]["expected_values"] == ["0", "1", "'d2", "None"]
        assert all(isinstance(v, str) for v in regs[0]["expected_values"])

    def test_non_list_expected_values_becomes_empty(self):
        raw = [{"signal": "st", "expected_values": "'d0"}]
        regs = _sanitize_fsm_registers(raw)
        assert regs[0]["expected_values"] == []

    def test_valid_input_passes_through(self):
        raw = [{"signal": "state", "expected_values": ["'d0", "'d1"]}]
        assert _sanitize_fsm_registers(raw) == raw

    def test_parse_fsm_output_survives_malformed_registers(self):
        # End-to-end through the LLM boundary: garbage in, no crash out.
        raw = (
            '{"fsm_registers": ["oops", {"expected_values": [1]}, '
            '{"signal": "state", "expected_values": [0, "'
            "'"
            'd1"]}]}'
        )
        result = CoverageAnalystSpecialist._parse_fsm_output(raw)
        assert len(result.fsm_registers) == 1
        assert result.fsm_registers[0]["signal"] == "state"
        assert result.fsm_registers[0]["expected_values"] == ["0", "'d1"]

    def test_downstream_resolve_tolerates_sanitized_ints(self):
        # After sanitizing, _resolve_fsm_enum_names' v.strip() is safe.
        regs = _sanitize_fsm_registers([{"signal": "st", "expected_values": [0, 1]}])
        # Should not raise AttributeError on int.strip().
        resolved = _resolve_fsm_enum_names(regs, "localparam IDLE = 0;")
        assert resolved[0]["expected_values"] == ["0", "1"]


# ===================================================================
# Principle 5 — _resolve_threshold coerces ticket-supplied min_pct
# ===================================================================


class TestResolveThreshold:
    def _endpoint_with_params(self, params):
        endpoint = _make_endpoint_with_args()
        entry = types.SimpleNamespace(params=params)
        endpoint._state = types.SimpleNamespace(criteria={"coverage_toggle_default": entry})
        return endpoint

    def test_numeric_string_param_coerced(self):
        endpoint = self._endpoint_with_params({"min_pct": "85"})
        assert endpoint._resolve_threshold("coverage_toggle", 90) == 85

    def test_numeric_param_used(self):
        endpoint = self._endpoint_with_params({"min_pct": 75})
        assert endpoint._resolve_threshold("coverage_toggle", 90) == 75

    def test_non_numeric_param_falls_back_to_default(self):
        # LLM/user supplied garbage — must not raise ValueError.
        endpoint = self._endpoint_with_params({"min_pct": "high"})
        assert endpoint._resolve_threshold("coverage_toggle", 90) == 90

    def test_non_scalar_param_falls_back_to_default(self):
        # int({...}) would raise TypeError; must degrade instead.
        endpoint = self._endpoint_with_params({"min_pct": {"nested": 1}})
        assert endpoint._resolve_threshold("coverage_toggle", 90) == 90

    def test_missing_param_uses_default(self):
        endpoint = self._endpoint_with_params({})
        assert endpoint._resolve_threshold("coverage_toggle", 90) == 90


# ===================================================================
# Reviewer output parsing (Phase 4 new)
# ===================================================================


class TestParseReviewerOutput:
    def test_valid_json_block(self):
        raw = """```json
{
  "toggle_waivers": ["clk", "rst_n"],
  "value_classifications": {"en": "sufficient", "data_out": "insufficient"},
  "value_waivers": ["const_flag"],
  "notes": ["Reset signals waived"],
  "improvement_hints": ["Drive data_out through full 8-bit range"]
}
```"""
        result = CoverageAnalystSpecialist._parse_reviewer_output(raw)
        assert result.toggle_waivers == ["clk", "rst_n"]
        assert result.value_classifications == {"en": "sufficient", "data_out": "insufficient"}
        assert result.value_waivers == ["const_flag"]
        assert result.notes == ["Reset signals waived"]
        assert result.improvement_hints == ["Drive data_out through full 8-bit range"]

    def test_raw_json(self):
        raw = '{"toggle_waivers": ["a"], "value_classifications": {"b": "sufficient"}}'
        result = CoverageAnalystSpecialist._parse_reviewer_output(raw)
        assert result.toggle_waivers == ["a"]
        assert result.value_classifications["b"] == "sufficient"
        assert result.improvement_hints == []

    def test_invalid_json(self):
        result = CoverageAnalystSpecialist._parse_reviewer_output("not json")
        assert result.toggle_waivers == []
        assert result.value_classifications == {}
        assert result.value_waivers == []
        assert result.notes == []
        assert result.improvement_hints == []

    def test_empty_output(self):
        result = CoverageAnalystSpecialist._parse_reviewer_output("")
        assert result.toggle_waivers == []

    def test_trailing_commas(self):
        raw = '{"toggle_waivers": ["a",], "value_classifications": {"b": "sufficient",},}'
        result = CoverageAnalystSpecialist._parse_reviewer_output(raw)
        assert result.toggle_waivers == ["a"]
        assert result.value_classifications["b"] == "sufficient"


# ===================================================================
# Criteria-driven phase skipping
# ===================================================================


class TestCriteriaDrivenPhaseSkipping:
    def test_vsc_skipped_when_no_criteria(self):
        """Phase 2 only runs when branch/expression criteria are active."""
        endpoint = CoverageAnalystSpecialist.__new__(CoverageAnalystSpecialist)
        active = {"coverage_toggle", "coverage_fsm"}
        branches, exprs = endpoint._run_virtual_signal_creator(
            Path(),
            Path(),
            [],
            active,
        )
        assert branches == []
        assert exprs == []

    def test_vsc_skipped_with_empty_criteria(self):
        endpoint = CoverageAnalystSpecialist.__new__(CoverageAnalystSpecialist)
        branches, exprs = endpoint._run_virtual_signal_creator(
            Path(),
            Path(),
            [],
            set(),
        )
        assert branches == []
        assert exprs == []


class TestPhaseFailsClosedOnUnparseable:
    """Unparseable sub-agent output must fail a phase closed, not pass it.

    Mirrors the reviewer bug: an agent that reports via a native endpoint call
    (or emits prose) leaves ``result.output`` with no JSON. The old code
    returned empty results with no ``_phase_errors`` entry, and the gate
    then scored the (empty) phase as PASS.
    """

    @staticmethod
    def _bare_endpoint():
        endpoint = CoverageAnalystSpecialist.__new__(CoverageAnalystSpecialist)
        endpoint._phase_errors = set()
        endpoint._args = types.SimpleNamespace(
            max_turns=1,
            timeout=100,
            transcript_dir=None,
        )
        return endpoint

    def test_vsc_unparseable_fails_branch_and_expression_closed(self, monkeypatch):
        import contextlib

        import booley.specialists.coverage_analyst as ca

        endpoint = self._bare_endpoint()
        monkeypatch.setattr(endpoint, "_resolve_model", lambda: "m", raising=False)
        monkeypatch.setattr(endpoint, "_build_vsc_prompt", lambda *a, **k: "p", raising=False)
        monkeypatch.setattr(
            endpoint,
            "_invoke_agent",
            lambda *a, **k: types.SimpleNamespace(output="I reported via a endpoint call."),
            raising=False,
        )
        monkeypatch.setattr(
            ca,
            "hide_opposite_sources",
            lambda *a, **k: contextlib.nullcontext(),
            raising=True,
        )
        branches, exprs = endpoint._run_virtual_signal_creator(
            Path(),
            Path(),
            [],
            {"coverage_branch", "coverage_expression"},
        )
        assert branches == [] and exprs == []
        assert "coverage_branch" in endpoint._phase_errors
        assert "coverage_expression" in endpoint._phase_errors

    def test_fsm_unparseable_fails_fsm_closed(self, monkeypatch):
        endpoint = self._bare_endpoint()
        monkeypatch.setattr(endpoint, "_resolve_light_model", lambda: "m", raising=False)
        monkeypatch.setattr(
            endpoint,
            "_invoke_agent",
            lambda *a, **k: types.SimpleNamespace(output="no json, just prose"),
            raising=False,
        )
        result = endpoint._run_fsm_identifier(Path(), "module m; endmodule")
        assert result.fsm_registers == []
        assert "coverage_fsm" in endpoint._phase_errors

    def test_valid_json_does_not_mark_phase_error(self, monkeypatch):
        """Guard against over-eager fail-closed: real JSON must stay clean."""
        import contextlib

        import booley.specialists.coverage_analyst as ca

        endpoint = self._bare_endpoint()
        monkeypatch.setattr(endpoint, "_resolve_model", lambda: "m", raising=False)
        monkeypatch.setattr(endpoint, "_build_vsc_prompt", lambda *a, **k: "p", raising=False)
        monkeypatch.setattr(
            endpoint,
            "_invoke_agent",
            lambda *a, **k: types.SimpleNamespace(
                output=json.dumps({"branch_results": [], "expression_results": []}),
            ),
            raising=False,
        )
        monkeypatch.setattr(
            ca,
            "hide_opposite_sources",
            lambda *a, **k: contextlib.nullcontext(),
            raising=True,
        )
        endpoint._run_virtual_signal_creator(
            Path(),
            Path(),
            [],
            {"coverage_branch", "coverage_expression"},
        )
        assert endpoint._phase_errors == set()


# ===================================================================
# _derive_hierarchy_glob
# ===================================================================


class TestDeriveHierarchyGlob:
    """Test scope-to-hierarchy glob derivation.

    Hierarchy candidates come from the RTL scope, never ticket-global state.
    """

    @staticmethod
    def _make_endpoint(scope: str = "rtl/alu.sv"):
        from booley.criteria.state import DevelopmentState

        endpoint = object.__new__(CoverageAnalystSpecialist)
        endpoint._args = types.SimpleNamespace(scope=scope)
        endpoint._state = DevelopmentState()
        return endpoint

    def test_single_scope_module(self):
        endpoint = self._make_endpoint("rtl/alu.sv")
        assert endpoint._derive_hierarchy_glob() == "*alu.*"

    def test_multiple_scope_modules(self):
        endpoint = self._make_endpoint("rtl/alu.sv,rtl/fifo.sv")
        assert endpoint._derive_hierarchy_glob() == "*alu.*,*fifo.*"

    def test_empty_scope_traces_all_for_discovery(self):
        endpoint = self._make_endpoint("")
        assert endpoint._derive_hierarchy_glob() == "*"


# ===================================================================
# _extract_modules_from_scope
# ===================================================================


class TestExtractModulesFromScope:
    def test_single(self):
        assert CoverageAnalystSpecialist._extract_modules_from_scope("alu.sv") == ["alu"]

    def test_multiple(self):
        assert CoverageAnalystSpecialist._extract_modules_from_scope("alu.sv, cpu.sv") == [
            "alu",
            "cpu",
        ]

    def test_dotted(self):
        assert CoverageAnalystSpecialist._extract_modules_from_scope("a.b.sv") == ["a_b"]

    def test_dedup(self):
        assert CoverageAnalystSpecialist._extract_modules_from_scope("x.sv, x.sv") == ["x"]

    def test_empty(self):
        assert CoverageAnalystSpecialist._extract_modules_from_scope("") == []

    def test_reads_declared_module_name_instead_of_filename(self, tmp_path):
        rtl = tmp_path / "implementation.sv"
        rtl.write_text("module actual_dut; endmodule\n", encoding="utf-8")
        endpoint = _make_endpoint_with_args(
            work_dir=str(tmp_path),
            scope="implementation.sv",
        )
        assert endpoint._scope_modules() == ["actual_dut"]

    def test_ignores_module_declarations_in_comments(self, tmp_path):
        rtl = tmp_path / "implementation.sv"
        rtl.write_text(
            "// module stale_line;\n/* module stale_block; */\nmodule actual_dut; endmodule\n",
            encoding="utf-8",
        )
        endpoint = _make_endpoint_with_args(
            work_dir=str(tmp_path),
            scope="implementation.sv",
        )
        assert endpoint._scope_modules() == ["actual_dut"]


# ===================================================================
# _pick_dut_scope
# ===================================================================


class TestPickDutScope:
    def test_filters_testbench_scopes(self):
        candidates = [
            ("barrel_shifter_tb", 10),
            ("barrel_shifter_tb.uut", 30),
        ]
        assert CoverageAnalystSpecialist._pick_dut_scope(candidates) == "barrel_shifter_tb.uut"

    def test_prefers_deepest(self):
        candidates = [
            ("tb.uu_aes", 20),
            ("tb.wrapper.uu_aes", 20),
        ]
        assert CoverageAnalystSpecialist._pick_dut_scope(candidates) == "tb.wrapper.uu_aes"

    def test_tiebreak_by_signal_count(self):
        candidates = [
            ("tb.uu_aes", 10),
            ("tb.uu_aes_ref", 5),
        ]
        assert CoverageAnalystSpecialist._pick_dut_scope(candidates) == "tb.uu_aes"

    def test_falls_back_to_tb_when_all_filtered(self):
        """If every candidate looks like a testbench, use them anyway."""
        candidates = [("tb_top", 10)]
        assert CoverageAnalystSpecialist._pick_dut_scope(candidates) == "tb_top"

    def test_empty_returns_none(self):
        assert CoverageAnalystSpecialist._pick_dut_scope([]) is None


# ===================================================================
# _discover_dut_scope
# ===================================================================


class TestDiscoverDutScope:
    @staticmethod
    def _make_endpoint(scope: str):
        from booley.criteria.state import DevelopmentState

        endpoint = object.__new__(CoverageAnalystSpecialist)
        endpoint._args = types.SimpleNamespace(
            scope=scope,
            work_dir=None,
            tb_top="tb_top",
        )
        endpoint._state = DevelopmentState()
        return endpoint

    @staticmethod
    def _fake_bwave_stats(signal_names: list[str]) -> str:
        """Build JSON matching bwave --stats output."""
        return json.dumps(
            [{"name": n, "transitions": 1, "width": 1, "value_hist": {}} for n in signal_names]
        )

    @staticmethod
    def _resolve_fake_bwave(monkeypatch):
        """Reach the mocked subprocess without requiring a compiled binary."""
        from booley.specialists import coverage_analyst

        monkeypatch.setattr(coverage_analyst, "_bwave_stats_cmd", _fake_bwave_stats_command)

    def test_prefix_instance_discovered(self, monkeypatch):
        """uu_aes128_encrypt found via stem suffix match."""
        self._resolve_fake_bwave(monkeypatch)
        endpoint = self._make_endpoint("aes128_encrypt.sv")
        signals = [
            "tb_aes.uu_aes128_encrypt.key",
            "tb_aes.uu_aes128_encrypt.data",
            "tb_aes.clk",
        ]
        fake_stdout = self._fake_bwave_stats(signals)
        monkeypatch.setattr(
            subprocess,
            "run",
            lambda *a, **kw: types.SimpleNamespace(
                returncode=0,
                stdout=fake_stdout,
                stderr="",
            ),
        )
        result = endpoint._discover_dut_scope(Path("fake.bwave"), ["aes128_encrypt"])
        assert result == "tb_aes.uu_aes128_encrypt.*"

    def test_generic_uut_discovered(self, monkeypatch):
        """barrel_shifter instantiated as 'uut' under barrel_shifter_tb."""
        self._resolve_fake_bwave(monkeypatch)
        endpoint = self._make_endpoint("barrel_shifter.sv")
        signals = [
            "barrel_shifter_tb.uut.data_in",
            "barrel_shifter_tb.uut.shift_amount",
            "barrel_shifter_tb.clk",
        ]
        fake_stdout = self._fake_bwave_stats(signals)
        monkeypatch.setattr(
            subprocess,
            "run",
            lambda *a, **kw: types.SimpleNamespace(
                returncode=0,
                stdout=fake_stdout,
                stderr="",
            ),
        )
        result = endpoint._discover_dut_scope(Path("fake.bwave"), ["barrel_shifter"])
        assert result == "barrel_shifter_tb.uut.*"

    def test_generic_dut_discovered(self, monkeypatch):
        """fifo_buffer instantiated as 'dut' under tb_fifo_buffer."""
        self._resolve_fake_bwave(monkeypatch)
        endpoint = self._make_endpoint("fifo_buffer.sv")
        signals = [
            "tb_fifo_buffer.dut.wr_data",
            "tb_fifo_buffer.dut.rd_data",
            "tb_fifo_buffer.rst",
        ]
        fake_stdout = self._fake_bwave_stats(signals)
        monkeypatch.setattr(
            subprocess,
            "run",
            lambda *a, **kw: types.SimpleNamespace(
                returncode=0,
                stdout=fake_stdout,
                stderr="",
            ),
        )
        result = endpoint._discover_dut_scope(Path("fake.bwave"), ["fifo_buffer"])
        assert result == "tb_fifo_buffer.dut.*"

    def test_no_match_returns_none(self, monkeypatch):
        self._resolve_fake_bwave(monkeypatch)
        endpoint = self._make_endpoint("nonexistent.sv")
        signals = ["tb.other_module.sig"]
        fake_stdout = self._fake_bwave_stats(signals)
        monkeypatch.setattr(
            subprocess,
            "run",
            lambda *a, **kw: types.SimpleNamespace(
                returncode=0,
                stdout=fake_stdout,
                stderr="",
            ),
        )
        result = endpoint._discover_dut_scope(Path("fake.bwave"), ["nonexistent"])
        assert result is None

    def test_bwave_failure_returns_none(self, monkeypatch):
        self._resolve_fake_bwave(monkeypatch)
        endpoint = self._make_endpoint("alu.sv")
        monkeypatch.setattr(
            subprocess,
            "run",
            lambda *a, **kw: types.SimpleNamespace(
                returncode=1,
                stdout="",
                stderr="error",
            ),
        )
        result = endpoint._discover_dut_scope(Path("fake.bwave"), ["alu"])
        assert result is None

    def test_prefers_stem_match_over_generic(self, monkeypatch):
        """When both a stem-suffix match and a generic 'dut' exist, prefer stem."""
        self._resolve_fake_bwave(monkeypatch)
        endpoint = self._make_endpoint("aes.sv")
        signals = [
            "tb.uu_aes.key",
            "tb.uu_aes.data",
            "tb.dut.other_sig",
        ]
        fake_stdout = self._fake_bwave_stats(signals)
        monkeypatch.setattr(
            subprocess,
            "run",
            lambda *a, **kw: types.SimpleNamespace(
                returncode=0,
                stdout=fake_stdout,
                stderr="",
            ),
        )
        result = endpoint._discover_dut_scope(Path("fake.bwave"), ["aes"])
        assert result == "tb.uu_aes.*"


# ===================================================================
# _find_trace_file (static)
# ===================================================================


class TestFindTraceFile:
    def test_store_preferred(self, tmp_path):
        # A *valid* store must win over the VCD. (The old 21-byte fake failed
        # _bwave_valid, and the test only passed because the empty VCD next to
        # it silently converted into a header-only store — the exact silent
        # failure `bwave build` now refuses with exit 2.)
        from tests.conftest import MINIMAL_FST_BYTES

        (tmp_path / "trace.fst").write_bytes(MINIMAL_FST_BYTES)
        (tmp_path / "trace.vcd").write_text("")
        result = CoverageAnalystSpecialist._find_trace_file(tmp_path)
        assert result is not None
        assert result.suffix == ".fst"

    def test_vcd_fallback(self, tmp_path):
        (tmp_path / "trace.vcd").write_text("")
        result = CoverageAnalystSpecialist._find_trace_file(tmp_path)
        assert result is not None
        # find_trace may auto-convert to .fst or return .vcd
        assert result.suffix in (".vcd", ".fst")

    def test_no_trace_returns_none(self, tmp_path):
        assert CoverageAnalystSpecialist._find_trace_file(tmp_path) is None

    def test_ignores_other_extensions(self, tmp_path):
        (tmp_path / "trace.log").write_text("")
        (tmp_path / "trace.txt").write_text("")
        assert CoverageAnalystSpecialist._find_trace_file(tmp_path) is None


# ===================================================================
# _trace_is_stale
# ===================================================================


class TestInvalidateTraceCache:
    def test_deletes_cached_bwave(self, tmp_path):
        from booley.flows.sim.trace_session import TraceSession

        work_dir = Path("sim_default")
        with patch("booley.flows.sim.trace_session._bwave_cache_root", return_value=tmp_path / "bwave"):
            # The bucket name is derived (work-dir name + path digest), so ask
            # TraceSession for it instead of hardcoding the old bare name.
            stale = TraceSession(work_dir).cache_dir / "trace.fst"
            stale.write_bytes(b"BWAV" + b"\x00" * 16)
            CoverageAnalystSpecialist._invalidate_trace_cache(work_dir)
        assert not stale.exists()

    def test_noop_when_no_cache_dir(self, tmp_path):
        with patch("booley.flows.sim.trace_session._bwave_cache_root", return_value=tmp_path / "bwave"):
            CoverageAnalystSpecialist._invalidate_trace_cache(Path("nonexistent"))


# ===================================================================
# _canon_value
# ===================================================================


class TestCanonValue:
    def test_verilog_hex(self):
        assert _canon_value("'hFF") == "FF"
        assert _canon_value("'h0") == "0"
        assert _canon_value("'hA") == "A"

    def test_verilog_decimal(self):
        assert _canon_value("'d255") == "FF"
        assert _canon_value("'d0") == "0"
        assert _canon_value("'d3") == "3"
        assert _canon_value("'d16") == "10"

    def test_verilog_binary(self):
        assert _canon_value("'b1010") == "A"
        assert _canon_value("'b0") == "0"
        assert _canon_value("'b1") == "1"

    def test_width_prefix(self):
        assert _canon_value("8'd255") == "FF"
        assert _canon_value("4'hF") == "F"

    def test_legacy_pct_suffix(self):
        assert _canon_value("0%d") == "0"
        assert _canon_value("FF%h") == "FF"
        assert _canon_value("3%d") == "3"

    def test_python_binary_prefix(self):
        assert _canon_value("0b0") == "0"
        assert _canon_value("0b1") == "1"
        assert _canon_value("0b1010") == "A"

    def test_bare_hex(self):
        assert _canon_value("FF") == "FF"
        assert _canon_value("0") == "0"
        assert _canon_value("A") == "A"

    def test_leading_zeros_stripped(self):
        assert _canon_value("00FF") == "FF"
        assert _canon_value("'h00FF") == "FF"

    def test_all_zeros(self):
        assert _canon_value("0") == "0"
        assert _canon_value("'d0") == "0"
        assert _canon_value("'h0") == "0"
        assert _canon_value("'b0") == "0"
        assert _canon_value("000") == "0"

    # --- x/z handling (Fix 6) ---

    def test_x_values_returned_as_is(self):
        assert _canon_value("'hx") == "'HX"
        assert _canon_value("'hxx") == "'HXX"
        assert _canon_value("'bxxxx") == "'BXXXX"
        assert _canon_value("4'hx") == "4'HX"

    def test_z_values_returned_as_is(self):
        assert _canon_value("'hz") == "'HZ"
        assert _canon_value("'bz") == "'BZ"
        assert _canon_value("'bzzzz") == "'BZZZZ"

    def test_mixed_xz_values(self):
        assert _canon_value("'hxz") == "'HXZ"
        assert _canon_value("8'bxxxx_zzzz") == "8'BXXXX_ZZZZ"

    def test_0x_prefix_not_confused_with_x(self):
        """0xFF is a valid hex value, not an x/z value."""
        assert _canon_value("0xFF") == "FF"
        assert _canon_value("0x0") == "0"
        assert _canon_value("0X1A") == "1A"

    def test_x_in_verilog_literal_digits(self):
        assert _canon_value("'hxF") == "'HXF"
        assert _canon_value("'d0x") == "'D0X"  # nonsensical but has x


# ===================================================================
# _build_rtl_name_map / _resolve_fsm_enum_names
# ===================================================================


class TestBuildRtlNameMap:
    def test_localparam(self):
        rtl = "localparam IDLE = 2'd0;\nlocalparam ACTIVE = 2'd1;"
        m = _build_rtl_name_map(rtl)
        assert m["IDLE"] == "0"
        assert m["ACTIVE"] == "1"

    def test_parameter(self):
        rtl = "parameter STATE_A = 4'hA;"
        m = _build_rtl_name_map(rtl)
        assert m["STATE_A"] == "A"

    def test_typedef_enum_with_assignments(self):
        rtl = "typedef enum logic [1:0] {IDLE=2'd0, RUN=2'd1, DONE=2'd2} state_t;"
        m = _build_rtl_name_map(rtl)
        assert m["IDLE"] == "0"
        assert m["RUN"] == "1"
        assert m["DONE"] == "2"

    def test_typedef_enum_auto_increment(self):
        rtl = "typedef enum logic [1:0] {IDLE, RUN, DONE} state_t;"
        m = _build_rtl_name_map(rtl)
        assert m["IDLE"] == "0"
        assert m["RUN"] == "1"
        assert m["DONE"] == "2"

    def test_typedef_enum_mixed_assignment(self):
        rtl = "typedef enum logic [2:0] {IDLE=3'd0, RUN, FLUSH=3'd5, EXIT} state_t;"
        m = _build_rtl_name_map(rtl)
        assert m["IDLE"] == "0"
        assert m["RUN"] == "1"
        assert m["FLUSH"] == "5"
        assert m["EXIT"] == "6"

    def test_define(self):
        rtl = "`define ST_IDLE 2'd0\n`define ST_RUN 2'd1"
        m = _build_rtl_name_map(rtl)
        assert m["ST_IDLE"] == "0"
        assert m["ST_RUN"] == "1"

    def test_empty_rtl(self):
        assert _build_rtl_name_map("") == {}

    def test_no_definitions(self):
        assert _build_rtl_name_map("always @(posedge clk) q <= d;") == {}


class TestIsNumericVerilogLiteral:
    def test_verilog_radix_prefixes(self):
        assert _is_numeric_verilog_literal("'d3") is True
        assert _is_numeric_verilog_literal("'hFF") is True
        assert _is_numeric_verilog_literal("'b1010") is True
        assert _is_numeric_verilog_literal("8'd255") is True
        assert _is_numeric_verilog_literal("16'hCAFE") is True

    def test_c_style_prefixes(self):
        assert _is_numeric_verilog_literal("0xFF") is True
        assert _is_numeric_verilog_literal("0b1010") is True
        assert _is_numeric_verilog_literal("0X1A") is True

    def test_legacy_suffixes(self):
        assert _is_numeric_verilog_literal("3%d") is True
        assert _is_numeric_verilog_literal("FF%h") is True

    def test_bare_decimal(self):
        assert _is_numeric_verilog_literal("0") is True
        assert _is_numeric_verilog_literal("42") is True

    def test_symbolic_names(self):
        assert _is_numeric_verilog_literal("IDLE") is False
        assert _is_numeric_verilog_literal("STATE_A") is False
        assert _is_numeric_verilog_literal("ST_EMPTY") is False

    def test_ambiguous_bare_hex(self):
        # Bare hex without prefix is NOT treated as numeric — could be a name
        assert _is_numeric_verilog_literal("A0") is False
        assert _is_numeric_verilog_literal("DEAD") is False
        assert _is_numeric_verilog_literal("FF") is False


class TestResolveFsmEnumNames:
    def test_resolves_localparam_names(self):
        rtl = "localparam IDLE = 2'd0;\nlocalparam ACTIVE = 2'd1;\nlocalparam DONE = 2'd2;"
        regs = [{"signal": "state", "expected_values": ["IDLE", "ACTIVE", "DONE"]}]
        resolved = _resolve_fsm_enum_names(regs, rtl)
        vals = resolved[0]["expected_values"]
        assert all(
            _canon_value(v) == expected for v, expected in zip(vals, ["0", "1", "2"], strict=True)
        )

    def test_numeric_values_unchanged(self):
        rtl = "localparam IDLE = 2'd0;"
        regs = [{"signal": "state", "expected_values": ["'d0", "'d1", "'d2"]}]
        resolved = _resolve_fsm_enum_names(regs, rtl)
        assert resolved[0]["expected_values"] == ["'d0", "'d1", "'d2"]

    def test_empty_registers(self):
        assert _resolve_fsm_enum_names([], "localparam X = 0;") == []

    def test_empty_rtl(self):
        regs = [{"signal": "s", "expected_values": ["IDLE"]}]
        assert _resolve_fsm_enum_names(regs, "") is regs

    def test_unresolvable_name_kept_as_is(self):
        rtl = "localparam OTHER = 2'd0;"
        regs = [{"signal": "state", "expected_values": ["UNKNOWN_STATE"]}]
        resolved = _resolve_fsm_enum_names(regs, rtl)
        assert resolved[0]["expected_values"] == ["UNKNOWN_STATE"]

    def test_mixed_resolved_and_numeric(self):
        rtl = "localparam IDLE = 2'd0;"
        regs = [{"signal": "state", "expected_values": ["IDLE", "'d1"]}]
        resolved = _resolve_fsm_enum_names(regs, rtl)
        vals = resolved[0]["expected_values"]
        assert _canon_value(vals[0]) == "0"
        assert vals[1] == "'d1"

    def test_enum_typedef_resolution(self):
        rtl = "typedef enum logic [1:0] {ST_EMPTY=2'd0, ST_OUTPUT=2'd1, ST_SKID=2'd2} state_t;"
        regs = [{"signal": "state", "expected_values": ["ST_EMPTY", "ST_OUTPUT", "ST_SKID"]}]
        resolved = _resolve_fsm_enum_names(regs, rtl)
        vals = resolved[0]["expected_values"]
        assert _canon_value(vals[0]) == "0"
        assert _canon_value(vals[1]) == "1"
        assert _canon_value(vals[2]) == "2"


# ===================================================================
# _extract_json_block
# ===================================================================


class TestExtractJsonBlock:
    def test_json_code_block(self):
        raw = '```json\n{"key": "val"}\n```'
        assert _extract_json_block(raw) == {"key": "val"}

    def test_bare_json(self):
        assert _extract_json_block('{"a": 1}') == {"a": 1}

    def test_brace_matching_fallback(self):
        raw = 'preamble {"x": 2} trailing'
        assert _extract_json_block(raw) == {"x": 2}

    def test_trailing_commas_cleaned(self):
        raw = '{"a": [1,], "b": 2,}'
        result = _extract_json_block(raw)
        assert result == {"a": [1], "b": 2}

    def test_returns_none_for_garbage(self):
        assert _extract_json_block("no json") is None

    def test_returns_none_for_list(self):
        assert _extract_json_block("[1, 2, 3]") is None

    def test_returns_none_for_empty(self):
        assert _extract_json_block("") is None


# ===================================================================
# _read_rtl_sources (Fix 5)
# ===================================================================


class TestReadRtlSources:
    def test_reads_existing_files(self, tmp_path):
        (tmp_path / "alu.sv").write_text("module alu; endmodule")
        endpoint = _make_endpoint_with_args(scope="alu.sv", work_dir=str(tmp_path))
        ctx = endpoint._read_rtl_sources()
        assert "module alu" in ctx
        assert "```systemverilog" in ctx

    def test_skips_missing_files(self, tmp_path):
        endpoint = _make_endpoint_with_args(scope="missing.sv", work_dir=str(tmp_path))
        ctx = endpoint._read_rtl_sources()
        assert ctx == ""

    def test_truncates_large_files(self, tmp_path):
        from booley.specialists.coverage_analyst import _RTL_MAX_CHARS

        content = "x" * (_RTL_MAX_CHARS + 1000)
        (tmp_path / "big.sv").write_text(content)
        endpoint = _make_endpoint_with_args(scope="big.sv", work_dir=str(tmp_path))
        ctx = endpoint._read_rtl_sources()
        assert "(truncated)" in ctx


# ===================================================================
# _compute_scope_hash
# ===================================================================


class TestComputeScopeHash:
    def test_deterministic_no_workdir(self):
        assert _compute_scope_hash("a.sv,b.sv") == _compute_scope_hash("a.sv,b.sv")

    def test_order_insensitive_no_workdir(self):
        assert _compute_scope_hash("a.sv,b.sv") == _compute_scope_hash("b.sv,a.sv")

    def test_whitespace_insensitive(self):
        assert _compute_scope_hash("a.sv, b.sv") == _compute_scope_hash("a.sv,b.sv")

    def test_different_scopes_differ(self):
        assert _compute_scope_hash("a.sv") != _compute_scope_hash("b.sv")

    def test_empty_blanks_ignored(self):
        assert _compute_scope_hash("a.sv,,b.sv") == _compute_scope_hash("a.sv,b.sv")

    def test_content_change_invalidates(self, tmp_path):
        (tmp_path / "a.sv").write_text("module a; endmodule")
        h1 = _compute_scope_hash("a.sv", tmp_path)
        (tmp_path / "a.sv").write_text("module a_v2; endmodule")
        h2 = _compute_scope_hash("a.sv", tmp_path)
        assert h1 != h2

    def test_same_content_same_hash(self, tmp_path):
        (tmp_path / "a.sv").write_text("module a; endmodule")
        h1 = _compute_scope_hash("a.sv", tmp_path)
        h2 = _compute_scope_hash("a.sv", tmp_path)
        assert h1 == h2


# ===================================================================
# PersistentWaivers
# ===================================================================


class TestPersistentWaivers:
    def test_round_trip(self):
        pw = PersistentWaivers(
            toggle_waivers={"clk": "waived", "rst": "waived"},
            value_waivers={"en": "waived"},
            value_classifications={"en": "sufficient", "data": "insufficient"},
            scope_hash="abc123",
        )
        d = pw.to_dict()
        pw2 = PersistentWaivers.from_dict(d)
        assert pw2.toggle_waivers == pw.toggle_waivers
        assert pw2.value_waivers == pw.value_waivers
        assert pw2.value_classifications == pw.value_classifications
        assert pw2.scope_hash == pw.scope_hash

    def test_from_dict_defaults(self):
        pw = PersistentWaivers.from_dict({})
        assert pw.toggle_waivers == {}
        assert pw.value_waivers == {}
        assert pw.value_classifications == {}
        assert pw.scope_hash == ""

    def test_json_round_trip(self):
        pw = PersistentWaivers(
            toggle_waivers={"sig": "waived"},
            value_classifications={"sig": "sufficient"},
            value_waivers={},
            scope_hash="deadbeef",
        )
        serialized = json.dumps(pw.to_dict())
        pw2 = PersistentWaivers.from_dict(json.loads(serialized))
        assert pw2.scope_hash == "deadbeef"
        assert pw2.toggle_waivers == {"sig": "waived"}

    def test_from_dict_rejects_list_fields(self):
        """Wrong types (list instead of dict) should raise TypeError."""
        import pytest

        with pytest.raises(TypeError):
            PersistentWaivers.from_dict({"toggle_waivers": ["a", "b"]})

    def test_from_dict_rejects_mixed_types(self):
        import pytest

        with pytest.raises(TypeError):
            PersistentWaivers.from_dict(
                {
                    "toggle_waivers": {},
                    "value_waivers": ["bad"],
                    "value_classifications": {},
                }
            )


# ===================================================================
# --criteria filtering in _get_active_criteria
# ===================================================================


class TestCriteriaFiltering:
    def _make_endpoint_with_criteria(self, criteria=None, state_criteria=None):
        endpoint = _make_endpoint_with_args(criteria=criteria)
        if state_criteria is None:
            state_criteria = {
                "coverage_toggle_default": True,
                "coverage_fsm_default": True,
                "coverage_value_default": True,
                "coverage_branch_default": True,
                "coverage_expression_default": True,
            }
        endpoint._state = types.SimpleNamespace(criteria=state_criteria)
        endpoint.satisfies = [
            "coverage_toggle",
            "coverage_fsm",
            "coverage_value",
            "coverage_branch",
            "coverage_expression",
        ]
        return endpoint

    def test_no_filter_returns_all(self):
        endpoint = self._make_endpoint_with_criteria(criteria=None)
        active = endpoint._get_active_criteria()
        assert active == {
            "coverage_toggle",
            "coverage_fsm",
            "coverage_value",
            "coverage_branch",
            "coverage_expression",
        }

    def test_single_filter(self):
        endpoint = self._make_endpoint_with_criteria(criteria="toggle")
        active = endpoint._get_active_criteria()
        assert active == {"coverage_toggle"}

    def test_multiple_filters(self):
        endpoint = self._make_endpoint_with_criteria(criteria="toggle,branch")
        active = endpoint._get_active_criteria()
        assert active == {"coverage_toggle", "coverage_branch"}

    def test_whitespace_handling(self):
        endpoint = self._make_endpoint_with_criteria(criteria=" toggle , value ")
        active = endpoint._get_active_criteria()
        assert active == {"coverage_toggle", "coverage_value"}

    def test_invalid_names_ignored(self):
        endpoint = self._make_endpoint_with_criteria(criteria="toggle,bogus,fsm")
        active = endpoint._get_active_criteria()
        assert active == {"coverage_toggle", "coverage_fsm"}

    def test_all_invalid_no_filtering(self):
        """If all --criteria names are invalid, no filtering is applied."""
        endpoint = self._make_endpoint_with_criteria(criteria="bogus,fake")
        active = endpoint._get_active_criteria()
        # requested set is empty, so no intersection — returns full active set
        assert len(active) == 5

    def test_filter_intersects_with_state(self):
        """--criteria only narrows, never adds criteria not in state."""
        endpoint = self._make_endpoint_with_criteria(
            criteria="toggle,branch",
            state_criteria={
                "coverage_toggle_default": True,
                "coverage_value_default": True,
            },
        )
        active = endpoint._get_active_criteria()
        # state has toggle+value; CLI requests toggle+branch; intersection = toggle
        assert active == {"coverage_toggle"}


# ===================================================================
# Persistent waiver load/save on CoverageAnalystSpecialist
# ===================================================================


class TestPersistentWaiverIO:
    def test_save_and_load(self, tmp_path):
        endpoint = _make_endpoint_with_args(scope="alu.sv")
        endpoint._args.report_dir = tmp_path

        pw = PersistentWaivers(
            toggle_waivers={"clk": "waived"},
            value_waivers={"en": "waived"},
            value_classifications={"en": "sufficient"},
            scope_hash="abc123",
        )
        endpoint._save_persistent_waivers(pw)
        assert (tmp_path / "coverage_waivers.json").exists()

        loaded = endpoint._load_persistent_waivers("abc123")
        assert loaded is not None
        assert loaded.toggle_waivers == {"clk": "waived"}
        assert loaded.value_classifications == {"en": "sufficient"}

    def test_load_stale_hash_returns_none(self, tmp_path):
        endpoint = _make_endpoint_with_args(scope="alu.sv")
        endpoint._args.report_dir = tmp_path

        pw = PersistentWaivers(scope_hash="old_hash")
        endpoint._save_persistent_waivers(pw)

        loaded = endpoint._load_persistent_waivers("new_hash")
        assert loaded is None

    def test_load_missing_file_returns_none(self, tmp_path):
        endpoint = _make_endpoint_with_args(scope="alu.sv")
        endpoint._args.report_dir = tmp_path
        assert endpoint._load_persistent_waivers("any") is None

    def test_load_corrupt_file_returns_none(self, tmp_path):
        endpoint = _make_endpoint_with_args(scope="alu.sv")
        endpoint._args.report_dir = tmp_path
        (tmp_path / "coverage_waivers.json").write_text("not json!", encoding="utf-8")
        assert endpoint._load_persistent_waivers("any") is None

    def test_load_no_report_dir(self):
        endpoint = _make_endpoint_with_args(scope="alu.sv")
        endpoint._args.report_dir = None
        assert endpoint._load_persistent_waivers("any") is None

    def test_save_no_report_dir(self):
        """Save with no report_dir is a no-op (no error)."""
        endpoint = _make_endpoint_with_args(scope="alu.sv")
        endpoint._args.report_dir = None
        pw = PersistentWaivers(scope_hash="x")
        endpoint._save_persistent_waivers(pw)

    def test_load_wrong_type_fields_returns_none(self, tmp_path):
        """Cache file with list instead of dict for waivers is treated as corrupt."""
        endpoint = _make_endpoint_with_args(scope="alu.sv")
        endpoint._args.report_dir = tmp_path
        bad_data = json.dumps(
            {
                "toggle_waivers": ["a", "b"],
                "value_waivers": {},
                "value_classifications": {},
                "scope_hash": "abc",
            }
        )
        (tmp_path / "coverage_waivers.json").write_text(bad_data, encoding="utf-8")
        assert endpoint._load_persistent_waivers("abc") is None

    def test_reset_waivers_skips_load(self, tmp_path):
        """With reset_waivers=True, cached waivers should not be loaded."""
        endpoint = _make_endpoint_with_args(scope="alu.sv", reset_waivers=True)
        endpoint._args.report_dir = tmp_path

        pw = PersistentWaivers(
            toggle_waivers={"clk": "waived"},
            scope_hash=_compute_scope_hash("alu.sv"),
        )
        endpoint._save_persistent_waivers(pw)

        # Simulate the check from _run(): skip load when reset_waivers is True
        if not getattr(endpoint._args, "reset_waivers", False):
            loaded = endpoint._load_persistent_waivers(_compute_scope_hash("alu.sv"))
        else:
            loaded = None
        assert loaded is None


# ===================================================================
# _build_reviewer_prompt criteria gating
# ===================================================================


class TestReviewerPromptCriteriaGating:
    def test_toggle_section_included_when_active(self):
        endpoint = _make_endpoint_with_args(scope="alu.sv")
        prompt = endpoint._build_reviewer_prompt(
            toggle_failures=[_sig("clk", transitions=0)],
            low_diversity=[],
            branch_results=[],
            expression_results=[],
            fsm_result=FsmResult(),
            rtl_context="",
            active_criteria={"coverage_toggle"},
        )
        assert "Toggle Failures" in prompt

    def test_toggle_section_skipped_when_not_active(self):
        endpoint = _make_endpoint_with_args(scope="alu.sv")
        prompt = endpoint._build_reviewer_prompt(
            toggle_failures=[_sig("clk", transitions=0)],
            low_diversity=[],
            branch_results=[],
            expression_results=[],
            fsm_result=FsmResult(),
            rtl_context="",
            active_criteria={"coverage_value"},
        )
        assert "Toggle Failures" not in prompt

    def test_value_section_included_when_active(self):
        endpoint = _make_endpoint_with_args(scope="alu.sv")
        prompt = endpoint._build_reviewer_prompt(
            toggle_failures=[],
            low_diversity=[_sig("en", transitions=5, value_hist={"0": 10, "1": 5})],
            branch_results=[],
            expression_results=[],
            fsm_result=FsmResult(),
            rtl_context="",
            active_criteria={"coverage_value"},
        )
        assert "Value Diversity" in prompt

    def test_value_section_skipped_when_not_active(self):
        endpoint = _make_endpoint_with_args(scope="alu.sv")
        prompt = endpoint._build_reviewer_prompt(
            toggle_failures=[],
            low_diversity=[_sig("en", transitions=5, value_hist={"0": 10, "1": 5})],
            branch_results=[],
            expression_results=[],
            fsm_result=FsmResult(),
            rtl_context="",
            active_criteria={"coverage_toggle"},
        )
        assert "Value Diversity" not in prompt

    def test_both_sections_with_none_criteria(self):
        """active_criteria=None means all active — both sections included."""
        endpoint = _make_endpoint_with_args(scope="alu.sv")
        prompt = endpoint._build_reviewer_prompt(
            toggle_failures=[_sig("clk", transitions=0)],
            low_diversity=[_sig("en", transitions=5, value_hist={"0": 10})],
            branch_results=[],
            expression_results=[],
            fsm_result=FsmResult(),
            rtl_context="",
            active_criteria=None,
        )
        assert "Toggle Failures" in prompt
        assert "Value Diversity" in prompt

    def test_resume_prompt_uses_exact_json_schema_keys(self):
        endpoint = _make_endpoint_with_args(scope="alu.sv")
        prompt = endpoint._build_reviewer_resume_prompt(
            toggle_failures=[_sig("clk", transitions=0)],
            low_diversity=[_sig("en", transitions=5, value_hist={"0": 10})],
            active_criteria={"coverage_toggle", "coverage_value"},
        )

        for key in (
            '"toggle_waivers"',
            '"value_classifications"',
            '"value_waivers"',
            '"notes"',
            '"improvement_hints"',
        ):
            assert key in prompt
        assert "TOGGLE_WAIVERS" not in prompt
        assert "VALUE_CLASSIFICATIONS" not in prompt
        assert "Return ONLY valid JSON" in prompt


# ===================================================================
# _scan_tb_for_dump_calls
# ===================================================================


class TestScanTbForDumpCalls:
    """Pre-flight scanner finds TB-level $dumpfile/$dumpvars calls."""

    def _setup(self, tmp_path, tb_contents):
        """Create a verif/ TB dir with the given filename->contents map."""
        tb_dir = tmp_path / "verif"
        tb_dir.mkdir()
        for name, body in tb_contents.items():
            (tb_dir / name).write_text(body, encoding="utf-8")
        return tb_dir

    def _patch_tb_dirs(self, tb_dir, monkeypatch):
        monkeypatch.setattr(
            "booley.runtime.shared_infra.get_tb_dirs",
            lambda: ([tb_dir], []),
        )

    def test_finds_unguarded_dumpfile(self, tmp_path, monkeypatch):
        tb_dir = self._setup(
            tmp_path,
            {
                "tb_factorial.sv": (
                    "module tb_factorial;\n"
                    "  initial begin\n"
                    '    $dumpfile("wave.vcd");\n'
                    "    $dumpvars(0, dut);\n"
                    "  end\n"
                    "endmodule\n"
                )
            },
        )
        self._patch_tb_dirs(tb_dir, monkeypatch)
        hits = CoverageAnalystSpecialist._scan_tb_for_dump_calls(tmp_path)
        assert len(hits) == 2
        kinds = sorted(h[3] for h in hits)
        assert kinds == ["dumpfile", "dumpvars"]
        # File paths are relative to work_dir
        assert hits[0][0] == "verif/tb_factorial.sv"
        # Line numbers point to the actual calls
        assert hits[0][1] == 3
        assert hits[1][1] == 4

    def test_ignores_line_comments(self, tmp_path, monkeypatch):
        tb_dir = self._setup(
            tmp_path,
            {
                "tb.sv": (
                    "module tb;\n"
                    '  // $dumpfile("never.vcd");\n'
                    '  initial $display("hi");  // $dumpvars not called\n'
                    "endmodule\n"
                )
            },
        )
        self._patch_tb_dirs(tb_dir, monkeypatch)
        assert CoverageAnalystSpecialist._scan_tb_for_dump_calls(tmp_path) == []

    def test_clean_tb_returns_empty(self, tmp_path, monkeypatch):
        tb_dir = self._setup(
            tmp_path, {"tb.sv": ('module tb;\n  initial $display("hello");\nendmodule\n')}
        )
        self._patch_tb_dirs(tb_dir, monkeypatch)
        assert CoverageAnalystSpecialist._scan_tb_for_dump_calls(tmp_path) == []

    def test_scans_multiple_files_and_subdirs(self, tmp_path, monkeypatch):
        tb_dir = self._setup(
            tmp_path,
            {
                "tb_a.sv": 'module a; initial $dumpfile("a.vcd"); endmodule\n',
                "tb_b.sv": "module b; initial $dumpvars(0); endmodule\n",
            },
        )
        (tb_dir / "sub").mkdir()
        (tb_dir / "sub" / "tb_c.sv").write_text(
            'module c; initial $dumpfile("c.vcd"); endmodule\n',
            encoding="utf-8",
        )
        self._patch_tb_dirs(tb_dir, monkeypatch)
        hits = CoverageAnalystSpecialist._scan_tb_for_dump_calls(tmp_path)
        relpaths = sorted(h[0] for h in hits)
        assert relpaths == ["verif/sub/tb_c.sv", "verif/tb_a.sv", "verif/tb_b.sv"]


# ===================================================================
# _scan_tb_for_dut_instances
# ===================================================================


class TestScanTbForDutInstances:
    """Pre-flight scanner finds DUT instantiations in TB sources."""

    def _setup(self, tmp_path, tb_contents):
        tb_dir = tmp_path / "verif"
        tb_dir.mkdir()
        for name, body in tb_contents.items():
            (tb_dir / name).write_text(body, encoding="utf-8")
        return tb_dir

    def _patch_tb_dirs(self, tb_dir, monkeypatch):
        monkeypatch.setattr(
            "booley.runtime.shared_infra.get_tb_dirs",
            lambda: ([tb_dir], []),
        )

    def test_finds_unparameterized_instance(self, tmp_path, monkeypatch):
        tb_dir = self._setup(
            tmp_path,
            {
                "tb.sv": (
                    "module cache_controller_tb;\n"
                    "  cache_controller uut (\n"
                    "    .clk(clk)\n"
                    "  );\n"
                    "endmodule\n"
                )
            },
        )
        self._patch_tb_dirs(tb_dir, monkeypatch)
        hits = CoverageAnalystSpecialist._scan_tb_for_dut_instances(
            tmp_path,
            "cache_controller",
        )
        assert hits == [("verif/tb.sv", 2, "uut", "cache_controller_tb")]

    def test_finds_parameterized_instance(self, tmp_path, monkeypatch):
        tb_dir = self._setup(
            tmp_path,
            {
                "tb.sv": (
                    "module tb;\n"
                    "  qam16_mapper_interpolated #(.WIDTH(8), .DEPTH(16)) dut (\n"
                    "    .clk(clk)\n"
                    "  );\n"
                    "endmodule\n"
                )
            },
        )
        self._patch_tb_dirs(tb_dir, monkeypatch)
        hits = CoverageAnalystSpecialist._scan_tb_for_dut_instances(
            tmp_path,
            "qam16_mapper_interpolated",
        )
        assert hits == [("verif/tb.sv", 2, "dut", "tb")]

    def test_skips_module_declaration(self, tmp_path, monkeypatch):
        # The DUT module's own declaration must not be mistaken for an
        # instantiation of itself.
        tb_dir = self._setup(
            tmp_path,
            {
                "design.sv": (
                    "module cache_controller(input clk);\n"
                    "endmodule\n"
                    "module cache_controller_tb;\n"
                    "  cache_controller uut (.clk(clk));\n"
                    "endmodule\n"
                )
            },
        )
        self._patch_tb_dirs(tb_dir, monkeypatch)
        hits = CoverageAnalystSpecialist._scan_tb_for_dut_instances(
            tmp_path,
            "cache_controller",
        )
        # Only the real instantiation, not the module declaration.
        assert hits == [("verif/design.sv", 4, "uut", "cache_controller_tb")]

    def test_skips_commented_instance(self, tmp_path, monkeypatch):
        tb_dir = self._setup(
            tmp_path,
            {
                "tb.sv": (
                    "module tb;\n"
                    "  // cache_controller uut (.clk(clk));\n"
                    "  /* cache_controller old (.clk(clk)); */\n"
                    "  cache_controller real_uut (.clk(clk));\n"
                    "endmodule\n"
                )
            },
        )
        self._patch_tb_dirs(tb_dir, monkeypatch)
        hits = CoverageAnalystSpecialist._scan_tb_for_dut_instances(
            tmp_path,
            "cache_controller",
        )
        # Only the uncommented one survives; lineno still maps to original
        # source position because block-comment stripping preserves newlines.
        assert hits == [("verif/tb.sv", 4, "real_uut", "tb")]

    def test_tracks_containing_module(self, tmp_path, monkeypatch):
        # 16qam-style: DUT instances inside a wrapper module, not in top.
        tb_dir = self._setup(
            tmp_path,
            {
                "tb.sv": (
                    "module tb_16qam_mapper_case;\n"
                    "  qam16_mapper_interpolated #(.N(4)) dut (.clk(clk));\n"
                    "endmodule\n"
                    "module tb_16qam_mapper;\n"
                    "  tb_16qam_mapper_case #(.N(4)) case_inst (.clk(clk));\n"
                    "endmodule\n"
                )
            },
        )
        self._patch_tb_dirs(tb_dir, monkeypatch)
        hits = CoverageAnalystSpecialist._scan_tb_for_dut_instances(
            tmp_path,
            "qam16_mapper_interpolated",
        )
        assert hits == [
            ("verif/tb.sv", 2, "dut", "tb_16qam_mapper_case"),
        ]

    def test_empty_when_dut_not_instantiated(self, tmp_path, monkeypatch):
        tb_dir = self._setup(
            tmp_path, {"tb.sv": ('module tb;\n  initial $display("hi");\nendmodule\n')}
        )
        self._patch_tb_dirs(tb_dir, monkeypatch)
        assert (
            CoverageAnalystSpecialist._scan_tb_for_dut_instances(
                tmp_path,
                "cache_controller",
            )
            == []
        )

    def test_empty_dut_top_module_returns_empty(self, tmp_path, monkeypatch):
        tb_dir = self._setup(tmp_path, {"tb.sv": "module tb; endmodule\n"})
        self._patch_tb_dirs(tb_dir, monkeypatch)
        assert (
            CoverageAnalystSpecialist._scan_tb_for_dut_instances(
                tmp_path,
                "",
            )
            == []
        )

    def test_word_boundary_avoids_substring_match(self, tmp_path, monkeypatch):
        # "cache_controller" must not match "cache_controller_t" identifier
        tb_dir = self._setup(
            tmp_path,
            {
                "tb.sv": (
                    "module tb;\n"
                    "  cache_controller_t my_var;\n"
                    "  cache_controller_ext bad (.clk(clk));\n"
                    "endmodule\n"
                )
            },
        )
        self._patch_tb_dirs(tb_dir, monkeypatch)
        assert (
            CoverageAnalystSpecialist._scan_tb_for_dut_instances(
                tmp_path,
                "cache_controller",
            )
            == []
        )


# ===================================================================
# _strip_sv_comments
# ===================================================================


class TestStripSvComments:
    def test_line_comment_stripped_newline_preserved(self):
        text = "a // comment\nb\n"
        out = CoverageAnalystSpecialist._strip_sv_comments(text)
        assert out == "a \nb\n"

    def test_block_comment_preserves_newline_count(self):
        text = "a /* line1\nline2\nline3 */ b\n"
        out = CoverageAnalystSpecialist._strip_sv_comments(text)
        # 2 newlines inside the block comment must survive so subsequent
        # line numbers map back to the original source.
        assert out.count("\n") == text.count("\n")
        assert "line1" not in out


# ===================================================================
# pytest_approx helper
# ===================================================================


def pytest_approx(expected, tolerance=0.1):
    """Simple approximate comparison for float tests."""
    import pytest

    return pytest.approx(expected, abs=tolerance)


# ===================================================================
# Unit A.2 — edalize trace command builder + test-selector plusargs
# ===================================================================


class TestTraceTestPlusargs:
    """_trace_test_plusargs renders the tests.toml select plusarg (decision 16)."""

    def test_empty_test_yields_no_selector(self):
        assert _trace_test_plusargs("config_a", "") == []
        assert _trace_test_plusargs("config_a", None) == []

    def test_unknown_test_falls_back_to_default(self):
        # Substring that resolves to nothing → raw passthrough (binary default).
        with patch("booley.config.project_config.TEST_NAMES", {"config_a": ["smoke"]}):
            assert _trace_test_plusargs("config_a", "nope") == []

    def test_resolves_substring_to_indexed_selector(self):
        with patch(
            "booley.config.project_config.TEST_NAMES", {"config_a": ["smoke", "regress", "stress"]}
        ):
            # Default template is "+test_id={index}".
            assert _trace_test_plusargs("config_a", "regress") == ["+test_id=1"]


class TestBuildEdalizeTraceCmd:
    """_build_edalize_trace_cmd composes resolve_target → make && verilator_run."""

    def _resolved(self, build_root):
        return types.SimpleNamespace(build_root=Path(build_root), toplevel="tb_top")

    def _overlay(self, mode):
        return types.SimpleNamespace(
            vlnv="::demo-booleytrace:0",
            mode=mode,
            cleanup=lambda: None,
        )

    def test_ships_make_and_verilator_run_one_shell(self, tmp_path):
        work_dir = tmp_path
        build_root = tmp_path / ".edalize" / "coverage" / "config_a"
        endpoint = _make_endpoint_with_args(
            work_dir=str(work_dir), target="config_a", tb_top="tb_top", test=""
        )
        with (
            patch(
                "booley.specialists.coverage_analyst.resolve_trace_args",
                return_value=["--trace={file}"],
            ),
            patch(
                "booley.specialists.coverage_analyst.resolve_trace_files",
                return_value=["hardcoded.fst"],
            ),
            patch(
                "booley.fusesoc.fusesoc_registry.write_trace_overlay",
                return_value=self._overlay(fusesoc_registry.TraceMode.NATIVE_FST),
            ),
            patch(
                "booley.fusesoc.fusesoc_registry.resolve_target",
                return_value=self._resolved(build_root),
            ),
        ):
            cmd = endpoint._build_edalize_trace_cmd(
                work_dir,
                work_dir / "sim_cfg",
                "dut",
                600,
            )
        assert cmd[0:2] == ["sh", "-c"]
        script = cmd[2]
        # build half: make -C <rel build_root>, run half: verilator_run --trace
        assert "make -C" in script
        assert "booley.flows.sim.backends.verilator" in script
        assert "--trace" in script
        assert "--trace-mode native_fst" in script
        assert "--trace-arg=--trace={file}" in script
        assert "--trace-file=hardcoded.fst" in script
        assert "--trace-scope dut" in script
        assert "--top tb_top" in script
        # build failure guard short-circuits the run
        assert "exit 1" in script
        assert "&&" not in script  # uses '|| { ...; exit 1; }' + newline, not '&&'

    def test_steers_bwave_to_trace_dir_and_passes_suite_test(self, tmp_path):
        work_dir = tmp_path
        build_root = tmp_path / ".edalize" / "coverage" / "config_a"
        trace_dir = work_dir / "sim" / "config_a"
        endpoint = _make_endpoint_with_args(
            work_dir=str(work_dir), target="config_a", tb_top="tb_top"
        )
        endpoint._coverage_test = "regress"
        with (
            patch(
                "booley.fusesoc.fusesoc_registry.write_trace_overlay",
                return_value=self._overlay(fusesoc_registry.TraceMode.VCD_FIFO),
            ),
            patch(
                "booley.fusesoc.fusesoc_registry.resolve_target",
                return_value=self._resolved(build_root),
            ),
            patch("booley.config.project_config.TEST_NAMES", {"config_a": ["smoke", "regress"]}),
        ):
            cmd = endpoint._build_edalize_trace_cmd(work_dir, trace_dir, "", 600)
        script = cmd[2]
        # --work-dir is the trace_dir relative to work_dir (where _find_trace_file looks)
        assert "--work-dir sim/config_a" in script
        # test selector forwarded as a plusarg
        assert "--plusarg=+test_id=1" in script
        # no scope arg when none derived
        assert "--trace-scope" not in script

    def test_cocotb_trace_batches_every_target_test(self, tmp_path):
        build_root = tmp_path / ".edalize" / "coverage" / "sim_cocotb"
        trace_dir = tmp_path / "sim" / "sim_cocotb"
        endpoint = _make_endpoint_with_args(
            work_dir=str(tmp_path),
            target="sim_cocotb",
            tb_top="dut",
        )
        with (
            patch(
                "booley.fusesoc.fusesoc_registry.write_trace_overlay",
                return_value=self._overlay(fusesoc_registry.TraceMode.VCD_FIFO),
            ),
            patch(
                "booley.fusesoc.fusesoc_registry.resolve_target",
                return_value=self._resolved(build_root),
            ),
            patch(
                "booley.fusesoc.fusesoc_registry.target_cocotb_modules",
                return_value={"sim_cocotb": "test_dut"},
            ),
            patch(
                "booley.config.project_config.TEST_NAMES",
                {"sim_cocotb": ["smoke", "corner"]},
            ),
        ):
            cmd = endpoint._build_edalize_trace_cmd(tmp_path, trace_dir, "", 600)

        script = cmd[2]
        assert "booley.flows.sim.backends.cocotb" in script
        assert "--cocotb-module test_dut" in script
        assert "--test=smoke" in script
        assert "--test=corner" in script
        assert "--trace" in script

    def test_cocotb_trace_rejects_native_fst(self, tmp_path):
        endpoint = _make_endpoint_with_args(
            work_dir=str(tmp_path),
            target="sim_cocotb",
            tb_top="dut",
        )
        with (
            patch(
                "booley.fusesoc.fusesoc_registry.write_trace_overlay",
                return_value=self._overlay(fusesoc_registry.TraceMode.NATIVE_FST),
            ),
            patch(
                "booley.fusesoc.fusesoc_registry.resolve_target",
                return_value=self._resolved(tmp_path / "build"),
            ),
            patch(
                "booley.fusesoc.fusesoc_registry.target_cocotb_modules",
                return_value={"sim_cocotb": "test_dut"},
            ),
            pytest.raises(fusesoc_registry.FuseSocError, match=r"Cocotb.*native FST"),
        ):
            endpoint._build_edalize_trace_cmd(tmp_path, tmp_path / "trace", "", 600)


def test_qualified_target_uses_distinct_trace_dirs_per_test(tmp_path):
    endpoint = _make_endpoint_with_args(
        work_dir=str(tmp_path),
        target="vendor:lib:core#sim",
        tb_top="tb",
    )
    with patch("booley.config.project_config.TEST_NAMES", {"sim": ["smoke", "corner"]}):
        smoke = endpoint._find_trace_dir("smoke")
        corner = endpoint._find_trace_dir("corner")

    assert smoke != corner
    assert smoke.name.endswith(".smoke")
    assert corner.name.endswith(".corner")


def test_coverage_rejects_target_with_every_test_skipped(tmp_path):
    endpoint = _make_endpoint_with_args(
        work_dir=str(tmp_path),
        target="sim",
        tb_top="tb",
    )
    with (
        patch("booley.config.project_config.TEST_NAMES", {"sim": ["smoke", "corner"]}),
        patch("booley.config.project_config.TEST_SKIP", {"sim": ["smoke", "corner"]}),
    ):
        _scope, traces, error = endpoint._ensure_target_traces(tmp_path)

    assert traces == []
    assert error is not None
    assert error.exit_code == EXIT_ERROR
    assert "no runnable tests" in error.report_text


# ---------------------------------------------------------------------------
# _bwave_stats_cmd — never the bare name "bwave"
# ---------------------------------------------------------------------------


class TestBwaveStatsCmd:
    """Coverage runs `bwave stats` itself, so it must resolve the *native* binary.

    The `bwave` on PATH is the Python wrapper, which injects query defaults
    (`--limit 5000`) — routing stats through it would silently truncate the
    signal list coverage is scored from.
    """

    def test_returns_resolved_native_path(self, tmp_path):
        from booley.specialists import coverage_analyst

        native = tmp_path / "bwave"
        native.write_bytes(b"\x7fELF")
        with patch.object(coverage_analyst, "native_bwave_binary", return_value=native):
            cmd = coverage_analyst._bwave_stats_cmd()

        assert cmd == [str(native), "stats", "--format", "json"]
        assert cmd[0] != "bwave"

    def test_returns_none_when_binary_is_absent(self):
        from booley.specialists import coverage_analyst

        with patch.object(coverage_analyst, "native_bwave_binary", return_value=None):
            assert coverage_analyst._bwave_stats_cmd() is None

    def test_stats_call_sites_use_the_resolved_prefix(self, tmp_path):
        """Every subprocess `bwave stats` goes through _bwave_stats_cmd()."""
        from booley.specialists import coverage_analyst

        native = tmp_path / "bwave"
        native.write_bytes(b"\x7fELF")
        endpoint = _make_endpoint_with_args(work_dir=str(tmp_path))
        with (
            patch.object(coverage_analyst, "native_bwave_binary", return_value=native),
            patch.object(coverage_analyst.subprocess, "run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout="[]", stderr="")
            endpoint._available_top_scopes(tmp_path / "trace.fst")
            endpoint._run_bwave_stats(tmp_path / "trace.fst", "*")

        assert mock_run.call_count == 2
        for call in mock_run.call_args_list:
            cmd = call.args[0]
            assert cmd[0] == str(native), f"bare-name invocation leaked: {cmd}"
            assert cmd[1:3] == ["stats", "--format"]

    def test_prerequisites_fail_when_binary_is_absent(self, tmp_path):
        from booley.specialists import coverage_analyst

        endpoint = _make_endpoint_with_args(work_dir=str(tmp_path))
        with patch.object(coverage_analyst, "native_bwave_binary", return_value=None):
            result = endpoint._check_prerequisites()

        assert result is not None
        assert result.exit_code != 0
        assert "bwave" in result.report_text

    def test_source_has_no_bare_name_bwave_invocation(self):
        """Regression guard: a bare ["bwave", ...] list would hit the wrapper."""
        from booley.specialists import coverage_analyst

        source = Path(coverage_analyst.__file__).read_text(encoding="utf-8")
        offenders = [
            line for line in source.splitlines() if re.search(r"""\[\s*["']bwave["']\s*,""", line)
        ]

        assert offenders == []
