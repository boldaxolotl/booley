"""The metrics listing: each Target's runnable Flows with the keys ``metrics.toml`` accepts."""

from __future__ import annotations

from pathlib import Path

import pytest

from booley.core.booley_dir import reset_cache
from booley.flows.metric_declarations import Better
from booley.measurement.flow_metrics import metric_declarations
from booley.measurement.metrics_file import load_metrics_rules
from booley.measurement.metrics_listing import render_metrics_listing
from booley.measurement.run_key import KeyedCommit, MeasurementKey
from booley.measurement.run_store import MeasurementStore
from booley.runtime.project_gitignore import PROJECT_GITIGNORE
from booley.targets.catalog import TargetCatalog
from booley.targets.domain import FuseSocError, StaleTargetCatalogError, TargetHandle
from tests.measurement.metrics_project_support import fill_listing, write_listing_project
from tests.measurement.test_run_store import make_run
from tests.runtime.test_commit_export import commit_all, git, new_repository, write

SYNTH_IDENTITY = "::synth_core:0#synth_core"
CLOCK_FALLBACK = (
    "# Clock names appear after the first measurement; a single-clock design needs no prefix."
)


@pytest.fixture(autouse=True)
def no_ambient_booley_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    """Drop the env override and the process cache another test warmed."""
    monkeypatch.delenv("BOOLEY_DIR", raising=False)
    reset_cache()


def listing_of(project: Path) -> str:
    return render_metrics_listing(project)


def block(listing: str, header: str) -> str:
    """The lines of the block that starts with *header*, up to the next blank line."""
    for candidate in listing.split("\n\n"):
        if candidate.splitlines()[0] == header:
            return candidate
    raise AssertionError(f"no block {header!r} in:\n{listing}")


def test_each_target_lists_its_flows_with_every_metric_and_unit(tmp_path: Path) -> None:
    listing = listing_of(write_listing_project(tmp_path))

    assert block(listing, "[sim_core.sim]") == (
        "[sim_core.sim]\n"
        "tests_failed_max =  # tests, plain\n"
        "tests_total_min =  # tests, plain\n"
        '"smoke.cycle_count_max" =  # cycles, per-test\n'
        '"stress.cycle_count_max" =  # cycles, per-test\n'
        '"stress+soak.cycle_count_max" =  # cycles, per-test\n'
        '"nightly.cycle_count_max" =  # cycles, per-test, skipped by default'
    )
    assert block(listing, "[sim_core.coverage]") == (
        "[sim_core.coverage]\n"
        "tests_failed_max =  # tests, plain\n"
        "tests_total_min =  # tests, plain\n"
        "line_coverage_min =  # %, plain\n"
        "branch_coverage_min =  # %, plain\n"
        "expression_coverage_min =  # %, plain\n"
        "toggle_coverage_min =  # %, plain\n"
        "cover_property_coverage_min =  # %, plain"
    )
    synth = block(listing, "[synth_core.synth]")
    assert synth.splitlines()[0] == "[synth_core.synth]"
    assert synth.count(CLOCK_FALLBACK) == 1
    assert "fmax_mhz_min =  # MHz, per-clock" in synth

    # Every declared synth metric is listed with its bound direction.
    for declaration in metric_declarations("synth"):
        limit = "max" if declaration.better is Better.LOWER else "min"
        assert f"{declaration.name}_{limit} =  # {declaration.unit}, " in synth
    # The Icarus Target runs the Simulation Flow only; coverage needs Verilator.
    assert "[sim_icarus.sim]" in listing
    assert "[sim_icarus.coverage]" not in listing
    assert "[synth_core.sim]" not in listing


def test_simulation_targets_list_a_mutation_table_with_its_settings(tmp_path: Path) -> None:
    """AC4: a Simulation Target lists ``mutation`` with its metrics and its settings."""
    listing = listing_of(write_listing_project(tmp_path))

    assert block(listing, "[sim_core.mutation]") == (
        "[sim_core.mutation]\n"
        "mutation_score_min =  # %, plain\n"
        "mutants_detected_min =  # Mutants, plain\n"
        "mutants_survived_max =  # Mutants, plain\n"
        "count = 10  # setting: Mutants to score\n"
        "seed = 0  # setting: selects the Mutants"
    )
    assert "[sim_icarus.mutation]" in listing
    assert "[synth_core.mutation]" not in listing


def test_per_test_keys_are_quoted_selectors_of_one_test_run(tmp_path: Path) -> None:
    project = write_listing_project(tmp_path)
    listing = listing_of(project)

    sim_core = block(listing, "[sim_core.sim]")
    assert '"stress+soak.cycle_count_max" =  #' in sim_core
    assert "stress+short" not in listing  # the default Variant is the bare Test name
    assert "@" not in listing  # never a seed
    # A Target with no registered Tests prints the key of its one unnamed run.
    assert (
        block(listing, "[sim_icarus.sim]").splitlines()[3]
        == "cycle_count_max =  # cycles, per-test"
    )
    # An ambiguous bare name prints its minimal qualified selector.
    assert '["a:x#shared".sim]' in listing
    assert '["b:x#shared".sim]' in listing

    (project / ".booley" / "metrics.toml").write_bytes(fill_listing(listing).encode())

    rules = load_metrics_rules(project)  # every printed key resolves to one Test Run
    tables = {(table.target, table.flow): table for table in rules.tables}
    runs = {
        bound.key: bound.test_run
        for bound in tables["sim_core", "sim"].bounds
        if bound.metric == "cycle_count"
    }
    assert runs == {
        "smoke.cycle_count_max": "smoke@1",
        "stress.cycle_count_max": "stress+short@1",
        "stress+soak.cycle_count_max": "stress+soak@1",
        "nightly.cycle_count_max": "nightly@1",
    }


def store_synth_run(project: Path) -> None:
    """Store a synth run of ``synth_core`` that reported two clocks."""
    keyed = KeyedCommit(MeasurementKey("0" * 40, None), "1" * 40, "m", (), None)
    run = make_run(
        keyed,
        "r1",
        identity=SYNTH_IDENTITY,
        flow="synth",
        per_clock={"fmax_mhz": {"clk_i": 1.0, "clk_b": 2.0}},
    )
    MeasurementStore(project / ".booley").write(run, [])


def test_clock_names_come_from_the_stored_measurement_run(tmp_path: Path) -> None:
    project = write_listing_project(tmp_path)
    store_synth_run(project)

    synth = block(listing_of(project), "[synth_core.synth]")

    assert CLOCK_FALLBACK not in synth
    assert '"clk_b.fmax_mhz_min" =  # MHz, per-clock' in synth
    assert '"clk_i.fmax_mhz_min" =  # MHz, per-clock' in synth
    assert "\nfmax_mhz_min" not in synth

    # The printed clock-prefixed keys load back as bounds of the synth table.
    (project / ".booley" / "metrics.toml").write_bytes(fill_listing(listing_of(project)).encode())
    table = {(t.target, t.flow): t for t in load_metrics_rules(project).tables}[
        "synth_core", "synth"
    ]
    assert {bound.key for bound in table.bounds} >= {"clk_b.fmax_mhz_min", "clk_i.fmax_mhz_min"}


@pytest.mark.parametrize(
    ("answer", "reason"),
    [
        pytest.param(None, "no such Target", id="refused"),
        pytest.param("sim_fifo", "it selects ::sim_fifo:0#sim_fifo", id="another-target"),
    ],
)
def test_a_target_no_selector_reaches_gets_a_comment_instead_of_blocks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, answer: str | None, reason: str
) -> None:
    project = write_listing_project(tmp_path)
    real_select = TargetCatalog.select

    def select(self: TargetCatalog, token: str, **options: str) -> TargetHandle:
        if token != "sim_icarus":
            return real_select(self, token, **options)
        if answer is None:
            raise FuseSocError("no such\nTarget")
        return real_select(self, answer, **options)

    monkeypatch.setattr(TargetCatalog, "select", select)

    listing = listing_of(project)

    assert (
        f"# ::sim_icarus:0#sim_icarus: no metrics.toml selector reaches this Target: {reason}"
        in listing
    )
    assert "[sim_icarus." not in listing
    assert "[sim_core.sim]" in listing  # the other Targets are still listed


def test_a_project_without_a_runnable_target_says_so(tmp_path: Path) -> None:
    (tmp_path / ".booley").mkdir()
    (tmp_path / ".booley" / "booley.toml").write_bytes(b"[project]\n")

    assert listing_of(tmp_path) == "# No Target runs a Flow that declares metrics."


def test_damaged_stored_run_falls_back_to_the_unprefixed_key(tmp_path: Path) -> None:
    project = write_listing_project(tmp_path)
    run_dir = project / ".booley" / "measurements" / "damaged"
    run_dir.mkdir(parents=True)
    (run_dir / "run.json").write_bytes(b"{}")

    assert CLOCK_FALLBACK in block(listing_of(project), "[synth_core.synth]")


# _table_selector: render_metrics_listing builds its catalog and selects from it in one
# call, so no public entry point can edit a core file in between, and patching
# TargetCatalog.select to raise would replace Booley code instead.
def test_stale_target_catalog_is_not_swallowed(tmp_path: Path) -> None:
    from booley.measurement.metrics_listing import _table_selector

    project = write_listing_project(tmp_path)
    catalog = TargetCatalog.build(project)
    handle = catalog.list()[0]
    (project / "synth_core.core").write_bytes(b"CAPI=2:\nname: ::synth_core:0\n")

    with pytest.raises(StaleTargetCatalogError):
        _table_selector(catalog, handle)


def test_linked_worktree_lists_clock_names_of_the_main_checkouts_store(tmp_path: Path) -> None:
    repo = write_listing_project(new_repository(tmp_path / "repo"))
    write(repo / ".booley" / ".gitignore", PROJECT_GITIGNORE)
    head = commit_all(repo)
    linked = tmp_path / "linked"
    git(repo, "worktree", "add", "-q", "-b", "feature", str(linked), head)
    store_synth_run(repo)
    reset_cache()  # in real use the linked worktree is another process

    synth = block(listing_of(linked), "[synth_core.synth]")

    assert CLOCK_FALLBACK not in synth
    assert '"clk_i.fmax_mhz_min" =  # MHz, per-clock' in synth
