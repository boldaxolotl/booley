from pathlib import Path

import pytest

from booley.evidence.recipe import (
    InvalidRecipeSnapshotError,
    implementation_comparison_basis,
    jsonable,
    recipe_changes,
    recipe_snapshot_fingerprint,
    validated_recipe_compatibility,
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
            "constraints": [{"core": "acme:ip:core:1.0", "sha256": "a" * 64}],
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
        "constraints": ["a" * 64],
    }


def _synth_snapshot(
    *,
    schema: int,
    digest: str = "a" * 64,
    name: str = "/checkout/timing.sdc",
) -> dict:
    snapshot = {
        "schema": schema,
        "target": "default",
        "vlnv": "::core:0",
        "toplevel": "top",
        "parameters": {},
        "recipe_args": ["--synth-mode", "logical"],
        "constraints": [{"name": name, "sha256": digest}],
        "technology": {"liberty": "/opt/pdk/stdcells.lib", "physical_pdk": None},
    }
    if schema == 4:
        snapshot["flow"] = "synth"
        snapshot["constraints"] = [{"core": "::core:0", "sha256": digest}]
    return snapshot


def test_recipe_compatibility_fails_closed() -> None:
    legacy = _synth_snapshot(schema=3)
    current = _synth_snapshot(schema=4)
    legacy_fingerprint = recipe_snapshot_fingerprint(legacy)
    current_fingerprint = recipe_snapshot_fingerprint(current)

    assert (
        validated_recipe_compatibility(legacy, legacy_fingerprint, current, current_fingerprint)
        is not None
    )
    assert validated_recipe_compatibility(legacy, "0" * 64, current, current_fingerprint) is None
    assert (
        validated_recipe_compatibility(legacy, legacy_fingerprint, None, current_fingerprint)
        is None
    )
    malformed = _synth_snapshot(schema=4, digest="not-a-digest")
    assert (
        validated_recipe_compatibility(
            legacy,
            legacy_fingerprint,
            malformed,
            recipe_snapshot_fingerprint(malformed),
        )
        is None
    )
    missing = _synth_snapshot(schema=4, digest="")
    missing["constraints"][0]["sha256"] = None
    assert (
        validated_recipe_compatibility(
            legacy,
            legacy_fingerprint,
            missing,
            recipe_snapshot_fingerprint(missing),
        )
        is None
    )
    unknown_schema = {**current, "schema": 5}
    assert (
        validated_recipe_compatibility(
            legacy,
            legacy_fingerprint,
            unknown_schema,
            recipe_snapshot_fingerprint(unknown_schema),
        )
        is None
    )


def test_comparison_basis_uses_ordered_constraint_content() -> None:
    first = _synth_snapshot(schema=4)
    first["constraints"].append({"core": "::dep:0", "sha256": "b" * 64})
    renamed = {
        **first,
        "constraints": [
            {"core": "::renamed:0", "sha256": "a" * 64},
            {"core": "::other:0", "sha256": "b" * 64},
        ],
    }
    reordered = {**first, "constraints": list(reversed(first["constraints"]))}
    changed = {
        **first,
        "constraints": [first["constraints"][0], {"core": "::dep:0", "sha256": "c" * 64}],
    }

    basis = implementation_comparison_basis(first)
    assert basis == implementation_comparison_basis(renamed)
    assert basis != implementation_comparison_basis(reordered)
    assert basis != implementation_comparison_basis(changed)
    for digest in (None, "abc", "g" * 64):
        invalid = {**first, "constraints": [{"core": "::core:0", "sha256": digest}]}
        with pytest.raises(InvalidRecipeSnapshotError, match="valid SHA-256"):
            implementation_comparison_basis(invalid)
