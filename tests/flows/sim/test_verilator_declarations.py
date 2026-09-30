"""Pinned early-tree contract and conservative source attribution regressions."""

import json
from dataclasses import replace

import pytest

from booley.flows.sim.verilator_declarations import (
    DeclarationSource,
    decode_declarations,
    possible_logical_locations,
    resolve_declaration_source,
)
from booley.flows.sim.verilator_identity import PINNED_VERILATOR


def _evidence(tmp_path, *, filename="rtl/unused.sv", kind="MODULE"):
    tree, meta = tmp_path / "Custom_023_cells.tree.json", tmp_path / "Custom.tree.meta.json"
    tree.write_text(
        json.dumps(
            {
                "type": "NETLIST",
                "modulesp": [
                    {"type": kind, "name": "unused", "loc": "e,2:8,2:14"},
                    {"type": "PACKAGE", "name": "std", "loc": "d,35:9,35:12"},
                ],
            }
        )
    )
    meta.write_text(
        json.dumps(
            {
                "files": {
                    "e": {"filename": filename, "realpath": filename, "language": "1800-2023"},
                    "d": {
                        "filename": "<verilated_std>",
                        "realpath": "/usr/local/share/verilator/include/verilated_std.sv",
                    },
                }
            }
        )
    )
    return tree, meta


def _source(path="rtl/unused.sv", aliases=None):
    return DeclarationSource(
        path,
        aliases or (path, "src/core/unused.sv"),
        "sha256:" + "a" * 64,
        "systemVerilogSource",
        False,
        False,
    )


def _decode(paths, sources):
    return decode_declarations(
        *paths,
        compiler=(PINNED_VERILATOR.tag, PINNED_VERILATOR.commit),
        sources=sources,
        build_identity="generation:1",
    )


@pytest.mark.parametrize("kind", ["MODULE", "IFACE", "PACKAGE"])
def test_decodes_declaration_kinds_and_excludes_builtins(tmp_path, kind):
    inventory = _decode(_evidence(tmp_path, kind=kind), (_source(),))
    assert inventory.status == "complete"
    assert len(inventory.declarations) == 1
    declaration = inventory.declarations[0]
    assert (declaration.kind, declaration.source, declaration.line, declaration.column) == (
        kind,
        "rtl/unused.sv",
        2,
        8,
    )
    assert inventory.document()["raw_evidence"][0]["sha256"].startswith("sha256:")


def test_staged_aliases_are_shared_and_collision_is_incomplete(tmp_path):
    paths = _evidence(tmp_path, filename="src/core/unused.sv")
    source = _source()
    assert _decode(paths, (source,)).status == "complete"
    assert resolve_declaration_source((source,), "src/core/unused.sv") == source
    assert _decode(paths, (source, replace(source, path="other.sv"))).status == "incomplete"
    assert resolve_declaration_source((source,), "foreign/src/core/unused.sv") is None


@pytest.mark.parametrize(
    "failure",
    [
        "missing",
        "malformed",
        "bad_root",
        "bad_location",
        "unknown_file",
        "unknown_kind",
        "symlink",
        "deep",
    ],
)
def test_expected_evidence_failures_are_advisory(tmp_path, failure):
    paths = _evidence(tmp_path)
    tree, meta = paths
    document = json.loads(tree.read_text())
    if failure == "missing":
        meta.unlink()
    elif failure == "malformed":
        tree.write_text("{")
    elif failure == "symlink":
        original = tree.rename(tmp_path / "original")
        tree.symlink_to(original)
    elif failure == "deep":
        tree.write_text("[" * 140 + "0" + "]" * 140)
    else:
        if failure == "bad_root":
            document["type"] = "OTHER"
        elif failure == "bad_location":
            document["modulesp"][0]["loc"] = "e,false:1"
        elif failure == "unknown_file":
            document["modulesp"][0]["loc"] = "z,2:8,2:14"
        elif failure == "unknown_kind":
            document["modulesp"][0]["type"] = "OTHER"
        tree.write_text(json.dumps(document))
    inventory = _decode(paths, (_source(),))
    assert inventory.status == "incomplete"
    assert inventory.diagnostics


@pytest.mark.parametrize(
    "data",
    [
        b'`line 100 "used.sv" 0',
        b'  `line\t100 "logical.sv" 0',
        b'`define LOC `line 100 "used.sv" 0',
        b"`define JOIN li``ne",
        b"`define NAME li\\\nne",
        b"// `line decoy",
        b'"`line decoy"',
    ],
)
def test_logical_source_safety_trigger_covers_literal_and_macro_constructions(data):
    assert possible_logical_locations(data)


def test_comment_module_decoys_do_not_trigger_location_safety():
    assert not possible_logical_locations(b'// module decoy;\nstring s = "module decoy";')
