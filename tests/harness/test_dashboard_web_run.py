"""`booley dashboard --web`: the Run form's choices as JSON, behind the `web-run-panel` flag."""

from __future__ import annotations

import json
import shutil

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from booley.config.feature_flags import enabled_features
from booley.harness import booley as tlr
from booley.harness.dashboard import launch, run_form
from tests.harness import test_dashboard_web as web_tests
from tests.harness.test_dashboard_run import FLOW_NAMES, write_config, write_picker_project
from tests.harness.test_dashboard_run_pickers import SIM_FIFO_RUNS, TARGETS_BY_FLOW
from tests.harness.test_dashboard_specialist_form import write_coverage_record
from tests.harness.test_dashboard_web import _job, _web
from tests.uvicorn_support import capture_serving

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")

# The Project fixture of the snapshot tests, shared by assignment so a test finds it by name.
world = web_tests.world

FLAG = "web-run-panel"
# Every Run route with the parameters that make it answer for a worktree.
ROUTES = {
    "entries": {},
    "targets": {"flow": "sim"},
    "tests": {"target": "sim_fifo"},
    "form": {"kind": "flow", "name": "sim"},
}


def _app(world, features):
    return _web().create_app(
        world.main, world.booley, proc_root=world.proc, cgroup=world.cgroup, features=features
    )


def _client(world, feature_flags) -> TestClient:
    feature_flags.enable(world.booley, FLAG)
    return TestClient(_app(world, enabled_features(world.main)))


def _get(client, route, worktree, **params):
    response = client.get(f"/api/run/{route}", params={"worktree": str(worktree), **params})
    assert response.status_code == 200, response.text
    return response.json()


def test_entries_list_the_flows_and_specialists_the_worktree_enables(world, feature_flags):
    write_config(
        world.wt,
        "[flows.fpga]\nenabled = false\n[specialists.coverage_analyst]\nenabled = false\n",
    )
    write_config(world.other, "not = [valid")
    with _client(world, feature_flags) as client:
        wt, main, broken = (
            _get(client, "entries", w) for w in (world.wt, world.main, world.other)
        )

    assert [flow["name"] for flow in wt["flows"]] == ["lint", "mutation", "sim", "synth"]
    assert [item["name"] for item in wt["specialists"]] == ["reviewer"]
    assert all(item["blurb"] for item in (*wt["flows"], *wt["specialists"]))
    assert wt["specialists_problem"] == ""
    assert [flow["name"] for flow in main["flows"]] == FLOW_NAMES
    assert [item["name"] for item in main["specialists"]] == ["coverage_analyst", "reviewer"]
    assert broken["specialists"] == []
    assert broken["specialists_problem"].startswith("EndpointConfigError")


def test_targets_equal_booley_targets_for_and_tests_list_variants(world, feature_flags, capsys):
    write_picker_project(world.wt)
    with _client(world, feature_flags) as client:
        for flow in FLOW_NAMES:
            body = _get(client, "targets", world.wt, flow=flow)
            argv = ["targets", "--for", "sim" if flow == "mutation" else flow, "--json"]
            assert tlr._cmd_targets(tlr._build_parser().parse_args(argv), world.wt) == 0
            surface = json.loads(capsys.readouterr().out)
            cli = [t["selector"] for core in surface["cores"] for t in core["targets"]]
            assert body == {"targets": TARGETS_BY_FLOW[flow], "problem": ""}
            assert sorted(body["targets"]) == sorted(cli)
        tests = _get(client, "tests", world.wt, target="sim_fifo")

    assert tests == {"runs": SIM_FIFO_RUNS, "problem": ""}


def test_flow_form_places_every_option_with_the_extra_arguments(world, feature_flags, monkeypatch):
    with _client(world, feature_flags) as client:
        for flow in FLOW_NAMES:
            body = _get(client, "form", world.wt, kind="flow", name=flow)
            shown = {s for field in body["fields"] for s in field["spellings"]}
            listed = {s for spellings in body["extra"]["options"] for s in spellings}
            assert shown | listed == launch.form(flow).placed()
            assert body["extra"]["hint"] == launch.form(flow).extra_hint()
            assert body["extra"]["hint"].startswith("Typed as on the command line")
        sim = _get(client, "form", world.wt, kind="flow", name="sim")
        # No public entry point builds a Flow with an unplaced group.
        monkeypatch.delitem(run_form.GROUP_PLACEMENT, "Lint options")
        lint = _get(client, "form", world.wt, kind="flow", name="lint")

    assert {"dest": "target", "kind": "target"}.items() <= next(
        f for f in sim["fields"] if f["dest"] == "target"
    ).items()
    assert "'Lint options'" in lint["problem"]


def test_specialist_forms_follow_the_values_given(world, feature_flags):
    run = _job(world, "run-cov", world.wt, report={})
    invocation = run.parent / "flow-reports" / "sim" / "run-cov"
    write_coverage_record(invocation, "uart")
    with _client(world, feature_flags) as client:

        def form(name, **values):
            return _get(client, "form", world.wt, kind="specialist", name=name, **values)

        spec = form("reviewer", focus="spec", more="on")
        tb = form("reviewer", category="tb")
        coverage = form("coverage_analyst")

    assert [f["dest"] for f in spec["fields"]] == [
        *("category", "focus", "spec", "scope", "instruction", "more", "max_turns", "timeout")
    ]
    assert spec["values"]["category"] == "rtl"
    assert spec["note"]
    assert tb["values"]["focus"] == "quality"
    assert "spec" not in [f["dest"] for f in tb["fields"]]
    assert [f["dest"] for f in coverage["fields"]] == ["coverage_record", "instruction", "more"]
    (record,) = coverage["coverage_records"]
    assert record["path"].endswith("targets/uart/coverage.json")
    assert record["label"].startswith("uart")


def test_run_routes_refuse_an_unaccepted_worktree_like_the_snapshot(
    world, feature_flags, tmp_path
):
    outside = tmp_path / "outside"
    outside.mkdir()
    cases = [
        ({}, 400),
        ({"worktree": "wt"}, 400),
        ({"worktree": str(tmp_path / "nowhere")}, 404),
        ({"worktree": str(outside)}, 404),
    ]
    with _client(world, feature_flags) as client:
        for params, status in cases:
            expected = client.get("/api/snapshot", params=params)
            assert expected.status_code == status
            for route, extra in ROUTES.items():
                answer = client.get(f"/api/run/{route}", params={**params, **extra})
                assert (answer.status_code, answer.json()) == (status, expected.json())


def test_run_routes_are_unknown_while_the_flag_is_off(world, feature_flags):
    with _client(world, feature_flags) as on, TestClient(_app(world, frozenset())) as off:
        unknown = off.get("/api/no-such-route")
        for route, params in ROUTES.items():
            query = {"worktree": str(world.wt), **params}
            assert on.get(f"/api/run/{route}", params=query).status_code == 200
            answer = off.get(f"/api/run/{route}", params=query)
            assert (answer.status_code, answer.text) == (unknown.status_code, unknown.text)

    assert unknown.status_code == 404


@pytest.mark.parametrize(
    ("booley_toml", "status"),
    [pytest.param("enabled", 200, id="enabled"), pytest.param("unparsable", 404, id="unparsable")],
)
def test_served_app_has_run_routes_only_while_the_flag_is_on(
    world, feature_flags, monkeypatch, booley_toml, status
):
    feature_flags.register(FLAG)
    if booley_toml == "enabled":
        feature_flags.enable(world.booley, FLAG)
    else:
        (world.booley / "booley.toml").write_bytes(b"not = [valid")
    served = capture_serving(monkeypatch)

    assert _web().serve(world.main, 0) == 0

    app = served[0][0]
    assert isinstance(app, Starlette)
    client = TestClient(app)
    query = {"worktree": str(world.wt)}
    assert client.get("/api/run/entries", params=query).status_code == status
    assert client.get("/api/snapshot", params=query).status_code == 200


@pytest.mark.parametrize(
    ("route", "params", "status"),
    [
        ("targets", {}, 400),
        ("targets", {"flow": "nope"}, 404),
        ("tests", {}, 400),
        ("form", {"name": "sim"}, 400),
        ("form", {"kind": "flow"}, 400),
        ("form", {"kind": "flow", "name": "nope"}, 404),
        ("form", {"kind": "specialist", "name": "custom"}, 404),
        ("form", {"kind": "other", "name": "sim"}, 404),
    ],
)
def test_run_routes_refuse_unknown_entries_and_missing_parameters(
    world, feature_flags, route, params, status
):
    with _client(world, feature_flags) as client:
        answer = client.get(f"/api/run/{route}", params={"worktree": str(world.wt), **params})

    assert answer.status_code == status
    assert list(answer.json()) == ["error"]
