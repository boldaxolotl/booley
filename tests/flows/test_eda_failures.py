"""Shared EDA execution-failure classification regressions."""

import pytest

from booley.flows.base import SubprocessResult
from booley.flows.eda_failures import classify_eda_failure


def test_authenticated_missing_eda_tool_marker_survives_make_rc_translation() -> None:
    from booley.flows.eda_failures import (
        classify_eda_failure,
        format_missing_eda_tool,
        new_attempt_token,
        render_failure_marker,
    )

    token = new_attempt_token()
    marker = render_failure_marker(token, "missing_eda_tool", "yosys", "yosys")
    result = classify_eda_failure(
        SubprocessResult(returncode=2, stdout=marker + "\n", stderr=""),
        expected_token=token,
        expected_stage="yosys",
        expected_executable="yosys",
    )

    assert result is not None
    assert result.kind == "infrastructure"
    assert result.subject == "yosys"
    assert result.reason == format_missing_eda_tool("yosys")


def test_missing_required_toolchain_file_is_infrastructure() -> None:
    from booley.flows.eda_failures import classify_eda_failure

    result = classify_eda_failure(
        SubprocessResult(
            returncode=1,
            stdout="",
            stderr="fatal error: verilated.h: No such file or directory",
        ),
        expected_executable="verilator",
        authenticated_build=True,
    )

    assert result is not None
    assert result.kind == "infrastructure"
    assert result.failure_kind == "missing_required_file"
    assert result.subject == "verilated.h"


def test_untrusted_and_ambiguous_markers_are_ignored() -> None:
    from booley.flows.eda_failures import classify_eda_failure, render_failure_marker

    token = "0123456789abcdef0123456789abcdef"
    marker = render_failure_marker(token, "missing_eda_tool", "yosys", "yosys")
    for text in (
        marker.replace(token, "f" * 32),
        marker + " trailing",
        marker + "\n" + marker,
    ):
        result = classify_eda_failure(
            SubprocessResult(returncode=2, stdout=text),
            expected_token=token,
            expected_stage="yosys",
        )
        assert result is None


def test_runtime_missing_file_text_is_not_toolchain_infrastructure() -> None:
    from booley.flows.eda_failures import classify_eda_failure

    result = classify_eda_failure(
        SubprocessResult(
            returncode=1,
            stdout="DUT: vectors/input.bin: No such file or directory",
        ),
        authenticated_build=True,
    )
    assert result is None


def test_direct_spawn_failure_names_boundary_not_child() -> None:
    from booley.flows.eda_failures import classify_eda_failure

    result = classify_eda_failure(
        SubprocessResult(returncode=-1),
        expected_executable="verilator",
        boundary_executable="make",
    )
    assert result is not None
    assert result.subject == "make"


@pytest.mark.parametrize(
    "executable,alias", [("vivado", "vivado"), ("verilator", "verilator_bin"), ("yosys", "yosys")]
)
@pytest.mark.parametrize("rc", [0, 127])
def test_owned_dynamic_loader_failure(executable, alias, rc):
    diagnostic = f"{alias}: error while loading shared libraries: libncurses.so.5: cannot open shared object file"
    failure = classify_eda_failure(
        SubprocessResult(returncode=rc, stderr=diagnostic), expected_executable=executable
    )
    assert failure is not None
    assert failure.kind == "infrastructure"
    assert diagnostic in failure.reason


@pytest.mark.parametrize(
    "text",
    [
        "helper: error while loading shared libraries: libx.so: missing",
        'ERROR: RTL string "yosys: error while loading shared libraries: libx.so: missing"',
        "yosys: compilation error in design",
    ],
)
def test_loader_unowned_and_rtl_negative_controls(text):
    assert (
        classify_eda_failure(
            SubprocessResult(returncode=0, stdout=text), expected_executable="vivado"
        )
        is None
    )


_MARKER_LINE = (
    "BOOLEY_EDA_FAILURE token=0123456789abcdef0123456789abcdef "
    "kind=missing_eda_tool stage=sv2v subject=sv2v"
)


@pytest.mark.parametrize(
    ("text", "default", "expected"),
    [
        ("make: Entering directory '/w'\nsv2v: bad option", None, "sv2v: bad option"),
        ("make[1]: Entering directory '/w'\nERROR: boom", None, "ERROR: boom"),
        (f"BOOLEY_STAGE: sv2v\n{_MARKER_LINE}\nERROR: boom", None, "ERROR: boom"),
        ("WARNING: [Vivado 1-2] foo not found\nERROR: real", None, "ERROR: real"),
        ("[WARNING STA-1] foo not found\n[ERROR STA-2] real", None, "[ERROR STA-2] real"),
        ("/bin/sh: lsb_release: command not found\nERROR: real", None, "ERROR: real"),
        (
            "/opt/Vivado/bin/loader: line 280: lsb_release: command not found\nERROR: real",
            None,
            "ERROR: real",
        ),
        ("mesh: net_7: not found\nplain", None, "mesh: net_7: not found"),
        (
            "/bin/sh: 1: klayout: not found\nmake: *** Error 127",
            None,
            "/bin/sh: 1: klayout: not found",
        ),
        ("make: *** Error 1\nplain line\nsecond", None, "plain line"),
        ("make: *** Error 1\n\n", None, "no diagnostic available"),
        ("make: *** Error 1\nnothing", "fallback", "fallback"),
        ("x" * 600, None, "x" * 500),
    ],
    ids=[
        "make",
        "make-nested",
        "stage-and-marker",
        "vivado-warning",
        "bracket-warning",
        "shell-missing-program",
        "loader-missing-program",
        "non-shell-not-found",
        "shell-missing-program-fallback",
        "first-line-fallback",
        "empty-fallback",
        "default",
        "cap-500",
    ],
)
def test_2808_first_tool_diagnostic(text: str, default: str | None, expected: str) -> None:
    from booley.flows.eda_failures import first_tool_diagnostic

    assert first_tool_diagnostic(text, default) == expected


def test_2808_strip_failure_markers_drops_marker_lines_only() -> None:
    from booley.flows.eda_failures import strip_failure_markers

    other_token = _MARKER_LINE.replace("0123456789abcdef", "fedcba9876543210")
    text = f"before\n{_MARKER_LINE}\n  BOOLEY_EDA_FAILURE junk\n{other_token}\nafter"
    assert strip_failure_markers(text) == "before\nafter"
