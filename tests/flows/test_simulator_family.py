"""Unit tests for the simulator family names."""

import pytest

from booley.flows import simulator_family


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("verilator", "verilator"),
        ("icarus", "icarus"),
        ("iverilog", "icarus"),
        ("xrun", "xcelium"),
        ("vcs", "vcs"),
    ],
)
def test_eda_tool_normalization(raw: str | None, expected: str) -> None:
    assert simulator_family.normalize_eda_tool(raw) == expected


@pytest.mark.parametrize("raw", (None, "", "mystery-sim"))
def test_eda_tool_normalization_rejects_missing_or_unknown_values(raw: str | None) -> None:
    with pytest.raises(ValueError, match="unknown simulator EDA tool"):
        simulator_family.normalize_eda_tool(raw)


def test_matching_eda_tool_canonicalizes_aliases() -> None:
    assert simulator_family.matching_eda_tool("iverilog", "icarus", target="sim") == "icarus"


def test_matching_eda_tool_rejects_disagreement() -> None:
    with pytest.raises(ValueError, match=r"declared 'icarus'.*configured 'verilator'"):
        simulator_family.matching_eda_tool("icarus", "verilator", target="sim")
