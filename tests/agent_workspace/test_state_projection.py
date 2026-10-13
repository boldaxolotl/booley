"""Category state projection preserves findings and restores original bytes."""

from __future__ import annotations

import json
import os
import signal
from pathlib import Path

import pytest

from booley.agent_workspace import state_projection
from booley.agent_workspace.state_projection import (
    filter_state_file_for_category,
    project_state_for_category,
)


def _sample_state() -> dict:
    """Fixture: state with both RTL-side and TB-side review findings."""
    return {
        "slug": "demo-0001",
        "criteria": {
            "review_rtl_code_style_clean": {
                "met": False,
                "mandatory": True,
                "detail": {
                    "issues": 1,
                    "CRITICAL": 1,
                    "MAJOR": 0,
                    "verify_attempts": 0,
                    "pending": [
                        {
                            "file": "rtl/dut.v",
                            "line": 42,
                            "summary": "secret RTL implementation note",
                            "fix_suggestion": "rename port foo_i to bar_i",
                        },
                    ],
                    "resolved": [],
                    "checks": ["c1", "c2"],
                },
            },
            "review_tb_quality_clean": {
                "met": False,
                "mandatory": True,
                "detail": {
                    "issues": 2,
                    "MAJOR": 2,
                    "pending": [
                        {"file": "verif/tb.sv", "summary": "tb gap"},
                    ],
                },
            },
            "lint_clean_default": {
                "met": True,
                "mandatory": False,
                "detail": {
                    "warnings": 0,
                    "checks": ["lint_rule_naming_rtl_internals"],
                },
            },
            "sim_pass_default": {
                "met": False,
                "mandatory": True,
                "detail": {"tb_path": "verif/tb.sv"},
            },
            "_report_submitted": {"met": False, "mandatory": True},
        },
        "timeline": [
            {"mcp_tool": "coder", "endpoint_kind": "mcp_tool", "args": {"category": "rtl"}}
        ],
    }


class TestProjectStateForCategory:
    def test_tb_strips_rtl_review_findings(self):
        st = _sample_state()
        out = project_state_for_category(st, "tb")
        rtl_detail = out["criteria"]["review_rtl_code_style_clean"]["detail"]
        # Summary counts survive
        assert rtl_detail["CRITICAL"] == 1
        assert rtl_detail["issues"] == 1
        assert rtl_detail["verify_attempts"] == 0
        # Prose payload stripped
        assert "pending" not in rtl_detail
        assert "resolved" not in rtl_detail
        assert "checks" not in rtl_detail

    def test_tb_strips_lint_detail(self):
        # lint findings carry RTL signal/file names too
        out = project_state_for_category(_sample_state(), "tb")
        lint_detail = out["criteria"]["lint_clean_default"]["detail"]
        assert lint_detail["warnings"] == 0
        assert "checks" not in lint_detail

    def test_tb_preserves_own_review_findings(self):
        out = project_state_for_category(_sample_state(), "tb")
        tb_detail = out["criteria"]["review_tb_quality_clean"]["detail"]
        assert "pending" in tb_detail
        assert tb_detail["pending"][0]["summary"] == "tb gap"

    def test_rtl_strips_tb_review_findings(self):
        out = project_state_for_category(_sample_state(), "rtl")
        tb_detail = out["criteria"]["review_tb_quality_clean"]["detail"]
        assert "pending" not in tb_detail
        assert tb_detail["MAJOR"] == 2

    def test_rtl_preserves_rtl_review_findings(self):
        out = project_state_for_category(_sample_state(), "rtl")
        rtl_detail = out["criteria"]["review_rtl_code_style_clean"]["detail"]
        # RTL Specialists see their own findings in full
        assert "pending" in rtl_detail
        assert rtl_detail["pending"][0]["file"] == "rtl/dut.v"

    def test_preserves_timeline(self):
        out = project_state_for_category(_sample_state(), "tb")
        assert out["timeline"][0]["mcp_tool"] == "coder"

    def test_preserves_sim_and_underscore_criteria(self):
        # Cross-category and developer-internal criteria pass through.
        out = project_state_for_category(_sample_state(), "tb")
        assert out["criteria"]["sim_pass_default"]["detail"]["tb_path"] == "verif/tb.sv"
        assert "_report_submitted" in out["criteria"]

    def test_unknown_category_is_noop(self):
        st = _sample_state()
        out = project_state_for_category(st, "neither")
        assert out is st

    def test_non_dict_passes_through(self):
        assert project_state_for_category([], "tb") == []
        assert project_state_for_category("nope", "tb") == "nope"

    def test_idempotent(self):
        once = project_state_for_category(_sample_state(), "tb")
        twice = project_state_for_category(once, "tb")
        assert once == twice

    def test_original_state_unchanged(self):
        # Projection must not mutate the input — caller may still hold it.
        st = _sample_state()
        project_state_for_category(st, "tb")
        assert "pending" in st["criteria"]["review_rtl_code_style_clean"]["detail"]


# ---------------------------------------------------------------------------
# State file filtering — context manager round-trip
# ---------------------------------------------------------------------------


class TestFilterStateFileForCategory:
    def _write_state(self, path: Path) -> bytes:
        body = json.dumps(_sample_state(), indent=2).encode("utf-8")
        path.write_bytes(body)
        return body

    def test_filters_then_restores(self, tmp_path):
        sf = tmp_path / "booley_state.json"
        original = self._write_state(sf)

        with filter_state_file_for_category(sf, "tb") as applied:
            assert applied is True
            mid = json.loads(sf.read_text(encoding="utf-8"))
            # Filtered: RTL review pending stripped
            assert "pending" not in mid["criteria"]["review_rtl_code_style_clean"]["detail"]

        # Restored byte-for-byte
        assert sf.read_bytes() == original

    def test_restores_even_when_body_raises(self, tmp_path):
        sf = tmp_path / "booley_state.json"
        original = self._write_state(sf)

        with pytest.raises(RuntimeError), filter_state_file_for_category(sf, "tb"):
            raise RuntimeError("agent crashed")

        # Restored despite the exception
        assert sf.read_bytes() == original

    def test_restores_on_sigterm(self, tmp_path, monkeypatch):
        """A killed run must not leave the projected (lossy) state behind."""
        sf = tmp_path / "booley_state.json"
        original = self._write_state(sf)
        killed: list[tuple[int, int]] = []

        with filter_state_file_for_category(sf, "tb") as applied:
            assert applied is True
            handler = signal.getsignal(signal.SIGTERM)
            assert callable(handler)
            monkeypatch.setattr(
                state_projection.os,
                "kill",
                lambda pid, sig: killed.append((pid, sig)),
            )
            handler(signal.SIGTERM, None)
            assert sf.read_bytes() == original
            assert killed == [(os.getpid(), signal.SIGTERM)]

        assert sf.read_bytes() == original

    def test_skips_when_path_missing(self, tmp_path):
        sf = tmp_path / "does_not_exist.json"
        with filter_state_file_for_category(sf, "tb") as applied:
            assert applied is False

    def test_skips_when_path_none(self):
        with filter_state_file_for_category(None, "tb") as applied:
            assert applied is False

    def test_skips_unknown_category(self, tmp_path):
        sf = tmp_path / "booley_state.json"
        original = self._write_state(sf)
        with filter_state_file_for_category(sf, "neither") as applied:
            assert applied is False
        assert sf.read_bytes() == original

    def test_corrupt_json_is_passthrough(self, tmp_path):
        sf = tmp_path / "booley_state.json"
        sf.write_bytes(b"not json at all {")
        with filter_state_file_for_category(sf, "tb") as applied:
            assert applied is False
        # File untouched
        assert sf.read_bytes() == b"not json at all {"
