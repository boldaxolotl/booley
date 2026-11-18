from __future__ import annotations

import dataclasses
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from booley.flows.sim.execution.contract import SimulationPreview
from booley.flows.sim.record.codec import SimulationRecordIntegrityError
from booley.flows.sim.record.flow_planning import plan_ordinary_hdl_simulation_record
from booley.targets.domain import TargetHandle, TargetInput, TargetInspection
from tests.conftest import symlink_or_skip


def test_resolved_target_plans_one_private_serial_item_per_exact_test(
    tmp_path: Path,
) -> None:
    project, handle, inspection, preview = _planning_fixture(tmp_path)
    document = _plan(handle, inspection, preview, ("reset", "count"))
    assert "build_timeout" not in repr(document)
    assert [item["selection"]["names"] for item in document["work_items"]] == [  # type: ignore[index]
        ("reset",),
        ("count",),
    ]
    assert document["required_suite"]["names"] == ("reset", "count")  # type: ignore[index]
    assert document["workload"]["run_cwd"]["configured"] == "."  # type: ignore[index]
    assert document["workload"]["eda"]["kind"] == "icarus"  # type: ignore[index]
    assert all(item["kind"] == "ordinary_hdl" for item in document["work_items"])  # type: ignore[union-attr]
    catalog_empty = _plan(handle, inspection, preview, (), catalog_backed=True)
    assert catalog_empty["required_suite"]["names"] == ()  # type: ignore[index]
    assert catalog_empty["required_suite"]["default_invocation"] is False  # type: ignore[index]
    assert catalog_empty["required_suite"]["source_path"] == ".booley/tests.toml"  # type: ignore[index]
    (project / "booley.toml").write_text(
        '[flows.sim]\nrun_cwd = "."\npre_sim_build_access = "legacy-per-test"\n',
        encoding="utf-8",
    )
    private = _plan(handle, inspection, preview, ("reset", "count"))
    assert private["build_variants"][0]["sharing_eligible"] is False  # type: ignore[index]


def test_invalid_run_cwd_template_is_refused_before_a_manifest_exists(tmp_path: Path) -> None:
    """Planning rejects a bad template; no plan means nothing can be published."""
    project, handle, inspection, preview = _planning_fixture(tmp_path)
    (project / "booley.toml").write_text(
        '[flows.sim]\nrun_cwd = "runs/{bogus}"\n', encoding="utf-8"
    )

    with pytest.raises(ValueError, match=r"invalid \[flows\.sim\]\.run_cwd template"):
        _plan(handle, inspection, preview, ("reset", "count"))


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ('"make -C tests build"', r"\[flows\.sim\]\.pre_run_commands must be a list of strings"),
        ('["make", 3]', r"\[flows\.sim\]\.pre_run_commands must be a list of strings"),
        ('["make", ""]', r"pre_run_commands must be a list of strings, none empty"),
    ],
    ids=["bare-string", "non-string-entry", "empty-entry"],
)
def test_invalid_pre_run_commands_are_refused_before_a_manifest_exists(
    tmp_path: Path, value: str, message: str
) -> None:
    """Planning rejects a knob that is not a list of strings; nothing is frozen."""
    project, handle, inspection, preview = _planning_fixture(tmp_path)
    (project / "booley.toml").write_text(
        f"[flows.sim]\npre_run_commands = {value}\n", encoding="utf-8"
    )

    with pytest.raises(ValueError, match=message):
        _plan(handle, inspection, preview, ("reset", "count"))


def _planning_fixture(tmp_path: Path):
    project = tmp_path / ".booley"
    project.mkdir()
    (project / "booley.toml").write_text("[flows.sim]\nrun_cwd = '.'\n", encoding="utf-8")
    (project / "tests.toml").write_text('[sim]\ntests = ["reset", "count"]\n', encoding="utf-8")
    source = tmp_path / "tb.sv"
    source.write_text("module tb; endmodule\n", encoding="utf-8")
    handle = cast(
        TargetHandle,
        SimpleNamespace(
            identity="acme:lib:dut:1#sim",
            selector="sim",
            name="sim",
            vlnv="acme:lib:dut:1",
            project_root=tmp_path.resolve(),
            flow=None,
            eda_tool=None,
        ),
    )
    inputs = (TargetInput("tb.sv", "acme:lib:dut:1", "systemVerilogSource", ("tb",), False, {}),)
    inspection = TargetInspection(
        handle,
        "tb",
        "sim",
        "icarus",
        {},
        {},
        inputs,
    )
    preview = SimulationPreview(
        commands=(("run", "reset"), ("run", "count")),
        groups=(("reset",), ("count",)),
        target_identity=handle.identity,
        toplevel="tb",
        eda_tool="icarus",
        sources=("tb.sv",),
        constraints=(),
        parameters={},
        flow_options={},
    )

    return project, handle, inspection, preview


def _plan(handle, inspection, preview, required, *, catalog_backed=None):
    return plan_ordinary_hdl_simulation_record(
        handle=handle,
        inspection=inspection,
        preview=preview,
        groups=preview.groups,
        required_suite=required,
        required_suite_catalog_backed=catalog_backed,
        revision="abc123",
        invocation_id=1,
        execution_id="",
        trace=False,
    ).manifest.document


def test_source_recipe_ignores_flow_options_defines(tmp_path):
    """No Flow applies `flow_options.defines`, so the recipe must not record it."""
    _project, handle, inspection, preview = _planning_fixture(tmp_path)
    baseline = _plan(handle, inspection, preview, ("reset", "count"))
    baseline_recipe = baseline["workload"]["source_recipe"]
    assert baseline_recipe["defines"] == ()
    baseline_command = baseline["workload"]["build_recipe"]["command_model_sha256"]
    for shape in ({"A": 1}, ["A", "B=2"], "A"):
        planned = dataclasses.replace(inspection, flow_options={"defines": shape})
        document = _plan(handle, planned, preview, ("reset", "count"))
        assert document["workload"]["source_recipe"] == baseline_recipe
        assert document["workload"]["source_recipe"]["defines"] == ()
        # The opaque whole-mapping identity still covers every flow_options key.
        assert document["workload"]["build_recipe"]["command_model_sha256"] != baseline_command


def test_empty_target_toplevel_is_refused_at_planning(tmp_path):
    _project, handle, inspection, preview = _planning_fixture(tmp_path)
    with pytest.raises(SimulationRecordIntegrityError, match="empty toplevel; set toplevel"):
        _plan(handle, dataclasses.replace(inspection, toplevel=""), preview, ("reset", "count"))


def test_copyto_destination_outside_the_run_directory_is_refused_at_planning(tmp_path):
    _project, handle, inspection, preview = _planning_fixture(tmp_path)
    (tmp_path / "fw.hex").write_text("00\n", encoding="utf-8")
    staged = TargetInput("fw.hex", "acme:lib:dut:1", "user", (), False, {"copyto": "C:fw.hex"})
    planned = dataclasses.replace(inspection, inputs=(*inspection.inputs, staged))
    with pytest.raises(SimulationRecordIntegrityError, match=r"copyto destination 'C:fw\.hex'"):
        _plan(handle, planned, preview, ("reset", "count"))


@pytest.mark.parametrize("linked", ["entry", "below"])
def test_target_input_under_symlinked_booley_dir_is_project_relative(
    tmp_path: Path, linked: str
) -> None:
    """#2191: a stealth-core input below the linked entry is project-relative.

    An input reached through a link *below* the entry that points outside the
    Project Root still escapes it.
    """
    root = tmp_path / "root"
    root.mkdir()
    _project, handle, inspection, preview = _planning_fixture(root)
    entry = root / ".booley"
    external = tmp_path / "external"
    entry.rename(external)
    symlink_or_skip(entry, external, target_is_directory=True)
    if linked == "entry":
        directory = external / "cores"
    else:
        directory = tmp_path / "outside"
        symlink_or_skip(external / "cores", directory, target_is_directory=True)
    directory.mkdir(exist_ok=True)
    (directory / "dut.sv").write_text("module dut; endmodule\n", encoding="utf-8")
    # The Target reports the input by its lexical spelling through the entry.
    lexical = entry / "cores" / "dut.sv"
    inputs = (
        TargetInput(str(lexical), "acme:lib:dut:1", "systemVerilogSource", ("tb",), False, {}),
    )
    planned = dataclasses.replace(inspection, inputs=inputs)

    if linked == "below":
        with pytest.raises(SimulationRecordIntegrityError, match="escapes Project Root"):
            _plan(handle, planned, preview, ("reset", "count"))
        return
    document = _plan(handle, planned, preview, ("reset", "count"))
    closure = document["build_variants"][0]["source_closure"]  # type: ignore[index]
    assert [entry_["path"] for entry_ in closure] == [".booley/cores/dut.sv"]


_VARIANTS = (
    '[sim.variants.reset]\ndefault = "a"\n'
    'b = { args = ["+n=2"], env = { Y = "2", X = "1" } }\na = ["+n=1"]\n'
)
_SECTION = '[sim]\ntests = ["reset", "count"]\n'


def _target_tests(handle, inspection, preview, tmp_path: Path, text: str):
    (tmp_path / ".booley" / "tests.toml").write_bytes(text.encode())
    return _plan(handle, inspection, preview, ("reset", "count"))["required_suite"][  # type: ignore[index]
        "target_tests"
    ]


def test_target_tests_fingerprint_only_the_targets_own_resolved_section(tmp_path: Path) -> None:
    _, handle, inspection, preview = _planning_fixture(tmp_path)

    def fingerprint(text: str):
        return _target_tests(handle, inspection, preview, tmp_path, text)

    base = fingerprint(_SECTION + _VARIANTS)
    assert [item["name"] for item in base["tests"]] == ["reset", "count"]
    assert base["settings"] == {"select": None, "env": None, "seed": None, "default_seed": None}
    reordered = _VARIANTS.replace(
        'b = { args = ["+n=2"], env = { Y = "2", X = "1" } }\na = ["+n=1"]\n',
        'a = ["+n=1"]\nb = { args = ["+n=2"], env = { X = "1", Y = "2" } }\n',
    )
    unrelated = '# note\n[other]\ntests = ["x"]\n[test_lists]\nunused = ["y"]\n'
    assert fingerprint(unrelated + _SECTION + reordered) == base
    skipped = fingerprint(_SECTION + 'skip = ["count"]\n' + _VARIANTS)
    assert [item["sha256"] for item in skipped["tests"]] != [
        item["sha256"] for item in base["tests"]
    ]
    assert skipped["tests"][0] == base["tests"][0]
    seeded = fingerprint(_SECTION + 'seed = "+seed={seed}"\ndefault_seed = 3\n' + _VARIANTS)
    assert seeded["tests"] == base["tests"]
    assert seeded["settings"]["seed"] == "+seed={seed}"
    assert seeded["settings"]["default_seed"] == 3


def test_malformed_tests_toml_is_refused_naming_the_cause(tmp_path: Path) -> None:
    _, handle, inspection, preview = _planning_fixture(tmp_path)
    with pytest.raises(SimulationRecordIntegrityError, match=r"cannot read the Target's Tests"):
        _target_tests(handle, inspection, preview, tmp_path, "[sim\n")
