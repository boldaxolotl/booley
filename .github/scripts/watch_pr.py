#!/usr/bin/env python3
"""Read-only, deadline-bounded CI and Mergify queue watcher."""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol

from booley.core.boundary import (
    BoundaryError,
    as_str,
    require_dict,
    require_int,
    require_list,
    require_opt_str,
    require_str,
)

MAX_LINKS = 8
MAX_LINK_LENGTH = 256
MAX_REPO_LENGTH = 256
MAX_HEAD_LENGTH = 64
MAX_OUTPUT_LINE_LENGTH = 16_384
MAX_PR_NUMBER = 2_147_483_647
MAX_TIMEOUT_SECONDS = 31 * 24 * 60 * 60
QUEUE_POLL_SECONDS = 10 * 60
CI_POLL_SECONDS = 60
RETRY_DELAYS = (1, 2)
CONTROL_COMMAND = re.compile(r"(?im)^\s*@mergifyio\s+(queue\b|dequeue\b)")
TRANSIENT_ERROR = re.compile(
    r"(?:timed out|timeout|temporar|connection|network|502|503|504)", re.I
)
IMMEDIATE_ERROR = re.compile(
    r"(?:auth|permission|forbidden|unauthor|invalid|unknown field|schema)", re.I
)


class WatchError(RuntimeError):
    """An observation could not be obtained or validated."""


class CancelledWatchError(WatchError):
    """The caller requested cancellation."""


class Clock(Protocol):
    def monotonic(self) -> float: ...

    def sleep(self, seconds: float) -> None: ...


class Transport(Protocol):
    query_count: int

    def ci_snapshot(self, repo: str, pr: int, deadline: float) -> Snapshot: ...

    def queue_snapshot(self, repo: str, pr: int, deadline: float) -> Snapshot: ...

    def cancel(self) -> None: ...


@dataclass(frozen=True)
class Check:
    name: str
    bucket: str
    state: str
    link: str | None = None
    head_sha: str | None = None
    attempt: int | None = None
    started_at: str | None = None
    workflow: str | None = None


@dataclass(frozen=True)
class Comment:
    identifier: str
    body: str
    author: str | None = None


@dataclass(frozen=True)
class Snapshot:
    url: str
    state: str
    merged_at: str | None
    head_sha: str | None
    labels: frozenset[str] = frozenset()
    checks: tuple[Check, ...] = ()
    mergify_state: str | None = None
    comments: tuple[Comment, ...] = ()


class Outcome(StrEnum):
    PENDING = "pending"
    CI_PASSED = "ci_passed"
    MERGED = "merged"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    HEAD_CHANGED = "head_changed"
    CLOSED = "closed"
    CHECK_FAILED = "check_failed"
    QUEUE_FAILED = "queue_failed"
    DEQUEUED = "dequeued"
    COMPETING_CONTROL = "competing_control"
    OBSERVATION_ERROR = "observation_error"


@dataclass(frozen=True)
class Decision:
    outcome: Outcome
    terminal: bool
    links: tuple[str, ...] = ()
    next_wait_seconds: int | None = None


@dataclass
class QueueMemory:
    baseline_comment_ids: set[str] = field(default_factory=set)
    initialized: bool = False


@dataclass(frozen=True)
class WatchResult:
    outcome: Outcome
    url: str
    observed_head: str | None
    elapsed_seconds: int
    query_count: int
    links: tuple[str, ...]


class SystemClock:
    """Production clock kept behind the small clock protocol."""

    def monotonic(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


def _redact_error(text: str) -> str:
    """Keep subprocess diagnostics generic and bounded."""
    compact = " ".join(text.split())
    return compact[:240] or "gh read failed"


def _is_transient(text: str) -> bool:
    return bool(TRANSIENT_ERROR.search(text)) and not IMMEDIATE_ERROR.search(text)


def _json_object(value: Any, field_name: str) -> dict[str, Any]:
    try:
        return require_dict(value, field=field_name)
    except BoundaryError as error:
        raise WatchError(f"invalid {field_name} response") from error


def _json_list(value: Any, field_name: str) -> list[Any]:
    try:
        return require_list(value, field=field_name)
    except BoundaryError as error:
        raise WatchError(f"invalid {field_name} response") from error


def _text(value: Any, field_name: str, *, optional: bool = False) -> str | None:
    try:
        if optional:
            if as_str(value) == "":
                return None
            return require_opt_str({"value": value}, "value", field=field_name)
        return require_str({field_name: value}, field_name)
    except BoundaryError as error:
        raise WatchError(f"invalid {field_name} value") from error


def _optional_int(value: Any, field_name: str) -> int | None:
    if value is None:
        return None
    try:
        return require_int(value, field=field_name)
    except BoundaryError as error:
        raise WatchError(f"invalid {field_name} value") from error


def _parse_check(raw: Any, field_name: str) -> Check:
    entry = _json_object(raw, field_name)
    name = _text(entry.get("name"), f"{field_name}.name")
    bucket = _text(entry.get("bucket"), f"{field_name}.bucket")
    state = _text(entry.get("state"), f"{field_name}.state")
    link = _text(entry.get("link"), f"{field_name}.link", optional=True)
    head_sha = _text(
        entry.get("headSha", entry.get("head_sha")), f"{field_name}.headSha", optional=True
    )
    started_at = _text(entry.get("startedAt"), f"{field_name}.startedAt", optional=True)
    attempt = _optional_int(entry.get("attempt"), f"{field_name}.attempt")
    workflow = _text(entry.get("workflow"), f"{field_name}.workflow", optional=True)
    return Check(
        name=name,
        bucket=bucket.lower(),
        state=state.lower(),
        link=link,
        head_sha=head_sha,
        attempt=attempt,
        started_at=started_at,
        workflow=workflow,
    )


def _parse_comments(value: Any) -> tuple[Comment, ...]:
    """Flatten either a normal or gh --paginate --slurp response."""
    pages = _json_list(value, "comments")
    try:
        entries = [entry for page in pages for entry in require_list(page, field="comment page")]
    except BoundaryError:
        entries = pages
    comments: list[Comment] = []
    for index, raw in enumerate(entries):
        entry = _json_object(raw, f"comments[{index}]")
        identifier = entry.get("id")
        comment_id = as_str(identifier)
        if not comment_id:
            try:
                comment_id = str(require_int(identifier, field=f"comments[{index}].id"))
            except BoundaryError as error:
                raise WatchError(f"invalid comments[{index}].id value") from error
        body = _text(entry.get("body"), f"comments[{index}].body")
        author_value = entry.get("user", entry.get("author"))
        author = None
        if author_value is not None:
            author_entry = _json_object(author_value, f"comments[{index}].author")
            author = _text(author_entry.get("login"), f"comments[{index}].author.login")
        comments.append(Comment(comment_id, body, author))
    return tuple(comments)


def _parse_rollup(value: Any) -> str | None:
    """Read only explicitly structured Mergify fields from check data."""
    rollup = _json_list(value, "statusCheckRollup")
    mergify_state: str | None = None
    for index, raw in enumerate(rollup):
        entry = _json_object(raw, f"statusCheckRollup[{index}]")
        name = as_str(entry.get("name", entry.get("context")))
        if name is None or "mergify" not in name.lower():
            continue
        state = as_str(entry.get("state", entry.get("conclusion")))
        if state is not None:
            mergify_state = state.lower()
    return mergify_state


def _snapshot_from_json(
    pull: Any, checks: Any, comments: Any, *, require_comments: bool
) -> Snapshot:
    pr = _json_object(pull, "pull request")
    url = _text(pr.get("url"), "pull request.url")
    state = _text(pr.get("state"), "pull request.state")
    merged_at = _text(pr.get("mergedAt"), "pull request.mergedAt", optional=True)
    head_sha = _text(pr.get("headRefOid"), "pull request.headRefOid", optional=True)
    labels_raw = _json_list(pr.get("labels", []), "labels")
    labels: set[str] = set()
    for index, raw in enumerate(labels_raw):
        label = _json_object(raw, f"labels[{index}]")
        name = _text(label.get("name"), f"labels[{index}].name")
        labels.add(name.lower())
    mergify_state = _parse_rollup(pr.get("statusCheckRollup", []))
    check_entries = _json_list(checks, "required checks")
    parsed_checks = tuple(
        _parse_check(item, f"checks[{index}]") for index, item in enumerate(check_entries)
    )
    parsed_comments = _parse_comments(comments) if require_comments else ()
    return Snapshot(
        url=url,
        state=state.lower(),
        merged_at=merged_at,
        head_sha=head_sha,
        labels=frozenset(labels),
        checks=parsed_checks,
        mergify_state=mergify_state,
        comments=parsed_comments,
    )


class GhTransport:
    """Bounded read-only adapter for the GitHub CLI."""

    def __init__(self, *, clock: Clock, executable: str = "gh") -> None:
        self._clock = clock
        self._executable = executable
        self._active: subprocess.Popen[str] | None = None
        self.query_count = 0
        self.cancelled = False

    def cancel(self) -> None:
        self.cancelled = True
        process = self._active
        if process is not None and process.poll() is None:
            try:
                if os.name == "nt":
                    process.terminate()
                else:
                    os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass

    def _run(self, args: Sequence[str], remaining: float, *, accepted: set[int]) -> Any:
        if self.cancelled:
            raise CancelledWatchError("cancelled")
        if remaining <= 0:
            raise TimeoutError("deadline expired")
        self.query_count += 1
        process: subprocess.Popen[str] | None = None
        try:
            process = subprocess.Popen(
                [self._executable, *args],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=os.name != "nt",
            )
            self._active = process
            stdout, stderr = process.communicate(timeout=max(0.01, remaining))
        except subprocess.TimeoutExpired as error:
            self.cancel()
            raise TimeoutError("gh read exceeded deadline") from error
        except OSError as error:
            raise WatchError("could not start gh") from error
        finally:
            self._active = None
        assert process is not None
        if self.cancelled:
            raise CancelledWatchError("cancelled")
        if process.returncode not in accepted:
            message = _redact_error(stderr)
            if _is_transient(message):
                raise ConnectionError(message)
            raise WatchError("gh read failed")
        try:
            return json.loads(stdout)
        except json.JSONDecodeError as error:
            raise WatchError("gh returned invalid JSON") from error

    def _read(
        self, args: Sequence[str], deadline: float, *, accepted: set[int] | None = None
    ) -> Any:
        if accepted is None:
            accepted = {0}
        last_error: Exception | None = None
        for attempt in range(3):
            if self.cancelled:
                raise CancelledWatchError("cancelled")
            remaining = deadline - self._clock.monotonic()
            try:
                return self._run(args, remaining, accepted=accepted)
            except ConnectionError as error:
                last_error = error
                if attempt == 2:
                    break
                delay = RETRY_DELAYS[attempt]
                if deadline - self._clock.monotonic() <= delay:
                    break
                self._clock.sleep(delay)
        if last_error is not None:
            raise WatchError("transient gh read failed after three attempts") from last_error
        raise WatchError("gh read failed")

    def _pull(self, repo: str, pr: int, deadline: float) -> Any:
        fields = "state,mergedAt,headRefOid,url,labels,statusCheckRollup"
        return self._read(["pr", "view", str(pr), "--repo", repo, "--json", fields], deadline)

    def _checks(self, repo: str, pr: int, deadline: float) -> Any:
        fields = "name,state,bucket,link,startedAt,completedAt,workflow"
        return self._read(
            ["pr", "checks", str(pr), "--repo", repo, "--required", "--json", fields],
            deadline,
            accepted={0, 1, 8},
        )

    def _comments(self, repo: str, pr: int, deadline: float) -> Any:
        path = f"repos/{repo}/issues/{pr}/comments?per_page=100"
        return self._read(["api", "--paginate", "--slurp", path], deadline)

    def ci_snapshot(self, repo: str, pr: int, deadline: float) -> Snapshot:
        first = self._pull(repo, pr, deadline)
        first_obj = _json_object(first, "pull request")
        if str(first_obj.get("state", "")).lower() == "closed":
            return _snapshot_from_json(first, [], [], require_comments=False)
        first_head = _text(first_obj.get("headRefOid"), "pull request.headRefOid", optional=True)
        checks = self._checks(repo, pr, deadline)
        final = self._pull(repo, pr, deadline)
        final_obj = _json_object(final, "pull request")
        final_head = _text(final_obj.get("headRefOid"), "pull request.headRefOid", optional=True)
        if first_head != final_head:
            first_url = _text(first_obj.get("url"), "pull request.url")
            raise HeadChangedError(first_head, final_head, first_url)
        return _snapshot_from_json(final, checks, [], require_comments=False)

    def queue_snapshot(self, repo: str, pr: int, deadline: float) -> Snapshot:
        pull = self._pull(repo, pr, deadline)
        checks = self._checks(repo, pr, deadline)
        comments = self._comments(repo, pr, deadline)
        return _snapshot_from_json(pull, checks, comments, require_comments=True)


class HeadChangedError(WatchError):
    """The CI observation crossed a fixed-head change."""

    def __init__(self, old: str | None, new: str | None, url: str) -> None:
        super().__init__("head changed")
        self.old = old
        self.new = new
        self.url = url


def _latest_checks(checks: Iterable[Check], current_head: str | None) -> tuple[Check, ...]:
    grouped: dict[tuple[str | None, str], list[Check]] = {}
    for check in checks:
        if check.head_sha is not None and check.head_sha != current_head:
            continue
        grouped.setdefault((check.workflow, check.name), []).append(check)
    latest: list[Check] = []
    for identity, entries in grouped.items():
        if len(entries) == 1:
            latest.append(entries[0])
            continue
        if identity[0] is None:
            raise WatchError(f"ambiguous required check identity for {identity[1]}")
        with_start = [entry for entry in entries if entry.started_at is not None]
        if len(with_start) == len(entries):
            latest.append(
                max(
                    with_start,
                    key=lambda entry: (entry.started_at or "", entry.attempt or -1),
                )
            )
            continue
        with_attempt = [entry for entry in entries if entry.attempt is not None]
        if len(with_attempt) == len(entries):
            latest.append(max(with_attempt, key=lambda entry: entry.attempt or 0))
            continue
        workflow, name = identity
        raise WatchError(f"ambiguous attempts for required check {workflow or '<status>'}/{name}")
    return tuple(latest)


def _failure_links(checks: Iterable[Check]) -> tuple[str, ...]:
    links: list[str] = []
    for check in checks:
        if check.bucket in {"fail", "cancel"} and check.link:
            links.append(check.link)
    return _bounded_links(links)


def _bounded_links(links: Iterable[str]) -> tuple[str, ...]:
    result: list[str] = []
    for link in links:
        if isinstance(link, str) and link.startswith(("https://", "http://")):
            short = link[:MAX_LINK_LENGTH]
            if short not in result:
                result.append(short)
        if len(result) == MAX_LINKS:
            break
    return tuple(result)


def classify_ci(snapshot: Snapshot, expected_head: str) -> Decision:
    """Classify one fixed-head CI snapshot without I/O or wall-clock reads."""
    if snapshot.head_sha != expected_head:
        return Decision(Outcome.HEAD_CHANGED, True)
    if snapshot.state == "closed":
        return Decision(Outcome.CLOSED, True)
    current = _latest_checks(snapshot.checks, expected_head)
    if not current:
        return Decision(Outcome.PENDING, False)
    failures = tuple(check for check in current if check.bucket in {"fail", "cancel"})
    if failures:
        return Decision(Outcome.CHECK_FAILED, True, _failure_links(failures))
    if all(check.bucket == "pass" for check in current):
        return Decision(
            Outcome.CI_PASSED, True, _bounded_links(check.link for check in current if check.link)
        )
    return Decision(Outcome.PENDING, False)


def _control_comments(comments: Iterable[Comment]) -> tuple[Comment, ...]:
    return tuple(
        comment for comment in comments if comment.author and CONTROL_COMMAND.search(comment.body)
    )


def _queue_terminal(snapshot: Snapshot, memory: QueueMemory) -> Decision | None:
    controls = _control_comments(snapshot.comments)
    new_controls = [
        item for item in controls if item.identifier not in memory.baseline_comment_ids
    ]
    if new_controls:
        return Decision(Outcome.COMPETING_CONTROL, True)
    if snapshot.merged_at or snapshot.state == "closed":
        return Decision(Outcome.MERGED if snapshot.merged_at else Outcome.CLOSED, True)
    if "dequeued" in snapshot.labels:
        return Decision(Outcome.DEQUEUED, True)
    current = _latest_checks(snapshot.checks, snapshot.head_sha)
    failures = tuple(check for check in current if check.bucket in {"fail", "cancel"})
    if failures:
        return Decision(Outcome.CHECK_FAILED, True, _failure_links(failures))
    if snapshot.mergify_state in {"failure", "error", "cancelled", "cancel"}:
        return Decision(Outcome.QUEUE_FAILED, True)
    return None


def classify_queue(snapshot: Snapshot, memory: QueueMemory, *, remaining_seconds: int) -> Decision:
    """Classify queue state, preserving uncertainty until the next observation."""
    terminal = _queue_terminal(snapshot, memory)
    if terminal is not None:
        return terminal
    wait = min(QUEUE_POLL_SECONDS, max(0, remaining_seconds))
    return Decision(Outcome.PENDING, False, next_wait_seconds=wait)


def _startup(mode: str, repo: str, pr: int, timeout: int) -> None:
    line = f"watch_pr: started mode={mode} repo={repo} pr={pr} timeout_seconds={timeout}"
    assert len(line) <= MAX_OUTPUT_LINE_LENGTH
    print(line, flush=True)


def _bounded_text(value: str | None, limit: int) -> str | None:
    return value[:limit] if value is not None else None


def _summary(result: WatchResult) -> str:
    payload = {
        "outcome": result.outcome,
        "pr_url": _bounded_text(result.url, MAX_LINK_LENGTH),
        "observed_head": _bounded_text(result.observed_head, MAX_HEAD_LENGTH),
        "elapsed_seconds": result.elapsed_seconds,
        "query_count": result.query_count,
        "links": list(_bounded_links(result.links)),
    }
    summary = json.dumps(payload, separators=(",", ":"))
    assert len(summary) <= MAX_OUTPUT_LINE_LENGTH
    return summary


def _result(
    outcome: Outcome,
    snapshot: Snapshot | None,
    started: float,
    clock: Clock,
    transport: Transport,
) -> WatchResult:
    return WatchResult(
        outcome,
        snapshot.url if snapshot else "",
        snapshot.head_sha if snapshot else None,
        max(0, int(clock.monotonic() - started)),
        transport.query_count,
        (),
    )


def _result_with_links(
    decision: Decision,
    snapshot: Snapshot,
    started: float,
    clock: Clock,
    transport: Transport,
) -> WatchResult:
    return WatchResult(
        decision.outcome,
        snapshot.url,
        snapshot.head_sha,
        max(0, int(clock.monotonic() - started)),
        transport.query_count,
        decision.links,
    )


def _watch_ci(
    transport: Transport,
    clock: Clock,
    repo: str,
    pr: int,
    expected_head: str,
    deadline: float,
    started: float,
) -> WatchResult:
    snapshot: Snapshot | None = None
    while clock.monotonic() < deadline:
        try:
            snapshot = transport.ci_snapshot(repo, pr, deadline)
        except HeadChangedError as error:
            return _result(
                Outcome.HEAD_CHANGED,
                Snapshot(error.url, "open", None, error.new),
                started,
                clock,
                transport,
            )
        decision = classify_ci(snapshot, expected_head)
        if decision.terminal:
            return _result_with_links(decision, snapshot, started, clock, transport)
        remaining = deadline - clock.monotonic()
        clock.sleep(min(CI_POLL_SECONDS, max(0, remaining)))
    return _result(Outcome.TIMEOUT, snapshot, started, clock, transport)


def _watch_queue(
    transport: Transport,
    clock: Clock,
    repo: str,
    pr: int,
    deadline: float,
    started: float,
) -> WatchResult:
    memory = QueueMemory()
    snapshot: Snapshot | None = None
    while clock.monotonic() < deadline:
        snapshot = transport.queue_snapshot(repo, pr, deadline)
        if not memory.initialized:
            memory.baseline_comment_ids = {
                comment.identifier for comment in _control_comments(snapshot.comments)
            }
            memory.initialized = True
        decision = classify_queue(
            snapshot,
            memory,
            remaining_seconds=max(0, int(deadline - clock.monotonic())),
        )
        if decision.terminal:
            return _result_with_links(decision, snapshot, started, clock, transport)
        wait = decision.next_wait_seconds or QUEUE_POLL_SECONDS
        clock.sleep(min(wait, max(0, deadline - clock.monotonic())))
    return _result(Outcome.TIMEOUT, snapshot, started, clock, transport)


def watch(
    transport: Transport,
    *,
    mode: str,
    repo: str,
    pr: int,
    timeout_seconds: int,
    expected_head: str | None,
    clock: Clock,
) -> WatchResult:
    """Run one watcher with an overall monotonic deadline."""
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
    if mode == "ci" and not expected_head:
        raise ValueError("expected_head is required in ci mode")
    started = clock.monotonic()
    deadline = started + timeout_seconds
    if mode == "ci":
        assert expected_head is not None
        return _watch_ci(transport, clock, repo, pr, expected_head, deadline, started)
    if mode == "queue":
        return _watch_queue(transport, clock, repo, pr, deadline, started)
    raise ValueError(f"unsupported mode: {mode}")


EXIT_CODES = {
    Outcome.PENDING: 2,
    Outcome.CI_PASSED: 0,
    Outcome.MERGED: 0,
    Outcome.TIMEOUT: 124,
    Outcome.HEAD_CHANGED: 1,
    Outcome.CLOSED: 1,
    Outcome.CHECK_FAILED: 1,
    Outcome.QUEUE_FAILED: 1,
    Outcome.DEQUEUED: 1,
    Outcome.COMPETING_CONTROL: 1,
    Outcome.OBSERVATION_ERROR: 2,
}


def _exit_code(outcome: Outcome, *, signal_number: int | None = None) -> int:
    if outcome is Outcome.CANCELLED:
        return 128 + signal_number if signal_number is not None else 130
    return EXIT_CODES[outcome]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, help="OWNER/REPO")
    parser.add_argument("--pr", required=True, type=int)
    parser.add_argument("--mode", required=True, choices=("ci", "queue"))
    parser.add_argument("--expected-head")
    parser.add_argument("--timeout-seconds", required=True, type=int)
    return parser


def _validate_args(args: argparse.Namespace) -> None:
    if (
        len(args.repo) > MAX_REPO_LENGTH
        or "/" not in args.repo
        or any(not part for part in args.repo.split("/", 1))
    ):
        raise ValueError("--repo must be OWNER/REPO")
    if not 0 < args.pr <= MAX_PR_NUMBER:
        raise ValueError(f"--pr must be between 1 and {MAX_PR_NUMBER}")
    if not 0 < args.timeout_seconds <= MAX_TIMEOUT_SECONDS:
        raise ValueError(f"--timeout-seconds must be between 1 and {MAX_TIMEOUT_SECONDS}")
    if args.mode == "ci" and (
        not args.expected_head or not re.fullmatch(r"[0-9a-fA-F]{7,64}", args.expected_head)
    ):
        raise ValueError("--expected-head must be a hexadecimal commit SHA in ci mode")


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    try:
        args = parser.parse_args(argv)
        _validate_args(args)
    except ValueError as error:
        parser.error(str(error))
    clock = SystemClock()
    transport = GhTransport(clock=clock)
    cancelled_by: int | None = None
    started = clock.monotonic()

    def cancel(_signum: int, _frame: Any) -> None:
        nonlocal cancelled_by
        cancelled_by = _signum
        transport.cancel()
        raise CancelledWatchError("cancelled")

    signal.signal(signal.SIGINT, cancel)
    signal.signal(signal.SIGTERM, cancel)
    try:
        _startup(args.mode, args.repo, args.pr, args.timeout_seconds)
        result = watch(
            transport,
            mode=args.mode,
            repo=args.repo,
            pr=args.pr,
            timeout_seconds=args.timeout_seconds,
            expected_head=args.expected_head,
            clock=clock,
        )
    except CancelledWatchError:
        result = _result(Outcome.CANCELLED, None, started, clock, transport)
    except TimeoutError:
        result = _result(Outcome.TIMEOUT, None, started, clock, transport)
    except WatchError:
        result = _result(Outcome.OBSERVATION_ERROR, None, started, clock, transport)
    print(_summary(result), flush=True)
    return _exit_code(result.outcome, signal_number=cancelled_by)


if __name__ == "__main__":
    raise SystemExit(main())
