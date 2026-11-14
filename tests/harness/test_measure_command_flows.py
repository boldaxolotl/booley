"""``booley measure <sha>`` runs real child Flows in an exported checkout and stores the run (#3953).

The Project is a committed repository whose Targets are named for the stub Flow's behaviors
(``tests/harness/test_checkout_flow.py``): a target starting with ``metrics`` reports one typed
metric (``marker`` = 7), ``crash`` raises. The command runs through the real entry point; the
children are real processes. The command's refusals and cancellation, which need no process, are
in ``test_measure_cli.py``.
"""

from __future__ import annotations

import json
import sys

import pytest

from booley.harness import booley as cli
from booley.harness.standalone_job import standalone_jobs_dir
from booley.measurement import metrics_file
from booley.measurement.run_key import resolve_measurement_key
from booley.measurement.run_store import MeasurementStore
from booley.runtime import job_records
from tests.harness.test_checkout_flow import (
    FLOW,
    World,
    WorldBuilder,
    git,
    linux_only,
    make_world,
    use_fusesoc,
)
from tests.harness.test_measure_children_flows import declare_stub_metrics, started_targets

__all__ = ["make_world"]

# A Target whose name tells the stub Flow what to do; each is a one-file FuseSoC core.
CORE = """CAPI=2:
name: acme:ip:{name}:0.1
filesets:
  rtl:
    files: [top.v]
    file_type: verilogSource
targets:
  {name}:
    filesets: [rtl]
    toplevel: top
    default_tool: icarus
"""
TARGETS = ("metrics_ok", "metrics_b", "crash")
# The queue of two leaves a window of one child: the tables run one after another.
ONE_AT_A_TIME = "queue_max = 2\n"


def metrics_project(make_world: WorldBuilder, metrics: str, config: str = ONE_AT_A_TIME) -> World:
    """A committed Project with the Targets above and *metrics* as its ``metrics.toml``."""
    files = {f"{name}.core": CORE.format(name=name) for name in TARGETS}
    files["top.v"] = "module top; endmodule\n"
    files[".booley/metrics.toml"] = metrics
    return make_world(files=files, config=config)


@pytest.fixture(autouse=True)
def _stub_tables(monkeypatch: pytest.MonkeyPatch) -> None:
    """The stub Flow gets a metric table, and FuseSoC resolves the cores for its preflight."""
    use_fusesoc(monkeypatch)
    declare_stub_metrics(monkeypatch)
    monkeypatch.setattr(metrics_file, "TABLE_FLOWS", (*metrics_file.TABLE_FLOWS, FLOW))


def measure(
    monkeypatch: pytest.MonkeyPatch, world: World, *arguments: str, entry: str = "_dispatch_main"
) -> int:
    """Run ``booley -C <live Project> measure <arguments>`` in a Sandbox."""
    monkeypatch.setattr(cli.runtime_context, "inside_session_runtime", lambda: True)
    monkeypatch.setattr(cli, "_host_install_authority_error", lambda *_: None)
    monkeypatch.setattr(sys, "argv", ["booley", "-C", str(world.live), "measure", *arguments])
    return getattr(cli, entry)()


def store_of(world: World) -> MeasurementStore:
    return MeasurementStore(world.main)


def head_key(world: World):
    return resolve_measurement_key(world.live, git(world.live, "rev-parse", "HEAD")).key


def stored_documents(world: World) -> list[dict[str, object]]:
    paths = sorted((world.main / "measurements").glob("*/*.json"))
    return [json.loads(path.read_text(encoding="utf-8")) for path in paths]


def measure_records(world: World) -> list[job_records.JobRecord]:
    records = job_records.list_records(standalone_jobs_dir(world.main), skip_malformed=True)
    return [record for record in records if record.endpoint == "measure"]


@linux_only
def test_a_met_commit_exits_zero_stores_one_run_and_is_measured(
    make_world: WorldBuilder, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    world = metrics_project(make_world, "[metrics_ok.checkout_stub]\nmarker_max = 10\n")

    assert measure(monkeypatch, world) == 0

    (record,) = measure_records(world)
    assert (record.status, record.exit_code) == ("done", 0)
    key = head_key(world)
    (stored,) = store_of(world).history(key)
    assert stored.run_id == record.run_id
    assert store_of(world).measured(key) == stored
    assert stored.path.with_suffix(".md").is_file()  # the Markdown report
    assert "Measurement Report" in capsys.readouterr().out
    # Each child's text reports are stored beside the document.
    (kept,) = [d["reports"] for d in stored_documents(world)]
    assert any(report["name"] == "report.json" for report in kept)  # type: ignore[union-attr]
    for report in kept:  # type: ignore[union-attr]
        assert (stored.path.parent / report["path"]).is_file()


@linux_only
@pytest.mark.parametrize("all_tables", [False, True], ids=["stop", "all"])
def test_an_unmet_bound_stops_later_tables_unless_all(
    make_world: WorldBuilder, monkeypatch: pytest.MonkeyPatch, all_tables: bool
) -> None:
    world = metrics_project(
        make_world,
        "[metrics_ok.checkout_stub]\nmarker_max = 5\n"
        '[metrics_b.checkout_stub]\ntrack = ["marker"]\n',
    )

    assert measure(monkeypatch, world, *(["--all"] if all_tables else [])) == 1

    assert len(stored_documents(world)) == 1
    (stored,) = store_of(world).history(head_key(world))
    tables = {t["identity"].split("#")[1]: t for t in stored.document["tables"]}
    assert [r["verdict"] for r in tables["metrics_ok"]["results"]] == ["unmet"]
    assert not stored.measured
    if all_tables:
        assert started_targets(world) == ["metrics_ok", "metrics_b"]
        assert [r["verdict"] for r in tables["metrics_b"]["results"]] == ["tracked"]
        assert stored.status == "completed"
    else:
        assert started_targets(world) == ["metrics_ok"]  # the second child never started
        assert [r["verdict"] for r in tables["metrics_b"]["results"]] == ["not_evaluated"]
        assert stored.status == "stopped"


@linux_only
def test_a_crashing_flow_gives_error_verdicts_with_an_excerpt_and_exits_two(
    make_world: WorldBuilder, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    world = metrics_project(make_world, "[crash.checkout_stub]\nmarker_max = 10\n")

    assert measure(monkeypatch, world) == 2

    assert len(stored_documents(world)) == 1
    (stored,) = store_of(world).history(head_key(world))
    (table,) = stored.document["tables"]
    assert [r["verdict"] for r in table["results"]] == ["error"]
    assert "stub crashed" in table["results"][0]["detail"]
    assert "stub crashed" in capsys.readouterr().out  # the report carries the excerpt
    assert not stored.measured


@linux_only
def test_a_retired_key_in_the_commit_does_not_stop_its_measurement(
    make_world: WorldBuilder, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # The commit's own ``booley.toml`` holds a retired scheduler key; the live one does not.
    world = metrics_project(
        make_world,
        "[metrics_ok.checkout_stub]\nmarker_max = 10\n",
        config=ONE_AT_A_TIME + "max_heavy = 1\n",
    )
    live_settings = world.main / "booley.toml"
    live_settings.write_bytes(b"[jobs]\nmax_light = 1\nqueue_max = 2\n")

    assert measure(monkeypatch, world) == 0
    assert len(stored_documents(world)) == 1

    live_settings.write_bytes(b"[jobs]\nmax_light = 1\nmax_heavy = 1\n")
    capsys.readouterr()
    assert measure(monkeypatch, world, entry="main") == 2
    assert "max_heavy" in capsys.readouterr().err
    assert len(stored_documents(world)) == 1  # nothing new was stored
