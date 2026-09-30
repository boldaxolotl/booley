"""Coverage source-gap policy does not consult hits, dispositions or scored classes."""

from dataclasses import replace

from booley.flows.sim.coverage_campaign import freeze_coverage_mapping
from booley.flows.sim.coverage_source_gaps import GAP_CODE, INCOMPLETE_CODE, source_gap_findings
from booley.flows.sim.verilator_declarations import (
    Declaration,
    DeclarationDiagnostic,
    DeclarationInventory,
)
from tests.flows.sim.test_verilator_declarations import _source


def _inventory():
    sources = (
        _source("rtl/unused.sv"),
        _source("rtl/used.sv"),
        replace(_source("rtl/header.svh"), include=True),
        replace(_source("tb.sv"), testbench=True),
        _source("rtl/pkg.sv"),
    )
    declarations = tuple(
        Declaration(
            "PACKAGE" if s.path.endswith("pkg.sv") else "MODULE", f"m{i}", s.path, s.path, 1, 1
        )
        for i, s in enumerate(sources)
    )
    return DeclarationInventory(("v5.052", "pinned"), "generation:1", sources, declarations)


def _closure(inventory):
    return freeze_coverage_mapping(
        {
            "rtl": [
                {"path": s.path, "sha256": s.sha256} for s in inventory.sources if not s.testbench
            ],
            "testbench": [],
        }
    )


def test_absent_points_warns_only_for_module_bearing_non_include_rtl():
    inventory = _inventory()
    findings = source_gap_findings(inventory, _closure(inventory), ("rtl/used.sv",), ())
    assert [(f.code, f.pointer) for f in findings] == [(GAP_CODE, "/source_closure/rtl/0/path")]
    assert "rtl/unused.sv" in findings[0].message


def test_presence_counts_even_when_no_scored_point_exists():
    inventory = _inventory()
    assert (
        source_gap_findings(inventory, _closure(inventory), ("rtl/used.sv", "rtl/unused.sv"), ())
        == ()
    )


def test_incomplete_observations_suppress_all_source_accusations():
    inventory = _inventory()
    for observed, unresolved in [
        (None, ()),
        (inventory, ("ambiguous.sv",)),
        (
            replace(
                inventory,
                diagnostics=(DeclarationDiagnostic("logical_locations", "line mapping"),),
            ),
            (),
        ),
    ]:
        findings = source_gap_findings(observed, _closure(inventory), (), unresolved)
        assert [f.code for f in findings] == [INCOMPLETE_CODE]


def test_presentation_bounds_escaped_paths_and_omissions():
    from booley.flows.sim.coverage_source_gaps import source_gap_report_lines
    from tests.mcp_tools.test_coverage_evidence import _campaign_with_source_gaps

    campaign, paths = _campaign_with_source_gaps(8)
    lines = source_gap_report_lines(campaign)
    assert lines[0] == "RTL sources without coverage points: 8"
    assert len(lines) == 5
    assert all(paths[i] in lines[i + 1] for i in range(3))
    assert "5 more" in lines[-1]
    records = list(campaign.source_closure["rtl"])
    records[0] = {"path": 'rtl/aaa_quote"\n.sv', "sha256": "sha256:" + "a" * 64}
    campaign = replace(
        campaign, source_closure=freeze_coverage_mapping({"rtl": records, "testbench": []})
    )
    text = "\n".join(source_gap_report_lines(campaign))
    assert "\\n.sv" in text and '\\"' in text
