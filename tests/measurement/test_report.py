"""The Measurement Report renderers: Markdown and terminal text from the view JSON (#3748)."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from booley.measurement.run_document import RunStatus, Verdict
from booley.measurement.run_store import FlowReport, MeasurementStore, StoredRun
from booley.presentation import colors
from tests.measurement.test_base_run import Entry, Table, build_run, values
from tests.measurement.test_rules_diff import AREA, SYNTH
from tests.measurement.test_run_store import (
    fingerprint,
    in_tree_repository,
    key_of,
    make_run,
    no_ambient_booley_dir,  # noqa: F401  autouse: no ambient Booley Directory in these tests
)
from tests.measurement.test_run_view import (
    feature_repo,
    stored_in_versioned_case,
    synth_entries,
    tree_of,
    view_of,
)
from tests.runtime.test_commit_export import add_gitlink, commit_all, git, write


# The renderers are imported on use, so a missing module fails the test body, not collection.
def markdown_report(view: dict[str, Any]) -> str:
    from booley.measurement.report import markdown_report as render

    return render(view)


def terminal_report(view: dict[str, Any], *, width: int | None = None) -> str:
    from booley.measurement.report import terminal_report as render

    return render(view, width=width)


def write_markdown_report(view: dict[str, Any]) -> Path:
    from booley.measurement.report import write_markdown_report as write_report

    return write_report(view)


@pytest.fixture(autouse=True)
def plain_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    """Terminal text without ANSI unless a test turns colour on."""
    monkeypatch.setattr(colors, "COLORS_ENABLED", False)


def section(markdown: str, title: str) -> str:
    """The body of the Markdown section *title*; empty when the report has none."""
    marker = f"## {title}\n"
    if marker not in markdown:
        return ""
    return markdown.split(marker, 1)[1].split("\n## ", 1)[0]


def metric_row(markdown: str, metric: str) -> list[str]:
    """The cells of the metric table row for *metric*."""
    (row,) = [
        line
        for line in section(markdown, "Metrics").splitlines()
        if line.startswith("|") and f"| {metric} |" in line
    ]
    return [cell.strip() for cell in row.strip("|").split("|")]


def failing_world(tmp_path: Path) -> tuple[Path, MeasurementStore, StoredRun, str, str]:
    """Two commits on ``feature`` and a run of the head with one unmet and one error result."""
    repo, _, first = feature_repo(tmp_path)
    write(repo / "extra.v", "module e; endmodule\n")
    head = commit_all(repo, "second")
    store = MeasurementStore(repo / ".booley")
    table = Table("synth", synth_entries(), target="core")
    stored = store.write(build_run(key_of(repo, head), "r1", (table,)), [])
    return repo, store, stored, head, first


def test_reports_render_from_the_view_json_alone(tmp_path: Path) -> None:
    repo, store, stored, head, first = failing_world(tmp_path)
    view = view_of(repo, store, stored, head)
    other = view_of(repo, store, stored, head, base=first)
    kept = {path.name for path in stored.path.parent.iterdir()}

    written = write_markdown_report(view)
    first_text = written.read_text(encoding="utf-8")
    write_markdown_report(other)

    assert written == Path(view["view"]["paths"]["markdown"])
    assert first_text == markdown_report(view)
    assert written.read_text(encoding="utf-8") == markdown_report(other) != first_text
    assert {path.name for path in stored.path.parent.iterdir()} == kept | {written.name}

    # The JSON round trip is the whole input: no repository, store or clock is read.
    copied = json.loads(json.dumps(view))
    before = (markdown_report(copied), terminal_report(copied, width=100))
    repo.rename(tmp_path / "gone")
    assert (markdown_report(copied), terminal_report(copied, width=100)) == before
    assert before[0] == first_text


def test_unmet_and_error_rows_lead_with_excerpt_and_rerun_commands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, store, stored, head, _ = failing_world(tmp_path)
    view = view_of(repo, store, stored, head)

    markdown = markdown_report(view)

    assert "**1 unmet, 1 error, 1 not evaluated of 4 enforced**" in markdown
    assert markdown.index("## Failing") < markdown.index("## Metrics")
    failing = section(markdown, "Failing")
    assert "core synth area_um2_max: 9 vs ≤ 5, unmet" in failing
    assert "core synth area_kge_max: — vs ≤ 5, error" in failing
    assert "too big" in failing
    assert "boom" in failing
    assert "booley measure --target core\nbooley measure\n" in failing
    assert "Run r1: failing" in markdown

    monkeypatch.setattr(colors, "COLORS_ENABLED", True)
    terminal = terminal_report(view)
    assert colors.red("unmet") in terminal
    assert colors.red("error") in terminal
    assert colors.green("met") in terminal
    assert colors.red("Run r1: failing") in terminal
    assert colors.red("1 unmet, 1 error, 1 not evaluated of 4 enforced") in terminal


def test_identical_tree_commit_reports_measured_as_and_its_own_base(tmp_path: Path) -> None:
    repo, _ = in_tree_repository(tmp_path / "repo")
    write(repo / ".booley" / "metrics.toml", SYNTH + AREA)
    base_a = commit_all(repo, "rules")
    git(repo, "checkout", "-q", "-b", "feature")
    write(repo / "rtl.v", "module b; endmodule\n")
    write(repo / ".booley" / "metrics.toml", SYNTH + "area_kge_max = 80\n")
    first = commit_all(repo, "first")
    git(repo, "checkout", "-q", "main")
    write(repo / "rtl.v", "module b; endmodule\n")
    write(repo / ".booley" / "metrics.toml", SYNTH + "area_kge_max = 70\n")
    new_base = commit_all(repo, "main moved on")
    second = git(repo, "commit-tree", tree_of(repo, first), "-p", new_base, "-m", "same tree")
    store = MeasurementStore(repo / ".booley")
    stored = store.write(make_run(key_of(repo, first), "r1", flow="synth"), [])

    other = markdown_report(view_of(repo, store, stored, second))
    own = markdown_report(view_of(repo, store, stored, first))

    assert f"measured as `{first}`, identical tree" in other
    assert f"Base `{new_base}`" in other
    assert "1 file changed (1 modified), 0 submodules" in other
    assert "- M .booley/metrics.toml" in other
    assert "- M rtl.v" not in other
    assert "synth_core synth area_kge_max: maximum raised, 70 → 80" in other
    assert "measured as" not in own
    assert f"Base `{base_a}`" in own
    assert "- M rtl.v" in own
    assert "maximum raised, 60 → 80" in own


def test_two_bases_give_two_base_sections_and_the_same_verdicts(tmp_path: Path) -> None:
    repo, store, stored, head, first = failing_world(tmp_path)
    main = git(repo, "rev-parse", "main")

    default = markdown_report(view_of(repo, store, stored, head))
    chosen = markdown_report(view_of(repo, store, stored, head, base=first))

    assert f"Base `{main}`" in default
    assert f"Base `{first}` (--base, used as given)" in chosen
    assert f"booley measure --all {main}" in default
    assert f"booley measure --all {first}" in chosen
    assert section(default, "Changed files") != section(chosen, "Changed files")
    assert default.splitlines()[2] == chosen.splitlines()[2]  # the verdict line
    for same in ("Failing", "Metrics"):
        assert section(default, same) == section(chosen, same) != ""


def test_loosened_rule_turns_header_amber_and_lists_its_note(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, _ = in_tree_repository(tmp_path / "repo")
    write(
        repo / ".booley" / "metrics.toml",
        SYNTH + "area_kge_max = 60\ncell_count_max = 100\n",
    )
    commit_all(repo, "rules")
    git(repo, "checkout", "-q", "-b", "feature")
    write(
        repo / ".booley" / "metrics.toml",
        SYNTH
        + 'cell_count_max = 90\narea_kge_max = 80\nnotes = { area_kge_max = "after DSP sharing" }\n',
    )
    head = commit_all(repo, "loosen")
    entries = (
        Entry("cell_count_max", "cell_count", 85, bound=90),
        Entry("area_kge_max", "area_kge", 70, bound=80),
    )
    store = MeasurementStore(repo / ".booley")
    table = Table("synth", entries, target="synth_core")
    stored = store.write(build_run(key_of(repo, head), "r1", (table,)), [])
    view = view_of(repo, store, stored, head)

    markdown = markdown_report(view)

    assert "Run r1: rules loosened" in markdown
    rules = section(markdown, "Rules changes")
    assert "synth_core synth area_kge_max: maximum raised, 60 → 80 — after DSP sharing" in rules
    assert "maximum lowered, 100 → 90" in rules
    assert rules.index("maximum raised") < rules.index("maximum lowered")  # loosened first
    assert "- rules loosened" in section(markdown, "Warnings")
    monkeypatch.setattr(colors, "COLORS_ENABLED", True)
    terminal = terminal_report(view)
    assert colors.amber("Run r1: rules loosened") in terminal
    assert colors.green("Run r1: rules loosened") not in terminal
    assert colors.green("2 of 2 enforced met") not in terminal  # not an all-green verdict
    assert colors.amber("2 of 2 enforced met") in terminal


def test_thirty_changed_files_show_twenty_and_a_remainder(tmp_path: Path) -> None:
    repo, _, _ = feature_repo(tmp_path)
    for index in range(27):
        write(repo / f"f{index:02d}.v", "module m; endmodule\n")
    write(repo / "z.sdc", "create_clock\n")
    write(repo / "zz_protected.v", "module p; endmodule\n")
    head = commit_all(repo, "many")
    store = MeasurementStore(repo / ".booley")
    run = replace(
        make_run(key_of(repo, head), "r1", flow="synth"), protected_paths=("zz_protected.v",)
    )
    stored = store.write(run, [])

    changed = section(markdown_report(view_of(repo, store, stored, head)), "Changed files")

    lines = changed.strip().splitlines()
    assert "- 30 files changed" in lines[0]  # rtl.v from feature_repo counts too
    assert (
        len([line for line in lines if line.startswith("- A ") or line.startswith("- M ")]) == 20
    )
    assert "- ... and 10 more" in lines
    assert "- constraint edits: z.sdc" in lines
    assert "- protected edits: zz_protected.v" in lines
    assert "zz_protected.v (protected)" not in changed  # past the cap, so only the edits line


def test_delta_reads_no_base_run_then_no_base_value(tmp_path: Path) -> None:
    repo, _, head = feature_repo(tmp_path)
    base = git(repo, "rev-parse", "main")
    store = MeasurementStore(repo / ".booley")
    entries = (
        Entry("cell_count_max", "cell_count", 90, bound=100),
        Entry("area_um2", "area_um2", 12.0),
        Entry("area_kge", "area_kge", 12.3),
    )
    table = Table("synth", entries, values(plain={"cell_count": 90, "tests_total": 5}))
    stored = store.write(build_run(key_of(repo, head), "head", (table,)), [])

    without = markdown_report(view_of(repo, store, stored, head))

    assert section(without, "Metrics").count("no base run") == 3
    assert f"no base run; create it with `booley measure --all {base}`" in without

    base_values = values(plain={"cell_count": 80, "area_kge": 12.0, "tests_total": 5})
    store.write(
        build_run(
            key_of(repo, base),
            "base",
            (Table("synth", values=base_values),),
            toolchain=fingerprint("sha256:b"),
        ),
        [],
    )
    compared = markdown_report(view_of(repo, store, stored, head))

    assert metric_row(compared, "area_um2")[6] == "no base value"
    assert metric_row(compared, "cell_count")[6] == "+10"
    assert metric_row(compared, "area_kge")[6] == "+0.3"  # not 0.3000000000000007
    assert "base run `base`" in compared
    assert "toolchain differs from the base run: booley" in compared
    assert "worse than base: core synth cell_count_max 80 → 90" in compared

    # The commit on ``main`` is its own base: no Base, no Δ, nothing compared.
    own = store.write(make_run(key_of(repo, base), "own", flow="synth"), [])
    alone = markdown_report(view_of(repo, store, own, base))
    assert metric_row(alone, "cell_count")[6] == "no base"
    assert "- no base: the measured commit" in alone
    assert "- rules not compared: the measured commit" in alone


def test_tests_total_drop_is_warned_once(tmp_path: Path) -> None:
    repo, _, head = feature_repo(tmp_path)
    base = git(repo, "rev-parse", "main")
    store = MeasurementStore(repo / ".booley")
    head_table = Table(
        "sim",
        (Entry("tests_total_min", "tests_total", 8, bound=5),),
        values(plain={"tests_total": 8}),
    )
    base_table = Table(
        "sim",
        (Entry("tests_total_min", "tests_total", 10, bound=5),),
        values(plain={"tests_total": 10}),
    )
    store.write(build_run(key_of(repo, base), "base", (base_table,)), [])
    stored = store.write(build_run(key_of(repo, head), "head", (head_table,)), [])
    view = view_of(repo, store, stored, head)

    markdown = markdown_report(view)

    assert [w["key"] for w in view["view"]["base_run"]["worsened"]] == ["tests_total_min"]
    warnings = section(markdown, "Warnings")
    assert warnings.count("tests_total") == 1
    assert "- `tests_total` dropped: core sim 10 → 8" in warnings
    assert "worse than base" not in markdown


def test_stopped_and_cancelled_runs_say_so_in_the_header(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, _, head = feature_repo(tmp_path)
    store = MeasurementStore(repo / ".booley")
    entries = (
        Entry("cell_count_max", "cell_count", None, bound=100),
        Entry("area_um2_max", "area_um2", None, bound=100),
    )
    monkeypatch.setattr(colors, "COLORS_ENABLED", True)
    for status, headline, line in (
        (
            RunStatus.STOPPED,
            "Run r-stopped: stopped",
            "stopped early: 2 not evaluated, use `--all`",
        ),
        (RunStatus.CANCELLED, "Run r-cancelled: cancelled", "cancelled"),
    ):
        run = build_run(
            key_of(repo, head),
            f"r-{status.value}",
            (Table("synth", entries),),
            status=status,
            verdict=Verdict.NOT_EVALUATED,
        )
        view = view_of(repo, store, store.write(run, []), head)

        markdown = markdown_report(view)
        terminal = terminal_report(view)

        assert headline in section(markdown, "Run")
        assert f"- {line}" in section(markdown, "Run")
        assert "**2 not evaluated of 2 enforced**" in markdown
        assert colors.amber(headline) in terminal
        assert colors.green(headline) not in terminal


def test_markdown_needs_no_path_to_understand_a_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from booley.measurement import run_store

    repo, _, head = feature_repo(tmp_path)
    store = MeasurementStore(repo / ".booley")
    monkeypatch.setattr(run_store, "REPORT_CAP_BYTES", 10)
    sources = tmp_path / "sources"
    for name, size in (("a.rpt", 4), ("big.rpt", 20)):
        write(sources / name, "x" * size)
    reports = [FlowReport("acme:core:1.0#top", "synth", sources / n) for n in ("a.rpt", "big.rpt")]
    table = Table("synth", synth_entries())
    stored = store.write(build_run(key_of(repo, head), "r1", (table,)), reports)

    markdown = markdown_report(view_of(repo, store, stored, head))

    root = str(stored.path.parent)
    assert "too big" in section(markdown, "Failing")
    assert "boom" in section(markdown, "Failing")
    assert markdown.count(root) >= 2
    assert root not in markdown.split("## Files")[0]
    assert "core synth: big.rpt (20 bytes, not kept)" in section(markdown, "Files")
    assert "a.rpt" in section(markdown, "Files")


def test_targets_without_metrics_are_named_on_one_line(tmp_path: Path) -> None:
    repo, _, head = feature_repo(tmp_path)
    store = MeasurementStore(repo / ".booley")
    table = Table("synth", (Entry("cell_count_max", "cell_count", 90, bound=100),))
    stored = store.write(build_run(key_of(repo, head), "r1", (table,)), [])
    targets = ("acme:b:1.0#x", "acme:core:1.0#top", "acme:a:1.0#y")

    view = view_of(repo, store, stored, head, targets=targets)

    line = "Targets with no metrics: acme:b:1.0#x, acme:a:1.0#y"
    for text in (markdown_report(view), terminal_report(view)):
        assert text.count("Targets with no metrics") == 1
        assert line in text
    every = view_of(repo, store, stored, head, targets=("acme:core:1.0#top",))
    assert "Targets with no metrics" not in markdown_report(every)
    assert "Targets with no metrics" not in terminal_report(every)


def test_versioned_booley_directory_header_and_edits(tmp_path: Path) -> None:
    checkout, booley, store, stored = stored_in_versioned_case(tmp_path / "a")
    head = git(checkout, "rev-parse", "HEAD")
    view = view_of(checkout, store, stored, head)

    markdown = markdown_report(view)

    commit = view["view"]["commit"]
    header = section(markdown, "Run")
    assert (
        f"Booley Directory tree `{commit['booley_tree']}` (commit `{commit['booley_commit']}`)"
        in header
    )
    assert f"rules Base `{git(booley, 'rev-parse', 'main')}` (" in header
    assert "Run r1: rules loosened" in header
    assert "protected edits in the Booley Directory: .booley/metrics.toml" in markdown
    assert "synth_core synth area_kge_max: maximum raised, 60 → 80 — (no note)" in markdown

    # The paired Base cannot be chosen: say why instead of an empty rules diff.
    checkout, booley, store, stored = stored_in_versioned_case(tmp_path / "b")
    git(booley, "branch", "-m", "feature", "trunk")
    git(booley, "branch", "-D", "main")
    bare = markdown_report(view_of(checkout, store, stored, git(checkout, "rev-parse", "HEAD")))
    assert "- rules: no base: neither origin/HEAD nor local main exists" in bare
    assert "- rules not compared: neither origin/HEAD nor local main exists" in bare


def busy_view(tmp_path: Path) -> dict[str, Any]:
    """A view with submodule bumps, a tests diff, a failing and a never-started table."""
    repo, _ = in_tree_repository(tmp_path / "repo")
    write(repo / ".booley" / "tests.toml", "a = 1\n")
    git(repo, "add", "-f", ".booley/tests.toml")
    add_gitlink(repo, "ip/sub", "a" * 40)
    add_gitlink(repo, "ip/gone", "d" * 40)
    commit_all(repo, "base", staged_only=True)
    git(repo, "checkout", "-q", "-b", "feature")
    write(repo / ".booley" / "tests.toml", "a = 2\n")
    write(repo / ".booley" / "metrics.toml", "[broken\n")
    git(repo, "add", "-f", ".booley/tests.toml", ".booley/metrics.toml")
    add_gitlink(repo, "ip/sub", "b" * 40)
    add_gitlink(repo, "ip/new", "c" * 40)
    git(repo, "update-index", "--force-remove", "ip/gone")
    head = commit_all(repo, "head\n\nbody with a fence\n```\ncode\n```", staged_only=True)
    keyed = key_of(repo, head)
    store = MeasurementStore(repo / ".booley")
    store.write(make_run(keyed, "r0", minute=-5, verdict="unmet", flow="synth"), [])
    sim = Table(
        "sim",
        (
            Entry(
                "tests_failed_max",
                "tests_failed",
                2,
                bound=0,
                verdict=Verdict.UNMET,
                detail="2 bad",
            ),
        ),
        values(plain={"tests_failed": 2, "tests_total": 9}),
    )
    never = Entry("cell_count_max", "cell_count", None, bound=100, verdict=Verdict.NOT_EVALUATED)
    synth = Table("synth", (never,))
    toolchain = replace(fingerprint(), image="sha256:img", booley_note="dev install")
    run = build_run(keyed, "r1", (sim, synth), toolchain=toolchain)
    run = replace(run, tables=(run.tables[0], replace(run.tables[1], wall_time_s=None)))
    run = replace(run, protected_paths=("ip/sub",))
    stored = store.write(run, [])
    (stored.path.parent / "rX.json").write_bytes(b"{not json")
    return view_of(repo, store, stored, head)


def test_submodules_tests_diff_and_failing_tests_render(tmp_path: Path) -> None:
    view = busy_view(tmp_path)

    markdown = markdown_report(view)
    terminal = terminal_report(view, width=120)

    changed = section(markdown, "Changed files")
    assert f"- submodule ip/sub {'a' * 7}..{'b' * 7} (protected)" in changed
    assert f"- submodule ip/new added {'c' * 7}" in changed
    assert f"- submodule ip/gone removed {'d' * 7}" in changed
    assert "- protected edits: ip/sub" in changed
    assert "- 2 files changed (1 added, 1 modified), 3 submodules" in changed
    rules = section(markdown, "Rules changes")
    assert "- rules file unreadable:" in rules
    assert "- tests changed" in rules
    assert "a = 2" in rules
    assert "````\n" in markdown  # the body's fence is shorter than the one around it
    assert "body with a fence" in section(markdown, "Run")
    assert "- core sim: 2 failed of 9 tests" in section(markdown, "Failing tests")
    assert "- core synth: not started" in section(markdown, "Wall time")
    assert "- core sim: 1s" in section(markdown, "Wall time")
    header = section(markdown, "Run")
    assert "earlier: r0 (unmet)" in header
    assert "- history:" in header
    assert "rX.json" in header
    assert "image sha256:img" in header
    assert "dev install" in header
    assert "    +a = 2" in terminal
    assert "ip/sub" in terminal
    assert "not started" in terminal
