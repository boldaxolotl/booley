"""The rendered Public QA packets are executable current-v2 Ticket documents."""

from pathlib import Path

import pytest

from booley.ticket_board.ticket_document import (
    TicketAuthoringView,
    TicketConversionContext,
    convert_ticket_document,
)

ROOT = Path(__file__).resolve().parents[2] / "qa/scenarios/picorv32/tickets"
TESTS = {
    "sim_core": ("main", "axi"),
    "sim_wb": ("wb",),
    "sim_dhry_checked": ("dhry",),
    "sim_core_zbb": ("zbb_core",),
    "sim_axi_zbb": ("zbb_axi",),
    "sim_wb_zbb": ("zbb_wb",),
    "sim_zbb_disabled": ("zbb_disabled",),
}


def _render_packet(name: str, *, fpga: bool) -> str:
    asset = (ROOT / name).read_text()
    rendered = (
        asset.replace("{{ outer_destination_branch }}", "outer-release")
        .replace("{{ project_destination_ref }}", "refs/heads/project-data")
        .replace(
            "{{ configured_fpga_criterion }}",
            "  FPGA:\n    fpga_core_zbb (temp): pass" if fpga else "",
        )
    )
    return rendered.split("```markdown\n", 1)[1].split("\n```", 1)[0]


def _context() -> TicketConversionContext:
    view = TicketAuthoringView(
        resolve_target=lambda selector, _flow: selector,
        tests_for_target=lambda target: TESTS.get(target, ()),
    )
    return TicketConversionContext("draft", lambda _generated: view)


@pytest.mark.parametrize(
    ("name", "fpga", "target_count"),
    [
        ("continuity.md", False, 1),
        ("evolution.md", False, 6),
        ("evolution.md", True, 7),
    ],
)
def test_rendered_qa_packet_converts_through_ticket_boundary(
    name: str, fpga: bool, target_count: int
) -> None:
    conversion = convert_ticket_document(_render_packet(name, fpga=fpga), _context())

    assert conversion.diagnostics == ()
    assert conversion.document is not None
    plan = conversion.document.spec.target_plan
    assert plan is not None
    assert len(plan.entries) == target_count
    capabilities = {criterion.capability for criterion in conversion.document.spec.criteria}
    assert ("FPGA" in capabilities) is fpga
