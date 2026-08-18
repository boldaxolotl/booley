"""Tests for the Interactive Mode idle reaper (ADR 0018 WS2).

The reaper is self-contained; docker is mocked via the injected ``run`` callable.
"""

from __future__ import annotations

import json
import subprocess

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
        assert select_reap(cs, now=150, idle_timeout=1000, max_sessions=4) == []

    def test_idle_reaped(self):
        cs = [_sc("fresh", 0, last=900), _sc("stale", 0, last=100)]
        # now=1000, timeout=500 → stale idle_age=900>500, fresh=100<500
        assert select_reap(cs, now=1000, idle_timeout=500, max_sessions=10) == ["stale"]

    def test_concurrency_cap_stops_oldest(self):
        cs = [_sc("old", 10), _sc("mid", 20), _sc("new", 30)]
        # all fresh; cap=1 → keep newest, stop the two oldest
        out = select_reap(cs, now=30, idle_timeout=10_000, max_sessions=1)
        assert out == ["old", "mid"]

    def test_cap_ignores_already_idle(self):
        # stale is idle-reaped; cap then applies to the 2 survivors only
        cs = [_sc("stale", 0, last=0), _sc("a", 10, last=100), _sc("b", 20, last=100)]
        out = select_reap(cs, now=100, idle_timeout=50, max_sessions=2)
        # stale idle (100-0>50); a,b fresh (100-100=0); survivors=2<=cap → only stale
        assert out == ["stale"]

    def test_union_idle_and_cap(self):
        cs = [
            _sc("stale", 0, last=0),
            _sc("a", 10, last=100),
            _sc("b", 20, last=100),
            _sc("c", 30, last=100),
        ]
        out = select_reap(cs, now=100, idle_timeout=50, max_sessions=1)
        # stale idle; survivors a,b,c (cap 1) → stop oldest 2 = a,b; keep c
        assert out == ["stale", "a", "b"]

    def test_deterministic_order(self):
        cs = [_sc("c", 30), _sc("a", 10), _sc("b", 20)]
        out = select_reap(cs, now=30, idle_timeout=10_000, max_sessions=0)
        assert out == ["a", "b", "c"]  # sorted by started_at


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
        stopped = reaper.reap_once(now=86452.0, idle_timeout=7200, max_sessions=4, run=run)
        assert stopped == []
        assert not run.ran("stop", "abc")

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
        stopped = reaper.reap_once(now=30, idle_timeout=10_000, max_sessions=1, run=run)
        assert stopped == ["old"]
        assert run.ran("stop", "old")
        assert not run.ran("stop", "new")

    def test_reap_once_empty_when_no_containers(self):
        run = FakeRun([(lambda a: a[0] == "ps", _cp(0, stdout=""))])
        assert reaper.reap_once(now=0, idle_timeout=1, max_sessions=1, run=run) == []

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
                (lambda a: a[0] == "exec", _cp(1)),
            ]
        )

        assert reaper.reap_once(now=1000, idle_timeout=10, max_sessions=4, run=run) == ["abc"]
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

        assert reaper.reap_once(now=1000, idle_timeout=10, max_sessions=4, run=run) == []
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
