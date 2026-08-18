"""Unit tests for the Session-Runtime simulation command helpers."""

from pathlib import Path

import pytest

from booley.flows.sim import edam as sim_edam


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("verilator", "verilator"),
        ("icarus", "icarus"),
        ("iverilog", "icarus"),
        (None, "verilator"),
        ("xrun", "xcelium"),
        ("vcs", "vcs"),
    ],
)
def test_eda_tool_normalization(raw: str | None, expected: str) -> None:
    assert sim_edam.normalize_eda_tool(raw) == expected


def test_verilator_command(tmp_path: Path) -> None:
    assert sim_edam.sim_run_command(
        work_root=tmp_path / "wr",
        work_dir=tmp_path,
        toplevel="tb",
        eda_tool="verilator",
        plusargs=["test_id=3"],
    ) == ["wr/Vtb", "+test_id=3"]


def test_icarus_command(tmp_path: Path) -> None:
    assert sim_edam.sim_run_command(
        work_root=tmp_path / "wr",
        work_dir=tmp_path,
        toplevel="tb",
        eda_tool="icarus",
        plusargs=["test_id=2"],
    ) == ["make", "-C", "wr", "run", "EXTRA_OPTIONS=+test_id=2"]


def test_commercial_command_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="not supported"):
        sim_edam.sim_run_command(
            work_root=tmp_path / "wr",
            work_dir=tmp_path,
            toplevel="tb",
            eda_tool="xcelium",
        )


@pytest.mark.parametrize(
    ("output", "returncode", "fragment"),
    [
        ("[SIM_RESULT] PASSED\n", 0, '"passed":true'),
        ("[SIM_RESULT] FAILED\n", 0, '"passed":false'),
        ("some output\n", 1, '"passed":false'),
        ("nothing notable\n", 0, '"inconclusive":true'),
    ],
)
def test_reemit_summary(output: str, returncode: int, fragment: str) -> None:
    assert fragment in sim_edam.reemit_sim_summary(output, returncode)


def test_reemit_summary_is_idempotent() -> None:
    raw = 'sim out\n[SIM_SUMMARY] {"passed":true,"sva_errors":0}\n'
    assert sim_edam.reemit_sim_summary(raw, 0) == raw
