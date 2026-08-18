"""Boundary-validation tests (Coding Principle 5) for ticket_board.

Every case here feeds MALFORMED external input — non-object JSON, wrong-typed
JSON/JSONL fields, or mistyped TOML tables — through a parser and asserts the
code degrades gracefully instead of raising AttributeError/TypeError/ValueError
from a blind ``.get()``/index/``int()``/``float()``.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "src"))

from booley.ticket_board import analytics, execution, logs, notifications, scanner, validation
from booley.ticket_board.paths import runtime_file


def _write_state(logs_dir: Path, slug: str, text: str) -> Path:
    """Write raw booley_state.json content to a ticket's runtime dir."""
    path = runtime_file(logs_dir, slug, "booley_state.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# scanner: booley_state.json loading + summaries
# ---------------------------------------------------------------------------


class TestScannerStateGuards:
    def test_load_state_data_rejects_non_object(self, tmp_path):
        path = tmp_path / "s.json"
        path.write_text("[1, 2, 3]", encoding="utf-8")  # valid JSON, wrong shape
        assert scanner._load_state_data(path) is None

    def test_load_state_data_rejects_malformed_json(self, tmp_path):
        path = tmp_path / "s.json"
        path.write_text("{not json", encoding="utf-8")
        assert scanner._load_state_data(path) is None

    def test_load_state_data_accepts_object(self, tmp_path):
        path = tmp_path / "s.json"
        path.write_text('{"slug": "x"}', encoding="utf-8")
        assert scanner._load_state_data(path) == {"slug": "x"}

    def test_criteria_summary_handles_non_dict_criteria(self):
        # criteria arriving as a list must not crash .values()/len()
        assert scanner._load_criteria_summary({"criteria": ["a", "b"]}) is None

    def test_timeline_summary_handles_non_list_timeline(self):
        assert scanner._load_timeline_summary({"timeline": {"bad": 1}}) is None

    def test_timeline_summary_handles_non_dict_last_entry(self):
        assert scanner._load_timeline_summary({"timeline": ["not-a-dict"]}) is None

    def test_timeline_summary_happy_path(self):
        out = scanner._load_timeline_summary({"timeline": [{"flow": "sim", "timestamp": "t1"}]})
        assert out == {"last_endpoint": "sim", "endpoints_run": 1, "last_update": "t1"}


# ---------------------------------------------------------------------------
# validation: _validate_state_file
# ---------------------------------------------------------------------------


class TestValidateStateFile:
    def test_non_object_json(self, tmp_path):
        path = tmp_path / "booley_state.json"
        path.write_text('"just a string"', encoding="utf-8")
        out = validation._validate_state_file(path)
        assert out == [{"step": "developer", "message": "booley_state.json is not an object"}]

    def test_malformed_json(self, tmp_path):
        path = tmp_path / "booley_state.json"
        path.write_text("{oops", encoding="utf-8")
        out = validation._validate_state_file(path)
        assert out == [{"step": "developer", "message": "booley_state.json is unreadable"}]

    def test_non_dict_criteria_does_not_crash(self, tmp_path):
        # criteria as a list would blow up the .items() iteration if unguarded
        path = tmp_path / "booley_state.json"
        path.write_text(json.dumps({"slug": "x", "criteria": [1, 2]}), encoding="utf-8")
        assert validation._validate_state_file(path) == []

    def test_visible_criteria_without_mandatory_flags(self, tmp_path):
        path = tmp_path / "booley_state.json"
        path.write_text(
            json.dumps({"slug": "x", "criteria": {"c1": {"met": True}}}),
            encoding="utf-8",
        )
        out = validation._validate_state_file(path)
        assert any("none are mandatory" in f["message"] for f in out)


# ---------------------------------------------------------------------------
# notifications: ntfy_review_digest
# ---------------------------------------------------------------------------


class TestReviewDigest:
    def test_non_object_json(self, tmp_path):
        _write_state(tmp_path, "t", "42")
        assert notifications.ntfy_review_digest(tmp_path, "t") == ""

    def test_malformed_json(self, tmp_path):
        _write_state(tmp_path, "t", "{bad")
        assert notifications.ntfy_review_digest(tmp_path, "t") == ""

    def test_non_dict_criteria_and_timeline(self, tmp_path):
        _write_state(tmp_path, "t", json.dumps({"criteria": ["x"], "timeline": "nope"}))
        assert notifications.ntfy_review_digest(tmp_path, "t") == ""

    def test_non_numeric_cost_ignored(self, tmp_path):
        _write_state(
            tmp_path,
            "t",
            json.dumps({"timeline": [{"cost_usd": "free"}, {"cost_usd": 1.5}]}),
        )
        assert notifications.ntfy_review_digest(tmp_path, "t") == "$1.50"

    def test_happy_path(self, tmp_path):
        _write_state(
            tmp_path,
            "t",
            json.dumps(
                {
                    "criteria": {"c": {"detail": {"tests_total": 4, "tests_passed": 3}}},
                    "timeline": [{"cost_usd": 2.0}],
                }
            ),
        )
        digest = notifications.ntfy_review_digest(tmp_path, "t")
        assert "3P/1F sim" in digest
        assert "$2.00" in digest

    @pytest.mark.parametrize(
        "status, expected", [("ready", "triage ready"), ("failed", "triage report failed")]
    )
    def test_triage_report_status(self, tmp_path, status, expected):
        _write_state(tmp_path, "t", json.dumps({"criteria": {}, "timeline": []}))
        manifest = tmp_path / "t" / ".runtime" / "triage-prep" / "manifest.json"
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text(json.dumps({"status": status}), encoding="utf-8")

        assert notifications.ntfy_review_digest(tmp_path, "t") == expected


# ---------------------------------------------------------------------------
# analytics: parse_usage_log (JSONL)
# ---------------------------------------------------------------------------


class TestParseUsageLog:
    def test_skips_non_object_lines(self, tmp_path):
        path = tmp_path / "usage.jsonl"
        path.write_text('123\n[1,2]\n"str"\n', encoding="utf-8")
        assert analytics.parse_usage_log(path) == []

    def test_skips_malformed_line(self, tmp_path):
        path = tmp_path / "usage.jsonl"
        path.write_text('{bad json\n{"stage": "sim"}\n', encoding="utf-8")
        out = analytics.parse_usage_log(path)
        assert len(out) == 1 and out[0]["stage"] == "sim"

    def test_coerces_non_numeric_tokens_to_zero(self, tmp_path):
        path = tmp_path / "usage.jsonl"
        path.write_text(
            json.dumps({"stage": "sim", "input_tokens": "abc", "cost_usd": "free"}) + "\n",
            encoding="utf-8",
        )
        out = analytics.parse_usage_log(path)
        assert out[0]["input_tokens"] == 0
        assert out[0]["cost_usd"] == 0.0

    def test_numeric_strings_still_coerced(self, tmp_path):
        path = tmp_path / "usage.jsonl"
        path.write_text(
            json.dumps({"stage": "sim", "output_tokens": "12", "cost_usd": "1.25"}) + "\n",
            encoding="utf-8",
        )
        out = analytics.parse_usage_log(path)
        assert out[0]["output_tokens"] == 12
        assert out[0]["cost_usd"] == 1.25


# ---------------------------------------------------------------------------
# execution: TOML config readers
# ---------------------------------------------------------------------------


def _write_toml(project_root: Path, text: str) -> None:
    d = project_root / ".booley_project"
    d.mkdir(parents=True, exist_ok=True)
    (d / "booley.toml").write_text(text, encoding="utf-8")


class TestExecutionTomlGuards:
    def test_tb_prefixes_scalar_sources(self, tmp_path):
        _write_toml(tmp_path, 'sources = "oops"\n')
        assert execution.tb_source_prefixes(tmp_path) == ["tb/"]

    def test_tb_prefixes_scalar_testbench_section(self, tmp_path):
        _write_toml(tmp_path, "[sources]\ntestbench = 3\n")
        assert execution.tb_source_prefixes(tmp_path) == ["tb/"]

    def test_tb_prefixes_non_list_source_dirs(self, tmp_path):
        _write_toml(tmp_path, '[sources.testbench]\nsource_dirs = "tb"\n')
        assert execution.tb_source_prefixes(tmp_path) == ["tb/"]

    def test_tb_prefixes_happy_path(self, tmp_path):
        # ADR 0026: TB prefixes come from the .core tags:[tb] partition. A tb
        # fileset listing files under verif/ and tb/ yields their parent dirs,
        # sorted (tb/ precedes verif/).
        (tmp_path / "design.core").write_text(
            "CAPI=2:\n"
            "name: ::demo\n"
            "filesets:\n"
            "  tb:\n"
            "    files:\n"
            "      - verif/tb_top.sv: {file_type: systemVerilogSource}\n"
            "      - tb/tb_extra.sv: {file_type: systemVerilogSource}\n"
            "    tags: [tb]\n"
            "targets:\n"
            "  sim: {filesets: [tb], toplevel: tb_top}\n",
            encoding="utf-8",
        )
        assert execution.tb_source_prefixes(tmp_path) == ["tb/", "verif/"]

    def test_tb_prefixes_flat_repo_files(self, tmp_path):
        # ADR 0026 flat repo: TB entries are the files themselves at the root
        # (e.g. picorv32's testbench.v). They must yield an *exact-match* prefix
        # with no trailing '/', else the criteria TB path "testbench.v" — which
        # never starts with "testbench.v/" — fails validation at enqueue.
        (tmp_path / "testbench.v").write_text("module tb; endmodule\n", encoding="utf-8")
        (tmp_path / "testbench_wb.v").write_text("module tb_wb; endmodule\n", encoding="utf-8")
        (tmp_path / "design.core").write_text(
            "CAPI=2:\n"
            "name: ::demo\n"
            "filesets:\n"
            "  tb:\n"
            "    files:\n"
            "      - testbench.v: {file_type: verilogSource}\n"
            "      - testbench_wb.v: {file_type: verilogSource}\n"
            "    tags: [tb]\n"
            "targets:\n"
            "  sim: {filesets: [tb], toplevel: tb}\n",
            encoding="utf-8",
        )
        prefixes = execution.tb_source_prefixes(tmp_path)
        assert "testbench.v" in prefixes and "testbench.v/" not in prefixes
        assert any("testbench.v".startswith(p) for p in prefixes)

    def test_disabled_steps_scalar_tools_section(self, tmp_path):
        _write_toml(tmp_path, 'tools = "oops"\n')
        assert execution.disabled_flow_steps(tmp_path) == set()


class TestValidationSourcePrefixes:
    def test_flat_repo_scope_uses_exact_file_prefixes(self, tmp_path):
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

        prefixes = validation._source_prefixes(tmp_path, "rtl", "rtl")
        assert "picorv32.v" in prefixes
        assert "picorv32.v/" not in prefixes
        assert validation._scope_hits_prefix(["./picorv32.v"], prefixes)
        assert not validation._scope_hits_prefix(["picorv32.v.bak"], prefixes)


# ---------------------------------------------------------------------------
# logs: _reset_progress_file
# ---------------------------------------------------------------------------


class TestResetProgressFile:
    def test_non_object_json_left_untouched(self, tmp_path):
        prog = tmp_path / "progress.json"
        prog.write_text("[1, 2, 3]", encoding="utf-8")  # wrong shape
        # Must not raise; file is left as-is because it is not a dict.
        logs._reset_progress_file(prog, "rtl-implement", None)
        assert json.loads(prog.read_text(encoding="utf-8")) == [1, 2, 3]

    def test_malformed_json_left_untouched(self, tmp_path):
        prog = tmp_path / "progress.json"
        prog.write_text("{bad", encoding="utf-8")
        logs._reset_progress_file(prog, "rtl-implement", None)
        assert prog.read_text(encoding="utf-8") == "{bad"


class TestCriteriaSummaryIgnoresInternalCriteria:
    """fpu F-43: `_report_submitted` is seeded by the harness, not the ticket
    author, and every other surface filters `_`-prefixed criteria out (see
    criteria_acceptance._compute_criteria_stats). Counting it here made
    `board show` read 5/5 against the same run's run.log block of 4/4."""

    @staticmethod
    def _state(**criteria):
        return {"criteria": {k: {"met": v} for k, v in criteria.items()}}

    def test_internal_criterion_is_excluded_from_the_count(self):
        state = self._state(sim_pass=True, lint_clean=True, _report_submitted=True)

        assert scanner._load_criteria_summary(state) == (2, 2)

    def test_partial_progress_is_reported_against_real_criteria_only(self):
        state = self._state(sim_pass=True, lint_clean=False, _report_submitted=True)

        assert scanner._load_criteria_summary(state) == (1, 2)

    def test_only_internal_criteria_reads_as_no_criteria(self):
        assert scanner._load_criteria_summary(self._state(_report_submitted=True)) is None

    def test_bare_boolean_criteria_still_supported(self):
        # Older state files stored the met flag directly, not as {"met": ...}.
        state = {"criteria": {"sim_pass": True, "_report_submitted": True}}

        assert scanner._load_criteria_summary(state) == (1, 1)
