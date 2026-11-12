"""The Run wizard's form model (#3580): generated from each Flow's human parser.

The drift tests build the real parsers of every built-in Flow, so a Flow that
gains an option fails here until the option is shown or routed to extra
arguments. The module under test is imported in each test body, so a Dashboard
without it fails there and not at collection.
"""

from __future__ import annotations

import argparse

import pytest

from booley.mcp.registry import discover_mcp_tools

FLOW_NAMES = sorted(info.name for info in discover_mcp_tools() if info.kind == "flow")
# Never offered: the help action, the Project selector, hidden compatibility aliases.
NEVER_OFFERED = {"-h", "--help", "-C", "--project"}
HIDDEN_ALIASES = {"--work-dir", "--timeout-ms", "--elab-only", "--build-only", "--standalone"}


def parser_for(flow: str) -> argparse.ArgumentParser:
    """The human parser ``booley flow <flow>`` builds, as the form reads it."""
    from booley.flows.builtin_cli import build_cli_parser
    from booley.flows.cli_selection import CliInvocation
    from booley.harness.dashboard.launch import _load_flow

    return build_cli_parser(_load_flow(flow), CliInvocation())


# --- AC2: the drift tests -------------------------------------------------------


def test_every_offered_option_is_placed():
    for flow in FLOW_NAMES:
        assert_every_offered_option_is_placed(flow)


def assert_every_offered_option_is_placed(flow):
    from booley.flows.cli_help import option_groups
    from booley.harness.dashboard import run_form

    parser = parser_for(flow)
    form = run_form.describe(parser)  # raises FormError for an unplaced group or option
    visible = {
        spelling
        for _title, actions in option_groups(parser)
        for action in actions
        if action.help != argparse.SUPPRESS
        for spelling in action.option_strings
    }
    assert form.placed() == visible - NEVER_OFFERED
    assert form.placed().isdisjoint(HIDDEN_ALIASES | NEVER_OFFERED)
    assert form.fields, "every Flow has Common fields at least"


def test_unknown_group_fails_until_placed(monkeypatch):
    from booley.harness.dashboard import run_form

    parser = parser_for("lint")
    parser.add_argument_group("Brand new").add_argument("--x")
    with pytest.raises(run_form.FormError) as failure:
        run_form.describe(parser)
    assert "Brand new" in str(failure.value)
    assert "--x" in str(failure.value)
    monkeypatch.setitem(run_form.GROUP_PLACEMENT, "Brand new", "extra")
    assert "--x" in run_form.describe(parser).placed()


def test_unrenderable_option_in_a_field_group_fails():
    from booley.harness.dashboard import run_form

    for kind in (
        {"flags": ("--many",), "nargs": "+"},
        {"flags": ("--count",), "action": "count"},
        {"flags": ("--only-off",), "action": "store_false"},
        {"flags": ("path",)},  # a positional has no option spelling to write
    ):
        parser = parser_for("lint")
        flags, rest = kind["flags"], {k: v for k, v in kind.items() if k != "flags"}
        parser.add_argument_group("Lint options").add_argument(*flags, **rest)
        with pytest.raises(run_form.FormError) as failure:
            run_form.describe(parser)
        assert flags[0] in str(failure.value)
        assert "Lint options" in str(failure.value)


def test_extra_group_takes_any_kind():
    from booley.harness.dashboard import run_form

    parser = parser_for("lint")
    parser.add_argument_group("Output").add_argument("--many", nargs="+")
    form = run_form.describe(parser)
    assert "--many" in form.placed()
    assert "--many" in form.extra_hint()
    assert all("--many" not in field.spellings for field in form.fields)


def test_every_spelling_counts_as_placed():
    from booley.harness.dashboard import run_form

    form = run_form.describe(parser_for("sim"))
    coverage = [field for field in form.fields if "--coverage" in field.spellings]
    assert len(coverage) == 1
    assert coverage[0].spellings == ("--coverage", "--cov")
    assert coverage[0].label == "--coverage"
    assert "--cov" in form.placed()
    assert not any(field.label == "--cov" for field in form.fields)


# --- what the form offers -------------------------------------------------------


def test_the_form_follows_the_placement_rule():
    from booley.harness.dashboard import run_form

    synth = run_form.describe(parser_for("synth"))
    labels = {field.label: field for field in synth.fields}
    assert labels["--target"].kind == "text"
    assert labels["--target"].empty == "automatic"
    assert labels["--dry-run"].kind == "toggle"
    assert labels["--flatten/--no-flatten"].kind == "three-way"
    assert labels["--frontend"].choices == ("sv2v", "slang")
    assert "--timeout" in labels
    assert "--report-dir" not in labels  # Output is extra arguments
    assert "--repair-hold/--no-repair-hold" in synth.extra_hint()
    assert "-q/--quiet" in synth.extra_hint()
    assert "--report-dir" in synth.extra_hint()
    assert "--abc-recipe" in synth.extra_hint()
    # Hidden aliases of --timeout never reach the form.
    assert "--timeout-ms" not in synth.placed()
    assert [field.dest for field in synth.fields].count("timeout_ms") == 1


def test_choice_spellings_are_what_the_human_types():
    from booley.harness.dashboard import run_form

    sim = {field.label: field for field in run_form.describe(parser_for("sim")).fields}
    # The --help metavar spells the choices with hyphens; the parser takes those.
    assert sim["--mode"].choices == ("simulate", "elab-only", "elab-only-standalone")
    fpga = {field.label: field for field in run_form.describe(parser_for("fpga")).fields}
    # No metavar: str(choice), which is what its parser accepts (no hyphen).
    assert fpga["--ppa-profile"].choices == ("compact", "balanced", "max_frequency")


@pytest.mark.parametrize("flow", FLOW_NAMES)
def test_every_offered_choice_is_accepted_by_the_parser(flow):
    from booley.harness.dashboard import run_form

    parser = parser_for(flow)
    for field in run_form.describe(parser).fields:
        for value in field.choices:
            assert parser.parse_args([field.flag, value]) is not None


def test_target_and_test_fields_say_how_their_text_is_read():
    from booley.harness.dashboard import run_form

    fields = {field.label: field for field in run_form.describe(parser_for("sim")).fields}
    assert "comma list" in fields["--target"].hint
    assert "more of them" in fields["--test"].hint
    assert fields["--seed"].hint.startswith("Seed for every selected Test Run")
    assert fields["--target"].shown("") == "automatic"
    assert fields["--test"].shown("") == ""
    assert fields["--coverage"].shown("") == "off"
    assert fields["--coverage"].shown("on") == "on"
    assert fields["--mode"].shown("") == "default"
    assert fields["--mode"].shown("elab-only") == "elab-only"


# --- command_argv ---------------------------------------------------------------


def test_command_argv_writes_fields_in_group_order_then_extra_words():
    from booley.harness.dashboard import run_form

    form = run_form.describe(parser_for("synth"))
    values = {
        "target": "a,b",
        "dry_run": "on",
        "flatten": "off",
        "frontend": "slang",
        "fresh_openroad": "on",
        "baseline": "  main ",
    }
    assert run_form.command_argv(form, values, "--report-dir 'out dir' -q") == (
        "--target",
        "a,b",
        "--dry-run",
        "--baseline",
        "main",
        "--no-flatten",
        "--frontend",
        "slang",
        "--fresh-openroad",
        "--report-dir",
        "out dir",
        "-q",
    )
    assert run_form.command_argv(form, {"flatten": "on", "diagnostic": ""}, "") == ("--flatten",)
    assert run_form.command_argv(form, {}, "") == ()


def test_a_text_value_starting_with_a_dash_is_written_with_an_equals_sign():
    from booley.harness.dashboard import run_form

    form = run_form.describe(parser_for("lint"))
    assert run_form.command_argv(form, {"scope": "-x.sv"}, "") == ("--scope=-x.sv",)
    assert run_form.command_argv(form, {"scope": "a b.sv"}, "") == ("--scope", "a b.sv")


def test_one_leading_double_dash_in_extra_arguments_is_dropped():
    from booley.harness.dashboard import run_form

    form = run_form.describe(parser_for("lint"))
    assert run_form.command_argv(form, {"target": "t"}, "-- --scope a.sv") == (
        "--target",
        "t",
        "--scope",
        "a.sv",
    )
    assert run_form.command_argv(form, {}, "-- --") == ("--",)  # only the first one


def test_unbalanced_quotes_in_extra_arguments_are_an_arguments_error():
    from booley.harness.dashboard import launch, run_form

    form = run_form.describe(parser_for("lint"))
    with pytest.raises(launch.ArgumentsError, match="unbalanced quotes"):
        run_form.command_argv(form, {}, "--scope 'a")
