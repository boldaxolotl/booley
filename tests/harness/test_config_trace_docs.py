"""Contracts for the trace guidance in the public ``.core`` example."""

from pathlib import Path

TARGETS_MD = Path(__file__).resolve().parents[2] / "docs" / "user" / "TARGETS.md"
DESIGN_SECTION = (
    TARGETS_MD.read_text(encoding="utf-8")
    .split("## Design description (`.core`)", 1)[1]
    .split("\n## ", 1)[0]
)
CORE_EXAMPLE = DESIGN_SECTION.split("```yaml", 1)[1].split("```", 1)[0]


def test_public_core_example_needs_no_trace_dump_module():
    # Booley supplies the Icarus dump module itself, so the example must not
    # teach users to add one to their filesets.
    assert "booley_vcd_dump.sv" not in CORE_EXAMPLE
