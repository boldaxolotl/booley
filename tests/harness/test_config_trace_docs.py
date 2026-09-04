"""Contracts for the trace guidance in the public ``.core`` example."""

from pathlib import Path

CONFIG_MD = Path(__file__).resolve().parents[2] / "docs" / "user" / "CONFIG.md"
DESIGN_SECTION = (
    CONFIG_MD.read_text(encoding="utf-8")
    .split("## Design description (`.core`) and tests (`tests.toml`)", 1)[1]
    .split("\n## ", 1)[0]
)
CORE_EXAMPLE = DESIGN_SECTION.split("```yaml", 1)[1].split("```", 1)[0]


def test_public_core_example_documents_backend_owned_trace_setup():
    contract = " ".join(DESIGN_SECTION.split())

    assert "booley_vcd_dump.sv" not in CORE_EXAMPLE
    assert "Icarus trace overlay supplies the packaged dump module" in contract
    assert "already provide one" in contract
    assert "Existing project-supplied modules remain supported" in contract
    assert "Verilator relies on the Target's trace-capable C++ simulation harness" in contract
