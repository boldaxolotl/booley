"""`booley dashboard --web`: the Run form's choices as JSON, behind the `web-run-panel` flag."""

from __future__ import annotations

import json
import re
import shutil
import time
from html import escape
from urllib.parse import parse_qs, urlparse

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from booley.config.feature_flags import enabled_features
from booley.harness import booley as tlr
from booley.harness.dashboard import launch, model, run_form
from booley.harness.dashboard.jobs import JobReader, JobSnapshot
from booley.harness.standalone_job import standalone_jobs_dir
from booley.mcp.session_registry import Attribution, SessionRegistry
from booley.runtime import job_records
from booley.runtime.handles import reserve_session_handle
from tests.harness import test_dashboard_run as run_tests
from tests.harness import test_dashboard_run_specialist as specialist_tests
from tests.harness import test_dashboard_web as web_tests
from tests.harness.test_dashboard_run import (
    ADMITTED_BY_MEMORY,
    FLOW_NAMES,
    WORK_UNIT_TIMEOUT,
    write_config,
    write_picker_project,
)
from tests.harness.test_dashboard_run_pickers import SIM_FIFO_RUNS, TARGETS_BY_FLOW
from tests.harness.test_dashboard_specialist_form import write_coverage_record
from tests.harness.test_dashboard_web import _job, _queued_world, _web
from tests.uvicorn_support import capture_serving

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")

# The Project fixture of the snapshot tests, shared by assignment so a test finds it by name.
world = web_tests.world
empty_home = specialist_tests.empty_home
spawned = run_tests.spawned

FLAG = "web-run-panel"
# Every Run route with the parameters that make it answer for a worktree.
POSTS = ("command", "check", "submit")
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
    return TestClient(_app(world, enabled_features(world.main)), base_url="http://127.0.0.1:8090")


ORIGIN = {"Origin": "http://127.0.0.1:8090"}
JSON = {"Content-Type": "application/json"}


def _post(client, route, worktree, body=None, headers=ORIGIN, **content):
    """POST *body* as JSON for `worktree` (a path, or the query parameters themselves)."""
    params = worktree if isinstance(worktree, dict) else {"worktree": str(worktree)}
    return client.post(
        f"/api/run/{route}",
        params=params,
        headers=headers,
        **({"json": body} if body is not None else content),
    )


def _run(name, **fields):
    return {"kind": "specialist" if name == "reviewer" else "flow", "name": name, **fields}


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
    world, feature_flags, tmp_path, spawned
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
                # The worktree is refused before the route's own parameters are read.
                for given in (extra, {}):
                    answer = client.get(f"/api/run/{route}", params={**params, **given})
                    assert (answer.status_code, answer.json()) == (status, expected.json())
            for route in POSTS:
                answer = _post(client, route, params, _run("lint"))
                assert answer.status_code == status
                assert answer.json() == expected.json()
    assert spawned == []


def test_run_routes_are_unknown_while_the_flag_is_off(world, feature_flags):
    with _client(world, feature_flags) as on, TestClient(_app(world, frozenset())) as off:
        unknown = off.get("/api/no-such-route")
        for route, params in ROUTES.items():
            query = {"worktree": str(world.wt), **params}
            assert on.get(f"/api/run/{route}", params=query).status_code == 200
            answer = off.get(f"/api/run/{route}", params=query)
            assert (answer.status_code, answer.text) == (unknown.status_code, unknown.text)
        for route in POSTS:
            # A body that is no JSON object is refused before anything is read or started.
            assert _post(on, route, world.wt, []).status_code == 400
            answer = _post(off, route, world.wt, [])
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


def test_a_read_that_fails_is_the_problem_not_a_server_error(world, feature_flags, monkeypatch):
    # A Booley Directory the layout rules refuse: the readers raise instead of listing a problem.
    (world.wt / "booley.toml").write_bytes(b"[project]\ndir = '.booley_project'\n")
    with _client(world, feature_flags) as client:
        listing = _get(client, "targets", world.wt, flow="sim")
        tests = _get(client, "tests", world.wt, target="sim_fifo")
        monkeypatch.setattr(launch.Launcher, "form", _crash)
        built = _get(client, "form", world.wt, kind="specialist", name="reviewer")

    assert listing["targets"] == [] and listing["problem"]
    assert tests["runs"] == [] and tests["problem"].startswith("Tests unavailable: ")
    assert built == {"kind": "specialist", "problem": "RuntimeError: cannot build"}


def _crash(*_args):
    raise RuntimeError("cannot build")


def test_a_coverage_records_read_that_fails_lists_none(world, feature_flags, monkeypatch):
    from booley.harness.dashboard import web_run  # a module the base lacks: import in the test

    def crash(*_args):
        raise OSError("unreadable")

    monkeypatch.setattr(web_run, "finished_coverage_records", crash)
    with _client(world, feature_flags) as client:
        coverage = _get(client, "form", world.wt, kind="specialist", name="coverage_analyst")

    assert coverage["coverage_records"] == []
    assert next(f["dest"] for f in coverage["fields"]) == "coverage_record"


REVIEWER = _run("reviewer", values={"focus": "bugs", "scope": "fifo.v"})
REVIEWER_ARGV = ["--category", "rtl", "--focus", "bugs", "--scope", "fifo.v"]


@pytest.mark.parametrize(
    ("body", "status", "expected"),
    [
        pytest.param(
            _run("sim", values={"target": "sim_fifo", "test": ["a", "b"]}, extra="--seed 3"),
            200,
            "booley flow sim --target sim_fifo --test a --test b --seed 3",
            id="flow",
        ),
        pytest.param(_run("sim", extra="'x"), 422, "unbalanced quotes: No closing", id="quotes"),
        pytest.param(
            _run("reviewer", values={"focus": "bugs"}),
            422,
            "Scope is empty: name the files to review",
            id="specialist-incomplete",
        ),
        pytest.param(
            REVIEWER, 200, "booley specialist reviewer " + " ".join(REVIEWER_ARGV), id="specialist"
        ),
    ],
)
def test_command_route_quotes_the_form_or_returns_its_error(
    world, feature_flags, empty_home, body, status, expected
):
    with _client(world, feature_flags) as client:
        answer = _post(client, "command", world.wt, body)

    assert answer.status_code == status
    reply = answer.json()
    if status == 200:
        assert reply["command"].partition(" && ")[2] == expected
    else:
        assert expected in reply["error"]


@pytest.mark.parametrize("route", ["check", "submit"])
def test_a_parser_rejection_is_returned_and_starts_no_job(world, feature_flags, spawned, route):
    with _client(world, feature_flags) as client:
        answer = _post(client, route, world.wt, _run("lint", extra="--no-such-option"))

    assert answer.status_code == 422
    assert "unrecognized arguments: --no-such-option" in answer.json()["error"]
    assert spawned == []
    assert not list(standalone_jobs_dir(world.booley).glob("*"))


@pytest.mark.parametrize(
    ("body", "status"),
    [
        pytest.param({"kind": 1, "name": "lint"}, 400, id="kind-not-text"),
        pytest.param(_run("lint", values={"target": 1}), 400, id="value-not-text"),
        pytest.param(_run("lint", values={"target": ["a"]}), 400, id="list-for-a-text-field"),
        pytest.param(_run("reviewer", values={"scope": ["a"]}), 400, id="list-for-specialist"),
        pytest.param(_run("reviewer", extra="--x"), 400, id="specialist-extra"),
        pytest.param(_run("nope"), 404, id="unknown-flow"),
        pytest.param({"kind": "other", "name": "lint"}, 404, id="unknown-kind"),
    ],
)
def test_a_malformed_or_unknown_run_is_refused_before_any_check(
    world, feature_flags, spawned, body, status
):
    with _client(world, feature_flags) as client:
        answers = [_post(client, route, world.wt, body) for route in POSTS]

    assert [answer.status_code for answer in answers] == [status] * 3
    assert all(list(answer.json()) == ["error"] for answer in answers)
    assert spawned == []


def _connected_session(world):
    """A session of this host connected to `world.wt`, handle S1."""
    SessionRegistry(world.booley).upsert(
        Attribution("codex:s1", "thread", str(world.wt), "repo:w", namespace=SCOPE),
        "sim",
        "completed",
        now=time.time(),
    )
    reserve_session_handle(world.booley, "codex:s1")


SCOPE = "pid:[17]"


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        pytest.param(
            _run("lint"),
            [
                ADMITTED_BY_MEMORY,
                "Warning: session S1 Interactive is connected to this worktree",
                "Warning: 2 unfinished Jobs in this worktree (run-r, run-q)",
                "Warning: 1 uncommitted change in this worktree",
            ],
            id="warnings",
        ),
        pytest.param(_run("synth"), [ADMITTED_BY_MEMORY, WORK_UNIT_TIMEOUT], id="timeout"),
        pytest.param(
            REVIEWER,
            [
                ADMITTED_BY_MEMORY,
                "Model: <the Specialist's model> on the claude Agent CLI",
                "Uses model tokens on the claude Agent CLI's credential (auth = api_key)",
                "Timeout: 30m 00s per model call",
            ],
            id="specialist",
        ),
    ],
)
def test_check_returns_the_review_screen_lines(
    world, feature_flags, empty_home, monkeypatch, body, expected
):
    # The session registry scopes rows by the host's PID namespace, which the test cannot join.
    monkeypatch.setattr(model, "namespace", lambda: SCOPE)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    write_config(world.wt, '[agent]\nauth = "api_key"\n')
    if body["name"] == "lint":
        _queued_world(world)
        _connected_session(world)
        (world.wt / "notes.txt").write_bytes(b"draft\n")
    with _client(world, feature_flags) as client:
        answer = _post(client, "check", world.wt, body)
        quoted = _post(client, "command", world.wt, body)

    assert (answer.status_code, quoted.status_code) == (200, 200)
    reply = answer.json()
    # The model is the settings' default, which a release changes: the line's shape is the contract.
    shown = [re.sub(r"^Model: \S+", "Model: <the Specialist's model>", t) for t in reply["lines"]]
    assert shown == expected
    assert reply["command"] == quoted.json()["command"]
    assert reply["refusal"] == ""


@pytest.mark.parametrize("reverse", [False, True])
def test_unfinished_jobs_are_named_oldest_first_whatever_the_listing_order(world, reverse):
    _queued_world(world)
    jobs = JobReader(world.booley, proc_root=world.proc).read().jobs
    listed = tuple(sorted(jobs, key=lambda job: job.record.run_id, reverse=reverse))
    snapshot = model.DashboardSnapshot((), JobSnapshot(listed), observed_at=1.0)

    occupants = model.worktree_occupants(snapshot, world.wt)

    assert occupants.unfinished_jobs == ("run-r", "run-q")


@pytest.mark.parametrize(
    ("body", "argv", "handles"),
    [
        pytest.param(_run("lint"), ["booley", "flow", "lint"], "stored", id="busy-queue"),
        pytest.param(
            REVIEWER,
            ["booley", "specialist", "reviewer", *REVIEWER_ARGV],
            "stored",
            id="specialist",
        ),
        pytest.param(_run("lint"), ["booley", "flow", "lint"], "malformed", id="no-handle"),
    ],
)
def test_submit_starts_a_standalone_job_listed_under_now_as_yours(
    world, feature_flags, empty_home, spawned, monkeypatch, body, argv, handles
):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    write_config(world.wt, '[agent]\nauth = "api_key"\n')
    _queued_world(world)  # the memory class is busy and one Job waits: nothing refuses a launch
    if handles == "malformed":
        (world.booley / "runtime" / "job-handles").mkdir(parents=True)
        (world.booley / "runtime" / "job-handles" / "map.json").write_bytes(b"not json")
    with _client(world, feature_flags) as client:
        answer = _post(client, "submit", world.wt, body)
        listed = client.get("/api/snapshot", params={"worktree": str(world.wt)}).json()["now"]

    assert answer.status_code == 200
    run_id, handle = answer.json()["run_id"], answer.json()["handle"]
    record = job_records.read_record(run_id, standalone_jobs_dir(world.booley))
    assert record is not None
    assert (record.work_dir, record.session_key, record.argv) == (
        str(world.wt.resolve()),
        None,
        argv,
    )
    assert handle.startswith("J-")
    assert handle.endswith("?") == (handles == "malformed")
    assert len(spawned) == 1
    assert [row["owner"] for row in listed if row["handle"] == handle] == ["you"]


def test_a_refused_auth_check_is_returned_and_not_submitted(
    world, feature_flags, empty_home, spawned
):
    write_config(world.wt, '[agent]\nauth = "api_key"\n')
    refusal = (
        "Refused: [agent] auth = 'api_key' but ANTHROPIC_API_KEY is not set"
        " \u2014 export it, or switch auth to 'subscription'. Copy command still works."
    )
    with _client(world, feature_flags) as client:
        checked = _post(client, "check", world.wt, REVIEWER)
        submitted = _post(client, "submit", world.wt, REVIEWER)

    assert (checked.status_code, submitted.status_code) == (200, 422)
    assert checked.json()["refusal"] == refusal
    assert submitted.json() == {"error": refusal}
    assert spawned == []


def test_a_job_record_that_cannot_be_written_is_a_server_error(world, feature_flags, spawned):
    standalone_jobs_dir(world.booley).parent.mkdir(parents=True)
    standalone_jobs_dir(world.booley).write_bytes(b"")  # a file where the records belong
    with _client(world, feature_flags) as client:
        answer = _post(client, "submit", world.wt, _run("lint"))

    assert answer.status_code == 500
    assert answer.json()["error"].startswith("Could not start the Job: ")
    assert spawned == []


@pytest.mark.parametrize("route", POSTS)
@pytest.mark.parametrize(
    ("headers", "content", "status"),
    [
        pytest.param({"Origin": "http://evil.example"}, {"json": _run("lint")}, 403, id="foreign"),
        pytest.param({}, {"json": _run("lint")}, 403, id="no-origin"),
        pytest.param(
            {"Origin": "http://evil.example", "Host": "evil.example"},
            {"json": _run("lint")},
            403,
            id="rebound-name",
        ),
        pytest.param(ORIGIN, {"data": {"kind": "flow", "name": "lint"}}, 415, id="form-encoded"),
        pytest.param(ORIGIN, {"content": b"{", "headers": JSON}, 400, id="not-json"),
        pytest.param(ORIGIN, {"json": [_run("lint")]}, 400, id="not-an-object"),
    ],
)
def test_a_launch_from_another_origin_or_without_json_starts_nothing(
    world, feature_flags, spawned, route, headers, content, status
):
    with _client(world, feature_flags) as client:
        answer = client.post(
            f"/api/run/{route}",
            params={"worktree": str(world.wt)},
            **{**content, "headers": {**headers, **content.get("headers", {})}},
        )

    assert answer.status_code == status
    assert list(answer.json()) == ["error"]
    assert spawned == []
    assert not list(standalone_jobs_dir(world.booley).glob("*"))


def test_a_form_that_cannot_be_built_is_refused_not_a_server_error(
    world, feature_flags, spawned, monkeypatch
):
    # No public entry point builds a Flow with an unplaced group.
    monkeypatch.delitem(run_form.GROUP_PLACEMENT, "Lint options")
    with _client(world, feature_flags) as client:
        answers = [_post(client, route, world.wt, _run("lint")) for route in POSTS]

    assert [answer.status_code for answer in answers] == [422] * 3
    assert all("'Lint options'" in answer.json()["error"] for answer in answers)
    assert spawned == []


def _probe(client, wt, tmp_path, calls, steps, *, responses=None, reject=None, held=()):
    """Run the served page for *wt* through *steps*, answered as *client* answers *calls*.

    A call is ``(route, query, body)``: a GET of the Run route, or a POST of *body*. The page must
    send exactly those requests in that order. Returns the probe's output and the real answers.
    """
    page = client.get("/", params={"worktree": str(wt)})
    refs = web_tests._References()
    refs.feed(page.text)
    scripts = [client.get(url).text for url in refs.urls if url.endswith(".js")]
    routes: dict = {}
    answers = []
    for index, (route, query, body) in enumerate(calls):
        params = {"worktree": str(wt), **query}
        reply = (
            client.get(f"/api/run/{route}", params=params)
            if body is None
            else _post(client, route, wt, body)
        )
        is_json = reply.headers["content-type"].startswith("application/json")
        answers.append(
            {
                "status": reply.status_code,
                **({"body": reply.json()} if is_json else {"text": reply.text}),
            }
        )
        sent = {"reject": "refused"} if index == reject else answers[-1]
        sent = {**sent, "held": index in held}
        routes.setdefault(f"/api/run/{route}", []).append(sent)
    scenario = {
        "search": web_tests._search(wt),
        "hidden": refs.hidden,
        "responses": [r or web_tests._reply(client, wt) for r in responses or [None]],
        "routes": routes,
        "steps": steps,
    }
    probe = web_tests._run_page(";\n".join(scripts), scenario, tmp_path)
    asked = [
        (
            f.get("method") or "GET",
            urlparse(f["url"]).path,
            {key: value for key, [value] in parse_qs(urlparse(f["url"]).query).items()},
            f.get("body"),
        )
        for f in probe["fetches"]
        if "/api/run/" in f["url"]
    ]
    expected = [
        (
            "GET" if body is None else "POST",
            f"/api/run/{route}",
            {"worktree": str(wt), **query},
            body,
        )
        for route, query, body in calls
    ]
    assert asked == expected
    return probe, answers


def _edit(dest, value, kind="text", **target):
    event = "input" if kind == "text" else "change"
    target = {"dataset": {"dest": dest}, "type": kind, "value": value, **target}
    return {event: "run-fields", "target": target}


def _choose(entry):
    return {"change": "run-entry", "target": {"value": entry}}


def _option_values(markup, dest=None):
    if dest:
        markup = re.search(rf'<select data-dest="{dest}">(.*?)</select>', markup)[1]
    return re.findall(r'<option value="([^"]*)"', markup)


ENTRIES = ("entries", {}, None)
RUN = {"click": "run-button"}
REVIEW = {"click": "run-check"}
SUBMIT = {"click": "run-submit"}


@pytest.mark.parametrize("case", ["on", "off", "refused", "unanswered"])
def test_run_button_is_the_top_bar_primary_action_only_while_the_flag_is_on(
    world, feature_flags, tmp_path, case
):
    wt = tmp_path / "nowhere" if case == "refused" else world.wt
    calls, steps = ([ENTRIES], []) if case == "off" else ([ENTRIES, ENTRIES], [RUN])
    with _client(world, feature_flags) as on, TestClient(_app(world, frozenset())) as off:
        page = on.get("/", params={"worktree": str(world.wt)}).text
        css = on.get("/assets/dashboard.css").text
        probe, answers = _probe(
            off if case == "off" else on,
            wt,
            tmp_path,
            calls,
            steps,
            reject=0 if case == "unanswered" else None,
        )

    top_bar = r'<header id="topbar" class="topbar" hidden>.*?<button id="run-button" class="primary".*?</header>'
    assert re.search(top_bar + r"\s*<section id=\"run\".*?<main>", page, re.S)
    assert "<dialog" not in page
    assert re.search(r"button\.primary \{[^}]*var\(--primary\)", css)
    loaded, *clicked = probe["views"]
    assert loaded["run-button"]["hidden"] is loaded["topbar"]["hidden"] is (case == "off")
    if case != "off":
        (opened,) = clicked
        body = answers[-1]["body"]
        assert opened["run"]["hidden"] is False
        assert opened["run-button"]["attributes"]["aria-expanded"] == "true"
        assert opened["run-problem"]["text"] == body.get("error", "")
        assert _option_values(opened["run-entry"]["html"]) == [
            *([""] if case != "refused" else []),
            *(f"flow:{flow['name']}" for flow in body.get("flows", [])),
            *(f"specialist:{item['name']}" for item in body.get("specialists", [])),
        ]


@pytest.mark.parametrize("reviewed", [False, True], ids=["form", "reviewed"])
@pytest.mark.parametrize(
    "close", [{"key": "Escape"}, {"click": "run-cancel"}], ids=["escape", "cancel"]
)
def test_run_form_opens_in_place_survives_polls_and_closes_without_side_effects(
    world, feature_flags, tmp_path, close, reviewed
):
    write_picker_project(world.wt)
    lint = _run("lint", values={}, extra="")
    typed = {**lint, "extra": "--dry-run"}
    calls = [
        *[ENTRIES] * 2,
        ("form", {"kind": "flow", "name": "lint"}, None),
        ("targets", {"flow": "lint"}, None),
        ("command", {}, lint),
        ("command", {}, typed),
        *[("check", {}, typed)] * reviewed,
        ENTRIES,
    ]
    steps = [
        RUN,
        _choose("flow:lint"),
        _edit("extra", "--dry-run"),
        {"poll": True},
        *[REVIEW] * reviewed,
        close,
        RUN,
    ]
    with _client(world, feature_flags) as client:
        first = web_tests._reply(client, world.wt)
        _job(world, "run-new", world.wt, target="t_new", ended="2026-10-08T10:00:10Z")
        responses = [first, web_tests._reply(client, world.wt)]
        probe, _answers = _probe(client, world.wt, tmp_path, calls, steps, responses=responses)

    _loaded, _opened, _chosen, typing, polled, *shown, closed, reopened = probe["views"]
    assert [view["run-review"]["hidden"] for view in shown] == [False] * reviewed
    assert "t_new" not in typing["results"]["html"]
    assert "t_new" in polled["results"]["html"]
    assert all(polled[id_]["html"] == typing[id_]["html"] for id_ in ("run-entry", "run-fields"))
    assert not any(view["live"]["hidden"] for view in probe["views"])
    assert closed["run"]["hidden"] is True
    assert closed["run-button"]["attributes"]["aria-expanded"] == "false"
    assert closed["run-fields"]["html"] == closed["run-entry"]["html"] == ""
    assert (closed["run-review"]["html"], closed["run-review"]["hidden"]) == ("", True)
    assert closed["run-submit"]["disabled"] is True
    assert probe["focused"][-2:] == ["run-button", "run-entry"]
    assert reopened["run-entry"]["html"].startswith('<option value="" selected>')
    assert reopened["run-fields"]["html"] == ""


def test_flow_form_offers_the_api_choices_and_quotes_the_command(world, feature_flags, tmp_path):
    write_picker_project(world.wt)
    sim = _run("sim", values={}, extra="")
    picked = {**sim, "values": {"target": "sim_fifo"}}
    ticked = {**picked, "values": {"target": "sim_fifo", "test": ["stress+short"]}}
    seeded = {**ticked, "extra": "--seed 3"}
    commands = [("command", {}, body) for body in (sim, picked, ticked, seeded)]
    calls = [
        *[ENTRIES] * 2,
        ("form", {"kind": "flow", "name": "sim"}, None),
        ("targets", {"flow": "sim"}, None),
        commands[0],
        ("tests", {"target": "sim_fifo"}, None),
        *commands[1:],
    ]
    steps = [
        RUN,
        _choose("flow:sim"),
        _edit("target", "sim_fifo", "select-one"),
        _edit("test", "stress+short", "checkbox", checked=True),
        _edit("extra", "--seed 3"),
        {"release": "/api/run/command"},
        {"click": "run-copy"},
    ]
    with _client(world, feature_flags) as client:
        probe, answers = _probe(client, world.wt, tmp_path, calls, steps, held={8})

    _loaded, _opened, chosen, targeted, ticking, waiting, seeding, _copied = probe["views"]
    fields = chosen["run-fields"]["html"]
    assert _option_values(fields, "target") == ["", *TARGETS_BY_FLOW["sim"]]
    assert (
        re.findall(r'data-dest="test" value="([^"]*)"', targeted["run-fields"]["html"])
        == SIM_FIFO_RUNS
    )
    dests = set(re.findall(r'data-dest="(\w+)"', targeted["run-fields"]["html"]))
    assert dests == {f["dest"] for f in answers[2]["body"]["fields"]} | {"extra"}
    assert "worktree" not in dests
    shown = [view["run-command"]["text"] for view in (chosen, targeted, ticking, seeding)]
    assert shown == [answers[i]["body"]["command"] for i in (4, 6, 7, 8)]
    assert shown[-1].endswith("booley flow sim --target sim_fifo --test stress+short --seed 3")
    # Until the command for an edit arrives, Copy offers no command of the older inputs.
    assert (ticking["run-copy"]["disabled"], waiting["run-copy"]["disabled"]) == (False, True)
    assert seeding["run-copy"]["disabled"] is False
    assert probe["clipboard"] == [shown[-1]]


def test_review_shows_the_checks_facts_and_submit_starts_the_job_listed_as_yours(
    world, feature_flags, spawned, monkeypatch, tmp_path
):
    monkeypatch.setattr(model, "namespace", lambda: SCOPE)
    _queued_world(world)
    _connected_session(world)
    (world.wt / "notes.txt").write_bytes(b"draft\n")
    synth = _run("synth", values={}, extra="")
    wrong = {**synth, "extra": "--no-such-option"}
    calls = [
        *[ENTRIES] * 2,
        ("form", {"kind": "flow", "name": "synth"}, None),
        ("targets", {"flow": "synth"}, None),
        ("command", {}, synth),
        ("command", {}, wrong),
        ("check", {}, wrong),
        ("command", {}, synth),
        ("check", {}, synth),
        ("submit", {}, synth),
    ]
    steps = [
        RUN,
        _choose("flow:synth"),
        _edit("extra", "--no-such-option"),
        REVIEW,
        SUBMIT,
        _edit("extra", ""),
        REVIEW,
        {"click": "run-copy"},
        SUBMIT,
        REVIEW,
        SUBMIT,
        {"release": "/api/run/submit"},
        {"poll": True},
    ]
    with _client(world, feature_flags) as client:
        first = web_tests._reply(client, world.wt)
        responses = [first, None]
        probe, answers = _probe(
            client, world.wt, tmp_path, calls, steps, responses=responses, held={9}
        )

    _loaded, _opened, _chosen, _typed, invalid, _unsent, _cleared, reviewed, copied, pending = (
        probe["views"][:10]
    )
    released, polled = probe["views"][-2:]
    assert "unrecognized arguments: --no-such-option" in invalid["run-error"]["text"]
    assert (invalid["run-review"]["hidden"], invalid["run-submit"]["disabled"]) == (True, True)
    lines = answers[8]["body"]["lines"]
    assert lines == [
        ADMITTED_BY_MEMORY,
        WORK_UNIT_TIMEOUT,
        "Warning: session S1 Interactive is connected to this worktree",
        "Warning: 2 unfinished Jobs in this worktree (run-r, run-q)",
        "Warning: 1 uncommitted change in this worktree",
    ]
    assert reviewed["run-review"]["html"] == "".join(f"<li>{escape(line)}</li>" for line in lines)
    assert reviewed["run-review"]["hidden"] is False
    command = answers[8]["body"]["command"]
    assert (reviewed["run-command"]["text"], copied["run-copy"]["disabled"]) == (command, False)
    assert probe["clipboard"] == [command]
    assert (pending["run-submit"]["disabled"], pending["run-check"]["disabled"]) == (True, True)
    handle = answers[9]["body"]["handle"]
    assert (released["run"]["hidden"], handle in released["live"]["html"]) == (True, False)
    assert re.search(
        rf"{handle}</span>(<span>[^<]*</span>)*?<span>you</span>", polled["live"]["html"]
    )


def test_specialist_form_keeps_max_turns_and_timeout_under_more_and_shows_its_error(
    world, feature_flags, empty_home, tmp_path
):
    write_config(world.wt, '[agent]\nauth = "api_key"\n')
    run = _job(world, "run-cov", world.wt, report={})
    write_coverage_record(run.parent / "flow-reports" / "sim" / "run-cov", "uart")
    reviewer = {"kind": "specialist", "name": "reviewer", "extra": ""}
    analyst = {"kind": "specialist", "name": "coverage_analyst", "extra": "", "values": {}}
    base = {"category": "rtl"}
    more = {**base, "more": "on"}
    scoped = {**more, "scope": "fifo.v"}
    bugs = {**scoped, "focus": "bugs"}

    def form(name, **values):
        return ("form", {"kind": "specialist", "name": name, **values}, None)

    def command(values):
        return ("command", {}, {**reviewer, "values": values})

    calls = [
        *[ENTRIES] * 2,
        form("reviewer"),
        command(base),
        form("reviewer", **more),
        command(more),
        command(scoped),
        command(scoped),
        form("reviewer", **bugs),
        command(bugs),
        command(bugs),
        ("check", {}, {**reviewer, "values": bugs}),
        form("coverage_analyst"),
        ("command", {}, analyst),
    ]
    steps = [
        RUN,
        _choose("specialist:reviewer"),
        _edit("more", "", "checkbox", checked=True),
        _edit("scope", "fifo.v"),
        {"focus": "scope", "at": 6},
        {"release": "/api/run/form"},
        _edit("focus", "bugs", "select-one"),
        REVIEW,
        {"release": "/api/run/command"},
        SUBMIT,
        _choose("specialist:coverage_analyst"),
    ]
    with _client(world, feature_flags) as client:
        probe, answers = _probe(client, world.wt, tmp_path, calls, steps, held={4, 10})

    _loaded, _opened, chosen, opening, _typing, _focus, released, focused, *_review = probe[
        "views"
    ]
    _checked, refused, _submitted, analysing = _review
    for view in (chosen, opening):
        assert re.search(
            r'<select data-dest="focus"><option value="" selected disabled>',
            view["run-fields"]["html"],
        )
        assert "max_turns" not in view["run-fields"]["html"]
        assert view["run-error"]["text"] == answers[3]["body"]["error"] == "Choose a focus"
    assert 'data-dest="scope" value="fifo.v"' in released["run-fields"]["html"]
    assert all(
        f'data-dest="{dest}"' in released["run-fields"]["html"]
        for dest in ("max_turns", "timeout")
    )
    # The rebuilt form puts the caret back where the user was typing, and has no Textual hint.
    assert "field:scope" in probe["focused"]
    assert probe["selections"][0] == ["scope", 6, 6]
    assert "Enter shows" not in released["run-fields"]["html"]
    assert focused["run-error"]["hidden"] is True
    assert focused["run-command"]["text"].endswith(
        "booley specialist reviewer --category rtl --focus bugs --scope fifo.v"
    )
    for fact in ("Model: ", "Uses model tokens on the claude Agent CLI"):
        assert fact in refused["run-review"]["html"]
    assert refused["run-error"]["text"] == answers[11]["body"]["refusal"]
    assert refused["run-error"]["text"].startswith("Refused:")
    assert (refused["run-submit"]["disabled"], analysing["run-review"]["hidden"]) == (True, True)
    (record,) = answers[-2]["body"]["coverage_records"]
    assert (
        f'<datalist id="run-records"><option value="{escape(record["path"])}"'
        in analysing["run-fields"]["html"]
    )


def test_a_failed_read_for_an_older_target_is_not_shown_beside_the_newer_tests(
    world, feature_flags, tmp_path
):
    write_picker_project(world.wt)
    sim = _run("sim", values={}, extra="")
    wide = {**sim, "values": {"target": "sim_wide"}}
    fifo = {**sim, "values": {"target": "sim_fifo"}}
    calls = [
        *[ENTRIES] * 2,
        ("form", {"kind": "flow", "name": "sim"}, None),
        ("targets", {"flow": "sim"}, None),
        ("command", {}, sim),
        ("tests", {"target": "sim_wide"}, None),
        ("command", {}, wide),
        ("tests", {"target": "sim_fifo"}, None),
        ("command", {}, fifo),
    ]
    steps = [
        RUN,
        _choose("flow:sim"),
        _edit("target", "sim_wide", "select-one"),
        _edit("target", "sim_fifo", "select-one"),
        {"release": "/api/run/tests"},
    ]
    with _client(world, feature_flags) as client:
        probe, _answers = _probe(client, world.wt, tmp_path, calls, steps, reject=5, held={5})

    *_before, newer, released = probe["views"]
    assert (
        re.findall(r'data-dest="test" value="([^"]*)"', newer["run-fields"]["html"])
        == SIM_FIFO_RUNS
    )
    assert released["run-problem"]["hidden"] is True
    assert released["run-fields"]["html"] == newer["run-fields"]["html"]
