"""Tests for the durable job-record store (ADR 0027 async submit/poll)."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from booley.runtime import job_records as jobrec

_STARTED = "2026-07-04T13:15:02Z"
_RUN_STARTED = "2026-07-04T13:45:02Z"  # promoted 30 min after submit
_ARGV = ["python", "-m", "booley.flows.sim", "--cfg", "top"]


@pytest.fixture()
def _jobs_env(tmp_path: Path):
    """Point the runtime at a tmp tree so job records land under tmp/jobs/."""
    return tmp_path / "jobs"


def _rec(**fields: Any) -> jobrec.JobRecord:
    base: dict[str, Any] = {"run_id": "r", "endpoint": "sim", "started_at": "t", "timeout_s": 60}
    return jobrec.JobRecord(**{**base, **fields})


def _after(stamp: str, seconds: float) -> float:
    epoch = jobrec.parse_stamp(stamp)
    assert epoch is not None
    return epoch + seconds


@pytest.mark.parametrize(
    ("exit_code", "timed_out", "expected"),
    [
        (0, False, jobrec.STATUS_DONE),
        (1, False, jobrec.STATUS_FAILED),
        # A timeout-killed run must never read as success.
        (0, True, jobrec.STATUS_FAILED),
    ],
)
def test_terminal_status(exit_code: int, timed_out: bool, expected: str) -> None:
    assert jobrec.terminal_status(exit_code, timed_out=timed_out) == expected


_LIVE: dict[str, Any] = {"pid": 1234}
_STAMPED: dict[str, Any] = {"pid": 1234, "started_at": _STARTED}
_QUEUED: dict[str, Any] = {**_STAMPED, "run_started_at": _RUN_STARTED}
_NAMED: dict[str, Any] = {"pid": 1234, "argv": _ARGV}
_PAST = 60 + jobrec.DEADLINE_SLACK_SECONDS + 1


def _status_case(
    name: str, fields: dict[str, Any], expected: str, *, alive: bool = True, **kwargs: Any
) -> Any:
    return pytest.param(fields, alive, kwargs, expected, id=name)


@pytest.mark.parametrize(
    ("fields", "alive", "kwargs", "expected"),
    [
        _status_case(
            "cancelled-terminal",
            {"status": jobrec.STATUS_CANCELLED, "exit_code": 130},
            jobrec.STATUS_CANCELLED,
        ),
        # Even a live PID cannot override a recorded terminal outcome.
        _status_case(
            "terminal-beats-live-pid",
            {"status": jobrec.STATUS_DONE, "exit_code": 0, "pid": 1},
            jobrec.STATUS_DONE,
        ),
        _status_case("dead-pid-failed", _LIVE, jobrec.STATUS_FAILED, alive=False),
        # No PID yet means the spawn never happened: nothing to wait on.
        _status_case("no-pid-failed", {"pid": None}, jobrec.STATUS_FAILED),
        # Past the deadline nothing can finish this job (the watchdog died with the
        # server): a live PID is reuse or a hung orphan, and polling would never end.
        _status_case("past-deadline", _STAMPED, jobrec.STATUS_FAILED, now=_after(_STARTED, _PAST)),
        _status_case(
            "within-deadline", _STAMPED, jobrec.STATUS_RUNNING, now=_after(_STARTED, _PAST - 2)
        ),
        # A garbage stamp must not crash or fail a live job.
        _status_case("garbage-start-skips-deadline", _LIVE, jobrec.STATUS_RUNNING, now=9e18),
        # Container PID namespaces are dense: after a restart the recorded PID can
        # belong to an unrelated process.
        _status_case(
            "recycled-pid",
            _NAMED,
            jobrec.STATUS_FAILED,
            read_cmdline=lambda _pid: ["sleep", "3600"],
        ),
        _status_case(
            "matching-cmdline",
            _NAMED,
            jobrec.STATUS_RUNNING,
            read_cmdline=lambda _pid: list(_ARGV),
        ),
        # None means "cannot judge" (no /proc): never a mismatch.
        _status_case(
            "unreadable-cmdline", _NAMED, jobrec.STATUS_RUNNING, read_cmdline=lambda _pid: None
        ),
        # Queue wait (ADR 0028) must not count against the deadline: this is well
        # past submit + budget + slack but inside run start + budget.
        _status_case(
            "run-anchored-within", _QUEUED, jobrec.STATUS_RUNNING, now=_after(_RUN_STARTED, 30)
        ),
        _status_case(
            "run-anchored-past",
            _QUEUED,
            jobrec.STATUS_FAILED,
            now=_after(_RUN_STARTED, _PAST),
        ),
    ],
)
def test_derive_status(
    fields: dict[str, Any], alive: bool, kwargs: dict[str, Any], expected: str
) -> None:
    assert jobrec.derive_status(_rec(**fields), lambda _pid: alive, **kwargs) == expected


@pytest.mark.parametrize(
    ("pid", "alive", "age", "active"),
    [
        pytest.param(1234, True, 1, True, id="live"),
        pytest.param(None, False, 1, True, id="fresh-pidless"),
        pytest.param(None, False, jobrec.SPAWN_GRACE_SECONDS + 1, False, id="stale-pidless"),
    ],
)
def test_is_active(pid: int | None, alive: bool, age: float, active: bool) -> None:
    rec = _rec(pid=pid, started_at=_STARTED)
    assert jobrec.is_active(rec, lambda _pid: alive, now=_after(_STARTED, age)) is active


@pytest.mark.parametrize(
    ("stamp", "parses"), [(_STARTED, True), ("t", False), (None, False), (1234, False)]
)
def test_parse_stamp(stamp: object, parses: bool) -> None:
    assert (jobrec.parse_stamp(stamp) is not None) is parses


def test_write_then_read(_jobs_env) -> None:
    rec = jobrec.JobRecord(
        run_id="simulate-x-1",
        endpoint="sim",
        started_at=_STARTED,
        timeout_s=1290,
        argv=["python", "-m", "booley.flows.sim"],
        pid=4242,
        display_scope="nested",
        run_started_at=_RUN_STARTED,
        owner="srv|codex:t1",
        read_at="2026-01-01T00:00:00Z",
    )
    jobrec.write_record(rec, root=_jobs_env)
    got = jobrec.read_record(rec.run_id, root=_jobs_env)
    assert got is not None
    assert (got.endpoint, got.pid, got.argv) == ("sim", 4242, rec.argv)
    assert got.status == jobrec.STATUS_RUNNING  # default
    assert (got.display_scope, got.run_started_at) == ("nested", _RUN_STARTED)
    assert (got.owner, got.read_at) == ("srv|codex:t1", "2026-01-01T00:00:00Z")
    path = _jobs_env / f"{rec.run_id}.json"
    new_payload = json.loads(path.read_text(encoding="utf-8"))
    path.write_text(json.dumps({**new_payload, "lease_id": "historical"}), encoding="utf-8")
    historical = jobrec.read_record(rec.run_id, root=_jobs_env)
    assert historical is not None
    assert (historical.run_id, historical.argv) == (rec.run_id, rec.argv)
    assert jobrec.list_records(_jobs_env) == [historical]
    assert jobrec.strict_records(_jobs_env) == [historical]
    assert "lease_id" not in new_payload
    # write_record replaces atomically: the second write wins and no tmp file lingers.
    rec.status, rec.exit_code = jobrec.STATUS_DONE, 0
    jobrec.write_record(rec, root=_jobs_env)
    got = jobrec.read_record(rec.run_id, root=_jobs_env)
    assert got is not None and got.status == jobrec.STATUS_DONE
    assert not list(_jobs_env.glob("*.tmp"))
    jobrec.write_record(_rec(run_id="simulate-x-2"), root=_jobs_env)
    run_ids = {r.run_id for r in jobrec.list_records(root=_jobs_env)}
    assert run_ids == {"simulate-x-1", "simulate-x-2"}


@pytest.mark.parametrize("content", [None, "{not json"], ids=["missing", "malformed"])
def test_read_missing_or_malformed_returns_none(_jobs_env, content: str | None) -> None:
    if content is not None:
        _jobs_env.mkdir(parents=True)
        (_jobs_env / "broken-1.json").write_text(content, encoding="utf-8")
    assert jobrec.read_record("broken-1", root=_jobs_env) is None


def test_unreadable_record_raises_instead_of_reading_as_absent(
    _jobs_env, monkeypatch: pytest.MonkeyPatch
):
    rec = jobrec.JobRecord(run_id="sim-held-1", endpoint="sim", started_at="t", timeout_s=60)
    jobrec.write_record(rec, root=_jobs_env)
    from tests.sharing_error_support import retry_delays_target

    monkeypatch.setattr(retry_delays_target(), (0.0,), raising=False)
    real_open = Path.open

    def held_open(path: Path, *args: object, **kwargs: object):
        if path.name == "sim-held-1.json":
            error = PermissionError(13, "The process cannot access the file")
            error.winerror = 32  # type: ignore[attr-defined]
            raise error
        return real_open(path, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "open", held_open)
    with pytest.raises(PermissionError):
        jobrec.read_record("sim-held-1", root=_jobs_env)
    # Listing keeps its skip semantics; the Finish fence fails closed with the cause.
    assert jobrec.list_records(_jobs_env) == []
    with pytest.raises(jobrec.JobRecordError, match=r"sim-held-1\.json"):
        jobrec.strict_records(_jobs_env)


def test_write_record_survives_a_transient_windows_reader(
    _jobs_env, monkeypatch: pytest.MonkeyPatch
):
    rec = jobrec.JobRecord(run_id="sim-busy-1", endpoint="sim", started_at="t", timeout_s=60)
    real_replace = Path.replace
    blocked = False

    def replace_after_reader_closes(source: Path, destination: Path):
        nonlocal blocked
        if destination.name == "sim-busy-1.json" and not blocked:
            blocked = True
            error = PermissionError(13, "A reader holds the record open")
            error.winerror = 5  # type: ignore[attr-defined]
            raise error
        return real_replace(source, destination)

    monkeypatch.setattr(Path, "replace", replace_after_reader_closes)
    jobrec.write_record(rec, root=_jobs_env)
    assert blocked
    got = jobrec.read_record("sim-busy-1", root=_jobs_env)
    assert got is not None and got.run_id == "sim-busy-1"


@pytest.mark.parametrize("unlink_fails", [False, True])
def test_delete_record_is_best_effort(
    _jobs_env, monkeypatch: pytest.MonkeyPatch, unlink_fails: bool
) -> None:
    rec = _rec(run_id="lint-sync-1")
    jobrec.write_record(rec, root=_jobs_env)

    def fail_unlink(*_args: object, **_kwargs: object) -> None:
        raise OSError

    if unlink_fails:
        monkeypatch.setattr(Path, "unlink", fail_unlink)

    jobrec.delete_record(rec.run_id, root=_jobs_env)

    assert (jobrec.read_record(rec.run_id, root=_jobs_env) is None) is not unlink_fails


def test_explicit_none_disables_storage() -> None:
    rec = _rec(run_id="x")
    jobrec.write_record(rec, None)
    assert jobrec.read_record("x", None) is None
    assert jobrec.list_records(None) == []


def test_legacy_schema_loads_missing_goal_facts() -> None:
    rec = jobrec.JobRecord.from_dict(
        {
            "run_id": "old",
            "endpoint": "lint",
            "started_at": "2026-10-07T10:00:00Z",
            "timeout_s": 30,
        }
    )
    assert rec.work_dir is None and rec.session_key is None
    assert rec.schema == 2
    assert (rec.owner, rec.read_at, rec.display_scope) == (None, None, "developer")


def test_run_id_has_random_eight_hex_suffix() -> None:
    first = jobrec.make_run_id("lint", "20261007T100000Z", 1)
    assert re.fullmatch(r"lint-20261007T100000Z-1-[0-9a-f]{8}", first)
    assert jobrec.make_run_id("lint", "20261007T100000Z", 1) != first


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("work_dir", 17),
        ("session_key", []),
        ("schema", False),
        ("schema", 0),
        ("schema", "2"),
    ],
)
def test_malformed_routing_envelope_is_job_error(tmp_path, field, value):
    raw = jobrec.JobRecord("bad-envelope", "lint", "20261007T120000Z", 60).to_dict()
    raw[field] = value
    with pytest.raises(jobrec.JobRecordError, match=field):
        jobrec.JobRecord.from_dict(raw)
    root = tmp_path / "jobs"
    root.mkdir()
    (root / "bad-envelope.json").write_text(json.dumps(raw))
    with pytest.raises(jobrec.JobRecordError, match=field):
        jobrec.read_record("bad-envelope", root)
    with pytest.raises(jobrec.JobRecordError, match=field):
        jobrec.strict_records(root)


def test_list_records_skips_malformed_envelope(tmp_path: Path) -> None:
    jobrec.write_record(jobrec.JobRecord("good", "sim", "t", 60), root=tmp_path)
    (tmp_path / "bad.json").write_text(
        '{"run_id": "bad", "endpoint": "sim", "started_at": "t", "timeout_s": 60, "schema": 0}',
        encoding="utf-8",
    )

    assert [rec.run_id for rec in jobrec.list_records(tmp_path, skip_malformed=True)] == ["good"]


def test_job_record_paths_translate_only_under_former_mount(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.mount_change_support import simulate_mount_change

    current = tmp_path / "current"
    former = simulate_mount_change(monkeypatch, current, tmp_path / "former")
    workspace = str(tmp_path / "work" / "project")

    def load(**paths: str) -> jobrec.JobRecord:
        raw = jobrec.JobRecord("run-1", "sim", "t", 60).to_dict()
        raw.update(paths)
        return jobrec.JobRecord.from_dict(raw)

    moved = load(work_dir=f"{former}/worktrees/wt", report_root=f"{former}/runtime")
    assert moved.work_dir == str(current / "worktrees" / "wt")
    assert moved.report_root == str(current / "runtime")
    kept = load(work_dir=workspace, report_root=str(current / "runtime"))
    assert kept.work_dir == workspace
    assert kept.report_root == str(current / "runtime")
    assert load(work_dir="relative/dir").work_dir == "relative/dir"
    assert load(work_dir=str(tmp_path / "former-other")).work_dir == str(tmp_path / "former-other")


def test_malformed_mount_history_names_record_and_fix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from booley.runtime import sandbox_layout
    from booley.runtime.mount_history import MOUNT_HISTORY, MountHistoryError

    monkeypatch.setattr(sandbox_layout, "BOOLEY_DIR_TARGET", str(tmp_path / "current"))
    history = tmp_path / "current" / MOUNT_HISTORY
    history.parent.mkdir(parents=True)
    root = tmp_path / "jobs"
    root.mkdir()
    jobrec.write_record(
        jobrec.JobRecord("run-9", "sim", "t", 60, work_dir=str(tmp_path / "elsewhere")), root=root
    )
    for content in (
        b"{",
        b'{"schema": 2, "mounts": []}',
        b'{"schema": 1, "mounts": [3]}',
        b"\xff",
    ):
        history.write_bytes(content)
        with pytest.raises(
            MountHistoryError, match=r"Job record run-9.*sandbox-mounts\.json.*restore"
        ):
            jobrec.read_record("run-9", root)
    history.unlink()
    history.mkdir()
    with pytest.raises(MountHistoryError, match="cannot read"):
        jobrec.read_record("run-9", root)
