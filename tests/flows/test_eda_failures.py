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
