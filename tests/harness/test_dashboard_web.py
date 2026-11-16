"""`booley dashboard --web`: the per-worktree Jobs snapshot as JSON, over real Job records."""

from __future__ import annotations

import argparse
import html
import json
import os
import shutil
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

import pytest
from starlette.testclient import TestClient

from booley.harness import booley as tlr
from booley.runtime import job_records, job_slots
from booley.runtime.memory_budget import resolve_memory_budget
from booley.runtime.pid import ProcessIdentity
from tests.conftest import symlink_or_skip
from tests.git_support import git
from tests.subprocess_budget import run_bounded_child

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")

GIB = 2**30
MIB = 2**20
SCOPE = "pid:[4026531836]"
# 2026-10-08T10:00:20Z
NOW = 1791453620.0
# 8 GiB less the fixed headroom.
BUDGET_BYTES = 6442450944
STARTED = "2026-10-08T10:00:00Z"


@dataclass(frozen=True)
class World:
    main: Path
    other: Path
    wt: Path
    booley: Path
    proc: Path
    cgroup: Path


def _web():
    """Import lazily so a missing module fails inside the test, not at collection."""
    from booley.harness.dashboard import web

    return web


def _snapshot_module():
    from booley.harness.dashboard import web_snapshot

    return web_snapshot


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> World:
    """A Project with a linked worktree under `.booley/` and one outside the main checkout."""
    monkeypatch.delenv("BOOLEY_DIR", raising=False)
    monkeypatch.delenv("BOOLEY_SLOTS_DIR", raising=False)
    main = tmp_path / "repo"
    main.mkdir()
    git(main, "init", "-q", "-b", "main")
    no_excludes = tmp_path / "no-excludes"
    no_excludes.write_bytes(b"")
    git(main, "config", "core.excludesFile", str(no_excludes))
    (main / "base.txt").write_bytes(b"base\n")
    git(main, "add", "base.txt")
    git(main, "commit", "-qm", "base")
    (main / ".booley").mkdir()
    other = main / ".booley" / "worktrees" / "other"
    git(main, "worktree", "add", "-q", "-b", "booley/other", str(other), "HEAD")
    wt = tmp_path / "wt"
    git(main, "worktree", "add", "-q", "-b", "booley/wt", str(wt), "HEAD")
    (wt / ".booley").mkdir()
    cgroup = tmp_path / "cgroup"
    cgroup.mkdir()
    for name, text in (
        ("memory.current", "1048576\n"),
        ("memory.max", "2097152\n"),
        ("cpu.max", "200000 100000\n"),
        ("cpu.stat", "usage_usec 1000000\n"),
    ):
        (cgroup / name).write_bytes(text.encode())
    return World(main, other, wt, main / ".booley", tmp_path / "proc", cgroup)


def _client(world: World, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """The app over the world, with a fixed clock and an 8 GiB container.

    Patch `web_snapshot` further after this call and before the first request.
    """
    snapshot = _snapshot_module()
    monkeypatch.setattr(snapshot, "wall_clock", lambda: NOW)
    monkeypatch.setattr(
        snapshot,
        "compute_memory_budget",
        lambda **_kw: resolve_memory_budget(
            limit_bytes=8 * GIB, installed_bytes=None, processes=()
        ),
    )
    app = _web().create_app(world.main, world.booley, proc_root=world.proc, cgroup=world.cgroup)
    return TestClient(app)


def _live_process(world: World, pid: int) -> dict:
    """A running process in the fake `/proc`, as the identity a Job record carries."""
    directory = world.proc / str(pid)
    (directory / "ns").mkdir(parents=True, exist_ok=True)
    symlink_or_skip(directory / "ns/pid", Path(SCOPE))
    (directory / "stat").write_bytes(f"{pid} (job) S {'0 ' * 18}101 0\n".encode())
    return ProcessIdentity(pid, SCOPE, 101).to_payload()


def _identity(pid: int) -> dict:
    return ProcessIdentity(pid, SCOPE, 101).to_payload()


def _job(
    world: World,
    run_id: str,
    work_dir: Path | None,
    *,
    target: str | None = "uart",
    session: str | None = "sess1",
    status: str = "done",
    exit_code: int | None = 0,
    started: str = STARTED,
    ended: str | None = STARTED,
    pid: int | None = None,
    live: bool = False,
    report: dict | None = None,
    standalone_report_root: Path | None = None,
) -> Path:
    """Write one Job record (and its report); return the records root."""
    if session is None:
        from booley.harness.standalone_job import standalone_jobs_dir

        root = standalone_jobs_dir(world.booley)
    else:
        root = world.booley / ".interactive_logs" / session / ".runtime" / "jobs"
    identity = _live_process(world, pid) if live and pid else None
    running = status == "running"
    job_records.write_record(
        job_records.JobRecord(
            run_id,
            "sim",
            started,
            60,
            argv=[] if target is None else ["--target", target],
            pid=pid,
            status=status,
            exit_code=exit_code,
            run_started_at=started if running and pid else None,
            work_dir=None if work_dir is None else str(work_dir),
            session_key=session,
            process_identity=identity,
            ended_at=None if running else ended,
            report_root=(
                None if standalone_report_root is None else str(standalone_report_root.resolve())
            ),
        ),
        root,
    )
    if report is not None:
        parent = (
            root.parent if standalone_report_root is None else standalone_report_root.resolve()
        )
        path = parent / "flow-reports" / "sim" / run_id / "report.json"
        path.parent.mkdir(parents=True)
        path.write_bytes(json.dumps({"run_id": run_id, **report}).encode())
    return root


def _slot(world: World, kind: str, seq: int, pid: int, payload: dict) -> None:
    directory = job_slots.slots_dir(world.booley) / "memory"
    directory.mkdir(parents=True, exist_ok=True)
    name = job_slots._entry_name(kind, 1, seq, pid, 0)
    (directory / name).write_bytes(json.dumps({"created_at": STARTED, **payload}).encode())


def _claim(mib: float, **kw) -> dict:
    return job_slots.MemoryClaim(6144.0, reservation_mb=mib, **kw).to_payload()


def _snapshot(client: TestClient, worktree: Path | str) -> dict:
    response = client.get("/api/snapshot", params={"worktree": str(worktree)})
    assert response.status_code == 200, response.text
    return response.json()


def _targets(rows: list[dict]) -> list[str]:
    return [row["target"] for row in rows]


def _dashboard_args(*words: str) -> argparse.Namespace:
    return tlr._build_parser().parse_args(["dashboard", *words])


@pytest.mark.parametrize(("words", "port"), [([], 8090), (["--port", "9123"], 9123)])
def test_web_flag_serves_loopback_on_8090_or_port(world, monkeypatch, capsys, words, port):
    import uvicorn
    from starlette.applications import Starlette

    served: list[tuple[object, dict]] = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **kwargs: served.append((app, kwargs)))

    assert tlr._cmd_dashboard(_dashboard_args("--web", *words), world.main) == 0

    ((app, kwargs),) = served
    assert isinstance(app, Starlette)
    assert (kwargs["host"], kwargs["port"]) == ("127.0.0.1", port)
    assert f"http://127.0.0.1:{port}" in capsys.readouterr().out


def test_dashboard_help_documents_web_and_port(capsys):
    with pytest.raises(SystemExit) as stop:
        _dashboard_args("--help")

    text = " ".join(capsys.readouterr().out.split())
    assert stop.value.code == 0
    assert "--web" in text
    assert "127.0.0.1" in text
    assert "--port PORT" in text
    assert "default: 8090" in text


@pytest.mark.parametrize("port", ["0", "65536", "-1", "http"])
def test_port_outside_tcp_range_is_refused(capsys, port):
    with pytest.raises(SystemExit) as stop:
        _dashboard_args("--web", "--port", port)

    assert stop.value.code == 2
    assert "port must be a whole number from 1 to 65535" in capsys.readouterr().err


def test_port_without_web_is_refused(tmp_path, monkeypatch, capsys):
    from booley.harness.dashboard import command

    monkeypatch.setattr(command, "run_dashboard", lambda _root: 0)
    args = argparse.Namespace(attach_policy=False, on_attach=False, web=False, port=9123)

    assert tlr._cmd_dashboard(args, tmp_path) == 2
    assert "booley dashboard: --port needs --web" in capsys.readouterr().err


def test_dashboard_without_web_opens_the_textual_dashboard(tmp_path, monkeypatch):
    from booley.harness.dashboard import command

    opened: list[Path] = []
    monkeypatch.setattr(command, "run_dashboard", lambda root: opened.append(root) or 0)

    assert tlr._cmd_dashboard(_dashboard_args(), tmp_path) == 0
    assert opened == [tmp_path]


def test_linked_worktree_snapshot_lists_only_its_jobs(world, monkeypatch):
    _job(world, "run-wt", world.wt, target="t_wt")
    _job(world, "run-main", world.main, target="t_main")
    _job(world, "run-other", world.other, target="t_other")
    _job(world, "run-nowhere", None, target="t_nowhere")

    with _client(world, monkeypatch) as client:
        body = _snapshot(client, world.wt)

    assert _targets(body["results"]["jobs"]) == ["t_wt"]
    assert body["now"] == []
    assert body["header"]["worktree"] == str(world.wt.resolve())
    assert body["header"]["branch"] == "booley/wt"


def test_worktree_named_through_a_symlink_is_the_same_worktree(world, tmp_path, monkeypatch):
    _job(world, "run-wt", world.wt, target="t_wt")
    link = tmp_path / "link-to-wt"
    symlink_or_skip(link, world.wt, target_is_directory=True)

    with _client(world, monkeypatch) as client:
        body = _snapshot(client, link)

    assert _targets(body["results"]["jobs"]) == ["t_wt"]
    assert body["header"]["worktree"] == str(world.wt.resolve())


def test_main_checkout_is_a_worktree_with_only_its_jobs(world, monkeypatch):
    _job(world, "run-wt", world.wt, target="t_wt")
    _job(world, "run-main", world.main, target="t_main")

    with _client(world, monkeypatch) as client:
        body = _snapshot(client, world.main)

    assert _targets(body["results"]["jobs"]) == ["t_main"]
    assert body["header"]["worktree"] == str(world.main.resolve())
    assert body["header"]["branch"] == "main"


def test_detached_worktree_has_no_branch(world, tmp_path, monkeypatch):
    detached = tmp_path / "detached"
    git(world.main, "worktree", "add", "-q", "--detach", str(detached), "HEAD")

    with _client(world, monkeypatch) as client:
        body = _snapshot(client, detached)

    assert body["header"]["branch"] is None


@pytest.mark.parametrize(
    ("kind", "status"),
    [
        ("missing", 400),
        ("empty", 400),
        ("relative", 400),
        ("unknown", 404),
        ("deleted", 404),
        ("recreated", 404),
        ("foreign", 404),
        ("outside", 404),
    ],
)
def test_unaccepted_worktree_gets_a_json_error_without_jobs(
    world, tmp_path, kind, status, monkeypatch
):
    deleted = tmp_path / "deleted"
    git(world.main, "worktree", "add", "-q", "-b", "booley/deleted", str(deleted), "HEAD")
    outside = tmp_path / "outside"
    outside.mkdir()
    for path in (deleted, outside, tmp_path / "nowhere"):
        _job(world, f"run-{path.name}", path, target="leak")
    shutil.rmtree(deleted)
    if kind in {"recreated", "foreign"}:
        deleted.mkdir()
    if kind == "foreign":
        git(deleted, "init", "-q")
    params = {
        "missing": {},
        "empty": {"worktree": ""},
        "relative": {"worktree": "wt"},
        "unknown": {"worktree": str(tmp_path / "nowhere")},
        "deleted": {"worktree": str(deleted)},
        "recreated": {"worktree": str(deleted)},
        "foreign": {"worktree": str(deleted)},
        "outside": {"worktree": str(outside)},
    }[kind]

    with _client(world, monkeypatch) as client:
        response = client.get("/api/snapshot", params=params)

    assert response.status_code == status
    assert response.headers["content-type"].startswith("application/json")
    body = response.json()
    assert list(body) == ["error"]
    assert isinstance(body["error"], str)
    assert "leak" not in response.text


def test_a_sessions_key_without_a_handle_reads_as_its_process():
    from types import SimpleNamespace

    from booley.harness.dashboard.web_snapshot import _owner

    key = "pid:pid:[4026531836]:42:7"
    job = SimpleNamespace(record=SimpleNamespace(session_key=key), owner_handle="")

    assert _owner(job).startswith("PID 42")


def test_unlistable_worktrees_are_a_503_without_jobs(world, monkeypatch):
    from booley.harness.worktree_list import WorktreeListError

    web = _web()

    def refuse(_repository):
        raise WorktreeListError("git worktree failed")

    monkeypatch.setattr(web, "worktree_registrations", refuse)
    _job(world, "run-wt", world.wt, target="leak")

    with _client(world, monkeypatch) as client:
        response = client.get("/api/snapshot", params={"worktree": str(world.wt)})

    assert response.status_code == 503
    assert response.json() == {"error": "Git could not list the worktrees: git worktree failed"}


def test_report_route_serves_only_reports_of_the_named_worktree(world, monkeypatch):
    _job(world, "run-wt", world.wt, report={"passed": True})
    _job(world, "run-other", world.other, report={"secret": 1})
    (world.wt / "secret.json").write_bytes(b'{"secret": 2}')
    other_report = world.booley / ".interactive_logs/sess1/.runtime/flow-reports/sim/run-other"

    with _client(world, monkeypatch) as client:
        url = _snapshot(client, world.wt)["results"]["jobs"][0]["report_url"]
        served = client.get(url)
        foreign = client.get(
            "/api/report",
            params={"worktree": str(world.wt), "report": str(other_report / "report.json")},
        )
        stray = client.get(
            "/api/report",
            params={"worktree": str(world.wt), "report": str(world.wt / "secret.json")},
        )
        bare = client.get("/api/report", params={"worktree": str(world.wt)})
        elsewhere = client.get(
            url.replace(quote(str(world.wt), safe=""), quote(str(world.main), safe=""))
        )

    assert served.status_code == 200
    assert served.json() == {"run_id": "run-wt", "passed": True}
    assert served.headers["content-type"].startswith("application/json")
    assert (foreign.status_code, stray.status_code) == (404, 404)
    assert "secret" not in foreign.text + stray.text
    assert bare.status_code == 400
    assert elsewhere.status_code == 404


def test_now_lists_running_and_queued_jobs_with_queue_position(world, monkeypatch):
    _job(
        world, "run-r", world.wt, target="t_run", status="running", ended=None, pid=7001, live=True
    )
    _job(
        world,
        "run-q",
        world.wt,
        target="t_queued",
        status="running",
        ended=None,
        started="2026-10-08T10:00:15Z",
        pid=7002,
        live=True,
    )
    _job(
        world,
        "run-s",
        world.wt,
        target="t_spawn",
        status="running",
        ended=None,
        started="2026-10-08T10:00:18Z",
    )
    for seq, pid, mib in ((1, 7101, 200.0), (2, 7102, 500.0)):
        _slot(world, "h", seq, pid, {"memory": _claim(mib)})
    _slot(world, "w", 3, 7002, {"owner_identity": _identity(7002), "memory": _claim(400.0)})

    with _client(world, monkeypatch) as client:
        body = _snapshot(client, world.wt)

    queued_row = {
        "handle": "J-SIM?",
        "flow": "sim",
        "target": "t_queued",
        "owner": "sess1",
        "state": "queued",
        "stage": "",
        "elapsed_s": 5.0,
        "queue_position": 1,
    }
    assert [(row["target"], row["state"], row["queue_position"]) for row in body["now"]] == [
        ("t_spawn", "spawning", None),
        ("t_queued", "queued", 1),
        ("t_run", "running", None),
    ]
    assert body["now"][1] == queued_row
    assert body["now"][2]["elapsed_s"] == 20.0
    assert body["results"] == {"jobs": [], "passed": 0, "failed": 0}


def test_results_are_newest_first_capped_with_counts_and_report_links(world, monkeypatch):
    standalone_booley = world.wt / ".booley"
    for i in range(52):
        ended = f"2026-10-08T10:00:{i:02d}Z"
        status, code = ("cancelled", 130) if i == 10 else ("done", i % 3)
        report = None if i == 20 else {"criterion_key": "k", "criterion_met": i % 2 == 0}
        _job(
            world,
            f"run-{i}",
            world.wt,
            target=f"t{i}",
            status=status,
            exit_code=code,
            ended=ended,
            report=report,
        )
    _job(
        world,
        "run-sa",
        world.wt,
        target="sa",
        session=None,
        ended="2026-10-08T10:01:00Z",
        report={"criterion_key": "k", "criterion_met": True},
        standalone_report_root=standalone_booley,
    )

    with _client(world, monkeypatch) as client:
        body = _snapshot(client, world.wt)
        linked = client.get(body["results"]["jobs"][0]["report_url"])

    jobs = body["results"]["jobs"]
    ended = [row["ended_at"] for row in jobs]
    assert len(jobs) == 50
    assert ended == sorted(ended, reverse=True)
    assert (jobs[0]["target"], jobs[1]["target"], jobs[-1]["target"]) == ("sa", "t51", "t3")
    assert (body["results"]["passed"], body["results"]["failed"]) == (18, 31)
    assert jobs[1]["result"] == "PASSED"
    by_target = {row["target"]: row for row in jobs}
    assert by_target["t10"]["result"] == "CANCELLED"
    assert by_target["t12"] == {
        "handle": "J-SIM?",
        "flow": "sim",
        "target": "t12",
        "owner": "sess1",
        "result": "PASSED",
        "ended_at": "2026-10-08T10:00:12Z",
        "duration_s": 12.0,
        "report_url": by_target["t12"]["report_url"],
    }
    assert by_target["t13"]["result"] == "FAILED"
    assert all("criterion" not in row for row in jobs)
    assert by_target["t20"]["report_url"] is None
    assert linked.status_code == 200
    assert linked.json() == {"run_id": "run-sa", "criterion_key": "k", "criterion_met": True}


def test_standalone_job_of_the_worktree_is_owned_by_you(world, monkeypatch):
    _job(
        world,
        "run-sa",
        world.wt,
        target="t_sa",
        session=None,
        status="running",
        ended=None,
        pid=7003,
        live=True,
    )
    _job(
        world,
        "run-sa-other",
        world.other,
        target="t_sa_other",
        session=None,
        status="running",
        ended=None,
        pid=7004,
        live=True,
    )

    with _client(world, monkeypatch) as client:
        body = _snapshot(client, world.wt)

    assert [(row["target"], row["owner"], row["state"]) for row in body["now"]] == [
        ("t_sa", "you", "running")
    ]


def test_unfinished_jobs_of_old_sessions_are_listed_and_counted(world, monkeypatch):
    for index in range(65):
        newer = world.booley / ".interactive_logs" / f"new{index}" / ".runtime" / "jobs"
        newer.mkdir(parents=True)
    _job(world, "run-s", world.wt, target="t_spawn", status="running", ended=None)
    _job(
        world,
        "run-q",
        world.wt,
        target="t_queued",
        status="running",
        ended=None,
        pid=7002,
        live=True,
    )
    _job(
        world,
        "run-r",
        world.wt,
        target="t_run",
        status="running",
        ended=None,
        pid=7001,
        live=True,
    )
    _job(
        world,
        "run-sa",
        world.wt,
        target="t_sa",
        session=None,
        status="running",
        ended=None,
        pid=7003,
        live=True,
    )
    _slot(world, "h", 1, 7101, {"memory": _claim(200.0)})
    _slot(world, "w", 2, 7002, {"owner_identity": _identity(7002), "memory": _claim(400.0)})
    # The roots holding the unfinished Jobs are the oldest of all 66.
    for root in (world.booley / ".interactive_logs").glob("*/.runtime/jobs"):
        old = root.parent.parent.name in {"sess1", "standalone"}
        stamp = 1_000 if old else 2_000_000_000
        os.utime(root, (stamp, stamp))

    with _client(world, monkeypatch) as client:
        body = _snapshot(client, world.wt)

    assert sorted((row["target"], row["state"]) for row in body["now"]) == [
        ("t_queued", "queued"),
        ("t_run", "running"),
        ("t_sa", "running"),
        ("t_spawn", "spawning"),
    ]
    admission = body["header"]["admission"]
    assert (admission["running_jobs"], admission["queued_jobs"]) == (2, 1)


def test_header_carries_resources_budget_and_jobs_counted_once(world, monkeypatch):
    ticks = iter((100.0, 110.0))
    # One Job holding a companion claim and two work-item claims.
    _job(world, "run-a", world.wt, status="running", ended=None, pid=7010, live=True)
    for seq, mib, extra in ((1, 100.0, {"companion": True}), (2, 200.0, {}), (3, 300.0, {})):
        _slot(
            world,
            "h",
            seq,
            7010,
            {"owner_identity": _identity(7010), "memory": _claim(mib, **extra)},
        )
    # A running Job whose child-lane claim waits is still running.
    _job(world, "run-c", world.main, status="running", ended=None, pid=7011, live=True)
    _slot(
        world,
        "w",
        4,
        7011,
        {"owner_identity": _identity(7011), "owner_kind": "execution", "memory": _claim(50.0)},
    )
    # A queued Job counts as queued, not reserved.
    _job(world, "run-b", world.other, status="running", ended=None, pid=7012, live=True)
    _slot(world, "w", 5, 7012, {"owner_identity": _identity(7012), "memory": _claim(400.0)})
    # A stale holder of a dead owner reserves nothing.
    _slot(world, "h", 6, 7013, {"owner_identity": _identity(7013), "memory": _claim(1000.0)})
    before = sorted(path.name for path in (world.booley / "runtime/jobs/slots/memory").iterdir())
    client = _client(world, monkeypatch)
    monkeypatch.setattr(_snapshot_module(), "monotonic", lambda: next(ticks))

    with client:
        first = _snapshot(client, world.wt)["header"]
        # 2 s of CPU in the 10 s between the readings, on 2 CPUs.
        (world.cgroup / "cpu.stat").write_bytes(b"usage_usec 3000000\n")
        second = _snapshot(client, world.wt)["header"]

    assert first["resources"]["cpu_percent"] is None
    assert first["resources"]["memory_bytes"] == 1048576
    assert first["resources"]["memory_limit_bytes"] == 2097152
    assert isinstance(first["resources"]["disk_free_bytes"], int)
    assert first["admission"] == {
        "memory_budget_bytes": BUDGET_BYTES,
        "memory_budget_unavailable": None,
        "reserved_bytes": 600 * MIB,
        "running_alone": False,
        "running_jobs": 2,
        "queued_jobs": 1,
    }
    assert second["resources"]["cpu_percent"] == pytest.approx(10.0)
    after = sorted(path.name for path in (world.booley / "runtime/jobs/slots/memory").iterdir())
    assert after == before


def test_run_alone_holder_is_reported_not_summed(world, monkeypatch):
    _job(world, "run-a", world.wt, status="running", ended=None, pid=7020, live=True)
    alone = job_slots.MemoryClaim(6144.0, run_alone=True).to_payload()
    _slot(world, "h", 1, 7020, {"owner_identity": _identity(7020), "memory": alone})

    with _client(world, monkeypatch) as client:
        admission = _snapshot(client, world.wt)["header"]["admission"]

    assert (admission["running_alone"], admission["reserved_bytes"]) == (True, 0)
    assert admission["running_jobs"] == 1


def test_budget_shows_without_jobs_and_unavailable_budget_is_marked(world, monkeypatch):
    def unreadable(**_kw):
        raise OSError("MemTotal is unreadable")

    with _client(world, monkeypatch) as client:
        empty = _snapshot(client, world.wt)["header"]["admission"]
        monkeypatch.setattr(_snapshot_module(), "compute_memory_budget", unreadable)
        marked = _snapshot(client, world.wt)["header"]["admission"]

    assert empty == {
        "memory_budget_bytes": BUDGET_BYTES,
        "memory_budget_unavailable": None,
        "reserved_bytes": 0,
        "running_alone": False,
        "running_jobs": 0,
        "queued_jobs": 0,
    }
    assert marked["memory_budget_bytes"] is None
    assert marked["memory_budget_unavailable"] == "MemTotal is unreadable"


PROBE = Path(__file__).with_name("dashboard_page_probe.cjs")
LIVE_PID = 7001


class _References(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.urls: list[str] = []

    def handle_starttag(self, tag, attrs):
        self.urls.extend(value for name, value in attrs if name in {"src", "href"} and value)


def _page_js(client: TestClient) -> str:
    response = client.get("/assets/dashboard.js")
    assert response.status_code == 200
    return response.text


def _reply(client: TestClient, worktree: Path | str) -> dict:
    """What the page's `fetch` would get: the status and JSON body of one snapshot request."""
    response = client.get("/api/snapshot", params={"worktree": str(worktree)})
    return {"status": response.status_code, "body": response.json()}


def _run_page(js: str, scenario: dict, tmp_path: Path) -> dict:
    """Run the served page script in Node's `vm` against the scripted responses."""
    node = shutil.which("node")
    if node is None:
        if os.environ.get("CI"):
            pytest.fail("CI must run the Dashboard page probe: install Node.js")
        pytest.skip("Node.js is required for the Dashboard page probe")
    tmp_path.mkdir(parents=True, exist_ok=True)
    script = tmp_path / "dashboard.js"
    script.write_bytes(js.encode())
    scenario_file = tmp_path / "scenario.json"
    scenario_file.write_bytes(json.dumps(scenario).encode())
    done = run_bounded_child([node, str(PROBE), str(script), str(scenario_file)])
    assert done.returncode == 0, done.stdout + done.stderr
    return json.loads(done.stdout)


def _search(worktree: Path | str) -> str:
    return "?worktree=" + quote(str(worktree), safe="")


def _queued_world(world: World) -> None:
    """One running and one queued Job of `world.wt`."""
    _job(
        world,
        "run-r",
        world.wt,
        target="t_run",
        status="running",
        ended=None,
        pid=LIVE_PID,
        live=True,
    )
    _job(
        world,
        "run-q",
        world.wt,
        target="t_queued",
        status="running",
        ended=None,
        started="2026-10-08T10:00:15Z",
        pid=7002,
        live=True,
    )
    for seq, pid, mib in ((1, 7101, 200.0), (2, 7102, 500.0)):
        _slot(world, "h", seq, pid, {"memory": _claim(mib)})
    _slot(world, "w", 3, 7002, {"owner_identity": _identity(7002), "memory": _claim(400.0)})


def test_page_and_its_assets_are_served_from_this_server(world, monkeypatch):
    with _client(world, monkeypatch) as client:
        page = client.get("/", params={"worktree": str(world.wt)})
        references = _References()
        references.feed(page.text)
        assets = {url: client.get(url) for url in references.urls}

    assert page.status_code == 200
    assert page.headers["content-type"].startswith("text/html")
    assert page.headers["content-security-policy"] == "default-src 'self'"
    assert sorted(references.urls) == ["/assets/dashboard.css", "/assets/dashboard.js"]
    for url, response in assets.items():
        assert response.status_code == 200, url
        assert "http://" not in response.text
        assert "https://" not in response.text
    assert assets["/assets/dashboard.css"].headers["content-type"].startswith("text/css")
    assert "javascript" in assets["/assets/dashboard.js"].headers["content-type"]
    css = assets["/assets/dashboard.css"].text
    assert "url(" not in css
    assert "@import" not in css


def test_page_renders_the_snapshot_with_results_collapsed_and_polls(world, monkeypatch, tmp_path):
    _queued_world(world)
    _job(
        world,
        "run-p",
        world.wt,
        target="t_pass",
        report={"criterion_key": "k", "criterion_met": True},
    )
    _job(world, "run-f", world.wt, target="<b>x</b>", exit_code=1)
    _job(world, "run-bare", world.wt, target=None, ended=None)
    _job(world, "run-leak", world.other, target="leak")

    with _client(world, monkeypatch) as client:
        first = _reply(client, world.wt)
        _job(world, "run-new", world.wt, target="t_new", ended="2026-10-08T10:00:10Z")
        second = _reply(client, world.wt)
        probe = _run_page(
            _page_js(client),
            {"search": _search(world.wt), "responses": [first, second]},
            tmp_path,
        )

    assert len(probe["fetches"]) == 2
    for fetch in probe["fetches"]:
        url = urlparse(fetch["url"])
        assert url.path == "/api/snapshot"
        assert parse_qs(url.query) == {"worktree": [str(world.wt)]}
    assert probe["delays"] == [3000, 3000]
    one, two = probe["renders"]
    report_url = next(j for j in first["body"]["results"]["jobs"] if j["report_url"])["report_url"]
    for text in (
        str(world.wt),
        "booley/wt",
        "6.0 GiB",
        "position 1",
        '<details id="results-section">',
        "2 passed",
        "1 failed",
        f'href="{html.escape(report_url)}"',
        "&lt;b&gt;x&lt;/b&gt;",
    ):
        assert text in one, text
    assert "<b>x</b>" not in one
    for bad in ("null", "undefined", "NaN", "1970"):
        assert bad not in one, bad
    assert "leak" not in one
    assert "k · met" not in one
    assert "t_new" not in one
    assert one.count(' failed"') == 1
    assert "t_new" in two


def test_results_open_state_is_remembered_in_the_browser(world, monkeypatch, tmp_path):
    _job(world, "run-a", world.wt, target="t_a")

    with _client(world, monkeypatch) as client:
        first = _reply(client, world.wt)
        _job(world, "run-b", world.wt, target="t_b", ended="2026-10-08T10:00:10Z")
        second = _reply(client, world.wt)
        js = _page_js(client)
    probes = {
        storage: _run_page(
            js,
            {
                "search": _search(world.wt),
                "storage": storage,
                "toggle_after": 0,
                "responses": [first, second],
            },
            tmp_path / storage,
        )
        for storage in ("working", "blocked")
    }

    for storage, probe in probes.items():
        one, two = probe["renders"]
        assert '<details id="results-section">' in one, storage
        assert '<details id="results-section" open>' in two, storage
        assert "t_b" in two, storage
    assert probes["working"]["storage"] == {"booley.dashboard.results-open": "true"}
    assert probes["blocked"]["storage"] == {}


def test_unchanged_results_are_not_rewritten(world, monkeypatch, tmp_path):
    _job(
        world, "run-r", world.wt, target="t_run", status="running", ended=None, pid=7001, live=True
    )
    _job(world, "run-a", world.wt, target="t_a")

    with _client(world, monkeypatch) as client:
        first = _reply(client, world.wt)
        monkeypatch.setattr(_snapshot_module(), "wall_clock", lambda: NOW + 5)
        second = _reply(client, world.wt)
        probe = _run_page(
            _page_js(client),
            {"search": _search(world.wt), "responses": [first, second]},
            tmp_path,
        )

    assert first["body"]["results"] == second["body"]["results"]
    assert first["body"]["now"] != second["body"]["now"]
    assert probe["writes"] == {"live": 2, "results": 1}


def test_empty_worktree_says_no_jobs_have_run(world, monkeypatch, tmp_path):
    _job(world, "run-main", world.main, target="elsewhere")
    (world.cgroup / "memory.max").write_bytes(b"max\n")

    with _client(world, monkeypatch) as client:
        reply = _reply(client, world.wt)
        probe = _run_page(
            _page_js(client),
            {"search": _search(world.wt), "responses": [reply]},
            tmp_path,
        )

    assert reply["body"]["header"]["resources"]["memory_limit_bytes"] is None
    (render,) = probe["renders"]
    assert "No Jobs have run in this worktree yet." in render
    assert "(no limit)" in render
    assert "<details" not in render
    for bad in ("NaN", "undefined", "null", "elsewhere"):
        assert bad not in render


def test_page_shows_the_snapshot_error_instead_of_jobs(world, tmp_path, monkeypatch):
    deleted = tmp_path / "deleted"
    git(world.main, "worktree", "add", "-q", "-b", "booley/deleted", str(deleted), "HEAD")
    outside = tmp_path / "outside"
    outside.mkdir()
    nowhere = tmp_path / "nowhere"
    for path in (deleted, outside, nowhere):
        _job(world, f"run-{path.name}", path, target="leak")
    _job(world, "run-earlier", world.wt, target="earlier")

    with _client(world, monkeypatch) as client:
        good_wt = _reply(client, world.wt)
        good_deleted = _reply(client, deleted)
        shutil.rmtree(deleted)
        scenarios = {
            "missing": ("", [_reply(client, "")]),
            "unknown": (_search(nowhere), [_reply(client, nowhere)]),
            "outside": (_search(outside), [_reply(client, outside)]),
            "deleted": (_search(deleted), [good_deleted, _reply(client, deleted)]),
            "server-gone": (_search(world.wt), [good_wt, {"reject": "connection refused"}]),
            "not-json": (_search(world.wt), [good_wt, {"status": 500, "text": "oops"}]),
        }
        js = _page_js(client)
    assert scenarios["deleted"][1][-1]["status"] == 404

    for kind, (search, responses) in scenarios.items():
        probe = _run_page(js, {"search": search, "responses": responses}, tmp_path / kind)
        last = probe["renders"][-1]
        expected = {"server-gone": "did not answer", "not-json": "HTTP 500"}.get(
            kind
        ) or responses[-1]["body"]["error"]
        assert expected in last, kind
        assert 'class="error"' in last, kind
        for bad in ("leak", "earlier", "<details"):
            assert bad not in last, (kind, bad)
