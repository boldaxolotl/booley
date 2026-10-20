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


@pytest.fixture
def booley_dir(tmp_path):
    d = tmp_path / ".booley"
    d.mkdir()
    return d


def test_append_assigns_sequential_ids(booley_dir):
    for expected in ("F-1", "F-2", "F-3"):
        assert append(Finding(title="x"), booley_dir).id == expected


def test_an_impression_is_filable_with_no_evidence_at_all(booley_dir):
    """The evidence bar that protects the bug queue must not gate opinions."""
    entry = append(
        Finding(title="Booley is not worth the setup cost", kind="impression", bucket="booley"),
        booley_dir,
    )
    assert entry.missing_evidence() == []
    assert entry.is_filable()


def test_an_impression_is_not_counted_as_a_bug(booley_dir):
    append(Finding(title="great framework", kind="impression", bucket="booley"), booley_dir)
    log = read_log(booley_dir)
    assert log.impressions and not log.bugs
    assert log.counts() == {"blocker": 0, "workaround": 0, "note": 0}


def test_sentiment_is_validated(booley_dir):
    with pytest.raises(ValueError, match="sentiment"):
        Finding(title="x", kind="impression", sentiment="grumpy")


def test_append_continues_numbering_across_processes(booley_dir):
    """A fresh process must not restart at F-1 and collide with the log."""
    append(Finding(title="a"), booley_dir)
    append(Finding(title="b"), booley_dir)
    # Simulate a separate invocation: nothing cached, ids come from the file.
    assert append(Finding(title="c"), booley_dir).id == "F-3"


def test_append_creates_the_booley_dir(tmp_path):
    """Findings must be loggable before setup has finished building the dir."""
    missing = tmp_path / "nope" / ".booley"
    append(Finding(title="early"), missing)
    assert log_path(missing).is_file()


def test_read_log_of_missing_file_is_empty_not_an_error(booley_dir):
    log = read_log(booley_dir)
    assert log.entries == []
    assert log.corrupt_lines == 0


def test_corrupt_line_costs_one_finding_not_the_log(booley_dir):
    append(Finding(title="good one"), booley_dir)
    with log_path(booley_dir).open("a", encoding="utf-8") as fh:
        fh.write("{not json at all\n")
    append(Finding(title="another good one"), booley_dir)

    log = read_log(booley_dir)
    assert [e.title for e in log.entries] == ["good one", "another good one"]
    assert log.corrupt_lines == 1


def test_blank_lines_are_ignored_silently(booley_dir):
    append(Finding(title="x"), booley_dir)
    with log_path(booley_dir).open("a", encoding="utf-8") as fh:
        fh.write("\n   \n")
    log = read_log(booley_dir)
    assert len(log.entries) == 1
    assert log.corrupt_lines == 0


def test_unknown_fields_from_a_newer_booley_are_dropped_not_fatal(booley_dir):
    """Forward compatibility: a later version's extra field must still render."""
    with log_path(booley_dir).open("w", encoding="utf-8") as fh:
        fh.write(json.dumps({"title": "from the future", "invented_field": 42}) + "\n")
    log = read_log(booley_dir)
    assert [e.title for e in log.entries] == ["from the future"]


def test_wins_and_findings_are_separated(booley_dir):
    append(Finding(title="broke"), booley_dir)
    append(Finding(title="worked", kind="win"), booley_dir)
    log = read_log(booley_dir)
    assert [f.title for f in log.findings] == ["broke"]
    assert [w.title for w in log.wins] == ["worked"]


def test_counts_are_per_severity(booley_dir):
    append(Finding(title="a", severity="blocker"), booley_dir)
    append(Finding(title="b", severity="note"), booley_dir)
    append(Finding(title="c", severity="note"), booley_dir)
    append(Finding(title="w", kind="win"), booley_dir)  # must not be counted
    assert read_log(booley_dir).counts() == {"blocker": 1, "workaround": 0, "note": 2}


@pytest.mark.parametrize(
    "kwargs",
    [
        {"title": "x", "severity": "catastrophic"},
        {"title": "x", "bucket": "someone-elses"},
        {"title": "x", "kind": "rumour"},
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


def test_rewrite_replaces_the_log_atomically(booley_dir):
    append(Finding(title="a"), booley_dir)
    append(Finding(title="b"), booley_dir)
    rewrite(lambda entries: setattr(entries[0], "bucket", "booley"), booley_dir)

    reread = read_log(booley_dir)
    assert [e.bucket for e in reread.entries] == ["booley", "unknown"]
    # No temp file left behind for the next reader to trip over.
    assert not list(booley_dir.glob("*.tmp"))


def test_rewrite_refuses_to_drop_unparseable_raw_evidence(booley_dir):
    append(Finding(title="good"), booley_dir)
    path = log_path(booley_dir)
    with path.open("a", encoding="utf-8") as stream:
        stream.write("{malformed but valuable evidence\n")
    before = path.read_bytes()

    with pytest.raises(CorruptFindingsLogError, match="would be lost"):
        rewrite(lambda _entries: None, booley_dir)

    assert path.read_bytes() == before


class TestFilability:
    """The evidence gate. This is what keeps the issue tracker usable."""

    def test_impression_without_evidence_is_not_filable(self):
        finding = Finding(title="the output was confusing", bucket="booley")
        assert not finding.is_filable()
        assert finding.missing_evidence() == ["repro", "observed", "expected"]

    def test_full_evidence_is_filable(self):
        finding = Finding(
            title="doctor fails on valid config",
            bucket="booley",
            repro="booley doctor",
            observed="FAIL",
            expected="PASS",
        )
        assert finding.is_filable()
        assert finding.missing_evidence() == []

    def test_partial_evidence_is_not_filable(self):
        finding = Finding(title="x", bucket="booley", repro="booley doctor", observed="FAIL")
        assert not finding.is_filable()
        assert finding.missing_evidence() == ["expected"]

    def test_docs_findings_need_no_reproduction(self):
        """ "The doc says X, the code does Y" is actionable without a command."""
        finding = Finding(
            title="CONFIG.md documents a removed table",
            bucket="docs",
            component="docs/user/CONFIG.md",
            observed="describes [sources.rtl], which no longer exists",
        )
        assert finding.is_filable()

    def test_docs_finding_still_needs_a_component_and_observation(self):
        assert not Finding(title="docs are wrong somewhere", bucket="docs").is_filable()

    def test_the_users_own_problems_are_never_filable(self):
        finding = Finding(
            title="verilator missing from PATH",
            bucket="project",
            repro="verilator --version",
            observed="not found",
            expected="a version",
        )
        assert not finding.is_filable()

    def test_untriaged_findings_are_never_filable(self):
        """Nothing goes upstream that nobody decided was Booley's fault."""
        finding = Finding(title="x", bucket="unknown", repro="c", observed="o", expected="e")
        assert not finding.is_filable()

    def test_a_win_is_never_filable(self):
        assert not Finding(title="worked", kind="win", bucket="booley").is_filable()


# Spawned workers use the same first parsed read in both the old and fixed APIs.
def _publication_barrier(connection, publish):
    def paused_publish(*args, **kwargs):
        connection.send("read")
        assert connection.poll(60), "parent did not release publication"
        assert connection.recv() == "continue"
        return publish(*args, **kwargs)

    return paused_publish


def _paused_feedback_writer(directory, connection, operation, attempted):
    import argparse

    from booley.feedback import cli, findings, materialize

    findings._LOCK_TIMEOUT_S = 60
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
        if first and operation != "materialize":
            first = False
            connection.send("read")
            assert connection.poll(60), "parent did not release writer"
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
        assert cli._cmd_triage(args, directory) == 0
    else:
        materialize._rewrite_attachments_only = _publication_barrier(
            connection, materialize._rewrite_attachments_only
        )
        materialize.materialize_attachments(directory, [directory / "capture.log"])
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
    assert connection.poll(60), f"worker did not send {expected}"
    assert connection.recv() == expected


def _finish_workers(workers):
    for process, connection in workers:
        process.join(60)
        assert process.exitcode == 0
        connection.close()


def _stop_workers(workers):
    for process, connection in workers:
        if process.is_alive():
            process.terminate()
        process.join(10)
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

        ready = wait([connection for _, connection in workers], timeout=60)
        assert ready
        first = ready[0]
        _receive(first, "read")
        serialized = _sidecar_is_held(booley_dir)
        if serialized:
            first.send("continue")
            _receive(first, "done")
            remaining = [connection for _, connection in workers if connection is not first]
            for _ in range(7):
                ready = wait(remaining, timeout=60)
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
        assert attempted.wait(60), "appender did not attempt the transaction"
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
            assert connection.poll(60), "parent did not release holder"
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
            process.join(10)
        _stop_workers([(process, parent)])


def test_timeout_changes_neither_log_nor_caller_and_killed_holder_recovers(
    booley_dir, monkeypatch
):
    from booley.feedback import findings

    append(Finding(title="original"), booley_dir)
    path = log_path(booley_dir)
    before = path.read_bytes()
    candidate = Finding(title="waiting")
    assert findings._LOCK_TIMEOUT_S == 5
    monkeypatch.setattr(findings, "_LOCK_TIMEOUT_S", 0.01)
    with holding_findings_lock(booley_dir) as holder:
        with pytest.raises(findings.FindingsLockTimeoutError, match=r"append.*findings.lock"):
            append(candidate, booley_dir)
        with pytest.raises(findings.FindingsLockTimeoutError, match=r"rewrite.*findings.lock"):
            rewrite(lambda entries: entries.clear(), booley_dir)
        assert candidate.id == ""
        assert path.read_bytes() == before
        assert not path.with_suffix(".jsonl.tmp").exists()
        holder.terminate()
        holder.join(10)
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
