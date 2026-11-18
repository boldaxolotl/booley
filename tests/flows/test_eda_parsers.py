"""Tests for booley.flows.eda_parsers — the shared EDA log parsers.

The private successor of the retired public ``adapterlib.parsers`` (ADR
0039): the Verilator/Verible regexes + first-error helpers the built-in
``lint`` imports, and the compiler-error gist ``elaborate`` uses. Warning
parsing through the regexes is additionally covered end-to-end in
test_lint.py (``parse_warnings`` / ``parse_verible_warnings``).
"""

from __future__ import annotations

import textwrap

import pytest

from booley.flows.eda_parsers import (
    VERILATOR_WARNING_RE,
    extract_error_gist,
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
    def test_warning_regex_ignores_echoed_source_gutter_line(self):
        """A string literal echoed under a real warning is not a second warning (#3673)."""
        log = (
            "%Warning-UNUSEDSIGNAL: rtl/a.sv:42:5: Signal is not used: 'foo'\n"
            '    6 |     $display("%Warning-UNUSEDSIGNAL: rtl/x.sv:1:1: fake");\n'
            "      |     ^\n"
        )
        found = [m.group("file") for m in VERILATOR_WARNING_RE.finditer(log)]
        assert found == ["rtl/a.sv"]


class TestVeribleParsers:
    def test_first_error_line(self):
        assert verible_first_error_line(VERIBLE_OUTPUT) == (
            'rtl/b.sv:3:1: syntax error at token "endmodule"'
        )
        assert verible_first_error_line("clean run\n") is None


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        (VERILATOR_OUTPUT, "Blocked and non-blocking"),
        ("rtl/top.v:12: syntax error near endmodule\nI give up.\n", "syntax error"),
        ("", ""),
    ],
    ids=["verilator", "icarus-style", "empty"],
)
def test_error_gist(output: str, expected: str) -> None:
    gist = extract_error_gist(output)
    assert expected in gist if expected else gist == ""
