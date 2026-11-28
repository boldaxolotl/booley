"""Unit tests for the Mutant report snippet scanner."""

from __future__ import annotations

from booley.flows.mutation.snippet import (
    SNIPPET_MAX_CHARS,
    detection_snippet,
    tail,
    tail_snippet,
)


def test_failure_words_ignore_paths_flags_and_identifiers() -> None:
    output = "\n".join(
        [
            "CMD: iverilog -Werror /ip/error-correction/dut.sv",
            "[SIM_SUMMARY] sva_errors=0",
            "reading /ip/assertion-lib/x/error.sv",
            "%Error: tb.sv:3: bad thing",
            "after one",
            "after two",
            "after three",
        ]
    )

    snippet = detection_snippet("full", output, timed_out=False)

    assert snippet == "%Error: tb.sv:3: bad thing\nafter one\nafter two"


def test_failure_snippet_skips_blank_context_and_collapses_whitespace() -> None:
    output = "ok\n  AssertionError:    boom  \n\n   \n  ctx  a  \n"

    assert detection_snippet("t", output, timed_out=False) == "AssertionError: boom\nctx a"


def test_failure_without_a_failure_line_falls_back_to_the_tail() -> None:
    output = "\n".join(f"line {number}" for number in range(10))

    snippet = detection_snippet("full", output, timed_out=False)

    assert snippet == tail_snippet(output)
    assert snippet.splitlines() == [f"line {number}" for number in range(5, 10)]


def test_snippets_are_capped_and_empty_output_is_safe() -> None:
    long_line = "FATAL: " + "x" * 500

    assert len(detection_snippet("t", long_line, timed_out=False)) == SNIPPET_MAX_CHARS
    assert tail("", 5) == ""
    assert (
        detection_snippet("t", "", timed_out=True) == "t: ran past its time limit and was stopped"
    )


def test_header_and_banner_lines_are_never_anchors() -> None:
    output = "\n".join(
        [
            "[Verilator simulation: assertions on verilator]",
            "CMD: vvp sim +define+ASSERT_ON",
            "FILTER: mismatch",
            "FATAL: real failure",
        ]
    )

    assert detection_snippet("full", output, timed_out=False) == "FATAL: real failure"


def test_names_file_names_and_zero_counts_are_not_failures() -> None:
    output = "\n".join(
        [
            "[verilator simulation: assertion_tb]",
            "running assertions.test_x (1/2)",
            "writing error.log",
            "[SIM] failed=0",
            "Error: tb.sv:9: real failure.",
        ]
    )

    assert detection_snippet("full", output, timed_out=False).startswith("Error: tb.sv:9")


def test_killing_test_line_outranks_an_earlier_failure_word() -> None:
    output = "\n".join(
        [
            "    10.00ns INFO  test_a: error flag cleared",
            "    60.00ns WARNING  cocotb.regression  mixed.test_bad failed",
            "    AssertionError: boom",
        ]
    )

    snippet = detection_snippet("<cocotb-suite>", output, timed_out=False, killing_test="test_bad")

    assert snippet.startswith("60.00ns WARNING cocotb.regression mixed.test_bad failed")
    # An unnamed killer, or one that printed no line, falls back to the first failure word.
    assert detection_snippet("t", output, timed_out=False).startswith("10.00ns INFO test_a: error")
    assert detection_snippet("t", output, timed_out=False, killing_test="nope").startswith(
        "10.00ns INFO test_a: error"
    )


def test_timeout_line_outranks_an_earlier_failure_line() -> None:
    output = "FATAL: tb: mismatch\nERROR: iverilog simulation timed out (25s)\n"

    snippet = detection_snippet("full", output, timed_out=True)

    assert snippet.startswith("ERROR: iverilog simulation timed out (25s)")
