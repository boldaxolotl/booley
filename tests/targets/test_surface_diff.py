"""Semantic deltas between two versions of one Target authoring surface."""

from __future__ import annotations

import pytest

from booley.targets.surface_diff import (
    ChangeSet,
    SurfaceDelta,
    SurfaceDiffError,
    TargetSurfaceFile,
    diff_core_surface,
    diff_tests_surface,
    merge_deltas,
)

_VLNV = "acme:lib:toy:1.0"
_BASELINE = """\
CAPI=2:
name: acme:lib:toy:1.0
filesets:
  rtl:
    files: [toy.sv]
    file_type: systemVerilogSource
  tb:
    files: [tb.sv]
    file_type: systemVerilogSource
parameters:
  WIDTH: {datatype: int, paramtype: vlogparam, default: 8}
  TRACE: {datatype: bool, paramtype: vlogdefine}
targets:
  sim_smoke:
    flow: sim
    filesets: [rtl, tb]
    parameters: [WIDTH=8]
    toplevel: tb_toy
  lint_rtl:
    flow: lint
    filesets: [rtl]
    toplevel: toy
"""


def _core_delta(current: str | None, baseline: str | None = _BASELINE) -> SurfaceDelta:
    return diff_core_surface(
        TargetSurfaceFile(
            "toy.core",
            baseline.encode() if baseline is not None else None,
            current.encode() if current is not None else None,
        )
    )


def _tests_delta(baseline: bytes | None, current: bytes | None) -> SurfaceDelta:
    return diff_tests_surface(TargetSurfaceFile(".booley_project/tests.toml", baseline, current))


def test_unchanged_surface_yields_an_empty_delta() -> None:
    assert _core_delta(_BASELINE) == SurfaceDelta()
    tests = b"[sim_smoke]\nmodule = 'test_toy'\n"
    assert _tests_delta(tests, tests) == SurfaceDelta()


def test_whitespace_and_comment_edits_are_not_changes() -> None:
    current = "# reformatted\n" + _BASELINE.replace("[rtl, tb]", "[ rtl,  tb ]")
    assert _core_delta(current) == SurfaceDelta()


def test_target_added() -> None:
    delta = _core_delta(_BASELINE + "  lint_tb:\n    flow: lint\n    filesets: [tb]\n")

    assert [item.canonical for item in delta.targets.added] == [f"{_VLNV}#lint_tb"]
    assert delta.targets.modified == delta.targets.deleted == ()
    assert delta.filesets == ChangeSet()


def test_target_deleted() -> None:
    delta = _core_delta(_BASELINE.split("  lint_rtl:\n", 1)[0])

    assert [item.canonical for item in delta.targets.deleted] == [f"{_VLNV}#lint_rtl"]
    assert delta.targets.deleted[0].body["toplevel"] == "toy"
    assert delta.targets.added == delta.targets.modified == ()


def test_target_modified_carries_current_body() -> None:
    delta = _core_delta(_BASELINE.replace("[WIDTH=8]", "[WIDTH=16]"))

    assert [item.canonical for item in delta.targets.modified] == [f"{_VLNV}#sim_smoke"]
    assert delta.targets.modified[0].body["parameters"] == ["WIDTH=16"]


def test_fileset_added_modified_and_deleted() -> None:
    current = (
        _BASELINE.replace("files: [tb.sv]", "files: [tb.sv, tb_pkg.sv]")
        .replace("  rtl:\n    files: [toy.sv]\n", "  gates:\n    files: [toy_gates.v]\n")
        .replace("[rtl, tb]", "[gates, tb]")
        .replace("filesets: [rtl]\n", "filesets: [gates]\n")
    )

    delta = _core_delta(current)

    assert [item.key for item in delta.filesets.added] == ["toy.core#gates"]
    assert delta.filesets.added[0].referenced_by == (f"{_VLNV}#lint_rtl", f"{_VLNV}#sim_smoke")
    assert [item.body["files"] for item in delta.filesets.modified] == [["tb.sv", "tb_pkg.sv"]]
    assert [item.name for item in delta.filesets.deleted] == ["rtl"]


def test_parameter_added_modified_and_deleted() -> None:
    current = (
        _BASELINE.replace("default: 8", "default: 32")
        .replace(
            "  TRACE: {datatype: bool, paramtype: vlogdefine}\n", "  DEPTH: {datatype: int}\n"
        )
        .replace("[WIDTH=8]", "[WIDTH=8, DEPTH=4]")
    )

    delta = _core_delta(current)

    assert [item.name for item in delta.parameters.added] == ["DEPTH"]
    assert delta.parameters.added[0].referenced_by == (f"{_VLNV}#sim_smoke",)
    assert [item.body["default"] for item in delta.parameters.modified] == [32]
    assert [item.key for item in delta.parameters.deleted] == ["toy.core#TRACE"]


def test_new_core_file_has_no_baseline() -> None:
    delta = _core_delta(_BASELINE, baseline=None)

    assert [item.name for item in delta.targets.added] == ["lint_rtl", "sim_smoke"]
    assert [item.name for item in delta.filesets.added] == ["rtl", "tb"]
    assert delta.filesets.added[1].referenced_by == (f"{_VLNV}#sim_smoke",)
    assert [item.name for item in delta.parameters.added] == ["TRACE", "WIDTH"]


def test_deleted_core_file_has_no_current() -> None:
    delta = _core_delta(None)

    assert [item.name for item in delta.targets.deleted] == ["lint_rtl", "sim_smoke"]
    assert [item.name for item in delta.filesets.deleted] == ["rtl", "tb"]
    assert [item.name for item in delta.parameters.deleted] == ["TRACE", "WIDTH"]


def test_tests_toml_tables_added_modified_and_deleted() -> None:
    baseline = b"[sim_smoke]\nmodule = 'old'\n\n[sim_gone]\nmodule = 'gone'\n"
    current = b"[sim_smoke]\nmodule = 'new'\n\n['acme:lib:toy:1.0#sim_new']\nmodule = 'n'\n"

    assert _tests_delta(baseline, current).test_tables == ChangeSet(
        added=("acme:lib:toy:1.0#sim_new",), modified=("sim_smoke",), deleted=("sim_gone",)
    )


def test_new_and_deleted_tests_toml_files() -> None:
    content = b"[sim_smoke]\nmodule = 'test_toy'\n"

    assert _tests_delta(None, content).test_tables == ChangeSet(added=("sim_smoke",))
    assert _tests_delta(content, None).test_tables == ChangeSet(deleted=("sim_smoke",))


def test_merged_deltas_are_sorted_across_files() -> None:
    other = _BASELINE.replace("acme:lib:toy:1.0", "acme:lib:aaa:1.0")
    delta = merge_deltas(
        (
            _core_delta(None),
            diff_core_surface(TargetSurfaceFile("aaa.core", None, other.encode())),
            _tests_delta(None, b"[b]\nx = 1\n\n[a]\nx = 1\n"),
        )
    )

    assert [item.canonical for item in delta.targets.deleted] == [
        f"{_VLNV}#lint_rtl",
        f"{_VLNV}#sim_smoke",
    ]
    assert [item.key for item in delta.filesets.added] == ["aaa.core#rtl", "aaa.core#tb"]
    assert delta.test_tables.added == ("a", "b")


@pytest.mark.parametrize(
    ("content", "message"),
    [
        (b"targets: [", r"cannot parse \.core bad\.core"),
        (b"- a list\n", r"\.core bad\.core is not a mapping"),
        (b"targets: {}\n", "has no valid name"),
        (b"name: a:b:c:1\ntargets: []\n", "mapping-valued targets"),
        (b"name: a:b:c:1\ntargets: {1: {}}\n", "non-string Target name"),
        (b"name: a:b:c:1\nfilesets: []\n", "mapping-valued filesets"),
        (b"name: a:b:c:1\nparameters: 5\n", "mapping-valued parameters"),
        (b"name: a:b:c:1\ntargets: {t: {filesets: [x]}}\n", "undefined fileset"),
    ],
)
def test_unparseable_or_invalid_core_raises(content: bytes, message: str) -> None:
    for baseline, current in ((None, content), (content, None)):
        with pytest.raises(SurfaceDiffError, match=message):
            diff_core_surface(TargetSurfaceFile("bad.core", baseline, current))


@pytest.mark.parametrize("content", [b"[broken", b"\xff"])
def test_unparseable_tests_toml_raises(content: bytes) -> None:
    for baseline, current in ((None, content), (content, None)):
        with pytest.raises(SurfaceDiffError, match=r"cannot parse tests\.toml"):
            diff_tests_surface(TargetSurfaceFile("tests.toml", baseline, current))
