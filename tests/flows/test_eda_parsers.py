"""Tests for booley.flows.eda_parsers — the shared EDA log parsers.

The private successor of the retired public ``adapterlib.parsers`` (ADR
0039): the Verilator/Verible regexes + first-error helpers the built-in
``lint`` imports, and the compiler-error gist ``elaborate`` uses. Warning
parsing through the regexes is additionally covered end-to-end in
test_lint.py (``parse_warnings`` / ``parse_verible_warnings``).
"""

from __future__ import annotations

import textwrap

from booley.flows.eda_parsers import (
    VERIBLE_FINDING_RE,
    VERILATOR_WARNING_RE,
    extract_error_gist,
    first_error_line,
    verible_first_error_line,
)

VERILATOR_OUTPUT = textwrap.dedent(
    """
    %Warning-UNUSEDSIGNAL: rtl/a.sv:42:5: Signal is not used: 'foo'
    %Error-BLKANDNBLK: rtl/b.sv:7:3: Blocked and non-blocking assignments
    %Error: rtl/c.sv:12:9: syntax error, unexpected IDENTIFIER
    %Error: Exiting due to 2 error(s)
    """
)

VERIBLE_OUTPUT = textwrap.dedent(
    """
    rtl/a.sv:4:11: Interface names must end with _if. [interface-name-style]
    rtl/a.sv:10:1-5: Remove trailing spaces. [no-trailing-spaces]
    Some unrelated EDA-tool chatter.
    rtl/b.sv:3:1: syntax error at token "endmodule"
    """
)


class TestVerilatorParsers:
    def test_warning_regex_captures_location_and_rule(self):
        matches = list(VERILATOR_WARNING_RE.finditer(VERILATOR_OUTPUT))
        assert len(matches) == 1
        m = matches[0]
        assert (m.group("rule"), m.group("file"), m.group("line"), m.group("col")) == (
            "UNUSEDSIGNAL",
            "rtl/a.sv",
            "42",
            "5",
        )

    def test_first_error_line_finds_the_first_error(self):
        # The FIRST %Error line — a located finding, not the exit epilogue.
        assert first_error_line(VERILATOR_OUTPUT) == (
            "%Error-BLKANDNBLK: rtl/b.sv:7:3: Blocked and non-blocking assignments"
        )
        assert first_error_line("clean run\n") is None


class TestVeribleParsers:
    def test_finding_regex_keeps_rule_and_skips_parse_errors(self):
        # The column-range form (10:1-5) keeps its leading column; the located
        # parse-error line has no [rule] and must NOT match the finding regex —
        # it is the eda_tool_error signal, surfaced via the first-error helper
        # (QA-7).
        found = [
            (m.group("file"), int(m.group("line")), int(m.group("col")), m.group("rule"))
            for m in VERIBLE_FINDING_RE.finditer(VERIBLE_OUTPUT)
        ]
        assert found == [
            ("rtl/a.sv", 4, 11, "interface-name-style"),
            ("rtl/a.sv", 10, 1, "no-trailing-spaces"),
        ]

    def test_first_error_line(self):
        assert verible_first_error_line(VERIBLE_OUTPUT) == (
            'rtl/b.sv:3:1: syntax error at token "endmodule"'
        )
        assert verible_first_error_line("clean run\n") is None


class TestErrorGist:
    def test_verilator_error_gist(self):
        gist = extract_error_gist(VERILATOR_OUTPUT)
        assert "Blocked and non-blocking" in gist

    def test_icarus_style_gist(self):
        out = "rtl/top.v:12: syntax error near endmodule\nI give up.\n"
        assert "syntax error" in extract_error_gist(out)

    def test_empty_output(self):
        assert extract_error_gist("") == ""
