"""A persisted Test Run cannot carry an environment ``tests.toml`` would refuse (ADR 0072).

A Test Run's environment names become shell text in the previewed and the
launched command. Every decoder of a persisted Test Run therefore applies the
loader's rules: a shell-exportable name that Booley and the simulator
integration do not own. The export renderer refuses anything else again.
"""

from __future__ import annotations

import os
import shlex
import subprocess
from types import SimpleNamespace

import pytest

from booley.config.tests_toml_rules import TestRunNameError
from booley.flows.sim.campaign.codec import SimulationCampaignIntegrityError
from booley.flows.sim.campaign.projection import _validate_test_runs
from booley.flows.sim.campaign.serial_execution import _work_item_test_runs
from booley.flows.sim.campaign.test_run_binding import observation_test_runs
from booley.flows.sim.campaign.test_run_report import _manifest_test_runs
from booley.flows.sim.execution.engine import _export_line
from booley.flows.sim.flow import _manifest_run_groups
from booley.flows.test_runs import (
    build_test_run,
    declared_inputs_fingerprint,
    test_run_from_document,
)

_HOSTILE = ["X; id #", "A B", "$(id)", "A=B", "1X", "", "X\nY", "`id`"]
_RESERVED = ["BOOLEY_TARGET", "COCOTB_RANDOM_SEED", "RANDOM_SEED", "PYTHONPATH", "MODULE"]


def _record(name: str, test: str | None = "stress") -> dict[str, object]:
    """A record that is self-consistent in every way except its env name."""
    env = {name: "1"}
    return {
        "name": f"{test}@7" if test else "<default>@7",
        "test": test,
        "variant": None,
        "seed": 7,
        "args": [],
        "env": env,
        "fingerprint": declared_inputs_fingerprint((), env),
    }


@pytest.mark.parametrize("name", [*_HOSTILE, *_RESERVED])
def test_building_a_test_run_refuses_the_name(name: str):
    with pytest.raises(TestRunNameError, match="Test Run environment"):
        build_test_run("stress", seed=7, env={name: "1"})


@pytest.mark.parametrize("name", [*_HOSTILE, *_RESERVED])
def test_every_decoder_of_a_persisted_test_run_refuses_the_name(name: str):
    record = _record(name)
    item = {"work_item_id": "w", "test_runs": (record,)}
    manifest = SimpleNamespace(document={"work_items": (item,)})

    with pytest.raises(TestRunNameError, match="Test Run environment"):
        test_run_from_document(record)
    with pytest.raises(ValueError, match="Test Run environment"):
        _validate_test_runs([{**record, "grade": "pass"}])
    with pytest.raises(ValueError, match="Test Run environment"):
        _work_item_test_runs(item)
    with pytest.raises(ValueError, match="Test Run environment"):
        _manifest_test_runs(manifest)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="Test Run environment"):
        _manifest_run_groups(manifest)  # type: ignore[arg-type]
    unnamed = {"work_item_id": "w", "test_runs": (_record(name, test=None),)}
    with pytest.raises(ValueError, match="Test Run environment"):
        observation_test_runs(unnamed, ({"test": "found"},))


@pytest.mark.parametrize("name", ["X; id #", "BOOLEY_TARGET"])
def test_the_manifest_codec_refuses_the_name(tmp_path, name: str):
    from booley.flows.sim.campaign.codec import (
        canonical_json_bytes,
        decode_simulation_campaign_manifest,
    )
    from tests.flows.sim.test_campaign_test_runs import _v4_document

    document = _v4_document(tmp_path)
    run = document["work_items"][0]["test_runs"][0]
    run["env"] = {name: "1"}
    run["fingerprint"] = declared_inputs_fingerprint(tuple(run["args"]), run["env"])

    with pytest.raises(SimulationCampaignIntegrityError):
        decode_simulation_campaign_manifest(canonical_json_bytes(document))


@pytest.mark.parametrize("name", [*_HOSTILE])
def test_the_export_renderer_refuses_a_name_that_is_not_an_identifier(name: str):
    with pytest.raises(ValueError, match="simulator environment"):
        _export_line(name, "1")


@pytest.mark.parametrize("value", ["plain", "two words", "a;b", "$(id)", "it's", "", "x\ny"])
def test_a_valid_environment_renders_shell_safe_exports(value: str):
    line = _export_line("FAULT_MODE", value)

    assert shlex.split(line) == ["export", f"FAULT_MODE={value}"]
    shown = subprocess.run(
        ["sh", "-c", f'{line} && printf %s "$FAULT_MODE"'],
        capture_output=True,
        text=True,
        # MSYS sh spawns slowly on a loaded Windows runner; this only guards a hang.
        timeout=60 if os.name == "nt" else 10,
        check=True,
    )
    assert shown.stdout == value


def test_preview_of_a_valid_variant_environment_is_shell_safe(tmp_path):
    from booley.flows.sim.execution import SelectedTestRuns
    from tests.flows.sim.test_test_run_launch import _inspection, _preview, _project

    root = _project(
        tmp_path,
        '[sim]\ntests = ["stress"]\n[sim.variants.stress]\ndefault = "a"\n'
        'a = { env = { MODE = "x; touch pwned #", OTHER = "$(id)" } }\n',
    )
    run = build_test_run(
        "stress", variant="a", seed=1, env={"MODE": "x; touch pwned #", "OTHER": "$(id)"}
    )

    preview = _preview(root, _inspection("verilator"), SelectedTestRuns((run,)))
    steps = preview.commands[0][-1].split(" && ")

    exports = [step for step in steps if step.startswith("export MODE=") or "OTHER=" in step]
    assert [shlex.split(step) for step in exports] == [
        ["export", "MODE=x; touch pwned #"],
        ["export", "OTHER=$(id)"],
    ]
