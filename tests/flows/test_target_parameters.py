"""Declared parameter values survive transport without expression guessing."""

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


def test_synthesis_flow_carries_types_without_changing_numeric_arguments():
    from types import SimpleNamespace

    from booley.flows.synth.flow import AsicSynthesizeFlow

    resolved = SimpleNamespace(
        parameters={
            "N": {"datatype": "int", "paramtype": "vlogparam", "default": 12},
            "B": {"datatype": "bool", "paramtype": "vlogparam", "default": True},
            "R": {"datatype": "real", "paramtype": "vlogparam", "default": 1.25},
            "NAME": {"datatype": "str", "paramtype": "vlogparam", "default": ' a"b\\c '},
            "FILE": {"datatype": "file", "paramtype": "vlogparam", "default": "a.mem"},
        }
    )
    command = []
    AsicSynthesizeFlow._append_typed_param_args(None, command, resolved, "synth", "top")
    assert command == [
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
