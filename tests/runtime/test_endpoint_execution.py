"""Contract tests for transport-independent endpoint execution sequencing."""

from __future__ import annotations

import os
import sys
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch

import pytest

from booley.flows import job_resources
from booley.flows.base import FlowMechanics
from booley.runtime.endpoint_execution import (
    EXIT_ERROR,
    EXIT_SUCCESS,
    EndpointOutcome,
    EndpointRejectedError,
    ExecutionResult,
    execute_endpoint,
    normalize_completion_error,
)


@dataclass
class _ReleaseWitness(AbstractContextManager[object]):
    events: list[str]
    borrowed: object
    requeued: bool = False

    def __enter__(self) -> object:
        self.events.append("admit(requeued)" if self.requeued else "admit")
        return self.borrowed

    def __exit__(self, *exc_info: object) -> None:
        self.events.append("release")


class _Endpoint:
    """Small fake exposing the lifecycle operations owned outside the coordinator."""

    def __init__(
        self,
        *,
        fail: bool = False,
        reject_gate: bool = False,
        reject_admission: bool = False,
        unexpected_failure: bool = False,
        publication_failure: bool = False,
        requeue_after_oom: bool = False,
    ) -> None:
        self.events: list[str] = []
        self.requeue_after_oom = requeue_after_oom
        self.fail = fail
        self.reject_gate = reject_gate
        self.reject_admission = reject_admission
        self.unexpected_failure = unexpected_failure
        self.publication_failure = publication_failure
        self.borrowed = object()

    def admission(
        self, prepared: object, *, requeued: bool = False
    ) -> AbstractContextManager[object]:
        assert prepared == "prepared"
        if self.reject_gate:
            self.events.append("gate-rejected")
            raise EndpointRejectedError(
                EndpointOutcome(exit_code=EXIT_ERROR, report_text="unbound target")
            )
        if self.reject_admission:
            self.events.append("admission-rejected")
            raise EndpointRejectedError(
                EndpointOutcome(exit_code=EXIT_ERROR, report_text="admission refused")
            )
        return _ReleaseWitness(self.events, self.borrowed, requeued)

    def may_requeue_after_oom(self, prepared: object, admission: object) -> bool:
        assert prepared == "prepared"
        return self.requeue_after_oom

    def begin_requeued_attempt(self) -> None:
        self.events.append("begin-requeued-attempt")

    def invoke_endpoint(
        self, prepared: object, *, admission: object, started: float
    ) -> EndpointOutcome:
        assert prepared == "prepared"
        assert admission is self.borrowed
        assert started > 0
        self.events.append("invoke")
        if self.unexpected_failure:
            raise RuntimeError("unexpected adapter failure")
        if self.fail:
            self.events.append("normalize-error")
            return EndpointOutcome(exit_code=EXIT_ERROR)
        return EndpointOutcome(
            exit_code=EXIT_SUCCESS,
            report_text="ok",
        )

    def finish_execution(
        self,
        prepared: object,
        outcome: EndpointOutcome,
        *,
        started: float | None,
        records_published: bool,
    ) -> ExecutionResult:
        assert prepared == "prepared"
        if not outcome.report_text.startswith(("admission refused", "unbound target")):
            assert started is not None
        self.events.append(f"finish:{outcome.exit_code}:{records_published}")
        return ExecutionResult(exit_code=outcome.exit_code, outcome=outcome)

    def publish_records(self, prepared: object, outcome: EndpointOutcome) -> None:
        assert prepared == "prepared"
        self.events.append(f"publish:{outcome.exit_code}")
        if self.publication_failure:
            raise RuntimeError("publication failed")


def test_execute_endpoint_owns_admit_invoke_publish_finish_order() -> None:
    endpoint = _Endpoint()

    result = execute_endpoint(endpoint, "prepared")

    assert isinstance(result, ExecutionResult)
    assert result.exit_code == EXIT_SUCCESS
    assert result.outcome.report_text == "ok"
    assert endpoint.events == [
        "admit",
        "invoke",
        "publish:0",
        "finish:0:True",
        "release",
    ]


def test_execute_endpoint_normalizes_failure_and_releases_admission() -> None:
    endpoint = _Endpoint(fail=True)

    result = execute_endpoint(endpoint, "prepared")

    assert result.exit_code == EXIT_ERROR
    assert result.outcome.exit_code == EXIT_ERROR
    assert endpoint.events == [
        "admit",
        "invoke",
        "normalize-error",
        "publish:2",
        "finish:2:True",
        "release",
    ]


def test_completion_error_message_is_bounded() -> None:
    outcome = EndpointOutcome()

    normalize_completion_error(outcome, RuntimeError("x" * 600), "publish report")

    message = outcome.detail["completion_error"]["message"]
    assert len(message) == 500
    assert message.endswith("…")


def test_admission_rejection_finishes_without_invoking_endpoint() -> None:
    endpoint = _Endpoint(reject_admission=True)

    result = execute_endpoint(endpoint, "prepared")

    assert result.exit_code == EXIT_ERROR
    assert result.outcome.report_text == "admission refused"
    assert endpoint.events == [
        "admission-rejected",
        "publish:2",
        "finish:2:True",
    ]


def test_unexpected_adapter_failure_propagates_and_releases_admission() -> None:
    endpoint = _Endpoint(unexpected_failure=True)

    with pytest.raises(RuntimeError, match="unexpected adapter failure"):
        execute_endpoint(endpoint, "prepared")

    assert endpoint.events == ["admit", "invoke", "release"]


def test_publication_failure_runs_non_persisting_finish_and_releases_admission() -> None:
    endpoint = _Endpoint(publication_failure=True)

    result = execute_endpoint(endpoint, "prepared")

    assert result.exit_code == EXIT_ERROR
    assert result.outcome.detail["completion_error"] == {
        "operation": "publish Simulation Record projections",
        "type": "RuntimeError",
        "message": "publication failed",
    }
    assert result.outcome.report_text == (
        "ok\nCompletion failure (publish Simulation Record projections): "
        "RuntimeError: publication failed"
    )
    assert endpoint.events == [
        "admit",
        "invoke",
        "publish:0",
        "finish:2:False",
        "release",
    ]


def test_gate_rejection_finishes_before_admission() -> None:
    endpoint = _Endpoint(reject_gate=True)

    result = execute_endpoint(endpoint, "prepared")

    assert result.exit_code == EXIT_ERROR
    assert result.outcome.report_text == "unbound target"
    assert endpoint.events == ["gate-rejected", "publish:2", "finish:2:True"]


class _CommandEndpoint(_Endpoint):
    """Endpoint whose Job runs one command through a Flow's local executor."""

    def __init__(self, flow: FlowMechanics, code: str) -> None:
        super().__init__()
        self.flow, self.code = flow, code

    def invoke_endpoint(
        self, prepared: object, *, admission: object, started: float
    ) -> EndpointOutcome:
        self.flow._execute_local([sys.executable, "-c", self.code])
        return EndpointOutcome(exit_code=EXIT_SUCCESS)


@pytest.mark.skipif(os.name == "nt", reason="POSIX signals")
def test_job_outcome_records_only_its_own_oom_kill(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.flows.test_flow_base import EchoFlow, _flow_env

    flow = EchoFlow()
    with patch.dict(os.environ, _flow_env()):
        flow.parse_args(["--target", "test", "--work-dir", str(tmp_path)])
    # The container's OOM-kill counter moves during both Jobs' commands.
    counts = iter([0, 1, 1, 2])
    monkeypatch.setattr(
        job_resources,
        "cgroup_memory_events",
        lambda: job_resources.MemoryEvents(*[next(counts)] * 2),
    )
    killed = "import os, signal; os.kill(os.getpid(), signal.SIGKILL)"

    own_kill = execute_endpoint(_CommandEndpoint(flow, killed), "prepared")
    other_failure = execute_endpoint(_CommandEndpoint(flow, "raise SystemExit(1)"), "prepared")

    assert own_kill.outcome.oom_killed is True
    assert other_failure.outcome.oom_killed is False


class _RequeueEndpoint(_Endpoint):
    """Endpoint armed for a requeue whose attempts follow a script of behaviours."""

    def __init__(self, *attempts: Callable[[], EndpointOutcome]) -> None:
        super().__init__(requeue_after_oom=True)
        self.attempts = list(attempts)
        self.armed: list[bool] = []

    def invoke_endpoint(
        self, prepared: object, *, admission: object, started: float
    ) -> EndpointOutcome:
        self.events.append("invoke")
        # An armed attempt stops its commands at the first OOM kill; the second never does.
        self.armed.append(_stop_on_oom_armed())
        return self.attempts[len(self.armed) - 1]()


def _stop_on_oom_armed() -> bool:
    """Whether the current attempt stops its commands after an OOM kill."""
    from booley.runtime.endpoint_execution import _JOB_ATTEMPT

    attempt = _JOB_ATTEMPT.get()
    assert attempt is not None
    return attempt.stop_on_oom


def _oom_failure() -> EndpointOutcome:
    """An attempt whose command was OOM-killed and which failed."""
    from booley.runtime.endpoint_execution import note_job_oom_kill

    note_job_oom_kill()
    return EndpointOutcome(exit_code=EXIT_ERROR, report_text="attempt 1 verdict")


def _final_success() -> EndpointOutcome:
    return EndpointOutcome(exit_code=EXIT_SUCCESS, report_text="attempt 2 verdict")


def test_requeue_releases_the_claim_before_publishing_the_final_attempt() -> None:
    endpoint = _RequeueEndpoint(_oom_failure, _final_success)

    result = execute_endpoint(endpoint, "prepared")

    # The killed attempt publishes nothing: publication and finish see attempt 2 only.
    assert endpoint.events == [
        "admit",
        "invoke",
        "begin-requeued-attempt",
        "release",
        "admit(requeued)",
        "invoke",
        "publish:0",
        "finish:0:True",
        "release",
    ]
    # Attempt 1 was armed (stopped after its kill); attempt 2 never is.
    assert endpoint.armed == [True, False]
    assert result.exit_code == EXIT_SUCCESS
    assert result.outcome.report_text.startswith("attempt 2 verdict\n")
    assert "attempt 1 verdict" not in result.outcome.report_text
    assert result.outcome.detail["oom_requeue"] == {
        "reason": "requeued after out-of-memory",
        "attempts": 2,
    }
    assert result.outcome.oom_killed is False


_REQUEUE_NOTE = (
    "Requeued after out-of-memory: this result is from the Job's second attempt, which ran alone."
)
_SECOND_KILL_LINE = "Killed for out-of-memory: the second attempt was OOM-killed too, so the Job is not retried again."
_UNARMED_KILL_LINE = "Killed for out-of-memory: this Job may not be requeued after an OOM kill, so it is not retried."


def _second_oom_failure() -> EndpointOutcome:
    """The requeued attempt, OOM-killed too."""
    from booley.runtime.endpoint_execution import note_job_oom_kill

    note_job_oom_kill()
    return EndpointOutcome(exit_code=EXIT_ERROR, report_text="attempt 2 verdict")


def test_job_oom_killed_on_both_attempts_names_the_second_kill() -> None:
    endpoint = _RequeueEndpoint(_oom_failure, _second_oom_failure)

    result = execute_endpoint(endpoint, "prepared")

    assert endpoint.events.count("invoke") == 2
    assert result.exit_code == EXIT_ERROR
    assert result.outcome.report_text == f"attempt 2 verdict\n{_REQUEUE_NOTE}\n{_SECOND_KILL_LINE}"
    assert result.outcome.detail["oom_requeue"]["attempts"] == 2
    assert result.outcome.oom_killed is True


def test_unarmed_job_oom_kill_names_the_kill_and_no_retry() -> None:
    endpoint = _RequeueEndpoint(_oom_failure)
    endpoint.requeue_after_oom = False

    result = execute_endpoint(endpoint, "prepared")

    assert endpoint.events.count("invoke") == 1
    assert result.outcome.report_text == f"attempt 1 verdict\n{_UNARMED_KILL_LINE}"
    assert "oom_requeue" not in result.outcome.detail


def test_requeued_job_that_passes_keeps_todays_text() -> None:
    from booley.runtime.endpoint_execution import note_job_oom_kill

    def survived_kill() -> EndpointOutcome:
        note_job_oom_kill()  # a command was killed, yet the Job succeeded
        return _final_success()

    for second in (_final_success, survived_kill):
        endpoint = _RequeueEndpoint(_oom_failure, second)
        result = execute_endpoint(endpoint, "prepared")
        assert result.outcome.report_text == f"attempt 2 verdict\n{_REQUEUE_NOTE}"


def test_final_work_item_oom_kill_is_named_in_the_job_result() -> None:
    from booley.runtime.endpoint_execution import (
        job_attempt,
        note_final_work_item_oom_kill,
        note_job_oom_kill,
    )

    def run_items() -> EndpointOutcome:
        note_final_work_item_oom_kill("outside-item", passed=False)  # no work item attempt
        with job_attempt(stop_on_oom=False, work_item=True):
            note_job_oom_kill()
            note_final_work_item_oom_kill("gamma", passed=False)
        with job_attempt(stop_on_oom=True, work_item=True):
            note_job_oom_kill()  # armed: it is requeued, not final
            note_final_work_item_oom_kill("armed", passed=False)
        with job_attempt(stop_on_oom=False, work_item=True):
            note_job_oom_kill()
            note_final_work_item_oom_kill("passed", passed=True)
        with job_attempt(stop_on_oom=False, work_item=True):
            note_final_work_item_oom_kill("unkilled", passed=False)
        return EndpointOutcome(exit_code=EXIT_ERROR, report_text="verdict")

    endpoint = _RequeueEndpoint(run_items)

    result = execute_endpoint(endpoint, "prepared")

    assert result.outcome.report_text == (
        "verdict\nKilled for out-of-memory: work item gamma's last attempt was OOM-killed, "
        "so it is not retried again."
    )


_HOST_JOB_LINE = (
    "Killed for host out-of-memory: the host ran out of memory while the Sandbox stayed within "
    "its [sandbox].memory limit, so the Job is not retried. Free memory on the host, "
    "or lower [sandbox].memory and recreate the Sandbox, then run it again."
)


def _host_oom_failure() -> EndpointOutcome:
    """An attempt whose command the host's OOM killer took and which failed."""
    from booley.runtime.endpoint_execution import note_job_oom_kill

    note_job_oom_kill(host=True)
    return EndpointOutcome(exit_code=EXIT_ERROR, report_text="attempt 1 verdict")


def test_host_oom_kill_is_not_requeued_and_names_host_memory() -> None:
    endpoint = _RequeueEndpoint(_host_oom_failure, _final_success)

    result = execute_endpoint(endpoint, "prepared")

    # Armed for a requeue, yet nothing runs again: the host's pressure stays.
    assert endpoint.events.count("invoke") == 1
    assert endpoint.armed == [True]
    assert result.exit_code == EXIT_ERROR
    # The host line leads; the Flow's own error stays visible below it.
    assert result.outcome.report_text == f"{_HOST_JOB_LINE}\nattempt 1 verdict"
    assert "oom_requeue" not in result.outcome.detail
    assert result.outcome.oom_killed is True


def test_host_oom_kill_of_a_work_item_is_named_and_not_requeued() -> None:
    from booley.runtime.endpoint_execution import (
        job_attempt,
        note_final_work_item_oom_kill,
        note_job_oom_kill,
        work_item_attempt_requeues,
    )

    def run_items() -> EndpointOutcome:
        with job_attempt(stop_on_oom=True, work_item=True):
            note_job_oom_kill(host=True)
            # Armed, yet a host kill never requeues the item.
            assert not work_item_attempt_requeues(passed=False)
            note_final_work_item_oom_kill("alpha", passed=False)
        with job_attempt(stop_on_oom=False, work_item=True):
            note_job_oom_kill(host=True)
            note_final_work_item_oom_kill("beta", passed=False, raised=True)
        with job_attempt(stop_on_oom=False, work_item=True):
            note_job_oom_kill(host=True)
            note_final_work_item_oom_kill("passed", passed=True)
        return EndpointOutcome(exit_code=EXIT_ERROR, report_text="verdict")

    result = execute_endpoint(_RequeueEndpoint(run_items), "prepared")

    assert result.outcome.report_text == (
        "Killed for host out-of-memory: the host ran out of memory while the Sandbox stayed "
        "within its [sandbox].memory limit, so work item alpha, work item beta is not retried. "
        "Free memory on the host, or lower [sandbox].memory and recreate the Sandbox, "
        "then run it again.\nverdict"
    )


def test_a_host_kill_the_job_survived_adds_no_line() -> None:
    from booley.runtime.endpoint_execution import note_job_oom_kill

    def survived() -> EndpointOutcome:
        note_job_oom_kill(host=True)
        return _final_success()

    result = execute_endpoint(_RequeueEndpoint(survived), "prepared")

    assert result.outcome.report_text == "attempt 2 verdict"


@pytest.mark.skipif(os.name == "nt", reason="POSIX signals")
@pytest.mark.parametrize(
    ("limit_events", "requeued"),
    [([0, 1, 1, 1], True), ([0, 0], False)],
    ids=["sandbox-limit", "host"],
)
def test_sandbox_limit_kill_requeues_and_host_kill_does_not(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, limit_events: list[int], requeued: bool
) -> None:
    import itertools

    from tests.flows.test_flow_base import EchoFlow, _flow_env

    flow = EchoFlow()
    with patch.dict(os.environ, _flow_env()):
        flow.parse_args(["--target", "test", "--work-dir", str(tmp_path)])
    kills = itertools.chain([0, 1], itertools.repeat(1))
    limits = iter(limit_events)
    monkeypatch.setattr(
        job_resources,
        "cgroup_memory_events",
        lambda: job_resources.MemoryEvents(next(kills), next(limits)),
    )
    runs: list[str] = []
    killed = "import os, signal; os.kill(os.getpid(), signal.SIGKILL)"

    def command(code: str) -> Callable[[], EndpointOutcome]:
        def attempt() -> EndpointOutcome:
            runs.append(code)
            result = flow._execute_local([sys.executable, "-c", code])
            return EndpointOutcome(
                exit_code=EXIT_ERROR if result.returncode else EXIT_SUCCESS,
                report_text="verdict",
            )

        return attempt

    endpoint = _RequeueEndpoint(command(killed), command("raise SystemExit(0)"))

    result = execute_endpoint(endpoint, "prepared")

    assert (len(runs) == 2) is requeued
    if requeued:
        assert result.exit_code == EXIT_SUCCESS
        assert result.outcome.detail["oom_requeue"]["attempts"] == 2
    else:
        assert result.exit_code == EXIT_ERROR
        assert result.outcome.report_text.startswith("Killed for host out-of-memory")


def test_requeued_job_rejected_at_its_second_admission_names_the_requeue() -> None:
    endpoint = _RequeueEndpoint(_oom_failure)
    real_admission = endpoint.admission

    def refuse_requeued(
        prepared: object, *, requeued: bool = False
    ) -> AbstractContextManager[object]:
        if requeued:
            raise EndpointRejectedError(
                EndpointOutcome(exit_code=EXIT_ERROR, report_text="admission refused")
            )
        return real_admission(prepared, requeued=requeued)

    endpoint.admission = refuse_requeued  # type: ignore[method-assign]

    result = execute_endpoint(endpoint, "prepared")

    assert endpoint.events == [
        "admit",
        "invoke",
        "begin-requeued-attempt",
        "release",
        "publish:2",
        "finish:2:True",
    ]
    assert result.outcome.report_text == (
        "admission refused\nRequeued after out-of-memory: the Job did not start again."
    )
    assert result.outcome.detail["oom_requeue"]["attempts"] == 2


def test_only_a_failed_or_stopped_oom_killed_job_is_requeued() -> None:
    from booley.core.exit_codes import EXIT_CANCELLED
    from booley.runtime.endpoint_execution import note_job_oom_kill

    def oom_cancelled() -> EndpointOutcome:
        note_job_oom_kill()
        return EndpointOutcome(exit_code=EXIT_CANCELLED)

    def oom_survived() -> EndpointOutcome:
        note_job_oom_kill()  # killed command, yet the Job went on and succeeded
        return EndpointOutcome(exit_code=EXIT_SUCCESS)

    def stopped_but_succeeded() -> EndpointOutcome:
        note_job_oom_kill()
        assert _job_stopped()  # a later command was skipped: the result misses work
        return EndpointOutcome(exit_code=EXIT_SUCCESS)

    def stopped_and_cancelled() -> EndpointOutcome:
        note_job_oom_kill()
        assert _job_stopped()
        return EndpointOutcome(exit_code=EXIT_CANCELLED)

    def plain_failure() -> EndpointOutcome:
        return EndpointOutcome(exit_code=EXIT_ERROR)

    single_attempt = {
        "cancelled": oom_cancelled,
        "survived": oom_survived,
        "stopped-cancelled": stopped_and_cancelled,
        "no-oom-failure": plain_failure,
    }
    for name, behaviour in single_attempt.items():
        endpoint = _RequeueEndpoint(behaviour)
        result = execute_endpoint(endpoint, "prepared")
        assert endpoint.events.count("invoke") == 1, name
        assert "oom_requeue" not in result.outcome.detail, name
        assert "Killed for out-of-memory" not in result.outcome.report_text, name

    # Contrast: succeeding after a skipped command is requeued, so the result is attempt 2's.
    endpoint = _RequeueEndpoint(stopped_but_succeeded, _final_success)
    result = execute_endpoint(endpoint, "prepared")
    assert endpoint.events.count("invoke") == 2
    assert result.outcome.report_text.startswith("attempt 2 verdict")


def _job_stopped() -> bool:
    from booley.runtime.endpoint_execution import job_stopped_after_oom

    return job_stopped_after_oom()


def test_an_unarmed_endpoint_is_never_requeued_after_an_oom_kill() -> None:
    endpoint = _RequeueEndpoint(_oom_failure)
    endpoint.requeue_after_oom = False

    result = execute_endpoint(endpoint, "prepared")

    assert endpoint.events.count("invoke") == 1
    assert result.outcome.oom_killed is True
    assert "oom_requeue" not in result.outcome.detail


def test_item_attempt_kill_stays_in_the_item() -> None:
    """A work item's OOM kill marks only its own attempt, and a Flow kill still stops the item."""
    from booley.runtime import endpoint_execution as attempts

    job_attempt = attempts.job_attempt
    job_stopped_after_oom = attempts.job_stopped_after_oom
    note_job_oom_kill = attempts.note_job_oom_kill

    with job_attempt(stop_on_oom=True) as flow:
        with job_attempt(stop_on_oom=True, work_item=True) as item:
            assert not job_stopped_after_oom()
            assert not attempts.work_item_attempt_requeues(passed=False)  # no kill yet
            note_job_oom_kill()
            assert item.oom_killed.is_set() and not flow.oom_killed.is_set()
            assert job_stopped_after_oom() and item.stopped.is_set()
            # A failed attempt, or one that skipped a command, is requeued.
            assert attempts.work_item_attempt_requeues(passed=False)
            assert attempts.work_item_attempt_requeues(passed=True)
        # The Flow's own lanes keep running: its attempt was not killed or stopped.
        assert not job_stopped_after_oom() and not flow.stopped.is_set()

        with job_attempt(stop_on_oom=True, work_item=True) as other:
            note_job_oom_kill()
            # A kill that skipped nothing and passed leaves a complete result.
            assert not other.stopped.is_set()
            assert not attempts.work_item_attempt_requeues(passed=True)

        with job_attempt(stop_on_oom=False, work_item=True) as last:
            note_job_oom_kill()  # the requeued attempt is never armed
            assert last.oom_killed.is_set()
            assert not job_stopped_after_oom()
            assert not attempts.work_item_attempt_requeues(passed=False)

        with job_attempt(stop_on_oom=True, work_item=True) as stopped_by_flow:
            flow.oom_killed.set()  # a Flow-level kill (for example a shared build)
            assert job_stopped_after_oom() and flow.stopped.is_set()
            assert not stopped_by_flow.oom_killed.is_set()
            assert not attempts.work_item_attempt_requeues(passed=False)

    # Under the Flow's own claim the current attempt is the Flow's: never a work item's.
    with job_attempt(stop_on_oom=True) as flow_only:
        note_job_oom_kill()
        assert flow_only.oom_killed.is_set()
        assert not attempts.work_item_attempt_requeues(passed=False)
    assert not attempts.work_item_attempt_requeues(passed=False)  # outside any attempt
