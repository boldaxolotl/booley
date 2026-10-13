"""Transport changes cannot reuse older synthesis recipe evidence."""

from types import SimpleNamespace

import pytest

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
