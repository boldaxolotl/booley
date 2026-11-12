"""The Measurement Run view: the stored run plus what is derived for the asked commit (#3747)."""

from __future__ import annotations

import copy
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from booley.measurement.base import BaseError
from booley.measurement.run_document import RunStatus, Verdict
from booley.measurement.run_store import FlowReport, MeasurementStore, StoredRun
from booley.runtime.pinned_history import FileCommitError
from tests.measurement.test_base_run import Entry, Table, build_run, values
from tests.measurement.test_rules_diff import AREA, SYNTH, versioned_case
from tests.measurement.test_run_store import (
    IDENTITY,
    fingerprint,
    in_tree_repository,
    key_of,
    make_run,
    no_ambient_booley_dir,  # noqa: F401  autouse: no ambient Booley Directory in these tests
)
from tests.runtime.test_commit_export import add_gitlink, commit_all, git, write


def tree_of(repository: Path, commit: str) -> str:
    return git(repository, "rev-parse", f"{commit}^{{tree}}")


def feature_repo(tmp_path: Path) -> tuple[Path, str, str]:
    """An in-tree repository: ``main`` at the Base, ``feature`` one commit later."""
    repo, base = in_tree_repository(tmp_path / "repo")
    git(repo, "checkout", "-q", "-b", "feature")
    write(repo / "rtl.v", "module b; endmodule\n")
    return repo, base, commit_all(repo, "feature work\n\nlonger body")


def view_of(
    repo: Path,
    store: MeasurementStore,
    run: StoredRun,
    commit: str,
    *,
    base: str | None = None,
    targets: tuple[str, ...] = (),
) -> dict[str, Any]:
    from booley.measurement.run_view import run_view

    return run_view(repo, store, run, commit, base=base, targets=targets)


def test_view_holds_the_stored_run_unchanged_and_round_trips(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from booley.measurement import run_store

    repo, _, head = feature_repo(tmp_path)
    store = MeasurementStore(repo / ".booley")
    monkeypatch.setattr(run_store, "REPORT_CAP_BYTES", 10)
    sources = tmp_path / "sources"
    for name, size in (("a.rpt", 4), ("big.rpt", 20)):
        write(sources / name, "x" * size)
    reports = [FlowReport(IDENTITY, "synth", sources / n) for n in ("a.rpt", "big.rpt")]
    keyed = key_of(repo, head)
    stored = store.write(make_run(keyed, "r1", flow="synth"), reports)
    bytes_before = stored.path.read_bytes()
    document_before = copy.deepcopy(stored.document)

    view = view_of(repo, store, stored, head)

    assert {k: v for k, v in view.items() if k != "view"} == json.loads(bytes_before)
    assert json.loads(json.dumps(view)) == view
    assert stored.path.read_bytes() == bytes_before
    assert stored.document == document_before
    latest = store.measured(keyed.key)
    assert latest is not None and latest.run_id == "r1"
    (evidence,) = view["view"]["evidence"]
    kept = stored.path.parent / "r1" / "0-synth" / "a.rpt"
    assert evidence == {
        "identity": IDENTITY,
        "flow": "synth",
        "reports": [str(kept)],
        "dropped": [{"name": "big.rpt", "bytes": 20}],
    }
    assert kept.is_file()
    assert view["view"]["paths"] == {
        "json": str(stored.path),
        "markdown": str(stored.path.with_suffix(".md")),
    }
    commit = view["view"]["commit"]
    assert (commit["sha"], commit["tree"], commit["measured_as"]) == (
        head,
        tree_of(repo, head),
        None,
    )
    assert (commit["subject"], commit["body"]) == ("feature work", "longer body")
    assert (commit["booley_commit"], commit["booley_tree"]) == (None, None)


def test_identical_tree_commit_records_measured_as_and_its_own_base_and_diffs(
    tmp_path: Path,
) -> None:
    repo, base_a = in_tree_repository(tmp_path / "repo")
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
    second = git(
        repo, "commit-tree", tree_of(repo, first), "-p", new_base, "-m", "same tree, new parent"
    )
    store = MeasurementStore(repo / ".booley")
    stored = store.write(make_run(key_of(repo, first), "r1", flow="synth"), [])

    view = view_of(repo, store, stored, second)["view"]
    own = view_of(repo, store, stored, first)["view"]

    assert view["commit"]["measured_as"] == first
    assert (view["commit"]["sha"], view["commit"]["tree"]) == (second, tree_of(repo, first))
    assert view["base"]["commit"] == new_base
    assert [f["path"] for f in view["changed"]["files"]] == [".booley/metrics.toml"]
    (change,) = view["rules_diff"]["changes"]
    assert (change["old"], change["new"], change["kind"]) == (70, 80, "maximum raised")
    assert own["commit"]["measured_as"] is None
    assert own["base"]["commit"] == base_a
    assert sorted(f["path"] for f in own["changed"]["files"]) == [
        ".booley/metrics.toml",
        "rtl.v",
    ]
    (own_change,) = own["rules_diff"]["changes"]
    assert (own_change["old"], own_change["new"]) == (60, 80)


def test_two_bases_give_two_base_blocks_and_the_same_verdicts(tmp_path: Path) -> None:
    repo, base, first = feature_repo(tmp_path)
    write(repo / "rtl.v", "module c; endmodule\n")
    head = commit_all(repo, "second")
    store = MeasurementStore(repo / ".booley")
    stored = store.write(make_run(key_of(repo, head), "r1", flow="synth"), [])

    default = view_of(repo, store, stored, head)
    chosen = view_of(repo, store, stored, head, base=first)

    assert default["view"]["base"]["commit"] == base
    assert chosen["view"]["base"] == {
        "commit": first,
        "choice": "--base, used as given",
        "requested": first,
        "no_base": None,
    }
    assert default["view"]["counts"] == chosen["view"]["counts"]
    assert default["view"]["excerpts"] == chosen["view"]["excerpts"]
    assert default["tables"] == chosen["tables"]
    assert (default["view"]["base_run"]["command"], chosen["view"]["base_run"]["command"]) == (
        f"booley measure --all {base}",
        f"booley measure --all {first}",
    )


def stored_in_versioned_case(tmp_path: Path) -> tuple[Path, Path, MeasurementStore, StoredRun]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    checkout, booley = versioned_case(tmp_path, "standalone")
    head = git(checkout, "rev-parse", "HEAD")
    store = MeasurementStore(booley)
    run = make_run(key_of(checkout, head), "r1", flow="synth")
    stored = store.write(replace(run, protected_paths=(".booley/metrics.toml",)), [])
    return checkout, booley, store, stored


def test_versioned_booley_directory_names_the_rules_base(tmp_path: Path) -> None:
    checkout, booley, store, stored = stored_in_versioned_case(tmp_path / "a")
    head = git(checkout, "rev-parse", "HEAD")

    view = view_of(checkout, store, stored, head)["view"]

    assert view["rules_base"]["commit"] == git(booley, "rev-parse", "main")
    (change,) = view["rules_diff"]["changes"]
    assert (change["kind"], change["old"], change["new"]) == ("maximum raised", 60, 80)
    assert (change["direction"], change["note"]) == ("loosened", "(no note)")
    assert view["amber"] == ["rules loosened"]
    assert view["protected_edits"]["booley_directory"] == [".booley/metrics.toml"]
    assert view["rules_diff"]["not_compared"] is None
    assert view["commit"]["booley_commit"] == git(booley, "rev-parse", "HEAD")
    # A later paired commit moves HEAD; the stored one still decides.
    write(booley / "metrics.toml", SYNTH + "area_kge_max = 999\n")
    git(booley, "add", "metrics.toml")
    git(booley, "commit", "-q", "-m", "later")
    later = view_of(checkout, store, stored, head)["view"]
    assert later["rules_diff"] == view["rules_diff"]

    # No Base for the paired commit: say why instead of an empty rules diff.
    checkout, booley, store, stored = stored_in_versioned_case(tmp_path / "b")
    git(booley, "branch", "-m", "feature", "trunk")
    git(booley, "branch", "-D", "main")
    reason = "neither origin/HEAD nor local main exists"
    bare = view_of(checkout, store, stored, git(checkout, "rev-parse", "HEAD"))["view"]
    assert bare["rules_base"]["no_base"] == reason
    assert bare["rules_diff"]["not_compared"] == reason
    assert (bare["rules_diff"]["changes"], bare["amber"]) == ([], [])

    # A paired commit that no longer exists is not a "shares no history" case.
    checkout, _, store, stored = stored_in_versioned_case(tmp_path / "c")
    document = json.loads(stored.path.read_text(encoding="utf-8"))
    document["booley_commit"] = "c" * 40
    stored.path.write_bytes(json.dumps(document).encode())
    (gone,) = store.history(stored.key)
    with pytest.raises(FileCommitError):
        view_of(checkout, store, gone, git(checkout, "rev-parse", "HEAD"))


def test_commit_with_another_tree_is_refused(tmp_path: Path) -> None:
    from booley.measurement.run_view import TreeMismatchError

    repo, base, head = feature_repo(tmp_path)
    store = MeasurementStore(repo / ".booley")
    stored = store.write(make_run(key_of(repo, head), "r1", flow="synth"), [])

    with pytest.raises(TreeMismatchError) as refused:
        view_of(repo, store, stored, base)
    assert tree_of(repo, base) in str(refused.value)
    assert tree_of(repo, head) in str(refused.value)
    with pytest.raises(BaseError):
        view_of(repo, store, stored, head, base="no-such-revision")


def test_delta_reads_no_base_run_then_no_base_value(tmp_path: Path) -> None:
    repo, base, head = feature_repo(tmp_path)
    store = MeasurementStore(repo / ".booley")
    tables = (
        Table(
            "synth",
            (
                Entry("cell_count_max", "cell_count", 90, bound=100),
                Entry("area_um2", "area_um2", 12.0),
            ),
            values(plain={"cell_count": 90, "tests_total": 5}),
        ),
    )
    stored = store.write(build_run(key_of(repo, head), "head", tables), [])

    no_base_run = view_of(repo, store, stored, head)["view"]["base_run"]
    assert {d["missing"] for d in no_base_run["deltas"]} == {"no base run"}
    assert no_base_run["command"] == f"booley measure --all {base}"

    store.write(
        build_run(
            key_of(repo, base),
            "base",
            (
                Table(
                    "synth",
                    values=values(plain={"cell_count": 80, "tests_total": 10}),
                ),
            ),
            toolchain=fingerprint("sha256:b"),
        ),
        [],
    )
    compared = view_of(repo, store, stored, head)["view"]["base_run"]
    by_key = {d["key"]: d for d in compared["deltas"]}
    assert (by_key["cell_count_max"]["delta"], by_key["cell_count_max"]["missing"]) == (10, None)
    assert (by_key["area_um2"]["delta"], by_key["area_um2"]["missing"]) == (None, "no base value")
    assert [w["key"] for w in compared["worsened"]] == ["cell_count_max"]
    assert [(t["base_total"], t["total"]) for t in compared["tests_dropped"]] == [(10, 5)]
    assert compared["toolchain_changes"] == ["booley"]
    assert (compared["base_run_id"], compared["problem"]) == ("base", None)

    # The commit on ``main`` is its own base: nothing to compare with.
    own_run = store.write(make_run(key_of(repo, base), "own", flow="synth"), [])
    own = view_of(repo, store, own_run, base)["view"]
    assert {d["missing"] for d in own["base_run"]["deltas"]} == {"no base"}
    assert own["base"]["commit"] is None
    assert own["changed"] is None
    assert "is its own base" in own["rules_diff"]["not_compared"]


def test_history_gives_position_earlier_outcomes_and_problems(tmp_path: Path) -> None:
    repo, _, head = feature_repo(tmp_path)
    store = MeasurementStore(repo / ".booley")
    keyed = key_of(repo, head)
    store.write(make_run(keyed, "r1", minute=1, verdict="error", flow="synth"), [])
    store.write(
        make_run(keyed, "r2", minute=2, status="cancelled", verdict="error", flow="synth"), []
    )
    viewed = store.write(make_run(keyed, "r3", minute=3, flow="synth"), [])
    store.write(make_run(keyed, "r4", minute=4, verdict="unmet", flow="synth"), [])
    other_rules = replace(keyed, key=replace(keyed.key, booley_tree="f" * 40))
    store.write(make_run(other_rules, "other", minute=2, flow="synth"), [])
    broken = viewed.path.parent / "rX.json"
    broken.write_bytes(b"{not json")

    history = view_of(repo, store, viewed, head)["view"]["history"]

    assert (history["position"], history["total"]) == (3, 4)
    assert history["earlier"] == [
        {"run_id": "r1", "outcome": "error"},
        {"run_id": "r2", "outcome": "cancelled"},
    ]
    (problem,) = history["problems"]
    assert "rX.json" in problem
    latest = store.readable_history(keyed.key)[0][-1]
    last = view_of(repo, store, latest, head)["view"]["history"]
    assert (last["position"], last["earlier"][-1]) == (4, {"run_id": "r3", "outcome": "met"})


def synth_entries() -> tuple[Entry, ...]:
    return (
        Entry("cell_count_max", "cell_count", 90, bound=100),
        Entry("area_um2_max", "area_um2", 9, bound=5, verdict=Verdict.UNMET, detail="too big"),
        Entry("area_kge_max", "area_kge", None, bound=5, verdict=Verdict.ERROR, detail="boom"),
        Entry("wns_ns", "wns_ns", 0.5),
        Entry("whs_ns_min", "whs_ns", None, bound=0, verdict=Verdict.NOT_EVALUATED),
    )


def test_verdict_counts_and_excerpts_match_the_stored_results(tmp_path: Path) -> None:
    repo, _, head = feature_repo(tmp_path)
    store = MeasurementStore(repo / ".booley")
    stored = store.write(
        build_run(key_of(repo, head), "r1", (Table("synth", synth_entries()),)), []
    )

    view = view_of(repo, store, stored, head)["view"]

    assert view["counts"] == {
        "met": 1,
        "unmet": 1,
        "error": 1,
        "tracked": 1,
        "not_evaluated": 1,
        "enforced": 4,
    }
    assert [(e["key"], e["verdict"], e["excerpt"]) for e in view["excerpts"]] == [
        ("area_um2_max", "unmet", "too big"),
        ("area_kge_max", "error", "boom"),
    ]


@pytest.mark.parametrize(
    "status", [RunStatus.STOPPED, RunStatus.CANCELLED], ids=["stopped", "cancelled"]
)
def test_stopped_and_cancelled_runs_carry_status_and_not_evaluated(
    tmp_path: Path, status: RunStatus
) -> None:
    repo, _, head = feature_repo(tmp_path)
    store = MeasurementStore(repo / ".booley")
    table = Table(
        "synth",
        (
            Entry("cell_count_max", "cell_count", None, bound=100),
            Entry("area_um2_max", "area_um2", None, bound=100),
        ),
    )
    run = build_run(
        key_of(repo, head), "r1", (table,), status=status, verdict=Verdict.NOT_EVALUATED
    )
    stored = store.write(run, [])

    view = view_of(repo, store, stored, head)

    assert view["status"] == status.value
    assert view["view"]["counts"]["not_evaluated"] == 2
    assert view["view"]["history"]["earlier"] == []


def test_target_without_a_table_is_listed_in_given_order(tmp_path: Path) -> None:
    repo, _, head = feature_repo(tmp_path)
    store = MeasurementStore(repo / ".booley")
    table = Table("synth", (Entry("cell_count_max", "cell_count", 90, bound=100),))
    stored = store.write(build_run(key_of(repo, head), "r1", (table,)), [])
    targets = ("acme:b:1.0#x", "acme:core:1.0#top", "acme:other:1.0#top", "acme:a:1.0#x")

    view = view_of(repo, store, stored, head, targets=targets)["view"]

    assert view["targets_without_table"] == ["acme:b:1.0#x", "acme:other:1.0#top", "acme:a:1.0#x"]


def test_changed_files_mark_protected_constraint_and_submodule_bumps(tmp_path: Path) -> None:
    repo, _ = in_tree_repository(tmp_path / "repo")
    write(repo / ".booley" / "tests.toml", "a = 1\n")
    git(repo, "add", "-f", ".booley/tests.toml")
    add_gitlink(repo, "ip/sub")
    commit_all(repo, "base", staged_only=True)
    git(repo, "checkout", "-q", "-b", "feature")
    write(repo / "rtl.v", "module b; endmodule\n")
    write(repo / "top.sdc", "create_clock\n")
    write(repo / ".booley" / "tests.toml", "a = 2\n")
    git(repo, "add", "-f", "rtl.v", "top.sdc", ".booley/tests.toml")
    add_gitlink(repo, "ip/sub", "b" * 40)
    head = commit_all(repo, "head", staged_only=True)
    store = MeasurementStore(repo / ".booley")
    run = replace(
        make_run(key_of(repo, head), "r1", flow="synth"),
        protected_paths=(".booley/tests.toml", "ip/sub"),
    )
    stored = store.write(run, [])

    view = view_of(repo, store, stored, head)["view"]

    changed = view["changed"]
    assert [
        (f["path"], f["status"], f["protected"], f["constraint"]) for f in changed["files"]
    ] == [
        (".booley/tests.toml", "M", True, False),
        ("rtl.v", "M", False, False),
        ("top.sdc", "A", False, True),
    ]
    (submodule,) = changed["submodules"]
    assert (submodule["path"], submodule["old"], submodule["new"]) == (
        "ip/sub",
        "a" * 40,
        "b" * 40,
    )
    assert (submodule["files"], submodule["protected"]) == (None, True)
    assert (changed["file_count"], changed["submodule_count"]) == (3, 1)
    assert view["protected_edits"]["repository"] == [".booley/tests.toml", "ip/sub"]
    assert "a = 2" in view["rules_diff"]["tests_diff"]
    assert view["amber"] == ["tests changed"]
