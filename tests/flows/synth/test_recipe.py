"""Transport changes cannot reuse older synthesis recipe evidence."""

import dataclasses
from types import SimpleNamespace

import pytest

from booley.flows.synth.backends.openroad import ppa as openroad_ppa
from booley.flows.synth.backends.yosys import ppa as yosys_ppa
from booley.flows.synth.recipe import (
    default_recipe_args,
    synthesis_recipe_snapshot,
    synthesis_recipe_snapshot_fingerprint,
)


@pytest.mark.parametrize("datatype,value", [("int", 12), ("str", "MAC"), ("file", "a.mem")])
def test_parameter_transport_revision_only_invalidates_string_recipes(tmp_path, datatype, value):
    resolved = SimpleNamespace(
        flow_options={"synth_mode": "logical"},
        sdc_files=(),
        build_root=tmp_path,
        vlnv="proof:lib:top:1",
        toplevel="top",
        parameters={"N": {"datatype": datatype, "paramtype": "vlogparam", "default": value}},
    )
    current = synthesis_recipe_snapshot(resolved, default_recipe_args(), target="synth")
    previous = dict(current)
    previous.pop("parameter_transport_revision", None)
    assert current["recipe_args"] == previous["recipe_args"]
    changed = synthesis_recipe_snapshot_fingerprint(
        current
    ) != synthesis_recipe_snapshot_fingerprint(previous)
    assert changed is (datatype in ("str", "file"))


def _resolved(tmp_path, *, mode: str, **flow_options):
    return SimpleNamespace(
        flow_options={"synth_mode": mode, **flow_options},
        sdc_files=(),
        build_root=tmp_path,
        vlnv="proof:lib:top:1",
        toplevel="top",
        parameters={},
    )


def _fingerprint(tmp_path, *, mode: str, profile: str | None = None) -> str:
    options = {"ppa_profile": profile} if profile else {}
    snapshot = synthesis_recipe_snapshot(
        _resolved(tmp_path, mode=mode, **options), default_recipe_args(), target="synth"
    )
    return synthesis_recipe_snapshot_fingerprint(snapshot)


# The edited profile table is the input under test, and no public API edits it.
_YOSYS_TABLE = yosys_ppa._YOSYS_PROFILES
_OPENROAD_TABLE = openroad_ppa._PROFILES


@pytest.mark.parametrize(
    ("table", "field", "value", "mode", "invalidates"),
    [
        (_YOSYS_TABLE, "abc_delay_ps", 1234, "logical", True),
        (_YOSYS_TABLE, "abc_delay_ps", 1234, "physical", True),
        (_OPENROAD_TABLE, "setup_margin_ns", 0.7, "physical", True),
        # Logical mode never runs OpenROAD, so its repair knobs are not recipe.
        (_OPENROAD_TABLE, "setup_margin_ns", 0.7, "logical", False),
    ],
)
def test_profile_table_edit_invalidates_only_recipes_that_run_it(
    tmp_path, monkeypatch, table, field, value, mode, invalidates
):
    before = _fingerprint(tmp_path, mode=mode, profile="max_frequency")
    monkeypatch.setitem(
        table, "max_frequency", dataclasses.replace(table["max_frequency"], **{field: value})
    )

    assert (_fingerprint(tmp_path, mode=mode, profile="max_frequency") != before) is invalidates


def test_default_profile_is_resolved_when_unset(tmp_path):
    """A Target without ``ppa_profile`` fingerprints as ``balanced``."""
    assert _fingerprint(tmp_path, mode="logical") == _fingerprint(
        tmp_path, mode="logical", profile="balanced"
    )
