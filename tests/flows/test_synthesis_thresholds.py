"""Tests for synthesis_ok threshold evaluation in DevelopmentState.

Timing (``critical_path_ps``/``fmax_mhz``) is per-clock: the legacy top-level
scalars were removed, so every detail/baseline dict carries a ``per_clock`` map
in JSON form (``{clk: {period_ns, wns_ns, whs_ns, critical_path_ps, fmax_mhz}}``).
A *flat* timing threshold (``fmax_mhz_min``) resolves to the timing-worst clock,
so single-clock criteria keep gating exactly as before; a clock-scoped threshold
(``clk_i.fmax_mhz_min``) gates only that clock.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from booley.dev_support.development_state import DevelopmentState
from booley.flows.synthesis_recipe import RECIPE_FINGERPRINT_DETAIL, RECIPE_FINGERPRINT_PARAM


def _per_clock(critical_path_ps: float, fmax_mhz: float, clock: str = "clk_i") -> dict:
    """Build a single-clock ``per_clock`` JSON block for a detail/baseline dict."""
    return {
        clock: {
            "period_ns": None,
            "wns_ns": None,
            "whs_ns": None,
            "critical_path_ps": critical_path_ps,
            "fmax_mhz": fmax_mhz,
        }
    }


@pytest.fixture()
def state(tmp_path: Path) -> DevelopmentState:
    """Fresh state with a synthesis_ok_default criterion."""
    sf = tmp_path / "state.json"
    st = DevelopmentState.load(sf)
    st.slug = "test"
    return st


def _init_with_params(state: DevelopmentState, params: dict) -> None:
    """Initialize state with synthesis_ok_default criterion carrying params."""
    state.init_criteria(
        {"synthesis_ok_default": True},
        criterion_params={"synthesis_ok_default": params},
    )


class TestRecipeFingerprint:
    def test_matching_frozen_recipe_passes(self, state):
        _init_with_params(state, {RECIPE_FINGERPRINT_PARAM: "abc123"})
        state.set_criterion(
            "synthesis_ok_default",
            True,
            detail={RECIPE_FINGERPRINT_DETAIL: "abc123"},
        )
        assert state.is_met("synthesis_ok_default")
        assert state.criteria["synthesis_ok_default"].detail["checks"][0]["pass"] is True

    def test_changed_recipe_rejects_otherwise_passing_evidence(self, state):
        _init_with_params(state, {RECIPE_FINGERPRINT_PARAM: "frozen"})
        state.set_criterion(
            "synthesis_ok_default",
            True,
            detail={RECIPE_FINGERPRINT_DETAIL: "changed"},
        )
        assert not state.is_met("synthesis_ok_default")
        check = state.criteria["synthesis_ok_default"].detail["checks"][0]
        assert check["expected"] == "frozen"
        assert check["actual"] == "changed"


# ===========================================================================
# Absolute cap tests
# ===========================================================================


class TestAbsoluteCaps:
    def test_cell_count_max_pass(self, state):
        _init_with_params(state, {"cell_count_max": 500})
        state.set_criterion(
            "synthesis_ok_default",
            True,
            detail={
                "cells": 400,
                "area_um2": 1000,
                "wire_count": 100,
                "per_clock": _per_clock(500, 2000.0),
                "has_critical": False,
                "latches": 0,
                "comb_loops": 0,
                "multi_driven": 0,
                "process_count": 0,
            },
        )
        assert state.is_met("synthesis_ok_default")
        checks = state.criteria["synthesis_ok_default"].detail["checks"]
        assert checks[0]["pass"] is True

    def test_cell_count_max_fail(self, state):
        _init_with_params(state, {"cell_count_max": 500})
        state.set_criterion(
            "synthesis_ok_default",
            True,
            detail={
                "cells": 600,
                "area_um2": 1000,
                "wire_count": 100,
                "per_clock": _per_clock(500, 2000.0),
                "has_critical": False,
                "latches": 0,
                "comb_loops": 0,
                "multi_driven": 0,
                "process_count": 0,
            },
        )
        assert not state.is_met("synthesis_ok_default")
        checks = state.criteria["synthesis_ok_default"].detail["checks"]
        assert checks[0]["pass"] is False

    def test_wire_count_max_pass(self, state):
        _init_with_params(state, {"wire_count_max": 300})
        state.set_criterion(
            "synthesis_ok_default",
            True,
            detail={
                "cells": 100,
                "area_um2": 500,
                "wire_count": 200,
                "per_clock": _per_clock(500, 2000.0),
                "has_critical": False,
                "latches": 0,
                "comb_loops": 0,
                "multi_driven": 0,
                "process_count": 0,
            },
        )
        assert state.is_met("synthesis_ok_default")

    def test_area_um2_max_fail(self, state):
        _init_with_params(state, {"area_um2_max": 1000})
        state.set_criterion(
            "synthesis_ok_default",
            True,
            detail={
                "cells": 100,
                "area_um2": 1500,
                "wire_count": 100,
                "per_clock": _per_clock(500, 2000.0),
                "has_critical": False,
                "latches": 0,
                "comb_loops": 0,
                "multi_driven": 0,
                "process_count": 0,
            },
        )
        assert not state.is_met("synthesis_ok_default")

    def test_critical_path_ps_max_pass(self, state):
        # Flat critical-path cap resolves to the timing-worst clock.
        _init_with_params(state, {"critical_path_ps_max": 600})
        state.set_criterion(
            "synthesis_ok_default",
            True,
            detail={
                "cells": 100,
                "area_um2": 500,
                "wire_count": 100,
                "per_clock": _per_clock(500, 2000.0),
                "has_critical": False,
                "latches": 0,
                "comb_loops": 0,
                "multi_driven": 0,
                "process_count": 0,
            },
        )
        assert state.is_met("synthesis_ok_default")
        checks = state.criteria["synthesis_ok_default"].detail["checks"]
        assert checks[0]["value"] == 500.0

    def test_critical_path_ps_max_fail(self, state):
        _init_with_params(state, {"critical_path_ps_max": 400})
        state.set_criterion(
            "synthesis_ok_default",
            True,
            detail={
                "cells": 100,
                "area_um2": 500,
                "wire_count": 100,
                "per_clock": _per_clock(500, 2000.0),
                "has_critical": False,
                "latches": 0,
                "comb_loops": 0,
                "multi_driven": 0,
                "process_count": 0,
            },
        )
        assert not state.is_met("synthesis_ok_default")

    def test_fmax_mhz_min_pass(self, state):
        _init_with_params(state, {"fmax_mhz_min": 500})
        state.set_criterion(
            "synthesis_ok_default",
            True,
            detail={
                "cells": 100,
                "area_um2": 500,
                "wire_count": 100,
                "per_clock": _per_clock(1000, 1000.0),
                "has_critical": False,
                "latches": 0,
                "comb_loops": 0,
                "multi_driven": 0,
                "process_count": 0,
            },
        )
        assert state.is_met("synthesis_ok_default")

    def test_fmax_mhz_min_fail(self, state):
        _init_with_params(state, {"fmax_mhz_min": 500})
        state.set_criterion(
            "synthesis_ok_default",
            True,
            detail={
                "cells": 100,
                "area_um2": 500,
                "wire_count": 100,
                "per_clock": _per_clock(3333, 300.0),
                "has_critical": False,
                "latches": 0,
                "comb_loops": 0,
                "multi_driven": 0,
                "process_count": 0,
            },
        )
        assert not state.is_met("synthesis_ok_default")


# ===========================================================================
# Per-clock timing thresholds (explicit <clock>.<param>)
# ===========================================================================


class TestPerClockThresholds:
    """A clock-scoped threshold gates exactly one clock in a multi-clock design."""

    def _two_clock_detail(self) -> dict:
        # clk_i is fast (2000 MHz), clk_slow is slow (250 MHz) → they must be
        # gated independently.
        return {
            "cells": 100,
            "area_um2": 500,
            "wire_count": 100,
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
            },
            "has_critical": False,
            "latches": 0,
            "comb_loops": 0,
            "multi_driven": 0,
            "process_count": 0,
        }

    def test_per_clock_fmax_min_gates_named_clock_pass(self, state):
        # clk_i (2000 MHz) clears its own 1000 MHz floor even though clk_slow
        # (250 MHz) would fail a flat floor — scoping isolates clk_i.
        _init_with_params(state, {"clk_i.fmax_mhz_min": 1000})
        state.set_criterion("synthesis_ok_default", True, detail=self._two_clock_detail())
        assert state.is_met("synthesis_ok_default")
        checks = state.criteria["synthesis_ok_default"].detail["checks"]
        assert checks[0]["value"] == 2000.0

    def test_per_clock_fmax_min_gates_named_clock_fail(self, state):
        # Same floor, but pointed at the slow clock → fails on clk_slow alone.
        _init_with_params(state, {"clk_slow.fmax_mhz_min": 1000})
        state.set_criterion("synthesis_ok_default", True, detail=self._two_clock_detail())
        assert not state.is_met("synthesis_ok_default")
        checks = state.criteria["synthesis_ok_default"].detail["checks"]
        assert checks[0]["value"] == 250.0

    def test_per_clock_critical_path_max_gates_named_clock(self, state):
        # clk_i critical path 500ps is under a 600ps cap; clk_slow's 4000ps is
        # irrelevant because the cap names clk_i.
        _init_with_params(state, {"clk_i.critical_path_ps_max": 600})
        state.set_criterion("synthesis_ok_default", True, detail=self._two_clock_detail())
        assert state.is_met("synthesis_ok_default")

    def test_flat_fmax_min_resolves_to_worst_clock(self, state):
        # A flat floor gates the timing-worst clock (clk_slow @ 250 MHz), so a
        # 1000 MHz floor fails the whole design even though clk_i clears it.
        _init_with_params(state, {"fmax_mhz_min": 1000})
        state.set_criterion("synthesis_ok_default", True, detail=self._two_clock_detail())
        assert not state.is_met("synthesis_ok_default")
        checks = state.criteria["synthesis_ok_default"].detail["checks"]
        assert checks[0]["value"] == 250.0  # worst clock, not clk_i

    def test_per_clock_and_flat_coexist(self, state):
        # clk_i.fmax_mhz_min gates clk_i only; a separate area cap gates flat.
        _init_with_params(state, {"clk_i.fmax_mhz_min": 1000, "area_um2_max": 1000})
        state.set_criterion("synthesis_ok_default", True, detail=self._two_clock_detail())
        assert state.is_met("synthesis_ok_default")
        checks = state.criteria["synthesis_ok_default"].detail["checks"]
        assert len(checks) == 2

    def test_per_clock_increase_gates_named_clock(self, state):
        # Delta thresholds are per-clock too: clk_i critical path grew 20%.
        _init_with_params(state, {"clk_i.critical_path_ps_increase_at_most": 10})
        detail = self._two_clock_detail()
        detail["per_clock"]["clk_i"]["critical_path_ps"] = 600  # was 500 baseline
        detail["baseline_metrics"] = {
            "per_clock": _per_clock(500, 2000.0),
        }
        state.set_criterion("synthesis_ok_default", True, detail=detail)
        assert not state.is_met("synthesis_ok_default")
        checks = state.criteria["synthesis_ok_default"].detail["checks"]
        assert checks[0]["pct"] == pytest.approx(20.0)


# ===========================================================================
# Growth cap tests (increase_at_most)
# ===========================================================================


class TestGrowthCaps:
    def test_cell_count_increase_pass(self, state):
        _init_with_params(state, {"cell_count_increase_at_most": 10})
        state.set_criterion(
            "synthesis_ok_default",
            True,
            detail={
                "cells": 105,
                "area_um2": 500,
                "wire_count": 100,
                "per_clock": _per_clock(500, 2000.0),
                "has_critical": False,
                "latches": 0,
                "comb_loops": 0,
                "multi_driven": 0,
                "process_count": 0,
                "baseline_metrics": {
                    "cells": 100,
                    "area_um2": 500,
                    "wire_count": 100,
                    "per_clock": _per_clock(500, 2000.0),
                },
            },
        )
        assert state.is_met("synthesis_ok_default")

    def test_cell_count_increase_fail(self, state):
        _init_with_params(state, {"cell_count_increase_at_most": 10})
        state.set_criterion(
            "synthesis_ok_default",
            True,
            detail={
                "cells": 120,
                "area_um2": 500,
                "wire_count": 100,
                "per_clock": _per_clock(500, 2000.0),
                "has_critical": False,
                "latches": 0,
                "comb_loops": 0,
                "multi_driven": 0,
                "process_count": 0,
                "baseline_metrics": {
                    "cells": 100,
                    "area_um2": 500,
                    "wire_count": 100,
                    "per_clock": _per_clock(500, 2000.0),
                },
            },
        )
        assert not state.is_met("synthesis_ok_default")
        checks = state.criteria["synthesis_ok_default"].detail["checks"]
        failed = [c for c in checks if not c["pass"]]
        assert len(failed) == 1
        assert failed[0]["pct"] == pytest.approx(20.0)


# ===========================================================================
# Reduction floor tests (reduce_at_least)
# ===========================================================================


class TestReductionFloors:
    def test_cell_count_reduce_pass(self, state):
        _init_with_params(state, {"cell_count_reduce_at_least": 15})
        state.set_criterion(
            "synthesis_ok_default",
            True,
            detail={
                "cells": 80,
                "area_um2": 500,
                "wire_count": 100,
                "per_clock": _per_clock(500, 2000.0),
                "has_critical": False,
                "latches": 0,
                "comb_loops": 0,
                "multi_driven": 0,
                "process_count": 0,
                "baseline_metrics": {
                    "cells": 100,
                    "area_um2": 500,
                    "wire_count": 100,
                    "per_clock": _per_clock(500, 2000.0),
                },
            },
        )
        assert state.is_met("synthesis_ok_default")
        checks = state.criteria["synthesis_ok_default"].detail["checks"]
        assert checks[0]["pass"] is True
        assert checks[0]["pct"] == pytest.approx(20.0)

    def test_cell_count_reduce_fail(self, state):
        _init_with_params(state, {"cell_count_reduce_at_least": 25})
        state.set_criterion(
            "synthesis_ok_default",
            True,
            detail={
                "cells": 80,
                "area_um2": 500,
                "wire_count": 100,
                "per_clock": _per_clock(500, 2000.0),
                "has_critical": False,
                "latches": 0,
                "comb_loops": 0,
                "multi_driven": 0,
                "process_count": 0,
                "baseline_metrics": {
                    "cells": 100,
                    "area_um2": 500,
                    "wire_count": 100,
                    "per_clock": _per_clock(500, 2000.0),
                },
            },
        )
        assert not state.is_met("synthesis_ok_default")


# ===========================================================================
# Delta without baseline
# ===========================================================================


class TestDeltaWithoutBaseline:
    def test_skips_with_warning(self, state):
        _init_with_params(state, {"cell_count_reduce_at_least": 10})
        state.set_criterion(
            "synthesis_ok_default",
            True,
            detail={
                "cells": 80,
                "area_um2": 500,
                "wire_count": 100,
                "per_clock": _per_clock(500, 2000.0),
                "has_critical": False,
                "latches": 0,
                "comb_loops": 0,
                "multi_driven": 0,
                "process_count": 0,
                # No baseline_metrics!
            },
        )
        # Delta checks skipped → criterion still passes
        assert state.is_met("synthesis_ok_default")
        checks = state.criteria["synthesis_ok_default"].detail["checks"]
        assert checks[0]["skipped"] is True


# ===========================================================================
# Pass-only mode (no params)
# ===========================================================================


class TestPassOnlyMode:
    def test_no_params_only_implicit(self, state):
        state.init_criteria({"synthesis_ok_default": True})
        state.set_criterion(
            "synthesis_ok_default",
            True,
            detail={
                "cells": 100,
                "area_um2": 500,
                "wire_count": 100,
                "per_clock": _per_clock(500, 2000.0),
                "has_critical": False,
                "latches": 0,
                "comb_loops": 0,
                "multi_driven": 0,
                "process_count": 0,
            },
        )
        assert state.is_met("synthesis_ok_default")
        # No checks appended since no params
        assert "checks" not in state.criteria["synthesis_ok_default"].detail


# ===========================================================================
# Implicit fail overrides threshold pass
# ===========================================================================


class TestImplicitFailOverrides:
    def test_latches_fail_even_with_area_within_cap(self, state):
        _init_with_params(state, {"cell_count_max": 500})
        # The Flow would set met=False due to has_critical, but let's test
        # that threshold evaluation doesn't flip it back to True
        state.set_criterion(
            "synthesis_ok_default",
            False,
            detail={
                "cells": 100,
                "area_um2": 500,
                "wire_count": 100,
                "per_clock": _per_clock(500, 2000.0),
                "has_critical": True,
                "latches": 2,
                "comb_loops": 0,
                "multi_driven": 0,
                "process_count": 0,
            },
        )
        assert not state.is_met("synthesis_ok_default")


# ===========================================================================
# Params persistence via init_criteria
# ===========================================================================


class TestParamsPersistence:
    def test_params_stored_in_entry(self, state):
        _init_with_params(state, {"cell_count_max": 500})
        entry = state.criteria["synthesis_ok_default"]
        assert entry.params == {"cell_count_max": 500}

    def test_params_survive_save_load(self, state):
        _init_with_params(state, {"cell_count_max": 500})
        state.save()
        loaded = DevelopmentState.load(state._file_path)
        entry = loaded.criteria["synthesis_ok_default"]
        assert entry.params == {"cell_count_max": 500}
