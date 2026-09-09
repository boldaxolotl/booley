from pathlib import Path

from booley.evidence.recipe import (
    implementation_comparison_basis,
    jsonable,
    recipe_changes,
    recipe_snapshot_fingerprint,
)


def test_jsonable_and_fingerprint_are_deterministic() -> None:
    left = {"z": (Path("rtl/top.sv"),), "a": {2: "two", 1: "one"}}
    right = {"a": {1: "one", 2: "two"}, "z": ["rtl/top.sv"]}

    assert jsonable(left) == {"a": {"1": "one", "2": "two"}, "z": ["rtl/top.sv"]}
    assert recipe_snapshot_fingerprint(jsonable(left)) == recipe_snapshot_fingerprint(right)


def test_recipe_changes_report_sorted_leaf_paths() -> None:
    changes = recipe_changes(
        {"parameters": {"width": 8}, "files": ["a.sv", "b.sv"]},
        {"parameters": {"depth": 4, "width": 16}, "files": ["a.sv"]},
    )

    assert changes == [
        {"path": "files[1]", "before": "b.sv", "after": None},
        {"path": "parameters.depth", "before": None, "after": 4},
        {"path": "parameters.width", "before": 8, "after": 16},
    ]


def test_comparison_basis_keeps_methodology_and_omits_design_inputs() -> None:
    basis = implementation_comparison_basis(
        {
            "flow": "fpga",
            "vlnv": "acme:ip:core:1.0",
            "toplevel": "top",
            "eda_tool": "vivado",
            "parameters": {"width": 8},
            "flow_options": {"part": "xc7a35t", "out_of_context": True},
            "ppa_profile": {"name": "balanced"},
            "constraints": [Path("timing.xdc")],
        }
    )

    assert basis == {
        "flow": "fpga",
        "vlnv": "acme:ip:core:1.0",
        "toplevel": "top",
        "eda_tool": "vivado",
        "part": "xc7a35t",
        "out_of_context": True,
        "ppa_profile": {"name": "balanced"},
        "constraints": ["timing.xdc"],
    }
