"""Declared parameter values survive transport without expression guessing."""

import json

import pytest

from booley.flows import target_parameters as parameters


def test_declared_types_and_raw_values():
    specs = {
        "NAME": {"paramtype": "vlogparam", "datatype": "str", "default": " 123 "},
        "FILE": {"paramtype": "vlogparam", "datatype": "file", "default": "../a b"},
        "BOOL": {"paramtype": "vlogparam", "datatype": "bool", "default": True},
        "TEXT_BOOL": {"paramtype": "vlogparam", "datatype": "str", "default": True},
    }
    values = parameters.vlogparam_specs(specs)
    assert values["NAME"].value == " 123 "
    assert values["FILE"].datatype == "file"
    assert values["BOOL"].value == "1"
    assert values["TEXT_BOOL"].value == "True"


@pytest.mark.parametrize(
    ("value", "literal", "packed"),
    [
        ("", '""', '""'),
        ("MAC", '"\\115\\101\\103"', "24'h4d4143"),
        ('a" b', '"\\141\\042\\040\\142"', "32'h61222062"),
        ("é", '"\\303\\251"', "16'hc3a9"),
    ],
)
def test_receiver_literals(value, literal, packed):
    assert parameters.sv_byte_string_literal(value) == literal
    assert parameters.packed_string_constant(value) == packed


def test_macro_contract_remains_raw_tokens():
    assert parameters.vlogdefine_args(
        {"M": {"datatype": "str", "paramtype": "vlogdefine", "default": "a b"}}
    ) == ["M=a b"]


def test_synthesis_flow_carries_types_without_changing_numeric_arguments(
    tmp_path, monkeypatch, capsys
):
    from booley.flows.synth.flow import AsicSynthesizeFlow
    from booley.runtime import runtime_context

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(runtime_context, "inside_session_runtime", lambda: True)
    monkeypatch.setenv("BOOLEY_RUNTIME_DIR", str(tmp_path / "runtime"))
    (tmp_path / ".booley").mkdir()
    (tmp_path / ".booley/booley.toml").write_bytes(b"")
    params = "".join(
        f"  {name}:\n    datatype: {datatype}\n    paramtype: vlogparam\n    default: {default}\n"
        for name, datatype, default in (
            ("N", "int", "12"),
            ("B", "bool", "true"),
            ("R", "real", "1.25"),
            ("NAME", "str", "' a\"b\\c '"),
            ("FILE", "file", "a.mem"),
        )
    )
    core = (
        "CAPI=2:\nname: acme:lib:demo:1\nparameters:\n" + params + "targets:\n"
        "  synth:\n    flow: generic\n"
        "    flow_options: {tool: yosys, synth_mode: logical, arch: xilinx}\n"
        "    filesets: []\n    toplevel: top\n    parameters: [N, B, R, NAME, FILE]\n"
    )
    (tmp_path / "demo.core").write_bytes(core.encode())
    args = ["--work-dir", str(tmp_path), "--target", "synth", "--dry-run"]
    assert AsicSynthesizeFlow().execute_cli(args).exit_code == 0
    plan, _ = json.JSONDecoder().raw_decode(capsys.readouterr().out)
    argv = [a for unit in plan["work_units"] for c in unit["commands"] for a in c["argv"]]
    assert argv[argv.index("-p") :][:14] == [
        "-p",
        "N=12",
        "-p",
        "B=1",
        "-p",
        "R=1.25",
        "-p",
        'NAME= a"b\\c ',
        "--param-datatype",
        "NAME=str",
        "-p",
        "FILE=a.mem",
        "--param-datatype",
        "FILE=file",
    ]
