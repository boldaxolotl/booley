"""Shared EDA execution-failure classification regressions."""

from booley.flows.base import SubprocessResult


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
