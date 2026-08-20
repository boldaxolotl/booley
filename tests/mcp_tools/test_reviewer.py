"""Tests for ReviewerSpecialist -- issue parsing, gate logic, prompt construction."""

from __future__ import annotations

import contextlib
import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from booley.dev_support.development_state import DevelopmentState
from booley.specialists.reviewer import (
    RTL_FOCUS_CATEGORIES,
    SEVERITY_CRITICAL,
    SEVERITY_MAJOR,
    SEVERITY_MINOR,
    TB_FOCUS_CATEGORIES,
    ReviewerSpecialist,
    ReviewIssue,
    _validate_finding_dict,
    _validate_issue_dict,
    check_gate,
    count_by_severity,
    format_summary_line,
    parse_issues,
    parse_review_output,
    report_findings_to_issues,
    validate_scope_category,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _skip_workspace_snapshot_for_mocked_agents(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reviewer unit tests mock the agent; snapshot behavior has focused tests."""

    @contextlib.contextmanager
    def passthrough(params, _access, _category=None):
        yield params, None

    monkeypatch.setattr(
        "booley.specialists.specialist.isolated_agent_workspace",
        passthrough,
    )


@pytest.fixture()
def state_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Create a minimal state file and set env vars."""
    sf = tmp_path / "state.json"
    st = DevelopmentState.load(sf)
    st.slug = "test-review"
    st.save()
    monkeypatch.setenv("BOOLEY_SLUG", "test-review")
    monkeypatch.setenv("BOOLEY_STATE_FILE", str(sf))
    return sf


@pytest.fixture()
def review_endpoint() -> ReviewerSpecialist:
    return ReviewerSpecialist()


def _make_agent_result(issues: list[dict]) -> MagicMock:
    """Build a mock agent result with issues JSON."""
    result = MagicMock()
    result.output = json.dumps({"issues": issues})
    result.structured = None
    return result


def _make_issue_dict(
    severity: str = "MAJOR",
    category: str = "bugs",
    file: str = "rtl/mod_a.sv",
    line: int = 42,
    summary: str = "Test issue",
) -> dict:
    return {
        "severity": severity,
        "confidence": "HIGH",
        "category": category,
        "file": file,
        "line": line,
        "summary": summary,
        "fix_suggestion": "Fix it",
    }


# ---------------------------------------------------------------------------
# Issue parsing
# ---------------------------------------------------------------------------


class TestIssueParsing:
    def test_parse_issues_object(self):
        output = json.dumps(
            {
                "issues": [
                    _make_issue_dict("CRITICAL"),
                    _make_issue_dict("MINOR"),
                ]
            }
        )
        issues = parse_issues(output)
        assert len(issues) == 2
        assert issues[0].severity == "CRITICAL"
        assert issues[1].severity == "MINOR"

    def test_parse_issues_bare_array_rejected(self):
        """Bare arrays are not the spec'd wrapper format and are no longer
        accepted: the old greedy ``\\[.*\\]`` fallback swallowed prose like
        ``[MAJOR] ...`` headings, producing parse errors that masked real
        wrappers. Agents must emit ``{"issues": [...]}``."""
        output = json.dumps([_make_issue_dict("MAJOR")])
        issues = parse_issues(output)
        assert issues == []

    def test_parse_issues_embedded_in_text(self):
        output = (
            "Here is my review:\n```json\n"
            + json.dumps({"issues": [_make_issue_dict("CRITICAL")]})
            + "\n```\n"
        )
        issues = parse_issues(output)
        assert len(issues) == 1

    def test_parse_issues_empty(self):
        output = json.dumps({"issues": []})
        issues = parse_issues(output)
        assert issues == []

    def test_parse_issues_no_json(self):
        output = "No issues found, everything looks great!"
        issues = parse_issues(output)
        assert issues == []

    def test_parse_issues_invalid_json(self):
        output = "{issues: [broken json"
        issues = parse_issues(output)
        assert issues == []

    def test_issue_from_dict_defaults(self):
        issue = ReviewIssue.from_dict({})
        assert issue.severity == "MINOR"
        assert issue.confidence == "MEDIUM"
        assert issue.line == 0
        assert issue.file == ""

    def test_issue_to_dict_round_trip(self):
        original = ReviewIssue(
            severity="CRITICAL",
            confidence="HIGH",
            category="bugs",
            file="rtl/mod_a.sv",
            line=42,
            summary="Bad logic",
            fix_suggestion="Fix the mux",
        )
        d = original.to_dict()
        restored = ReviewIssue.from_dict(d)
        assert restored.severity == original.severity
        assert restored.file == original.file
        assert restored.line == original.line

    def test_issue_to_dict_omits_empty_fix(self):
        issue = ReviewIssue(
            severity="MINOR",
            confidence="LOW",
            category="quality",
            file="rtl/mod_b.sv",
            line=10,
            summary="Style",
        )
        d = issue.to_dict()
        assert "fix_suggestion" not in d


# ---------------------------------------------------------------------------
# Strict-schema validation: issue dicts
# ---------------------------------------------------------------------------


class TestIssueSchema:
    """Strict per-issue schema rejection — malformed entries dropped upstream."""

    def test_valid_issue_passes(self):
        assert _validate_issue_dict(_make_issue_dict("CRITICAL")) == []

    def test_unknown_severity_rejected(self):
        errs = _validate_issue_dict(_make_issue_dict("INFO"))
        assert any("severity" in e for e in errs)

    def test_lowercase_severity_accepted(self):
        # Validator is case-insensitive on the enum check — lowercase
        # normalizes cleanly. Strictness is on the enum *values*, not
        # the casing of the string.
        assert _validate_issue_dict(_make_issue_dict("critical")) == []

    def test_missing_confidence_rejected(self):
        d = _make_issue_dict("MAJOR")
        del d["confidence"]
        errs = _validate_issue_dict(d)
        assert any("confidence" in e for e in errs)

    def test_empty_file_rejected(self):
        d = _make_issue_dict("MAJOR", file="")
        errs = _validate_issue_dict(d)
        assert any("file" in e for e in errs)

    def test_negative_line_rejected(self):
        d = _make_issue_dict("MAJOR", line=-5)
        errs = _validate_issue_dict(d)
        assert any("line" in e for e in errs)

    def test_non_int_line_rejected(self):
        d = _make_issue_dict("MAJOR")
        d["line"] = "42"
        errs = _validate_issue_dict(d)
        assert any("line" in e for e in errs)

    def test_empty_summary_rejected(self):
        d = _make_issue_dict("MAJOR", summary="   ")
        errs = _validate_issue_dict(d)
        assert any("summary" in e for e in errs)

    def test_category_required(self):
        d = _make_issue_dict("MAJOR")
        del d["category"]
        errs = _validate_issue_dict(d)
        assert any("category" in e for e in errs)

    def test_mismatched_category_rejected_for_focus(self):
        d = _make_issue_dict("MAJOR", category="protocol")
        errs = _validate_issue_dict(d, allowed_category="bugs")
        assert any("exactly 'bugs'" in e for e in errs)

    def test_non_dict_rejected(self):
        assert _validate_issue_dict("not a dict") != []
        assert _validate_issue_dict([1, 2, 3]) != []

    def test_parse_review_output_drops_malformed_entries(self):
        output = json.dumps(
            {
                "issues": [
                    _make_issue_dict("CRITICAL"),
                    {"severity": "MAJOR"},  # missing required fields
                    _make_issue_dict("MINOR"),
                    {
                        "severity": "INFO",
                        "confidence": "HIGH",
                        "category": "x",
                        "file": "a.sv",
                        "line": 1,
                        "summary": "s",
                    },  # bad severity
                ],
            }
        )
        result = parse_review_output(output)
        assert result.json_present is True
        assert len(result.issues) == 2
        assert {i.severity for i in result.issues} == {"CRITICAL", "MINOR"}
        # Two rejected (indices 2 and 4 in 1-based ordering).
        assert {ord_idx for ord_idx, _ in result.rejected} == {2, 4}

    def test_parse_review_output_rejects_wrong_focus_category(self):
        output = json.dumps(
            {
                "issues": [
                    _make_issue_dict("MAJOR", category="bugs"),
                    _make_issue_dict("MAJOR", category="protocol"),
                ],
            }
        )
        result = parse_review_output(output, allowed_category="bugs")

        assert len(result.issues) == 1
        assert result.issues[0].category == "bugs"
        assert {ord_idx for ord_idx, _ in result.rejected} == {2}

    def test_parse_review_output_distinguishes_no_json(self):
        result = parse_review_output("Looks fine to me, no issues!")
        assert result.json_present is False
        assert result.issues == []

    def test_parse_review_output_zero_issues_is_json_present(self):
        result = parse_review_output(json.dumps({"issues": []}))
        assert result.json_present is True
        assert result.issues == []

    def test_parse_review_output_with_verilog_bitselect_in_spec_clause(self):
        """Regression: spec_clause quoting Verilog bit-selects must not
        confuse JSON wrapper extraction.

        The old regex-based extractor terminated early on the first
        ``]}`` substring it found, which appears inside spec_clause
        values like ``{{20{instr[31]}}, instr[31:20]}`` — causing
        c-static-branch-predict-0001 to block on a "no JSON wrapper"
        false negative even though the agent emitted a perfectly valid
        ``{"issues": [...]}``.
        """
        agent_output = (
            "### [MAJOR] JALR target uses uncited ambiguity reading\n"
            "**File:** `rtl/static_branch_predict.sv:67`\n"
            '**Spec clause:** "rd <-pc + {{20{instr[31]}}, '
            'instr[31:20]} + rs1"\n\n'
            "```json\n"
            "{\n"
            '  "issues": [\n'
            "    {\n"
            '      "severity": "MAJOR",\n'
            '      "confidence": "HIGH",\n'
            '      "category": "spec",\n'
            '      "file": "rtl/static_branch_predict.sv",\n'
            '      "line": 67,\n'
            '      "summary": "JALR target omits rs1",\n'
            '      "fix_suggestion": "Add SPEC-INTERPRETATION cite.",\n'
            '      "spec_clause": "rd <-pc + '
            '{{20{instr[31]}}, instr[31:20]} + rs1"\n'
            "    },\n"
            "    {\n"
            '      "severity": "MAJOR",\n'
            '      "confidence": "HIGH",\n'
            '      "category": "spec",\n'
            '      "file": "rtl/static_branch_predict.sv",\n'
            '      "line": 58,\n'
            '      "summary": "Inactive PC forced to zero",\n'
            '      "fix_suggestion": "Cite the ambiguity.",\n'
            '      "spec_clause": "predict_branch_pc_o is the target."\n'
            "    }\n"
            "  ]\n"
            "}\n"
            "```\n\n"
            "SPEC COMPLIANCE SUMMARY: 0 CRITICAL, 2 MAJOR, 0 MINOR"
        )
        result = parse_review_output(agent_output)
        assert result.json_present is True
        assert len(result.issues) == 2
        assert all(i.severity == "MAJOR" for i in result.issues)
        assert result.issues[0].line == 67
        assert result.issues[1].line == 58

    def test_parse_review_output_skips_malformed_prose_braces(self):
        """An earlier malformed ``{...}`` in prose must not mask a real
        wrapper that appears later in the output."""
        agent_output = (
            "Initial thought: something like { a stray { unbalanced ref }\n"
            "Actually here is the review:\n"
            '{"issues": [{"severity": "MINOR", "confidence": "LOW", '
            '"category": "quality", "file": "a.sv", "line": 1, '
            '"summary": "nit"}]}'
        )
        result = parse_review_output(agent_output)
        assert result.json_present is True
        assert len(result.issues) == 1
        assert result.issues[0].severity == "MINOR"

    def test_parse_review_output_brackets_in_string_dont_break_scan(self):
        """``]}`` inside a JSON string value is not a wrapper terminator."""
        issue = _make_issue_dict("MAJOR")
        issue["spec_clause"] = "value [a:b]} with embedded brackets"
        payload = json.dumps({"issues": [issue]})
        result = parse_review_output(payload)
        assert result.json_present is True
        assert len(result.issues) == 1


# ---------------------------------------------------------------------------
# Strict-schema validation: verify finding dicts
# ---------------------------------------------------------------------------


class TestFindingSchema:
    """Strict per-finding schema rejection for the verify pass."""

    def test_valid_fixed_with_evidence(self):
        d = {"index": 1, "status": "FIXED", "evidence": "a.sv:1 — fix"}
        assert _validate_finding_dict(d) == []

    def test_valid_still_present(self):
        assert (
            _validate_finding_dict(
                {"index": 2, "status": "STILL_PRESENT"},
            )
            == []
        )

    def test_fixed_without_evidence_rejected(self):
        d = {"index": 1, "status": "FIXED"}
        errs = _validate_finding_dict(d)
        assert len(errs) == 1
        assert "evidence" in errs[0]

    def test_fixed_with_blank_evidence_rejected(self):
        d = {"index": 1, "status": "FIXED", "evidence": "   "}
        errs = _validate_finding_dict(d)
        assert any("evidence" in e for e in errs)

    def test_waived_requires_non_blank_justification(self):
        assert (
            _validate_finding_dict(
                {"index": 1, "status": "WAIVED", "justification": "intentional tradeoff"}
            )
            == []
        )
        errs = _validate_finding_dict({"index": 1, "status": "WAIVED"})
        assert any("justification" in error for error in errs)

    def test_zero_index_rejected(self):
        errs = _validate_finding_dict({"index": 0, "status": "STILL_PRESENT"})
        assert any("index" in e for e in errs)

    def test_string_index_rejected(self):
        errs = _validate_finding_dict({"index": "1", "status": "STILL_PRESENT"})
        assert any("index" in e for e in errs)

    def test_unknown_status_rejected(self):
        errs = _validate_finding_dict({"index": 1, "status": "MAYBE"})
        assert any("status" in e for e in errs)

    def test_non_dict_rejected(self):
        assert _validate_finding_dict("FIXED") != []
        assert _validate_finding_dict(None) != []


# ---------------------------------------------------------------------------
# Upstream rejection: _run_single_review fails loud on missing JSON wrapper
# ---------------------------------------------------------------------------


def _make_report_findings_result(findings: list[dict], output: str = "") -> MagicMock:
    """Mock agent result that reported via the native ReportFindings endpoint.

    Mirrors a Claude-backend review: the findings arrive as a captured
    ``ReportFindings`` agent capability-call input. ``output`` is the agent's final text,
    which the prompt requires to carry the ``{"issues": [...]}`` mirror.
    """
    return MagicMock(
        output=output,
        structured=None,
        captured_agent_capability_calls={"ReportFindings": [{"findings": findings}]},
    )


class TestReportFindingsMapper:
    """Unit tests for the ReportFindings -> ReviewIssue mapping."""

    def test_confirmed_maps_to_blocking_major(self):
        issues, dropped = report_findings_to_issues(
            [
                {
                    "file": "rtl/a.sv",
                    "line": 7,
                    "summary": "off-by-one",
                    "verdict": "CONFIRMED",
                    "failure_scenario": "idx=N overflows",
                }
            ],
            focus="bugs",
        )
        assert dropped == 0
        assert len(issues) == 1
        iss = issues[0]
        assert iss.severity == SEVERITY_MAJOR
        assert iss.confidence == "HIGH"
        assert iss.category == "bugs"  # forced to focus, not RF's slug
        assert iss.file == "rtl/a.sv"
        assert iss.line == 7
        assert "idx=N overflows" in iss.fix_suggestion

    def test_absent_verdict_is_fail_closed_major(self):
        """A single-pass review usually omits verdict -> must still block."""
        issues, _ = report_findings_to_issues(
            [{"file": "rtl/a.sv", "line": 1, "summary": "x"}],
            focus="quality",
        )
        assert issues[0].severity == SEVERITY_MAJOR

    def test_plausible_maps_to_advisory_minor(self):
        issues, _ = report_findings_to_issues(
            [{"file": "rtl/a.sv", "line": 1, "summary": "maybe", "verdict": "PLAUSIBLE"}],
            focus="bugs",
        )
        assert issues[0].severity == SEVERITY_MINOR
        assert issues[0].confidence == "MEDIUM"

    def test_entries_missing_file_or_summary_dropped(self):
        issues, dropped = report_findings_to_issues(
            [
                {"file": "", "summary": "no file", "verdict": "CONFIRMED"},
                {"file": "rtl/a.sv", "summary": "", "verdict": "CONFIRMED"},
                {"file": "rtl/a.sv", "summary": "ok", "verdict": "CONFIRMED"},
                "not-a-dict",
            ],
            focus="bugs",
        )
        assert len(issues) == 1
        assert dropped == 3

    def test_bad_line_defaults_to_zero(self):
        issues, _ = report_findings_to_issues(
            [{"file": "rtl/a.sv", "line": "nope", "summary": "x"}],
            focus="bugs",
        )
        assert issues[0].line == 0


class TestReportFindingsCapture:
    """End-to-end: review agent reports via the native ReportFindings endpoint."""

    @patch("booley.specialists.specialist._call_agent_sync")
    def test_confirmed_finding_fails_gate(self, mock_agent, state_file: Path):
        """A CONFIRMED ReportFindings entry blocks the gate (exit 1, recorded)."""
        mock_agent.return_value = _make_report_findings_result(
            [
                {
                    "file": "rtl/mod_a.sv",
                    "line": 42,
                    "summary": "latch inferred",
                    "verdict": "CONFIRMED",
                },
            ]
        )
        endpoint = ReviewerSpecialist()
        endpoint.parse_args(
            [
                "--scope",
                "rtl/mod_a.sv",
                "--category",
                "rtl",
                "--focus",
                "bugs",
            ]
        )
        endpoint.read_state()
        result = endpoint._run()

        assert result.exit_code == 0  # _done completed; findings are reported separately
        assert result.detail["issues"] == 1
        st = DevelopmentState.load(state_file)
        # Criterion recorded as performed (not a Specialist error).
        assert st.has_criterion("review_rtl_bugs_done")

    @patch("booley.specialists.specialist._call_agent_sync")
    def test_empty_report_is_clean_pass(self, mock_agent, state_file: Path):
        """Zero findings in BOTH channels => clean pass, NOT exit 2.

        An empty agent-capability call is a legitimate clean review only when the text
        channel agrees (``{"issues": []}``) — an empty call on its own is no
        longer evidence of anything (SETUP-F-33).
        """
        mock_agent.return_value = _make_report_findings_result([], output='{"issues": []}')
        endpoint = ReviewerSpecialist()
        endpoint.parse_args(
            [
                "--scope",
                "rtl/mod_a.sv",
                "--category",
                "rtl",
                "--focus",
                "bugs",
            ]
        )
        endpoint.read_state()
        result = endpoint._run()

        assert result.exit_code == 0
        assert result.detail["issues"] == 0
        st = DevelopmentState.load(state_file)
        assert st.is_met("review_rtl_bugs_done")

    @patch("booley.specialists.specialist._call_agent_sync")
    def test_plausible_only_passes_gate(self, mock_agent, state_file: Path):
        mock_agent.return_value = _make_report_findings_result(
            [
                {
                    "file": "rtl/mod_a.sv",
                    "line": 3,
                    "summary": "style nit",
                    "verdict": "PLAUSIBLE",
                },
            ]
        )
        endpoint = ReviewerSpecialist()
        endpoint.parse_args(
            [
                "--scope",
                "rtl/mod_a.sv",
                "--category",
                "rtl",
                "--focus",
                "bugs",
            ]
        )
        endpoint.read_state()
        result = endpoint._run()

        assert result.exit_code == 0  # PLAUSIBLE -> MINOR -> does not block
        assert result.detail["issues"] == 1
        assert result.detail[SEVERITY_MINOR] == 1


_CRITICAL_TEXT_ISSUE = {
    "issues": [
        {
            "severity": "CRITICAL",
            "confidence": "HIGH",
            "category": "bugs",
            "file": "rtl/mod_a.sv",
            "line": 42,
            "summary": "FMAX polarity inverted — returns the minimum",
            "fix_suggestion": "Swap the comparison operands",
        }
    ]
}


def _review_args() -> list[str]:
    return ["--scope", "rtl/mod_a.sv", "--category", "rtl", "--focus", "bugs"]


class TestChannelDisagreement:
    """SETUP-F-33: an empty ReportFindings call must never mask a text CRITICAL."""

    @patch("booley.specialists.specialist._call_agent_sync")
    def test_empty_endpoint_call_does_not_mask_text_critical(self, mock_agent, state_file: Path):
        """The exact F-33 bait: ReportFindings([]) + a CRITICAL issue JSON in text."""
        mock_agent.return_value = _make_report_findings_result(
            [],
            output=(
                "The diff introduces a clear correctness bug.\n" + json.dumps(_CRITICAL_TEXT_ISSUE)
            ),
        )
        endpoint = ReviewerSpecialist()
        endpoint.parse_args(_review_args())
        endpoint.read_state()
        result = endpoint._run()

        assert result.exit_code == 0
        assert result.criterion_met is True  # _done records review completion
        assert result.detail["issues"] == 1
        assert result.detail[SEVERITY_CRITICAL] == 1
        st = DevelopmentState.load(state_file)
        assert st.is_met("review_rtl_bugs_done")

    @patch("booley.specialists.specialist._call_agent_sync")
    def test_empty_endpoint_call_and_prose_only_is_endpoint_error(
        self, mock_agent, state_file: Path
    ):
        """Neither channel produced a verdict => Specialist error, never a clean pass."""
        mock_agent.return_value = _make_report_findings_result(
            [],
            output="I looked at the files. Nothing jumped out at me.",
        )
        endpoint = ReviewerSpecialist()
        endpoint.parse_args(_review_args())
        endpoint.read_state()
        result = endpoint._run()

        assert result.exit_code == 2
        st = DevelopmentState.load(state_file)
        assert not st.is_met("review_rtl_bugs_done")

    @patch("booley.specialists.specialist._call_agent_sync")
    def test_endpoint_findings_win_when_text_is_silent(self, mock_agent, state_file: Path):
        """Non-empty agent-capability channel is still usable with no text JSON at all."""
        mock_agent.return_value = _make_report_findings_result(
            [{"file": "rtl/mod_a.sv", "line": 3, "summary": "latch", "verdict": "CONFIRMED"}],
            output="see the agent-capability call",
        )
        endpoint = ReviewerSpecialist()
        endpoint.parse_args(_review_args())
        endpoint.read_state()
        result = endpoint._run()

        assert result.exit_code == 0
        assert result.detail["issues"] == 1

    @patch("booley.specialists.specialist._call_agent_sync")
    def test_more_severe_text_channel_wins_over_advisory_endpoint_channel(
        self,
        mock_agent,
        state_file: Path,
    ):
        """PLAUSIBLE via endpoint + CRITICAL in text: the blocking channel wins, no merge."""
        mock_agent.return_value = _make_report_findings_result(
            [{"file": "rtl/mod_a.sv", "line": 3, "summary": "nit", "verdict": "PLAUSIBLE"}],
            output=json.dumps(_CRITICAL_TEXT_ISSUE),
        )
        endpoint = ReviewerSpecialist()
        endpoint.parse_args(_review_args())
        endpoint.read_state()
        result = endpoint._run()

        assert result.exit_code == 0
        # Text channel taken whole — not merged with the agent-capability channel.
        assert result.detail["issues"] == 1
        assert result.detail[SEVERITY_CRITICAL] == 1

    @patch("booley.specialists.specialist._call_agent_sync")
    def test_endpoint_channel_wins_when_text_is_clean(self, mock_agent, state_file: Path):
        """Blocking endpoint finding + '{"issues": []}' text: the agent-capability channel still blocks."""
        mock_agent.return_value = _make_report_findings_result(
            [{"file": "rtl/mod_a.sv", "line": 3, "summary": "latch", "verdict": "CONFIRMED"}],
            output='{"issues": []}',
        )
        endpoint = ReviewerSpecialist()
        endpoint.parse_args(_review_args())
        endpoint.read_state()
        result = endpoint._run()

        assert result.exit_code == 0
        assert result.detail["issues"] == 1
        assert result.detail[SEVERITY_MAJOR] == 1

    @patch("booley.specialists.specialist._call_agent_sync")
    def test_all_issues_rejected_is_not_a_clean_pass(self, mock_agent, state_file: Path):
        """Every reported issue failing the schema is a Specialist error, not '0 issues'."""
        mock_agent.return_value = MagicMock(
            output=json.dumps(
                {
                    "issues": [
                        {
                            "severity": "CRITICAL",
                            "confidence": "HIGH",
                            "category": "wrong_focus",  # rejected: not the active focus
                            "file": "rtl/mod_a.sv",
                            "line": 42,
                            "summary": "polarity inverted",
                        }
                    ]
                }
            ),
            structured=None,
        )
        endpoint = ReviewerSpecialist()
        endpoint.parse_args(_review_args())
        endpoint.read_state()
        result = endpoint._run()

        assert result.exit_code == 2
        st = DevelopmentState.load(state_file)
        assert not st.is_met("review_rtl_bugs_done")

    @patch("booley.specialists.specialist._call_agent_sync")
    def test_advisory_endpoint_call_cannot_outvote_a_rejected_text_channel(
        self,
        mock_agent,
        state_file: Path,
        capsys,
    ):
        """The residual F-33 false PASS: a MINOR nit burying a rejected CRITICAL.

        ReportFindings severities cap at MAJOR/MINOR, so a ``PLAUSIBLE``
        cosmetic finding maps to MINOR and passes the gate. If the agent's real
        CRITICAL is in the text JSON but dies on the schema (``"line": "42"``),
        the text channel's verdict is *unknown* — it must not be outvoted into
        a clean pass by the nit.
        """
        mock_agent.return_value = _make_report_findings_result(
            [{"file": "rtl/mod_a.sv", "line": 3, "summary": "naming nit", "verdict": "PLAUSIBLE"}],
            output=json.dumps(
                {
                    "issues": [
                        {
                            "severity": "CRITICAL",
                            "confidence": "HIGH",
                            "category": "bugs",
                            "file": "rtl/mod_a.sv",
                            "line": "42",  # string -> schema-rejected
                            "summary": "FMAX polarity inverted — returns the minimum",
                        }
                    ]
                }
            ),
        )
        endpoint = ReviewerSpecialist()
        endpoint.parse_args(_review_args())
        endpoint.read_state()
        result = endpoint._run()

        assert result.exit_code == 2  # Specialist error, NOT gate_passed: true
        assert result.detail.get("gate_passed") is not True
        st = DevelopmentState.load(state_file)
        assert not st.is_met("review_rtl_bugs_done")
        # And the output must not contradict itself.
        assert "gate_passed: true" not in capsys.readouterr().out.lower()

    @patch("booley.specialists.specialist._call_agent_sync")
    def test_rejected_text_channel_with_blocking_endpoint_findings_still_fails(
        self,
        mock_agent,
        state_file: Path,
    ):
        """A agent-capability channel that already fails the gate is reported, not discarded.

        No false PASS is possible here, and a concrete FAIL is more actionable
        than a bare Specialist error.
        """
        mock_agent.return_value = _make_report_findings_result(
            [{"file": "rtl/mod_a.sv", "line": 3, "summary": "latch", "verdict": "CONFIRMED"}],
            output=json.dumps(
                {
                    "issues": [
                        {
                            "severity": "CRITICAL",
                            "confidence": "HIGH",
                            "category": "bugs",
                            "file": "rtl/mod_a.sv",
                            "line": "42",  # string -> schema-rejected
                            "summary": "polarity inverted",
                        }
                    ]
                }
            ),
        )
        endpoint = ReviewerSpecialist()
        endpoint.parse_args(_review_args())
        endpoint.read_state()
        result = endpoint._run()

        assert result.exit_code == 0
        assert result.detail["issues"] == 1
        assert result.detail[SEVERITY_MAJOR] == 1
        assert result.detail["gate_passed"] is False

    @patch("booley.specialists.specialist._call_agent_sync")
    def test_rejected_text_channel_with_empty_endpoint_call_is_a_endpoint_error(
        self,
        mock_agent,
        state_file: Path,
    ):
        mock_agent.return_value = _make_report_findings_result(
            [],
            output=json.dumps(
                {"issues": [{"severity": "nope", "category": "bugs", "file": "a.sv"}]}
            ),
        )
        endpoint = ReviewerSpecialist()
        endpoint.parse_args(_review_args())
        endpoint.read_state()
        result = endpoint._run()

        assert result.exit_code == 2
        st = DevelopmentState.load(state_file)
        assert not st.is_met("review_rtl_bugs_done")

    @patch("booley.specialists.specialist._call_agent_sync")
    def test_partial_rejection_still_reviews_normally(self, mock_agent, state_file: Path):
        """One survivor means the text channel is usable — only a WARN, no error."""
        mock_agent.return_value = _make_report_findings_result(
            [],
            output=json.dumps(
                {
                    "issues": [
                        {"severity": "nope", "category": "bugs", "file": "a.sv"},
                        {
                            "severity": "MINOR",
                            "confidence": "LOW",
                            "category": "bugs",
                            "file": "rtl/mod_a.sv",
                            "line": 5,
                            "summary": "cosmetic",
                        },
                    ]
                }
            ),
        )
        endpoint = ReviewerSpecialist()
        endpoint.parse_args(_review_args())
        endpoint.read_state()
        result = endpoint._run()

        assert result.exit_code == 0
        assert result.detail["issues"] == 1
        assert result.detail["gate_passed"] is True

    def test_output_instructions_teach_the_endpoint_contract(self):
        """The prompt must name ReportFindings — the contract mismatch behind F-33."""
        text = ReviewerSpecialist._output_instructions("bugs")
        assert "ReportFindings" in text
        assert "mirror" in text.lower()


class TestReadOnlyEnforcement:
    """SETUP-F-35: allowed_agent_capabilities is advisory under bypassPermissions."""

    @patch("booley.specialists.specialist._call_agent_sync")
    def test_review_denies_mutating_tools(self, mock_agent, state_file: Path):
        mock_agent.return_value = _make_report_findings_result([], output='{"issues": []}')
        endpoint = ReviewerSpecialist()
        endpoint.parse_args(_review_args())
        endpoint.read_state()
        endpoint._run()

        params = mock_agent.call_args[0][0]
        assert "Bash" in params.disallowed_agent_capabilities
        assert "Write" in params.disallowed_agent_capabilities
        assert "Edit" in params.disallowed_agent_capabilities
        # The read-only allowlist is unchanged.
        assert params.allowed_agent_capabilities == ["Read", "Grep", "Glob", "ReportFindings"]


class TestNoReportDirNotice:
    """SETUP-F-39: the "nothing was persisted" notice belongs to every endpoint.

    ``Endpoint._post_run`` emits it on stderr for whichever endpoint ran (covered by
    ``TestNoReportDirWarning`` in test_base.py). The reviewer used to build its
    own copy into ``report_text``; keeping both printed the same warning twice
    per run, which is the noise F-28 was about. These tests pin the removal.
    """

    @patch("booley.specialists.specialist._call_agent_sync")
    def test_verdict_text_does_not_carry_its_own_notice(self, mock_agent, state_file: Path):
        mock_agent.return_value = _make_report_findings_result([], output='{"issues": []}')
        endpoint = ReviewerSpecialist()
        endpoint.parse_args(_review_args())
        endpoint.read_state()
        result = endpoint._run()

        assert "no --report-dir" not in result.report_text

    @patch("booley.specialists.specialist._call_agent_sync")
    def test_silent_with_report_dir(self, mock_agent, state_file: Path, tmp_path: Path):
        mock_agent.return_value = _make_report_findings_result([], output='{"issues": []}')
        endpoint = ReviewerSpecialist()
        endpoint.parse_args([*_review_args(), "--report-dir", str(tmp_path / "rep")])
        endpoint.read_state()
        result = endpoint._run()

        assert "no --report-dir" not in result.report_text


class TestUpstreamReviewRejection:
    """Single review must not treat free-form prose as a clean pass."""

    @patch("booley.specialists.specialist._call_agent_sync")
    def test_no_json_wrapper_returns_endpoint_error(
        self,
        mock_agent,
        state_file: Path,
    ):
        """Agent emits prose with no JSON wrapper => Specialist error, criterion not set."""
        mock_agent.return_value = MagicMock(
            output="I reviewed the files and everything looks fine.",
            structured=None,
        )
        endpoint = ReviewerSpecialist()
        endpoint.parse_args(
            [
                "--scope",
                "rtl/mod_a.sv",
                "--category",
                "rtl",
                "--focus",
                "bugs",
            ]
        )
        endpoint.read_state()
        result = endpoint._run()

        assert result.exit_code == 2  # EXIT_ERROR
        # Must NOT have been promoted to a clean review.
        assert (
            "no parseable" in result.report_text.lower()
            or "invocation failed" in result.report_text.lower()
        )
        st = DevelopmentState.load(state_file)
        assert not st.is_met("review_rtl_bugs_done")

    @patch("booley.specialists.specialist._call_agent_sync")
    def test_malformed_entries_dropped_but_review_still_completes(
        self,
        mock_agent,
        state_file: Path,
    ):
        """Mix of valid + malformed issues: valid ones counted, bad ones dropped."""
        mock_agent.return_value = MagicMock(
            output=json.dumps(
                {
                    "issues": [
                        _make_issue_dict("CRITICAL"),
                        {
                            "severity": "BOGUS",
                            "confidence": "HIGH",
                            "category": "x",
                            "file": "a.sv",
                            "line": 1,
                            "summary": "garbage",
                        },
                    ]
                }
            ),
            structured=None,
        )
        endpoint = ReviewerSpecialist()
        endpoint.parse_args(
            [
                "--scope",
                "rtl/mod_a.sv",
                "--category",
                "rtl",
                "--focus",
                "bugs",
            ]
        )
        endpoint.read_state()
        result = endpoint._run()

        # Only the valid CRITICAL is counted; _done completion is independent
        # from the quality verdict recorded in detail.
        assert result.detail["issues"] == 1
        assert result.detail["CRITICAL"] == 1


# ---------------------------------------------------------------------------
# Severity counting
# ---------------------------------------------------------------------------


class TestSeverityCounting:
    def test_count_mixed(self):
        issues = [
            ReviewIssue.from_dict(_make_issue_dict("CRITICAL")),
            ReviewIssue.from_dict(_make_issue_dict("CRITICAL")),
            ReviewIssue.from_dict(_make_issue_dict("MAJOR")),
            ReviewIssue.from_dict(_make_issue_dict("MINOR")),
        ]
        counts = count_by_severity(issues)
        assert counts[SEVERITY_CRITICAL] == 2
        assert counts[SEVERITY_MAJOR] == 1
        assert counts[SEVERITY_MINOR] == 1

    def test_count_empty(self):
        counts = count_by_severity([])
        assert counts[SEVERITY_CRITICAL] == 0
        assert counts[SEVERITY_MAJOR] == 0
        assert counts[SEVERITY_MINOR] == 0

    def test_count_all_one_severity(self):
        issues = [ReviewIssue.from_dict(_make_issue_dict("MINOR")) for _ in range(5)]
        counts = count_by_severity(issues)
        assert counts[SEVERITY_MINOR] == 5
        assert counts[SEVERITY_CRITICAL] == 0

    def test_unknown_severity_ignored(self):
        issues = [ReviewIssue.from_dict({"severity": "UNKNOWN", "file": "f.sv"})]
        counts = count_by_severity(issues)
        assert sum(counts.values()) == 0


# ---------------------------------------------------------------------------
# Gate logic
# ---------------------------------------------------------------------------


class TestGateLogic:
    def test_pass_clean(self):
        counts = {SEVERITY_CRITICAL: 0, SEVERITY_MAJOR: 0, SEVERITY_MINOR: 0}
        assert check_gate(counts) is True

    def test_pass_minor_only(self):
        counts = {SEVERITY_CRITICAL: 0, SEVERITY_MAJOR: 0, SEVERITY_MINOR: 3}
        assert check_gate(counts) is True

    def test_fail_critical(self):
        counts = {SEVERITY_CRITICAL: 1, SEVERITY_MAJOR: 0, SEVERITY_MINOR: 0}
        assert check_gate(counts) is False

    def test_fail_major(self):
        counts = {SEVERITY_CRITICAL: 0, SEVERITY_MAJOR: 1, SEVERITY_MINOR: 0}
        assert check_gate(counts) is False

    def test_fail_both(self):
        counts = {SEVERITY_CRITICAL: 1, SEVERITY_MAJOR: 2, SEVERITY_MINOR: 1}
        assert check_gate(counts) is False


# ---------------------------------------------------------------------------
# Scope validation
# ---------------------------------------------------------------------------


class TestScopeValidation:
    def test_rtl_scope_valid(self):
        errors = validate_scope_category(["rtl/mod_a.sv", "fw/main.c"], "rtl")
        assert errors == []

    def test_rtl_scope_invalid_tb_path(self):
        errors = validate_scope_category(["tb/mod_a_tb.sv"], "rtl")
        assert len(errors) == 1
        assert "doesn't match RTL" in errors[0]

    @patch("booley.specialists.reviewer._get_tb_prefixes", return_value=("tb/", "tb\\"))
    def test_tb_scope_valid(self, _mock):
        errors = validate_scope_category(["tb/mod_a_tb.sv"], "tb")
        assert errors == []

    @patch("booley.specialists.reviewer._get_tb_prefixes", return_value=("tb/", "tb\\"))
    def test_tb_scope_invalid_rtl_path(self, _mock):
        errors = validate_scope_category(["rtl/mod_a.sv"], "tb")
        assert len(errors) == 1
        assert "doesn't match TB" in errors[0]

    @patch(
        "booley.specialists.reviewer._get_rtl_prefixes",
        return_value=("rtl/", "rtl\\", "fw/", "fw\\"),
    )
    @patch("booley.specialists.reviewer._get_tb_prefixes", return_value=("tb/", "tb\\"))
    def test_mixed_scope_rtl_partial_invalid(self, _mock_tb, _mock_rtl):
        errors = validate_scope_category(
            ["rtl/mod_a.sv", "tb/mod_a_tb.sv"],
            "rtl",
        )
        assert len(errors) == 1
        assert "tb/mod_a_tb.sv" in errors[0]

    def test_empty_scope(self):
        errors = validate_scope_category([], "rtl")
        assert errors == []

    def test_flat_repo_root_file_is_valid_exact_scope(self, tmp_path: Path):
        (tmp_path / "picorv32.v").write_text("module picorv32; endmodule\n")
        (tmp_path / "testbench.v").write_text("module testbench; endmodule\n")
        (tmp_path / "picorv32.core").write_text(
            "CAPI=2:\n"
            "name: ::picorv32\n"
            "filesets:\n"
            "  rtl: {files: [picorv32.v]}\n"
            "  tb: {files: [testbench.v], tags: [tb]}\n"
            "targets:\n"
            "  sim: {filesets: [rtl, tb], toplevel: testbench}\n"
        )

        assert validate_scope_category(["picorv32.v"], "rtl", tmp_path) == []
        assert validate_scope_category(["./picorv32.v"], "rtl", tmp_path) == []
        errors = validate_scope_category(["picorv32.v.bak"], "rtl", tmp_path)
        assert errors and "picorv32.v.bak" in errors[0]


# ---------------------------------------------------------------------------
# CLI args
# ---------------------------------------------------------------------------


class TestReviewerToolArgs:
    def test_focus_contract_surfaces_are_aligned(self, state_file: Path):
        endpoint = ReviewerSpecialist()
        endpoint.parse_args(
            [
                "--category",
                "rtl",
                "--scope",
                "rtl/a.sv",
                "--focus",
                ",".join(sorted(RTL_FOCUS_CATEGORIES)),
            ]
        )
        assert endpoint._validate_args() == []
        assert "spec" in RTL_FOCUS_CATEGORIES
        assert {"quality"} == TB_FOCUS_CATEGORIES

        satisfies = set(ReviewerSpecialist.satisfies_args.values())
        for focus in RTL_FOCUS_CATEGORIES:
            assert f"--category rtl --focus {focus}" in satisfies
        for focus in TB_FOCUS_CATEGORIES:
            assert f"--category tb --focus {focus}" in satisfies
        assert "--category tb --focus spec" not in satisfies

    def test_focus_required(self, state_file: Path):
        """RTL without --focus errors."""
        endpoint = ReviewerSpecialist()
        # --focus is required by argparse, so omitting it raises SystemExit
        with pytest.raises(SystemExit):
            endpoint.parse_args(
                [
                    "--scope",
                    "rtl/mod_a.sv",
                    "--category",
                    "rtl",
                ]
            )

    def test_rtl_valid_focuses(self, state_file: Path):
        """--focus bugs validates OK for RTL."""
        endpoint = ReviewerSpecialist()
        endpoint.parse_args(
            [
                "--scope",
                "rtl/mod_a.sv",
                "--category",
                "rtl",
                "--focus",
                "bugs",
            ]
        )
        errors = endpoint._validate_args()
        focus_errors = [e for e in errors if "focus" in e.lower()]
        assert focus_errors == []

    def test_rtl_spec_focus_accepted(self, state_file: Path):
        """--focus spec validates OK for RTL (reinstated — ADR 0038)."""
        endpoint = ReviewerSpecialist()
        endpoint.parse_args(
            [
                "--scope",
                "rtl/mod_a.sv",
                "--category",
                "rtl",
                "--focus",
                "spec",
            ]
        )
        errors = endpoint._validate_args()
        focus_errors = [e for e in errors if "focus" in e.lower()]
        assert focus_errors == []

    def test_tb_valid_focuses(self, state_file: Path):
        """--focus quality validates OK for TB."""
        endpoint = ReviewerSpecialist()
        endpoint.parse_args(
            [
                "--scope",
                "tb/mod_a_tb.sv",
                "--category",
                "tb",
                "--focus",
                "quality",
            ]
        )
        errors = endpoint._validate_args()
        focus_errors = [e for e in errors if "focus" in e.lower()]
        assert focus_errors == []

    def test_invalid_rtl_focus_rejected(self, state_file: Path):
        """--focus bogus errors for RTL."""
        endpoint = ReviewerSpecialist()
        endpoint.parse_args(
            [
                "--scope",
                "rtl/mod_a.sv",
                "--category",
                "rtl",
                "--focus",
                "bogus",
            ]
        )
        errors = endpoint._validate_args()
        assert any("Invalid RTL focus" in e for e in errors)

    def test_invalid_focus_with_tb_error(self, state_file: Path):
        endpoint = ReviewerSpecialist()
        endpoint.parse_args(
            [
                "--scope",
                "tb/mod_a_tb.sv",
                "--category",
                "tb",
                "--focus",
                "bugs",
            ]
        )
        errors = endpoint._validate_args()
        assert any("Invalid TB focus" in e for e in errors)

    def test_scope_mismatch_rtl(self, state_file: Path):
        endpoint = ReviewerSpecialist()
        endpoint.parse_args(
            [
                "--scope",
                "tb/mod_a_tb.sv",
                "--category",
                "rtl",
                "--focus",
                "bugs",
            ]
        )
        errors = endpoint._validate_args()
        assert any("doesn't match RTL" in e for e in errors)

    def test_scope_mismatch_tb(self, state_file: Path):
        endpoint = ReviewerSpecialist()
        endpoint.parse_args(
            [
                "--scope",
                "rtl/mod_a.sv",
                "--category",
                "tb",
                "--focus",
                "quality",
            ]
        )
        errors = endpoint._validate_args()
        assert any("doesn't match TB" in e for e in errors)


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------


class TestPromptConstruction:
    def test_reviewer_agent_capabilities_are_read_only(self):
        assert ReviewerSpecialist.agent_capabilities == ["Read", "Grep", "Glob"]

    def test_rtl_system_prompt_includes_focus_guide(self, state_file: Path):
        endpoint = ReviewerSpecialist()
        endpoint.parse_args(
            [
                "--scope",
                "rtl/mod_a.sv",
                "--category",
                "rtl",
                "--focus",
                "bugs",
            ]
        )
        system = endpoint._build_system_prompt("bugs")
        assert "bugs" in system
        assert "Review methodology" in system

    def test_rtl_optimization_prompt_checks_for_runtime_constant_work(self, state_file: Path):
        endpoint = ReviewerSpecialist()
        endpoint.parse_args(
            [
                "--scope",
                "rtl/mod_a.sv",
                "--category",
                "rtl",
                "--focus",
                "optimization",
            ]
        )

        system = endpoint._build_system_prompt("optimization")

        assert "Compile/elaboration-time computation left in runtime hardware" in system
        assert "Flag elaboration-invariant work implemented as runtime hardware" in system
        assert "constant function evaluated into a `localparam`" in system

    def test_rtl_user_prompt_includes_scope(self, state_file: Path):
        endpoint = ReviewerSpecialist()
        endpoint.parse_args(
            [
                "--scope",
                "rtl/mod_a.sv",
                "--category",
                "rtl",
                "--focus",
                "bugs",
            ]
        )
        prompt = endpoint._build_prompt(focus_override="bugs")
        assert "mod_a.sv" in prompt

    def test_rtl_security_system_prompt(self, state_file: Path):
        endpoint = ReviewerSpecialist()
        endpoint.parse_args(
            [
                "--scope",
                "rtl/mod_a.sv",
                "--category",
                "rtl",
                "--focus",
                "security",
            ]
        )
        system = endpoint._build_system_prompt("security")
        assert "security" in system
        prompt = endpoint._build_prompt()
        assert "mod_a.sv" in prompt

    def test_rtl_protocol_system_prompt_uses_protocol_cdc_guide(self, state_file: Path):
        endpoint = ReviewerSpecialist()
        endpoint.parse_args(
            [
                "--scope",
                "rtl/mod_a.sv",
                "--category",
                "rtl",
                "--focus",
                "protocol",
            ]
        )
        system = endpoint._build_system_prompt("protocol")
        assert "Protocol & Clock Domain Crossings" in system
        assert '"category": "protocol"' in system

    def test_reviewer_prompts_do_not_request_info_severity(self, state_file: Path):
        for category, focus in [
            ("rtl", "bugs"),
            ("rtl", "protocol"),
            ("rtl", "ifdef"),
            ("rtl", "security"),
            ("rtl", "optimization"),
            ("rtl", "quality"),
            ("tb", "quality"),
        ]:
            endpoint = ReviewerSpecialist()
            scope = "tb/mod_a_tb.sv" if category == "tb" else "rtl/mod_a.sv"
            endpoint.parse_args(
                [
                    "--scope",
                    scope,
                    "--category",
                    category,
                    "--focus",
                    focus,
                ]
            )
            system = endpoint._build_system_prompt(focus)
            assert "INFO" not in system

    def test_tb_system_prompt_includes_tb_guides(self, state_file: Path):
        endpoint = ReviewerSpecialist()
        endpoint.parse_args(
            [
                "--scope",
                "tb/mod_a_tb.sv",
                "--category",
                "tb",
                "--focus",
                "quality",
            ]
        )
        system = endpoint._build_system_prompt("quality")
        assert "Review methodology" in system
        assert "Testbench style guide" in system
        assert "unit_tb_style_guide.md" not in system
        assert "read its RTL source" not in system
        assert "FSM structure" not in system
        assert "datapath" not in system.lower()
        prompt = endpoint._build_prompt()
        assert "mod_a_tb.sv" in prompt
        assert "code_review/rtl/" not in prompt

    @pytest.mark.parametrize(
        ("category", "scope", "label"),
        [
            ("rtl", "rtl/mod_a.sv", "RTL"),
            ("tb", "tb/mod_a_tb.sv", "Testbench"),
        ],
    )
    def test_project_style_overlay_is_inlined(
        self,
        state_file: Path,
        tmp_path: Path,
        category: str,
        scope: str,
        label: str,
    ):
        """A project-authored overlay is appended after the packaged guide."""
        overlay = tmp_path / ".booley_project" / f"{category}_style_guide.md"
        overlay.parent.mkdir(parents=True, exist_ok=True)
        overlay.write_text("Signal names MUST carry a `u_` prefix.\n")

        endpoint = ReviewerSpecialist()
        endpoint.parse_args(
            [
                "--scope",
                scope,
                "--category",
                category,
                "--focus",
                "quality",
                "--work-dir",
                str(tmp_path),
            ]
        )
        system = endpoint._build_system_prompt("quality")

        assert f"## {label} style guide\n" in system
        assert f"## {label} style guide — project overlay" in system
        assert "Signal names MUST carry a `u_` prefix." in system
        # Overlay must follow the packaged guide so precedence reads correctly.
        assert system.index(f"## {label} style guide\n") < system.index(
            f"## {label} style guide — project overlay"
        )

    @pytest.mark.parametrize(
        ("category", "scope"),
        [("rtl", "rtl/mod_a.sv"), ("tb", "tb/mod_a_tb.sv")],
    )
    def test_missing_project_style_overlay_is_omitted(
        self,
        state_file: Path,
        tmp_path: Path,
        category: str,
        scope: str,
    ):
        """No overlay authored is the normal case — review still runs."""
        endpoint = ReviewerSpecialist()
        endpoint.parse_args(
            [
                "--scope",
                scope,
                "--category",
                category,
                "--focus",
                "quality",
                "--work-dir",
                str(tmp_path),
            ]
        )
        system = endpoint._build_system_prompt("quality")

        assert "style guide — project overlay" not in system
        assert "style guide" in system.lower()

    def test_project_style_overlay_skipped_for_non_quality_focus(
        self,
        state_file: Path,
        tmp_path: Path,
    ):
        """Overlays ride along with style guides — quality focus only."""
        overlay = tmp_path / ".booley_project" / "rtl_style_guide.md"
        overlay.parent.mkdir(parents=True, exist_ok=True)
        overlay.write_text("Signal names MUST carry a `u_` prefix.\n")

        endpoint = ReviewerSpecialist()
        endpoint.parse_args(
            [
                "--scope",
                "rtl/mod_a.sv",
                "--category",
                "rtl",
                "--focus",
                "bugs",
                "--work-dir",
                str(tmp_path),
            ]
        )
        system = endpoint._build_system_prompt("bugs")

        assert "style guide — project overlay" not in system
        assert "u_` prefix" not in system

    def test_diff_ref_injection(self, state_file: Path):
        endpoint = ReviewerSpecialist()
        endpoint.parse_args(
            [
                "--scope",
                "rtl/mod_a.sv",
                "--category",
                "rtl",
                "--focus",
                "bugs",
                "--diff-ref",
                "abc1234",
            ]
        )
        prompt = endpoint._build_prompt(focus_override="bugs")
        assert "abc1234" in prompt
        assert "changed code" in prompt.lower()

    def test_steer_injection(self, state_file: Path):
        endpoint = ReviewerSpecialist()
        endpoint.parse_args(
            [
                "--scope",
                "rtl/mod_a.sv",
                "--category",
                "rtl",
                "--focus",
                "bugs",
                "--steer",
                "Pay attention to clock domain crossings",
            ]
        )
        prompt = endpoint._build_prompt(focus_override="bugs")
        assert "Pay attention to clock domain crossings" in prompt

    def test_steer_injection_tb(self, state_file: Path):
        endpoint = ReviewerSpecialist()
        endpoint.parse_args(
            [
                "--scope",
                "tb/mod_a_tb.sv",
                "--category",
                "tb",
                "--focus",
                "quality",
                "--steer",
                "Check for false-pass risks",
            ]
        )
        prompt = endpoint._build_prompt()
        assert "Check for false-pass risks" in prompt

    def test_output_format_in_system_prompt(self, state_file: Path):
        endpoint = ReviewerSpecialist()
        endpoint.parse_args(
            [
                "--scope",
                "rtl/mod_a.sv",
                "--category",
                "rtl",
                "--focus",
                "bugs",
            ]
        )
        system = endpoint._build_system_prompt("bugs")
        assert "CRITICAL" in system
        assert "MAJOR" in system
        assert "MINOR" in system
        assert "STRICT SCHEMA" in system
        assert "``issues`` array" in system
        # Low-confidence gate must survive prompt trims (wording may vary).
        assert "LOW confidence" in system
        assert '"issues": []' in system

    # ----- Unattended-mode spec reviewer override (REMOVED in ADR-0014) -----
    # The unattended spec-focus preamble used to demote MAJOR -> MINOR for
    # spec-silence findings under `// SPEC-INTERPRETATION:` comments.  With
    # spec compliance centralized in spec_arbiter, the reviewer no longer has
    # a spec focus and the preamble is dead code.  Its tests were deleted.


# ---------------------------------------------------------------------------
# Summary line formatting
# ---------------------------------------------------------------------------


class TestSummaryFormatting:
    def test_format_with_issues(self):
        counts = {SEVERITY_CRITICAL: 1, SEVERITY_MAJOR: 0, SEVERITY_MINOR: 1}
        line = format_summary_line("rtl", "bugs", 2, counts, 45.3)
        assert "[review]" in line
        assert "rtl / bugs" in line
        assert "2 issues" in line
        assert "1 CRITICAL" in line
        assert "1 MINOR" in line
        assert "45s" in line

    def test_format_no_issues(self):
        counts = {SEVERITY_CRITICAL: 0, SEVERITY_MAJOR: 0, SEVERITY_MINOR: 0}
        line = format_summary_line("tb", "", 0, counts, 10.0)
        assert "[review]" in line
        assert "0 issues" in line

    def test_format_single_issue(self):
        counts = {SEVERITY_CRITICAL: 0, SEVERITY_MAJOR: 1, SEVERITY_MINOR: 0}
        line = format_summary_line("rtl", "ifdef", 1, counts, 28.0)
        assert "1 issue" in line
        assert "1 MAJOR" in line
        # Should not say "issues" (plural) for count=1
        assert "1 issues" not in line


# ---------------------------------------------------------------------------
# Full RTL review run (mocked agent)
# ---------------------------------------------------------------------------


class TestFullRtlReview:
    @patch("booley.specialists.specialist._call_agent_sync")
    def test_clean_review(self, mock_agent, state_file: Path, capsys):
        """No issues => PASS, exit 0, _done met."""
        mock_agent.return_value = _make_agent_result([])
        endpoint = ReviewerSpecialist()
        endpoint.parse_args(
            [
                "--scope",
                "rtl/mod_a.sv",
                "--category",
                "rtl",
                "--focus",
                "bugs",
            ]
        )
        endpoint.read_state()
        result = endpoint._run()
        assert result.exit_code == 0
        assert result.criterion_met is True
        assert result.criterion_key == "review_rtl_bugs_done"
        st = DevelopmentState.load(state_file)
        assert st.is_met("review_rtl_bugs_done") is True
        captured = capsys.readouterr()
        assert "RESULT: REVIEWED — NO FINDINGS" in captured.out

    @patch("booley.specialists.specialist._call_agent_sync")
    def test_critical_issues_done_met(self, mock_agent, state_file: Path, capsys):
        """Critical issues fail the verdict but satisfy completed-review _done."""
        mock_agent.return_value = _make_agent_result(
            [
                _make_issue_dict("CRITICAL"),
                _make_issue_dict("MINOR"),
            ]
        )
        endpoint = ReviewerSpecialist()
        endpoint.parse_args(
            [
                "--scope",
                "rtl/mod_a.sv",
                "--category",
                "rtl",
                "--focus",
                "bugs",
            ]
        )
        endpoint.read_state()
        result = endpoint._run()
        assert result.exit_code == 0
        assert result.criterion_met is True
        assert result.criterion_key == "review_rtl_bugs_done"
        st = DevelopmentState.load(state_file)
        assert st.is_met("review_rtl_bugs_done") is True
        assert st.criteria["review_rtl_bugs_done"].detail["gate_passed"] is False
        captured = capsys.readouterr()
        assert "RESULT: REVIEWED WITH FINDINGS" in captured.out
        assert "critical" in captured.out

    @patch("booley.specialists.specialist._call_agent_sync")
    def test_findings_complete_done_review_and_replay(self, mock_agent, state_file: Path):
        """A finding-bearing _done review completes and only replays afterward."""
        common = ["--scope", "rtl/mod_a.sv", "--category", "rtl", "--focus", "bugs"]

        mock_agent.return_value = _make_agent_result([_make_issue_dict("CRITICAL")])
        t1 = ReviewerSpecialist()
        t1.parse_args(common)
        t1.read_state()
        first = t1._run()
        assert first.exit_code == 0
        assert first.criterion_met is True

        t2 = ReviewerSpecialist()
        t2.parse_args(common)
        t2.read_state()
        replay = t2._run()
        assert replay.exit_code == 0
        assert replay.criterion_met is True
        assert "already completed" in replay.report_text
        assert "RESULT: REVIEWED WITH FINDINGS" in replay.report_text
        assert mock_agent.call_count == 1

    @patch("booley.specialists.specialist._call_agent_sync")
    def test_steering_does_not_reopen_completed_done_review(
        self,
        mock_agent,
        state_file: Path,
    ):
        """Steering cannot turn terminal _done into a cleanliness workflow."""
        common = ["--scope", "rtl/mod_a.sv", "--category", "rtl", "--focus", "bugs"]
        mock_agent.return_value = _make_agent_result([_make_issue_dict("CRITICAL")])

        t1 = ReviewerSpecialist()
        t1.parse_args(common)
        t1.read_state()
        t1._run()

        t2 = ReviewerSpecialist()
        t2.parse_args([*common, "--steer", "attempt 1"])
        t2.read_state()
        replay = t2._run()
        assert replay.exit_code == 0
        assert "_clean" in replay.report_text
        assert mock_agent.call_count == 1

    @patch("booley.specialists.specialist._call_agent_sync")
    def test_minor_only_passes(self, mock_agent, state_file: Path, capsys):
        """Minor-only issues => PASS, criterion_met=True."""
        mock_agent.return_value = _make_agent_result(
            [
                _make_issue_dict("MINOR"),
                _make_issue_dict("MINOR"),
            ]
        )
        endpoint = ReviewerSpecialist()
        endpoint.parse_args(
            [
                "--scope",
                "rtl/mod_a.sv",
                "--category",
                "rtl",
                "--focus",
                "bugs",
            ]
        )
        endpoint.read_state()
        result = endpoint._run()
        assert result.exit_code == 0
        assert result.criterion_met is True

    @patch("booley.specialists.specialist._call_agent_sync")
    def test_single_focus_security(self, mock_agent, state_file: Path, capsys):
        """Single focus runs one agent call, criterion key reflects focus."""
        mock_agent.return_value = _make_agent_result([])
        endpoint = ReviewerSpecialist()
        endpoint.parse_args(
            [
                "--scope",
                "rtl/mod_a.sv",
                "--category",
                "rtl",
                "--focus",
                "security",
            ]
        )
        endpoint.read_state()
        result = endpoint._run()
        assert result.exit_code == 0
        assert result.criterion_key == "review_rtl_security_done"
        assert mock_agent.call_count == 1

    @patch("booley.specialists.specialist._call_agent_sync")
    def test_criterion_set_rtl_spec(self, mock_agent, state_file: Path):
        """_done criterion is set on completion."""
        mock_agent.return_value = _make_agent_result([])
        endpoint = ReviewerSpecialist()
        endpoint.parse_args(
            [
                "--scope",
                "rtl/mod_a.sv",
                "--category",
                "rtl",
                "--focus",
                "bugs",
            ]
        )
        endpoint.read_state()
        endpoint._run()
        st = DevelopmentState.load(state_file)
        assert st.is_met("review_rtl_bugs_done") is True


# ---------------------------------------------------------------------------
# Full TB review run (mocked agent)
# ---------------------------------------------------------------------------


class TestFullTbReview:
    @patch("booley.specialists.reviewer._get_tb_prefixes", return_value=("tb/", "tb\\"))
    @patch("booley.specialists.specialist._call_agent_sync")
    def test_tb_clean_review(self, mock_agent, _mock_tb, state_file: Path, capsys):
        mock_agent.return_value = _make_agent_result([])
        endpoint = ReviewerSpecialist()
        endpoint.parse_args(
            [
                "--scope",
                "tb/mod_a_tb.sv",
                "--category",
                "tb",
                "--focus",
                "quality",
            ]
        )
        endpoint.read_state()
        result = endpoint._run()
        assert result.exit_code == 0
        captured = capsys.readouterr()
        assert "RESULT: REVIEWED — NO FINDINGS" in captured.out

    @patch("booley.specialists.reviewer._get_tb_prefixes", return_value=("tb/", "tb\\"))
    @patch("booley.specialists.specialist._call_agent_sync")
    def test_tb_criterion_set(self, mock_agent, _mock_tb, state_file: Path):
        mock_agent.return_value = _make_agent_result([])
        endpoint = ReviewerSpecialist()
        endpoint.parse_args(
            [
                "--scope",
                "tb/mod_a_tb.sv",
                "--category",
                "tb",
                "--focus",
                "quality",
            ]
        )
        endpoint.read_state()
        endpoint._run()
        st = DevelopmentState.load(state_file)
        assert st.is_met("review_tb_quality_done") is True


# ---------------------------------------------------------------------------
# Diff-ref in full run
# ---------------------------------------------------------------------------


class TestDiffRefInRun:
    @patch.object(ReviewerSpecialist, "_prepare_diff_boundary", return_value=None)
    @patch("booley.specialists.specialist._call_agent_sync")
    def test_diff_ref_passed_to_prompt(self, mock_agent, _prepare, state_file: Path):
        mock_agent.return_value = _make_agent_result([])
        endpoint = ReviewerSpecialist()
        endpoint.parse_args(
            [
                "--scope",
                "rtl/mod_a.sv",
                "--category",
                "rtl",
                "--focus",
                "bugs",
                "--diff-ref",
                "HEAD",
            ]
        )
        endpoint.read_state()
        endpoint._run()
        call_kwargs = mock_agent.call_args
        prompt = call_kwargs.args[0].prompt
        assert "HEAD" in prompt


# ---------------------------------------------------------------------------
# Agent invocation failure
# ---------------------------------------------------------------------------


class TestAgentFailure:
    @patch("booley.specialists.specialist._call_agent_sync", side_effect=RuntimeError("boom"))
    def test_agent_error_returns_exit_2(self, mock_agent, state_file: Path):
        endpoint = ReviewerSpecialist()
        endpoint.parse_args(
            [
                "--scope",
                "rtl/mod_a.sv",
                "--category",
                "rtl",
                "--focus",
                "bugs",
            ]
        )
        endpoint.read_state()
        result = endpoint._run()
        assert result.exit_code == 2


# ---------------------------------------------------------------------------
# _interpret_output (fallback path)
# ---------------------------------------------------------------------------


class TestInterpretOutput:
    def test_interpret_clean(self, state_file: Path):
        endpoint = ReviewerSpecialist()
        endpoint.parse_args(
            [
                "--scope",
                "rtl/mod_a.sv",
                "--category",
                "rtl",
                "--focus",
                "bugs",
            ]
        )
        output = json.dumps({"issues": []})
        result = endpoint._interpret_output(output, None)
        assert result.exit_code == 0
        assert result.criterion_met is True
        assert result.criterion_key == "review_rtl_bugs_done"

    def test_interpret_with_critical(self, state_file: Path):
        endpoint = ReviewerSpecialist()
        endpoint.parse_args(
            [
                "--scope",
                "rtl/mod_a.sv",
                "--category",
                "rtl",
                "--focus",
                "security",
            ]
        )
        output = json.dumps({"issues": [_make_issue_dict("CRITICAL")]})
        result = endpoint._interpret_output(output, None)
        assert result.exit_code == 0
        assert result.criterion_met is True
        assert result.criterion_key == "review_rtl_security_done"


# ---------------------------------------------------------------------------
# TB focus validation
# ---------------------------------------------------------------------------


class TestTbFocusValidation:
    def test_tb_focus_quality_allowed(self, state_file: Path):
        endpoint = ReviewerSpecialist()
        endpoint.parse_args(
            [
                "--scope",
                "tb/mod_a_tb.sv",
                "--category",
                "tb",
                "--focus",
                "quality",
            ]
        )
        errors = endpoint._validate_args()
        focus_errors = [e for e in errors if "focus" in e.lower()]
        assert focus_errors == []

    def test_tb_focus_spec_rejected(self, state_file: Path):
        """--focus spec stays rejected on TB — the spec focus is RTL-only (ADR 0038)."""
        endpoint = ReviewerSpecialist()
        endpoint.parse_args(
            [
                "--scope",
                "tb/mod_a_tb.sv",
                "--category",
                "tb",
                "--focus",
                "spec",
            ]
        )
        errors = endpoint._validate_args()
        focus_errors = [e for e in errors if "focus" in e.lower()]
        assert focus_errors  # must reject

    def test_tb_focus_invalid_category(self, state_file: Path):
        endpoint = ReviewerSpecialist()
        endpoint.parse_args(
            [
                "--scope",
                "tb/mod_a_tb.sv",
                "--category",
                "tb",
                "--focus",
                "bugs",
            ]
        )
        errors = endpoint._validate_args()
        assert any("Invalid TB focus" in e for e in errors)


# ---------------------------------------------------------------------------
# RTL spec focus (reinstated — ADR 0038)
# ---------------------------------------------------------------------------


@pytest.fixture()
def ticket_md(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A ticket file with frontmatter + body, resolved via --ticket only."""
    monkeypatch.delenv("BOOLEY_LOGS_DIR", raising=False)
    ticket = tmp_path / "ticket.md"
    ticket.write_text(
        "---\n"
        "summary: add widget\n"
        "type: feature\n"
        "---\n\n"
        "## Description\n\n"
        "o_valid pulses exactly one cycle per accepted input.\n",
        encoding="utf-8",
    )
    return ticket


class TestRtlSpecFocus:
    def _spec_endpoint(self, ticket: Path | None) -> ReviewerSpecialist:
        endpoint = ReviewerSpecialist()
        argv = [
            "--scope",
            "rtl/mod_a.sv",
            "--category",
            "rtl",
            "--focus",
            "spec",
        ]
        if ticket is not None:
            argv += ["--ticket", str(ticket)]
        endpoint.parse_args(argv)
        endpoint.read_state()
        return endpoint

    def test_system_prompt_includes_spec_guide(self, state_file: Path, ticket_md: Path):
        system = self._spec_endpoint(ticket_md)._build_system_prompt("spec")
        assert "Spec Compliance" in system

    def test_prompt_inlines_spec_from_ticket_body(self, state_file: Path, ticket_md: Path):
        prompt = self._spec_endpoint(ticket_md)._build_prompt(focus_override="spec")
        assert "## Specification" in prompt
        assert "o_valid pulses exactly one cycle" in prompt

    def test_spec_field_takes_priority_over_body(
        self,
        state_file: Path,
        ticket_md: Path,
        tmp_path: Path,
    ):
        (tmp_path / "arch_spec.md").write_text("The real spec text.", encoding="utf-8")
        ticket_md.write_text(
            ticket_md.read_text(encoding="utf-8").replace(
                "type: feature\n",
                'type: feature\nspec: "arch_spec.md"\n',
            ),
            encoding="utf-8",
        )
        endpoint = self._spec_endpoint(ticket_md)
        endpoint.args.work_dir = str(tmp_path)
        prompt = endpoint._build_prompt(focus_override="spec")
        assert "The real spec text." in prompt
        assert "o_valid pulses exactly one cycle" not in prompt

    def test_run_errors_without_spec(self, state_file: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv("BOOLEY_LOGS_DIR", raising=False)
        result = self._spec_endpoint(None)._run()
        assert result.exit_code == 2
        assert "spec" in result.report_text.lower()

    @patch("booley.specialists.specialist._call_agent_sync")
    def test_clean_spec_review_sets_criterion(
        self,
        mock_agent,
        state_file: Path,
        ticket_md: Path,
    ):
        mock_agent.return_value = _make_agent_result([])
        endpoint = self._spec_endpoint(ticket_md)
        endpoint._run()
        st = DevelopmentState.load(state_file)
        assert st.is_met("review_rtl_spec_done") is True


class TestTbQualityPrompt:
    @patch("booley.specialists.reviewer._get_tb_prefixes", return_value=("tb/", "tb\\"))
    @patch("booley.specialists.specialist._call_agent_sync")
    def test_tb_quality_system_prompt_has_methodology(
        self,
        mock_agent,
        _mock_tb,
        state_file: Path,
    ):
        """Quality focus system prompt contains review methodology."""
        mock_agent.return_value = _make_agent_result([])
        endpoint = ReviewerSpecialist()
        endpoint.parse_args(
            [
                "--scope",
                "tb/mod_a_tb.sv",
                "--category",
                "tb",
                "--focus",
                "quality",
            ]
        )
        endpoint.read_state()
        system = endpoint._build_system_prompt("quality")
        assert "Review methodology" in system

    def test_tb_quality_system_prompt_flags_iverilog_ternary_string_display(
        self,
        state_file: Path,
    ):
        """TB guide warns about Icarus decimal-printing ternary string sentinels."""
        endpoint = ReviewerSpecialist()
        endpoint.parse_args(
            [
                "--scope",
                "tb/mod_a_tb.sv",
                "--category",
                "tb",
                "--focus",
                "quality",
            ]
        )
        endpoint.read_state()
        system = endpoint._build_system_prompt("quality")
        assert "Ternary string expression in display/sentinel calls" in system
        assert "$display((total_failed == 0) ?" in system
        assert "print a large decimal" in system

    def test_output_instructions_pin_category_to_focus(self):
        output = ReviewerSpecialist._output_instructions("protocol")

        assert '- category:       "protocol"' in output
        assert '"category": "protocol"' in output
        assert "category:       string" not in output


# ---------------------------------------------------------------------------
# One-shot guard
# ---------------------------------------------------------------------------


class TestOneShotGuard:
    @patch("booley.specialists.specialist._call_agent_sync")
    def test_one_shot_guard(self, mock_agent, state_file: Path):
        """Pre-set review_rtl_bugs_done => no re-run, benign exit 0 (F-49)."""
        # Pre-set the criterion as met
        st = DevelopmentState.load(state_file)
        st.set_criterion("review_rtl_bugs_done", met=True)
        st.save()

        endpoint = ReviewerSpecialist()
        endpoint.parse_args(
            [
                "--scope",
                "rtl/mod_a.sv",
                "--category",
                "rtl",
                "--focus",
                "bugs",
            ]
        )
        endpoint.read_state()
        result = endpoint._run()
        # A policy refusal is NOT an infra crash: exit 2 is reserved for
        # "the MCP tool could not run" (docs/BOOLEY-FLOWS.md exit taxonomy).
        assert result.exit_code == 0
        assert mock_agent.call_count == 0
        assert "already completed" in result.report_text
        assert "did NOT re-run" in result.report_text
        assert result.criterion_met is True

    @patch("booley.specialists.specialist._call_agent_sync")
    def test_one_shot_replays_prior_verdict_verbatim(self, mock_agent, state_file: Path):
        """The replayed report repeats the recorded findings, not just a refusal (F-49)."""
        st = DevelopmentState.load(state_file)
        st.set_criterion(
            "review_rtl_bugs_done",
            met=True,
            detail={
                "issues": 1,
                "issue_list": [
                    {
                        "severity": "MINOR",
                        "confidence": "HIGH",
                        "category": "bugs",
                        "file": "rtl/mod_a.sv",
                        "line": 42,
                        "summary": "unused signal",
                    }
                ],
                "gate_passed": True,
            },
        )
        st.save()

        endpoint = ReviewerSpecialist()
        endpoint.parse_args(["--scope", "rtl/mod_a.sv", "--category", "rtl", "--focus", "bugs"])
        endpoint.read_state()
        result = endpoint._run()

        assert result.exit_code == 0
        assert mock_agent.call_count == 0
        assert "RESULT: REVIEWED WITH FINDINGS (1 minor)" in result.report_text
        assert "rtl/mod_a.sv:42 — unused signal" in result.report_text
        # State is untouched: replaying must not rewrite the recorded verdict.
        assert DevelopmentState.load(state_file).is_met("review_rtl_bugs_done")


# ---------------------------------------------------------------------------
# TB ordering guard
# ---------------------------------------------------------------------------


class TestTbOrderingGuard:
    """TB ordering guard was 'quality before spec' — spec focus is now gone
    (ADR-0014).  The guard is a no-op hook; nothing to test."""


# ---------------------------------------------------------------------------
# _clean mode
# ---------------------------------------------------------------------------


class TestCleanModeInitial:
    """Tests for _clean mode: initial review pass."""

    @patch("booley.specialists.specialist._call_agent_sync")
    def test_clean_initial_gate_passes_met_immediately(
        self,
        mock_agent,
        state_file: Path,
        capsys,
    ):
        """Gate passes on initial review => criterion met immediately."""
        mock_agent.return_value = _make_agent_result([])
        st = DevelopmentState.load(state_file)
        st.set_criterion("review_rtl_bugs_clean", met=False)
        st.save()

        endpoint = ReviewerSpecialist()
        endpoint.parse_args(
            [
                "--scope",
                "rtl/mod_a.sv",
                "--category",
                "rtl",
                "--focus",
                "bugs",
            ]
        )
        endpoint.read_state()
        result = endpoint._run()

        assert result.exit_code == 0
        assert result.criterion_met is True
        assert result.criterion_key == "review_rtl_bugs_clean"
        st = DevelopmentState.load(state_file)
        assert st.is_met("review_rtl_bugs_clean") is True
        captured = capsys.readouterr()
        assert "RESULT: PASS" in captured.out

    @patch("booley.specialists.specialist._call_agent_sync")
    def test_clean_initial_gate_fails_stores_issues(
        self,
        mock_agent,
        state_file: Path,
        capsys,
    ):
        """Gate fails on initial review => criterion unmet, issues stored."""
        mock_agent.return_value = _make_agent_result(
            [
                _make_issue_dict("CRITICAL"),
                _make_issue_dict("MINOR", summary="Style nit"),
            ]
        )
        st = DevelopmentState.load(state_file)
        st.set_criterion("review_rtl_bugs_clean", met=False)
        st.save()

        endpoint = ReviewerSpecialist()
        endpoint.parse_args(
            [
                "--scope",
                "rtl/mod_a.sv",
                "--category",
                "rtl",
                "--focus",
                "bugs",
            ]
        )
        endpoint.read_state()
        result = endpoint._run()

        assert result.exit_code == 1
        assert result.criterion_met is False
        assert result.criterion_key == "review_rtl_bugs_clean"
        assert result.detail["verify_attempts"] == 0
        assert result.detail["original_issues"] == 2
        assert len(result.detail["pending"]) == 2
        assert result.detail["resolved"] == []
        st = DevelopmentState.load(state_file)
        assert st.is_met("review_rtl_bugs_clean") is False

    @patch("booley.specialists.specialist._call_agent_sync")
    def test_minor_finding_remains_open_until_fixed_or_waived(
        self,
        mock_agent,
        state_file: Path,
    ):
        mock_agent.return_value = _make_agent_result([_make_issue_dict("MINOR")])
        st = DevelopmentState.load(state_file)
        st.set_criterion("review_rtl_bugs_clean", met=False)
        st.save()

        endpoint = ReviewerSpecialist()
        endpoint.parse_args(_review_args())
        endpoint.read_state()
        result = endpoint._run()

        assert result.exit_code == 1
        assert result.criterion_met is False
        assert len(result.detail["pending"]) == 1

    @patch("booley.specialists.specialist._call_agent_sync")
    def test_clean_already_met_returns_early(
        self,
        mock_agent,
        state_file: Path,
    ):
        """Already met _clean criterion => immediate exit, no agent call."""
        st = DevelopmentState.load(state_file)
        st.set_criterion("review_rtl_bugs_clean", met=True)
        st.save()

        endpoint = ReviewerSpecialist()
        endpoint.parse_args(
            [
                "--scope",
                "rtl/mod_a.sv",
                "--category",
                "rtl",
                "--focus",
                "bugs",
            ]
        )
        endpoint.read_state()
        result = endpoint._run()

        assert result.exit_code == 0
        assert mock_agent.call_count == 0
        assert "already met" in result.report_text


class TestCleanModeVerify:
    """Tests for _clean mode: verify review pass."""

    @patch("booley.specialists.specialist._call_agent_sync")
    def test_source_change_triggers_fresh_discovery_before_clean_passes(
        self,
        mock_agent,
        state_file: Path,
    ):
        mock_agent.side_effect = [
            MagicMock(
                output=json.dumps(
                    {
                        "findings": [
                            {
                                "index": 1,
                                "status": "FIXED",
                                "evidence": "rtl/mod_a.sv:5 — original bug fixed",
                            }
                        ]
                    }
                ),
                structured=None,
            ),
            _make_agent_result([_make_issue_dict("MINOR", summary="New final-state issue")]),
        ]
        st = DevelopmentState.load(state_file)
        st.set_criterion(
            "review_rtl_bugs_clean",
            met=False,
            detail={
                "issues": 1,
                "pending": [_make_issue_dict("MAJOR")],
                "resolved": [],
                "verify_attempts": 0,
                "original_issues": 1,
                "review_source_digest": "prior-source-digest",
            },
        )
        st.save()

        endpoint = ReviewerSpecialist()
        endpoint.parse_args(_review_args())
        endpoint.read_state()
        result = endpoint._run()

        assert mock_agent.call_count == 2
        assert result.exit_code == 1
        assert result.criterion_met is False
        assert result.detail["pending"][0]["summary"] == "New final-state issue"

    @patch("booley.specialists.specialist._call_agent_sync")
    def test_justified_waiver_resolves_finding_and_is_persisted(
        self,
        mock_agent,
        state_file: Path,
    ):
        mock_agent.return_value = MagicMock(
            output=json.dumps(
                {
                    "findings": [
                        {
                            "index": 1,
                            "status": "WAIVED",
                            "justification": "The ticket requires this observable latency.",
                        }
                    ]
                }
            ),
            structured=None,
        )
        st = DevelopmentState.load(state_file)
        st.set_criterion(
            "review_rtl_bugs_clean",
            met=False,
            detail={
                "issues": 1,
                "pending": [_make_issue_dict("MINOR")],
                "resolved": [],
                "verify_attempts": 0,
                "original_issues": 1,
            },
        )
        st.save()

        endpoint = ReviewerSpecialist()
        endpoint.parse_args([*_review_args(), "--steer", "Waive finding 1: required latency"])
        endpoint.read_state()
        result = endpoint._run()

        assert result.exit_code == 0
        assert result.criterion_met is True
        waiver = result.detail["resolved"][0]
        assert waiver["status"] == "waived"
        assert waiver["justification"] == "The ticket requires this observable latency."
        assert "WAIVED MINOR" in "\n".join(result.display_lines)
        assert "ACCEPTED WAIVERS (user-visible)" in result.report_text
        assert "Justification: The ticket requires this observable latency." in result.report_text

    @patch("booley.specialists.specialist._call_agent_sync")
    def test_verify_all_fixed_becomes_met(
        self,
        mock_agent,
        state_file: Path,
        capsys,
    ):
        """All findings fixed (with evidence) => gate passes, criterion met."""
        mock_agent.return_value = MagicMock(
            output=json.dumps(
                {
                    "findings": [
                        {
                            "index": 1,
                            "status": "FIXED",
                            "evidence": "rtl/mod_a.sv:5 — renamed clk_i to clk",
                        },
                        {
                            "index": 2,
                            "status": "FIXED",
                            "evidence": "rtl/mod_a.sv:42 — added rst_n reset block",
                        },
                    ]
                }
            ),
            structured=None,
        )
        st = DevelopmentState.load(state_file)
        st.set_criterion(
            "review_rtl_bugs_clean",
            met=False,
            detail={
                "issues": 2,
                "pending": [
                    _make_issue_dict("CRITICAL"),
                    _make_issue_dict("MAJOR", summary="Missing reset"),
                ],
                "resolved": [],
                "CRITICAL": 1,
                "MAJOR": 1,
                "MINOR": 0,
                "verify_attempts": 0,
                "original_issues": 2,
                "elapsed_s": 30.0,
            },
        )
        st.save()

        endpoint = ReviewerSpecialist()
        endpoint.parse_args(
            [
                "--scope",
                "rtl/mod_a.sv",
                "--category",
                "rtl",
                "--focus",
                "bugs",
            ]
        )
        endpoint.read_state()
        result = endpoint._run()

        assert result.exit_code == 0
        assert result.criterion_met is True
        assert result.detail["verify_attempts"] == 1
        assert result.detail["issues"] == 0
        assert result.detail["pending"] == []
        assert len(result.detail["resolved"]) == 2
        assert all(r["status"] == "fixed" for r in result.detail["resolved"])
        st = DevelopmentState.load(state_file)
        assert st.is_met("review_rtl_bugs_clean") is True

    @patch("booley.specialists.specialist._call_agent_sync")
    def test_verify_some_still_present(
        self,
        mock_agent,
        state_file: Path,
        capsys,
    ):
        """Some findings remain => criterion still unmet, attempts incremented."""
        mock_agent.return_value = MagicMock(
            output=json.dumps(
                {
                    "findings": [
                        {
                            "index": 1,
                            "status": "FIXED",
                            "evidence": "rtl/mod_a.sv:5 — clk renamed",
                        },
                        {"index": 2, "status": "STILL_PRESENT"},
                    ]
                }
            ),
            structured=None,
        )
        st = DevelopmentState.load(state_file)
        st.set_criterion(
            "review_rtl_bugs_clean",
            met=False,
            detail={
                "issues": 2,
                "pending": [
                    _make_issue_dict("CRITICAL"),
                    _make_issue_dict("MAJOR", summary="Missing reset"),
                ],
                "resolved": [],
                "CRITICAL": 1,
                "MAJOR": 1,
                "MINOR": 0,
                "verify_attempts": 0,
                "original_issues": 2,
                "elapsed_s": 30.0,
            },
        )
        st.save()

        endpoint = ReviewerSpecialist()
        endpoint.parse_args(
            [
                "--scope",
                "rtl/mod_a.sv",
                "--category",
                "rtl",
                "--focus",
                "bugs",
            ]
        )
        endpoint.read_state()
        result = endpoint._run()

        assert result.exit_code == 1
        assert result.criterion_met is False
        assert result.detail["verify_attempts"] == 1
        assert result.detail["issues"] == 1
        assert result.detail["original_issues"] == 2
        # Split into open vs closed
        assert len(result.detail["pending"]) == 1
        assert result.detail["pending"][0]["status"] == "still_present"
        assert len(result.detail["resolved"]) == 1
        assert result.detail["resolved"][0]["status"] == "fixed"

    @patch("booley.specialists.reviewer._get_tb_prefixes", return_value=("verif/", "verif\\"))
    @patch("booley.specialists.specialist._call_agent_sync")
    def test_verify_stale_dump_call_finding_checks_current_source(
        self,
        mock_agent,
        _mock_tb_prefixes,
        state_file: Path,
        tmp_path: Path,
    ):
        """A dump-call false positive must not block when current source lacks it."""
        tb_path = tmp_path / "verif" / "lane1" / "tb_aes_encrypt.sv"
        tb_path.parent.mkdir(parents=True)
        tb_path.write_text(
            "module tb_aes_encrypt;\n"
            "  initial swap_index = i + ($urandom(seed) % 8);\n"
            "endmodule\n",
            encoding="utf-8",
        )
        mock_agent.return_value = MagicMock(
            output=json.dumps(
                {
                    "findings": [
                        {"index": 1, "status": "STILL_PRESENT"},
                    ]
                }
            ),
            structured=None,
        )
        st = DevelopmentState.load(state_file)
        st.set_criterion(
            "review_tb_quality_clean",
            met=False,
            detail={
                "issues": 1,
                "pending": [
                    _make_issue_dict(
                        "CRITICAL",
                        category="quality",
                        file="verif/lane1/tb_aes_encrypt.sv",
                        line=2,
                        summary=(
                            "Testbench contains user-authored $dumpfile/$dumpvars "
                            "calls that override harness tracing"
                        ),
                    )
                ],
                "resolved": [],
                "CRITICAL": 1,
                "MAJOR": 0,
                "MINOR": 0,
                "verify_attempts": 0,
                "original_issues": 1,
                "elapsed_s": 30.0,
            },
        )
        st.save()

        endpoint = ReviewerSpecialist()
        endpoint.parse_args(
            [
                "--work-dir",
                str(tmp_path),
                "--scope",
                "verif/lane1/tb_aes_encrypt.sv",
                "--category",
                "tb",
                "--focus",
                "quality",
            ]
        )
        endpoint.read_state()
        result = endpoint._run()

        assert result.exit_code == 0
        assert result.criterion_met is True
        assert result.detail["pending"] == []
        assert result.detail["resolved"][0]["status"] == "fixed"

    @patch("booley.specialists.specialist._call_agent_sync")
    def test_verify_attempts_exhausted(
        self,
        mock_agent,
        state_file: Path,
    ):
        """Two verify attempts already done => exit error, no agent call."""
        st = DevelopmentState.load(state_file)
        st.set_criterion(
            "review_rtl_bugs_clean",
            met=False,
            detail={
                "issues": 1,
                "issue_list": [_make_issue_dict("CRITICAL")],
                "CRITICAL": 1,
                "MAJOR": 0,
                "MINOR": 0,
                "verify_attempts": 2,
                "original_issues": 1,
                "elapsed_s": 30.0,
            },
        )
        st.save()

        endpoint = ReviewerSpecialist()
        endpoint.parse_args(
            [
                "--scope",
                "rtl/mod_a.sv",
                "--category",
                "rtl",
                "--focus",
                "bugs",
            ]
        )
        endpoint.read_state()
        result = endpoint._run()

        assert result.exit_code == 1
        assert mock_agent.call_count == 0
        assert "2 verify attempts exhausted" in result.report_text

    @patch("booley.specialists.specialist._call_agent_sync")
    def test_verify_malformed_json_conservative(
        self,
        mock_agent,
        state_file: Path,
    ):
        """Malformed verify JSON => all issues treated as still_present."""
        mock_agent.return_value = MagicMock(
            output="I couldn't parse the code properly, here are my thoughts...",
            structured=None,
        )
        st = DevelopmentState.load(state_file)
        st.set_criterion(
            "review_rtl_bugs_clean",
            met=False,
            detail={
                "issues": 2,
                "issue_list": [
                    _make_issue_dict("CRITICAL"),
                    _make_issue_dict("MAJOR", summary="Missing reset"),
                ],
                "CRITICAL": 1,
                "MAJOR": 1,
                "MINOR": 0,
                "verify_attempts": 0,
                "original_issues": 2,
                "elapsed_s": 30.0,
            },
        )
        st.save()

        endpoint = ReviewerSpecialist()
        endpoint.parse_args(
            [
                "--scope",
                "rtl/mod_a.sv",
                "--category",
                "rtl",
                "--focus",
                "bugs",
            ]
        )
        endpoint.read_state()
        result = endpoint._run()

        assert result.exit_code == 1
        assert result.criterion_met is False
        assert result.detail["issues"] == 2
        assert result.detail["verify_attempts"] == 1

    @patch("booley.specialists.specialist._call_agent_sync")
    def test_verify_fixed_without_evidence_is_demoted(
        self,
        mock_agent,
        state_file: Path,
    ):
        """FIXED claims without ``evidence`` are demoted to STILL_PRESENT.

        Blocks the rubber-stamp pattern where the agent reads the file
        once and emits ``status: FIXED`` for every original finding
        without anchoring to the actual code change.
        """
        mock_agent.return_value = MagicMock(
            # Three FIXED claims, none with evidence — should all demote.
            output=json.dumps(
                {
                    "findings": [
                        {"index": 1, "status": "FIXED"},
                        {"index": 2, "status": "FIXED", "evidence": ""},
                        {"index": 3, "status": "FIXED", "evidence": "   "},
                    ]
                }
            ),
            structured=None,
        )
        st = DevelopmentState.load(state_file)
        st.set_criterion(
            "review_rtl_bugs_clean",
            met=False,
            detail={
                "issues": 3,
                "pending": [
                    _make_issue_dict("CRITICAL"),
                    _make_issue_dict("MAJOR", summary="Reset missing"),
                    _make_issue_dict("MAJOR", summary="Writeback timing"),
                ],
                "resolved": [],
                "CRITICAL": 1,
                "MAJOR": 2,
                "MINOR": 0,
                "verify_attempts": 0,
                "original_issues": 3,
                "elapsed_s": 30.0,
            },
        )
        st.save()

        endpoint = ReviewerSpecialist()
        endpoint.parse_args(
            [
                "--scope",
                "rtl/mod_a.sv",
                "--category",
                "rtl",
                "--focus",
                "bugs",
            ]
        )
        endpoint.read_state()
        result = endpoint._run()

        # All three demoted to still_present → gate fails, met=False.
        assert result.criterion_met is False
        assert result.exit_code == 1
        assert len(result.detail["pending"]) == 3
        assert result.detail["resolved"] == []
        assert all(p["status"] == "still_present" for p in result.detail["pending"])

    @patch("booley.specialists.specialist._call_agent_sync")
    def test_verify_fixed_with_evidence_is_accepted(
        self,
        mock_agent,
        state_file: Path,
    ):
        """FIXED with a non-blank ``evidence`` string is accepted as fixed."""
        mock_agent.return_value = MagicMock(
            output=json.dumps(
                {
                    "findings": [
                        {
                            "index": 1,
                            "status": "FIXED",
                            "evidence": "rtl/mod_a.sv:5 — port renamed clk_i→clk",
                        },
                    ]
                }
            ),
            structured=None,
        )
        st = DevelopmentState.load(state_file)
        st.set_criterion(
            "review_rtl_bugs_clean",
            met=False,
            detail={
                "issues": 1,
                "pending": [_make_issue_dict("CRITICAL")],
                "resolved": [],
                "CRITICAL": 1,
                "MAJOR": 0,
                "MINOR": 0,
                "verify_attempts": 0,
                "original_issues": 1,
                "elapsed_s": 30.0,
            },
        )
        st.save()

        endpoint = ReviewerSpecialist()
        endpoint.parse_args(
            [
                "--scope",
                "rtl/mod_a.sv",
                "--category",
                "rtl",
                "--focus",
                "bugs",
            ]
        )
        endpoint.read_state()
        result = endpoint._run()

        assert result.criterion_met is True
        assert result.detail["pending"] == []
        assert len(result.detail["resolved"]) == 1


class TestMutualExclusionGuard:
    """Tests for _done/_clean mutual exclusion."""

    def test_both_done_and_clean_exits_error(self, state_file: Path):
        """Both _done and _clean for same base key => exit error."""
        st = DevelopmentState.load(state_file)
        st.set_criterion("review_rtl_bugs_done", met=True)
        st.set_criterion("review_rtl_bugs_clean", met=False)
        st.save()

        endpoint = ReviewerSpecialist()
        endpoint.parse_args(
            [
                "--scope",
                "rtl/mod_a.sv",
                "--category",
                "rtl",
                "--focus",
                "bugs",
            ]
        )
        endpoint.read_state()
        result = endpoint._run()

        assert result.exit_code == 2
        assert "Mutual exclusion" in result.report_text


# ---------------------------------------------------------------------------
# Bounded clean-review impasse
# ---------------------------------------------------------------------------


class TestCleanReviewImpasse:
    @patch("booley.specialists.specialist._call_agent_sync")
    def test_impasse_never_creates_automatic_waiver(self, mock_agent, state_file: Path):
        st = DevelopmentState.load(state_file)
        finding = _make_issue_dict("MAJOR", summary="Still unresolved")
        st.set_criterion(
            "review_rtl_bugs_clean",
            met=False,
            detail={
                "issues": 1,
                "pending": [finding],
                "resolved": [],
                "total_verify_cycles": 3,
            },
        )
        st.save()

        endpoint = ReviewerSpecialist()
        endpoint.parse_args(_review_args())
        endpoint.read_state()
        result = endpoint._run()

        assert result.exit_code == 1
        assert "without creating an automatic waiver" in result.report_text
        assert mock_agent.call_count == 0
        detail = DevelopmentState.load(state_file).criteria["review_rtl_bugs_clean"].detail
        assert detail["pending"] == [finding]
        assert detail["resolved"] == []


# ---------------------------------------------------------------------------
# Finding identity: index-based matching
# ---------------------------------------------------------------------------


class TestFindingIdentityByIndex:
    """Verify that status annotation uses issue_list index, not (file, line, summary) tuple."""

    @patch("booley.specialists.specialist._call_agent_sync")
    def test_line_shift_does_not_lose_fix_status(
        self,
        mock_agent,
        state_file: Path,
    ):
        """If coder inserts lines, shifting the issue's line number,
        index-based matching still marks it correctly."""
        # Agent says index 1 is fixed (the CRITICAL at original line 42)
        mock_agent.return_value = MagicMock(
            output=json.dumps(
                {
                    "findings": [
                        {
                            "index": 1,
                            "status": "FIXED",
                            "evidence": "rtl/mod_a.sv:42 — off-by-one corrected",
                        },
                        {"index": 2, "status": "STILL_PRESENT"},
                    ]
                }
            ),
            structured=None,
        )
        st = DevelopmentState.load(state_file)
        st.set_criterion(
            "review_rtl_bugs_clean",
            met=False,
            detail={
                "issues": 2,
                "pending": [
                    _make_issue_dict(
                        "CRITICAL", file="rtl/mod_a.sv", line=42, summary="Off by one"
                    ),
                    _make_issue_dict(
                        "MAJOR", file="rtl/mod_a.sv", line=50, summary="Missing reset"
                    ),
                ],
                "resolved": [],
                "CRITICAL": 1,
                "MAJOR": 1,
                "MINOR": 0,
                "verify_attempts": 0,
                "original_issues": 2,
                "elapsed_s": 30.0,
            },
        )
        st.save()

        endpoint = ReviewerSpecialist()
        endpoint.parse_args(
            [
                "--scope",
                "rtl/mod_a.sv",
                "--category",
                "rtl",
                "--focus",
                "bugs",
            ]
        )
        endpoint.read_state()
        result = endpoint._run()

        assert result.criterion_met is False
        assert len(result.detail["resolved"]) == 1
        assert result.detail["resolved"][0]["summary"] == "Off by one"
        assert result.detail["resolved"][0]["status"] == "fixed"
        assert len(result.detail["pending"]) == 1
        assert result.detail["pending"][0]["summary"] == "Missing reset"
        assert result.detail["pending"][0]["status"] == "still_present"

    @patch("booley.specialists.specialist._call_agent_sync")
    def test_summary_rephrase_does_not_affect_matching(
        self,
        mock_agent,
        state_file: Path,
    ):
        """Even if summaries differ between original and verify, index wins."""
        mock_agent.return_value = MagicMock(
            output=json.dumps(
                {
                    "findings": [
                        {
                            "index": 1,
                            "status": "FIXED",
                            "evidence": "rtl/mod_a.sv:10 — addressed by recent edit",
                        },
                    ]
                }
            ),
            structured=None,
        )
        st = DevelopmentState.load(state_file)
        st.set_criterion(
            "review_rtl_bugs_clean",
            met=False,
            detail={
                "issues": 1,
                "pending": [
                    _make_issue_dict("MAJOR", summary="Original wording of the bug"),
                ],
                "resolved": [],
                "CRITICAL": 0,
                "MAJOR": 1,
                "MINOR": 0,
                "verify_attempts": 0,
                "original_issues": 1,
                "elapsed_s": 30.0,
            },
        )
        st.save()

        endpoint = ReviewerSpecialist()
        endpoint.parse_args(
            [
                "--scope",
                "rtl/mod_a.sv",
                "--category",
                "rtl",
                "--focus",
                "bugs",
            ]
        )
        endpoint.read_state()
        result = endpoint._run()

        assert result.criterion_met is True
        assert result.detail["pending"] == []
        assert result.detail["resolved"][0]["status"] == "fixed"


# ---------------------------------------------------------------------------
# Previously-fixed findings: persistence and regression
# ---------------------------------------------------------------------------


class TestVerifyFixedPersistence:
    """Test that previously-fixed findings keep their status across verify passes."""

    @patch("booley.specialists.specialist._call_agent_sync")
    def test_previously_fixed_stays_fixed_when_unmentioned(
        self,
        mock_agent,
        state_file: Path,
    ):
        """Model doesn't mention issue 1 (prior status=fixed) => stays fixed.

        Setup uses the legacy ``issue_list`` field to exercise the
        backward-compat fallback path for in-flight tickets that
        predate the pending/resolved split.
        """
        # Only mentions issue 2 as still present; omits issue 1
        mock_agent.return_value = MagicMock(
            output=json.dumps(
                {
                    "findings": [
                        {"index": 2, "status": "STILL_PRESENT"},
                    ]
                }
            ),
            structured=None,
        )
        st = DevelopmentState.load(state_file)
        st.set_criterion(
            "review_rtl_bugs_clean",
            met=False,
            detail={
                "issues": 2,
                "issue_list": [
                    {**_make_issue_dict("MAJOR", summary="Bug A"), "status": "fixed"},
                    {**_make_issue_dict("MAJOR", summary="Bug B"), "status": "still_present"},
                ],
                "CRITICAL": 0,
                "MAJOR": 1,
                "MINOR": 0,
                "verify_attempts": 0,
                "original_issues": 2,
                "elapsed_s": 30.0,
            },
        )
        st.save()

        endpoint = ReviewerSpecialist()
        endpoint.parse_args(
            [
                "--scope",
                "rtl/mod_a.sv",
                "--category",
                "rtl",
                "--focus",
                "bugs",
            ]
        )
        endpoint.read_state()
        result = endpoint._run()

        # Bug A (prior status=fixed, unmentioned) → stays fixed → resolved
        # Bug B (explicitly STILL_PRESENT) → stays in pending
        assert len(result.detail["resolved"]) == 1
        assert result.detail["resolved"][0]["summary"] == "Bug A"
        assert result.detail["resolved"][0]["status"] == "fixed"
        assert len(result.detail["pending"]) == 1
        assert result.detail["pending"][0]["summary"] == "Bug B"
        assert result.detail["pending"][0]["status"] == "still_present"

    @patch("booley.specialists.specialist._call_agent_sync")
    def test_previously_fixed_can_regress(
        self,
        mock_agent,
        state_file: Path,
    ):
        """Model explicitly says STILL_PRESENT for a previously-fixed issue => regresses.

        Setup uses the legacy ``issue_list`` field — the fallback path
        should still classify items correctly into pending vs resolved.
        """
        mock_agent.return_value = MagicMock(
            output=json.dumps(
                {
                    "findings": [
                        {"index": 1, "status": "STILL_PRESENT"},
                        {
                            "index": 2,
                            "status": "FIXED",
                            "evidence": "rtl/mod_a.sv:7 — bug B addressed",
                        },
                    ]
                }
            ),
            structured=None,
        )
        st = DevelopmentState.load(state_file)
        st.set_criterion(
            "review_rtl_bugs_clean",
            met=False,
            detail={
                "issues": 2,
                "issue_list": [
                    {**_make_issue_dict("MAJOR", summary="Bug A"), "status": "fixed"},
                    {**_make_issue_dict("MAJOR", summary="Bug B"), "status": "still_present"},
                ],
                "CRITICAL": 0,
                "MAJOR": 2,
                "MINOR": 0,
                "verify_attempts": 0,
                "original_issues": 2,
                "elapsed_s": 30.0,
            },
        )
        st.save()

        endpoint = ReviewerSpecialist()
        endpoint.parse_args(
            [
                "--scope",
                "rtl/mod_a.sv",
                "--category",
                "rtl",
                "--focus",
                "bugs",
            ]
        )
        endpoint.read_state()
        result = endpoint._run()

        # Bug A regressed → pending; Bug B fixed → resolved.
        assert len(result.detail["pending"]) == 1
        assert result.detail["pending"][0]["summary"] == "Bug A"
        assert result.detail["pending"][0]["status"] == "still_present"
        assert len(result.detail["resolved"]) == 1
        assert result.detail["resolved"][0]["summary"] == "Bug B"
        assert result.detail["resolved"][0]["status"] == "fixed"


# ---------------------------------------------------------------------------
# Contradicting-instruction fixes (benchmark batch-01 prompt audit)
# ---------------------------------------------------------------------------


def _tb_args(scope: str = "verif/mod_a_tb.sv") -> list[str]:
    return ["--scope", scope, "--category", "tb", "--focus", "quality"]


@pytest.fixture()
def ticket_logs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A logs dir with a mounted ticket snapshot, as Ticket Mode provides."""
    logs = tmp_path / "logs"
    logs.mkdir()
    monkeypatch.setenv("BOOLEY_LOGS_DIR", str(logs))
    return logs


def _write_ticket(
    logs: Path, ticket_type: str = "feature", body: str = "Latency is 3 cycles."
) -> None:
    (logs / "ticket.md").write_text(
        f"---\nsummary: t\ntype: {ticket_type}\n---\n\n{body}\n", encoding="utf-8"
    )


class TestTbReviewGetsTheSpec:
    """A TB reviewer graded on spec traceability needs the spec in hand.

    Checks like "every numeric contract the spec states must be asserted" were
    unanswerable: the spec was inlined for RTL spec-focus reviews only.
    """

    def test_tb_prompt_inlines_the_specification(self, ticket_logs: Path, state_file: Path):
        _write_ticket(ticket_logs, body="The output latency is 3 cycles.")
        endpoint = ReviewerSpecialist()
        endpoint.parse_args(_tb_args())

        prompt = endpoint._build_prompt()

        assert "## Specification" in prompt
        assert "latency is 3 cycles" in prompt

    def test_tb_prompt_survives_a_missing_spec(self, tmp_path: Path, state_file: Path):
        """Absence is not fatal on the TB side — unlike spec focus, which guards."""
        endpoint = ReviewerSpecialist()
        endpoint.parse_args(_tb_args())

        prompt = endpoint._build_prompt()

        assert "## Specification" not in prompt
        assert "## Focus: quality" in prompt


class TestDocumentedAssumptions:
    """A recorded judgement call must not read to the reviewer as an invention."""

    def test_assumptions_are_inlined_when_present(self, ticket_logs: Path, state_file: Path):
        _write_ticket(ticket_logs)
        (ticket_logs / "answered_questions.md").write_text(
            "- Spec is silent on rate=0; treated as RMAX.\n", encoding="utf-8"
        )
        endpoint = ReviewerSpecialist()
        endpoint.parse_args(["--scope", "rtl/mod_a.sv", "--category", "rtl", "--focus", "spec"])

        prompt = endpoint._build_prompt()

        assert "## Documented Assumptions" in prompt
        assert "rate=0" in prompt
        assert "not an unexplained invention" in prompt

    def test_no_section_without_the_file(self, ticket_logs: Path, state_file: Path):
        _write_ticket(ticket_logs)
        endpoint = ReviewerSpecialist()
        endpoint.parse_args(["--scope", "rtl/mod_a.sv", "--category", "rtl", "--focus", "spec"])

        assert "## Documented Assumptions" not in endpoint._build_prompt()


class TestTicketTypeCoveragePolicy:
    """bugfix/refactor are told to minimize change; the TB gate demanded new scenarios."""

    @pytest.mark.parametrize("ticket_type", ["bugfix", "refactor"])
    def test_coverage_checks_are_demoted(
        self, ticket_type: str, ticket_logs: Path, state_file: Path
    ):
        _write_ticket(ticket_logs, ticket_type=ticket_type)
        endpoint = ReviewerSpecialist()
        endpoint.parse_args(_tb_args())

        prompt = endpoint._build_prompt()

        assert f"## Ticket Type: {ticket_type}" in prompt
        assert "MINOR at most" in prompt
        assert "no randomized vectors" in prompt
        # False-pass checks are never relaxed.
        assert "no ticket type excuses those" in prompt.lower()

    @pytest.mark.parametrize("ticket_type", ["feature", "verification"])
    def test_full_strength_for_authoring_types(
        self, ticket_type: str, ticket_logs: Path, state_file: Path
    ):
        _write_ticket(ticket_logs, ticket_type=ticket_type)
        endpoint = ReviewerSpecialist()
        endpoint.parse_args(_tb_args())

        assert "## Ticket Type" not in endpoint._build_prompt()

    def test_rtl_reviews_carry_no_coverage_policy(self, ticket_logs: Path, state_file: Path):
        """The RTL guides have no coverage-expansion checks to relax."""
        _write_ticket(ticket_logs, ticket_type="bugfix")
        endpoint = ReviewerSpecialist()
        endpoint.parse_args(["--scope", "rtl/mod_a.sv", "--category", "rtl", "--focus", "spec"])

        assert "## Ticket Type" not in endpoint._build_prompt()


class TestSchemaExampleMatchesCategory:
    """A TB reviewer told not to read RTL was shown an rtl/ path to report."""

    def test_tb_example_is_a_tb_path(self):
        text = ReviewerSpecialist._output_instructions("quality", "tb")
        assert "verif/mod_a_tb.sv" in text
        assert "rtl/mod_a.sv" not in text

    def test_rtl_example_is_an_rtl_path(self):
        assert "rtl/mod_a.sv" in ReviewerSpecialist._output_instructions("spec", "rtl")

    def test_default_stays_rtl(self):
        assert "rtl/mod_a.sv" in ReviewerSpecialist._output_instructions("bugs")


class TestDeadEndMessagesNameARealAction:
    """Both exhaustion paths used to point the agent at "triage" — an operator
    workflow the agent cannot invoke, and which does not exist at all in an
    unattended run."""

    @patch("booley.specialists.specialist._call_agent_sync")
    def test_completed_done_message_names_clean_mode(self, mock_agent, state_file: Path):
        common = ["--scope", "rtl/mod_a.sv", "--category", "rtl", "--focus", "bugs"]
        mock_agent.return_value = _make_agent_result([_make_issue_dict("CRITICAL")])

        endpoint = ReviewerSpecialist()
        endpoint.parse_args(common)
        endpoint.read_state()
        endpoint._run()

        endpoint = ReviewerSpecialist()
        endpoint.parse_args([*common, "--steer", "fixed finding"])
        endpoint.read_state()
        result = endpoint._run()

        assert result.exit_code == 0
        assert "triage" not in result.report_text.lower()
        assert "_clean" in result.report_text
        assert mock_agent.call_count == 1

    def test_replay_message(self):
        from booley.specialists import reviewer as reviewer_mod

        source = Path(reviewer_mod.__file__).read_text(encoding="utf-8")
        assert "raise it in triage instead" not in source
        assert "resolve this through triage" not in source


# ---------------------------------------------------------------------------
# Diff boundary and project-policy regression (Pico RISC-V32 F-6)
# ---------------------------------------------------------------------------


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _make_pico_tb_repo(repo: Path) -> tuple[str, Path]:
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "reviewer-test@example.invalid")
    _git(repo, "config", "user.name", "Reviewer Test")
    tb = repo / "verif" / "testbench.v"
    tb.parent.mkdir()
    tb.write_text(
        "module testbench;\n"
        "  integer feature_mode;\n"
        "  initial begin\n"
        '    if ($test$plusargs("vcd")) begin\n'
        '      $dumpfile("dump.vcd");\n'
        "      $dumpvars(0, testbench);\n"
        "    end\n"
        '    $display("ERROR!");\n'
        '    $display("TIMEOUT");\n'
        '    $display("ALL TESTS PASSED.");\n'
        "    feature_mode = 0;\n"
        "  end\n"
        "endmodule\n",
        encoding="utf-8",
    )
    project = repo / ".booley_project"
    project.mkdir()
    (project / "booley.toml").write_text(
        "[flows.sim]\n"
        'pass_sentinels = ["ALL TESTS PASSED."]\n'
        'fail_sentinels = ["ERROR!", "TIMEOUT"]\n'
        'trace_files = ["dump.vcd"]\n',
        encoding="utf-8",
    )
    _git(repo, "add", "verif/testbench.v")
    _git(repo, "add", "-f", ".booley_project/booley.toml")
    _git(repo, "commit", "-qm", "baseline")
    base = _git(repo, "rev-parse", "HEAD")
    tb.write_text(tb.read_text(encoding="utf-8").replace("feature_mode = 0", "feature_mode = 1"))
    return base, tb


class TestDiffBoundary:
    @patch("booley.specialists.reviewer._get_tb_prefixes", return_value=("verif/", "verif\\"))
    @patch("booley.specialists.specialist._call_agent_sync")
    def test_unchanged_baseline_findings_cannot_fail_gate(
        self,
        mock_agent,
        _mock_tb_prefixes,
        state_file: Path,
        tmp_path: Path,
    ):
        base, _ = _make_pico_tb_repo(tmp_path)
        mock_agent.return_value = _make_agent_result(
            [
                _make_issue_dict(
                    "CRITICAL", "quality", "verif/testbench.v", 5, "$dumpfile is forbidden"
                ),
                _make_issue_dict(
                    "CRITICAL", "quality", "verif/testbench.v", 6, "$dumpvars is forbidden"
                ),
                _make_issue_dict(
                    "CRITICAL",
                    "quality",
                    "verif/testbench.v",
                    8,
                    "Missing [SIM_RESULT] pass sentinel",
                ),
                _make_issue_dict(
                    "CRITICAL",
                    "quality",
                    "verif/testbench.v",
                    10,
                    "Nonstandard verdict sentinel",
                ),
            ]
        )
        endpoint = ReviewerSpecialist()
        endpoint.parse_args(
            [
                *_tb_args("verif/testbench.v"),
                "--work-dir",
                str(tmp_path),
                "--diff-ref",
                base,
            ]
        )
        endpoint.read_state()

        result = endpoint._run()

        assert result.exit_code == 0
        assert result.detail["issues"] == 0
        prompt = mock_agent.call_args.args[0].prompt
        assert "## Enforced Diff Boundary" in prompt
        assert "verif/testbench.v: 11" in prompt
        assert "feature_mode = 1" in prompt
        assert "## Project Simulation Contract" in prompt
        assert "ALL TESTS PASSED." in prompt
        assert "dump.vcd" in prompt

    @patch("booley.specialists.reviewer._get_tb_prefixes", return_value=("verif/", "verif\\"))
    @patch("booley.specialists.specialist._call_agent_sync")
    def test_configured_tb_owned_trace_is_not_a_finding_on_changed_line(
        self,
        mock_agent,
        _mock_tb_prefixes,
        state_file: Path,
        tmp_path: Path,
    ):
        base, tb = _make_pico_tb_repo(tmp_path)
        tb.write_text(
            tb.read_text(encoding="utf-8").replace(
                '$dumpfile("dump.vcd");',
                '$dumpfile("dump.vcd"); // project-owned trace',
            ),
            encoding="utf-8",
        )
        mock_agent.return_value = _make_agent_result(
            [
                _make_issue_dict(
                    "CRITICAL",
                    "quality",
                    "verif/testbench.v",
                    5,
                    "User-authored $dumpfile is forbidden; remove all dump calls",
                )
            ]
        )
        endpoint = ReviewerSpecialist()
        endpoint.parse_args(
            [
                *_tb_args("verif/testbench.v"),
                "--work-dir",
                str(tmp_path),
                "--diff-ref",
                base,
            ]
        )
        endpoint.read_state()

        result = endpoint._run()

        assert result.exit_code == 0
        assert result.detail["issues"] == 0
        assert "project's configured sentinel/trace contract" in result.report_text

    @patch("booley.specialists.reviewer._get_tb_prefixes", return_value=("verif/", "verif\\"))
    @patch("booley.specialists.specialist._call_agent_sync")
    def test_clean_retry_resolves_legacy_baseline_findings_without_agent(
        self,
        mock_agent,
        _mock_tb_prefixes,
        state_file: Path,
        tmp_path: Path,
    ):
        base, _ = _make_pico_tb_repo(tmp_path)
        stale = [
            _make_issue_dict(
                "CRITICAL", "quality", "verif/testbench.v", line, "Baseline sentinel/VCD issue"
            )
            for line in (5, 6, 8, 10)
        ]
        state = DevelopmentState.load(state_file)
        state.init_criteria({"review_tb_quality_clean": True})
        state.set_criterion(
            "review_tb_quality_clean",
            False,
            detail={
                "issues": 4,
                "pending": stale,
                "resolved": [],
                "CRITICAL": 4,
                "MAJOR": 0,
                "MINOR": 0,
                "verify_attempts": 1,
                "original_issues": 4,
            },
        )
        state.save()
        endpoint = ReviewerSpecialist()
        endpoint.parse_args(
            [
                *_tb_args("verif/testbench.v"),
                "--work-dir",
                str(tmp_path),
                "--diff-ref",
                base,
            ]
        )
        endpoint.read_state()

        result = endpoint._run()

        assert result.exit_code == 0
        assert result.criterion_met is True
        assert result.detail["pending"] == []
        assert len(result.detail["resolved"]) == 4
        mock_agent.assert_not_called()

    @patch("booley.specialists.reviewer._get_tb_prefixes", return_value=("verif/", "verif\\"))
    @patch("booley.specialists.specialist._call_agent_sync")
    def test_invalid_diff_ref_is_endpoint_error(
        self, mock_agent, _mock_tb_prefixes, state_file: Path, tmp_path: Path
    ):
        _make_pico_tb_repo(tmp_path)
        endpoint = ReviewerSpecialist()
        endpoint.parse_args(
            [
                "--scope",
                "verif/testbench.v",
                "--category",
                "tb",
                "--focus",
                "quality",
                "--work-dir",
                str(tmp_path),
                "--diff-ref",
                "does-not-exist",
            ]
        )
        endpoint.read_state()

        result = endpoint._run()

        assert result.exit_code == 2
        assert "Diff boundary error" in result.report_text
        mock_agent.assert_not_called()
