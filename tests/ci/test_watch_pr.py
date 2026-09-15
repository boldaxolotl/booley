from __future__ import annotations

import datetime as dt
import importlib.util
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

SCRIPT = Path(__file__).parents[2] / ".github/scripts/watch_pr.py"
SPEC = importlib.util.spec_from_file_location("watch_pr", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
watch_pr = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = watch_pr
SPEC.loader.exec_module(watch_pr)


HEAD = "a" * 40
NEXT_HEAD = "b" * 40
URL = "https://github.com/example/repo/pull/7"


class FakeClock:
    def __init__(self, *, monotonic: float = 0, wall: float = 1_700_000_000) -> None:
        self.current = monotonic
        self.wall = wall
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.current

    def time(self) -> float:
        return self.wall

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.current += seconds
        self.wall += seconds


class FakeTransport:
    def __init__(self, *, ci: list[Any] | None = None, queue: list[Any] | None = None) -> None:
        self.ci = list(ci or [])
        self.queue = list(queue or [])
        self.query_count = 0
        self.deadlines: list[float] = []
        self.cancelled = False

    def ci_snapshot(self, _repo: str, _pr: int, deadline: float) -> watch_pr.Snapshot:
        self.query_count += 1
        self.deadlines.append(deadline)
        return self.ci.pop(0)

    def queue_snapshot(self, _repo: str, _pr: int, deadline: float) -> watch_pr.Snapshot:
        self.query_count += 1
        self.deadlines.append(deadline)
        return self.queue.pop(0)

    def cancel(self) -> None:
        self.cancelled = True


def check(
    name: str = "ci-required",
    bucket: str = "pending",
    *,
    head_sha: str | None = None,
    attempt: int | None = None,
    started_at: str | None = None,
    link: str | None = None,
) -> watch_pr.Check:
    return watch_pr.Check(name, bucket, bucket, link, head_sha, attempt, started_at)


def snapshot(
    *,
    head: str | None = HEAD,
    state: str = "open",
    merged_at: str | None = None,
    labels: set[str] | None = None,
    checks: tuple[watch_pr.Check, ...] = (),
    mergify_state: str | None = None,
    estimate: str | None = None,
    comments: tuple[watch_pr.Comment, ...] = (),
) -> watch_pr.Snapshot:
    return watch_pr.Snapshot(
        URL,
        state,
        merged_at,
        head,
        frozenset(labels or set()),
        checks,
        mergify_state,
        estimate,
        comments,
    )


def run_watch(
    transport: FakeTransport,
    clock: FakeClock,
    *,
    mode: str,
    timeout: int = 200,
    expected_head: str | None = HEAD,
) -> watch_pr.WatchResult:
    return watch_pr.watch(
        transport,
        mode=mode,
        repo="example/repo",
        pr=7,
        timeout_seconds=timeout,
        expected_head=expected_head,
        clock=clock,
    )


def test_ci_waits_for_required_checks_and_uses_sixty_second_cadence() -> None:
    transport = FakeTransport(ci=[snapshot(), snapshot(checks=(check(bucket="pass"),))])
    clock = FakeClock()

    result = run_watch(transport, clock, mode="ci")

    assert result.outcome == "ci_passed"
    assert clock.sleeps == [60]
    assert transport.query_count == 2


def test_ci_empty_checks_never_establish_success() -> None:
    transport = FakeTransport(ci=[snapshot(), snapshot()])
    clock = FakeClock()

    result = run_watch(transport, clock, mode="ci", timeout=100)

    assert result.outcome == "timeout"
    assert clock.sleeps == [60, 40]


@pytest.mark.parametrize("bucket", ["skipping", "pending"])
def test_ci_non_pass_buckets_remain_unresolved(bucket: str) -> None:
    decision = watch_pr.classify_ci(snapshot(checks=(check(bucket=bucket),)), HEAD)

    assert decision.outcome == "pending"
    assert decision.terminal is False


def test_ci_cancelled_required_check_is_actionable() -> None:
    decision = watch_pr.classify_ci(
        snapshot(checks=(check(bucket="cancel", link="https://ci.example/run/1"),)), HEAD
    )

    assert decision.outcome == "check_failed"
    assert decision.links == ("https://ci.example/run/1",)


def test_ci_ignores_old_failure_when_newer_same_head_attempt_is_pending() -> None:
    checks = (
        check(bucket="fail", head_sha=HEAD, attempt=1),
        check(bucket="pending", head_sha=HEAD, attempt=2),
    )

    assert watch_pr.classify_ci(snapshot(checks=checks), HEAD).outcome == "pending"


def test_ci_ignores_failure_from_an_old_head() -> None:
    decision = watch_pr.classify_ci(
        snapshot(checks=(check(bucket="fail", head_sha=NEXT_HEAD),)), HEAD
    )

    assert decision.outcome == "pending"


def test_ci_returns_head_change_and_closure() -> None:
    assert watch_pr.classify_ci(snapshot(head=NEXT_HEAD), HEAD).outcome == "head_changed"
    assert watch_pr.classify_ci(snapshot(state="closed"), HEAD).outcome == "closed"


def test_queue_baselines_existing_control_comment_and_waits_ten_minutes() -> None:
    existing = watch_pr.Comment("1", "@mergifyio queue default", "owner")
    transport = FakeTransport(
        queue=[
            snapshot(labels={"queued"}, comments=(existing,)),
            snapshot(merged_at="2026-09-15T10:00:00Z", comments=(existing,)),
        ]
    )
    clock = FakeClock()

    result = run_watch(transport, clock, mode="queue", timeout=700)

    assert result.outcome == "merged"
    assert clock.sleeps == [600]


def test_queue_uses_a_future_estimate_and_recomputes_after_head_update() -> None:
    first_estimate = (
        dt.datetime.fromtimestamp(1_700_000_120, dt.UTC).isoformat().replace("+00:00", "Z")
    )
    second_estimate = (
        dt.datetime.fromtimestamp(1_700_000_240, dt.UTC).isoformat().replace("+00:00", "Z")
    )
    transport = FakeTransport(
        queue=[
            snapshot(head=HEAD, labels={"merge-queue-checking"}, estimate=first_estimate),
            snapshot(head=NEXT_HEAD, labels={"merge-queue-checking"}, estimate=second_estimate),
            snapshot(head=NEXT_HEAD, merged_at="2026-09-15T10:00:00Z"),
        ]
    )
    clock = FakeClock()

    result = run_watch(transport, clock, mode="queue", timeout=500)

    assert result.outcome == "merged"
    assert clock.sleeps == [120, 120]


def test_queue_does_not_treat_old_failure_as_current_attempt() -> None:
    checks = (
        check(bucket="fail", head_sha=HEAD, attempt=1),
        check(bucket="pending", head_sha=HEAD, attempt=2),
    )
    transport = FakeTransport(queue=[snapshot(labels={"queued"}, checks=checks)])
    result = run_watch(transport, FakeClock(), mode="queue", timeout=1)

    assert result.outcome == "timeout"


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        (snapshot(labels={"dequeued"}), "dequeued"),
        (
            snapshot(
                comments=(watch_pr.Comment("2", "@mergifyio dequeue", "other-owner"),),
            ),
            "competing_control",
        ),
        (snapshot(mergify_state="failure"), "queue_failed"),
        (snapshot(state="closed"), "closed"),
    ],
)
def test_queue_terminal_states(state: watch_pr.Snapshot, expected: str) -> None:
    memory = watch_pr.QueueMemory(initialized=True)

    assert (
        watch_pr.classify_queue(state, memory, now=1_700_000_000, remaining_seconds=600).outcome
        == expected
    )


def test_queue_transient_label_inconsistency_stays_pending() -> None:
    first = snapshot(labels=set(), mergify_state="pending")
    second = snapshot(labels={"queued"}, mergify_state="pending")
    transport = FakeTransport(queue=[first, second])

    result = run_watch(transport, FakeClock(), mode="queue", timeout=100)

    assert result.outcome == "timeout"


def test_malformed_external_response_is_rejected() -> None:
    with pytest.raises(watch_pr.WatchError, match="statusCheckRollup"):
        watch_pr._snapshot_from_json(
            {
                "url": URL,
                "state": "OPEN",
                "mergedAt": None,
                "headRefOid": HEAD,
                "labels": [],
                "statusCheckRollup": {"bad": True},
            },
            [],
            [],
            require_comments=False,
        )


def test_paginated_comments_are_flattened_for_control_baselining() -> None:
    parsed = watch_pr._snapshot_from_json(
        {
            "url": URL,
            "state": "OPEN",
            "mergedAt": None,
            "headRefOid": HEAD,
            "labels": [],
            "statusCheckRollup": [],
        },
        [],
        [[{"id": 1, "body": "@mergifyio queue default", "user": {"login": "owner"}}], []],
        require_comments=True,
    )

    assert parsed.comments == (watch_pr.Comment("1", "@mergifyio queue default", "owner"),)


def test_transient_reads_are_retried_at_most_three_times() -> None:
    transport = watch_pr.GhTransport(clock=FakeClock())
    clock = transport._clock
    calls = 0

    def fake_run(*_args: Any, **_kwargs: Any) -> dict[str, bool]:
        nonlocal calls
        calls += 1
        if calls < 3:
            raise ConnectionError("network unavailable")
        return {"ok": True}

    transport._run = fake_run  # type: ignore[method-assign]

    assert transport._read(["pr", "view"], 100) == {"ok": True}
    assert calls == 3
    assert clock.sleeps == [1, 2]


def test_cli_smoke_uses_read_only_gh_commands_and_emits_two_lines(tmp_path: Path) -> None:
    fake_gh = tmp_path / "gh"
    log = tmp_path / "commands.log"
    fake_gh.write_text(
        "#!/usr/bin/python3\n"
        "import json, os, sys\n"
        f"open({str(log)!r}, 'a', encoding='utf-8').write(' '.join(sys.argv[1:]) + '\\n')\n"
        "if sys.argv[1:3] == ['pr', 'view']:\n"
        f"    print(json.dumps({{'url': {URL!r}, 'state': 'OPEN', 'mergedAt': None, 'headRefOid': {HEAD!r}, 'labels': [], 'statusCheckRollup': []}}))\n"
        "elif sys.argv[1:3] == ['pr', 'checks']:\n"
        "    print(json.dumps([{'name': 'ci-required', 'state': 'SUCCESS', 'bucket': 'pass', 'link': 'https://ci.example/run/1', 'startedAt': None, 'completedAt': None, 'workflow': 'CI'}]))\n"
        "else:\n"
        "    raise SystemExit('unexpected command')\n",
        encoding="utf-8",
    )
    fake_gh.chmod(fake_gh.stat().st_mode | 0o111)
    result = subprocess.run(
        [
            "python3",
            str(SCRIPT),
            "--repo",
            "example/repo",
            "--pr",
            "7",
            "--mode",
            "ci",
            "--expected-head",
            HEAD,
            "--timeout-seconds",
            "5",
        ],
        env={**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}"},
        capture_output=True,
        text=True,
        check=False,
    )

    lines = result.stdout.splitlines()
    assert result.returncode == 0
    assert len(lines) == 2
    summary = json.loads(lines[1])
    assert summary["outcome"] == "ci_passed"
    assert summary["query_count"] == 3
    commands = log.read_text(encoding="utf-8").splitlines()
    assert "--required" in "\n".join(commands)
    assert all(line.split()[:2] in (["pr", "view"], ["pr", "checks"]) for line in commands)


def test_cancel_terminates_active_child_process(tmp_path: Path) -> None:
    fake_gh = tmp_path / "gh"
    fake_gh.write_text("#!/bin/sh\nsleep 30\n", encoding="utf-8")
    fake_gh.chmod(fake_gh.stat().st_mode | 0o111)
    transport = watch_pr.GhTransport(clock=watch_pr.SystemClock(), executable=str(fake_gh))
    errors: list[BaseException] = []

    def run_child() -> None:
        try:
            transport._run(["api", "read-only"], 20, accepted={0})
        except watch_pr.CancelledWatchError as error:
            errors.append(error)

    worker = threading.Thread(target=run_child)
    worker.start()
    for _ in range(100):
        if transport._active is not None:
            break
        time.sleep(0.01)
    transport.cancel()
    worker.join(timeout=2)

    assert not worker.is_alive()
    assert errors and isinstance(errors[0], watch_pr.CancelledWatchError)
