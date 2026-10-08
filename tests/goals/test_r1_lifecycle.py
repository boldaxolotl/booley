"""Production regressions for independently verified lifecycle recovery defects."""

from __future__ import annotations

import json
import shutil
from dataclasses import replace
from pathlib import Path

import pytest

from booley.goals.abandon import abandon_goal
from booley.goals.finish import finish_goal
from booley.goals.lifecycle import LifecycleError
from booley.goals.model import GoalState
from booley.goals.store import GoalStore
from tests.conftest import symlink_or_skip
from tests.goals.conftest import enter_goals, git
from tests.goals.test_finish import Crash, environment, publication_layout, request
from tests.goals.test_finish import complete as _complete_fixture

complete = _complete_fixture
from tests.goals.test_status import publish

pytestmark = pytest.mark.timeout(120)


def stop_at(boundary):
    def stop(name):
        if name == boundary:
            raise Crash(name)

    return stop


def recreate(path):
    backup = path.with_name(path.name + "-original")
    path.rename(backup)
    shutil.copytree(backup, path, symlinks=True)
    return backup


def test_active_abandon_uses_stable_git_identity_but_finish_refuses_copied_inputs(complete):
    recreate(complete.worktree)
    with pytest.raises(LifecycleError, match="identity"):
        finish_goal(request(complete), environment(complete))
    result = abandon_goal(GoalStore(complete.control), complete.worktree, environment(complete))
    assert result["status"] == "abandoned"


def test_saved_exact_response_survives_record_and_checkout_recreation(complete):
    call = request(complete, explain_html=True)
    saved = finish_goal(call, environment(complete))
    record_dir = Path(saved["summary"]).parent.parent.parent
    recreate(record_dir)
    recreate(complete.worktree)
    assert finish_goal(call, environment(complete)) == saved


@pytest.mark.parametrize("changed", ["summary", "html"])
def test_renderer_upgrade_cannot_invalidate_frozen_response(complete, monkeypatch, changed):
    call = request(complete, explain_html=True)
    with pytest.raises(Crash):
        finish_goal(call, environment(complete, stop_at("finishing")))
    monkeypatch.setattr("booley.goals.finish.render_goal_briefing", lambda _: "new renderer")
    monkeypatch.setattr("booley.goals.finish_presentation.render_goal_html", lambda _: "new HTML")
    result = finish_goal(call, environment(complete))
    assert result["message"] != "new renderer"
    assert Path(result["html"]).read_text() != "new HTML"
    assert finish_goal(call, environment(complete)) == result


def test_private_protected_bytes_are_hash_only_in_all_public_artifacts(layout):
    marker = b"PRIVATE_CONTROL_MARKER_64977"
    hook = layout.control / "hooks" / "private.py"
    hook.parent.mkdir()
    hook.write_bytes(marker)
    layout.record = enter_goals(layout, [{"family": "lint", "target": "top"}])
    publish(layout)
    call = request(layout, explain_html=True)
    result = finish_goal(call, environment(layout))
    for text in (
        result["message"],
        Path(result["package"]).read_text(),
        Path(result["html"]).read_text(),
    ):
        assert marker.hex() not in text and marker.decode() not in text
    private = json.loads((Path(result["package"]).parent / "attempt.json").read_bytes())
    assert any(
        row.get("bytes") == marker.hex()
        for row in private["private_capture"]["nonversioned_observations"]
    )
    assert private["package"]["record"]["project_snapshot"] is None


def test_cache_only_protected_changes_do_not_revalidate_frozen_attempt(complete):
    call = request(complete)
    with pytest.raises(Crash):
        finish_goal(call, environment(complete, stop_at("finishing")))
    cache = complete.control / "mcp_tools" / "__pycache__"
    cache.mkdir()
    (cache / "tool.pyc").write_bytes(b"ephemeral cache")
    assert finish_goal(call, environment(complete))["status"] == "finished"


@pytest.mark.parametrize("abandon", [False, True])
def test_recovery_caller_retry_survives_prior_terminal_response_gap_and_reentry(complete, abandon):
    first = request(complete)
    with pytest.raises(Crash):
        finish_goal(first, environment(complete, stop_at("finishing")))
    recovery = (
        request(complete)
        if not abandon
        else replace(request(complete), abandon=True, instruction_quote="abandon")
    )
    with pytest.raises(Crash):
        finish_goal(recovery, environment(complete, stop_at("finished")))
    from booley.goals.entry import EntryEnvironment, EntryRequest, enter_goal_mode
    from booley.goals.model import parse_goal_args

    complete.record = enter_goal_mode(
        EntryRequest(
            complete.worktree,
            "replacement",
            parse_goal_args([{"family": "lint", "target": "top"}]),
        ),
        EntryEnvironment(complete.control),
    ).record
    occupant = GoalStore(complete.control).load(complete.record.id).to_json()
    result = finish_goal(recovery, environment(complete))
    assert (
        result["status"] == "finished" and result["recovered_operation_id"] == first.operation_id
    )
    assert GoalStore(complete.control).load(complete.record.id).to_json() == occupant


def test_old_owned_untracked_output_does_not_wedge_new_attempt_and_canonical_is_success(layout):
    complete = publication_layout(layout)
    first = request(complete)
    with pytest.raises(Crash):
        finish_goal(first, environment(complete, stop_at("destination-materialized")))
    git(complete.worktree, "commit", "--allow-empty", "-qm", "unrelated committed work")
    assert finish_goal(first, environment(complete))["status"] == "revalidation_required"
    old = complete.worktree / f".booley_project/goals/history/{complete.record.id}.md"
    original = old.read_bytes()
    second = request(complete)
    result = finish_goal(second, environment(complete))
    assert result["status"] == "finished" and old.read_bytes() == original
    package = json.loads(Path(result["package"]).read_bytes())
    assert package["earlier_attempts"][0]["operation_id"] == first.operation_id
    record_dir = Path(result["package"]).parent.parent.parent
    assert (record_dir / "review-package.json").read_bytes() == Path(
        result["package"]
    ).read_bytes()
    assert (record_dir / "SUMMARY.md").read_bytes() == Path(result["summary"]).read_bytes()
    assert (
        git(complete.worktree, "show", "--format=", "--name-only", "HEAD")
        == result["history_path"]
    )


def test_finishing_physical_substitution_allows_safe_abandon_without_publication(complete):
    call = request(complete)
    with pytest.raises(Crash):
        finish_goal(call, environment(complete, stop_at("finishing")))
    recreate(complete.worktree)
    before = git(complete.worktree, "rev-parse", "HEAD")
    result = abandon_goal(GoalStore(complete.control), complete.worktree, environment(complete))
    assert result["status"] == "abandoned"
    assert git(complete.worktree, "rev-parse", "HEAD") == before
    assert GoalStore(complete.control).load(complete.record.id).state is GoalState.ABANDONED


@pytest.mark.parametrize("boundary", ["ref-published", "index-synchronized", "finished"])
def test_existing_exact_publication_survives_physical_recreation(layout, boundary):
    complete = publication_layout(layout)
    call = request(complete)
    with pytest.raises(Crash):
        finish_goal(call, environment(complete, stop_at(boundary)))
    published = git(complete.worktree, "rev-parse", "HEAD")
    recreate(complete.worktree)
    result = finish_goal(call, environment(complete))
    assert result["status"] == "finished"
    assert git(complete.worktree, "rev-parse", "HEAD") == published


def test_physical_substitution_never_adopts_foreign_summary_or_ref(layout):
    complete = publication_layout(layout)
    call = request(complete)
    with pytest.raises(Crash):
        finish_goal(call, environment(complete, stop_at("ref-published")))
    recreate(complete.worktree)
    path = complete.worktree / f".booley_project/goals/history/{complete.record.id}.md"
    path.write_bytes(b"foreign summary")
    with pytest.raises(LifecycleError, match="destination conflicts"):
        finish_goal(call, environment(complete))
    assert GoalStore(complete.control).load(complete.record.id).state is GoalState.FINISHING


@pytest.mark.parametrize(
    "boundary",
    ["summary-staging-owned", "summary-linked", "index-staging-owned", "index-lock-acquired"],
)
def test_hard_process_death_recovers_only_durably_owned_publication_inodes(layout, boundary):
    import os
    import subprocess
    import sys

    complete = publication_layout(layout)
    call = request(complete)
    script = """import json, os, sys
from pathlib import Path
from booley.goals.entry import EntryEnvironment
from booley.goals.apply import ChangeEnvironment
from booley.goals.finish import FinishEnvironment, finish_goal
from booley.goals.lifecycle import LifecycleRequest
x=json.loads(sys.argv[1])
def die(name):
    if name == x['boundary']:
        os._exit(17)
request=LifecycleRequest(Path(x['work']),x['record'],x['operation'],summary=x['summary'])
finish_goal(request,FinishEnvironment(ChangeEnvironment(EntryEnvironment(Path(x['control']))),on_boundary=die))
raise AssertionError('did not reach death boundary')
"""
    args = {
        "work": str(complete.worktree),
        "control": str(complete.control),
        "record": call.record_id,
        "operation": call.operation_id,
        "summary": call.summary,
        "boundary": boundary,
    }
    child = subprocess.run(
        [sys.executable, "-c", script, json.dumps(args)],
        capture_output=True,
        env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[2] / "src")},
        timeout=90,
        check=False,
    )
    assert child.returncode == 17, child.stderr.decode()
    unrelated = complete.main / ".git/unrelated-staging"
    unrelated.write_bytes(b"user staging bytes")
    result = finish_goal(call, environment(complete))
    assert result["status"] == "finished"
    assert unrelated.read_bytes() == b"user staging bytes"
    assert not list((complete.worktree / ".booley_project/goals/history").glob(".*.tmp"))
    assert git(complete.worktree, "rev-list", "--count", "HEAD^..HEAD") == "1"


@pytest.mark.parametrize(
    "failure", ["intent-write", "terminal-save", "after-terminal", "after-closure"]
)
def test_abandonment_closure_requires_terminal_authority_and_recovers_crashes(
    layout, monkeypatch, failure
):
    from booley.goals.lifecycle import LifecycleOperation
    from booley.goals.proposals import load_proposal, proposal_path
    from tests.goals.test_proposals import proposal, setup_cycle

    changes = setup_cycle(layout)
    view = proposal(layout, changes)
    root = changes.store.load(layout.record.id)
    path = proposal_path(layout.control / "goals" / root.id, view.proposal.id) / "state.json"
    before = path.read_bytes()
    call = replace(request(layout), abandon=True, instruction_quote="stop")
    if failure == "intent-write":
        original = LifecycleOperation.write_sealed

        def fail(self, name, value):
            if name == "abandonment":
                raise OSError("owned intent write failed")
            return original(self, name, value)

        monkeypatch.setattr(LifecycleOperation, "write_sealed", fail)
    elif failure == "terminal-save":
        original = GoalStore.save

        def fail(self, lock, record):
            if record.state is GoalState.ABANDONED:
                raise OSError("terminal save failed")
            return original(self, lock, record)

        monkeypatch.setattr(GoalStore, "save", fail)
    boundary = (
        "abandoned"
        if failure == "after-terminal"
        else "proposal-closed"
        if failure == "after-closure"
        else None
    )
    with pytest.raises((OSError, Crash)):
        finish_goal(call, environment(layout, None if boundary is None else stop_at(boundary)))
    if failure in {"intent-write", "terminal-save"}:
        assert changes.store.load(root.id).state is GoalState.ACTIVE
        assert path.read_bytes() == before
    monkeypatch.undo()
    result = finish_goal(call, environment(layout))
    closed = load_proposal(path.parent.parent.parent, view.proposal.id)
    assert result["status"] == "abandoned" and closed.closed_by_abandonment
    assert closed.decision is None and closed.state == "pending"


def legacy_attempt(complete, call):
    from booley.goals.proposals import digest, encode
    from booley.review.goal_package import GoalCompletionPackage
    from booley.review.goal_presentation_v1 import render_goal_briefing

    with pytest.raises(Crash):
        finish_goal(call, environment(complete, stop_at("attempt-frozen")))
    directory = complete.control / "goals" / call.record_id / "operations" / call.operation_id
    authority = json.loads((directory / "attempt.authority.json").read_bytes())
    value = authority["value"]
    value["schema"] = "booley.goal-finish-attempt/v1"
    del value["presentation"]
    capture = value.pop("private_capture")
    value["package"]["input_proof"] = capture
    value["package"].pop("earlier_attempts")
    value["package_digest"] = digest(value["package"])
    summary = render_goal_briefing(GoalCompletionPackage.from_json(value["package"])).encode()
    import hashlib

    value["summary_hex"] = summary.hex()
    value["summary_digest"] = hashlib.sha256(summary).hexdigest()
    authority["digest"] = digest(value)
    (directory / "attempt.json").write_bytes(encode(value))
    (directory / "attempt.authority.json").write_bytes(encode(authority))
    return value, directory


@pytest.mark.parametrize("change_buffer", [False, True])
def test_unpublished_legacy_buffers_require_fresh_operation(complete, monkeypatch, change_buffer):
    call = request(complete, explain_html=True)
    frozen, directory = legacy_attempt(complete, call)
    before = (directory / "attempt.json").read_bytes()
    if change_buffer:
        (complete.control / "hooks" / "new-hook.py").parent.mkdir(exist_ok=True)
        (complete.control / "hooks" / "new-hook.py").write_text("protected new input\n")
    monkeypatch.setattr("booley.goals.finish.render_goal_briefing", lambda _: "upgraded renderer")
    result = finish_goal(call, environment(complete))
    record = GoalStore(complete.control).load(call.record_id)
    assert result["status"] == "revalidation_required"
    assert "legacy" in result["reason"] and record.state is GoalState.ACTIVE
    assert record.publication_floor > frozen["record_revision"]
    assert (directory / "attempt.json").read_bytes() == before
    assert git(complete.worktree, "rev-parse", "HEAD") == frozen["inputs"]["rtl_pin"]
    assert not (complete.worktree / frozen["path"]).exists()
    assert finish_goal(call, environment(complete)) == result


def publish_legacy_attempt(complete, call):
    from booley.goals.finish_presentation import html_bytes
    from booley.goals.lifecycle import bind_operation
    from booley.goals.proposals import digest, encode
    from booley.runtime.pinned_history import PinnedPublication, publish_pinned, raw_git

    frozen, directory = legacy_attempt(complete, call)
    content = bytes.fromhex(frozen["summary_hex"])
    with bind_operation(GoalStore(complete.control), call) as operation:
        operation.store.save(
            operation.lock,
            replace(
                operation.store.load(call.record_id),
                state=GoalState.FINISHING,
                finish_operation=call.operation_id,
                validated_head=frozen["inputs"]["rtl_pin"],
                package_digest="sha256:" + frozen["package_digest"],
                finish_attempt_digest="sha256:" + digest(frozen),
                publication_floor=frozen["record_revision"] + 1,
            ),
        )
        (directory / "SUMMARY.md").write_bytes(content)
        (directory / "review-package.json").write_bytes(encode(frozen["package"]))
        (directory / "explanation.html").write_bytes(html_bytes(frozen))
        destination = complete.worktree / frozen["path"]
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content)
        blob = (
            raw_git(complete.worktree, "hash-object", "-w", "--no-filters", str(destination))
            .strip()
            .decode()
        )
        operation.write_sealed(
            "publication",
            {
                "mode": "100644",
                "repository": frozen["inputs"]["roots"]["rtl"],
                "worktree": frozen["worktree"],
                "branch": frozen["branch"],
                "pin": frozen["inputs"]["rtl_pin"],
                "path": frozen["path"],
                "operation_id": call.operation_id,
                "blob": blob,
            },
        )
        publication = PinnedPublication(
            frozen["inputs"]["rtl_pin"], frozen["branch"], frozen["path"], blob
        )
        publish_pinned(
            complete.worktree,
            publication,
            "legacy exact publication",
            lambda commit: operation.write_sealed("intended-commit", {"commit": commit}),
        )
    return frozen, destination, content


def test_proved_post_cas_legacy_replays_frozen_recipe_without_recapture(layout, monkeypatch):
    import hashlib

    complete = publication_layout(layout)
    call = request(complete, explain_html=True)
    frozen, destination, content = publish_legacy_attempt(complete, call)
    head = git(complete.worktree, "rev-parse", "HEAD")
    recreate(complete.worktree)
    monkeypatch.setattr("booley.goals.finish.render_goal_briefing", lambda _: "changed renderer")
    result = finish_goal(call, environment(complete))
    assert result["status"] == "finished" and result["message"].encode() == content
    assert hashlib.sha256(destination.read_bytes()).hexdigest() == frozen["summary_digest"]
    assert git(complete.worktree, "rev-parse", "HEAD") == head
    assert finish_goal(call, environment(complete)) == result


@pytest.mark.parametrize("boundary", ["index-lock-acquired", "index-lock-filled"])
@pytest.mark.parametrize("successor", ["file", "symlink"])
def test_owned_index_lock_never_truncates_or_hands_off_successor(
    layout, tmp_path, boundary, successor
):
    complete = publication_layout(layout)
    call = request(complete)
    index = Path(git(complete.worktree, "rev-parse", "--git-path", "index"))
    before = index.read_bytes()
    lock = index.with_name("index.lock")
    target = tmp_path / "foreign-target"
    target.write_bytes(b"FOREIGN BYTES")

    def replace_lock(name):
        if name == boundary:
            lock.unlink()
            if successor == "symlink":
                symlink_or_skip(lock, target)
            else:
                lock.write_bytes(b"FOREIGN BYTES")

    with pytest.raises(LifecycleError, match="ownership changed"):
        finish_goal(call, environment(complete, replace_lock))
    assert lock.read_bytes() == b"FOREIGN BYTES" and target.read_bytes() == b"FOREIGN BYTES"
    assert index.read_bytes() == before
    lock.unlink()
    assert finish_goal(call, environment(complete))["status"] == "finished"


def test_canonical_external_edit_is_retained_and_cannot_be_silently_adopted(complete):
    call = request(complete)
    with pytest.raises(Crash):
        finish_goal(call, environment(complete, stop_at("finished")))
    record_dir = complete.control / "goals" / call.record_id
    path = record_dir / "SUMMARY.md"
    path.write_bytes(b"external canonical edit")
    with pytest.raises(LifecycleError, match="external canonical"):
        finish_goal(call, environment(complete))
    assert path.read_bytes() == b"external canonical edit"


def test_failed_committed_intent_recovery_never_closes_earlier_pending_proposal(layout):
    from booley.goals.proposals import load_proposal, proposal_path
    from tests.goals.test_proposals import interrupted_transaction, proposal, setup_cycle

    changes = setup_cycle(layout)
    first, second = proposal(layout, changes), proposal(layout, changes, maximum=300)
    pending, committed = sorted((first, second), key=lambda view: view.proposal.id)
    interrupted_transaction(layout, changes, committed, boundary="intent")
    root = layout.control / "goals" / layout.record.id
    path = proposal_path(root, pending.proposal.id) / "state.json"
    before = path.read_bytes()
    (root / "booley_state.json").write_bytes(b"foreign persisted projection")
    with pytest.raises(ValueError):
        abandon_goal(changes.store, layout.worktree, environment(layout))
    assert changes.store.load(layout.record.id).state is GoalState.ACTIVE
    assert path.read_bytes() == before
    assert load_proposal(root, pending.proposal.id).closed_by_abandonment is None


@pytest.mark.skipif(
    __import__("sys").platform == "win32", reason="POSIX non-executable working-file projection"
)
def test_same_git_mode_0664_is_accepted_without_chmod(complete):
    call = request(complete)
    with pytest.raises(Crash):
        finish_goal(call, environment(complete, stop_at("finishing")))
    directory = complete.control / "goals" / call.record_id / "operations" / call.operation_id
    value = json.loads((directory / "attempt.json").read_bytes())
    destination = complete.worktree / value["path"]
    destination.parent.mkdir(parents=True)
    destination.write_bytes(bytes.fromhex(value["summary_hex"]))
    destination.chmod(0o664)
    assert finish_goal(call, environment(complete))["status"] == "finished"
    assert destination.stat().st_mode & 0o777 == 0o664


@pytest.mark.parametrize("collision", [False, True])
@pytest.mark.skipif(__import__("os").name == "nt", reason="real distinct POSIX filesystems")
def test_cross_volume_publication_and_foreign_anchor_confinement(layout, tmp_path, collision):
    import tempfile
    from types import SimpleNamespace

    from tests.goals.conftest import write_project_files

    volume = Path("/dev/shm")
    if not volume.is_dir() or volume.stat().st_dev == layout.control.stat().st_dev:
        pytest.skip("distinct writable temporary filesystem unavailable")
    with tempfile.TemporaryDirectory(prefix="booley-cross-volume-", dir=volume) as scratch:
        work = Path(scratch) / "work"
        git(layout.main, "worktree", "add", "-q", "-b", "cross-volume", str(work))
        try:
            write_project_files(work / ".booley_project")
            current = publication_layout(SimpleNamespace(**{**vars(layout), "worktree": work}))
            call = request(current)
            before = git(current.main, "rev-parse", "HEAD")
            if collision:
                foreign = tmp_path / "foreign"
                foreign.mkdir()
                (foreign / "sentinel").write_bytes(b"unchanged")
                anchor = work / ".booley_project/goals" / (".publication-" + call.operation_id)
                anchor.parent.mkdir(exist_ok=True)
                anchor.symlink_to(foreign, target_is_directory=True)
                with pytest.raises(LifecycleError, match=r"staging anchor|commit changes"):
                    finish_goal(call, environment(current))
                assert list(foreign.iterdir()) == [foreign / "sentinel"]
                assert (foreign / "sentinel").read_bytes() == b"unchanged"
            else:
                with pytest.raises(Crash):
                    finish_goal(call, environment(current, stop_at("summary-linked")))
                result = finish_goal(call, environment(current))
                assert result["status"] == "finished"
                assert finish_goal(call, environment(current)) == result
            assert git(current.main, "rev-parse", "HEAD") == before
        finally:
            git(layout.main, "worktree", "remove", "--force", str(work))


def test_crlf_consumed_source_finishes_from_exact_pinned_builtin_projection(layout):
    (layout.worktree / ".gitattributes").write_bytes(b"rtl.v text eol=crlf\n")
    git(layout.worktree, "add", ".gitattributes")
    git(layout.worktree, "commit", "-qm", "pinned EOL policy")
    complete = publication_layout(layout)
    (layout.worktree / "rtl.v").unlink()
    git(layout.worktree, "checkout-index", "--all", "--force")
    assert b"\r\n" in (layout.worktree / "rtl.v").read_bytes()
    publish(complete)
    result = finish_goal(request(complete), environment(complete))
    assert result["status"] == "finished"
    facts = json.loads(Path(result["package"]).read_bytes())
    assert facts["input_proof"]["committed_materializations"][0]["policy"]["autocrlf"] == "false"


@pytest.mark.parametrize("ancestor", ["operations", "operation"])
def test_operation_storage_link_refuses_before_external_write(complete, tmp_path, ancestor):
    call = request(complete)
    record = complete.control / "goals" / call.record_id
    operations = record / "operations"
    operations.mkdir(exist_ok=True)
    foreign = tmp_path / "foreign-operation"
    foreign.mkdir()
    if ancestor == "operations":
        operations.rmdir()
        symlink_or_skip(operations, foreign, target_is_directory=True)
    else:
        symlink_or_skip(operations / call.operation_id, foreign, target_is_directory=True)
    with pytest.raises(LifecycleError, match="storage is linked/outside"):
        finish_goal(call, environment(complete))
    assert list(foreign.iterdir()) == []
    assert GoalStore(complete.control).load(call.record_id).state is GoalState.ACTIVE


@pytest.mark.parametrize("change", ["recipe", "digest", "unrequested", "path", "message"])
def test_prefence_presentation_substitution_refuses_before_effects(complete, change):
    import hashlib

    from booley.goals.proposals import digest, encode

    call = request(complete, explain_html=change != "unrequested")
    with pytest.raises(Crash):
        finish_goal(call, environment(complete, stop_at("attempt-frozen")))
    directory = complete.control / "goals" / call.record_id / "operations" / call.operation_id
    authority = json.loads((directory / "attempt.authority.json").read_bytes())
    presentation = authority["value"]["presentation"]
    if change == "recipe":
        presentation["recipe"] = "unknown"
    elif change in {"digest", "unrequested"}:
        presentation["html_digest"] = "0" * 64
    else:
        response = json.loads(bytes.fromhex(presentation["response_hex"]))
        if change == "path":
            response["package"] = str(complete.control / "outside.json")
        else:
            response["message"] = "Substituted response"
        content = encode(response)
        presentation["response_hex"] = content.hex()
        presentation["response_digest"] = hashlib.sha256(content).hexdigest()
    authority["digest"] = digest(authority["value"])
    (directory / "attempt.authority.json").write_bytes(encode(authority))
    (directory / "attempt.json").write_bytes(encode(authority["value"]))
    with pytest.raises(LifecycleError, match=r"presentation|HTML|response"):
        finish_goal(call, environment(complete))
    assert not (directory / "SUMMARY.md").exists()
    assert not (complete.control / "outside.json").exists()
    assert GoalStore(complete.control).load(call.record_id).state is GoalState.ACTIVE


def test_legacy_revalidation_then_v2_success_replaces_only_proved_canonical_bytes(complete):
    from booley.goals.proposals import encode

    call = request(complete, explain_html=True)
    frozen, directory = legacy_attempt(complete, call)
    record_dir = directory.parent.parent
    prior = {
        "SUMMARY.md": bytes.fromhex(frozen["summary_hex"]),
        "review-package.json": encode(frozen["package"]),
    }
    for name, content in prior.items():
        (record_dir / name).write_bytes(content)
    assert finish_goal(call, environment(complete))["status"] == "revalidation_required"
    result = finish_goal(request(complete), environment(complete))
    assert result["status"] == "finished"
    assert (record_dir / "SUMMARY.md").read_bytes() == Path(result["summary"]).read_bytes()
    assert (record_dir / "review-package.json").read_bytes() == Path(
        result["package"]
    ).read_bytes()
    facts = json.loads(Path(result["package"]).read_bytes())
    assert facts["earlier_attempts"][0]["operation_id"] == call.operation_id
    assert facts["earlier_attempts"][0]["outcome"] == "revalidation_required"
    assert json.loads((directory / "attempt.json").read_bytes()) == frozen


@pytest.mark.parametrize("change", ["bytes", "inode", "symlink"])
def test_owned_summary_staging_tamper_never_materializes_public_destination(
    layout, tmp_path, change
):
    complete = publication_layout(layout)
    call = request(complete)
    with pytest.raises(Crash):
        finish_goal(call, environment(complete, stop_at("summary-staging-owned")))
    directory = complete.control / "goals" / call.record_id / "operations" / call.operation_id
    saved = json.loads((directory / "summary-staging.json").read_bytes())
    stage = Path(saved["path"])
    foreign = tmp_path / "foreign-buffer"
    foreign.write_bytes(b"preserve foreign bytes")
    if change == "bytes":
        stage.write_bytes(b"edited owned inode")
    else:
        stage.rename(stage.with_name(stage.name + "-retained"))
        if change == "inode":
            stage.write_bytes(
                bytes.fromhex(json.loads((directory / "attempt.json").read_bytes())["summary_hex"])
            )
        else:
            symlink_or_skip(stage, foreign)
    before = git(complete.worktree, "rev-parse", "HEAD")
    with pytest.raises(LifecycleError, match=r"staging|inode"):
        finish_goal(call, environment(complete))
    path = json.loads((directory / "attempt.json").read_bytes())["path"]
    assert not (complete.worktree / path).exists()
    assert foreign.read_bytes() == b"preserve foreign bytes"
    assert git(complete.worktree, "rev-parse", "HEAD") == before
