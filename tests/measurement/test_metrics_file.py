"""``metrics.toml``: loading, load errors and the effective rules of each table."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from booley.measurement.metrics_file import MetricsLoadError, MetricsRules, TableRules

from tests.measurement.metrics_project_support import EXAMPLE_METRICS, write_metrics_project


def _load(root: Path, text: str) -> MetricsRules:
    from booley.measurement.metrics_file import load_metrics_rules

    write_metrics_project(root, text)
    return load_metrics_rules(root)


def _errors(root: Path, text: str) -> tuple[MetricsLoadError, ...]:
    from booley.measurement.metrics_file import MetricsFileError

    with pytest.raises(MetricsFileError) as raised:
        _load(root, text)
    return raised.value.errors


def _table(rules: MetricsRules, target: str, flow: str) -> TableRules:
    (found,) = (t for t in rules.tables if (t.target, t.flow) == (target, flow))
    return found


def _bounds(table: TableRules) -> dict[str, float]:
    return {bound.key: bound.value for bound in table.bounds}


def test_example_file_from_issue_3045_loads(tmp_path: Path) -> None:
    rules = _load(tmp_path, EXAMPLE_METRICS)
    assert [(t.target, t.flow) for t in rules.tables] == [
        ("synth_core", "synth"),
        ("synth_core", "fpga"),
        ("sim_core", "lint"),
        ("sim_core", "sim"),
        ("sim_core", "coverage"),
        ("sim_fifo", "sim"),
    ]
    assert _table(rules, "synth_core", "synth").identity == "::synth_core:0#synth_core"
    assert _bounds(_table(rules, "sim_core", "coverage")) == {
        "tests_failed_max": 0,
        "line_coverage_min": 90,
    }
    fifo = _table(rules, "sim_fifo", "sim")
    assert fifo.bounds == ()
    assert [tracked.key for tracked in fifo.tracked] == ["tests_failed"]


def test_sim_fifo_without_track_is_a_load_error(tmp_path: Path) -> None:
    text = EXAMPLE_METRICS.replace('track = ["tests_failed"]\n', "")
    (error,) = _errors(tmp_path, text)
    assert error.table == "[sim_fifo.sim]"
    assert "no reported metric" in error.message


# (toml, table, key, fragment of the message)
LOAD_ERRORS = {
    "invalid-toml": ("[sim_core.sim\n", "", None, "not valid TOML"),
    "unknown-target": ("[nope.sim]\ntests_failed_max = 0\n", "[nope.sim]", None, "nope"),
    "ambiguous-target": (
        "[shared.sim]\ntests_failed_max = 0\n",
        "[shared.sim]",
        None,
        "declared by 2 cores",
    ),
    "duplicate-target-table": (
        '[sim_core.sim]\ntests_failed_max = 0\n["::sim_core:0#sim_core".sim]\n'
        "tests_failed_max = 0\n",
        '["::sim_core:0#sim_core".sim]',
        None,
        "[sim_core.sim]",
    ),
    "not-a-flow-table": ("[sim_core]\nx = 1\n", "[sim_core]", "x", "holds only"),
    "target-not-a-table": ("sim_core = 3\n", "", "sim_core", "holds only"),
    "unknown-flow": ("[sim_core.elab]\ntests_failed_max = 0\n", "[sim_core.elab]", None, "Flow"),
    "review-flow": (
        "[sim_core.review]\ntests_failed_max = 0\n",
        "[sim_core.review]",
        None,
        "Flow",
    ),
    "unknown-metric": (
        "[sim_core.sim]\nbogus_max = 1\n",
        "[sim_core.sim]",
        "bogus_max",
        "unknown",
    ),
    "unknown-tracked-metric": (
        '[sim_core.sim]\ntrack = ["bogus"]\n',
        "[sim_core.sim]",
        "bogus",
        "unknown metric",
    ),
    "not-a-bound": (
        "[sim_core.sim]\ntests_failed = 0\n",
        "[sim_core.sim]",
        "tests_failed",
        "`_max` or `_min`",
    ),
    "non-number": (
        '[sim_core.sim]\ntests_failed_max = "zero"\n',
        "[sim_core.sim]",
        "tests_failed_max",
        "finite number",
    ),
    "bool-number": (
        "[sim_core.sim]\ntests_failed_max = true\n",
        "[sim_core.sim]",
        "tests_failed_max",
        "finite number",
    ),
    "track-not-a-list": (
        '[sim_core.sim]\ntrack = "tests_failed"\n',
        "[sim_core.sim]",
        "track",
        "list of strings",
    ),
    "notes-not-a-table": (
        '[sim_core.sim]\ntests_failed_max = 0\nnotes = "x"\n',
        "[sim_core.sim]",
        "notes",
        "table",
    ),
    "note-not-a-string": (
        "[sim_core.sim]\ntests_failed_max = 0\nnotes = { tests_failed_max = 3 }\n",
        "[sim_core.sim]",
        "tests_failed_max",
        "string",
    ),
    "prefix-on-plain": (
        '[sim_core.sim]\n"clk_i.tests_failed_max" = 0\n',
        "[sim_core.sim]",
        "clk_i.tests_failed_max",
        "takes no prefix",
    ),
    "unprefixed-per-test-several-runs": (
        "[sim_two.sim]\ncycle_count_max = 5\n",
        "[sim_two.sim]",
        "cycle_count_max",
        "name a test",
    ),
    "unknown-test-prefix": (
        '[sim_core.sim]\n"nope.cycle_count_max" = 5\n',
        "[sim_core.sim]",
        "nope.cycle_count_max",
        "unknown test prefix",
    ),
    "single-random-seed-prefix": (
        '[sim_core.sim]\n"smoke@x1.cycle_count_max" = 5\n',
        "[sim_core.sim]",
        "smoke@x1.cycle_count_max",
        "random seeds",
    ),
    "duplicate-track": (
        '[sim_core.sim]\ntrack = ["tests_total", "tests_total"]\n',
        "[sim_core.sim]",
        "tests_total",
        "same address",
    ),
    "duplicate-note": (
        "[sim_core.sim]\ntests_failed_max = 0\n"
        'notes = { "smoke.cycle_count_max" = "a", "smoke@1.cycle_count_max" = "b" }\n'
        "cycle_count_max = 5\n",
        "[sim_core.sim]",
        "smoke@1.cycle_count_max",
        "same address",
    ),
    "relative-bound": (
        "[sim_core.sim]\ntests_failed_increase_at_most = 1\n",
        "[sim_core.sim]",
        "tests_failed_increase_at_most",
        "only absolute bounds",
    ),
    "nested-table": (
        "[synth_core.synth]\nclk_i.fmax_mhz_min = 400\n",
        "[synth_core.synth]",
        "clk_i",
        "nested",
    ),
    "nested-table-unwritable-values": (
        "[synth_core.synth]\nclk_i.fmax_mhz_min = 1979-05-27\nclk_i.a = inf\nclk_i.b = true\n"
        'clk_i.c = "x"\n',
        "[synth_core.synth]",
        "clk_i",
        "nested",
    ),
    "unquoted-selector": (
        "[sim_core.sim]\nsmoke+fast.cycle_count_max = 10\n",
        "[sim_core.sim]",
        "smoke+fast.cycle_count_max",
        "quote",
    ),
    "only-off": (
        '[defaults.sim]\ntests_failed_max = 0\n[sim_fifo.sim]\noff = ["tests_failed_max"]\n',
        "[sim_fifo.sim]",
        None,
        "no reported metric",
    ),
    "no-reported-metric": ("[sim_core.lint]\n", "[sim_core.lint]", None, "no reported metric"),
    "off-names-no-default": (
        '[sim_core.sim]\ntrack = ["tests_total"]\noff = ["tests_failed_max"]\n',
        "[sim_core.sim]",
        "tests_failed_max",
        "no default bound",
    ),
    "note-names-no-bound": (
        '[sim_core.sim]\ntests_failed_max = 0\nnotes = { tests_total_max = "x" }\n',
        "[sim_core.sim]",
        "tests_total_max",
        "no effective bound",
    ),
    "tracked-and-bounded": (
        '[sim_core.sim]\ntests_failed_max = 0\ntrack = ["tests_failed"]\n',
        "[sim_core.sim]",
        "tests_failed",
        "also bounded",
    ),
    "same-address-twice": (
        '[sim_core.sim]\n"smoke.cycle_count_max" = 1\n"smoke@1.cycle_count_max" = 2\n',
        "[sim_core.sim]",
        "smoke@1.cycle_count_max",
        "same address",
    ),
    "defaults-track": (
        '[defaults.sim]\ntrack = ["tests_failed"]\n',
        "[defaults.sim]",
        "track",
        "bounds only",
    ),
    "defaults-test-prefix": (
        '[defaults.sim]\n"smoke.cycle_count_max" = 1\n',
        "[defaults.sim]",
        "smoke.cycle_count_max",
        "test prefix",
    ),
    "defaults-unknown-flow": (
        "[defaults.elab]\ntests_failed_max = 0\n",
        "[defaults.elab]",
        None,
        "Flow",
    ),
    "defaults-not-a-table": ("defaults = 1\n", "", "defaults", "holds only"),
}


def test_load_error_names_table_and_key(tmp_path: Path) -> None:
    """Every error class: a file that has it reports it, naming its table and key."""
    for case, (text, table, key, fragment) in LOAD_ERRORS.items():
        errors = _errors(tmp_path, text)
        matching = [error for error in errors if fragment in error.message]
        assert matching, (case, [error.render() for error in errors])
        error = matching[0]
        assert (error.table, error.key) == (table, key), case
        assert error.render().startswith(f"{table} " if table else ""), case
        if key is not None:
            assert key in error.render(), case


def test_unquoted_prefix_shows_quoted_line(tmp_path: Path) -> None:
    """A selector with ``+``/``@`` is not a TOML bare key; a dotted one nests."""
    cases = [
        ("clk_i.fmax_mhz_min = 400", '"clk_i.fmax_mhz_min" = 400'),
        ("smoke+fast.cycle_count_max = 10", '"smoke+fast.cycle_count_max" = 10'),
        ("smoke@7.cycle_count_max = 10", '"smoke@7.cycle_count_max" = 10'),
    ]
    for line, corrected in cases:
        (error,) = _errors(tmp_path, f"[synth_core.synth]\narea_kge_max = 5\n{line}\n")
        assert error.table == "[synth_core.synth]", line
        assert error.corrected_line == corrected, line
        assert f"write instead: {corrected}" in error.render(), line


def test_relative_bound_shows_absolute_line(tmp_path: Path) -> None:
    cases = [
        ("tests_failed_increase_at_most = 1", "tests_failed_max = <absolute number>"),
        ("tests_failed_reduce_at_least = 1", "tests_failed_max = <absolute number>"),
        ("tests_failed_increase_at_least = 1", "tests_failed_min = <absolute number>"),
        ("tests_failed_reduce_at_most = 1", "tests_failed_min = <absolute number>"),
        (
            '"smoke.cycle_count_increase_at_most_cycles" = 5',
            '"smoke.cycle_count_max" = <absolute number>',
        ),
    ]
    for line, corrected in cases:
        (error,) = _errors(tmp_path, f"[sim_core.sim]\n{line}\n")
        assert "only absolute bounds exist" in error.message, line
        assert error.corrected_line == corrected, line


def test_every_load_error_is_reported(tmp_path: Path) -> None:
    text = (
        "[nope.sim]\ntests_failed_max = 0\n"
        "[sim_core.sim]\nbogus_max = 1\ntests_failed_increase_at_most = 1\n"
        "[sim_core.lint]\n"
    )
    errors = _errors(tmp_path, text)
    assert [(error.table, error.key) for error in errors] == [
        ("[nope.sim]", None),
        ("[sim_core.sim]", "bogus_max"),
        ("[sim_core.sim]", "tests_failed_increase_at_most"),
        ("[sim_core.lint]", None),
    ]


def test_sim_defaults_reach_every_sim_table_and_no_coverage_table(tmp_path: Path) -> None:
    text = (
        "[defaults.sim]\ntests_failed_max = 0\n"
        '[sim_core.sim]\ntrack = ["tests_total"]\n'
        '[sim_fifo.sim]\ntrack = ["tests_total"]\n'
        "[sim_core.coverage]\nline_coverage_min = 90\n"
    )
    rules = _load(tmp_path, text)
    assert _bounds(_table(rules, "sim_core", "sim")) == {"tests_failed_max": 0}
    assert _bounds(_table(rules, "sim_fifo", "sim")) == {"tests_failed_max": 0}
    assert _bounds(_table(rules, "sim_core", "coverage")) == {"line_coverage_min": 90}


def test_defaults_create_no_table(tmp_path: Path) -> None:
    text = "[defaults.sim]\ntests_failed_max = 0\n[defaults.coverage]\ntests_failed_max = 0\n"
    assert _load(tmp_path, text).tables == ()


def test_test_prefix_matching_no_test_is_a_load_error(tmp_path: Path) -> None:
    (error,) = _errors(tmp_path, '[sim_core.sim]\n"nope.cycle_count_max" = 5\n')
    assert error.key == "nope.cycle_count_max"
    assert "Declared Tests: smoke" in error.message


def test_test_prefix_matching_several_seeds_is_a_load_error(tmp_path: Path) -> None:
    (error,) = _errors(tmp_path, '[sim_core.sim]\n"smoke@x3.cycle_count_max" = 5\n')
    assert error.key == "smoke@x3.cycle_count_max"
    assert "random seeds" in error.message


def test_unknown_clock_prefix_loads(tmp_path: Path) -> None:
    rules = _load(tmp_path, '[synth_core.synth]\n"no_such_clk.fmax_mhz_min" = 400\n')
    (bound,) = _table(rules, "synth_core", "synth").bounds
    assert (bound.metric, bound.limit, bound.prefix, bound.test_run) == (
        "fmax_mhz",
        "min",
        "no_such_clk",
        None,
    )


def test_effective_rules_apply_defaults_off_track_and_notes(tmp_path: Path) -> None:
    text = """\
[defaults.synth]
area_kge_max = 100
"clk_i.fmax_mhz_min" = 300
"clk_i.wns_ns_min" = 0

[synth_core.synth]
off = ["clk_i.wns_ns_min"]
area_kge_max = 50
track = ["cell_count", "clk_i.critical_path_ps"]
notes = { area_kge_max = "after sharing", "clk_i.fmax_mhz_min" = "from the library" }

[synth_core.fpga]
track = ["fmax_mhz"]

[sim_core.sim]
"smoke@1.cycle_count_max" = 9
notes = { "smoke.cycle_count_max" = "one cycle per beat" }
"""
    rules = _load(tmp_path, text)
    synth = _table(rules, "synth_core", "synth")
    # A list, not a dict by key: the table's own area bound must replace the default, not join it.
    assert [(bound.key, bound.value, bound.note) for bound in synth.bounds] == [
        ("clk_i.fmax_mhz_min", 300, "from the library"),
        ("area_kge_max", 50, "after sharing"),
    ]
    assert [(m.key, m.metric, m.prefix) for m in synth.tracked] == [
        ("cell_count", "cell_count", None),
        ("clk_i.critical_path_ps", "critical_path_ps", "clk_i"),
    ]
    fpga = _table(rules, "synth_core", "fpga")
    assert fpga.bounds == ()
    assert [m.key for m in fpga.tracked] == ["fmax_mhz"]
    (cycles,) = _table(rules, "sim_core", "sim").bounds
    assert (cycles.metric, cycles.limit, cycles.value) == ("cycle_count", "max", 9)
    assert (cycles.test_run, cycles.note) == ("smoke@1", "one cycle per beat")


def test_unprefixed_per_test_bound_resolves_a_single_test_run(tmp_path: Path) -> None:
    rules = _load(
        tmp_path, "[sim_core.sim]\ncycle_count_max = 9\n[sim_fifo.sim]\ncycle_count_max = 3\n"
    )
    assert _table(rules, "sim_core", "sim").bounds[0].test_run == "smoke@1"
    assert _table(rules, "sim_fifo", "sim").bounds[0].test_run == "<default>@1"


def test_a_quoted_vlnv_target_key_resolves_like_a_bare_name(tmp_path: Path) -> None:
    rules = _load(tmp_path, '["vendor:a:x:1.0#shared".sim]\ntests_failed_max = 0\n')
    (table,) = rules.tables
    assert (table.target, table.identity) == ("vendor:a:x:1.0#shared", "vendor:a:x:1.0#shared")


def test_missing_file_declares_nothing(tmp_path: Path) -> None:
    from booley.measurement.metrics_file import load_metrics_rules

    write_metrics_project(tmp_path)
    rules = load_metrics_rules(tmp_path)
    assert rules.tables == ()
    assert rules.path.name == "metrics.toml"
    assert not rules.path.exists()


def test_file_without_tables_declares_nothing(tmp_path: Path) -> None:
    assert _load(tmp_path, "# nothing yet\n").tables == ()
