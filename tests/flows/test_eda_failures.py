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


# Real capture: Verilator 5.054, ``verilator --lint-only -Wno-fatal top.sv``, exit 0.
# ``top.sv`` line 6 holds a string literal that reads like a spawn failure;
# Verilator echoes it (string literals kept, comments stripped) in a gutter line.
_VERILATOR_ECHOED_SOURCE_LOG = """\
%Warning-WIDTHEXPAND: top.sv:5:18: Operator ADD expects 8 bits on the LHS, but LHS's VARREF 'a' generates 4 bits.
                                 : ... note: In instance 'top'
    5 |     assign y = a + 8'h1;
      |                  ^
                      ... For warning description see https://verilator.org/warn/WIDTHEXPAND?v=5.054
                      ... Use "/* verilator lint_off WIDTHEXPAND */" and lint_on around source to disable this message.
%Warning-WIDTHTRUNC: top.sv:5:14: Operator ASSIGNW expects 4 bits on the Assign RHS, but Assign RHS's ADD generates 8 bits.
                                : ... note: In instance 'top'
    5 |     assign y = a + 8'h1;
      |              ^
                     ... For warning description see https://verilator.org/warn/WIDTHTRUNC?v=5.054
                     ... Use "/* verilator lint_off WIDTHTRUNC */" and lint_on around source to disable this message.
%Warning-WIDTHEXPAND: top.sv:6:19: Operator ADD expects 32 or 8 bits on the LHS, but LHS's VARREF 'a' generates 4 bits.
                                 : ... note: In instance 'top'
    6 |     initial if (a + 8'h1 == 0) $display("could not invoke verilator (x): [Errno 2] No such file or directory: 'verilator'");
      |                   ^
- V e r i l a t i o n   R e p o r t: Verilator 5.054 2026-11-01 rev vUNKNOWN-built20261112-bbb60a4
- Verilator: Built from 0.029 MB sources in 3 modules, into 0.012 MB in 3 C++ files needing 0.000 MB
- Verilator: Walltime 0.005 s (elab=0.001, cvt=0.002, bld=0.000); cpu 0.005 s on 1 threads; allocated 18.918 MB
"""


def test_echoed_verilator_source_is_not_a_missing_tool() -> None:
    from booley.flows.eda_failures import find_missing_executable

    assert "    6 |" in _VERILATOR_ECHOED_SOURCE_LOG
    result = classify_eda_failure(
        SubprocessResult(returncode=0, stdout=_VERILATOR_ECHOED_SOURCE_LOG),
        expected_stage="lint",
        expected_executable="verilator",
        boundary_executable="sh",
    )
    assert result is None
    assert find_missing_executable(_VERILATOR_ECHOED_SOURCE_LOG) is None


@pytest.mark.parametrize(
    "text,tool",
    [
        (
            "setup failed: could not invoke fusesoc (fusesoc): [Errno 2] No such file or directory: 'fusesoc'",
            "fusesoc",
        ),
        ("ERROR:root:FileNotFoundError: [Errno 2] No such file or directory: 'xrun'", "xrun"),
        ("could not invoke vcs (vcs): missing", "vcs"),
    ],
)
def test_wrapped_spawn_failure_still_names_the_tool(text: str, tool: str) -> None:
    from booley.flows.eda_failures import find_missing_executable

    assert find_missing_executable(text) == tool


def test_ordinary_failure_during_another_jobs_oom_kill_keeps_its_own_failure(monkeypatch) -> None:
    monkeypatch.setenv("BOOLEY_DEV_MEMORY_ADMISSION", "1")
    # The container's OOM counter moved, but this command's own process was not killed.
    failure = classify_eda_failure(
        SubprocessResult(returncode=1, oom_kill_delta=1),
        expected_stage="yosys",
        expected_executable="yosys",
    )

    assert failure is None or failure.failure_kind != "abnormal_termination"


def test_switch_unset_classifies_any_container_oom_kill_as_abnormal(monkeypatch) -> None:
    monkeypatch.delenv("BOOLEY_DEV_MEMORY_ADMISSION", raising=False)

    failure = classify_eda_failure(
        SubprocessResult(returncode=1, oom_kill_delta=1, oom_killed=False),
        expected_stage="yosys",
        expected_executable="yosys",
    )

    assert failure is not None and failure.failure_kind == "abnormal_termination"
