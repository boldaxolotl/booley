"""Byte-preserving Target-surface span regression coverage."""

import pytest

from booley.ticket_board import target_plan


def _validate_core_boundary(
    baseline: str, current: str, names: dict[str, tuple[str, ...]]
) -> None:
    target_plan._validate_core_source_boundary(
        target_plan.TargetSurfaceFile("toy.core", baseline.encode(), current.encode()),
        names.get("targets", ()),
        names.get("filesets", ()),
        names.get("parameters", ()),
    )


def _core(section: str, entries: str, *, line_ending: str = "\n") -> str:
    text = (
        "CAPI=2:\n"
        "name: acme:lib:toy:1.0\n"
        f"{section}:\n"
        f"{entries}"
        "\n"
        "# separator comment\n"
        "\n"
        "targets:\n"
        "  baseline: {}\n"
    )
    return text.replace("\n", line_ending)


@pytest.mark.parametrize(
    ("section", "existing", "added", "names"),
    [
        (
            "parameters",
            "  existing:\n    datatype: int\n",
            "  future:\n    datatype: int  # inline\n    paramtype: plusarg",
            {"parameters": ("future",)},
        ),
        (
            "filesets",
            "  existing:\n    files: [existing.sv]\n",
            "  future:\n    files: [future.sv]\n    file_type: systemVerilogSource",
            {"filesets": ("future",)},
        ),
    ],
)
@pytest.mark.parametrize(
    ("line_ending", "final_newline"), [("\n", True), ("\r\n", True), ("\r\n", False)]
)
def test_final_block_entry_removal_preserves_separator_bytes(
    section: str,
    existing: str,
    added: str,
    names: dict[str, tuple[str, ...]],
    line_ending: str,
    final_newline: bool,
) -> None:
    baseline = _core(section, existing, line_ending=line_ending)
    current = _core(section, existing + added + "\n", line_ending=line_ending)
    if not final_newline:
        baseline = baseline.removesuffix(line_ending)
        current = current.removesuffix(line_ending)

    _validate_core_boundary(baseline, current, names)


def test_public_boundary_accepts_crlf_without_final_newline() -> None:
    baseline = _core("parameters", "  existing:\n    datatype: int\n")
    current = _core(
        "parameters",
        "  existing:\n    datatype: int\n  future:\n    datatype: int\n    paramtype: plusarg\n",
        line_ending="\r\n",
    )
    baseline = baseline.replace("\n", "\r\n").removesuffix("\r\n")
    current = current.removesuffix("\r\n")

    _validate_core_boundary(baseline, current, {"parameters": ("future",)})


@pytest.mark.parametrize("header", ["|", "|+", "&anchor |+", "!!str |+"])
def test_block_scalar_content_preserves_separator_bytes(header: str) -> None:
    baseline = _core("parameters", "  existing:\n    datatype: int\n")
    current = _core(
        "parameters",
        f"  existing:\n    datatype: int\n  future: {header}\n    first\n\n    third\n",
    )

    _validate_core_boundary(baseline, current, {"parameters": ("future",)})


def test_multiple_blank_separator_lines_and_comments_are_external_to_entry() -> None:
    baseline = _core("filesets", "  existing:\n    files: [existing.sv]\n")
    current = _core(
        "filesets",
        "  existing:\n    files: [existing.sv]\n  future:\n    files: [future.sv]\n",
    )
    baseline = baseline.replace("\n# separator comment", "\n\n\n# separator comment")
    current = current.replace("\n# separator comment", "\n\n\n# separator comment")

    _validate_core_boundary(baseline, current, {"filesets": ("future",)})


def test_flow_style_entry_and_nonfinal_sibling_keep_their_line_boundaries() -> None:
    baseline = _core("parameters", "  later: {datatype: int}\n")
    current = _core(
        "parameters",
        "  future: {datatype: int}\n  later: {datatype: int}\n",
    )

    _validate_core_boundary(baseline, current, {"parameters": ("future",)})


def test_public_boundary_rejects_separator_edits() -> None:
    baseline = _core("parameters", "  existing:\n    datatype: int\n")
    current = _core(
        "parameters",
        "  existing:\n    datatype: int\n  future:\n    datatype: int\n",
    ).replace("# separator comment", "# changed separator")

    with pytest.raises(target_plan.TargetPlanValidationError, match="outside planned"):
        _validate_core_boundary(baseline, current, {"parameters": ("future",)})
