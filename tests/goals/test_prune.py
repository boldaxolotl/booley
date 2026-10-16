"""Cleanup leaves project history and shared worktree locks intact."""
# ruff: noqa: F811 — pytest fixture imports intentionally share parameter names.

import pytest

from booley.goals.paths import record_paths
from booley.goals.store import GoalStore
from tests.goals.conftest import layout  # noqa: F401 — shared fixture
from tests.goals.test_inventory import remove_checkout, terminal
from tests.goals.test_store import repo  # noqa: F401 — shared fixture


def test_prune_preserves_history_and_shared_locks(repo):
    from booley.goals.prune import prune_record

    record = terminal(repo)
    remove_checkout(repo)
    lock = repo["project"] / "goals/locks/worktree-other.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_bytes(b"keep")
    history = repo["project"] / "goals/history/keep.md"
    history.parent.mkdir()
    history.write_bytes(b"keep")
    prune_record(GoalStore(repo["project"]), repo["main"], record.id, confirm=True)
    assert lock.read_bytes() == history.read_bytes() == b"keep"


def test_prune_refusal_matrix_and_external_child_link(repo, monkeypatch, tmp_path):
    import json

    import pytest

    from booley.goals import prune
    from booley.goals.model import GoalState
    from booley.goals.paths import record_paths
    from booley.goals.store import GoalStoreError

    record = terminal(repo)
    store = GoalStore(repo["project"])
    path = record_paths(store.booley_dir, record.id).record_file
    original = path.read_bytes()
    remove_checkout(repo)
    for state in (GoalState.ENTERING, GoalState.ACTIVE, GoalState.FINISHING):
        data = json.loads(original)
        data["state"] = state.value
        path.write_text(json.dumps(data))
        with pytest.raises(GoalStoreError):
            prune.prune_record(store, repo["main"], record.id, confirm=True)
    path.write_bytes(original)
    jobs = path.parent / ".runtime/jobs"
    jobs.mkdir(parents=True)
    (jobs / "broken.json").write_bytes(b"bad")
    with pytest.raises(GoalStoreError, match="Jobs"):
        prune.prune_record(store, repo["main"], record.id, confirm=True)
    (jobs / "broken.json").unlink()
    original_presence = prune.presence
    for observed in [("missing", "present"), ("present", "missing"), ("unavailable", "missing")]:
        monkeypatch.setattr(prune, "presence", lambda *_, observed=observed: observed)
        with pytest.raises(GoalStoreError):
            prune.prune_record(store, repo["main"], record.id, confirm=True)
    monkeypatch.setattr(prune, "presence", original_presence)
    target = tmp_path / "keep"
    target.mkdir()
    (target / "data").write_text("keep")
    (path.parent / "external").symlink_to(target, target_is_directory=True)
    prune.prune_record(store, repo["main"], record.id, confirm=True)
    assert (target / "data").read_text() == "keep"


def test_prune_waiting_proposal_writer_cannot_recreate_removed_root(repo, monkeypatch):
    import threading
    from uuid import uuid4

    from booley.goals import store as store_module
    from booley.goals.changes import ChangeKind
    from booley.goals.model import GoalFamily, GoalSpec
    from booley.goals.paths import record_paths
    from booley.goals.proposals import Proposal, save_new
    from booley.goals.prune import prune_record

    record = terminal(repo)
    remove_checkout(repo)
    store = GoalStore(repo["project"])
    spec = GoalSpec("lint_clean_top", GoalFamily.LINT, "top", {}, ("ad-hoc",))
    proposal = Proposal(
        str(uuid4()),
        record.id,
        record.worktree,
        ChangeKind.ADD,
        spec.key,
        None,
        spec,
        {"family": GoalFamily.LINT.value, "target": "top"},
        {},
        "test",
        1,
        None,
        record.entered_at,
    )
    Proposal.from_json(proposal.to_json())
    opened = threading.Event()
    resume = threading.Event()
    errors = []
    original = store_module.wait_for_file_lock

    def wait(handle, **kwargs):
        if threading.current_thread().name == "proposal-writer":
            opened.set()
            assert resume.wait(10)
        return original(handle, **kwargs)

    monkeypatch.setattr(store_module, "wait_for_file_lock", wait)

    def writer():
        try:
            with store.record_lock(record.id) as lock:
                save_new(lock, proposal)
        except store_module.GoalStoreError as exc:
            errors.append(exc)

    thread = threading.Thread(target=writer, name="proposal-writer")
    thread.start()
    assert opened.wait(10)
    try:
        prune_record(store, repo["main"], record.id, confirm=True)
    finally:
        resume.set()
        thread.join(10)
    assert not thread.is_alive() and errors
    assert not record_paths(store.booley_dir, record.id).root.exists()


@pytest.mark.parametrize("boundary", ["intent", "rename", "delete", "clear"])
def test_prune_retries_each_durable_boundary(repo, monkeypatch, boundary):
    from pathlib import Path

    from booley.goals import prune
    from booley.goals.paths import pruning_intent, pruning_root, record_paths

    class Crash(BaseException):
        pass

    record = terminal(repo)
    remove_checkout(repo)
    store = GoalStore(repo["project"])
    marker = pruning_intent(store.booley_dir, record.id)
    quarantine = pruning_root(store.booley_dir, record.id)
    with monkeypatch.context() as patch:
        if boundary == "intent":
            original = prune.atomic_write_once

            def stop(*args, **kwargs):
                original(*args, **kwargs)
                raise Crash

            patch.setattr(prune, "atomic_write_once", stop)
        elif boundary == "rename":
            original = Path.rename

            def stop(path, target):
                result = original(path, target)
                if target == quarantine:
                    raise Crash
                return result

            patch.setattr(Path, "rename", stop)
        elif boundary == "delete":

            def stop(path):
                (path / "record.json").unlink()
                raise Crash

            patch.setattr(prune, "_remove_tree", stop)
        else:
            patch.setattr(prune, "_clear_intent", lambda *_: (_ for _ in ()).throw(Crash()))
        with pytest.raises(Crash):
            prune.prune_record(store, repo["main"], record.id, confirm=True)
    assert marker.exists()
    before = marker.read_bytes()
    assert prune.prune_record(store, repo["main"], record.id)["pending"]
    assert marker.read_bytes() == before
    prune.prune_record(store, repo["main"], record.id, confirm=True)
    assert not marker.exists() and not quarantine.exists()
    assert not record_paths(store.booley_dir, record.id).root.exists()


def test_prune_intent_retry_restores_access_when_eligibility_is_lost(repo, monkeypatch):
    from booley.goals import prune
    from booley.goals.paths import pruning_intent
    from booley.goals.store import GoalStoreError
    from tests.goals.test_inventory import snapshot
    from tests.goals.test_store import _git

    class Crash(BaseException):
        pass

    record = terminal(repo)
    remove_checkout(repo)
    store = GoalStore(repo["project"])
    before = snapshot(repo["project"])
    original = prune.atomic_write_once

    def stop(*args, **kwargs):
        original(*args, **kwargs)
        raise Crash

    with monkeypatch.context() as patch:
        patch.setattr(prune, "atomic_write_once", stop)
        with pytest.raises(Crash):
            prune.prune_record(store, repo["main"], record.id, confirm=True)
    _git(repo["main"], "branch", record.branch)
    with pytest.raises(GoalStoreError):
        prune.prune_record(store, repo["main"], record.id, confirm=True)
    assert not pruning_intent(store.booley_dir, record.id).exists()
    assert store.load(record.id).id == record.id
    assert {
        k: v for k, v in snapshot(repo["project"]).items() if not k.endswith("prune.lock")
    } == before


def _failed_campaign(layout):
    import json

    from booley.flows.campaign_results import StartedCampaign
    from booley.goals.recorder import GoalEvidenceRecorder
    from tests.goals.conftest import bind, enter_goals, git

    layout.record = enter_goals(layout, [{"family": "SIM", "target": "top"}])
    recorder = GoalEvidenceRecorder(bind(layout))
    record_id = layout.record.id
    path = record_paths(layout.control, record_id).record_file
    data = json.loads(path.read_bytes())
    data["state"] = "failed"
    path.write_text(json.dumps(data))
    git(layout.main, "worktree", "remove", "--force", str(layout.worktree))
    git(layout.main, "branch", "-D", layout.record.branch)
    campaign = StartedCampaign(
        layout.control / "manifest.json",
        "22222222-2222-4222-8222-222222222222",
        "sha256:" + "c" * 64,
        ("top",),
        True,
    )
    return recorder, campaign, path


def test_prune_waiting_campaign_registration_cannot_recreate_root(layout, monkeypatch):
    import threading

    from booley.flows.execution_persistence import AcceptanceRecordingError
    from booley.goals import store as store_module
    from booley.goals.prune import prune_record

    recorder, campaign, path = _failed_campaign(layout)
    record_id = layout.record.id
    opened = threading.Event()
    resume = threading.Event()
    errors = []
    original = store_module.wait_for_file_lock

    def wait(handle, **kwargs):
        if threading.current_thread().name == "campaign-writer":
            opened.set()
            assert resume.wait(10)
        return original(handle, **kwargs)

    monkeypatch.setattr(store_module, "wait_for_file_lock", wait)

    def writer():
        try:
            recorder.register_campaign(campaign)
        except AcceptanceRecordingError as exc:
            errors.append(exc)

    thread = threading.Thread(target=writer, name="campaign-writer")
    thread.start()
    assert opened.wait(10)
    try:
        prune_record(GoalStore(layout.control), layout.main, record_id, confirm=True)
    finally:
        resume.set()
        thread.join(10)
    assert not thread.is_alive()
    assert not path.parent.exists()


def test_prune_refuses_live_jobs_but_not_stale_running_label(repo):
    import os
    from dataclasses import replace

    from booley.goals.prune import prune_record
    from booley.goals.store import GoalStoreError
    from booley.runtime import job_records
    from booley.runtime.timefmt import utc_now_rfc3339

    record = terminal(repo)
    remove_checkout(repo)
    store = GoalStore(repo["project"])
    jobs = record_paths(store.booley_dir, record.id).jobs_dir
    job = job_records.JobRecord("live-job", "sim", utc_now_rfc3339(), 60, pid=os.getpid())
    job_records.write_record(job, jobs)
    with pytest.raises(GoalStoreError, match="active record-owned Jobs"):
        prune_record(store, repo["main"], record.id, confirm=True)
    from booley.runtime.pid import REUSED, capture_process_identity, observe_process

    identity = capture_process_identity(os.getpid())
    assert identity is not None
    reused = replace(identity, start_token=identity.start_token + 1)
    assert observe_process(reused).state == REUSED
    job_records.write_record(
        replace(job, process_identity=reused.to_payload(), started_at="2020-01-01T00:00:00Z"), jobs
    )
    prune_record(store, repo["main"], record.id, confirm=True)
    assert not jobs.parent.parent.exists()


def test_prune_refuses_expired_job_with_live_writer_identity(repo):
    import subprocess
    import sys

    from booley.goals.paths import pruning_intent
    from booley.goals.prune import prune_record
    from booley.goals.store import GoalStoreError
    from booley.runtime import job_records
    from booley.runtime.pid import capture_process_identity
    from tests.goals.test_inventory import snapshot

    record = terminal(repo)
    remove_checkout(repo)
    store = GoalStore(repo["project"])
    paths = record_paths(store.booley_dir, record.id)
    release = repo["main"] / "release-producer"
    report = paths.runtime_dir / "flow-reports/late/report.json"
    script = (
        "import time\nfrom pathlib import Path\n"
        f"while not Path({str(release)!r}).exists(): time.sleep(0.01)\n"
        f"p = Path({str(report)!r})\np.parent.mkdir(parents=True, exist_ok=True)\n"
        "p.write_text('{}')\n"
    )
    child = subprocess.Popen([sys.executable, "-c", script], start_new_session=True)
    try:
        identity = capture_process_identity(child.pid)
        assert identity is not None
        job = job_records.JobRecord(
            "expired",
            "sim",
            "2000-01-01T00:00:00Z",
            1,
            pid=child.pid,
            process_identity=identity.to_payload(),
        )
        job_records.write_record(job, paths.jobs_dir)
        before = snapshot(paths.root)
        with pytest.raises(GoalStoreError, match="writer"):
            prune_record(store, repo["main"], record.id, confirm=True)
        assert snapshot(paths.root) == before
        assert not pruning_intent(store.booley_dir, record.id).exists()
        release.touch()
        child.wait(timeout=5)
        assert report.is_file()  # a producer really remained able to mkdir/write
    finally:
        child.terminate()
        child.wait(timeout=5)
    if sys.platform == "win32":
        with pytest.raises(GoalStoreError, match="writer group"):
            prune_record(store, repo["main"], record.id, confirm=True)
    else:
        prune_record(store, repo["main"], record.id, confirm=True)
        assert not paths.root.exists()


def test_prune_refuses_owned_descendant_after_leader_exit(repo, tmp_path):
    import contextlib
    import os
    import signal
    import subprocess
    import sys
    import time

    from booley.goals.prune import prune_record
    from booley.goals.store import GoalStoreError
    from booley.runtime import job_records
    from booley.runtime.pid import capture_process_identity, is_pid_alive

    if sys.platform == "win32":
        pytest.skip("POSIX owned process-group observation")
    record = terminal(repo)
    remove_checkout(repo)
    store = GoalStore(repo["project"])
    paths = record_paths(store.booley_dir, record.id)
    ready, release = tmp_path / "ready", tmp_path / "release"
    descendant_script = (
        f"import time; from pathlib import Path; Path({str(ready)!r}).touch(); time.sleep(30)"
    )
    script = (
        "import subprocess,sys,time\nfrom pathlib import Path\n"
        f"subprocess.Popen([sys.executable,'-c',{descendant_script!r}])\n"
        f"while not Path({str(release)!r}).exists(): time.sleep(0.01)\n"
    )
    child = subprocess.Popen([sys.executable, "-c", script], start_new_session=True)
    try:
        identity = capture_process_identity(child.pid)
        assert identity is not None
        deadline = time.monotonic() + 5
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert ready.exists()
        release.touch()
        child.wait(timeout=5)
        assert not is_pid_alive(child.pid)
        job_records.write_record(
            job_records.JobRecord(
                "descendant",
                "sim",
                "2000-01-01T00:00:00Z",
                1,
                pid=child.pid,
                process_identity=identity.to_payload(),
            ),
            paths.jobs_dir,
        )
        with pytest.raises(GoalStoreError, match="writer group"):
            prune_record(store, repo["main"], record.id, confirm=True)
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(child.pid, signal.SIGKILL)
        child.wait(timeout=5)
