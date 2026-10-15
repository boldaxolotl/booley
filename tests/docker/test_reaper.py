"""Tests for the Interactive Mode idle reaper (ADR 0018 WS2).

The reaper is self-contained; docker is mocked via the injected ``run`` callable.
"""

from __future__ import annotations

import http.client
import json
import subprocess

import pytest

from booley.docker import reaper
from booley.docker.reaper import SessionContainer, parse_docker_time, select_reap


def _sc(cid, started, last=None):
    return SessionContainer(
        id=cid, name=cid, started_at=started, last_activity=last if last is not None else started
    )


def _cp(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(
        args=["docker"], returncode=returncode, stdout=stdout, stderr=stderr
    )


# ===========================================================================
# select_reap — pure policy
# ===========================================================================


class TestSelectReap:
    def test_nothing_to_reap(self):
        cs = [_sc("a", 100, 100), _sc("b", 100, 100)]
        assert select_reap(cs, now=150, idle_timeout=1000) == []

    def test_idle_reaped(self):
        cs = [_sc("fresh", 0, last=900), _sc("stale", 0, last=100)]
        # now=1000, timeout=500 → stale idle_age=900>500, fresh=100<500
        assert select_reap(cs, now=1000, idle_timeout=500) == ["stale"]

    def test_fresh_sandboxes_are_not_reaped_when_host_is_over_cap(self):
        cs = [_sc("old", 10), _sc("mid", 20), _sc("new", 30)]
        # Capacity is enforced before a start; the reaper only owns idle policy.
        out = select_reap(cs, now=30, idle_timeout=10_000)
        assert out == []

    def test_idle_selection_ignores_fresh_sandboxes(self):
        cs = [_sc("stale", 0, last=0), _sc("a", 10, last=100), _sc("b", 20, last=100)]
        out = select_reap(cs, now=100, idle_timeout=50)
        assert out == ["stale"]

    def test_only_idle_sandboxes_are_selected(self):
        cs = [
            _sc("stale", 0, last=0),
            _sc("a", 10, last=100),
            _sc("b", 20, last=100),
            _sc("c", 30, last=100),
        ]
        out = select_reap(cs, now=100, idle_timeout=50)
        assert out == ["stale"]

    def test_deterministic_order(self):
        cs = [_sc("c", 30, last=40), _sc("a", 10), _sc("b", 20)]
        out = select_reap(cs, now=40, idle_timeout=5)
        assert out == ["a", "b"]  # sorted by started_at; c remains fresh


# ===========================================================================
# parse_docker_time
# ===========================================================================


class TestParseDockerTime:
    def test_nanosecond_z(self):
        # 1970-01-01T00:00:10Z == epoch 10
        assert parse_docker_time("1970-01-01T00:00:10.123456789Z") == 10.123456

    def test_plain_z(self):
        assert parse_docker_time("1970-01-01T00:00:05Z") == 5.0

    def test_zero_time_is_none(self):
        assert parse_docker_time("0001-01-01T00:00:00Z") is None

    def test_empty_is_none(self):
        assert parse_docker_time("") is None
        assert parse_docker_time("   ") is None

    def test_garbage_is_none(self):
        assert parse_docker_time("not-a-time") is None


# ===========================================================================
# collect / reap_once — with mocked docker
# ===========================================================================


class FakeRun:
    def __init__(self, rules):
        self.calls = []
        self.rules = rules

    def __call__(self, args, *, timeout=30):
        self.calls.append(list(args))
        for pred, resp in self.rules:
            if pred(args):
                return resp
        if args[0] == "inspect" and ".Config.Labels" in args[-1]:
            return _cp(0, stdout="{}")
        if args[0] == "inspect" and "ExecIDs" in args[-1]:
            return _cp(stdout="null")
        return _cp(0)

    def ran(self, *needles):
        return any(all(n in c for n in needles) for c in self.calls)


class TestCollectAndReap:
    def test_collect_uses_heartbeat_when_present(self):
        run = FakeRun(
            [
                (lambda a: a[0] == "ps", _cp(0, stdout="abc123\tbooley-sess\n")),
                (
                    lambda a: a[0] == "inspect" and "StartedAt" in a[-1],
                    _cp(0, stdout="1970-01-01T00:00:10Z"),
                ),
                (lambda a: a[0] == "exec", _cp(0, stdout="999.0\n")),
            ]
        )
        cs = reaper.collect(run)
        assert len(cs) == 1
        assert cs[0].started_at == 10.0
        assert cs[0].last_activity == 999.0  # heartbeat wins

    def test_collect_clamps_stale_heartbeat_to_started_at(self):
        # Restarted container: the previous session's heartbeat file survived in
        # the writable layer, but nothing rewrote it. It must not outrank a
        # fresher StartedAt, or the container is reaped on the next tick.
        run = FakeRun(
            [
                (lambda a: a[0] == "ps", _cp(0, stdout="abc\tname\n")),
                (
                    lambda a: a[0] == "inspect" and "StartedAt" in a[-1],
                    _cp(0, stdout="1970-01-02T00:00:00Z"),
                ),
                (lambda a: a[0] == "exec", _cp(0, stdout="10.0\n")),  # ~24h stale
            ]
        )
        cs = reaper.collect(run)
        assert cs[0].started_at == 86400.0
        assert cs[0].last_activity == 86400.0  # start time wins over stale heartbeat

    def test_stale_heartbeat_survives_a_reap_pass(self):
        # End-to-end of the same scenario: booted 52 s ago, day-old heartbeat,
        # 2 h idle timeout → must not be stopped.
        run = FakeRun(
            [
                (lambda a: a[0] == "ps", _cp(0, stdout="abc\tname\n")),
                (
                    lambda a: a[0] == "inspect" and "StartedAt" in a[-1],
                    _cp(0, stdout="1970-01-02T00:00:00Z"),
                ),
                (lambda a: a[0] == "exec", _cp(0, stdout="10.0\n")),
            ]
        )
        stopped = reaper.reap_once(now=86452.0, idle_timeout=7200, run=run)
        assert stopped == []
        assert not run.ran("stop", "abc")

    def test_fresh_revalidation_prevents_idle_stop(self):
        heartbeat_reads = iter(["10.0\n", "999.0\n"])

        def run(args, *, timeout=30):
            del timeout
            if args[0] == "ps":
                return _cp(0, stdout="abc\tname\n")
            if args[0] == "inspect" and "StartedAt" in args[-1]:
                return _cp(0, stdout="1970-01-01T00:00:10Z")
            if args[0] == "exec":
                return _cp(0, stdout=next(heartbeat_reads))
            if args[0] == "inspect" and "ExecIDs" in args[-1]:
                return _cp(stdout="[]")
            return _cp(0, stdout="{}")

        assert reaper.reap_once(now=1000, idle_timeout=100, run=run) == []

    def test_stale_revalidation_still_stops_idle_container(self):
        run = FakeRun(
            [
                (lambda a: a[0] == "ps", _cp(0, stdout="abc\tname\n")),
                (
                    lambda a: a[0] == "inspect" and "StartedAt" in a[-1],
                    _cp(0, stdout="1970-01-01T00:00:10Z"),
                ),
                (lambda a: a[0] == "exec", _cp(0, stdout="10.0\n")),
            ]
        )
        assert reaper.reap_once(now=1000, idle_timeout=100, run=run) == ["abc"]
        assert run.ran("stop", "abc")

    def test_revalidation_failure_does_not_stop_idle_container(self):
        heartbeat_reads = iter(["10.0\n", None])

        def run(args, *, timeout=30):
            del timeout
            if args[0] == "ps":
                return _cp(0, stdout="abc\tname\n")
            if args[0] == "inspect" and "StartedAt" in args[-1]:
                return _cp(0, stdout="1970-01-01T00:00:10Z")
            if args[0] == "exec":
                value = next(heartbeat_reads)
                return (
                    _cp(1, stderr="daemon unavailable") if value is None else _cp(0, stdout=value)
                )
            if args[0] == "inspect" and "ExecIDs" in args[-1]:
                return _cp(stdout="[]")
            return _cp(0, stdout="{}")

        assert reaper.reap_once(now=1000, idle_timeout=100, run=run) == []

    def test_fresh_over_cap_sandboxes_are_never_stopped(self):
        heartbeat_reads = {"old": iter(["999.0\n"]), "new": iter(["999.0\n"])}

        def run(args, *, timeout=30):
            del timeout
            if args[0] == "ps":
                return _cp(0, stdout="old\told\nnew\tnew\n")
            if args[0] == "inspect" and "StartedAt" in args[-1]:
                started = "1970-01-01T00:00:10Z" if "old" in args else "1970-01-01T00:00:20Z"
                return _cp(0, stdout=started)
            if args[0] == "exec":
                return _cp(0, stdout=next(heartbeat_reads[args[1]]))
            if args[0] == "inspect" and "ExecIDs" in args[-1]:
                return _cp(stdout="[]")
            return _cp(0, stdout="{}")

        assert reaper.reap_once(now=1000, idle_timeout=100, run=run) == []

    def test_collect_falls_back_to_started_at(self):
        run = FakeRun(
            [
                (lambda a: a[0] == "ps", _cp(0, stdout="abc\tname\n")),
                (
                    lambda a: a[0] == "inspect" and "StartedAt" in a[-1],
                    _cp(0, stdout="1970-01-01T00:00:42Z"),
                ),
                (lambda a: a[0] == "exec", _cp(1)),  # no heartbeat
            ]
        )
        cs = reaper.collect(run)
        assert cs[0].last_activity == 42.0

    def test_reap_once_stops_selected(self):
        run = FakeRun(
            [
                (lambda a: a[0] == "ps", _cp(0, stdout="old\tc-old\nnew\tc-new\n")),
                (
                    lambda a: a[0] == "inspect" and "old" in a and "StartedAt" in a[-1],
                    _cp(0, stdout="1970-01-01T00:00:10Z"),
                ),
                (
                    lambda a: a[0] == "inspect" and "new" in a and "StartedAt" in a[-1],
                    _cp(0, stdout="1970-01-01T00:00:30Z"),
                ),
                (lambda a: a[0] == "exec", _cp(1)),  # no heartbeats → last=started
            ]
        )
        stopped = reaper.reap_once(now=30, idle_timeout=10_000, run=run)
        assert stopped == []
        assert not run.ran("stop", "old")
        assert not run.ran("stop", "new")

    def test_reap_once_empty_when_no_containers(self):
        run = FakeRun([(lambda a: a[0] == "ps", _cp(0, stdout=""))])
        assert reaper.reap_once(now=0, idle_timeout=1, run=run) == []

    def test_licensed_vscode_session_removes_container_relay_then_networks(self):
        project_id = "b" * 64
        labels = {
            reaper.LICENSE_LABEL: "site-a",
            reaper.PROJECT_LABEL: project_id,
        }
        run = FakeRun(
            [
                (lambda a: a[0] == "ps", _cp(0, stdout="abc\tvscode-devcontainer\n")),
                (
                    lambda a: a[0] == "inspect" and "StartedAt" in a[-1],
                    _cp(0, stdout="1970-01-01T00:00:10Z"),
                ),
                (
                    lambda a: a[0] == "inspect" and ".Config.Labels" in a[-1],
                    _cp(0, stdout=json.dumps(labels)),
                ),
                (
                    lambda a: a[0] == "exec",
                    _cp(0, stdout=f"{reaper._HEARTBEAT_MISSING}\n"),
                ),
            ]
        )

        assert reaper.reap_once(now=1000, idle_timeout=10, run=run) == ["abc"]
        session_id = project_id[:16]
        lifecycle = [call for call in run.calls if call[0] in {"stop", "rm", "network"}]
        assert lifecycle == [
            ["stop", "abc"],
            ["rm", "-f", "abc"],
            ["rm", "-f", f"booley-license-relay-{session_id}"],
            ["network", "rm", f"booley-license-outbound-{session_id}"],
            ["network", "rm", f"booley-license-private-{session_id}"],
        ]

    def test_license_ownership_inspect_failure_does_not_strand_stopped_session(self):
        run = FakeRun(
            [
                (lambda a: a[0] == "ps", _cp(0, stdout="abc\tlicensed\n")),
                (
                    lambda a: a[0] == "inspect" and "StartedAt" in a[-1],
                    _cp(0, stdout="1970-01-01T00:00:10Z"),
                ),
                (
                    lambda a: a[0] == "inspect" and ".Config.Labels" in a[-1],
                    _cp(1, stderr="daemon unavailable"),
                ),
                (lambda a: a[0] == "exec", _cp(1)),
            ]
        )

        assert reaper.reap_once(now=1000, idle_timeout=10, run=run) == []
        assert not run.ran("stop", "abc")

    def test_licensed_cleanup_reports_every_residual(self):
        def fail(args, *, timeout=30):
            del timeout
            return _cp(1, stderr=f"cannot remove {args[-1]}")

        assert reaper.cleanup_licensed_session("abc", "c" * 64, fail) == (
            "container:abc",
            "container:booley-license-relay-cccccccccccccccc",
            "network:booley-license-outbound-cccccccccccccccc",
            "network:booley-license-private-cccccccccccccccc",
        )


def test_attached_sandbox_is_not_stopped():
    run = _attachment_run()
    assert (
        reaper.reap_once(
            now=1000,
            idle_timeout=100,
            run=run,
            exec_inspector=lambda eid: {
                "Running": True,
                "ProcessConfig": {"entrypoint": "shell", "arguments": []},
            },
        )
        == []
    )
    assert not run.ran("stop", "abc")


def _attachment_run(ids='["abc123"]', heartbeat="10"):
    return FakeRun(
        [
            (lambda a: a[0] == "ps" and "-aq" not in a, _cp(stdout="abc\tname\n")),
            (
                lambda a: a[0] == "inspect" and "StartedAt" in a[-1],
                _cp(stdout="1970-01-01T00:00:10Z"),
            ),
            (lambda a: a[0] == "inspect" and "ExecIDs" in a[-1], _cp(stdout=ids)),
            (lambda a: a[0] == "exec", _cp(stdout=heartbeat)),
        ]
    )


@pytest.mark.parametrize("heartbeat", ["10", reaper._HEARTBEAT_MISSING])
@pytest.mark.parametrize("entrypoint", ["code", "sh", "sleep", "arbitrary"])
def test_live_exec_preserves_stale_or_missing_heartbeat(heartbeat, entrypoint, caplog):
    run = _attachment_run(heartbeat=heartbeat)
    with caplog.at_level("INFO"):
        assert (
            reaper.reap_once(
                now=10000,
                idle_timeout=100,
                run=run,
                exec_inspector=lambda eid: {
                    "Running": True,
                    "ProcessConfig": {"entrypoint": entrypoint, "arguments": ["sentinel-secret"]},
                },
            )
            == []
        )
    assert "live client attachments" in caplog.text
    assert "sentinel-secret" not in caplog.text
    assert not run.ran("stop")


@pytest.mark.parametrize(
    "value",
    [
        {"Running": False},
        {
            "Running": True,
            "ProcessConfig": {
                "entrypoint": "sh",
                "arguments": ["-c", reaper._HEARTBEAT_READ_SCRIPT],
            },
        },
    ],
)
def test_exited_exec_and_exact_reaper_reader_do_not_protect(value, caplog):
    run = _attachment_run()
    with caplog.at_level("INFO"):
        assert reaper.reap_once(
            now=1000, idle_timeout=100, run=run, exec_inspector=lambda eid: value
        ) == ["abc"]
    assert "no live client attachments" in caplog.text


@pytest.mark.parametrize(
    "value",
    [
        {},
        {"Running": 1},
        {"Running": "true"},
        {"Running": True},
        {"Running": True, "ProcessConfig": {"entrypoint": "sh", "arguments": None}},
    ],
)
def test_malformed_exec_preserves_sandbox(value, caplog):
    run = _attachment_run()
    assert (
        reaper.reap_once(now=1000, idle_timeout=100, run=run, exec_inspector=lambda eid: value)
        == []
    )
    assert "unknown client exec inspection" in caplog.text


@pytest.mark.parametrize(
    "error",
    [
        OSError(),
        TimeoutError(),
        ValueError(),
        http.client.BadStatusLine("broken"),
        http.client.IncompleteRead(b""),
    ],
)
def test_inspector_failure_in_either_phase_preserves_sandbox(error):
    for final in [False, True]:
        run = _attachment_run()
        observations = iter([False, True] if final else [True])

        def inspect(eid, observations=observations):
            if next(observations):
                raise error
            return {"Running": False}

        assert reaper.reap_once(now=1000, idle_timeout=100, run=run, exec_inspector=inspect) == []
        assert not run.ran("stop")


def test_attachment_appearing_at_final_inspection_preserves_licensed_topology():
    run = _attachment_run()
    run.rules.insert(
        0,
        (
            lambda a: a[0] == "inspect" and "Labels" in a[-1],
            _cp(
                stdout=json.dumps(
                    {"booley.license-profile": "test", "booley.project-id": "a" * 64}
                )
            ),
        ),
    )
    states = iter([False, True])
    assert (
        reaper.reap_once(
            now=1000,
            idle_timeout=100,
            run=run,
            exec_inspector=lambda eid: {
                "Running": next(states),
                "ProcessConfig": {"entrypoint": "sh", "arguments": []},
            },
        )
        == []
    )
    assert not run.ran("stop")
    assert not run.ran("rm")


@pytest.mark.parametrize("ids", ["null", "[]"])
def test_empty_exec_ids_reap_using_start_when_heartbeat_missing(ids):
    assert reaper.reap_once(
        now=1000, idle_timeout=100, run=_attachment_run(ids, reaper._HEARTBEAT_MISSING)
    ) == ["abc"]


@pytest.mark.parametrize("ids", ["{}", '"abc"', "[1]", '["../../x"]', "invalid"])
def test_invalid_exec_ids_keep(ids):
    assert reaper.reap_once(now=1000, idle_timeout=100, run=_attachment_run(ids)) == []


@pytest.mark.parametrize(
    "body,status,error",
    [
        (b'{"Running": false}', 200, None),
        (b"{}", 404, ValueError),
        (b"[]", 200, ValueError),
        (b"broken", 200, ValueError),
        (b"\xff", 200, ValueError),
        (b"x" * (reaper._EXEC_RESPONSE_LIMIT + 1), 200, ValueError),
    ],
)
def test_engine_transport_validates_and_closes(monkeypatch, body, status, error):
    calls = []

    class Response:
        def read(self, limit):
            assert limit == reaper._EXEC_RESPONSE_LIMIT + 1
            return body

    response = Response()
    response.status = status

    class Connection:
        def request(self, method, path):
            calls.append((method, path))

        def getresponse(self):
            return response

        def close(self):
            calls.append("closed")

    monkeypatch.setattr(reaper, "_EngineConnection", Connection)
    if error:
        with pytest.raises(error):
            reaper.inspect_exec("abc123")
    else:
        assert reaper.inspect_exec("abc123") == {"Running": False}
    assert calls == [("GET", "/exec/abc123/json"), "closed"]


def test_production_cli_pins_socket_and_scrubs_context(monkeypatch):
    monkeypatch.setenv("DOCKER_HOST", "tcp://elsewhere")
    monkeypatch.setenv("DOCKER_CONTEXT", "other")

    def run(args, **kwargs):
        assert args[:3] == ["docker", "--host", "unix:///var/run/docker.sock"]
        assert "DOCKER_HOST" not in kwargs["env"]
        assert "DOCKER_CONTEXT" not in kwargs["env"]
        return _cp()

    monkeypatch.setattr(subprocess, "run", run)
    reaper._run(["ps"])


def test_socket_transport_has_finite_timeout_and_fixed_endpoint(monkeypatch):
    calls = []

    class Socket:
        def settimeout(self, timeout):
            assert 0 < timeout <= 5

        def connect(self, path):
            calls.append(path)

        def close(self):
            calls.append("closed")

    # The production image is Linux; exercise its adapter with a complete fake on Windows.
    monkeypatch.setattr(reaper.socket, "AF_UNIX", 1, raising=False)
    monkeypatch.setattr(reaper.socket, "socket", lambda *args: Socket())
    connection = reaper._EngineConnection()
    connection.connect()
    connection.close()
    assert calls == ["/var/run/docker.sock", "closed"]


def test_definite_attachment_wins_over_other_unknown_exec():
    run = _attachment_run('["abc123", "def456"]')

    def inspect(eid):
        if eid == "abc123":
            raise ValueError("disappeared")
        return {"Running": True, "ProcessConfig": {"entrypoint": "cat", "arguments": []}}

    containers = reaper.collect(run, exec_inspector=inspect)
    assert containers[0].attachments == 1
    assert reaper.select_reap(containers, now=10000, idle_timeout=1) == []


@pytest.mark.parametrize(
    "error", [http.client.BadStatusLine("broken"), http.client.IncompleteRead(b"")]
)
def test_http_failure_closes_connection(monkeypatch, error):
    closed = []

    class Connection:
        def request(self, *args):
            raise error

        def close(self):
            closed.append(True)

    monkeypatch.setattr(reaper, "_EngineConnection", Connection)
    with pytest.raises(http.client.HTTPException):
        reaper.inspect_exec("abc123")
    assert closed == [True]


@pytest.mark.parametrize("final", [False, True])
@pytest.mark.parametrize("timeout", [False, True])
def test_exec_id_inspection_failure_in_either_phase_keeps(final, timeout):
    runner = _attachment_run()
    observations = iter([False, True] if final else [True])

    def run(args):
        if args[0] == "inspect" and "ExecIDs" in args[-1] and next(observations):
            if timeout:
                raise subprocess.TimeoutExpired("docker", 30)
            return _cp(1)
        return runner(args)

    assert (
        reaper.reap_once(
            now=1000, idle_timeout=100, run=run, exec_inspector=lambda eid: {"Running": False}
        )
        == []
    )
    assert not runner.ran("stop")


def test_concurrent_heartbeat_reader_does_not_hide_client_exec():
    run = _attachment_run('["abc123", "def456"]')

    def inspect(eid):
        arguments = ["-c", reaper._HEARTBEAT_READ_SCRIPT] if eid == "abc123" else []
        return {"Running": True, "ProcessConfig": {"entrypoint": "sh", "arguments": arguments}}

    assert reaper.collect(run, exec_inspector=inspect)[0].attachments == 1


def test_licensed_attachment_at_collection_preserves_topology():
    run = _attachment_run()
    run.rules.insert(
        0,
        (
            lambda a: a[0] == "inspect" and "Labels" in a[-1],
            _cp(
                stdout=json.dumps(
                    {"booley.license-profile": "test", "booley.project-id": "a" * 64}
                )
            ),
        ),
    )
    assert (
        reaper.reap_once(
            now=1000,
            idle_timeout=100,
            run=run,
            exec_inspector=lambda eid: {
                "Running": True,
                "ProcessConfig": {"entrypoint": "sh", "arguments": []},
            },
        )
        == []
    )
    assert not run.ran("stop")
    assert not run.ran("rm")
    assert not run.ran("network", "rm")
