"""Semantic deltas between two versions of one Target authoring surface."""

from __future__ import annotations

import pytest

from booley.targets.surface_diff import (
    ChangeSet,
    SurfaceDelta,
    SurfaceDiffError,
    TargetSurfaceFile,
    canonical_target_declaration,
    diff_surface,
    diff_surfaces,
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
    return diff_surface(
        TargetSurfaceFile(
            "toy.core",
            baseline.encode() if baseline is not None else None,
            current.encode() if current is not None else None,
        )
    )


def _only_change(delta: SurfaceDelta, target: str):
    assert [change.canonical for change in delta.target_changes] == [f"{_VLNV}#{target}"]
    return delta.target_changes[0]


def test_unchanged_surface_yields_an_empty_delta() -> None:
    assert _core_delta(_BASELINE) == SurfaceDelta()
    tests = b"[sim_smoke]\nmodule = 'test_toy'\n"
    assert diff_surface(TargetSurfaceFile("tests.toml", tests, tests)) == SurfaceDelta()


def test_whitespace_and_comment_edits_are_not_changes() -> None:
    current = "# reformatted\n" + _BASELINE.replace("[rtl, tb]", "[ rtl,  tb ]")
    assert _core_delta(current) == SurfaceDelta()


def test_target_added() -> None:
    delta = _core_delta(_BASELINE + "  lint_tb:\n    flow: lint\n    filesets: [tb]\n")

    assert [item.canonical for item in delta.targets.added] == [f"{_VLNV}#lint_tb"]
    assert delta.targets.modified == delta.targets.deleted == ()
    assert delta.target_changes == ()
    assert delta.filesets.added == delta.filesets.modified == ()


def test_target_removed() -> None:
    current = _BASELINE.split("  lint_rtl:\n", 1)[0]

    delta = _core_delta(current)

    assert [item.canonical for item in delta.targets.deleted] == [f"{_VLNV}#lint_rtl"]
    assert delta.targets.deleted[0].body["toplevel"] == "toy"
    assert delta.targets.added == delta.targets.modified == ()
    assert delta.target_changes == ()


def test_parameter_value_change() -> None:
    delta = _core_delta(_BASELINE.replace("[WIDTH=8]", "[WIDTH=16]"))

    assert [item.canonical for item in delta.targets.modified] == [f"{_VLNV}#sim_smoke"]
    change = _only_change(delta, "sim_smoke")
    assert change.changed_fields == ("parameters",)
    assert [(p.name, p.paramtype, p.before, p.after) for p in change.parameters] == [
        ("WIDTH", "vlogparam", ("WIDTH=8",), ("WIDTH=16",))
    ]
    assert change.toplevel is None
    assert change.filesets_added == change.filesets_removed == ()


def test_define_selection_change() -> None:
    delta = _core_delta(_BASELINE.replace("[WIDTH=8]", "[WIDTH=8, TRACE]"))

    change = _only_change(delta, "sim_smoke")
    assert [(p.name, p.paramtype, p.before, p.after) for p in change.parameters] == [
        ("TRACE", "vlogdefine", (), ("TRACE",))
    ]


def test_toplevel_change() -> None:
    delta = _core_delta(_BASELINE.replace("toplevel: toy\n", "toplevel: toy_wrapper\n"))

    change = _only_change(delta, "lint_rtl")
    assert change.changed_fields == ("toplevel",)
    assert change.toplevel is not None
    assert (change.toplevel.before, change.toplevel.after) == ("toy", "toy_wrapper")


def test_fileset_membership_change() -> None:
    delta = _core_delta(_BASELINE.replace("filesets: [rtl]\n", "filesets: [rtl, tb]\n"))

    change = _only_change(delta, "lint_rtl")
    assert change.changed_fields == ("filesets",)
    assert change.filesets_added == ("tb",)
    assert change.filesets_removed == ()
    assert delta.filesets == ChangeSet()


def test_referenced_fileset_definition_change() -> None:
    delta = _core_delta(_BASELINE.replace("files: [tb.sv]", "files: [tb.sv, tb_pkg.sv]"))

    # The Target bodies are unchanged, so only the input description names them.
    assert delta.targets == ChangeSet()
    assert [item.name for item in delta.filesets.modified] == ["tb"]
    change = _only_change(delta, "sim_smoke")
    assert change.changed_fields == ()
    assert [
        (item.before.body["files"], item.after.body["files"])
        for item in change.fileset_definitions
    ] == [(["tb.sv"], ["tb.sv", "tb_pkg.sv"])]


def test_referenced_parameter_definition_change() -> None:
    delta = _core_delta(_BASELINE.replace("default: 8", "default: 32"))

    assert [item.name for item in delta.parameters.modified] == ["WIDTH"]
    change = _only_change(delta, "sim_smoke")
    assert [
        (item.before.body["default"], item.after.body["default"])
        for item in change.parameter_definitions
    ] == [(8, 32)]


def test_deleted_fileset_and_parameter_declarations_are_described() -> None:
    current = _BASELINE.replace(
        "  tb:\n    files: [tb.sv]\n    file_type: systemVerilogSource\n", ""
    ).replace("  TRACE: {datatype: bool, paramtype: vlogdefine}\n", "")
    current = current.replace("filesets: [rtl, tb]", "filesets: [rtl]")

    delta = _core_delta(current)

    assert [item.key for item in delta.filesets.deleted] == ["toy.core#tb"]
    assert [item.key for item in delta.parameters.deleted] == ["toy.core#TRACE"]
    assert _only_change(delta, "sim_smoke").filesets_removed == ("tb",)


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
    assert delta.targets.added == delta.target_changes == ()


def _tests_delta(baseline: bytes | None, current: bytes | None) -> SurfaceDelta:
    return diff_surface(TargetSurfaceFile(".booley_project/tests.toml", baseline, current))


def test_tests_toml_entries_added_modified_and_removed() -> None:
    baseline = b"[sim_smoke]\nmodule = 'old'\n\n[sim_gone]\nmodule = 'gone'\n"
    current = b"[sim_smoke]\nmodule = 'new'\n\n['acme:lib:toy:1.0#sim_new']\nmodule = 'n'\n"

    delta = _tests_delta(baseline, current)

    assert delta.test_tables == ChangeSet(
        added=("acme:lib:toy:1.0#sim_new",), modified=("sim_smoke",), deleted=("sim_gone",)
    )
    smoke = delta.test_changes_for(f"{_VLNV}#sim_smoke")
    assert [(row.before, row.after) for row in smoke] == [({"module": "old"}, {"module": "new"})]
    assert [row.after for row in delta.test_changes_for(f"{_VLNV}#sim_new")] == [{"module": "n"}]
    assert [row.before for row in delta.test_changes_for(f"{_VLNV}#sim_gone")] == [
        {"module": "gone"}
    ]


def test_new_and_deleted_tests_toml_files() -> None:
    content = b"[sim_smoke]\nmodule = 'test_toy'\n"

    assert _tests_delta(None, content).test_tables == ChangeSet(added=("sim_smoke",))
    assert _tests_delta(content, None).test_tables == ChangeSet(deleted=("sim_smoke",))


def test_surfaces_merge_and_other_files_are_ignored() -> None:
    delta = diff_surfaces(
        (
            TargetSurfaceFile(
                "toy.core", _BASELINE.encode(), b"CAPI=2:\nname: acme:lib:toy:1.0\n"
            ),
            TargetSurfaceFile("tests.toml", None, b"[lint_rtl]\nmodule = 'x'\n"),
            TargetSurfaceFile("README.md", b"old", b"new"),
        )
    )

    assert [item.name for item in delta.targets.deleted] == ["lint_rtl", "sim_smoke"]
    assert delta.test_tables.added == ("lint_rtl",)


@pytest.mark.parametrize(
    ("path", "content", "message"),
    [
        ("bad.core", b"targets: [", r"cannot parse \.core bad\.core"),
        ("bad.core", b"- a list\n", r"\.core bad\.core is not a mapping"),
        ("bad.core", b"targets: {}\n", "has no valid name"),
        ("bad.core", b"name: a:b:c:1\ntargets: []\n", "mapping-valued targets"),
        ("bad.core", b"name: a:b:c:1\nfilesets: []\n", "mapping-valued filesets"),
        ("bad.core", b"name: a:b:c:1\ntargets: {t: {filesets: [x]}}\n", "undefined fileset"),
        ("tests.toml", b"[broken", r"cannot parse tests\.toml"),
        ("tests.toml", b"\xff", r"cannot parse tests\.toml"),
    ],
)
def test_unparseable_or_invalid_input_raises(path: str, content: bytes, message: str) -> None:
    with pytest.raises(SurfaceDiffError, match=message):
        diff_surface(TargetSurfaceFile(path, None, content))
    with pytest.raises(SurfaceDiffError, match=message):
        diff_surface(TargetSurfaceFile(path, content, None))


_REORDERED = """\
CAPI=2:
name: acme:lib:toy:1.0
targets:
  lint_rtl: {toplevel: toy, filesets: [rtl], flow: lint}
  sim_smoke:
    toplevel:   tb_toy   # same value
    parameters: [WIDTH=8]
    filesets: [rtl, tb]
    flow: sim
parameters:
  TRACE: {paramtype: vlogdefine, datatype: bool}
  WIDTH: {default: 8, paramtype: vlogparam, datatype: int}
filesets:
  tb: {file_type: systemVerilogSource, files: [tb.sv]}
  rtl: {file_type: systemVerilogSource, files: [toy.sv]}
"""


def _declaration(core: str, target: str = "sim_smoke", tests: bytes | None = None) -> str:
    return canonical_target_declaration(core.encode(), path="toy.core", target=target, tests=tests)


def test_canonical_declaration_ignores_key_order_and_whitespace() -> None:
    tests = b"[sim_smoke]\nmodule = 'm'\nseed = 1\n"
    reordered_tests = b"\n[sim_smoke]\nseed   = 1\nmodule = 'm'\n"

    assert _declaration(_BASELINE, tests=tests) == _declaration(_REORDERED, tests=reordered_tests)
    assert _declaration(_BASELINE) == _declaration(_REORDERED, f"{_VLNV}#sim_smoke")


@pytest.mark.parametrize(
    ("edit", "tests"),
    [
        (lambda text: text.replace("[WIDTH=8]", "[WIDTH=9]"), None),
        (lambda text: text.replace("toplevel: tb_toy", "toplevel: tb_other"), None),
        (lambda text: text.replace("files: [tb.sv]", "files: [tb2.sv]"), None),
        (lambda text: text.replace("default: 8", "default: 4"), None),
        (lambda text: text, b"[sim_smoke]\nmodule = 'm'\n"),
    ],
)
def test_canonical_declaration_tracks_semantic_edits(edit, tests: bytes | None) -> None:
    assert _declaration(edit(_BASELINE), tests=tests) != _declaration(_BASELINE)


def test_canonical_declaration_excludes_unselected_inputs() -> None:
    edited = _BASELINE.replace("files: [tb.sv]", "files: [tb2.sv]").replace(
        "  sim_smoke:\n", "  sim_smoke:\n    description: unrelated to lint\n"
    )

    assert _declaration(edited, "lint_rtl") == _declaration(_BASELINE, "lint_rtl")


def test_canonical_declaration_rejects_an_unknown_target() -> None:
    with pytest.raises(SurfaceDiffError, match="does not declare Target 'missing'"):
        _declaration(_BASELINE, "missing")
