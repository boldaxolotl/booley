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


@pytest.mark.parametrize("mode", ["logical", "physical"])
def test_yosys_profile_settings_change_fingerprint(tmp_path, monkeypatch, mode):
    """Editing a profile's Yosys settings must invalidate recipe evidence."""
    before = _fingerprint(tmp_path, mode=mode, profile="max_frequency")
    table = yosys_ppa._YOSYS_PROFILES
    monkeypatch.setitem(
        table, "max_frequency", dataclasses.replace(table["max_frequency"], abc_delay_ps=1234)
    )
    assert _fingerprint(tmp_path, mode=mode, profile="max_frequency") != before


def test_openroad_profile_settings_change_fingerprint(tmp_path, monkeypatch):
    """Editing a profile's OpenROAD settings must invalidate physical recipe evidence."""
    before = _fingerprint(tmp_path, mode="physical", profile="max_frequency")
    table = openroad_ppa._PROFILES
    monkeypatch.setitem(
        table, "max_frequency", dataclasses.replace(table["max_frequency"], setup_margin_ns=0.7)
    )
    assert _fingerprint(tmp_path, mode="physical", profile="max_frequency") != before


def test_openroad_profile_settings_do_not_affect_logical_fingerprint(tmp_path, monkeypatch):
    """Logical mode never runs OpenROAD, so its repair knobs are not recipe."""
    before = _fingerprint(tmp_path, mode="logical", profile="max_frequency")
    table = openroad_ppa._PROFILES
    monkeypatch.setitem(
        table, "max_frequency", dataclasses.replace(table["max_frequency"], setup_margin_ns=0.7)
    )
    assert _fingerprint(tmp_path, mode="logical", profile="max_frequency") == before


def test_default_profile_is_resolved_when_unset(tmp_path):
    """A Target without ``ppa_profile`` fingerprints as ``balanced``."""
    assert _fingerprint(tmp_path, mode="logical") == _fingerprint(
        tmp_path, mode="logical", profile="balanced"
    )
