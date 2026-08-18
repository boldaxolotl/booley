"""Negative architecture guards for ADR 0049 host-execution removal."""

from pathlib import Path

from booley.dev_support.criteria import eligible_eda_tool_criterion_families
from booley.fusesoc_registry import TargetRef
from booley.target_surface import flow_can_drive


def test_retired_host_execution_surfaces_are_absent() -> None:
    root = Path(__file__).resolve().parents[1]
    assert not list((root / "src/booley/host_mcp").glob("*.py"))
    assert not (root / "src/booley/venue.py").exists()

    pyproject = (root / "pyproject.toml").read_text(encoding="utf-8")
    assert "booley-host-mcp" not in pyproject
    assert "host_mcp/templates" not in pyproject

    production = root / "src/booley"
    forbidden_imports = (
        "booley.host_mcp",
        "from booley import venue",
    )
    forbidden_symbols = (
        "CLASS_HOST",
        "max_host",
        "supported_venues",
        "default_venue",
        "_execute_host",
        "host_mcp_url",
        "host_mcp_spec_wired",
        "write_host_sim_makefile",
        "host_sim_make_command",
    )
    for path in production.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for forbidden in forbidden_imports:
            assert forbidden not in text, f"{forbidden!r} remains in {path}"
        for forbidden in forbidden_symbols:
            assert forbidden not in text, f"{forbidden!r} remains in {path}"


def test_unsupported_commercial_simulators_have_no_public_eligibility() -> None:
    """Xcelium/VCS may occur in captured logs, never in runnable policy."""
    for tool in ("xcelium", "vcs"):
        ref = TargetRef(
            name="vendor_sim",
            vlnv="vendor:lib:ip:1",
            core_file=Path("vendor.core"),
            eda_tool=tool,
            flow="sim",
        )
        assert not flow_can_drive("sim", ref)
        assert not flow_can_drive("elab", ref)
        assert eligible_eda_tool_criterion_families(tool) == frozenset()
