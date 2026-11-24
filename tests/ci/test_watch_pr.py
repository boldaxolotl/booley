from __future__ import annotations

import importlib.util
import json
import os
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from tests.path_tool_fake import PathToolFake
from tests.subprocess_budget import run_bounded_child
from tests.time_budget import hang_guard_s

SCRIPT = Path(__file__).parents[2] / ".github/scripts/watch_pr.py"
SPEC = importlib.util.spec_from_file_location("watch_pr", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
watch_pr = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = watch_pr
SPEC.loader.exec_module(watch_pr)


_CHECKOUT_SRC = Path(__file__).resolve().parents[2] / "src"


@pytest.fixture(autouse=True)
def watcher_imports_this_checkout(monkeypatch: pytest.MonkeyPatch) -> None:
    """The watcher child imports ``booley``; the test interpreter need not have it installed.

    ``tests/ci/conftest.py`` drops the suite-wide ``PYTHONPATH`` fixture for the
    ``pip check`` tests, so this module restores it for its ``python3`` children.
    """
    monkeypatch.setenv(
        "PYTHONPATH",
        os.pathsep.join(filter(None, (str(_CHECKOUT_SRC), os.environ.get("PYTHONPATH")))),
    )


HEAD = "a" * 40
NEXT_HEAD = "b" * 40
URL = "https://github.com/example/repo/pull/7"
RULES_PATH = "repos/example/repo/rules/branches/main"
COMMENTS_PATH = "repos/example/repo/issues/7/comments?per_page=100"
EVENTS_PATH = "repos/example/repo/issues/7/events?per_page=100"
LONG_URL = "https://github.com/example/repo/pull/" + "x" * 10_000


def open_pull(**fields: object) -> str:
    return json.dumps(
        {
            "url": URL,
            "state": "OPEN",
            "mergedAt": None,
            "headRefOid": HEAD,
            "baseRefName": "main",
            "labels": [],
            "statusCheckRollup": [],
            **fields,
        }
    )


def passing_check() -> dict[str, object]:
    return {
        "name": "ci-required",
        "state": "SUCCESS",
        "bucket": "pass",
        "link": "https://ci.example/run/1",
        "startedAt": None,
        "completedAt": None,
        "workflow": "CI",
    }


def run_json(**fields: object) -> dict[str, object]:
    return {
        "workflowName": "Tests",
        "event": "pull_request",
        "status": "completed",
        "conclusion": "success",
        "headSha": HEAD,
        "attempt": 2,
        "createdAt": "2026-09-28T10:00:00Z",
        "databaseId": 123,
        "url": "https://ci.example/run/123",
        **fields,
    }


@pytest.fixture
def gh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> PathToolFake:
    return PathToolFake.install(tmp_path / "bin", "gh", monkeypatch)


def answer_reads(
    gh: PathToolFake,
    *,
    pull: str | list[str] | None = None,
    checks: str | list[str] | None = "[]",
    runs: str | list[str] = "[]",
    comments: str | list[str] = "[]",
    required: tuple[str, ...] = ("ci-required",),
) -> None:
    """Answer each read the watcher makes; a list answers that read once per call, in order."""
    rules = [
        {
            "type": "required_status_checks",
            "parameters": {"required_status_checks": [{"context": name} for name in required]},
        }
    ]
    reads = [
        (("pr", "view"), open_pull() if pull is None else pull),
        (("api", RULES_PATH), json.dumps(rules)),
        (("run", "list"), runs),
        (("api", COMMENTS_PATH), comments),
    ]
    if checks is not None:
        reads.append((("pr", "checks"), checks))
    for words, value in reads:
        for stdout in [value] if isinstance(value, str) else value:
            gh.answer(*words, stdout=stdout)


def gh_transport(gh: PathToolFake, clock: FakeClock | None = None) -> watch_pr.GhTransport:
    return watch_pr.GhTransport(clock=clock or FakeClock(), executable=str(gh.executable))


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
    dequeued_at: str | None = None,
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
        datetime.fromisoformat(dequeued_at) if dequeued_at else None,
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


@pytest.mark.parametrize(
    "state",
    [
        snapshot(),
        snapshot(checks=(check(bucket="pass"),), required_check_names=set()),
    ],
    ids=["no-checks", "visible-checks"],
)
def test_ci_empty_required_rules_fail_closed(state: watch_pr.Snapshot) -> None:
    with pytest.raises(watch_pr.WatchError, match="no required checks"):
        run_watch(FakeTransport(ci=[state]), FakeClock(), mode="ci", timeout=100)


def test_ci_reports_required_checks_missing_when_producer_never_registers() -> None:
    state = snapshot(required_check_names={"ci-required", "confidential-content"})
    transport = FakeTransport(ci=[state, state, state])

    result = run_watch(transport, FakeClock(), mode="ci", timeout=180)

    assert result.outcome == "required_checks_missing"
    assert result.missing_checks == ("ci-required", "confidential-content")


@pytest.mark.parametrize(
    ("status", "conclusion"),
    [("action_required", None), ("completed", "action_required"), ("in_progress", None)],
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


@pytest.mark.parametrize(
    "runs",
    [
        (producer_run(head_sha=NEXT_HEAD), producer_run(event="push", database_id=101)),
        (producer_run(workflow="Another workflow"),),
    ],
    ids=["another-head-or-event", "another-workflow"],
)
def test_ci_does_not_accept_producer_from_another_head_event_or_workflow(
    runs: tuple[watch_pr.ProducerRun, ...],
) -> None:
    state = snapshot(required_check_names={"ci-required"}, producer_runs=runs)

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


@pytest.mark.parametrize(
    ("checks", "required"),
    [
        (
            (check(name="confidential-content", bucket="pass"),),
            {"confidential-content", "ci-required"},
        ),
        ((check(bucket="skipping"),), None),
        ((check(bucket="pending"),), None),
        (
            (
                check(bucket="fail", head_sha=HEAD, attempt=1),
                check(bucket="pending", head_sha=HEAD, attempt=2),
            ),
            None,
        ),
        ((check(bucket="fail", head_sha=NEXT_HEAD),), None),
        ((), {"ci-required"}),
    ],
    ids=["absent-sibling", "skipping", "pending", "old-attempt", "old-head", "none-yet"],
)
def test_ci_unresolved_checks_stay_pending(
    checks: tuple[watch_pr.Check, ...], required: set[str] | None
) -> None:
    decision = watch_pr.classify_ci(snapshot(checks=checks, required_check_names=required), HEAD)

    assert decision.outcome == "pending"
    assert decision.terminal is False


@pytest.mark.parametrize("bucket", ["fail", "cancel"])
def test_ci_failed_check_is_actionable_beside_an_absent_sibling(bucket: str) -> None:
    state = snapshot(
        checks=(check(bucket=bucket, link="https://ci.example/run/failed"),),
        required_check_names={"ci-required", "confidential-content"},
    )

    decision = watch_pr.classify_ci(state, HEAD)

    assert decision.outcome == "check_failed"
    assert decision.links == ("https://ci.example/run/failed",)


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


def test_ci_expands_an_abbreviated_expected_head() -> None:
    transport = FakeTransport(ci=[snapshot(checks=(check(bucket="pass", head_sha=HEAD),))])

    result = run_watch(transport, FakeClock(), mode="ci", expected_head=HEAD[:9].upper())

    assert result.outcome == "ci_passed"


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        (snapshot(head=NEXT_HEAD, required_check_names={"ci-required"}), "head_changed"),
        (snapshot(state="closed", required_check_names={"ci-required"}), "closed"),
    ],
)
def test_ci_returns_head_change_and_closure(state: watch_pr.Snapshot, expected: str) -> None:
    assert watch_pr.classify_ci(state, HEAD).outcome == expected


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


def queue_command(created_at: str, body: str = "@mergifyio queue default") -> watch_pr.Comment:
    return watch_pr.Comment("9", body, "owner", datetime.fromisoformat(created_at))


def newer_control(command: str) -> watch_pr.Snapshot:
    return snapshot(
        labels={"dequeued"},
        comments=(queue_command("2026-11-03T10:00:05Z", command),),
        dequeued_at="2026-11-03T10:20:00Z",
    )


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        (snapshot(labels={"dequeued"}), "dequeued"),
        (
            snapshot(comments=(watch_pr.Comment("2", "@mergifyio dequeue", "other-owner"),)),
            "competing_control",
        ),
        (snapshot(mergify_state="failure"), "queue_failed"),
        (snapshot(state="closed"), "closed"),
        (snapshot(labels={"queued", "dequeued"}), "pending"),
        (snapshot(labels={"merge-queue-checking", "dequeued"}), "pending"),
        (newer_control("@mergifyio queue"), "dequeued"),
        (newer_control("@mergifyio queue default"), "dequeued"),
        (newer_control("@mergifyio dequeue"), "dequeued"),
    ],
    ids=[
        "dequeued",
        "competing-control",
        "queue-failed",
        "closed",
        "dequeued-beside-queued",
        "dequeued-beside-checking",
        "requeued",
        "requeued-named-queue",
        "dequeue",
    ],
)
def test_queue_states(state: watch_pr.Snapshot, expected: str) -> None:
    memory = watch_pr.QueueMemory(baseline_comment_ids={"9"}, initialized=True)

    assert watch_pr.classify_queue(state, memory, remaining_seconds=600, now=0).outcome == expected


def test_requeue_ignores_stale_dequeued_label_and_waits_for_merge() -> None:
    """Mergify adds ``queued`` before clearing the prior attempt's ``dequeued`` label."""
    transport = FakeTransport(
        queue=[
            snapshot(labels={"queued", "dequeued"}),
            snapshot(labels={"queued", "merge-queue-checking"}),
            snapshot(state="merged", merged_at="2026-09-29T09:31:45Z"),
        ]
    )

    result = run_watch(transport, FakeClock(), mode="queue", timeout=2000)

    assert result.outcome == "merged"


def test_requeue_ignores_dequeued_label_that_predates_the_queue_command() -> None:
    """Right after a requeue Mergify still shows only the prior attempt's label."""
    requeue = (queue_command("2026-11-03T10:00:05Z"),)
    transport = FakeTransport(
        queue=[
            snapshot(labels={"dequeued"}, comments=requeue, dequeued_at="2026-11-03T09:40:00Z"),
            snapshot(labels={"queued"}, comments=requeue),
            snapshot(state="merged", merged_at="2026-11-03T11:00:00Z", comments=requeue),
        ]
    )

    result = run_watch(transport, FakeClock(), mode="queue", timeout=2000)

    assert result.outcome == "merged"


def test_stale_dequeued_label_is_reported_once_the_grace_expires() -> None:
    """Mergify never acted on the queue command, so the PR really is dequeued."""
    stale = snapshot(
        labels={"dequeued"},
        comments=(queue_command("2026-11-03T10:00:05Z"),),
        dequeued_at="2026-11-03T09:40:00Z",
    )
    clock = FakeClock()

    result = run_watch(FakeTransport(queue=[stale, stale]), clock, mode="queue", timeout=2000)

    assert result.outcome == "dequeued"
    assert clock.sleeps == [600]


def test_queue_snapshot_reads_label_events_only_for_a_lone_dequeued_label(
    gh: PathToolFake,
) -> None:
    def event(kind: str, label: str, created_at: str) -> dict[str, object]:
        return {"event": kind, "label": {"name": label}, "created_at": created_at}

    comment = {
        "id": 9,
        "body": "@mergifyio queue default",
        "user": {"login": "owner"},
        "created_at": "2026-11-03T10:00:05Z",
    }
    answer_reads(
        gh,
        pull=[
            open_pull(labels=[{"name": "queued"}]),
            open_pull(labels=[{"name": "dequeued"}]),
        ],
        comments=json.dumps([[comment], []]),
    )
    events = [
        event("labeled", "dequeued", "2026-11-03T09:00:00Z"),
        event("unlabeled", "dequeued", "2026-11-03T09:30:00Z"),
        event("labeled", "dequeued", "2026-11-03T09:40:00Z"),
        event("labeled", "queued", "2026-11-03T09:50:00Z"),
        {"event": "closed", "created_at": "2026-11-03T09:55:00Z"},
    ]
    gh.answer("api", EVENTS_PATH, stdout=json.dumps([events]))
    transport = gh_transport(gh)

    queued = transport.queue_snapshot("example/repo", 7, hang_guard_s())
    dequeued = transport.queue_snapshot("example/repo", 7, hang_guard_s())

    assert queued.dequeued_at is None
    assert dequeued.dequeued_at == datetime.fromisoformat("2026-11-03T09:40:00Z")
    assert dequeued.comments == (queue_command("2026-11-03T10:00:05Z"),)
    assert sum(EVENTS_PATH in call.args for call in gh.calls()) == 1


def test_queue_transient_label_inconsistency_stays_pending() -> None:
    first = snapshot(labels=set(), mergify_state="pending")
    second = snapshot(labels={"queued"}, mergify_state="pending")
    transport = FakeTransport(queue=[first, second])

    result = run_watch(transport, FakeClock(), mode="queue", timeout=100)

    assert result.outcome == "timeout"


@pytest.mark.parametrize(
    ("read", "reads", "match"),
    [
        (
            "queue_snapshot",
            {"pull": open_pull(statusCheckRollup={"bad": True})},
            "statusCheckRollup",
        ),
        ("ci_snapshot", {"runs": json.dumps([run_json(status="unknown")])}, "status value"),
    ],
)
def test_malformed_external_response_is_rejected(
    gh: PathToolFake, read: str, reads: dict[str, Any], match: str
) -> None:
    answer_reads(gh, **reads)

    with pytest.raises(watch_pr.WatchError, match=match):
        getattr(gh_transport(gh), read)("example/repo", 7, hang_guard_s())


@pytest.mark.parametrize(
    ("status", "conclusion", "parsed"),
    [("completed", "success", "success"), ("in_progress", "", None)],
)
def test_ci_snapshot_rereads_checks_after_observing_completed_producer(
    gh: PathToolFake, status: str, conclusion: str, parsed: str | None
) -> None:
    answer_reads(
        gh,
        checks=["[]", json.dumps([passing_check()])],
        runs=json.dumps([run_json(status=status, conclusion=conclusion)]),
    )

    result = gh_transport(gh).ci_snapshot("example/repo", 7, hang_guard_s())

    assert tuple(item.name for item in result.checks) == ("ci-required",)
    assert result.producer_runs == (
        producer_run(
            status=status,
            conclusion=parsed,
            attempt=2,
            database_id=123,
            url="https://ci.example/run/123",
        ),
    )


def test_transient_reads_are_retried_at_most_three_times(gh: PathToolFake) -> None:
    clock = FakeClock()
    for _ in range(2):
        gh.answer("pr", "view", stderr="connection reset", returncode=1)
    answer_reads(gh)

    snapshot_ = gh_transport(gh, clock).queue_snapshot("example/repo", 7, hang_guard_s())

    assert snapshot_.url == URL
    assert clock.sleeps == [1, 2]
    assert sum(call.args[:2] == ["pr", "view"] for call in gh.calls()) == 3


@pytest.mark.parametrize(
    ("returncode", "stdout", "stderr", "error"),
    [
        (1, "", "", None),
        (8, "", "", None),
        (1, "", "no checks reported on the 'feature' branch", None),
        (8, "", "no required checks reported on the 'feature' branch", None),
        (0, "", "", "empty JSON"),
        (1, "", "GraphQL: missing pull request", "gh read failed"),
        (1, "{\n", "", "invalid JSON"),
    ],
    ids=[
        "exit-1",
        "exit-8",
        "plain-message",
        "required-message",
        "success-empty",
        "graphql",
        "malformed",
    ],
)
def test_required_checks_read_outcomes(
    gh: PathToolFake, returncode: int, stdout: str, stderr: str, error: str | None
) -> None:
    answer_reads(gh, checks=None)
    gh.answer("pr", "checks", stdout=stdout, stderr=stderr, returncode=returncode)
    transport = gh_transport(gh)

    if error is not None:
        with pytest.raises(watch_pr.WatchError, match=error):
            transport.ci_snapshot("example/repo", 7, hang_guard_s())
        return
    assert transport.ci_snapshot("example/repo", 7, hang_guard_s()).checks == ()


def test_every_outcome_has_an_explicit_exit_mapping() -> None:
    mapped = set(watch_pr.EXIT_CODES) | {watch_pr.Outcome.CANCELLED}

    assert mapped == set(watch_pr.Outcome)


def cli(*extra: str) -> list[str]:
    return ["python3", str(SCRIPT), "--repo", "example/repo", "--pr", "7", *extra]


CI_MODE = ["--mode", "ci", "--expected-head", HEAD, "--producer-registration-grace-seconds"]
CONFIDENTIAL_RUN = run_json(
    workflowName="Confidential content",
    event="pull_request_target",
    databaseId=2,
    url="https://ci.example/run/2",
)


@pytest.mark.skipif(os.name == "nt", reason="Popen(['gh', ...]) does not resolve a .cmd launcher")
@pytest.mark.parametrize(
    ("mode", "reads", "code", "expected"),
    [
        (
            [*CI_MODE, str(hang_guard_s())],
            {"pull": open_pull(url=LONG_URL), "checks": json.dumps([passing_check()])},
            0,
            {
                "outcome": "ci_passed",
                "query_count": 4,
                "pr_url": LONG_URL[: watch_pr.MAX_LINK_LENGTH],
            },
        ),
        (
            [*CI_MODE, str(hang_guard_s())],
            {
                "runs": json.dumps(
                    [
                        run_json(attempt=1, databaseId=1, url="https://ci.example/run/1"),
                        CONFIDENTIAL_RUN,
                    ]
                ),
                "required": ("ci-required", "confidential-content"),
            },
            1,
            {
                "outcome": "required_checks_missing",
                "missing_checks": ["ci-required", "confidential-content"],
                "links": ["https://ci.example/run/1", "https://ci.example/run/2"],
            },
        ),
        (
            ["--mode", "queue", "--producer-registration-grace-seconds", "0"],
            {"pull": open_pull(state="MERGED", mergedAt="2026-09-15T10:00:00Z")},
            0,
            {"outcome": "merged"},
        ),
    ],
    ids=["passing-ci", "missing-producers", "queue-ignores-grace"],
)
def test_cli_scenarios(
    gh: PathToolFake,
    mode: list[str],
    reads: dict[str, Any],
    code: int,
    expected: dict[str, object],
) -> None:
    answer_reads(gh, **reads)

    result = run_bounded_child(
        cli(*mode, "--timeout-seconds", str(hang_guard_s())), timeout=2 * hang_guard_s()
    )

    lines = result.stdout.splitlines()
    assert len(lines) == 2, result.stderr
    summary = json.loads(lines[1])
    assert result.returncode == code
    assert {key: summary[key] for key in expected} == expected
    assert all(len(line) <= watch_pr.MAX_OUTPUT_LINE_LENGTH for line in lines)
    assert all(
        call.args[:2] in (["pr", "view"], ["pr", "checks"], ["run", "list"])
        or (call.args[0] == "api" and "--method" not in call.args)
        for call in gh.calls()
    )


def ci_argv(timeout: int, grace: int) -> list[str]:
    return [
        *cli()[2:],
        *CI_MODE,
        str(grace),
        "--timeout-seconds",
        str(timeout),
    ]


@pytest.mark.parametrize(
    ("argv", "fragment"),
    [
        (
            [
                "--repo",
                f"owner/{'x' * watch_pr.MAX_REPO_LENGTH}",
                "--pr",
                "7",
                "--mode",
                "queue",
                "--timeout-seconds",
                "5",
            ],
            "--repo must be OWNER/REPO",
        ),
        (ci_argv(60, 61), "no greater than the CI timeout"),
        (ci_argv(2 * 60 * 60, 0), "must be positive"),
        (ci_argv(2 * 60 * 60, 60 * 60 + 1), "at most one hour"),
    ],
    ids=["overlong-repo", "grace-over-timeout", "grace-zero", "grace-over-an-hour"],
)
def test_cli_rejects_invalid_arguments(
    argv: list[str], fragment: str, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as raised:
        watch_pr.main(argv)

    stderr = capsys.readouterr().err
    assert raised.value.code == 2
    assert fragment in stderr
    assert "x" * watch_pr.MAX_REPO_LENGTH not in stderr
    assert all(len(line) <= watch_pr.MAX_OUTPUT_LINE_LENGTH for line in stderr.splitlines())


@pytest.mark.skipif(
    os.name == "nt",
    reason="the fake is a .cmd wrapper there: terminate() ends only it and the child keeps the pipes",
)
def test_cancel_terminates_active_child_process(gh: PathToolFake) -> None:
    gh.answer("pr", "view", hang_s=4 * hang_guard_s())
    clock = watch_pr.SystemClock()
    transport = gh_transport(gh, clock)
    errors: list[BaseException] = []

    def read() -> None:
        try:
            transport.ci_snapshot("example/repo", 7, clock.monotonic() + 2 * hang_guard_s())
        except watch_pr.CancelledWatchError as error:
            errors.append(error)

    worker = threading.Thread(target=read)
    worker.start()
    poll_deadline = time.monotonic() + hang_guard_s()
    while not gh.calls() and time.monotonic() < poll_deadline:
        time.sleep(0.01)
    transport.cancel()
    worker.join(timeout=hang_guard_s())

    assert not worker.is_alive()
    assert errors and isinstance(errors[0], watch_pr.CancelledWatchError)


@pytest.mark.skipif(os.name == "nt", reason="POSIX signal behavior")
def test_cancel_interrupts_quiet_polling_sleep(gh: PathToolFake) -> None:
    answer_reads(gh)
    # The test drives this Popen itself (SIGTERM, communicate).
    process = subprocess.Popen(
        cli(
            *CI_MODE,
            str(2 * hang_guard_s()),
            "--timeout-seconds",
            str(4 * hang_guard_s()),
        ),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert process.stdout is not None
    assert process.stdout.readline().startswith("watch_pr: started")
    # The six reads of the first CI snapshot, each logged after its whole answer.
    poll_deadline = time.monotonic() + hang_guard_s()
    while len(gh.calls()) < 6 and time.monotonic() < poll_deadline:
        time.sleep(0.01)

    process.send_signal(signal.SIGTERM)
    stdout, stderr = process.communicate(timeout=hang_guard_s())

    assert stderr == ""
    assert process.returncode == 143
    summary = json.loads(stdout)
    assert summary["outcome"] == "cancelled"
    assert summary["elapsed_seconds"] >= 0
