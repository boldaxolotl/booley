"""Transport changes cannot reuse older synthesis recipe evidence."""

from types import SimpleNamespace

from booley.flows.synth.recipe import (
    default_recipe_args,
    synthesis_recipe_snapshot,
    synthesis_recipe_snapshot_fingerprint,
)


def test_parameter_transport_revision_changes_identity_without_numeric_argv_changes(tmp_path):
    resolved = SimpleNamespace(
        flow_options={"synth_mode": "logical"},
        sdc_files=(),
        build_root=tmp_path,
        vlnv="proof:lib:top:1",
        toplevel="top",
        parameters={"N": {"datatype": "int", "paramtype": "vlogparam", "default": 12}},
    )
    current = synthesis_recipe_snapshot(resolved, default_recipe_args(), target="synth")
    previous = dict(current)
    previous.pop("parameter_transport_revision")
    assert current["recipe_args"] == previous["recipe_args"]
    assert synthesis_recipe_snapshot_fingerprint(current) != synthesis_recipe_snapshot_fingerprint(
        previous
    )
