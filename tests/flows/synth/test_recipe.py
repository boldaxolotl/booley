"""Transport changes cannot reuse older synthesis recipe evidence."""

import dataclasses
from types import SimpleNamespace

import pytest

from booley.core.boundary import BoundaryError
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


def _fingerprint(tmp_path, *, mode: str, profile: str | None = None, **flow_options) -> str:
    options = {"ppa_profile": profile} if profile else {}
    options.update(flow_options)
    snapshot = synthesis_recipe_snapshot(
        _resolved(tmp_path, mode=mode, **options), default_recipe_args(), target="synth"
    )
    return synthesis_recipe_snapshot_fingerprint(snapshot)


# The edited profile table is the input under test, and no public API edits it.
_YOSYS_TABLE = yosys_ppa._YOSYS_PROFILES
_OPENROAD_TABLE = openroad_ppa._PROFILES


@pytest.mark.parametrize(
    ("table", "field", "value", "mode", "options", "invalidates"),
    [
        (_YOSYS_TABLE, "abc_delay_ps", 1234, "logical", {}, True),
        (_YOSYS_TABLE, "abc_delay_ps", 1234, "physical", {}, True),
        (_OPENROAD_TABLE, "setup_margin_ns", 0.7, "physical", {}, True),
        # Logical mode never runs OpenROAD, so its repair knobs are not recipe.
        (_OPENROAD_TABLE, "setup_margin_ns", 0.7, "logical", {}, False),
        # An override that replaces the edited field hides the edit.
        (
            _OPENROAD_TABLE,
            "setup_margin_ns",
            0.7,
            "physical",
            {"advanced_settings_openroad": {"setup_margin_ns": 0.5}},
            False,
        ),
    ],
)
def test_profile_table_edit_invalidates_only_recipes_that_run_it(
    tmp_path, monkeypatch, table, field, value, mode, options, invalidates
):
    before = _fingerprint(tmp_path, mode=mode, profile="max_frequency", **options)
    monkeypatch.setitem(
        table, "max_frequency", dataclasses.replace(table["max_frequency"], **{field: value})
    )

    after = _fingerprint(tmp_path, mode=mode, profile="max_frequency", **options)
    assert (after != before) is invalidates


@pytest.mark.parametrize(
    ("mode", "flow_options", "call", "yosys", "openroad"),
    [
        pytest.param(
            "physical",
            {"ppa_profile": "balanced", "advanced_settings_openroad": {"repair_hold": True}},
            {},
            {},
            {"repair_hold": True},
            id="balanced-target-repair-hold",
        ),
        pytest.param(
            "logical",
            {"advanced_settings_yosys": {"abc_recipe": "fast"}},
            {},
            {"abc_recipe": "fast"},
            None,
            id="target-abc-recipe-logical",
        ),
        pytest.param(
            "physical",
            {"advanced_settings_yosys": {"abc_recipe": "fast"}},
            {},
            {"abc_recipe": "fast"},
            {},
            id="target-abc-recipe-physical",
        ),
        pytest.param(
            "physical",
            {"advanced_settings_openroad": {"repair_hold": True}},
            {"repair_hold": False},
            {},
            {"repair_hold": False},
            id="call-flag-beats-target",
        ),
        pytest.param(
            "physical",
            {
                "ppa_profile": "compact",
                "advanced_settings_yosys": {"abc_recipe": "fast"},
                "advanced_settings_openroad": {"repair_hold": True},
            },
            {"ppa_profile": "max_frequency"},
            {"abc_recipe": "balanced"},
            {"repair_hold": False, "gate_cloning": True, "utilization_pct": 40.0},
            id="call-profile-skips-target",
        ),
    ],
)
def test_snapshot_records_the_settings_in_effect(
    tmp_path, mode, flow_options, call, yosys, openroad
):
    args = default_recipe_args()
    vars(args).update(call)

    snapshot = synthesis_recipe_snapshot(
        _resolved(tmp_path, mode=mode, **flow_options), args, target="synth"
    )

    settings = snapshot["effective_ppa_settings"]
    assert ("openroad" in settings) is (mode == "physical")
    assert {k: settings["yosys"][k] for k in yosys} == yosys
    if openroad is not None:
        assert {k: settings["openroad"][k] for k in openroad} == openroad


@pytest.mark.parametrize(
    ("call", "message"),
    [
        pytest.param({"setup_margin_ns": float("-inf")}, "setup-margin-ns", id="argv-parse"),
        pytest.param({"utilization_pct": float("nan")}, "utilization_pct", id="range-check"),
    ],
)
def test_snapshot_rejects_settings_the_backend_rejects(tmp_path, call, message):
    args = default_recipe_args()
    vars(args).update(call)

    with pytest.raises(BoundaryError, match=message):
        synthesis_recipe_snapshot(_resolved(tmp_path, mode="physical"), args, target="synth")


def test_default_profile_is_resolved_when_unset(tmp_path):
    """A Target without ``ppa_profile`` fingerprints as ``balanced``."""
    assert _fingerprint(tmp_path, mode="logical") == _fingerprint(
        tmp_path, mode="logical", profile="balanced"
    )
