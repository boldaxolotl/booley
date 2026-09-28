from __future__ import annotations

import importlib.util
import json
import os
import signal
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


def write_fake_gh(tmp_path: Path, source: str) -> Path:
    script = tmp_path / "fake_gh.py"
    script.write_text(source, encoding="utf-8")
    if os.name == "nt":
        launcher = tmp_path / "gh.cmd"
        launcher.write_text(
            f'@echo off\r\n"{sys.executable}" "{script}" %*\r\n',
            encoding="utf-8",
        )
    else:
        launcher = tmp_path / "gh"
        launcher.write_text(f"#!/usr/bin/env python3\n{source}", encoding="utf-8")
        launcher.chmod(launcher.stat().st_mode | 0o111)
    return launcher


def run_script(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["python3", str(SCRIPT), *args],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def write_passing_ci_gh(tmp_path: Path, log: Path, long_url: str) -> None:
    write_fake_gh(
        tmp_path,
        "import json, os, sys\n"
        f"open({str(log)!r}, 'a', encoding='utf-8').write(' '.join(sys.argv[1:]) + '\\n')\n"
        "if sys.argv[1:3] == ['pr', 'view']:\n"
        f"    print(json.dumps({{'url': {long_url!r}, 'state': 'OPEN', 'mergedAt': None, 'headRefOid': {HEAD!r}, 'baseRefName': 'main', 'labels': [], 'statusCheckRollup': []}}))\n"
        "elif sys.argv[1:3] == ['pr', 'checks']:\n"
        "    print(json.dumps([{'name': 'ci-required', 'state': 'SUCCESS', 'bucket': 'pass', 'link': 'https://ci.example/run/1', 'startedAt': None, 'completedAt': None, 'workflow': 'CI'}]))\n"
        "elif sys.argv[1:3] == ['run', 'list']:\n"
        "    print('[]')\n"
        "elif sys.argv[1] == 'api':\n"
        "    print(json.dumps([{'type': 'required_status_checks', 'parameters': {'required_status_checks': [{'context': 'ci-required'}]}}]))\n"
        "else:\n"
        "    raise SystemExit('unexpected command')\n",
    )


def _missing_checks_fake_gh_source(run: dict[str, object], confidential: dict[str, object]) -> str:
    pull = {
        "url": URL,
        "state": "OPEN",
        "mergedAt": None,
        "headRefOid": HEAD,
        "baseRefName": "main",
        "labels": [],
        "statusCheckRollup": [],
    }
    rules = [
        {
            "type": "required_status_checks",
            "parameters": {
                "required_status_checks": [
                    {"context": "ci-required"},
                    {"context": "confidential-content"},
                ]
            },
        }
    ]
    return (
        "import json, sys\n"
        "if sys.argv[1:3] == ['pr', 'view']:\n"
        f"    print(json.dumps({pull!r}))\n"
        "elif sys.argv[1:3] == ['pr', 'checks']:\n"
        "    print('[]')\n"
        "elif sys.argv[1:3] == ['run', 'list']:\n"
        f"    print(json.dumps({[run, confidential]!r}))\n"
        "elif sys.argv[1] == 'api':\n"
        f"    print(json.dumps({rules!r}))\n"
    )


class FakeClock:
    def __init__(self, *, monotonic: float = 0) -> None:
        self.current = monotonic
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.current

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.current += seconds


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
    workflow: str | None = "CI",
) -> watch_pr.Check:
    return watch_pr.Check(name, bucket, bucket, link, head_sha, attempt, started_at, workflow)


def producer_run(
    workflow: str = "Tests",
    event: str = "pull_request",
    status: str = "in_progress",
    *,
    conclusion: str | None = None,
    head_sha: str = HEAD,
    attempt: int = 1,
    created_at: str = "2026-09-28T10:00:00Z",
    database_id: int = 100,
    url: str = "https://ci.example/run/100",
) -> watch_pr.ProducerRun:
    return watch_pr.ProducerRun(
        workflow,
        event,
        status,
        conclusion,
        head_sha,
        attempt,
        created_at,
        database_id,
        url,
    )


def snapshot(
    *,
    head: str | None = HEAD,
    state: str = "open",
    merged_at: str | None = None,
    labels: set[str] | None = None,
    checks: tuple[watch_pr.Check, ...] = (),
    required_check_names: set[str] | None = None,
    mergify_state: str | None = None,
    comments: tuple[watch_pr.Comment, ...] = (),
    producer_runs: tuple[watch_pr.ProducerRun, ...] = (),
) -> watch_pr.Snapshot:
    if required_check_names is None:
        required_check_names = {item.name for item in checks}
    return watch_pr.Snapshot(
        URL,
        state,
        merged_at,
        head,
        frozenset(labels or set()),
        checks,
        frozenset(required_check_names),
        mergify_state,
        comments,
        producer_runs,
    )


def run_watch(
    transport: FakeTransport,
    clock: FakeClock,
    *,
    mode: str,
    timeout: int = 200,
    expected_head: str | None = HEAD,
    producer_registration_grace: int = 120,
) -> watch_pr.WatchResult:
    return watch_pr.watch(
        transport,
        mode=mode,
        repo="example/repo",
        pr=7,
        timeout_seconds=timeout,
        expected_head=expected_head,
        producer_registration_grace_seconds=producer_registration_grace,
        clock=clock,
    )


def test_ci_waits_for_required_checks_and_uses_sixty_second_cadence() -> None:
    transport = FakeTransport(
        ci=[
            snapshot(
                required_check_names={"ci-required"},
                producer_runs=(producer_run(),),
            ),
            snapshot(
                checks=(check(bucket="pass"),),
                required_check_names={"ci-required"},
            ),
        ]
    )
    clock = FakeClock()

    result = run_watch(transport, clock, mode="ci")

    assert result.outcome == "ci_passed"
    assert clock.sleeps == [60]
    assert transport.query_count == 2


def test_ci_empty_required_rules_fail_closed() -> None:
    transport = FakeTransport(ci=[snapshot()])

    with pytest.raises(watch_pr.WatchError, match="no required checks"):
        run_watch(transport, FakeClock(), mode="ci", timeout=100)


def test_ci_empty_required_rules_fail_closed_even_with_visible_checks() -> None:
    state = snapshot(checks=(check(bucket="pass"),), required_check_names=set())

    with pytest.raises(watch_pr.WatchError, match="no required checks"):
        run_watch(FakeTransport(ci=[state]), FakeClock(), mode="ci")


def test_ci_reports_required_checks_missing_when_producer_never_registers() -> None:
    state = snapshot(required_check_names={"ci-required", "confidential-content"})
    transport = FakeTransport(ci=[state, state, state])

    result = run_watch(transport, FakeClock(), mode="ci", timeout=180)

    assert result.outcome == "required_checks_missing"
    assert result.missing_checks == ("ci-required", "confidential-content")


def test_ci_waits_beyond_registration_grace_while_producer_is_running() -> None:
    pending = snapshot(
        required_check_names={"ci-required"},
        producer_runs=(producer_run(),),
    )
    passed = snapshot(
        checks=(check(bucket="pass"),),
        required_check_names={"ci-required"},
    )
    transport = FakeTransport(ci=[pending, pending, pending, passed])
    clock = FakeClock()

    result = run_watch(transport, clock, mode="ci", timeout=240)

    assert result.outcome == "ci_passed"
    assert clock.sleeps == [60, 60, 60]


@pytest.mark.parametrize(
    ("status", "conclusion"),
    [("action_required", None), ("completed", "action_required")],
)
def test_ci_waits_while_producer_requires_approval(status: str, conclusion: str | None) -> None:
    pending = snapshot(
        required_check_names={"ci-required"},
        producer_runs=(producer_run(status=status, conclusion=conclusion),),
    )

    result = run_watch(
        FakeTransport(ci=[pending, pending, pending]),
        FakeClock(),
        mode="ci",
        timeout=180,
    )

    assert result.outcome == "timeout"


def test_ci_reports_completed_producer_that_did_not_publish_context() -> None:
    state = snapshot(
        required_check_names={"confidential-content"},
        producer_runs=(
            producer_run(
                workflow="Confidential content",
                event="pull_request_target",
                status="completed",
                conclusion="success",
            ),
        ),
    )

    result = run_watch(FakeTransport(ci=[state]), FakeClock(), mode="ci")

    assert result.outcome == "required_checks_missing"
    assert result.missing_checks == ("confidential-content",)
    assert result.links == ("https://ci.example/run/100",)


def test_ci_does_not_accept_producer_from_another_head_or_event() -> None:
    state = snapshot(
        required_check_names={"ci-required"},
        producer_runs=(
            producer_run(head_sha=NEXT_HEAD),
            producer_run(event="push", database_id=101),
        ),
    )

    result = run_watch(
        FakeTransport(ci=[state, state, state]), FakeClock(), mode="ci", timeout=180
    )

    assert result.outcome == "required_checks_missing"


def test_ci_does_not_accept_producer_from_another_workflow() -> None:
    state = snapshot(
        required_check_names={"ci-required"},
        producer_runs=(producer_run(workflow="Another workflow"),),
    )

    result = run_watch(
        FakeTransport(ci=[state, state, state]), FakeClock(), mode="ci", timeout=180
    )

    assert result.outcome == "required_checks_missing"


def test_ci_uses_newest_producer_attempt() -> None:
    state = snapshot(
        required_check_names={"ci-required"},
        producer_runs=(
            producer_run(status="completed", conclusion="failure"),
            producer_run(attempt=2, created_at="2026-09-28T10:01:00Z", database_id=101),
        ),
    )

    decision = watch_pr.classify_missing_producers(
        state,
        HEAD,
        watch_pr.CiMemory(),
        now=500,
        registration_grace_seconds=120,
    )

    assert decision.outcome == "pending"


def test_ci_producer_appearance_clears_registration_timer() -> None:
    memory = watch_pr.CiMemory()
    missing = snapshot(required_check_names={"ci-required"})
    running = snapshot(required_check_names={"ci-required"}, producer_runs=(producer_run(),))

    watch_pr.classify_missing_producers(
        missing, HEAD, memory, now=0, registration_grace_seconds=120
    )
    watch_pr.classify_missing_producers(
        running, HEAD, memory, now=60, registration_grace_seconds=120
    )
    decision = watch_pr.classify_missing_producers(
        missing, HEAD, memory, now=150, registration_grace_seconds=120
    )

    assert decision.outcome == "pending"
    assert memory.unregistered_since == {"ci-required": 150}


def test_ci_rejects_required_context_without_a_producer_mapping() -> None:
    state = snapshot(required_check_names={"new-required-context"})

    with pytest.raises(watch_pr.WatchError, match="no producer mapping"):
        run_watch(FakeTransport(ci=[state]), FakeClock(), mode="ci")


def test_ci_waits_for_required_check_that_has_not_appeared() -> None:
    state = snapshot(
        checks=(check(name="confidential-content", bucket="pass"),),
        required_check_names={"confidential-content", "ci-required"},
    )

    decision = watch_pr.classify_ci(state, HEAD)

    assert decision.outcome == "pending"
    assert decision.terminal is False


def test_ci_visible_failure_wins_over_absent_sibling() -> None:
    state = snapshot(
        checks=(check(bucket="fail", link="https://ci.example/run/failed"),),
        required_check_names={"ci-required", "confidential-content"},
    )

    decision = watch_pr.classify_ci(state, HEAD)

    assert decision.outcome == "check_failed"
    assert decision.links == ("https://ci.example/run/failed",)


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


def test_ci_keeps_same_named_checks_from_distinct_workflows() -> None:
    checks = (
        check(name="test", bucket="fail", workflow="Linux", link="https://ci.example/linux"),
        check(name="test", bucket="pass", workflow="Windows"),
    )

    decision = watch_pr.classify_ci(snapshot(checks=checks), HEAD)

    assert decision.outcome == "check_failed"
    assert decision.links == ("https://ci.example/linux",)


def test_ci_rejects_duplicate_checks_without_a_workflow_identity() -> None:
    checks = (
        check(name="status", bucket="fail", workflow=None, started_at="2026-09-15T10:00:00Z"),
        check(name="status", bucket="pass", workflow=None, started_at="2026-09-15T10:01:00Z"),
    )

    with pytest.raises(watch_pr.WatchError, match="ambiguous required check identity"):
        watch_pr.classify_ci(snapshot(checks=checks), HEAD)


def test_ci_ignores_failure_from_an_old_head() -> None:
    decision = watch_pr.classify_ci(
        snapshot(checks=(check(bucket="fail", head_sha=NEXT_HEAD),)), HEAD
    )

    assert decision.outcome == "pending"


def test_ci_returns_head_change_and_closure() -> None:
    assert watch_pr.classify_ci(snapshot(head=NEXT_HEAD), HEAD).outcome == "head_changed"
    assert watch_pr.classify_ci(snapshot(state="closed"), HEAD).outcome == "closed"


def test_ci_head_change_and_closure_win_with_missing_producers() -> None:
    missing = snapshot(required_check_names={"ci-required"})

    assert (
        watch_pr.classify_ci(
            snapshot(head=NEXT_HEAD, required_check_names={"ci-required"}), HEAD
        ).outcome
        == "head_changed"
    )
    assert (
        watch_pr.classify_ci(
            snapshot(state="closed", required_check_names={"ci-required"}), HEAD
        ).outcome
        == "closed"
    )
    assert watch_pr.classify_ci(missing, HEAD).outcome == "pending"


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


def test_queue_keeps_fixed_cadence_after_head_update() -> None:
    transport = FakeTransport(
        queue=[
            snapshot(head=HEAD, labels={"merge-queue-checking"}),
            snapshot(head=NEXT_HEAD, labels={"merge-queue-checking"}),
            snapshot(head=NEXT_HEAD, merged_at="2026-09-15T10:00:00Z"),
        ]
    )
    clock = FakeClock()

    result = run_watch(transport, clock, mode="queue", timeout=1300)

    assert result.outcome == "merged"
    assert clock.sleeps == [600, 600]


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

    assert watch_pr.classify_queue(state, memory, remaining_seconds=600).outcome == expected


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


def test_producer_runs_are_parsed_from_structured_fields() -> None:
    parsed = watch_pr._parse_producer_runs(
        [
            {
                "workflowName": "Tests",
                "event": "pull_request",
                "status": "in_progress",
                "conclusion": "",
                "headSha": HEAD,
                "attempt": 2,
                "createdAt": "2026-09-28T10:00:00Z",
                "databaseId": 123,
                "url": "https://ci.example/run/123",
            }
        ]
    )

    assert parsed == (producer_run(attempt=2, database_id=123, url="https://ci.example/run/123"),)


def test_producer_runs_reject_unknown_status() -> None:
    value = {
        "workflowName": "Tests",
        "event": "pull_request",
        "status": "unknown",
        "conclusion": "",
        "headSha": HEAD,
        "attempt": 1,
        "createdAt": "2026-09-28T10:00:00Z",
        "databaseId": 123,
        "url": "https://ci.example/run/123",
    }

    with pytest.raises(watch_pr.WatchError, match="status value"):
        watch_pr._parse_producer_runs([value])


def test_ci_snapshot_rereads_checks_after_observing_completed_producer() -> None:
    pull = {
        "url": URL,
        "state": "OPEN",
        "mergedAt": None,
        "headRefOid": HEAD,
        "baseRefName": "main",
        "labels": [],
        "statusCheckRollup": [],
    }
    transport = watch_pr.GhTransport(clock=FakeClock())
    checks = iter([[], [{"name": "ci-required", "bucket": "pass", "state": "SUCCESS"}]])
    transport._pull = lambda *_args: pull  # type: ignore[method-assign]
    transport._required_checks = lambda *_args: frozenset({"ci-required"})  # type: ignore[method-assign]
    transport._checks = lambda *_args: next(checks)  # type: ignore[method-assign]
    transport._producer_runs = lambda *_args: (  # type: ignore[method-assign]
        producer_run(status="completed", conclusion="success"),
    )

    result = transport.ci_snapshot("example/repo", 7, 100)

    assert tuple(item.name for item in result.checks) == ("ci-required",)


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


@pytest.mark.parametrize("exit_code", [1, 8])
@pytest.mark.skipif(os.name == "nt", reason="fake gh uses POSIX command discovery")
def test_checks_accept_empty_stdout_for_no_check_exits(tmp_path: Path, exit_code: int) -> None:
    executable = write_fake_gh(tmp_path, f"raise SystemExit({exit_code})\n")
    transport = watch_pr.GhTransport(clock=FakeClock(), executable=str(executable))

    assert transport._checks("example/repo", 7, 10) == []


@pytest.mark.skipif(os.name == "nt", reason="fake gh uses POSIX command discovery")
def test_checks_reject_empty_stdout_for_success(tmp_path: Path) -> None:
    executable = write_fake_gh(tmp_path, "raise SystemExit(0)\n")
    transport = watch_pr.GhTransport(clock=FakeClock(), executable=str(executable))

    with pytest.raises(watch_pr.WatchError, match="empty JSON"):
        transport._checks("example/repo", 7, 10)


@pytest.mark.skipif(os.name == "nt", reason="fake gh uses POSIX command discovery")
def test_checks_reject_generic_exit_one_error(tmp_path: Path) -> None:
    executable = write_fake_gh(
        tmp_path,
        "import sys\nprint('GraphQL: missing pull request', file=sys.stderr)\nraise SystemExit(1)\n",
    )
    transport = watch_pr.GhTransport(clock=FakeClock(), executable=str(executable))

    with pytest.raises(watch_pr.WatchError, match="gh read failed"):
        transport._checks("example/repo", 7, 10)


@pytest.mark.skipif(os.name == "nt", reason="fake gh uses POSIX command discovery")
def test_checks_accept_documented_no_checks_message(tmp_path: Path) -> None:
    executable = write_fake_gh(
        tmp_path,
        "import sys\n"
        "print(\"no checks reported on the 'feature' branch\", file=sys.stderr)\n"
        "raise SystemExit(1)\n",
    )
    transport = watch_pr.GhTransport(clock=FakeClock(), executable=str(executable))

    assert transport._checks("example/repo", 7, 10) == []


@pytest.mark.skipif(os.name == "nt", reason="fake gh uses POSIX command discovery")
def test_checks_reject_malformed_nonempty_stdout(tmp_path: Path) -> None:
    executable = write_fake_gh(tmp_path, "print('{')\nraise SystemExit(1)\n")
    transport = watch_pr.GhTransport(clock=FakeClock(), executable=str(executable))

    with pytest.raises(watch_pr.WatchError, match="invalid JSON"):
        transport._checks("example/repo", 7, 10)


def test_every_outcome_has_an_explicit_exit_mapping() -> None:
    mapped = set(watch_pr.EXIT_CODES) | {watch_pr.Outcome.CANCELLED}

    assert mapped == set(watch_pr.Outcome)


@pytest.mark.skipif(os.name == "nt", reason="fake gh uses POSIX command discovery")
def test_cli_smoke_uses_read_only_gh_commands_and_emits_two_lines(tmp_path: Path) -> None:
    log = tmp_path / "commands.log"
    long_url = "https://github.com/example/repo/pull/" + "x" * 10_000
    write_passing_ci_gh(tmp_path, log, long_url)
    result = run_script(
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
        "--producer-registration-grace-seconds",
        "5",
        env={**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}"},
    )

    lines = result.stdout.splitlines()
    assert result.returncode == 0
    assert len(lines) == 2
    summary = json.loads(lines[1])
    assert summary["outcome"] == "ci_passed"
    assert summary["query_count"] == 4
    assert len(summary["pr_url"]) == watch_pr.MAX_LINK_LENGTH
    assert all(len(line) <= watch_pr.MAX_OUTPUT_LINE_LENGTH for line in lines)
    commands = log.read_text(encoding="utf-8").splitlines()
    assert "--required" in "\n".join(commands)
    assert all(
        line.split()[:2] in (["pr", "view"], ["pr", "checks"], ["run", "list"])
        or line.split()[0] == "api"
        for line in commands
    )


@pytest.mark.skipif(os.name == "nt", reason="fake gh uses POSIX command discovery")
def test_cli_reports_bounded_missing_check_evidence(tmp_path: Path) -> None:
    run = {
        "workflowName": "Tests",
        "event": "pull_request",
        "status": "completed",
        "conclusion": "success",
        "headSha": HEAD,
        "attempt": 1,
        "createdAt": "2026-09-28T10:00:00Z",
        "databaseId": 1,
        "url": "https://ci.example/run/1",
    }
    confidential = {
        **run,
        "workflowName": "Confidential content",
        "event": "pull_request_target",
        "databaseId": 2,
        "url": "https://ci.example/run/2",
    }
    write_fake_gh(
        tmp_path,
        _missing_checks_fake_gh_source(run, confidential),
    )

    result = run_script(
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
        "--producer-registration-grace-seconds",
        "5",
        env={**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}"},
    )

    lines = result.stdout.splitlines()
    assert len(lines) == 2, result.stderr
    summary = json.loads(lines[1])
    assert result.returncode == 1
    assert summary["outcome"] == "required_checks_missing"
    assert summary["missing_checks"] == ["ci-required", "confidential-content"]
    assert summary["links"] == ["https://ci.example/run/1", "https://ci.example/run/2"]


def test_cli_rejects_overlong_repo_without_echoing_it() -> None:
    repo = f"owner/{'x' * watch_pr.MAX_REPO_LENGTH}"

    result = subprocess.run(
        [
            "python3",
            str(SCRIPT),
            "--repo",
            repo,
            "--pr",
            "7",
            "--mode",
            "queue",
            "--timeout-seconds",
            "5",
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 2
    assert repo not in result.stderr
    assert all(len(line) <= watch_pr.MAX_OUTPUT_LINE_LENGTH for line in result.stderr.splitlines())


def test_cli_rejects_registration_grace_longer_than_ci_timeout() -> None:
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
            "60",
            "--producer-registration-grace-seconds",
            "61",
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 2
    assert "no greater than the CI timeout" in result.stderr


@pytest.mark.parametrize("grace", ["0", str(60 * 60 + 1)])
def test_cli_rejects_invalid_registration_grace_in_ci_mode(grace: str) -> None:
    result = run_script(
        "--repo",
        "example/repo",
        "--pr",
        "7",
        "--mode",
        "ci",
        "--expected-head",
        HEAD,
        "--timeout-seconds",
        str(2 * 60 * 60),
        "--producer-registration-grace-seconds",
        grace,
    )

    assert result.returncode == 2


def test_cli_ignores_registration_grace_validation_in_queue_mode() -> None:
    args = watch_pr._parser().parse_args(
        [
            "--repo",
            "example/repo",
            "--pr",
            "7",
            "--mode",
            "queue",
            "--timeout-seconds",
            "1",
            "--producer-registration-grace-seconds",
            "0",
        ]
    )

    watch_pr._validate_args(args)


def test_cancel_terminates_active_child_process() -> None:
    transport = watch_pr.GhTransport(clock=watch_pr.SystemClock(), executable=sys.executable)
    errors: list[BaseException] = []

    def run_child() -> None:
        try:
            transport._run(["-c", "import time; time.sleep(30)"], 20, accepted={0})
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


@pytest.mark.skipif(os.name == "nt", reason="POSIX signal behavior")
def test_cancel_interrupts_quiet_polling_sleep(tmp_path: Path) -> None:
    write_fake_gh(
        tmp_path,
        "import json, sys\n"
        "if sys.argv[1:3] == ['pr', 'view']:\n"
        f"    print(json.dumps({{'url': {URL!r}, 'state': 'OPEN', 'mergedAt': None, 'headRefOid': {HEAD!r}, 'baseRefName': 'main', 'labels': [], 'statusCheckRollup': []}}))\n"
        "elif sys.argv[1:3] == ['pr', 'checks']:\n"
        "    print('[]')\n"
        "elif sys.argv[1:3] == ['run', 'list']:\n"
        "    print('[]')\n"
        "elif sys.argv[1] == 'api':\n"
        "    print(json.dumps([{'type': 'required_status_checks', 'parameters': {'required_status_checks': [{'context': 'ci-required'}]}}]))\n",
    )
    process = subprocess.Popen(
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
            "120",
            "--producer-registration-grace-seconds",
            "60",
        ],
        env={**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}"},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert process.stdout is not None
    assert process.stdout.readline().startswith("watch_pr: started")
    time.sleep(0.2)

    process.send_signal(signal.SIGTERM)
    stdout, stderr = process.communicate(timeout=2)

    assert stderr == ""
    assert process.returncode == 143
    summary = json.loads(stdout)
    assert summary["outcome"] == "cancelled"
    assert summary["elapsed_seconds"] >= 0
