"""The Measurement Run view: the stored run plus what is derived for the asked commit (#3747)."""

from __future__ import annotations

import copy
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from booley.measurement import run_store
from booley.measurement.base import BaseError
from booley.measurement.report import markdown_report
from booley.measurement.run_document import Verdict
from booley.measurement.run_key import resolve_measurement_key
from booley.measurement.run_store import FlowReport, MeasurementStore, StoredRun
from booley.measurement.run_view import TreeMismatchError, run_view
from tests.measurement.test_base_run import Entry, Table, build_run
from tests.measurement.test_rules_diff import push_origin, versioned_case
from tests.measurement.test_run_store import (
    IDENTITY,
    in_tree_repository,
    key_of,
    make_run,
    no_ambient_booley_dir,  # noqa: F401  autouse: no ambient Booley Directory in these tests
)
from tests.runtime.test_commit_export import commit_all, git, write


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
    target: str | None = None,
) -> dict[str, Any]:
    return run_view(repo, store, run, commit, base=base, target=target)


def test_view_holds_the_stored_run_unchanged_and_round_trips(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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


def stored_in_versioned_case(tmp_path: Path) -> tuple[Path, Path, MeasurementStore, StoredRun]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    checkout, booley = versioned_case(tmp_path, "standalone")
    head = git(checkout, "rev-parse", "HEAD")
    store = MeasurementStore(booley)
    run = make_run(key_of(checkout, head), "r1", flow="synth")
    stored = store.write(replace(run, protected_paths=(".booley/metrics.toml",)), [])
    return checkout, booley, store, stored


def stealth_view(
    root: Path, stored_under: dict[str, int], *, on_main: bool = False
) -> tuple[dict[str, Any], str, str]:
    """The view of a Stealth head against an RTL Base whose runs sit under other rules.

    *stored_under* maps ``main`` or ``feature`` (the Booley Directory commit a base run
    was measured under) to the minute it finished. Origin lags the local ``main``.
    Returns the view, the Booley Directory ``main`` commit and the markdown report.
    """
    root.mkdir(parents=True, exist_ok=True)
    checkout, booley = versioned_case(root, "standalone")
    main = git(booley, "rev-parse", "main")
    commits = {"main": main, "feature": git(booley, "rev-parse", "HEAD")}
    push_origin(booley, f"{main}~1")
    first = git(checkout, "rev-parse", "HEAD")
    write(checkout / "rtl.v", "module b; endmodule\n")
    head = commit_all(checkout, "rtl work")
    if on_main:
        git(booley, "checkout", "-q", "-f", "main")
    store = MeasurementStore(booley)
    for name, minute in stored_under.items():
        keyed = resolve_measurement_key(checkout, first, booley_commit=commits[name])
        store.write(make_run(keyed, f"under-{name}", minute=minute, flow="synth"), [])
    stored = store.write(make_run(key_of(checkout, head), "head", minute=9, flow="synth"), [])
    view = view_of(checkout, store, stored, head, base=first)["view"]
    return view, main, markdown_report(view_of(checkout, store, stored, head, base=first))


def test_stealth_rules_base_follows_unpushed_main_and_picks_its_base_run(tmp_path: Path) -> None:
    view, main, _ = stealth_view(tmp_path / "a", {"main": 1, "feature": 2})
    assert view["rules_base"]["commit"] == main
    assert view["rules_base"]["choice"] == "merge-base with local main"
    (change,) = view["rules_diff"]["changes"]
    assert (change["kind"], change["old"], change["new"]) == ("maximum raised", 60, 80)
    assert change["note"] == "(no note)"
    assert view["amber"] == ["rules loosened"]
    assert view["base_run"]["base_run_id"] == "under-main"
    assert view["base_run"]["rules_differ"] is False

    view, _, report = stealth_view(tmp_path / "b", {"feature": 2})
    assert view["base_run"]["base_run_id"] == "under-feature"
    assert view["base_run"]["rules_differ"] is True
    assert "base run `under-feature` (its rules differ)" in report

    # The measured Booley Directory commit is local main: its own rules Base.
    view, _, _ = stealth_view(tmp_path / "c", {"main": 1, "feature": 2}, on_main=True)
    assert view["rules_base"]["commit"] is None
    assert view["base_run"]["base_run_id"] == "under-main"
    assert view["base_run"]["rules_differ"] is False


def test_commit_with_another_tree_is_refused(tmp_path: Path) -> None:
    repo, base, head = feature_repo(tmp_path)
    store = MeasurementStore(repo / ".booley")
    stored = store.write(make_run(key_of(repo, head), "r1", flow="synth"), [])

    with pytest.raises(TreeMismatchError) as refused:
        view_of(repo, store, stored, base)
    assert tree_of(repo, base) in str(refused.value)
    assert tree_of(repo, head) in str(refused.value)
    with pytest.raises(BaseError):
        view_of(repo, store, stored, head, base="no-such-revision")


def test_history_gives_position_earlier_outcomes_and_problems(tmp_path: Path) -> None:
    repo, _, head = feature_repo(tmp_path)
    store = MeasurementStore(repo / ".booley")
    keyed = key_of(repo, head)
    # build_run counts from a later epoch than make_run: -100_000 minutes sorts it first.
    both = build_run(keyed, "r0a", (Table("synth", synth_entries()),), minute=-100_000)
    store.write(both, [])
    store.write(make_run(keyed, "r0b", minute=0, status="stopped", flow="synth"), [])
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

    assert (history["position"], history["total"]) == (5, 6)
    assert history["earlier"] == [
        {"run_id": "r0a", "outcome": "error"},  # error wins over unmet
        {"run_id": "r0b", "outcome": "stopped"},
        {"run_id": "r1", "outcome": "error"},
        {"run_id": "r2", "outcome": "cancelled"},
    ]
    (problem,) = history["problems"]
    assert "rX.json" in problem
    latest = store.readable_history(keyed.key)[0][-1]
    last = view_of(repo, store, latest, head)["view"]["history"]
    assert (last["position"], last["earlier"][-1]) == (6, {"run_id": "r3", "outcome": "met"})


def test_history_carries_an_earlier_run_with_a_damaged_result_as_a_problem(
    tmp_path: Path,
) -> None:
    repo, _, head = feature_repo(tmp_path)
    store = MeasurementStore(repo / ".booley")
    keyed = key_of(repo, head)
    entries = (
        Entry("area_um2_max", "area_um2", 9, bound=5, verdict=Verdict.UNMET, detail="big"),
        Entry("cell_count_max", "cell_count", 90, bound=100),
    )
    damaged = store.write(build_run(keyed, "r1", (Table("synth", entries),), minute=-100_000), [])
    document = json.loads(damaged.path.read_bytes())
    del document["tables"][0]["results"][1]["verdict"]
    damaged.path.write_bytes(json.dumps(document).encode())
    viewed = store.write(make_run(keyed, "r2", minute=2, flow="synth"), [])

    history = view_of(repo, store, viewed, head)["view"]["history"]

    assert history["earlier"] == []
    (problem,) = history["problems"]
    assert "r1.json" in problem


def test_commit_subject_is_the_first_paragraph(tmp_path: Path) -> None:
    repo, _ = in_tree_repository(tmp_path / "repo")
    write(repo / "rtl.v", "module b; endmodule\n")
    head = commit_all(repo, "wrapped\nsubject line\n\nthe body")
    store = MeasurementStore(repo / ".booley")
    stored = store.write(make_run(key_of(repo, head), "r1", flow="synth"), [])

    commit = view_of(repo, store, stored, head)["view"]["commit"]

    assert (commit["subject"], commit["body"]) == ("wrapped subject line", "the body")


def synth_entries() -> tuple[Entry, ...]:
    return (
        Entry("cell_count_max", "cell_count", 90, bound=100),
        Entry("area_um2_max", "area_um2", 9, bound=5, verdict=Verdict.UNMET, detail="too big"),
        Entry("area_kge_max", "area_kge", None, bound=5, verdict=Verdict.ERROR, detail="boom"),
        Entry("wns_ns", "wns_ns", 0.5),
        Entry("whs_ns_min", "whs_ns", None, bound=0, verdict=Verdict.NOT_EVALUATED),
    )
