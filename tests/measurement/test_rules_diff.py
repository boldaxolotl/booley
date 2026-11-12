"""The rules diff of a measured commit and the header state it sets (#3697)."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from booley.measurement.rules_diff import RulesDiff

if TYPE_CHECKING:
    from booley.measurement.run_key import KeyedCommit

from tests.runtime.test_commit_export import (
    commit_all,
    git,
    make_versioned_booley_directory,
    new_repository,
    write,
)

SYNTH = "[synth_core.synth]\n"
AREA = "area_kge_max = 60\n"
CLOCK_MIN = '"clk_i.fmax_mhz_min" = 400\n'
FPGA = "[synth_core.fpga]\nlut_count_max = 5000\n"
DEFAULT_BOUND = "[defaults.sim]\ntests_failed_max = 0\n"
SMOKE_CYCLES = '"smoke.cycle_count_max" = 1000\n'


def commit_pair(
    tmp_path: Path, base_files: dict[str, str], head_files: dict[str, str]
) -> tuple[Path, KeyedCommit, str, str]:
    """A repository with a base commit on ``main`` and a head commit on ``feature``.

    Returns the repository, the head's Measurement Key, the base and the head commit.
    """
    from booley.measurement.base import resolve_base
    from booley.measurement.run_key import resolve_measurement_key

    repo = new_repository(tmp_path / "repo")
    write(repo / ".booley" / "booley.toml", "[project]\n")
    write(repo / "rtl.v", "rtl\n")
    for name, text in base_files.items():
        write(repo / name, text)
    commit_all(repo, "base")
    git(repo, "checkout", "-q", "-b", "feature")
    for name, text in head_files.items():
        write(repo / name, text)
    head = commit_all(repo, "head")
    base = resolve_base(repo, head).commit
    assert base is not None
    return repo, resolve_measurement_key(repo, head), base, head


def metrics_diff(tmp_path: Path, base: str | None, head: str) -> RulesDiff:
    """The rules diff between two ``metrics.toml`` texts (``None``: the file is absent)."""
    from booley.measurement.rules_diff import rules_diff

    base_files = {} if base is None else {".booley/metrics.toml": base}
    repo, keyed, base_commit, _ = commit_pair(tmp_path, base_files, {".booley/metrics.toml": head})
    return rules_diff(repo, keyed, base_commit)


# (base, head, kind, direction, key, old, new, note shown)
ROWS = {
    "bound-added": (
        SYNTH + AREA,
        SYNTH + AREA + CLOCK_MIN,
        "bound added",
        "tightened",
        "clk_i.fmax_mhz_min",
        None,
        400,
        None,
    ),
    "table-added": (
        SYNTH + AREA,
        SYNTH + AREA + FPGA,
        "table added",
        "tightened",
        None,
        None,
        None,
        None,
    ),
    "max-lowered": (
        SYNTH + AREA,
        SYNTH + "area_kge_max = 50\n",
        "maximum lowered",
        "tightened",
        "area_kge_max",
        60,
        50,
        None,
    ),
    "min-raised": (
        SYNTH + CLOCK_MIN,
        SYNTH + '"clk_i.fmax_mhz_min" = 500\n',
        "minimum raised",
        "tightened",
        "clk_i.fmax_mhz_min",
        400,
        500,
        None,
    ),
    "max-raised": (
        SYNTH + AREA,
        SYNTH + 'area_kge_max = 80\nnotes = { area_kge_max = "after DSP sharing" }\n',
        "maximum raised",
        "loosened",
        "area_kge_max",
        60,
        80,
        "after DSP sharing",
    ),
    "min-lowered": (
        SYNTH + CLOCK_MIN,
        SYNTH + '"clk_i.fmax_mhz_min" = 300\n',
        "minimum lowered",
        "loosened",
        "clk_i.fmax_mhz_min",
        400,
        300,
        "(no note)",
    ),
    "bound-removed": (
        SYNTH + AREA + CLOCK_MIN,
        SYNTH + AREA,
        "bound removed",
        "loosened",
        "clk_i.fmax_mhz_min",
        400,
        None,
        "(no note)",
    ),
    "turned-off": (
        DEFAULT_BOUND + "[sim_core.sim]\n",
        DEFAULT_BOUND + '[sim_core.sim]\noff = ["tests_failed_max"]\ntrack = ["tests_failed"]\n',
        "bound turned off",
        "loosened",
        "tests_failed_max",
        0,
        None,
        "(no note)",
    ),
    "moved-to-track": (
        SYNTH + AREA + CLOCK_MIN,
        SYNTH + AREA + 'track = ["clk_i.fmax_mhz"]\n',
        "bound moved to track",
        "loosened",
        "clk_i.fmax_mhz_min",
        400,
        None,
        "(no note)",
    ),
    "table-removed": (
        SYNTH + AREA + FPGA,
        SYNTH + AREA,
        "table removed",
        "loosened",
        None,
        None,
        None,
        "(no note)",
    ),
    "track-edit": (
        SYNTH + AREA,
        SYNTH + AREA + 'track = ["cell_count"]\n',
        "track edited",
        "neutral",
        "cell_count",
        None,
        None,
        None,
    ),
    "track-removed": (
        SYNTH + AREA + 'track = ["cell_count"]\n',
        SYNTH + AREA,
        "track edited",
        "neutral",
        "cell_count",
        None,
        None,
        None,
    ),
    "tracked-became-bound": (
        SYNTH + AREA + 'track = ["cell_count"]\n',
        SYNTH + "area_kge_max = 60\ncell_count_max = 9000\n",
        "bound added",
        "tightened",
        "cell_count_max",
        None,
        9000,
        None,
    ),
    "equal-numbers": (
        SYNTH + AREA + "\n",
        SYNTH + "area_kge_max = 60.0\n",
        None,
        None,
        None,
        None,
        None,
        None,
    ),
    "notes-edit": (
        SYNTH + 'area_kge_max = 60\nnotes = { area_kge_max = "old" }\n',
        SYNTH + 'area_kge_max = 60\nnotes = { area_kge_max = "new" }\n',
        "note edited",
        "neutral",
        "area_kge_max",
        60,
        60,
        "new",
    ),
}


def test_each_metrics_change_is_classified_with_its_note(tmp_path: Path) -> None:
    for row, (base, head, kind, direction, key, old, new, note) in ROWS.items():
        diff = metrics_diff(tmp_path / row, base, head)

        if kind is None:  # the same rules respelled as a different number type
            assert diff.changes == (), row
            continue
        (change,) = diff.changes
        assert (change.kind.value, change.direction.value, change.key) == (kind, direction, key), (
            row
        )
        assert (change.old, change.new, change.note_text) == (old, new, note), row
        assert diff.unreadable is None, row


def test_unreadable_base_metrics_is_reported_and_amber(tmp_path: Path) -> None:
    from booley.measurement.rules_diff import AmberReason, header_amber

    diff = metrics_diff(tmp_path / "bad", "[synth_core.synth\n", SYNTH + AREA)
    missing = metrics_diff(tmp_path / "missing", None, SYNTH + AREA)

    assert diff.unreadable is not None and "base metrics.toml" in diff.unreadable
    assert "not valid TOML" in diff.unreadable
    assert diff.changes == ()
    assert header_amber(diff) == (AmberReason.RULES_LOOSENED,)
    assert [c.kind.value for c in missing.changes] == ["table added"]
    assert header_amber(missing) == ()


def test_unplaceable_base_booley_directory_is_reported(tmp_path: Path) -> None:
    from booley.measurement.rules_diff import AmberReason, header_amber, rules_diff

    repo, keyed, base, _ = commit_pair(
        tmp_path,
        {".booley/metrics.toml": SYNTH + AREA, "booley.toml": '[project]\ndir = "../outside"\n'},
        {"booley.toml": "[project]\n"},
    )

    diff = rules_diff(repo, keyed, base)

    assert diff.unreadable is not None and diff.unreadable.startswith("base commit")
    assert header_amber(diff) == (AmberReason.RULES_LOOSENED,)


def test_respelled_key_is_a_removal_and_an_addition(tmp_path: Path) -> None:
    from booley.measurement.rules_diff import AmberReason, header_amber

    table = "[sim_core.sim]\n"
    diff = metrics_diff(
        tmp_path, table + SMOKE_CYCLES, table + '"smoke@1.cycle_count_max" = 1000\n'
    )

    assert sorted(c.kind.value for c in diff.changes) == ["bound added", "bound removed"]
    assert header_amber(diff) == (AmberReason.RULES_LOOSENED,)


def test_max_raised_turns_header_amber_lowered_and_notes_do_not(tmp_path: Path) -> None:
    from booley.measurement.rules_diff import header_amber

    cases = {
        "raised": (SYNTH + "area_kge_max = 80\n", ["rules loosened"]),
        "lowered": (SYNTH + "area_kge_max = 50\n", []),
        "notes": (SYNTH + 'area_kge_max = 60\nnotes = { area_kge_max = "why" }\n', []),
    }
    for name, (head, amber) in cases.items():
        diff = metrics_diff(tmp_path / name, SYNTH + AREA, head)

        assert [reason.value for reason in header_amber(diff)] == amber, name


def test_tests_toml_change_is_reported_with_diff_and_amber(tmp_path: Path) -> None:
    from booley.measurement.rules_diff import AmberReason, header_amber, rules_diff

    repo, keyed, base, _ = commit_pair(
        tmp_path,
        {".booley/tests.toml": '[sim_core]\ntests = ["smoke"]\n'},
        {".booley/tests.toml": '[sim_core]\ntests = ["smoke", "other"]\n'},
    )

    diff = rules_diff(repo, keyed, base)

    assert diff.changes == ()
    assert diff.tests_diff is not None
    assert "--- a/.booley/tests.toml" in diff.tests_diff
    assert '-tests = ["smoke"]' in diff.tests_diff
    assert '+tests = ["smoke", "other"]' in diff.tests_diff
    assert header_amber(diff) == (AmberReason.TESTS_CHANGED,)


def test_tests_toml_diff_without_final_newline_keeps_lines_apart(tmp_path: Path) -> None:
    from booley.measurement.rules_diff import rules_diff

    repo, keyed, base, _ = commit_pair(
        tmp_path,
        {".booley/tests.toml": 'tests = ["smoke"]'},
        {".booley/tests.toml": 'tests = ["smoke", "other"]'},
    )

    diff = rules_diff(repo, keyed, base)

    assert diff.tests_diff is not None
    assert '-tests = ["smoke"]\n\\ No newline at end of file\n+tests' in diff.tests_diff


def test_tests_toml_added_or_invalid_bytes_still_show_a_diff(tmp_path: Path) -> None:
    from booley.measurement.rules_diff import AmberReason, header_amber, rules_diff
    from booley.measurement.run_key import resolve_measurement_key

    repo, keyed, base, _ = commit_pair(
        tmp_path / "added", {}, {".booley/tests.toml": 'tests = ["smoke"]\n'}
    )
    added = rules_diff(repo, keyed, base)
    repo, _, base, _ = commit_pair(tmp_path / "bytes", {".booley/tests.toml": "x"}, {})
    tests = repo / ".booley" / "tests.toml"
    git(repo, "checkout", "-q", "-b", "other-base", base)
    tests.write_bytes(b"a = '\xff'")
    other_base = commit_all(repo, "invalid bytes")
    git(repo, "checkout", "-q", "feature")
    tests.write_bytes(b"a = '\xfe'")
    head = commit_all(repo, "other invalid bytes")
    invalid = rules_diff(repo, resolve_measurement_key(repo, head), other_base)

    assert added.tests_diff is not None and '+tests = ["smoke"]' in added.tests_diff
    assert header_amber(added) == (AmberReason.TESTS_CHANGED,)
    assert invalid.tests_diff is not None and "not valid UTF-8" in invalid.tests_diff


def test_directory_named_like_a_rules_file_is_unreadable_not_a_crash(tmp_path: Path) -> None:
    from booley.measurement.rules_diff import AmberReason, header_amber, rules_diff

    repo, keyed, base, _ = commit_pair(
        tmp_path, {".booley/metrics.toml/x": "x"}, {".booley/metrics.toml/x": "y"}
    )

    diff = rules_diff(repo, keyed, base)

    assert diff.unreadable is not None and "not a file" in diff.unreadable
    assert header_amber(diff) == (AmberReason.RULES_LOOSENED,)


def test_unreadable_measured_metrics_is_reported_and_amber(tmp_path: Path) -> None:
    from booley.measurement.rules_diff import AmberReason, header_amber

    diff = metrics_diff(tmp_path, SYNTH + AREA, "[synth_core.synth\n")

    assert diff.unreadable is not None and "measured metrics.toml" in diff.unreadable
    assert header_amber(diff) == (AmberReason.RULES_LOOSENED,)


def test_booley_and_reviews_toml_edits_leave_header_unchanged(tmp_path: Path) -> None:
    from booley.measurement.base import changed_files, protected_edits
    from booley.measurement.protected_set import protected_set
    from booley.measurement.rules_diff import header_amber, paired_protected_edits, rules_diff
    from booley.runtime.commit_export import export_commit

    metrics = {".booley/metrics.toml": SYNTH + AREA}
    repo, keyed, base, head = commit_pair(
        tmp_path,
        {**metrics, ".booley/reviews.toml": "# reviews\n"},
        {
            **metrics,
            ".booley/booley.toml": "[project]\n# edited\n",
            ".booley/reviews.toml": "# new\n",
        },
    )

    diff = rules_diff(repo, keyed, base)
    protected = protected_set(export_commit(repo, head, tmp_path / "out").root)
    edits = protected_edits(changed_files(repo, base, head), protected)

    assert (diff.changes, diff.tests_diff, diff.unreadable) == ((), None, None)
    assert header_amber(diff) == ()
    assert edits == (".booley/booley.toml", ".booley/reviews.toml")
    assert paired_protected_edits(repo, keyed, diff, protected) == ()
    assert rules_diff(repo, keyed, None) == RulesDiff((), None, None, None)


def versioned_case(tmp_path: Path, layout: str) -> tuple[Path, Path]:
    """A Versioned Booley Directory whose branch raises a ``_max`` and edits ``booley.toml``."""
    checkout, booley = make_versioned_booley_directory(tmp_path, layout)
    default_branch = booley if layout == "standalone" else tmp_path / "main" / ".booley"
    write(default_branch / "metrics.toml", SYNTH + AREA)
    git(default_branch, "add", "metrics.toml")
    commit_all(default_branch, "baseline", staged_only=True)
    if layout == "standalone":
        git(booley, "checkout", "-q", "-b", "feature")
    else:
        git(booley, "merge", "-q", "--no-edit", "main")
    write(booley / "metrics.toml", SYNTH + "area_kge_max = 80\n")
    write(booley / "booley.toml", "[submodules]\npaths = []\n# edited\n")
    git(booley, "add", "metrics.toml", "booley.toml")
    commit_all(booley, "loosen", staged_only=True)
    # A live, uncommitted edit must not change what is reported.
    write(booley / "metrics.toml", SYNTH + "area_kge_max = 999\n")
    return checkout, booley


def test_versioned_booley_directory_loosened_rule_and_protected_edit(tmp_path: Path) -> None:
    from booley.measurement.protected_set import protected_set
    from booley.measurement.rules_diff import (
        AmberReason,
        header_amber,
        paired_protected_edits,
        rules_diff,
    )
    from booley.measurement.run_key import resolve_measurement_key
    from booley.runtime.commit_export import export_commit

    for layout in ("standalone", "paired-checkout"):
        root = tmp_path / layout
        root.mkdir()
        checkout, _ = versioned_case(root, layout)
        head = git(checkout, "rev-parse", "HEAD")
        keyed = resolve_measurement_key(checkout, head)

        diff = rules_diff(checkout, keyed, None)  # no base run, no RTL base
        protected = protected_set(export_commit(checkout, head, root / "out").root)
        edits = paired_protected_edits(checkout, keyed, diff, protected)

        (change,) = diff.changes
        assert (change.kind.value, change.old, change.new) == ("maximum raised", 60, 80), layout
        assert header_amber(diff) == (AmberReason.RULES_LOOSENED,), layout
        assert diff.paired_base is not None and diff.paired_base.no_base is None, layout
        assert edits == (".booley/booley.toml", ".booley/metrics.toml"), layout


def test_versioned_booley_directory_without_default_branch_reports_no_base(
    tmp_path: Path,
) -> None:
    from booley.measurement.protected_set import protected_set
    from booley.measurement.rules_diff import header_amber, paired_protected_edits, rules_diff
    from booley.measurement.run_key import resolve_measurement_key
    from booley.runtime.commit_export import export_commit

    checkout, booley = versioned_case(tmp_path, "standalone")
    git(booley, "branch", "-m", "feature", "trunk")  # neither origin/HEAD nor main remains
    git(booley, "branch", "-D", "main")
    head = git(checkout, "rev-parse", "HEAD")
    keyed = resolve_measurement_key(checkout, head)

    diff = rules_diff(checkout, keyed, None)
    protected = protected_set(export_commit(checkout, head, tmp_path / "out").root)

    assert diff.changes == ()
    assert header_amber(diff) == ()
    assert diff.paired_base is not None
    assert diff.paired_base.no_base == "neither origin/HEAD nor local main exists"
    assert paired_protected_edits(checkout, keyed, diff, protected) == ()


def test_booley_commit_without_a_versioned_booley_directory_is_refused(tmp_path: Path) -> None:
    from booley.measurement.rules_diff import rules_diff

    repo, keyed, base, head = commit_pair(tmp_path, {}, {"rtl.v": "rtl2\n"})

    with pytest.raises(ValueError, match="Versioned Booley Directory"):
        rules_diff(repo, replace(keyed, booley_commit=head), base)
