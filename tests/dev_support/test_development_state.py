"""Tests for DevelopmentState — load/save, criteria ops, category reset."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import ClassVar

import pytest

from booley.dev_support.development_state import (
    CATEGORY_RTL,
    CATEGORY_TB,
    CriterionEntry,
    DevelopmentState,
    _check_absolute_cap,
    _check_absolute_min,
)

# _coerce_metric / resolve_metric / min_gate_key live in threshold_eval now — the
# per-clock refactor stopped re-exporting the coercion helper through
# development_state (which now imports resolve_metric, not _coerce_metric).
from booley.flows.synth.threshold_eval import (
    _coerce_metric,
    min_gate_key,
    resolve_metric,
)


class TestCriterionEntry:
    def test_round_trip(self):
        e = CriterionEntry(
            met=True, mandatory=False, updated_at="2026-01-01T00:00:00Z", detail={"cells": 42}
        )
        d = e.to_dict()
        e2 = CriterionEntry.from_dict(d)
        assert e2.met is True
        assert e2.mandatory is False
        assert e2.detail == {"cells": 42}

    def test_defaults(self):
        e = CriterionEntry.from_dict({})
        assert e.met is False
        assert e.mandatory is True
        assert e.ever_met is False

    def test_ever_met_round_trip(self):
        e = CriterionEntry(met=False, mandatory=True, ever_met=True)
        d = e.to_dict()
        assert d["ever_met"] is True
        e2 = CriterionEntry.from_dict(d)
        assert e2.ever_met is True

    def test_ever_met_absent_in_old_data(self):
        """Backward compat: old state files without ever_met default to False."""
        e = CriterionEntry.from_dict({"met": True, "mandatory": True})
        assert e.ever_met is False

    def test_ever_met_not_serialized_when_false(self):
        e = CriterionEntry(met=False, mandatory=True, ever_met=False)
        d = e.to_dict()
        assert "ever_met" not in d


class TestDevelopmentStateLoadSave:
    def test_load_nonexistent(self, tmp_path: Path):
        state = DevelopmentState.load(tmp_path / "missing.json")
        assert state.slug == ""
        assert state.criteria == {}

    def test_save_and_load(self, tmp_path: Path):
        path = tmp_path / "state.json"
        st = DevelopmentState.load(path)
        st.slug = "test-ticket"
        st.ticket_type = "feature"
        st.init_criteria(
            {"lint_clean_lite": True, "sim_pass_lite": True, "review_rtl_bugs_done": True}
        )
        st.set_criterion("lint_clean_lite", True, detail={"warnings": 0})
        st.save()

        st2 = DevelopmentState.load(path)
        assert st2.slug == "test-ticket"
        assert st2.ticket_type == "feature"
        assert st2.is_met("lint_clean_lite") is True
        assert st2.criteria["lint_clean_lite"].detail == {"warnings": 0}
        assert st2.is_met("sim_pass_lite") is False
        assert st2.last_updated != ""

    def test_atomic_no_tmp_lingers(self, tmp_path: Path):
        path = tmp_path / "state.json"
        st = DevelopmentState.load(path)
        st.slug = "atomic-test"
        st.save()
        assert not path.with_suffix(".tmp").exists()
        assert path.exists()

    def test_corrupted_file_starts_fresh(self, tmp_path: Path):
        path = tmp_path / "state.json"
        path.write_text("not json {{{", encoding="utf-8")
        st = DevelopmentState.load(path)
        assert st.slug == ""
        assert st.criteria == {}

    def test_load_ignores_legacy_verification_lanes(self, tmp_path: Path):
        path = tmp_path / "state.json"
        path.write_text(
            json.dumps(
                {
                    "slug": "legacy",
                    "criteria": {},
                    "timeline": [],
                    "verification_lanes": {
                        "2": {"tb_files": ["verif/lane2/tb.sv"]},
                    },
                }
            ),
            encoding="utf-8",
        )

        loaded = DevelopmentState.load(path)
        loaded.save()
        saved = json.loads(path.read_text(encoding="utf-8"))

        assert loaded.slug == "legacy"
        assert "verification_lanes" not in saved


class TestMetricCoercion:
    """Gate metrics cross a tool/LLM boundary — non-numeric must not crash."""

    _MAP: ClassVar[dict[str, str]] = {"fmax": "fmax_mhz", "area": "area_um2"}

    def test_coerce_numeric_forms(self):
        assert _coerce_metric(5) == 5.0
        assert _coerce_metric(2.5) == 2.5
        assert _coerce_metric("3.5") == 3.5

    def test_coerce_rejects_non_numeric(self):
        assert _coerce_metric("oops") is None
        assert _coerce_metric(None) is None
        assert _coerce_metric([1]) is None

    def test_coerce_rejects_bool(self):
        # float(True) == 1.0 would be silent corruption, not a real metric.
        assert _coerce_metric(True) is None
        assert _coerce_metric(False) is None

    def test_absolute_cap_non_numeric_skips_instead_of_crashing(self):
        detail = {"area_um2": "not-a-number"}
        result = _check_absolute_cap("area_max", "_max", 100.0, detail, self._MAP)
        assert result["skipped"] is True
        assert result["pass"] is True

    def test_absolute_cap_numeric_string_compares(self):
        result = _check_absolute_cap("area_max", "_max", 100.0, {"area_um2": "50"}, self._MAP)
        assert result["pass"] is True
        assert result["value"] == 50.0

    def test_absolute_min_non_numeric_skips(self):
        result = _check_absolute_min("fmax_min", 200.0, {"fmax_mhz": "fast"}, self._MAP, {"fmax"})
        assert result["skipped"] is True

    def test_delta_non_numeric_current_skips(self, tmp_path: Path):
        state = DevelopmentState.load(tmp_path / "s.json")
        detail = {"area_um2": "huge"}
        baseline = {"area_um2": 100.0}
        result = state._check_delta(
            "area_increase_at_most",
            "area",
            10.0,
            detail,
            baseline,
            metric_map=self._MAP,
            mode="increase",
        )
        assert result["skipped"] is True

    def test_delta_zero_string_baseline_no_zero_division(self, tmp_path: Path):
        # "0" baseline coerces to 0.0 → treated as no-baseline, not a crash.
        state = DevelopmentState.load(tmp_path / "s.json")
        result = state._check_delta(
            "area_increase_at_most",
            "area",
            10.0,
            {"area_um2": 50.0},
            {"area_um2": "0"},
            metric_map=self._MAP,
            mode="increase",
        )
        assert result["skipped"] is True


class TestResolveMetric:
    """resolve_metric addresses flat, per-clock, and worst-clock-fallback metrics.

    Timing metrics have no whole-design scalar, so a flat timing threshold falls
    back to the timing-worst clock while an explicit ``<clock>.<sub>`` prefix
    reads that clock directly.
    """

    _MAP: ClassVar[dict[str, str]] = {
        "fmax_mhz": "fmax_mhz",
        "critical_path_ps": "critical_path_ps",
        "area": "area_um2",
    }

    def _multi_clock(self) -> dict:
        return {
            "per_clock": {
                "clk_i": {
                    "period_ns": 0.5,
                    "wns_ns": 0.0,
                    "whs_ns": 0.0,
                    "critical_path_ps": 500,
                    "fmax_mhz": 2000.0,
                },
                "clk_slow": {
                    "period_ns": 4.0,
                    "wns_ns": 0.0,
                    "whs_ns": 0.0,
                    "critical_path_ps": 4000,
                    "fmax_mhz": 250.0,
                },
            }
        }

    def test_explicit_per_clock_hit(self):
        value, label = resolve_metric(
            self._multi_clock(),
            "clk_slow.fmax_mhz",
            self._MAP,
        )
        assert value == 250.0
        assert label == "per_clock[clk_slow].fmax_mhz"

    def test_flat_timing_falls_back_to_worst_clock(self):
        # No stored scalar → flat fmax_mhz reads the timing-worst clock (clk_slow).
        value, label = resolve_metric(self._multi_clock(), "fmax_mhz", self._MAP)
        assert value == 250.0
        assert label == "fmax_mhz"

    def test_flat_non_timing_reads_detail_directly(self):
        value, label = resolve_metric({"area_um2": 1234}, "area", self._MAP)
        assert value == 1234.0
        assert label == "area_um2"

    def test_unknown_metric_returns_none_none(self):
        # Prefix maps to nothing → caller skips the param entirely.
        assert resolve_metric(self._multi_clock(), "bogus", self._MAP) == (None, None)

    def test_unknown_per_clock_sub_returns_none_none(self):
        assert resolve_metric(
            self._multi_clock(),
            "clk_i.bogus",
            self._MAP,
        ) == (None, None)

    def test_known_but_absent_flat_returns_none_with_label(self):
        # fmax_mhz is known, but neither a flat scalar nor any per_clock entry
        # exists → (None, label) so the caller emits a clean "not available" skip.
        value, label = resolve_metric({}, "fmax_mhz", self._MAP)
        assert value is None
        assert label == "fmax_mhz"

    def test_known_but_absent_per_clock_returns_none_with_label(self):
        # The named clock isn't in the per_clock map → (None, per-clock label).
        value, label = resolve_metric(
            self._multi_clock(),
            "clk_missing.fmax_mhz",
            self._MAP,
        )
        assert value is None
        assert label == "per_clock[clk_missing].fmax_mhz"


class TestMinGateKey:
    """min_gate_key picks the sub-metric a *_min gate checks against min_allowed."""

    def test_flat_prefix_is_itself(self):
        assert min_gate_key("fmax_mhz") == "fmax_mhz"

    def test_per_clock_prefix_returns_sub_metric(self):
        assert min_gate_key("clk_i.fmax_mhz") == "fmax_mhz"


class TestCriteriaOperations:
    @pytest.fixture
    def state(self, tmp_path: Path) -> DevelopmentState:
        path = tmp_path / "state.json"
        st = DevelopmentState.load(path)
        st.slug = "test"
        st.init_criteria(
            {
                "lint_clean_lite": True,
                "lint_clean_full": True,
                "sim_pass_lite": True,
                "review_rtl_bugs_done": True,
                "review_tb_quality_done": True,
            }
        )
        return st

    def test_all_mandatory_met_false_initially(self, state: DevelopmentState):
        assert state.all_mandatory_met() is False

    def test_all_mandatory_met_true(self, state: DevelopmentState):
        for key in state.criteria:
            state.set_criterion(key, True)
        assert state.all_mandatory_met() is True

    def test_unmet_mandatory(self, state: DevelopmentState):
        state.set_criterion("lint_clean_lite", True)
        unmet = state.unmet_mandatory()
        assert "lint_clean_lite" not in unmet
        assert "lint_clean_full" in unmet

    def test_set_unknown_criterion_creates_optional(self, state: DevelopmentState):
        state.set_criterion("custom_check", True)
        assert state.criteria["custom_check"].mandatory is False

    def test_unknown_criterion_does_not_warn(
        self,
        state: DevelopmentState,
        caplog,
    ):
        # Auto-creating an optional criterion for a tool-reported key is the
        # intended, benign path (e.g. a bare `simulate` run during setup that
        # yields `sim_pass_sim`). It must NOT emit a WARNING on every run.
        with caplog.at_level(logging.WARNING):
            state.set_criterion("sim_pass_sim", True)
        assert "sim_pass_sim" in state.criteria
        assert not any(
            "unknown criterion" in r.getMessage() and r.levelno >= logging.WARNING
            for r in caplog.records
        )

    def test_base_key_fallback_single_config(self, tmp_path: Path):
        """sim_pass_default maps to sim_pass when declared as flat scalar."""
        state_path = tmp_path / "booley_state.json"
        st = DevelopmentState.load(state_path)
        st.init_criteria({"sim_pass": True})
        st.set_criterion("sim_pass_default", True, detail={"tests_passed": 1, "tests_total": 1})
        assert st.criteria["sim_pass"].met is True
        assert st.criteria["sim_pass"].detail == {"tests_passed": 1, "tests_total": 1}
        assert "sim_pass_default" not in st.criteria

    def test_base_key_fallback_no_false_match(self, tmp_path: Path):
        """Fallback doesn't fire when the base key doesn't exist."""
        state_path = tmp_path / "booley_state.json"
        st = DevelopmentState.load(state_path)
        st.init_criteria({"other_criterion": True})
        st.set_criterion("sim_pass_default", True)
        # Should create as optional, not match other_criterion
        assert "sim_pass_default" in st.criteria
        assert st.criteria["sim_pass_default"].mandatory is False

    def test_reset_tb_clears_verify_attempts_on_unmet(self, state: DevelopmentState):
        """Coder changes should reset exhausted verify_attempts on unmet criteria.

        Regression: review_tb_quality_clean with verify_attempts=2 and met=False
        was skipped by reset_category, permanently blocking the reviewer even
        after the coder fixed the issues.
        """
        state.init_criteria({"review_tb_quality_clean": True})
        state.set_criterion(
            "review_tb_quality_clean",
            False,
            detail={
                "issues": 1,
                "issue_list": [{"severity": "MAJOR", "summary": "test"}],
                "verify_attempts": 2,
                "original_issues": 2,
            },
        )
        assert state.criteria["review_tb_quality_clean"].detail["verify_attempts"] == 2

        reset = state.reset_category(CATEGORY_TB)
        assert "review_tb_quality_clean" in reset
        assert "verify_attempts" not in state.criteria["review_tb_quality_clean"].detail
        # issue_list preserved for targeted re-verification
        assert "issue_list" in state.criteria["review_tb_quality_clean"].detail

    def test_reset_tb_marks_done_review_stale(self, state: DevelopmentState):
        """A code change immediately invalidates a completed advisory review."""
        state.set_criterion("review_tb_quality_done", True)
        reset = state.reset_category(CATEGORY_TB)
        assert "review_tb_quality_done" in reset
        assert state.is_met("review_tb_quality_done") is False
        assert state.criteria["review_tb_quality_done"].stale is True

    def test_reset_preserves_total_verify_cycles(self, state: DevelopmentState):
        """total_verify_cycles must survive reset_category — it tracks cumulative
        attempts for impasse detection and must not be cleared when code changes."""
        state.init_criteria({"review_tb_quality_clean": True})
        state.set_criterion(
            "review_tb_quality_clean",
            False,
            detail={
                "issues": 1,
                "issue_list": [{"severity": "MAJOR", "summary": "test"}],
                "verify_attempts": 2,
                "total_verify_cycles": 3,
                "original_issues": 1,
            },
        )
        reset = state.reset_category(CATEGORY_TB)
        assert "review_tb_quality_clean" in reset
        detail = state.criteria["review_tb_quality_clean"].detail
        assert "verify_attempts" not in detail
        assert detail["total_verify_cycles"] == 3
        assert "issue_list" in detail

    def test_reset_rtl_clears_verify_attempts_on_unmet(self, state: DevelopmentState):
        """Same as TB variant but for RTL category."""
        state.init_criteria({"review_rtl_bugs_clean": True})
        state.set_criterion(
            "review_rtl_bugs_clean",
            False,
            detail={
                "issues": 1,
                "issue_list": [{"severity": "MAJOR", "summary": "test"}],
                "verify_attempts": 2,
                "original_issues": 1,
            },
        )
        reset = state.reset_category(CATEGORY_RTL)
        assert "review_rtl_bugs_clean" in reset
        assert "verify_attempts" not in state.criteria["review_rtl_bugs_clean"].detail
        assert "issue_list" in state.criteria["review_rtl_bugs_clean"].detail

    def test_category_override(self, state: DevelopmentState):
        state.init_criteria(
            {"custom_gate": True},
            category_overrides={"custom_gate": CATEGORY_RTL},
        )
        state.set_criterion("custom_gate", True)
        reset = state.reset_category(CATEGORY_RTL)
        assert "custom_gate" in reset

    def test_summary(self, state: DevelopmentState):
        state.set_criterion("lint_clean_lite", True)
        s = state.summary()
        assert s["total"] == 5
        assert s["met"] == 1
        assert s["mandatory"] == 5
        assert s["mandatory_met"] == 1
        assert s["all_mandatory_met"] is False

    def test_ever_met_latches_on_set_true(self, state: DevelopmentState):
        state.set_criterion("lint_clean_lite", True)
        assert state.criteria["lint_clean_lite"].ever_met is True
        state.set_criterion("lint_clean_lite", False)
        assert state.criteria["lint_clean_lite"].ever_met is True

    def test_ever_met_survives_reset_category(self, state: DevelopmentState):
        # Use an immediately resettable criterion; review freshness is fingerprint-owned.
        state.set_criterion("lint_clean_lite", True)
        assert state.criteria["lint_clean_lite"].ever_met is True
        state.reset_category(CATEGORY_RTL)
        assert state.is_met("lint_clean_lite") is False
        assert state.criteria["lint_clean_lite"].ever_met is True

    def test_ever_met_set_on_unknown_criterion(self, state: DevelopmentState):
        state.set_criterion("dynamic_check", True)
        assert state.criteria["dynamic_check"].ever_met is True


class TestConcurrentSafeWrites:
    def test_sequential_saves_no_corruption(self, tmp_path: Path):
        """Two sequential save() calls don't corrupt data."""
        path = tmp_path / "state.json"
        st = DevelopmentState.load(path)
        st.slug = "concurrent-test"
        st.init_criteria({"lint_clean_lite": True, "sim_pass_lite": True})
        st.set_criterion("lint_clean_lite", True)
        st.save()

        st.set_criterion("sim_pass_lite", True)
        st.save()

        st2 = DevelopmentState.load(path)
        assert st2.is_met("lint_clean_lite") is True
        assert st2.is_met("sim_pass_lite") is True
        assert not path.with_suffix(".tmp").exists()

    def test_save_after_reload_preserves_state(self, tmp_path: Path):
        """Load -> modify -> save cycle preserves prior state."""
        path = tmp_path / "state.json"
        st = DevelopmentState.load(path)
        st.slug = "reload-test"
        st.init_criteria({"a": True, "b": True})
        st.set_criterion("a", True, detail={"info": "first"})
        st.save()

        st2 = DevelopmentState.load(path)
        st2.set_criterion("b", True, detail={"info": "second"})
        st2.save()

        st3 = DevelopmentState.load(path)
        assert st3.is_met("a") is True
        assert st3.criteria["a"].detail == {"info": "first"}
        assert st3.is_met("b") is True
        assert st3.criteria["b"].detail == {"info": "second"}


class TestTimeline:
    def test_record_flow_run(self, tmp_path: Path):
        path = tmp_path / "state.json"
        st = DevelopmentState.load(path)
        st.record_mcp_tool_run(
            "lint", 0, endpoint_kind="flow", duration_s=1.5, criteria_set=["lint_clean_lite"]
        )
        st.record_mcp_tool_run("sim", 1, endpoint_kind="flow", duration_s=30.0)
        assert len(st.timeline) == 2
        assert st.timeline[0]["flow"] == "lint"
        assert st.timeline[0]["exit_code"] == 0
        assert st.timeline[0]["duration_s"] == 1.5
        assert st.timeline[1]["flow"] == "sim"

    def test_timeline_persists(self, tmp_path: Path):
        path = tmp_path / "state.json"
        st = DevelopmentState.load(path)
        st.slug = "timeline-test"
        st.record_mcp_tool_run("lint", 0, endpoint_kind="flow")
        st.save()

        st2 = DevelopmentState.load(path)
        assert len(st2.timeline) == 1
        assert st2.timeline[0]["flow"] == "lint"


class TestPendingConsistencyAssertion:
    """Structural check: met=True with detail['pending'] non-empty is impossible.

    Guards against tool bugs (or compromised LLM verdicts) that report a
    criterion as met while still listing open findings.
    """

    def _state_with_criterion(self, key: str) -> DevelopmentState:
        st = DevelopmentState()
        st.init_criteria({key: True})
        return st

    def test_met_true_with_pending_items_forced_false(self, caplog):
        st = self._state_with_criterion("review_rtl_bugs_clean")
        with caplog.at_level("ERROR"):
            st.set_criterion(
                "review_rtl_bugs_clean",
                met=True,
                detail={
                    "issues": 1,
                    "pending": [{"severity": "CRITICAL", "summary": "still broken"}],
                    "resolved": [],
                },
            )
        assert st.criteria["review_rtl_bugs_clean"].met is False
        assert st.criteria["review_rtl_bugs_clean"].ever_met is False
        assert any("Refusing met=True" in rec.message for rec in caplog.records)

    def test_met_true_with_empty_pending_passes_through(self):
        st = self._state_with_criterion("review_rtl_bugs_clean")
        st.set_criterion(
            "review_rtl_bugs_clean",
            met=True,
            detail={
                "issues": 0,
                "pending": [],
                "resolved": [{"severity": "CRITICAL", "status": "fixed"}],
            },
        )
        assert st.criteria["review_rtl_bugs_clean"].met is True

    def test_met_true_without_pending_key_passes_through(self):
        """Tools that don't use the pending/resolved schema are unaffected."""
        st = self._state_with_criterion("review_rtl_bugs_done")
        st.set_criterion(
            "review_rtl_bugs_done",
            met=True,
            detail={
                "issues": 2,
                "issue_list": [{"severity": "MINOR", "summary": "nit"}],
            },
        )
        assert st.criteria["review_rtl_bugs_done"].met is True

    def test_met_false_with_pending_unaffected(self):
        """Setting met=False is fine regardless of pending — that's the normal failure path."""
        st = self._state_with_criterion("review_rtl_bugs_clean")
        st.set_criterion(
            "review_rtl_bugs_clean",
            met=False,
            detail={
                "issues": 1,
                "pending": [{"severity": "CRITICAL", "summary": "open"}],
                "resolved": [],
            },
        )
        assert st.criteria["review_rtl_bugs_clean"].met is False


class TestEverFailedLatch:
    """`ever_failed` records that a tool actually observed a failure (F-53)."""

    def test_unrun_criterion_has_neither_latch(self, tmp_path):
        state = DevelopmentState.load(tmp_path / "s.json")
        state.init_criteria({"sim_pass_default": True})
        entry = state.criteria["sim_pass_default"]
        assert entry.ever_met is False
        assert entry.ever_failed is False

    def test_reported_failure_latches_and_survives_a_later_pass(self, tmp_path):
        state = DevelopmentState.load(tmp_path / "s.json")
        state.init_criteria({"sim_pass_default": True})
        state.set_criterion("sim_pass_default", False)
        assert state.criteria["sim_pass_default"].ever_failed is True
        state.set_criterion("sim_pass_default", True)
        assert state.criteria["sim_pass_default"].ever_failed is True
        assert state.criteria["sim_pass_default"].ever_met is True

    def test_first_run_pass_leaves_ever_failed_false(self, tmp_path):
        state = DevelopmentState.load(tmp_path / "s.json")
        state.init_criteria({"sim_pass_default": True})
        state.set_criterion("sim_pass_default", True)
        assert state.criteria["sim_pass_default"].ever_failed is False

    def test_latch_round_trips_through_the_state_file(self, tmp_path):
        path = tmp_path / "s.json"
        state = DevelopmentState.load(path)
        state.init_criteria({"sim_pass_default": True})
        state.set_criterion("sim_pass_default", False)
        state.save()
        assert DevelopmentState.load(path).criteria["sim_pass_default"].ever_failed is True
