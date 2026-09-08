"""Tests for compact, human-readable Booley Flow scope labels."""

from __future__ import annotations

import pytest

from booley.flows.display import format_flow_display_label, short_target_name
from booley.flows.fpga.flow import FpgaImplFlow
from booley.flows.lint.flow import LintFlow
from booley.flows.synth.flow import AsicSynthesizeFlow


@pytest.mark.parametrize(
    ("targets", "tests", "mode", "expected"),
    [
        (["::demo:core:0#sim_mul"], (), None, "target sim_mul"),
        (["sim_a", "sim_b"], (), None, "2 targets"),
        (["sim_a"], ["smoke"], None, "target sim_a · test smoke"),
        (["sim_a"], ["smoke", "edge"], None, "target sim_a · 2 tests"),
        (["sim_a", "sim_b"], ["smoke", "smoke"], None, "2 targets · test smoke"),
        (["sim_a", "sim_b"], None, None, "2 targets · tests"),
        (["sim_a"], (), "elaboration", "target sim_a · elaboration"),
        ([], (), None, None),
    ],
)
def test_format_flow_display_label(targets, tests, mode, expected):
    assert format_flow_display_label(targets, tests=tests, mode=mode) == expected


def test_short_target_name_preserves_unicode_and_strips_qualification():
    assert short_target_name("::core:0#sim_λ") == "sim_λ"


@pytest.mark.parametrize("flow_type", [LintFlow, AsicSynthesizeFlow, FpgaImplFlow])
@pytest.mark.parametrize(
    ("target", "expected"),
    [
        ("::lib:core:0#default", "target default"),
        ("::lib:core:0#small,::lib:core:0#large", "2 targets"),
    ],
)
def test_target_aware_flows_share_human_target_labels(flow_type, target, expected):
    flow = flow_type()
    flow.parse_args(["--target", target])

    assert flow._resolve_display_label() == expected
