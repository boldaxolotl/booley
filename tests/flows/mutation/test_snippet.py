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
