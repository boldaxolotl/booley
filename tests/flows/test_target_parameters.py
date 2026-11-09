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
        ("MAC", '"MAC"', "24'h4d4143"),
        ('a" b', '"a\\" b"', "32'h61222062"),
        ("a\\b", '"a\\\\b"', "24'h615c62"),
        ("\n\t\r", '"\\n\\t\\r"', "24'h0a090d"),
        ("é漢", '"é漢"', "40'hc3a9e6bca2"),
    ],
)
def test_receiver_literals(value, literal, packed):
    assert parameters.sv_string_literal(value) == literal
    assert parameters.packed_string_constant(value) == packed


@pytest.mark.parametrize("value", ["a\x00b", "\ud800"])
def test_invalid_text_rejected(value):
    with pytest.raises(ValueError):
        parameters.sv_string_literal(value)


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
