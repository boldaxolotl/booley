"""Tests for the per-Specialist dut_info gate (ADR 0009 / Phase 4).

Verifies that Specialists / Endpoints declaring ``required_dut_info_halves``
refuse to run when the corresponding halves of ``state.dut_info`` are
unpopulated, and proceed otherwise.

ADR 0022 (dec 12-13) shrank ``DutInfo`` to the DUT-identification overlay
(``dut_top_module``, ``dut_hier_path``, ``interface``) and the fusesoc merge
deleted the coder/planner/debugger/sva_coder specialists. The surviving
gate-bearing specialists are ``simulate`` and ``coverage_analyst`` (both
halves) and ``mutation_tester`` (dut half); ``tb_coder`` and ``reviewer``
declare no gate.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from booley.dev_support.development_state import DevelopmentState, DutInfo
from booley.flows.sim.flow import SimulateFlow
from booley.mcp.base import EXIT_FAILURE, EXIT_SUCCESS, McpToolResult
from booley.specialists.coverage_analyst import CoverageAnalystSpecialist
from booley.specialists.mutation_tester import MutationTesterSpecialist
from booley.specialists.reviewer import ReviewerSpecialist
from booley.specialists.tb_coder import TbCoderSpecialist

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _new_state(tmp_path: Path, *, dut_info: DutInfo | None = None) -> Path:
    """Create an on-disk state file with the given dut_info (or empty)."""
    sf = tmp_path / "state.json"
    st = DevelopmentState.load(sf)
    st.slug = "gate-test"
    if dut_info is not None:
        st.dut_info = dut_info
    st.save()
    return sf


def _full_dut_info() -> DutInfo:
    """A fully populated dut_info — both halves set."""
    return DutInfo(
        dut_top_module="foo",
        dut_hier_path="tb_foo.dut",
    )


def _dut_only_info() -> DutInfo:
    """Only the DUT half populated (TB ``dut_hier_path`` not yet set)."""
    return DutInfo(dut_top_module="foo")


# ---------------------------------------------------------------------------
# required_dut_info_halves declarations
# ---------------------------------------------------------------------------


class TestRequiredHalvesDeclarations:
    """Sanity-check that each surviving Specialist declares the expected halves."""

    def test_tb_coder_requires_nothing(self):
        # tb_coder is tb-permanent and the planner gate was retired with it.
        assert TbCoderSpecialist().required_dut_info_halves() == frozenset()

    def test_reviewer_requires_nothing(self):
        # Reviewer inherits the base default (no gate).
        assert ReviewerSpecialist().required_dut_info_halves() == frozenset()

    def test_mutation_tester_requires_dut(self):
        assert MutationTesterSpecialist().required_dut_info_halves() == frozenset({"dut"})

    def test_simulate_requires_both(self):
        assert SimulateFlow().required_dut_info_halves() == frozenset({"dut", "tb"})

    def test_coverage_analyst_requires_both(self):
        assert CoverageAnalystSpecialist().required_dut_info_halves() == frozenset({"dut", "tb"})


# ---------------------------------------------------------------------------
# Gate enforcement via main()
# ---------------------------------------------------------------------------


class TestGateBlocks:
    """Gate must reject recoverably when required halves are unpopulated.

    ``simulate`` gates on both halves, so it is the cleanest driver: an empty
    dut_info blocks on the DUT half, and a dut-only dut_info blocks on the TB
    half.  ``mutation_tester`` gates on the DUT half alone.
    """

    def test_simulate_with_empty_state_errors(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        sf = _new_state(tmp_path)  # empty dut_info → DUT half missing
        monkeypatch.setenv("BOOLEY_SLUG", "gate-test")
        monkeypatch.setenv("BOOLEY_STATE_FILE", str(sf))

        endpoint = SimulateFlow()
        exit_code = endpoint.main(
            [
                "--work-dir",
                str(tmp_path),
                "--target",
                "default",
            ]
        )
        assert exit_code == EXIT_FAILURE

    def test_simulate_with_only_dut_half_errors(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        # DUT half present but TB half (dut_hier_path) missing → blocks on tb.
        sf = _new_state(tmp_path, dut_info=_dut_only_info())
        monkeypatch.setenv("BOOLEY_SLUG", "gate-test")
        monkeypatch.setenv("BOOLEY_STATE_FILE", str(sf))

        endpoint = SimulateFlow()
        exit_code = endpoint.main(
            [
                "--work-dir",
                str(tmp_path),
                "--target",
                "default",
            ]
        )
        assert exit_code == EXIT_FAILURE

    def test_mutation_tester_with_empty_state_errors(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        sf = _new_state(tmp_path)  # empty dut_info → DUT half missing
        monkeypatch.setenv("BOOLEY_SLUG", "gate-test")
        monkeypatch.setenv("BOOLEY_STATE_FILE", str(sf))

        endpoint = MutationTesterSpecialist()
        exit_code = endpoint.main(
            [
                "--work-dir",
                str(tmp_path),
                "--scope",
                "rtl/foo.sv",
            ]
        )
        assert exit_code == EXIT_FAILURE


class TestGateAllows:
    """Gate must not fire when state has no file (human mode) or halves present."""

    def test_human_mode_no_state_file_skips_gate(self, tmp_path: Path):
        # No BOOLEY_STATE_FILE set — gate must short-circuit.  We can't run
        # the real specialist (it would invoke a simulator), so we go through
        # main with a mocked _run().
        endpoint = SimulateFlow()
        with patch.object(
            SimulateFlow,
            "_run",
            return_value=McpToolResult(exit_code=EXIT_SUCCESS),
        ):
            exit_code = endpoint.main(
                [
                    "--work-dir",
                    str(tmp_path),
                    "--target",
                    "default",
                ]
            )
        # _run() mocked to succeed — if the gate had fired we'd see EXIT_FAILURE.
        assert exit_code == EXIT_SUCCESS

    def test_simulate_with_full_dut_info_proceeds(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        sf = _new_state(tmp_path, dut_info=_full_dut_info())
        monkeypatch.setenv("BOOLEY_SLUG", "gate-test")
        monkeypatch.setenv("BOOLEY_STATE_FILE", str(sf))

        endpoint = SimulateFlow()
        # Stub the actual sim execution — we only want to verify the gate let it through.
        with patch.object(
            SimulateFlow,
            "_run",
            return_value=McpToolResult(exit_code=EXIT_SUCCESS),
        ):
            exit_code = endpoint.main(
                [
                    "--work-dir",
                    str(tmp_path),
                    "--target",
                    "default",
                ]
            )
        assert exit_code == EXIT_SUCCESS

    def test_simulate_with_unmet_review_criterion_proceeds(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        sf = _new_state(tmp_path, dut_info=_full_dut_info())
        state = DevelopmentState.load(sf)
        state.init_criteria({"review_tb_quality_clean": True, "sim_pass_default": True})
        state.dut_info = _full_dut_info()
        state.save()
        monkeypatch.setenv("BOOLEY_SLUG", "gate-test")
        monkeypatch.setenv("BOOLEY_STATE_FILE", str(sf))

        endpoint = SimulateFlow()
        with patch.object(
            SimulateFlow,
            "_run",
            return_value=McpToolResult(exit_code=EXIT_SUCCESS),
        ):
            exit_code = endpoint.main(
                [
                    "--work-dir",
                    str(tmp_path),
                    "--target",
                    "default",
                ]
            )
        assert exit_code == EXIT_SUCCESS

    def test_mutation_tester_with_dut_half_proceeds(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        # mutation_tester gates on the DUT half only — a dut-only dut_info
        # satisfies it.
        sf = _new_state(tmp_path, dut_info=_dut_only_info())
        monkeypatch.setenv("BOOLEY_SLUG", "gate-test")
        monkeypatch.setenv("BOOLEY_STATE_FILE", str(sf))

        endpoint = MutationTesterSpecialist()
        with patch.object(
            MutationTesterSpecialist,
            "_run",
            return_value=McpToolResult(exit_code=EXIT_SUCCESS),
        ):
            exit_code = endpoint.main(
                [
                    "--work-dir",
                    str(tmp_path),
                    "--scope",
                    "rtl/foo.sv",
                ]
            )
        assert exit_code == EXIT_SUCCESS


# ---------------------------------------------------------------------------
# Auto-defaulting of dut_info args (follow-up Phase 3)
# ---------------------------------------------------------------------------


class TestDefaultDutInfoArgs:
    """Endpoint.main() must fill trace-relevant args from state when omitted."""

    def _capture(self, endpoint, argv, sf, monkeypatch):
        monkeypatch.setenv("BOOLEY_SLUG", "gate-test")
        monkeypatch.setenv("BOOLEY_STATE_FILE", str(sf))
        captured: dict[str, object] = {}

        def fake_run(self_):
            captured["tb_top"] = getattr(self_.args, "tb_top", None)
            captured["trace_scope"] = getattr(self_.args, "trace_scope", None)
            captured["dut_top"] = getattr(self_.args, "dut_top", None)
            return McpToolResult(exit_code=EXIT_SUCCESS)

        with patch.object(type(endpoint), "_run", fake_run):
            endpoint.main(argv)
        return captured

    def test_simulate_sources_neither_tb_top_nor_trace_scope(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        # tb_top left the surface (ADR 0021 — sourced from the resolved Target)
        # and --trace-scope left it too (ADR 0022 — the --trace overlay traces the
        # full hierarchy). So _default_dut_info_args fills nothing on simulate:
        # it carries neither attr, so both stay absent.
        sf = _new_state(tmp_path, dut_info=_full_dut_info())
        captured = self._capture(
            SimulateFlow(),
            ["--work-dir", str(tmp_path), "--target", "default"],
            sf,
            monkeypatch,
        )
        assert captured["tb_top"] is None
        assert captured["trace_scope"] is None

    def test_mutation_tester_fills_dut_top_from_state(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        # mutation_tester carries --dut-top; with the half gated and present,
        # _default_dut_info_args sources it from state.dut_info.dut_top_module.
        sf = _new_state(tmp_path, dut_info=_full_dut_info())
        captured = self._capture(
            MutationTesterSpecialist(),
            ["--work-dir", str(tmp_path), "--scope", "rtl/foo.sv"],
            sf,
            monkeypatch,
        )
        assert captured["dut_top"] == "foo"
