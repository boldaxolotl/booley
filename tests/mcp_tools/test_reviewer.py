"""Tests for ReviewerSpecialist -- issue parsing, channel verdicts, prompt construction.

Every review runs through the public run boundary (``run_review``) against the shared
scripted agent, so prompts are read from the captured call parameters.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any, NamedTuple

import pytest

from booley.specialists.reviewer import (
    REVIEW_GUIDES,
    RTL_FOCUS_CATEGORIES,
    SEVERITY_CRITICAL,
    SEVERITY_MAJOR,
    SEVERITY_MINOR,
    MirrorFinding,
    ReviewerSpecialist,
    ReviewIssue,
    count_by_severity,
    format_summary_line,
    parse_review_output,
    report_findings_to_mirror,
    validate_scope_category,
)
from tests.mcp_tools.reviewer_support import (
    ScriptedAgent,
    guide_of,
    install_agent,
    issue,
    run_review,
)

# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def agent(monkeypatch: pytest.MonkeyPatch) -> ScriptedAgent:
    """The scripted model behind the public ``call_agent`` seam (no workspace snapshot)."""
    return install_agent(monkeypatch)


@pytest.fixture(autouse=True)
def _run_in_tmp_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Reviews without ``--work-dir`` resolve against the cwd: keep it hermetic."""
    monkeypatch.chdir(tmp_path)


@pytest.fixture
def ticket_md(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A standalone specification document."""
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


@pytest.fixture
def review_logs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    logs = tmp_path / "review-logs"
    logs.mkdir()
    monkeypatch.setenv("BOOLEY_LOGS_DIR", str(logs))
    return logs


def _argv(category: str, focus: str, scope: str | None = None, *extra: str) -> list[str]:
    """Reviewer arguments; the scope defaults to a file of the category."""
    scope = scope or ("tb/mod_a_tb.sv" if category == "tb" else "rtl/mod_a.sv")
    return ["--scope", scope, "--category", category, "--focus", focus, *extra]


BUGS = _argv("rtl", "bugs")
CLEAN = {"issues": []}


def finding(**overrides: Any) -> dict[str, Any]:
    """A schema-valid finding inside the default ``rtl/mod_a.sv`` scope."""
    return issue(**{"file": "rtl/mod_a.sv", "line": 42, **overrides})


def prompts(agent: ScriptedAgent, argv: list[str]) -> tuple[str, str]:
    """Run a clean review; return the joined system prompts and the shared user prompt."""
    outcome, _ = run_review(argv)
    assert outcome.exit_code == 0, outcome.report_text
    assert agent.calls
    system = "\n".join(call.system_prompt or "" for call in agent.calls)
    return system, agent.calls[0].prompt


def system_of(agent: ScriptedAgent, guide: str) -> str:
    """The system prompt of the call that carries one guide."""
    (params,) = [call for call in agent.calls if guide_of(call) == guide]
    return params.system_prompt or ""


def squash(text: str) -> str:
    """Collapse whitespace so wrapped guide text compares as one line."""
    return " ".join(text.split())


# ---------------------------------------------------------------------------
# Argument surface
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("option", "value"), [("--target", "sim_other"), ("--diff-ref", "abc1234")], ids=str
)
def test_removed_options_exit_2(option: str, value: str) -> None:
    with pytest.raises(SystemExit) as excinfo:
        run_review([*_argv("rtl", "bugs"), option, value])
    assert excinfo.value.code == 2


def test_dry_run_does_not_invoke_agent(agent: ScriptedAgent, tmp_path: Path) -> None:
    source = tmp_path / "rtl/mod_a.sv"
    source.parent.mkdir()
    source.write_text("module mod_a; endmodule\n", encoding="utf-8")

    outcome, _ = run_review(["--work-dir", str(tmp_path), *BUGS, "--dry-run"])

    assert outcome.exit_code == 0
    assert outcome.detail["mode"] == "dry_run"
    assert agent.calls == []


# ---------------------------------------------------------------------------
# Issue parsing
# ---------------------------------------------------------------------------


def _without(entry: dict[str, Any], key: str) -> dict[str, Any]:
    return {k: v for k, v in entry.items() if k != key}


_PROSE_BRACES = (
    "Initial thought: something like { a stray { unbalanced ref }\n"
    "Actually here is the review:\n"
    '{"issues": [{"severity": "MINOR", "confidence": "LOW", '
    '"category": "quality", "kind": "code_defect", '
    '"disposition": "advisory", "ticket_clause": "Keep names clear.", '
    '"file": "a.sv", "line": 1, '
    '"summary": "nit"}]}'
)


class _Wrapper(NamedTuple):
    output: str
    issue_count: int
    json_present: bool | None  # None: the case does not pin it


@pytest.mark.parametrize(
    "case",
    [
        _Wrapper(
            json.dumps({"issues": [issue(severity="CRITICAL"), issue(severity="MINOR")]}),
            2,
            True,
        ),
        _Wrapper(
            "Here is my review:\n```json\n"
            + json.dumps({"issues": [issue(severity="CRITICAL")]})
            + "\n```\n",
            1,
            True,
        ),
        # Bare arrays are not the spec'd wrapper: the old greedy ``\[.*\]`` fallback
        # swallowed prose like ``[MAJOR] ...`` headings and masked real wrappers.
        _Wrapper(json.dumps([issue()]), 0, None),
        _Wrapper(json.dumps(CLEAN), 0, True),
        _Wrapper("Looks fine to me, no issues!", 0, False),
        _Wrapper("{issues: [broken json", 0, None),
        _Wrapper(_PROSE_BRACES, 1, True),
    ],
    ids=[
        "object",
        "embedded_in_text",
        "bare_array_rejected",
        "empty_list_is_json_present",
        "no_json",
        "broken_json",
        "prose_braces_do_not_mask_wrapper",
    ],
)
def test_wrapper_extraction(case: _Wrapper) -> None:
    result = parse_review_output(case.output)

    assert len(result.issues) == case.issue_count
    if case.json_present is not None:
        assert result.json_present is case.json_present


def test_brackets_in_string_dont_break_scan() -> None:
    """``]}`` inside a JSON string value is not a wrapper terminator."""
    entry = issue(spec_clause="value [a:b]} with embedded brackets")

    result = parse_review_output(json.dumps({"issues": [entry]}))

    assert result.json_present is True
    assert len(result.issues) == 1


class _Malformed(NamedTuple):
    entry: Any
    field: str
    allowed_category: str | None = None


@pytest.mark.parametrize(
    "case",
    [
        _Malformed(issue(severity="INFO"), "severity"),
        _Malformed(_without(issue(), "confidence"), "confidence"),
        _Malformed(_without(issue(), "kind"), "kind"),
        _Malformed(_without(issue(), "disposition"), "disposition"),
        _Malformed(_without(issue(), "ticket_clause"), "ticket_clause"),
        _Malformed(issue(file=""), "file"),
        _Malformed(issue(line=-5), "line"),
        _Malformed(issue(line="42"), "line"),
        _Malformed(issue(summary="   "), "summary"),
        _Malformed(_without(issue(), "category"), "category"),
        _Malformed(issue(category="protocol"), "exactly 'bugs'", "bugs"),
        _Malformed("not a dict", "JSON object"),
        _Malformed([1, 2, 3], "JSON object"),
    ],
    ids=lambda case: f"{case.field}-{str(case.entry)[:12]}",
)
def test_malformed_issue_is_rejected(case: _Malformed) -> None:
    output = json.dumps({"issues": [case.entry]})

    result = parse_review_output(output, allowed_category=case.allowed_category)

    assert result.issues == []
    assert [index for index, _ in result.rejected] == [1]
    assert case.field in str(result.rejected[0][1])


def test_lowercase_severity_accepted() -> None:
    """The enum check is case-insensitive: strictness is on the values, not the casing."""
    result = parse_review_output(json.dumps({"issues": [issue(severity="critical")]}))

    assert len(result.issues) == 1
    assert result.rejected == []


def test_parse_review_output_drops_malformed_entries() -> None:
    bad_severity = {
        "severity": "INFO",
        "confidence": "HIGH",
        "category": "x",
        "file": "a.sv",
        "line": 1,
        "summary": "s",
    }
    output = json.dumps(
        {
            "issues": [
                issue(severity="CRITICAL"),
                {"severity": "MAJOR"},  # missing required fields
                issue(severity="MINOR"),
                bad_severity,
            ],
        }
    )

    result = parse_review_output(output)

    assert result.json_present is True
    assert {i.severity for i in result.issues} == {"CRITICAL", "MINOR"}
    # Two rejected (indices 2 and 4 in 1-based ordering).
    assert {index for index, _ in result.rejected} == {2, 4}


def test_parse_review_output_rejects_wrong_focus_category() -> None:
    output = json.dumps({"issues": [issue(category="bugs"), issue(category="protocol")]})

    result = parse_review_output(output, allowed_category="bugs")

    assert [i.category for i in result.issues] == ["bugs"]
    assert {index for index, _ in result.rejected} == {2}


def test_parse_review_output_with_verilog_bitselect_in_spec_clause() -> None:
    """Regression: spec_clause quoting Verilog bit-selects must not confuse wrapper extraction.

    The old regex-based extractor terminated early on the first ``]}`` substring it
    found, which appears inside spec_clause values like ``{{20{instr[31]}}, instr[31:20]}``
    -- causing c-static-branch-predict-0001 to block on a "no JSON wrapper" false
    negative even though the agent emitted a perfectly valid ``{"issues": [...]}``.
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
        '      "kind": "spec_ambiguity",\n'
        '      "disposition": "current",\n'
        '      "ticket_clause": "JALR target uses rs1",\n'
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
        '      "kind": "spec_ambiguity",\n'
        '      "disposition": "current",\n'
        '      "ticket_clause": "predict_branch_pc_o is the target.",\n'
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
    assert [i.line for i in result.issues] == [67, 58]
    assert all(i.severity == "MAJOR" for i in result.issues)


def test_issue_to_dict_omits_empty_fix() -> None:
    row = ReviewIssue(
        severity="MINOR",
        confidence="LOW",
        category="quality",
        file="rtl/mod_b.sv",
        line=10,
        summary="Style",
    )

    assert "fix_suggestion" not in row.to_dict()


def test_finding_ids_distinguish_defects_on_the_same_ticket_clause(agent: ScriptedAgent) -> None:
    agent.answer(
        {
            "issues": [
                finding(summary="Reset polarity is inverted"),
                finding(summary="Enable polarity is inverted"),
            ]
        }
    )

    outcome, _ = run_review(BUGS)

    ids = {row["finding_id"] for row in outcome.detail["issue_list"]}
    assert len(outcome.detail["issue_list"]) == 2
    assert len(ids) == 2


class TestReportFindingsMapper:
    """Unit tests for the ReportFindings -> MirrorFinding mapping."""

    def test_keeps_sent_severity_and_ignores_verdict(self) -> None:
        mirror = report_findings_to_mirror(
            [
                {"file": "./rtl/a.sv", "line": 7, "summary": "x", "severity": "critical"},
                {"file": "rtl/a.sv", "line": 8, "summary": "y", "verdict": "CONFIRMED"},
            ]
        )
        assert mirror == [
            MirrorFinding(file="rtl/a.sv", line=7, severity=SEVERITY_CRITICAL),
            MirrorFinding(file="rtl/a.sv", line=8, severity=None),
        ]

    def test_bad_line_defaults_to_zero(self) -> None:
        mirror = report_findings_to_mirror([{"file": "rtl/a.sv", "line": "nope", "summary": "x"}])
        assert mirror[0].line == 0


# ---------------------------------------------------------------------------
# Channel verdicts: text JSON versus the native ReportFindings mirror
# ---------------------------------------------------------------------------


def _mirror(line: int, summary: str, **extra: Any) -> dict[str, Any]:
    """One ReportFindings entry on ``rtl/mod_a.sv``."""
    return {"file": "rtl/mod_a.sv", "line": line, "summary": summary, **extra}


def _canonical(*rows: dict[str, Any]) -> str:
    return json.dumps({"issues": list(rows)})


class Verdict(NamedTuple):
    """One agent answer and the run outcome it must produce."""

    output: str  # the agent's final text
    reports: list[list[dict[str, Any]]] | None  # one list per ReportFindings call; None = none
    exit_code: int
    detail: dict[str, Any] = {}  # noqa: RUF012 - read-only expectation
    report_any: tuple[str, ...] = ()  # the report text carries at least one of these


_CRITICAL_TEXT = _canonical(
    finding(severity="CRITICAL", summary="FMAX polarity inverted — returns the minimum")
)
_LATCH = [_mirror(3, "latch", verdict="CONFIRMED")]
_STRING_LINE = {
    "severity": "CRITICAL",
    "confidence": "HIGH",
    "category": "bugs",
    "file": "rtl/mod_a.sv",
    "line": "42",  # string -> schema-rejected
    "summary": "polarity inverted",
}
_NOPE = {"severity": "nope", "category": "bugs", "file": "a.sv"}
_MINOR_ADVISORY = finding(
    severity="MINOR",
    confidence="LOW",
    disposition="advisory",
    ticket_clause="Keep names clear.",
    line=5,
    summary="cosmetic",
)

_VERDICTS: dict[str, Verdict] = {
    # --- native ReportFindings capture ---
    "confirmed_finding_needs_canonical_metadata": Verdict(
        "", [[_mirror(42, "latch inferred", verdict="CONFIRMED")]], 2
    ),
    # SETUP-F-33: zero findings in BOTH channels is a clean pass; an empty
    # capability call on its own is no evidence of anything.
    "empty_report_is_clean_pass": Verdict(json.dumps(CLEAN), [[]], 0, {"issues": 0}),
    "plausible_finding_needs_canonical_metadata": Verdict(
        "", [[_mirror(3, "style nit", verdict="PLAUSIBLE")]], 2
    ),
    # --- SETUP-F-33: an empty ReportFindings call must never mask a text CRITICAL ---
    "empty_endpoint_call_does_not_mask_text_critical": Verdict(
        "The diff introduces a clear correctness bug.\n" + _CRITICAL_TEXT,
        [[]],
        0,
        {"issues": 1, "CRITICAL": 1},
    ),
    "empty_endpoint_call_and_prose_only_is_endpoint_error": Verdict(
        "I looked at the files. Nothing jumped out at me.", [[]], 2
    ),
    # ReportFindings cannot replace the canonical Ticket-bound JSON.
    "endpoint_findings_without_metadata_mirror_are_rejected": Verdict(
        "see the agent-capability call", [_LATCH], 2
    ),
    # PLAUSIBLE via endpoint + CRITICAL in text: the blocking channel wins whole, no merge.
    "more_severe_text_channel_wins_over_advisory_endpoint_channel": Verdict(
        _CRITICAL_TEXT,
        [[_mirror(42, "nit", verdict="PLAUSIBLE")]],
        0,
        {"issues": 1, "CRITICAL": 1},
    ),
    "endpoint_finding_missing_from_text_is_contract_error": Verdict(
        json.dumps(CLEAN), [_LATCH], 2
    ),
    # Every reported issue failing the schema is a Specialist error, not "0 issues".
    "all_issues_rejected_is_not_a_clean_pass": Verdict(
        _canonical({**_STRING_LINE, "category": "wrong_focus"}), None, 2
    ),
    # The residual F-33 false PASS: a MINOR nit burying a rejected CRITICAL. The text
    # channel's verdict is unknown, so the nit must not outvote it into a clean pass.
    "advisory_endpoint_call_cannot_outvote_a_rejected_text_channel": Verdict(
        _canonical({**_STRING_LINE, "summary": "FMAX polarity inverted"}),
        [[_mirror(3, "naming nit", verdict="PLAUSIBLE")]],
        2,
    ),
    "rejected_text_channel_cannot_be_replaced_by_endpoint_findings": Verdict(
        _canonical(_STRING_LINE), [_LATCH], 2
    ),
    "rejected_text_channel_with_empty_endpoint_call_is_a_endpoint_error": Verdict(
        _canonical(_NOPE), [[]], 2
    ),
    # One survivor means the text channel is usable: only a WARN, no error.
    "partial_rejection_still_reviews_normally": Verdict(
        _canonical(_NOPE, _MINOR_ADVISORY),
        [[]],
        0,
        {"issues": 0, "observation_count": 1, "gate_passed": True},
    ),
    # --- Upstream rejection: prose is never a clean pass ---
    "no_json_wrapper_returns_endpoint_error": Verdict(
        "I reviewed the files and everything looks fine.",
        None,
        2,
        report_any=("no parseable", "invocation failed"),
    ),
    "malformed_entries_dropped_but_review_still_completes": Verdict(
        _canonical(
            finding(severity="CRITICAL"),
            {**_STRING_LINE, "severity": "BOGUS", "line": 1, "category": "x"},
        ),
        None,
        0,
        {"issues": 1, "CRITICAL": 1},
    ),
    # --- #1963: the mirror must match canonical findings, not out-count them ---
    # Replay of a recorded run: the same 3 findings reported twice, no severity sent; the
    # canonical JSON rates the first CONFIRMED finding MINOR and the PLAUSIBLE one MAJOR.
    "repeated_report_agrees_with_canonical_listed_once": Verdict(
        _canonical(
            finding(severity="MAJOR", line=316, summary="no timeout"),
            finding(severity="MAJOR", line=1156, summary="no edge vectors"),
            finding(severity="MINOR", line=384, summary="no stall injection"),
        ),
        [
            [
                _mirror(316, "no timeout", verdict="CONFIRMED"),
                _mirror(1156, "no edges", verdict="PLAUSIBLE"),
                _mirror(384, "no stalls", verdict="CONFIRMED"),
            ]
        ]
        * 2,
        0,
    ),
    "confirmed_finding_rated_minor_agrees": Verdict(
        _canonical(finding(severity="MINOR")),
        [[_mirror(42, "x", verdict="CONFIRMED")]],
        0,
    ),
    "split_report_that_resends_a_finding_agrees": Verdict(
        _canonical(
            finding(severity="MAJOR", line=5, summary="a"),
            finding(severity="MINOR", line=9, summary="b"),
        ),
        [
            [_mirror(5, "a", severity="MAJOR")],
            [_mirror(5, "a", severity="MAJOR"), _mirror(9, "b", severity="MINOR")],
        ],
        0,
    ),
    "mirror_severity_above_canonical_still_fails": Verdict(
        _canonical(finding(severity="MAJOR")),
        [[_mirror(42, "x", severity="CRITICAL")]],
        2,
    ),
    # A severe canonical issue elsewhere in the file must not hide an omitted finding.
    "mirror_finding_missing_beside_a_more_severe_canonical_issue_fails": Verdict(
        _canonical(finding(severity="CRITICAL")),
        [[_mirror(7, "x", verdict="PLAUSIBLE")]],
        2,
    ),
}


@pytest.mark.parametrize("case", _VERDICTS.values(), ids=list(_VERDICTS))
def test_channel_verdict(
    case: Verdict, agent: ScriptedAgent, capsys: pytest.CaptureFixture[str]
) -> None:
    if case.reports is None:
        agent.answer(case.output)
    else:
        agent.answer(
            agent.reported(*({"findings": rows} for rows in case.reports), output=case.output)
        )

    outcome, _ = run_review(BUGS)

    assert outcome.exit_code == case.exit_code
    for key, expected in case.detail.items():
        assert outcome.detail[key] == expected, key
    if case.report_any:
        assert any(text in outcome.report_text.lower() for text in case.report_any)
    if case.exit_code == 2:
        # A Specialist error is never a gate pass, and the output must not contradict itself.
        assert outcome.detail.get("gate_passed") is not True
        shown = (outcome.report_text + capsys.readouterr().out).lower()
        assert "gate_passed: true" not in shown


def test_tied_channels_preserve_canonical_ticket_metadata(agent: ScriptedAgent) -> None:
    canonical = finding(summary="latch")
    agent.answer(
        agent.reported(
            {"findings": [_mirror(42, "latch", verdict="CONFIRMED")]},
            output=_canonical(canonical),
        )
    )

    outcome, _ = run_review(BUGS)

    assert outcome.exit_code == 0
    row = outcome.detail["issue_list"][0]
    assert row["kind"] == "code_defect"
    assert row["disposition"] == "current"
    assert row["ticket_clause"] == canonical["ticket_clause"]


def test_output_instructions_teach_the_endpoint_contract(agent: ScriptedAgent) -> None:
    """The prompt must name ReportFindings -- the contract mismatch behind F-33."""
    system, _ = prompts(agent, BUGS)

    assert "ReportFindings" in system
    assert "mirror" in system.lower()


def test_review_denies_mutating_tools(agent: ScriptedAgent) -> None:
    """SETUP-F-35: allowed_agent_capabilities is advisory under bypassPermissions."""
    run_review(BUGS)

    params = agent.calls[0]
    assert {"Bash", "Write", "Edit"} <= set(params.disallowed_agent_capabilities or [])
    # The read-only allowlist is unchanged.
    assert params.allowed_agent_capabilities == ["Read", "Grep", "Glob", "ReportFindings"]


@pytest.mark.parametrize("with_report_dir", [False, True], ids=["no_report_dir", "report_dir"])
def test_verdict_text_does_not_carry_its_own_no_report_dir_notice(
    agent: ScriptedAgent, tmp_path: Path, with_report_dir: bool
) -> None:
    """SETUP-F-39: ``Endpoint._post_run`` owns the "nothing was persisted" notice.

    The reviewer used to build its own copy into ``report_text``; keeping both printed
    the same warning twice per run, which is the noise F-28 was about.
    """
    extra = ["--report-dir", str(tmp_path / "rep")] if with_report_dir else []

    outcome, _ = run_review([*BUGS, *extra])

    assert "no --report-dir" not in outcome.report_text


class TestSeverityCounting:
    def test_unknown_severity_ignored(self) -> None:
        rows = [ReviewIssue.from_dict({"severity": "UNKNOWN", "file": "f.sv"})]

        assert sum(count_by_severity(rows).values()) == 0


# ---------------------------------------------------------------------------
# Scope and argument validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("files", "category", "fragments"),
    [
        (["rtl/mod_a.sv"], "rtl", []),
        # Without a ``.core``, ``fw/`` is no RTL directory (#4052).
        (["fw/main.c"], "rtl", ["doesn't match RTL"]),
        (["tb/mod_a_tb.sv"], "rtl", ["doesn't match RTL"]),
        (["tb/mod_a_tb.sv"], "tb", []),
        (["rtl/mod_a.sv"], "tb", ["doesn't match TB"]),
        (["rtl/mod_a.sv", "tb/mod_a_tb.sv"], "rtl", ["tb/mod_a_tb.sv"]),
        ([], "rtl", []),
    ],
)
def test_scope_validation(files: list[str], category: str, fragments: list[str]) -> None:
    errors = validate_scope_category(files, category)

    assert len(errors) == len(fragments)
    for error, fragment in zip(errors, fragments, strict=True):
        assert fragment in error


def test_flat_repo_root_file_is_valid_exact_scope(tmp_path: Path) -> None:
    (tmp_path / "picorv32.v").write_text("module picorv32; endmodule\n", encoding="utf-8")
    (tmp_path / "testbench.v").write_text("module testbench; endmodule\n", encoding="utf-8")
    (tmp_path / "picorv32.core").write_text(
        "CAPI=2:\n"
        "name: ::picorv32\n"
        "filesets:\n"
        "  rtl: {files: [picorv32.v]}\n"
        "  tb: {files: [testbench.v], tags: [tb]}\n"
        "targets:\n"
        "  sim: {filesets: [rtl, tb], toplevel: testbench}\n",
        encoding="utf-8",
    )

    assert validate_scope_category(["picorv32.v"], "rtl", tmp_path) == []
    assert validate_scope_category(["./picorv32.v"], "rtl", tmp_path) == []
    errors = validate_scope_category(["picorv32.v.bak"], "rtl", tmp_path)
    assert errors == []  # explicit unregistered scope retains its narrow grant


class _Arguments(NamedTuple):
    argv: list[str]
    refusal: str | None  # None: accepted


@pytest.mark.parametrize(
    "case",
    [
        _Arguments(BUGS, None),
        _Arguments(_argv("rtl", "spec", None, "--spec-text", "o_valid pulses once."), None),
        _Arguments(_argv("tb", "quality"), None),
        # Every RTL focus together (reinstated spec focus, ADR 0038).
        _Arguments(
            _argv(
                "rtl",
                ",".join(sorted(RTL_FOCUS_CATEGORIES)),
                None,
                "--spec-text",
                "o_valid pulses once.",
            ),
            None,
        ),
        _Arguments(["--scope", "rtl/mod_a.sv", "--category", "rtl"], "--focus"),
        _Arguments(_argv("rtl", "bogus"), "Invalid RTL focus"),
        # The spec focus is RTL-only (ADR 0038).
        _Arguments(_argv("tb", "spec"), "Invalid TB focus"),
        _Arguments(_argv("tb", "bugs"), "Invalid TB focus"),
        _Arguments(_argv("rtl", "bugs", "tb/mod_a_tb.sv"), "doesn't match RTL"),
        _Arguments(_argv("tb", "quality", "rtl/mod_a.sv"), "doesn't match TB"),
    ],
    ids=[
        "rtl_bugs",
        "rtl_spec",
        "tb_quality",
        "every_rtl_focus",
        "focus_required",
        "invalid_rtl_focus",
        "tb_spec_rejected",
        "tb_bugs_rejected",
        "rtl_scope_with_tb_file",
        "tb_scope_with_rtl_file",
    ],
)
def test_argument_validation(case: _Arguments, agent: ScriptedAgent) -> None:
    outcome, _ = run_review([*case.argv, "--dry-run"])

    if case.refusal is None:
        assert outcome.exit_code == 0, outcome.report_text
    else:
        assert outcome.exit_code == 2
        assert case.refusal in outcome.report_text
    assert agent.calls == []


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------

# (category, focus, scope, guides in declaration order): the guides each focus loads.
_GUIDE_CASES = [
    ("rtl", "bugs", "rtl/a.sv", ["bugs-checklist", "bugs-hunt"]),
    ("rtl", "protocol", "rtl/a.sv", ["protocol-cdc-checklist", "protocol-cdc-hunt"]),
    ("rtl", "security", "rtl/a.sv", ["security-checklist", "security-hunt"]),
    ("rtl", "optimization", "rtl/a.sv", ["optimization-checklist", "optimization-hunt"]),
    ("rtl", "code_style", "rtl/a.sv", ["code_style-checklist"]),
    ("rtl", "spec", "rtl/a.sv", ["spec-hunt"]),
    ("tb", "quality", "tb/t.sv", ["hdl-checklist", "tb-hunt"]),
    ("tb", "quality", "tb/test.py", ["cocotb-checklist", "tb-hunt"]),
    (
        "tb",
        "quality",
        "tb/main.cpp,tb/test.py,tb/tb.sv",
        ["c-cpp-checklist", "hdl-checklist", "cocotb-checklist", "tb-hunt"],
    ),
]


@pytest.mark.parametrize(("category", "focus", "scope", "names"), _GUIDE_CASES)
def test_each_focus_loads_its_declared_guides(
    agent: ScriptedAgent,
    ticket_md: Path,
    category: str,
    focus: str,
    scope: str,
    names: list[str],
) -> None:
    system, _ = prompts(agent, _argv(category, focus, scope, "--spec", str(ticket_md)))

    headings = re.findall(r"^## Review guide — .*$", system, flags=re.MULTILINE)
    # Guide calls run concurrently, so compare as sets of headings (one call per guide).
    assert sorted(headings) == sorted(f"## Review guide — {focus} ({name})" for name in names)
    # A mixed-language TB scope folds its checklists into one call (``a+b``).
    assert sorted("+".join(guide_of(call) for call in agent.calls).split("+")) == sorted(names)
    if focus == "spec":
        assert "## Trace targets" in system
        assert "## Checklist" not in system


@pytest.mark.parametrize(("category", "focus", "scope", "names"), _GUIDE_CASES)
def test_dry_run_lists_guides_with_kind(
    ticket_md: Path, category: str, focus: str, scope: str, names: list[str]
) -> None:
    expected = [
        {"kind": "hunt" if name.endswith("-hunt") else "checklist", "name": name} for name in names
    ]

    argv = _argv(category, focus, scope, "--spec", str(ticket_md), "--dry-run")

    outcome, _ = run_review(argv)

    assert outcome.detail["guides"] == expected
    assert f"guides={','.join(names)}" in outcome.report_text


def test_security_hunt_prompt_names_scenario_categories_and_scope_boundaries(
    agent: ScriptedAgent,
) -> None:
    """A guide edit or packaging change must not drop what the hunt call receives."""
    run_review(_argv("rtl", "security"))

    system = system_of(agent, "security-hunt")
    assert "(security-checklist)" not in system
    boundaries = system.split("## Scope Boundaries", 1)[1].split("\n## ", 1)[0]
    assert "`bugs`" in boundaries
    assert "`protocol`" in boundaries
    assert '{"issues": []}' in boundaries
    for category in (
        "Attacker-controlled inputs",
        "Debug, test, and scan access",
        "Reset and power",
        "Error, alert, and escalation paths",
        "Fault injection",
        "Secret flow",
    ):
        assert f"**{category}**" in system, category


def test_optimization_hunt_guide_names_scenario_categories_and_scope_boundaries(
    agent: ScriptedAgent,
) -> None:
    run_review(_argv("rtl", "optimization"))

    system = system_of(agent, "optimization-hunt")
    assert "## Scope Boundaries" in system
    for category in (
        "Redundant storage",
        "Redundant terms",
        "Register reuse",
        "Equivalent structure",
        "Oversized",
        "Critical-path placement",
        "Behavior-neutral dead logic",
    ):
        assert f"**{category}**" in system
    assert "MAJOR** -- A likely critical-path or Fmax improvement" in system
    assert "MINOR** -- A small saving" in system
    assert "must not add material delay to a critical path" in system
    assert "reserved, debug, formal, and coverage hooks" in system
    assert "Track each wide register's lifetime" in system
    assert "## Patterns" not in system
    assert "share the same storage" not in system_of(agent, "optimization-checklist")


def test_rtl_optimization_prompt_checks_for_runtime_constant_work(agent: ScriptedAgent) -> None:
    system, _ = prompts(agent, _argv("rtl", "optimization"))

    assert "Runtime hardware driven only by elaboration constants" in system
    assert "registers always driven by constants or a MUX with constant select" in system
    assert "constant function evaluated into a `localparam`" in system
    assert "Specialize supported configurations at elaboration" in system
    assert "reject unsupported geometries at elaboration" in system


def test_rtl_optimization_prompt_prefers_static_hardware_topology(agent: ScriptedAgent) -> None:
    system, _ = prompts(agent, _argv("rtl", "optimization"))

    assert "Prefer static wiring over runtime steering" in system
    assert "variable part-selects, wide dynamic shifts" in system
    assert "Priority encoders" in system
    assert "is it REALLY necessary" in system
    assert "Size operands and intermediate expressions deliberately" in system


def test_rtl_bugs_prompt_omits_checks_lint_reports(agent: ScriptedAgent) -> None:
    system, _ = prompts(agent, BUGS)

    system = squash(system)
    for removed in (
        "**Width mismatches**",
        "**Undriven / partially driven signals**",
        "Undriven or partially driven signals",
        "an output is wrong or undriven",
        "**Inferred latches**",
        "**Multi-driven nets**",
        "multiple procedural drivers",
        "Missing labels on generate blocks",
        "casex",
        "Unbalanced `ifdef`",
        "include guard",
        "**Long combinational chains**",
    ):
        assert removed not in system, removed


def test_rtl_bugs_prompt_keeps_checks_lint_cannot_make(agent: ScriptedAgent) -> None:
    run_review(BUGS)

    checklist = squash(system_of(agent, "bugs-checklist"))
    assert "**Combinational loops**" in checklist
    assert "zero-width signals or empty loop ranges at legal parameter values" in checklist
    assert "reason across every built configuration" in checklist
    assert "Do not review an `ifdef` configuration that no Target builds" in checklist
    hunt = squash(system_of(agent, "bugs-hunt"))
    assert "Do not trace an `ifdef` configuration that no Target builds" in hunt


def test_rtl_optimization_prompt_checks_functionally_dead_rtl(agent: ScriptedAgent) -> None:
    system, _ = prompts(agent, _argv("rtl", "optimization"))

    system = squash(system)
    for present in (
        "Functionally dead logic that lint cannot see",
        "FSM states no legal input sequence reaches",
        "constant in practice",
        "Require proof across all legal configurations",
        "do not claim silicon savings",
        "interface items without complete context",
        "Long combinational chains",
        "limits Fmax",
    ):
        assert present in system, present
    for removed in (
        "Unused and dead RTL",
        "written but never read",
        "unused declarations",
        "lint cleanliness",
    ):
        assert removed not in system, removed


def test_rtl_style_guide_has_no_bit_range_order_rule(agent: ScriptedAgent) -> None:
    system, _ = prompts(agent, _argv("rtl", "code_style"))

    assert "ascending bit ranges" not in system.lower()


def test_rtl_user_prompt_includes_scope(agent: ScriptedAgent) -> None:
    system, prompt = prompts(agent, BUGS)

    assert "mod_a.sv" in prompt
    assert "bugs" in system
    assert "Review methodology" in system


def test_rtl_protocol_system_prompt_uses_protocol_cdc_guide(agent: ScriptedAgent) -> None:
    system, _ = prompts(agent, _argv("rtl", "protocol"))

    assert "Protocol & Clock Domain Crossings" in system
    assert '"category": "protocol"' in system
    # The output instructions pin the category to the focus.
    assert '- category:       "protocol"' in system
    assert "category:       string" not in system


@pytest.mark.parametrize(("category", "focus"), sorted(REVIEW_GUIDES))
def test_reviewer_prompts_do_not_request_info_severity(
    agent: ScriptedAgent, ticket_md: Path, category: str, focus: str
) -> None:
    system, _ = prompts(agent, _argv(category, focus, None, "--spec", str(ticket_md)))

    assert "INFO" not in system


def test_tb_system_prompt_includes_tb_guides(agent: ScriptedAgent) -> None:
    system, prompt = prompts(agent, _argv("tb", "quality"))

    assert "Review methodology" in system
    assert "Testbench style guide" in system
    assert "unit_tb_style_guide.md" not in system
    assert "read its RTL source" not in system
    assert "FSM structure" not in system
    assert "datapath" not in system.lower()
    assert "mod_a_tb.sv" in prompt
    assert "code_review/rtl/" not in prompt


@pytest.mark.parametrize(
    ("category", "scope", "label", "focus"),
    [
        ("rtl", "rtl/mod_a.sv", "RTL", "code_style"),
        ("tb", "tb/mod_a_tb.sv", "Testbench", "quality"),
    ],
)
def test_project_style_overlay_is_inlined(
    agent: ScriptedAgent, tmp_path: Path, category: str, scope: str, label: str, focus: str
) -> None:
    """A project-authored overlay is appended after the packaged guide."""
    overlay = tmp_path / ".booley" / f"{category}_style_guide.md"
    overlay.parent.mkdir(parents=True, exist_ok=True)
    overlay.write_text("Signal names MUST carry a `u_` prefix.\n", encoding="utf-8")

    system, _ = prompts(agent, _argv(category, focus, scope, "--work-dir", str(tmp_path)))

    assert f"## {label} style guide\n" in system
    assert f"## {label} style guide — project overlay" in system
    # The overlay text is passed through byte-for-byte.
    assert overlay.read_text(encoding="utf-8") in system
    # Overlay must follow the packaged guide so precedence reads correctly.
    assert system.index(f"## {label} style guide\n") < system.index(
        f"## {label} style guide — project overlay"
    )


def test_style_overlay_reads_relocated_booley_dir(agent: ScriptedAgent, tmp_path: Path) -> None:
    """A relative ``[project].dir`` moves the overlay with the Booley Directory."""
    (tmp_path / "booley.toml").write_text('[project]\ndir = "state"\n', encoding="utf-8")
    relocated = tmp_path / "state" / "rtl_style_guide.md"
    relocated.parent.mkdir()
    relocated.write_text("Relocated overlay rule.\n", encoding="utf-8")
    stale = tmp_path / ".booley" / "rtl_style_guide.md"
    stale.parent.mkdir()
    stale.write_text("Default-location rule.\n", encoding="utf-8")

    system, _ = prompts(agent, _argv("rtl", "code_style", None, "--work-dir", str(tmp_path)))

    assert "Relocated overlay rule." in system
    assert "Default-location rule." not in system


@pytest.mark.parametrize(
    ("category", "focus", "overlay_authored"),
    [("rtl", "code_style", False), ("tb", "quality", False), ("rtl", "bugs", True)],
    ids=["rtl_absent", "tb_absent", "non_quality_focus_skips_overlay"],
)
def test_style_overlay_is_omitted(
    agent: ScriptedAgent, tmp_path: Path, category: str, focus: str, overlay_authored: bool
) -> None:
    """No overlay authored is the normal case; overlays ride along with style guides only."""
    if overlay_authored:
        overlay = tmp_path / ".booley" / f"{category}_style_guide.md"
        overlay.parent.mkdir()
        overlay.write_text("Signal names MUST carry a `u_` prefix.\n", encoding="utf-8")

    system, _ = prompts(agent, _argv(category, focus, None, "--work-dir", str(tmp_path)))

    assert "style guide — project overlay" not in system
    assert "u_` prefix" not in system
    if not overlay_authored:
        assert "style guide" in system.lower()


@pytest.mark.parametrize(
    ("category", "focus", "steer"),
    [
        ("rtl", "bugs", "Pay attention to clock domain crossings"),
        ("tb", "quality", "Check for false-pass risks"),
    ],
)
def test_steer_injection(agent: ScriptedAgent, category: str, focus: str, steer: str) -> None:
    _, prompt = prompts(agent, _argv(category, focus, None, "--steer", steer))

    assert steer in prompt


def test_output_format_in_system_prompt(agent: ScriptedAgent) -> None:
    system, _ = prompts(agent, BUGS)

    assert "CRITICAL" in system
    assert "MAJOR" in system
    assert "MINOR" in system
    assert "STRICT SCHEMA" in system
    assert "``issues`` array" in system
    # Low-confidence gate must survive prompt trims (wording may vary).
    assert "LOW confidence" in system
    assert '"issues": []' in system


class TestSchemaExampleMatchesCategory:
    """A TB reviewer told not to read RTL was shown an rtl/ path to report."""

    def test_tb_example_is_a_tb_path(self, agent: ScriptedAgent) -> None:
        system, _ = prompts(agent, _argv("tb", "quality"))

        assert "verif/mod_a_tb.sv" in system
        assert "rtl/mod_a.sv" not in system

    @pytest.mark.parametrize("focus", ["spec", "bugs"])
    def test_rtl_example_is_an_rtl_path(
        self, agent: ScriptedAgent, ticket_md: Path, focus: str
    ) -> None:
        system, _ = prompts(agent, _argv("rtl", focus, None, "--spec", str(ticket_md)))

        assert "rtl/mod_a.sv" in system


# ---------------------------------------------------------------------------
# RTL spec focus (reinstated -- ADR 0038)
# ---------------------------------------------------------------------------


class TestRtlSpecFocus:
    def test_prompts_carry_spec_guide_and_inline_the_spec_verbatim(
        self, agent: ScriptedAgent, ticket_md: Path
    ) -> None:
        system, prompt = prompts(agent, _argv("rtl", "spec", None, "--spec", str(ticket_md)))

        assert "Spec Compliance" in system
        assert "## Specification" in prompt
        assert "o_valid pulses exactly one cycle" in prompt

    def test_standalone_spec_does_not_follow_ticket_frontmatter(
        self, agent: ScriptedAgent, ticket_md: Path, tmp_path: Path
    ) -> None:
        (tmp_path / "arch_spec.md").write_text("The real spec text.", encoding="utf-8")
        ticket_md.write_text(
            ticket_md.read_text(encoding="utf-8").replace(
                "type: feature\n", 'type: feature\nspec: "arch_spec.md"\n'
            ),
            encoding="utf-8",
        )

        _, prompt = prompts(
            agent,
            _argv("rtl", "spec", None, "--spec", str(ticket_md), "--work-dir", str(tmp_path)),
        )

        assert "The real spec text." not in prompt
        assert "o_valid pulses exactly one cycle" in prompt

    def test_run_errors_without_spec(
        self, agent: ScriptedAgent, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("BOOLEY_LOGS_DIR", raising=False)

        outcome, _ = run_review(_argv("rtl", "spec"))

        assert outcome.exit_code == 2
        assert "spec" in outcome.report_text.lower()
        assert agent.calls == []


# ---------------------------------------------------------------------------
# TB prompts (#2636)
# ---------------------------------------------------------------------------


def test_tb_quality_system_prompt_flags_iverilog_ternary_string_display(
    agent: ScriptedAgent,
) -> None:
    """TB guide warns about Icarus decimal-printing ternary string sentinels."""
    system, _ = prompts(agent, _argv("tb", "quality"))

    assert "Ternary string expression in display/sentinel calls" in system
    assert "$display((total_failed == 0) ?" in system
    assert "print a large decimal" in system


def test_simulator_specific_rules_are_marked(agent: ScriptedAgent) -> None:
    """Single-simulator rules carry an ``[<Simulator> only]`` mark (#2636)."""
    system, _ = prompts(agent, _argv("tb", "quality"))

    assert "[Icarus only]" in system
    assert "Icarus exposes" not in system
    assert 'flow_options.tool = "icarus"' not in system
    assert "**## Simulation Context**" in system

    # The Vivado xsim bug rule and check 11's clause are marked.
    urandom = system.index("Never use `$urandom_range()`")
    assert "[Vivado xsim only]" in system[urandom - 200 : urandom + 200]
    # The mark covers that clause alone, not the rest of check 11.
    assert (
        "`[Vivado xsim only]` for this clause alone: missing rejection sampling for large ranges"
        in system
    )

    # `$stop` is wrong on every simulator: its reason names no single one.
    stop = system.index("End simulation with `$finish`, not `$stop`")
    assert "Icarus" not in system[stop : stop + 400]


_SIM_CORE_TEMPLATE = """\
CAPI=2:
name: ::demo_core:0

filesets:
  rtl:
    files:
      - rtl/counter.sv: {{file_type: systemVerilogSource}}
  tb:
    files:
      - tb/tb_counter.sv: {{file_type: systemVerilogSource}}
    tags: [tb]
  lint_only_tb:
    files:
      - tb/tb_lint_only.sv: {{file_type: systemVerilogSource}}
    tags: [tb]

targets:
  default:
    filesets: [rtl]
  sim:
{sim_selection}
    filesets: [rtl, tb]
    toplevel: tb_counter
  lint_all:
    flow: lint
    flow_options:
      tool: verilator
    filesets: [rtl, tb, lint_only_tb]
    toplevel: counter
"""

_VERILATOR_SELECTION = "    flow: sim\n    flow_options:\n      tool: verilator"
_ICARUS_SELECTION = "    default_tool: icarus"


def _sim_project(root: Path, sim_selection: str = _VERILATOR_SELECTION) -> Path:
    """Write a Project whose ``sim`` Target uses the given simulator selection."""
    from tests.fusesoc_test_support import write_core

    write_core(root, _SIM_CORE_TEMPLATE.format(sim_selection=sim_selection))
    return root


def _simulation_context(prompt: str) -> str:
    """Return the ``## Simulation Context`` section of *prompt* (asserted present)."""
    assert "## Simulation Context" in prompt, prompt
    section = prompt.split("## Simulation Context", 1)[1]
    return section.split("\n## ", 1)[0]


class TestTbSimulationContext:
    """A TB review tells the reviewer which simulator runs each scoped file (#2636)."""

    def test_verilator_only_project_names_verilator(
        self, agent: ScriptedAgent, tmp_path: Path
    ) -> None:
        project = _sim_project(tmp_path)

        _, prompt = prompts(
            agent, _argv("tb", "quality", "tb/tb_counter.sv", "--work-dir", str(project))
        )

        section = _simulation_context(prompt)
        file_lines = [line for line in section.splitlines() if line.startswith("- tb/")]
        assert file_lines == ["- tb/tb_counter.sv: Verilator"]

    def test_file_in_no_simulation_target_is_unknown(
        self, agent: ScriptedAgent, tmp_path: Path
    ) -> None:
        project = _sim_project(tmp_path)

        _, prompt = prompts(
            agent, _argv("tb", "quality", "tb/tb_lint_only.sv", "--work-dir", str(project))
        )

        section = _simulation_context(prompt)
        assert "tb/tb_lint_only.sv: unknown (no simulation Target uses this file)" in section
        assert "advisory" in section

    def test_icarus_project_names_icarus(self, agent: ScriptedAgent, tmp_path: Path) -> None:
        project = _sim_project(tmp_path, _ICARUS_SELECTION)

        _, prompt = prompts(
            agent, _argv("tb", "quality", "tb/tb_counter.sv", "--work-dir", str(project))
        )

        lines = _simulation_context(prompt).splitlines()
        assert [line for line in lines if line.startswith("- tb/")] == [
            "- tb/tb_counter.sv: Icarus"
        ]

    def test_rtl_prompt_has_no_simulation_context(
        self, agent: ScriptedAgent, tmp_path: Path
    ) -> None:
        project = _sim_project(tmp_path)

        _, prompt = prompts(
            agent, _argv("rtl", "bugs", "rtl/counter.sv", "--work-dir", str(project))
        )

        assert "## Simulation Context" not in prompt


class TestTbReviewGetsTheSpec:
    """A TB reviewer graded on spec traceability needs the spec in hand.

    Checks like "every numeric contract the spec states must be asserted" were
    unanswerable: the spec was inlined for RTL spec-focus reviews only.
    """

    def test_tb_prompt_inlines_the_specification(
        self, agent: ScriptedAgent, review_logs: Path
    ) -> None:
        spec = review_logs / "spec.md"
        spec.write_text("The output latency is 3 cycles.", encoding="utf-8")

        _, prompt = prompts(agent, _argv("tb", "quality", None, "--spec", str(spec)))

        assert "## Specification" in prompt
        assert "latency is 3 cycles" in prompt

    def test_tb_prompt_survives_a_missing_spec(self, agent: ScriptedAgent) -> None:
        """Absence is not fatal on the TB side -- unlike spec focus, which guards."""
        _, prompt = prompts(agent, _argv("tb", "quality"))

        assert "## Specification" not in prompt
        assert "## Focus: quality" in prompt


# ---------------------------------------------------------------------------
# Summary line formatting
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("category", "focus", "count", "counts", "seconds", "present", "absent"),
    [
        (
            "rtl",
            "bugs",
            2,
            {SEVERITY_CRITICAL: 1, SEVERITY_MAJOR: 0, SEVERITY_MINOR: 1},
            45.3,
            ["[review]", "rtl / bugs", "2 issues", "1 CRITICAL", "1 MINOR", "45s"],
            [],
        ),
        (
            "tb",
            "",
            0,
            {SEVERITY_CRITICAL: 0, SEVERITY_MAJOR: 0, SEVERITY_MINOR: 0},
            10.0,
            ["[review]", "0 issues"],
            [],
        ),
        (
            "rtl",
            "ifdef",
            1,
            {SEVERITY_CRITICAL: 0, SEVERITY_MAJOR: 1, SEVERITY_MINOR: 0},
            28.0,
            ["1 issue", "1 MAJOR"],
            ["1 issues"],  # singular for count=1
        ),
    ],
    ids=["with_issues", "no_issues", "single_issue"],
)
def test_summary_line(
    category: str,
    focus: str,
    count: int,
    counts: dict[str, int],
    seconds: float,
    present: list[str],
    absent: list[str],
) -> None:
    line = format_summary_line(category, focus, count, counts, seconds)

    for text in present:
        assert text in line
    for text in absent:
        assert text not in line


# ---------------------------------------------------------------------------
# Full reviews (scripted agent)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("argv", [BUGS, _argv("tb", "quality")], ids=["rtl", "tb"])
def test_clean_review(agent: ScriptedAgent, argv: list[str]) -> None:
    """No issues => PASS, exit 0, _done met."""
    outcome, _ = run_review(argv)

    assert outcome.exit_code == 0
    assert "RESULT: REVIEWED — NO FINDINGS" in outcome.report_text


def test_clean_interactive_review_tracks_explicit_spec(
    agent: ScriptedAgent, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    logs = tmp_path / "interactive-logs"
    logs.mkdir()
    spec = tmp_path / "interactive-spec.md"
    spec.write_text("Review this change.\n", encoding="utf-8")
    monkeypatch.setenv("BOOLEY_LOGS_DIR", str(logs))

    outcome, _ = run_review([*BUGS, "--work-dir", str(tmp_path), "--spec", str(spec)])

    assert outcome.exit_code == 0
    assert outcome.detail["contract"]["spec_source"] == str(spec.resolve())
    assert "RESULT: REVIEWED — NO FINDINGS" in outcome.report_text


def test_clean_interactive_review_needs_no_ticket_snapshot(
    agent: ScriptedAgent, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    logs = tmp_path / "interactive-logs"
    logs.mkdir()
    monkeypatch.setenv("BOOLEY_LOGS_DIR", str(logs))

    outcome, _ = run_review([*BUGS, "--work-dir", str(tmp_path)])

    system = agent.calls[0].system_prompt or ""
    assert "Anchor each finding in a supplied specification, accepted decision" in system
    assert "Goal" not in system
    assert "steering, or concrete code behavior" in system
    assert "Cite a relevant Ticket or accepted-decision clause" not in system
    assert outcome.exit_code == 0
    assert outcome.detail["contract"]["ticket_source"] == ""


def test_corrective_issues_complete_done(agent: ScriptedAgent) -> None:
    agent.answer(
        {"issues": [finding(severity="CRITICAL"), finding(severity="MINOR", summary="Separate")]}
    )

    outcome, _ = run_review(BUGS)

    assert outcome.exit_code == 0
    assert "RESULT: REVIEWED WITH FINDINGS" in outcome.report_text
    assert "critical" in outcome.report_text


class _Rerun(NamedTuple):
    second_issues: list[dict[str, Any]]
    steer: str | None
    expected_text: str


@pytest.mark.parametrize(
    "case",
    [
        # Corrective done findings remain visible and replay on a plain rerun.
        _Rerun([finding(severity="CRITICAL")], None, "RESULT: REVIEWED WITH FINDINGS"),
        # Steering cannot turn terminal _done into a cleanliness workflow.
        _Rerun([], "attempt 1", "NO FINDINGS"),
        # Both exhaustion paths used to point the agent at "triage", an operator
        # workflow the agent cannot invoke.
        _Rerun([], "fixed finding", "NO FINDINGS"),
    ],
    ids=["replay", "steering_does_not_hide_open_correction", "completed_done_names_clean_mode"],
)
def test_second_review_after_a_corrective_done(agent: ScriptedAgent, case: _Rerun) -> None:
    agent.answer(
        [{"issues": [finding(severity="CRITICAL")]}] * 2 + [{"issues": case.second_issues}]
    )
    first, _ = run_review(BUGS)
    assert first.exit_code == 0

    extra = ["--steer", case.steer] if case.steer else []
    rerun, _ = run_review([*BUGS, *extra])

    assert rerun.exit_code == 0
    assert case.expected_text in rerun.report_text
    assert "triage" not in rerun.report_text.lower()
    assert "explicit human approval" not in rerun.report_text
    if not case.second_issues:
        assert rerun.detail["issues"] == 0
        assert rerun.detail["issue_list"] == []
    # Each bugs review makes two calls (checklist and hunt guides).
    assert len(agent.calls) == 4


def test_current_minor_findings_remain_corrective(agent: ScriptedAgent) -> None:
    agent.answer({"issues": [finding(severity="MINOR"), finding(severity="MINOR")]})

    outcome, _ = run_review(BUGS)

    assert outcome.exit_code == 0
    assert outcome.detail["gate_passed"] is True


def test_single_focus_security(agent: ScriptedAgent) -> None:
    """Single focus runs one agent call per guide kind, one run per focus."""
    outcome, _ = run_review(_argv("rtl", "security"))

    assert outcome.exit_code == 0
    assert len(agent.calls) == 2  # security declares a checklist and a hunt guide


def test_agent_failure_is_a_reviewer_error_with_persisted_traceback(
    agent: ScriptedAgent,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    agent.answer(RuntimeError("boom"))
    caplog.set_level(logging.DEBUG)

    outcome, _ = run_review(
        [
            *_argv("rtl", "code_style"),
            "--work-dir",
            str(tmp_path),
            "--report-dir",
            str(tmp_path / "reports"),
            "--transcript-dir",
            str(tmp_path / "transcripts"),
        ]
    )

    assert outcome.exit_code == 2
    assert outcome.report_text.startswith("reviewer failed: RuntimeError: boom")
    assert "Diagnostic:" in outcome.report_text
    assert "Traceback" not in capsys.readouterr().err
    diagnostics = list((tmp_path / "transcripts").glob("reviewer.*.error.log"))
    assert len(diagnostics) == 1
    assert "RuntimeError: boom" in diagnostics[0].read_text(encoding="utf-8")
    assert str(diagnostics[0]) in outcome.report_text
    assert any(record.exc_info for record in caplog.records if record.levelname == "DEBUG")
    assert not any(record.exc_info for record in caplog.records if record.levelno >= 20)


# ---------------------------------------------------------------------------
# Spec arguments, done reports and category scope
# ---------------------------------------------------------------------------


def test_goal_spec_arg_ignores_ticket_document(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from booley.specialists.reviewer import resolve_spec_content

    spec = tmp_path / "spec.md"
    spec.write_text("Goal specification", encoding="utf-8")
    monkeypatch.setenv("BOOLEY_GOAL_FILE", str(tmp_path / "record.json"))
    monkeypatch.setenv("BOOLEY_LOGS_DIR", str(tmp_path / "absent"))

    text, source = resolve_spec_content("spec.md", tmp_path)

    assert text == "Goal specification"
    assert source.startswith("spec file:")
    assert resolve_spec_content(None, tmp_path) == (None, "")


@pytest.mark.parametrize("transport", [False, True])
def test_unbound_done_findings_do_not_claim_goal_approval(
    agent: ScriptedAgent, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, transport: bool
) -> None:
    from booley.flows.cli_selection import CliInvocation
    from booley.runtime import runtime_context
    from booley.runtime.booley_dir import reset_cache

    (tmp_path / "rtl").mkdir()
    (tmp_path / "rtl/mod_a.sv").write_text("module mod_a; endmodule\n", encoding="utf-8")
    directory = tmp_path / ".booley"
    directory.mkdir()
    (directory / "booley.toml").write_text("[project]\n", encoding="utf-8")
    monkeypatch.setenv("BOOLEY_DIR", str(directory))
    reset_cache()
    monkeypatch.setattr(runtime_context, "inside_session_runtime", lambda: True)
    agent.answer({"issues": [finding(severity="CRITICAL")]})
    argv = ["--work-dir", str(tmp_path), *BUGS]
    for _ in range(2):
        endpoint = ReviewerSpecialist()
        # Private seam: the transport origin is set only by the MCP server process, and
        # ``execute_cli`` (the only public entry that honours it) has no parameter for it.
        endpoint._cli_invocation = CliInvocation(transport=transport)
        result = endpoint.execute_cli(argv)
        assert result.exit_code == 0, result.outcome.report_text
        assert "explicit human approval" not in result.outcome.report_text
        assert "RESULT: REVIEWED WITH FINDINGS" in result.outcome.report_text
        report = json.loads((endpoint.args.report_dir / "reviewer.json").read_text("utf-8"))
        assert "criterion_key" not in report and "criterion_met" not in report
    assert not (directory / "reviewer-state").exists()


def test_standalone_done_findings_print_no_approval_notice(
    agent: ScriptedAgent, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A standalone run gets the same `done` report as an interactive run."""
    monkeypatch.delenv("BOOLEY_LOGS_DIR", raising=False)
    agent.answer({"issues": [finding(severity="CRITICAL")]})

    outcome, _ = run_review(BUGS)

    assert "RESULT: REVIEWED WITH FINDINGS" in outcome.report_text
    assert "explicit human approval" not in outcome.report_text


@pytest.mark.parametrize("category,scope", [("rtl", "shared/main.cpp"), ("tb", "shared/dut.sv")])
def test_registered_opposite_scope_is_refused(
    agent: ScriptedAgent, tmp_path: Path, category: str, scope: str
) -> None:
    from tests.test_target_catalog import _category_sim_core

    _category_sim_core(tmp_path)

    outcome, _ = run_review(
        [
            "--work-dir",
            str(tmp_path),
            *_argv(category, "bugs" if category == "rtl" else "quality", scope),
        ]
    )

    assert outcome.exit_code == 2
    assert "opposite-category" in outcome.report_text
    assert agent.calls == []


@pytest.mark.parametrize(
    ("category", "focus", "path"),
    [
        ("rtl", "bugs", "shared/new.sv"),
        *[("tb", "quality", f"shared/helper.{suffix}") for suffix in ("hh", "hxx", "HH", "HXX")],
    ],
)
def test_unregistered_same_directory_scope_is_own_grant_with_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, category: str, focus: str, path: str
) -> None:
    from tests.test_target_catalog import _category_sim_core

    _category_sim_core(tmp_path)
    (tmp_path / path).write_text("// source\n", encoding="utf-8")

    outcome, _ = run_review(
        ["--work-dir", str(tmp_path), *_argv(category, focus, path), "--dry-run"]
    )

    assert outcome.exit_code == 0, outcome.report_text
    assert path in caplog.text


@pytest.mark.parametrize("name", ["default.h", "rtl.c"])
def test_cpp_opposite_role_refuses_before_provider(
    agent: ScriptedAgent, tmp_path: Path, name: str
) -> None:
    (tmp_path / "design.core").write_text(
        "CAPI=2:\nname: ::roles:0\nfilesets:\n  rtl:\n    files: [default.h, rtl.c]\n"
        "  tb:\n    files: [tagged.hxx]\n    tags: [tb]\n",
        encoding="utf-8",
    )
    (tmp_path / name).write_text("// source\n", encoding="utf-8")

    outcome, _ = run_review(["--work-dir", str(tmp_path), *_argv("tb", "quality", name)])

    assert outcome.exit_code == 2
    assert name in outcome.report_text
    assert "tags:[tb]" in outcome.report_text
    assert agent.calls == []
    assert validate_scope_category(["tagged.hxx"], "tb", tmp_path) == []


@pytest.mark.parametrize(
    ("scope", "labels"),
    [
        ("tb/main.cpp", ["c-cpp-checklist"]),
        (
            "tb/main.cpp,tb/test.py,tb/tb.sv",
            ["c-cpp-checklist", "hdl-checklist", "cocotb-checklist"],
        ),
    ],
)
def test_cpp_guidance_preview_and_project_overlay(
    agent: ScriptedAgent, tmp_path: Path, scope: str, labels: list[str]
) -> None:
    overlay = tmp_path / ".booley" / "tb_style_guide.md"
    overlay.parent.mkdir()
    overlay.write_text("Project C lifecycle rule", encoding="utf-8")
    argv = _argv("tb", "quality", scope, "--work-dir", str(tmp_path))

    system, _ = prompts(agent, argv)
    preview, _ = run_review([*argv, "--dry-run"])

    assert "C/C++ testbench" in system
    assert "Project C lifecycle rule" in system
    assert ("Canonical style guide for SystemVerilog" in system) == ("tb/tb.sv" in scope)
    assert preview.detail["guides"] == [
        *({"kind": "checklist", "name": name} for name in labels),
        {"kind": "hunt", "name": "tb-hunt"},
    ]


def test_declared_review_guides_are_the_packaged_guides() -> None:
    """Every focus declares guides, and the declared files are exactly the packaged ones."""
    from booley.runtime.paths import refs_dir
    from booley.specialists.reviewer import TB_FOCUS_CATEGORIES

    focuses = {("rtl", focus) for focus in RTL_FOCUS_CATEGORIES} | {
        ("tb", focus) for focus in TB_FOCUS_CATEGORIES
    }
    assert set(REVIEW_GUIDES) == focuses
    root = refs_dir() / "code_review"
    declared = {guide.path for guides in REVIEW_GUIDES.values() for guide in guides}
    for path in declared:
        assert (root / path).read_text(encoding="utf-8").strip(), path
    packaged = {file.relative_to(root).as_posix() for file in root.rglob("*.md")}
    assert declared == packaged


def test_replay_message() -> None:
    from booley.specialists import reviewer as reviewer_mod

    source = Path(reviewer_mod.__file__).read_text(encoding="utf-8")
    assert "raise it in triage instead" not in source
    assert "resolve this through triage" not in source


def test_protocol_cdc_hunt_guide_names_scenarios_and_scope() -> None:
    """The hunt guide keeps the focus's scope boundaries and names every scenario category."""
    from booley.evidence.review_guides import review_guides_root

    text = (review_guides_root() / "rtl" / "protocol-cdc-hunt.md").read_text(encoding="utf-8")
    scope = text.split("## Scope Boundaries", 1)[1].split("\n## ", 1)[0]
    assert "Reviewer Specialist for Bugs" in scope
    assert "Reviewer Specialist for Optimization" in scope
    lowered = text.split("### 2. Enumerate scenarios", 1)[1].split("\n### 3.", 1)[0].lower()
    for category in (
        "start",
        "stall",
        "back-pressure",
        "abort",
        "back-to-back",
        "clock domain",
        "reset domain",
    ):
        assert category in lowered, category


# ---------------------------------------------------------------------------
# Defaults no run reaches: a Reviewer builds its prompts and guides itself
# ---------------------------------------------------------------------------


def _parsed(*argv: str) -> ReviewerSpecialist:
    endpoint = ReviewerSpecialist()
    endpoint.parse_args(list(argv))
    return endpoint


def test_system_prompt_defaults_to_every_guide_of_the_focus() -> None:
    # Private seam: a run always passes one call's guides, so only a direct call shows the default.
    endpoint = _parsed(*BUGS)

    system = endpoint._build_system_prompt("bugs")

    for guide in REVIEW_GUIDES[("rtl", "bugs")]:
        assert f"({guide.name})" in system


def test_tb_guides_resolve_the_scope_contract_on_first_use() -> None:
    # Private seam: a run resolves its scope contract at validation, before guide selection.
    endpoint = _parsed(*_argv("tb", "quality"))

    guides = endpoint._review_guides("quality", "tb")

    assert guides
    assert endpoint._scope_contract is not None


def test_discovery_prompt_without_override_uses_the_parsed_focus() -> None:
    # Private seam: a run always names the focus of the call it prompts for.
    endpoint = _parsed(*_argv("rtl", "bugs,security"))

    assert "bugs, security" in endpoint._build_prompt()


def test_tb_prompt_sections_are_resolved_once_per_run(tmp_path: Path) -> None:
    # Private seam: one run builds its prompt once, so the cached sections are reused
    # only when a caller builds it again.
    endpoint = _parsed("--work-dir", str(tmp_path), *_argv("tb", "quality"))

    assert endpoint._build_prompt() == endpoint._build_prompt()
    assert endpoint._simulation_context is not None
    assert endpoint._tb_project_policy is not None
