"""Tests for the setup/port findings log.

The log's job is to survive: concurrent writers, a corrupt line, a newer Booley
having added a field. Everything downstream (the report and outbound view)
reads through it, so a log that loses or mangles an entry loses a finding.
"""

from __future__ import annotations

import json
import multiprocessing
from contextlib import contextmanager
from pathlib import Path

import pytest

from booley.feedback.findings import (
    CorruptFindingsLogError,
    Finding,
    append,
    log_path,
    read_log,
    rewrite,
)
from tests.time_budget import hang_guard_s

# Waits that a healthy run ends at once; twice the hang guard keeps them above the hang-guard joins.
_WAIT_S = 2 * hang_guard_s()


@pytest.fixture
def booley_dir(tmp_path):
    d = tmp_path / ".booley"
    d.mkdir()
    return d


def test_append_assigns_sequential_ids(booley_dir):
    for expected in ("F-1", "F-2", "F-3"):
        assert append(Finding(title="x"), booley_dir).id == expected


def test_an_impression_is_not_counted_as_a_bug(booley_dir):
    append(Finding(title="great framework", kind="impression", bucket="booley"), booley_dir)
    log = read_log(booley_dir)
    assert log.impressions and not log.bugs
    assert log.counts() == {"blocker": 0, "workaround": 0, "note": 0}


def test_append_creates_the_booley_dir(tmp_path):
    """Findings must be loggable before setup has finished building the dir."""
    missing = tmp_path / "nope" / ".booley"
    append(Finding(title="early"), missing)
    assert log_path(missing).is_file()


def test_read_log_of_missing_file_is_empty_not_an_error(booley_dir):
    log = read_log(booley_dir)
    assert log.entries == []
    assert log.corrupt_lines == 0


@pytest.mark.parametrize(
    ("raw", "corrupt"),
    [
        ("[1, 2]\n", 1),  # valid JSON that is not an object (fix 216e8477d)
        ("{not json at all\n", 1),
        ('{"title": "junk", "attachments": "not-a-list"}\n', 1),
        ("\n   \n", 0),  # blank lines are ignored silently
    ],
    ids=["non-object", "not-json", "malformed-attachments", "blank-lines"],
)
def test_a_bad_line_costs_at_most_one_finding_not_the_log(booley_dir, raw, corrupt):
    append(Finding(title="good one"), booley_dir)
    with log_path(booley_dir).open("a", encoding="utf-8", newline="\n") as fh:
        fh.write(raw)
    append(Finding(title="another good one"), booley_dir)

    log = read_log(booley_dir)
    assert [e.title for e in log.entries] == ["good one", "another good one"]
    assert log.corrupt_lines == corrupt


def test_unknown_fields_from_a_newer_booley_are_dropped_not_fatal(booley_dir):
    """Forward compatibility: a later version's extra field must still render."""
    with log_path(booley_dir).open("w", encoding="utf-8") as fh:
        fh.write(json.dumps({"title": "from the future", "invented_field": 42}) + "\n")
    log = read_log(booley_dir)
    assert [e.title for e in log.entries] == ["from the future"]


def test_wins_are_separated_and_counts_are_per_severity(booley_dir):
    append(Finding(title="a", severity="blocker"), booley_dir)
    append(Finding(title="b", severity="note"), booley_dir)
    append(Finding(title="c", severity="note"), booley_dir)
    append(Finding(title="worked", kind="win"), booley_dir)  # must not be counted
    log = read_log(booley_dir)
    assert [w.title for w in log.wins] == ["worked"]
    assert [f.title for f in log.findings] == ["a", "b", "c"]
    assert log.counts() == {"blocker": 1, "workaround": 0, "note": 2}


@pytest.mark.parametrize(
    "kwargs",
    [
        {"title": "x", "severity": "catastrophic"},
        {"title": "x", "bucket": "someone-elses"},
        {"title": "x", "kind": "rumour"},
        {"title": "x", "kind": "impression", "sentiment": "grumpy"},
        {"title": "   "},
    ],
)
def test_invalid_entries_are_rejected_at_construction(kwargs):
    with pytest.raises(ValueError):
        Finding(**kwargs)


def test_created_timestamp_is_stamped_once(booley_dir):
    finding = append(Finding(title="x"), booley_dir)
    assert finding.created
    # Round-tripping must not restamp it — the log records when it happened.
    assert read_log(booley_dir).entries[0].created == finding.created


def test_rewrite_refuses_to_drop_unparseable_raw_evidence(booley_dir):
    append(Finding(title="good"), booley_dir)
    path = log_path(booley_dir)
    with path.open("a", encoding="utf-8") as stream:
        stream.write("{malformed but valuable evidence\n")
    before = path.read_bytes()

    with pytest.raises(CorruptFindingsLogError, match="would be lost"):
        rewrite(lambda _entries: None, booley_dir)

    assert path.read_bytes() == before


_EVIDENCE = {"repro": "booley doctor", "observed": "FAIL", "expected": "PASS"}
_FRICTION = {
    "kind": "friction",
    "bucket": "booley",
    "component": "targets",
    "expected": "a line saying the filter matched nothing",
}


@pytest.mark.parametrize(
    ("kwargs", "filable", "missing"),
    [
        ({"bucket": "booley"}, False, ["repro", "observed", "expected"]),
        ({"bucket": "booley", **_EVIDENCE}, True, []),
        ({"bucket": "booley", "repro": "booley doctor", "observed": "FAIL"}, False, ["expected"]),
        # Docs findings need a file and an observation, not a command.
        ({"bucket": "docs", "component": "docs/user/CONFIG.md", "observed": "x"}, True, []),
        ({"bucket": "docs", "observed": "it is wrong"}, False, ["component"]),
        ({"bucket": "docs"}, False, None),
        # Nothing goes upstream that nobody decided was Booley's fault.
        ({"bucket": "project", **_EVIDENCE}, False, None),
        ({"bucket": "unknown", **_EVIDENCE}, False, None),
        ({"kind": "win", "bucket": "booley"}, False, None),
        # An opinion has no evidence bar.
        ({"kind": "impression", "bucket": "booley"}, True, []),
        # Friction clears a different bar: no repro, but somewhere to aim the fix.
        (_FRICTION, True, []),
        ({**_FRICTION, "component": ""}, False, ["component"]),
        ({**_FRICTION, "expected": ""}, False, ["expected"]),
        ({**_FRICTION, "expected": "", "notes": "I looked for ten minutes"}, True, []),
        ({**_FRICTION, "component": "", "exposed_by": "booley targets"}, True, []),
    ],
    ids=[
        "no-evidence",
        "full-evidence",
        "partial-evidence",
        "docs-file-and-observation",
        "docs-without-file",
        "docs-empty",
        "users-own-problem",
        "untriaged",
        "win",
        "impression",
        "friction",
        "friction-without-component",
        "friction-without-expected",
        "friction-notes-stand-in-for-expected",
        "friction-exposed-by-stands-in-for-component",
    ],
)
def test_the_evidence_gate(kwargs, filable, missing):
    """The gate that keeps the issue tracker usable."""
    finding = Finding(title="a finding", **kwargs)
    assert finding.is_filable() is filable
    if missing is not None:
        assert finding.missing_evidence() == missing


# Spawned workers use the same first parsed read in both the old and fixed APIs.
def _publication_barrier(connection, publish):
    def paused_publish(*args, **kwargs):
        connection.send("read")
        assert connection.poll(_WAIT_S), "parent did not release publication"
        assert connection.recv() == "continue"
        return publish(*args, **kwargs)

    return paused_publish


def _run_publication_worker(directory, connection, operation):
    """Pause a materializing writer just before it publishes the rewritten log."""
    from booley.feedback import materialize

    # The race needs a pause between the parsed read and publication; no public hook exists there.
    materialize._rewrite_attachments_only = _publication_barrier(
        connection, materialize._rewrite_attachments_only
    )
    if operation == "cleanup_apply":
        from booley.harness.setup import run_root_cleanup

        run_root_cleanup.apply(directory.parent, "race")
    else:
        materialize.materialize_attachments(directory, [directory / "capture.log"])


def _paused_feedback_writer(directory, connection, operation, attempted):
    import argparse

    from booley.feedback import cli, findings, materialize
    from booley.harness import feedback_view

    # No public hook lengthens the lock wait for a race worker.
    findings._LOCK_TIMEOUT_S = _WAIT_S
    original = findings.read_log
    wait_for_lock = getattr(findings, "wait_for_file_lock", None)
    if wait_for_lock is not None and attempted is not None:

        def record_wait(handle, **kwargs):
            return wait_for_lock(handle, **kwargs, on_wait=attempted.set)

        findings.wait_for_file_lock = record_wait
    first = True

    def paused_read(*args, **kwargs):
        nonlocal first
        log = original(*args, **kwargs)
        if attempted is not None:
            attempted.set()
        if first and operation not in ("materialize", "cleanup_apply"):
            first = False
            connection.send("read")
            assert connection.poll(_WAIT_S), "parent did not release writer"
            assert connection.recv() == "continue"
        return log

    findings.read_log = paused_read
    cli.read_log = paused_read
    materialize.read_log = paused_read
    directory = Path(directory)
    if operation == "append":
        findings.append(findings.Finding(title="concurrent"), directory)
    elif operation in ("triage", "triage_attach"):
        parser = argparse.ArgumentParser()
        cli.add_subparser(parser.add_subparsers(dest="command"))
        command = ["feedback", "triage", "F-1", "--bucket", "booley"]
        if operation == "triage_attach":
            command += ["--attach", str(directory / "late.log")]
        args = parser.parse_args(command)
        assert cli.run(args, directory.parent, list_view=feedback_view.render_listing) == 0
    else:
        _run_publication_worker(directory, connection, operation)
    connection.send("done")


def _sidecar_is_held(directory):
    from booley.core.file_lock import (
        LockContentionError,
        acquire_file_lock,
        release_file_lock,
    )

    sidecar = directory / ".runtime" / "findings.lock"
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    with sidecar.open("a+") as handle:
        try:
            acquire_file_lock(handle)
        except LockContentionError:
            return True
        release_file_lock(handle)
    return False


def _receive(connection, expected):
    assert connection.poll(_WAIT_S), f"worker did not send {expected}"
    assert connection.recv() == expected


def _finish_workers(workers):
    for process, connection in workers:
        process.join(_WAIT_S)
        assert process.exitcode == 0
        connection.close()


def _stop_workers(workers):
    for process, connection in workers:
        if process.is_alive():
            process.terminate()
        process.join(hang_guard_s())
        connection.close()


def _start_writer(context, directory, operation, attempted=None):
    parent, child = context.Pipe()
    process = context.Process(
        target=_paused_feedback_writer, args=(str(directory), child, operation, attempted)
    )
    process.start()
    child.close()
    return process, parent


def test_concurrent_append_allocates_distinct_ids(booley_dir):
    context = multiprocessing.get_context("spawn")
    workers = []
    try:
        for _ in range(8):
            workers.append(_start_writer(context, booley_dir, "append"))
        # A locked reader excludes the other seven; the old API admits all eight.
        from multiprocessing.connection import wait

        ready = wait([connection for _, connection in workers], timeout=_WAIT_S)
        assert ready
        first = ready[0]
        _receive(first, "read")
        serialized = _sidecar_is_held(booley_dir)
        if serialized:
            first.send("continue")
            _receive(first, "done")
            remaining = [connection for _, connection in workers if connection is not first]
            for _ in range(7):
                ready = wait(remaining, timeout=_WAIT_S)
                assert ready
                connection = ready[0]
                _receive(connection, "read")
                connection.send("continue")
                _receive(connection, "done")
                remaining.remove(connection)
        else:
            for _, connection in workers:
                if connection is not first:
                    _receive(connection, "read")
            for _, connection in workers:
                connection.send("continue")
            for _, connection in workers:
                _receive(connection, "done")
        _finish_workers(workers)
        log = read_log(booley_dir)
        assert log.corrupt_lines == 0
        assert len(log.entries) == 8
        assert len({entry.id for entry in log.entries}) == 8
    finally:
        _stop_workers(workers)


def _race_feedback_append(directory, operation, addition_operation="append"):
    context = multiprocessing.get_context("spawn")
    workers = []
    try:
        writer = _start_writer(context, directory, operation)
        workers.append(writer)
        _receive(writer[1], "read")
        held = _sidecar_is_held(directory)
        attempted = context.Event()
        addition = _start_writer(context, directory, addition_operation, attempted)
        workers.append(addition)
        assert attempted.wait(_WAIT_S), "appender did not attempt the transaction"
        assert _sidecar_is_held(directory) == held
        if held:
            # Probe proves the writer really owns the shared sidecar, then let
            # it publish before waiting for the blocked appender's parsed read.
            writer[1].send("continue")
            _receive(writer[1], "done")
        _receive(addition[1], "read")
        addition[1].send("continue")
        _receive(addition[1], "done")
        if not held:
            writer[1].send("continue")
            _receive(writer[1], "done")
        _finish_workers(workers)
    finally:
        _stop_workers(workers)


def test_triage_retains_concurrent_append(booley_dir):
    append(Finding(title="original"), booley_dir)
    _race_feedback_append(booley_dir, "triage")
    log = read_log(booley_dir)
    assert [entry.title for entry in log.entries] == ["original", "concurrent"]
    assert log.entries[0].bucket == "booley"
    assert log.corrupt_lines == 0


def _hold_findings_lock(directory, connection):
    from booley.core.file_lock import acquire_file_lock, release_file_lock

    sidecar = Path(directory) / ".runtime" / "findings.lock"
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    with sidecar.open("a+") as handle:
        acquire_file_lock(handle)
        connection.send("held")
        try:
            assert connection.poll(_WAIT_S), "parent did not release holder"
            connection.recv()
        finally:
            release_file_lock(handle)


@contextmanager
def holding_findings_lock(directory):
    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe()
    process = context.Process(target=_hold_findings_lock, args=(str(directory), child))
    process.start()
    child.close()
    try:
        _receive(parent, "held")
        yield process
    finally:
        if process.is_alive():
            parent.send("release")
            process.join(hang_guard_s())
        _stop_workers([(process, parent)])


def test_timeout_changes_neither_log_nor_caller_and_killed_holder_recovers(
    booley_dir, lock_wait_expires
):
    from booley.feedback import findings

    append(Finding(title="original"), booley_dir)
    path = log_path(booley_dir)
    before = path.read_bytes()
    candidate = Finding(title="waiting")
    with holding_findings_lock(booley_dir) as holder:
        with pytest.raises(findings.FindingsLockTimeoutError, match=r"append.*findings.lock"):
            append(candidate, booley_dir)
        with pytest.raises(findings.FindingsLockTimeoutError, match=r"rewrite.*findings.lock"):
            rewrite(lambda entries: entries.clear(), booley_dir)
        assert candidate.id == ""
        assert path.read_bytes() == before
        assert not path.with_suffix(".jsonl.tmp").exists()
        holder.terminate()
        holder.join(hang_guard_s())
        assert not holder.is_alive()
        assert append(candidate, booley_dir).id == "F-2"
    assert (booley_dir / ".runtime" / "findings.lock").is_file()


def test_explicit_id_collision_refuses_without_writing(booley_dir):
    append(Finding(title="original", id="custom"), booley_dir)
    before = log_path(booley_dir).read_bytes()
    with pytest.raises(ValueError, match=r"custom.*already exists"):
        append(Finding(title="duplicate", id="custom"), booley_dir)
    assert log_path(booley_dir).read_bytes() == before


def test_rewrite_preserves_unknown_json_and_latest_rows(booley_dir):
    path = log_path(booley_dir)
    path.write_text('{"title":"future","id":"F-1","future":{"nested":[1,2]}}\n\n')
    append(Finding(title="new"), booley_dir)
    rewrite(lambda entries: setattr(entries[0], "bucket", "booley"), booley_dir)
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert rows[0]["future"] == {"nested": [1, 2]}
    assert rows[0]["bucket"] == "booley"
    assert rows[1]["title"] == "new"
    assert rows[1]["bucket"] == "unknown"
    # No temp file left behind for the next reader to trip over.
    assert not list(booley_dir.glob("*.tmp"))


@pytest.mark.parametrize("mutation", ["add", "remove", "reverse", "replace", "id"])
def test_rewrite_rejects_structural_callback_edits(booley_dir, mutation):
    append(Finding(title="one"), booley_dir)
    append(Finding(title="two"), booley_dir)
    path = log_path(booley_dir)
    before = path.read_bytes()

    def update(entries):
        if mutation == "add":
            entries.append(Finding(title="extra"))
        elif mutation == "remove":
            entries.pop()
        elif mutation == "reverse":
            entries.reverse()
        elif mutation == "replace":
            entries[0] = Finding.from_dict(json.loads(entries[0].to_json()))
        else:
            entries[0].id = "F-99"

    with pytest.raises(ValueError, match="preserve rows"):
        rewrite(update, booley_dir)
    assert path.read_bytes() == before
    assert not path.with_suffix(".jsonl.tmp").exists()
    assert append(Finding(title="after failure"), booley_dir).id == "F-3"


def test_rewrite_callback_exception_and_replace_failure_release_lock(booley_dir, monkeypatch):
    append(Finding(title="original"), booley_dir)
    path = log_path(booley_dir)
    before = path.read_bytes()

    def failed_update(entries):
        entries[0].notes = "must not publish"
        raise ValueError("invalid batch")

    with pytest.raises(ValueError, match="invalid batch"):
        rewrite(failed_update, booley_dir)
    assert path.read_bytes() == before
    assert not path.with_suffix(".jsonl.tmp").exists()
    original_replace = Path.replace

    def failed_replace(source, target):
        if source == path.with_suffix(".jsonl.tmp"):
            raise OSError("publication interrupted")
        return original_replace(source, target)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "replace", failed_replace)
        with pytest.raises(OSError, match="publication interrupted"):
            rewrite(lambda entries: setattr(entries[0], "notes", "new"), booley_dir)
    assert path.read_bytes() == before
    assert path.with_suffix(".jsonl.tmp").exists()
    rewrite(lambda entries: setattr(entries[0], "notes", "retry"), booley_dir)
    assert read_log(booley_dir).entries[0].notes == "retry"
    assert not path.with_suffix(".jsonl.tmp").exists()


def test_lock_io_failure_is_not_misreported_as_timeout_or_written(booley_dir, monkeypatch):
    from booley.feedback import findings

    append(Finding(title="original"), booley_dir)
    before = log_path(booley_dir).read_bytes()
    candidate = Finding(title="not written")

    def failed_lock(*_args, **_kwargs):
        raise OSError("storage failure")

    monkeypatch.setattr(findings, "wait_for_file_lock", failed_lock)
    with pytest.raises(OSError, match="storage failure") as error:
        append(candidate, booley_dir)
    assert not isinstance(error.value, findings.FindingsLockTimeoutError)
    assert candidate.id == ""
    assert log_path(booley_dir).read_bytes() == before
