"""Behavioral contracts for manifest-owned Project Setup cleanup."""

from __future__ import annotations

import json
import os
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from booley.feedback.findings import Finding, append, read_log
from booley.feedback.render import _attachment_block
from booley.harness.setup import cleanup as cleanup_module
from booley.harness.setup.cleanup import (
    CleanupBlockedError,
    CleanupError,
    FileIdentity,
    ManifestEntry,
    apply_cleanup,
    prepare_run,
    preview_cleanup,
    record_artifact,
)
from booley.runtime import job_records


def _project(tmp_path: Path) -> Path:
    (tmp_path / ".booley_project").mkdir()
    return tmp_path


def test_minimal_cleanup_removes_only_current_run_and_scratch_root(tmp_path: Path) -> None:
    root = _project(tmp_path)
    scratch = prepare_run(root, "run-minimal")
    capture = scratch / "doctor.stdout"
    capture.write_text("transient\n", encoding="utf-8")
    record_artifact(
        root,
        "run-minimal",
        capture,
        producer="doctor",
        artifact_class="command-capture",
    )

    plan = preview_cleanup(root, run_id="run-minimal")
    assert not plan.inventory_only
    assert plan.grouped()["remove"]["count"] == 2
    result = apply_cleanup(plan)

    assert result.unresolved == ()
    assert not scratch.exists()


def test_preserved_artifact_keeps_the_run_root_and_is_not_adopted(tmp_path: Path) -> None:
    root = _project(tmp_path)
    scratch = prepare_run(root, "run-preserve")
    report = scratch / "report.md"
    report.write_text("retain\n", encoding="utf-8")
    record_artifact(
        root,
        "run-preserve",
        report,
        producer="findings",
        artifact_class="report",
        disposition="preserve",
    )

    result = apply_cleanup(preview_cleanup(root, run_id="run-preserve"))

    assert result.unresolved
    assert report.exists()
    assert scratch.exists()


def test_diagnostic_retains_raw_evidence_but_minimal_removes_it(tmp_path: Path) -> None:
    root = _project(tmp_path)
    scratch = prepare_run(root, "run-mode")
    evidence = scratch / "raw.log"
    evidence.write_text("raw\n", encoding="utf-8")
    record_artifact(
        root,
        "run-mode",
        evidence,
        producer="doctor",
        artifact_class="raw-evidence",
    )

    diagnostic = preview_cleanup(root, run_id="run-mode", retention_mode="diagnostic")
    assert any(
        item.path.endswith("raw.log") and item.category == "preserve" for item in diagnostic.items
    )


def test_legacy_plan_is_inventory_only_and_never_deletes(tmp_path: Path) -> None:
    root = _project(tmp_path)
    residue = root / ".booley_project" / "tmp" / "doctor"
    residue.mkdir(parents=True)
    marker = residue / "old.log"
    marker.write_text("old\n", encoding="utf-8")

    plan = preview_cleanup(root)
    result = apply_cleanup(plan)

    assert plan.inventory_only
    assert marker.exists()
    assert result.unresolved


def test_changed_identity_becomes_unresolved(tmp_path: Path) -> None:
    root = _project(tmp_path)
    scratch = prepare_run(root, "run-identity")
    candidate = scratch / "capture.log"
    candidate.write_text("before\n", encoding="utf-8")
    record_artifact(
        root,
        "run-identity",
        candidate,
        producer="doctor",
        artifact_class="command-capture",
    )
    plan = preview_cleanup(root, run_id="run-identity")
    candidate.unlink()
    candidate.write_text("replacement\n", encoding="utf-8")

    result = apply_cleanup(plan)

    assert any("capture.log" in item for item in result.unresolved)
    assert candidate.exists()


def test_bounded_directory_does_not_delete_an_unmanifested_child(tmp_path: Path) -> None:
    root = _project(tmp_path)
    scratch = prepare_run(root, "run-tree")
    tree = scratch / "doctor"
    tree.mkdir()
    known = tree / "known.log"
    known.write_text("known\n", encoding="utf-8")
    record_artifact(
        root,
        "run-tree",
        tree,
        producer="doctor",
        artifact_class="doctor-scratch",
    )
    unexpected = tree / "unexpected.log"
    unexpected.write_text("keep\n", encoding="utf-8")

    result = apply_cleanup(preview_cleanup(root, run_id="run-tree"))

    assert unexpected.exists()
    assert any("doctor" in item for item in result.unresolved)
    assert not known.exists()


def test_symlink_escape_is_rejected(tmp_path: Path) -> None:
    root = _project(tmp_path)
    scratch = prepare_run(root, "run-link")
    outside = tmp_path / "outside.log"
    outside.write_text("secret\n", encoding="utf-8")
    linked = scratch / "linked.log"
    try:
        linked.symlink_to(outside)
    except OSError:
        pytest.skip("symlink creation is unavailable")

    with pytest.raises(CleanupError, match="outside"):
        record_artifact(
            root,
            "run-link",
            linked,
            producer="probe",
            artifact_class="probe",
        )


def test_attachment_is_materialized_before_source_deletion(tmp_path: Path) -> None:
    root = _project(tmp_path)
    project_dir = root / ".booley_project"
    scratch = prepare_run(root, "run-attachment")
    source = scratch / "doctor.log"
    source.write_text("line 1\nline 2\n", encoding="utf-8")
    finding = append(Finding(title="doctor finding", attachments=[str(source)]), project_dir)
    record_artifact(
        root,
        "run-attachment",
        source,
        producer="doctor",
        artifact_class="raw-evidence",
    )

    result = apply_cleanup(preview_cleanup(root, run_id="run-attachment"))

    assert not source.exists()
    assert result.unresolved == ()
    attachment = read_log(project_dir).entries[0].attachments[0]
    assert Path(attachment).exists()
    assert "line 2" in "\n".join(_attachment_block(attachment))
    assert finding.id == read_log(project_dir).entries[0].id


def test_corrupt_findings_log_blocks_only_referenced_source(tmp_path: Path) -> None:
    root = _project(tmp_path)
    project_dir = root / ".booley_project"
    scratch = prepare_run(root, "run-corrupt")
    attached = scratch / "attached.log"
    unrelated = scratch / "unrelated.log"
    attached.write_text("attached\n", encoding="utf-8")
    unrelated.write_text("unrelated\n", encoding="utf-8")
    append(Finding(title="attached", attachments=[str(attached)]), project_dir)
    with (project_dir / "findings.jsonl").open("a", encoding="utf-8") as stream:
        stream.write("not json\n")
    for path in (attached, unrelated):
        record_artifact(
            root,
            "run-corrupt",
            path,
            producer="doctor",
            artifact_class="raw-evidence",
        )

    result = apply_cleanup(preview_cleanup(root, run_id="run-corrupt"))

    assert attached.exists()
    assert not unrelated.exists()
    assert any("attached.log" in item for item in result.unresolved)


def test_stale_digest_is_rejected(tmp_path: Path) -> None:
    root = _project(tmp_path)
    scratch = prepare_run(root, "run-digest")
    candidate = scratch / "capture.log"
    candidate.write_text("x\n", encoding="utf-8")
    record_artifact(
        root,
        "run-digest",
        candidate,
        producer="doctor",
        artifact_class="command-capture",
    )
    plan = preview_cleanup(root, run_id="run-digest")
    extra = scratch / "extra.log"
    extra.write_text("new\n", encoding="utf-8")
    record_artifact(
        root,
        "run-digest",
        extra,
        producer="doctor",
        artifact_class="command-capture",
    )

    with pytest.raises(CleanupBlockedError):
        apply_cleanup(plan)


@pytest.mark.parametrize("run_id", [".", "..", "nested/run", "é"])
def test_prepare_rejects_unsafe_run_ids(tmp_path: Path, run_id: str) -> None:
    with pytest.raises(CleanupError, match="safe ASCII"):
        prepare_run(_project(tmp_path), run_id)


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ([], "cannot read cleanup manifest"),
        ({"version": 999, "run_id": "bad", "entries": [], "journal": {}}, "unsupported"),
        ({"version": 1, "run_id": "bad", "entries": {}, "journal": {}}, "structure"),
        ({"version": 1, "run_id": "bad", "entries": [None], "journal": {}}, "entry"),
        (
            {
                "version": 1,
                "run_id": "bad",
                "entries": [{"path": ".booley_project/tmp/setup/bad/x"}],
                "journal": {},
            },
            "identity",
        ),
    ],
)
def test_manifest_validation_fails_closed(tmp_path: Path, payload: object, message: str) -> None:
    root = _project(tmp_path)
    scratch = prepare_run(root, "bad")
    (scratch / "manifest.json").write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(CleanupError, match=message):
        preview_cleanup(root, run_id="bad")


def test_manifest_entry_rejects_invalid_claim_fields() -> None:
    identity = {"kind": "file", "device": 1, "inode": 2, "size": 0, "mode": 0o600}
    base = {
        "path": ".booley_project/tmp/setup/run/file",
        "identity": identity,
    }
    with pytest.raises(ValueError, match="dependencies"):
        ManifestEntry.from_dict({**base, "dependencies": "file"})
    with pytest.raises(CleanupError, match="positive integer"):
        ManifestEntry.from_dict({**base, "process_group_id": 0})
    with pytest.raises(CleanupError, match="boolean"):
        ManifestEntry.from_dict({**base, "active": "yes"})


def test_record_rejects_missing_and_shared_paths(tmp_path: Path) -> None:
    root = _project(tmp_path)
    prepare_run(root, "run-invalid")
    with pytest.raises(CleanupError, match="missing artifact"):
        record_artifact(
            root,
            "run-invalid",
            root / ".booley_project" / "missing.log",
            producer="doctor",
            artifact_class="capture",
        )
    with pytest.raises(CleanupError, match="shared"):
        record_artifact(
            root,
            "run-invalid",
            root / ".booley_project" / "tmp",
            producer="doctor",
            artifact_class="capture",
        )


def test_active_claims_block_preview(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _project(tmp_path)
    scratch = prepare_run(root, "run-active")
    active = scratch / "active.log"
    active.write_text("active\n", encoding="utf-8")
    record_artifact(
        root,
        "run-active",
        active,
        producer="doctor",
        artifact_class="capture",
        active=True,
    )
    assert preview_cleanup(root, run_id="run-active").items[0].category == "unresolved"

    process = scratch / "process.log"
    process.write_text("process\n", encoding="utf-8")
    record_artifact(
        root,
        "run-active",
        process,
        producer="doctor",
        artifact_class="capture",
        process_group_id=417,
    )
    monkeypatch.setattr(cleanup_module, "is_process_group_alive", lambda group: group.id == 417)
    assert any(
        item.category == "unresolved" and item.path.endswith("process.log")
        for item in preview_cleanup(root, run_id="run-active").items
    )


def test_flow_cache_uses_explicit_owner_for_eviction(tmp_path: Path) -> None:
    root = _project(tmp_path)
    scratch = prepare_run(root, "run-cache")
    cache = scratch / "cache"
    cache.write_text("cache\n", encoding="utf-8")
    record_artifact(
        root,
        "run-cache",
        cache,
        producer="flow",
        artifact_class="flow-cache",
    )
    plan = preview_cleanup(root, run_id="run-cache", cache_disposition="evict-setup-touched")
    assert any(item.category == "evict-cache" for item in plan.items)
    result = apply_cleanup(plan, cache_evictor=lambda path: path.endswith("cache"))
    assert result.evicted == (".booley_project/tmp/setup/run-cache/cache",)
    assert cache.exists()


def test_apply_recovers_a_quarantined_entry(tmp_path: Path) -> None:
    root = _project(tmp_path)
    scratch = prepare_run(root, "run-recover")
    candidate = scratch / "capture.log"
    candidate.write_text("capture\n", encoding="utf-8")
    entry = record_artifact(
        root,
        "run-recover",
        candidate,
        producer="doctor",
        artifact_class="capture",
    )
    quarantine = candidate.with_name(".booley-cleanup-recovery-capture.log")
    candidate.rename(quarantine)
    journal = {entry.path: {"status": "quarantining", "quarantine": str(quarantine)}}

    outcome = cleanup_module._apply_entry(root, entry, journal, lambda: None)

    assert outcome == ("removed", entry.identity.size, None)
    assert not quarantine.exists()


def test_file_identity_handles_missing_path() -> None:
    identity = FileIdentity("file", 1, 2, 3, 0o600)
    assert not identity.matches(Path("/definitely/missing/booley-artifact"))


def test_cleanup_rejects_invalid_modes_and_duplicate_runs(tmp_path: Path) -> None:
    root = _project(tmp_path)
    prepare_run(root, "run-options")
    with pytest.raises(CleanupError, match="retention mode"):
        preview_cleanup(root, retention_mode="bad")
    with pytest.raises(CleanupError, match="cache disposition"):
        preview_cleanup(root, cache_disposition="bad")
    with pytest.raises(CleanupError, match="already has"):
        prepare_run(root, "run-options")


def test_cleanup_uses_plan_ledger_and_inventories_legacy_logs(tmp_path: Path) -> None:
    root = _project(tmp_path)
    project = root / ".booley_project"
    (project / "tmp" / "legacy").mkdir(parents=True)
    (project / "tmp" / "legacy" / "old.log").write_text("old\n", encoding="utf-8")
    (project / "existing.log").write_text("existing\n", encoding="utf-8")
    legacy = preview_cleanup(root)
    assert legacy.inventory_only
    assert len(legacy.items) == 2

    (project / "SETUP-PLAN.md").write_text("- Run ID: `run-ledger`\n", encoding="utf-8")
    scratch = prepare_run(root, "run-ledger")
    assert preview_cleanup(root).manifest_path == str(scratch / "manifest.json")


def test_cleanup_rejects_unsafe_manifest_paths_and_root(tmp_path: Path) -> None:
    root = _project(tmp_path)
    scratch = prepare_run(root, "run-paths")
    outside = tmp_path / "outside.log"
    outside.write_text("outside\n", encoding="utf-8")
    for path in (outside, root / ".booley_project" / ".git"):
        with pytest.raises(CleanupError):
            record_artifact(
                root,
                "run-paths",
                path,
                producer="doctor",
                artifact_class="capture",
            )
    with pytest.raises(CleanupError, match="root"):
        record_artifact(
            root,
            "run-paths",
            root / ".booley_project",
            producer="doctor",
            artifact_class="capture",
        )
    (scratch / "manifest.json").write_text(
        json.dumps({"version": 1, "run_id": "other", "entries": [], "journal": {}}),
        encoding="utf-8",
    )
    with pytest.raises(CleanupError, match="beneath"):
        preview_cleanup(root, run_id="run-paths")


def test_cleanup_rejects_symlinked_project_and_final_candidate(tmp_path: Path) -> None:
    target = tmp_path / "target-project"
    target.mkdir()
    (target / ".booley_project").mkdir()
    linked_root = tmp_path / "linked-root"
    linked_root.mkdir()
    try:
        (linked_root / ".booley_project").symlink_to(
            target / ".booley_project", target_is_directory=True
        )
    except OSError:
        pytest.skip("symlink creation is unavailable")
    with pytest.raises(CleanupError):
        preview_cleanup(linked_root)

    real_root = tmp_path / "real"
    real_root.mkdir()
    root = _project(real_root)
    scratch = prepare_run(root, "run-final-link")
    target_file = root / "outside.log"
    target_file.write_text("outside\n", encoding="utf-8")
    link = scratch / "candidate.log"
    link.symlink_to(target_file)
    with pytest.raises(CleanupError):
        record_artifact(
            root,
            "run-final-link",
            link,
            producer="doctor",
            artifact_class="capture",
        )


def test_cleanup_handles_done_and_absent_journal_states(tmp_path: Path) -> None:
    root = _project(tmp_path)
    scratch = prepare_run(root, "run-journal")
    candidate = scratch / "capture.log"
    candidate.write_text("capture\n", encoding="utf-8")
    entry = record_artifact(
        root,
        "run-journal",
        candidate,
        producer="doctor",
        artifact_class="capture",
    )
    journal = {entry.path: {"status": "done"}}
    assert cleanup_module._apply_entry(root, entry, journal, lambda: None) == ("removed", 0, None)
    candidate.unlink()
    journal.clear()
    assert cleanup_module._apply_entry(root, entry, journal, lambda: None) == ("absent", 0, None)


def test_cleanup_classifies_unsupported_disposition(tmp_path: Path) -> None:
    root = _project(tmp_path)
    scratch = prepare_run(root, "run-disposition")
    candidate = scratch / "capture.log"
    candidate.write_text("capture\n", encoding="utf-8")
    record_artifact(
        root,
        "run-disposition",
        candidate,
        producer="doctor",
        artifact_class="capture",
        disposition="unexpected",
    )
    item = next(
        item
        for item in preview_cleanup(root, run_id="run-disposition").items
        if item.path.endswith("capture.log")
    )
    assert item.category == "unresolved"


def test_cleanup_discovers_quarantined_manifest_from_ledger(tmp_path: Path) -> None:
    root = _project(tmp_path)
    project = root / ".booley_project"
    (project / "SETUP-PLAN.md").write_text(
        "ignored line\n- Run ID: `run-quarantine`\n", encoding="utf-8"
    )
    scratch = prepare_run(root, "run-quarantine")
    quarantine = scratch.parent / ".booley-cleanup-recovery-run-quarantine"
    scratch.rename(quarantine)

    assert cleanup_module._find_manifest(root, None) == quarantine / "manifest.json"
    assert cleanup_module._size(quarantine / "manifest.json") > 0


def test_cleanup_checks_job_record_liveness(tmp_path: Path) -> None:
    root = _project(tmp_path)
    scratch = prepare_run(root, "run-job")
    candidate = scratch / "job.log"
    candidate.write_text("job\n", encoding="utf-8")
    records = tmp_path / "job-records"
    record = job_records.JobRecord(
        run_id="job",
        endpoint="doctor",
        started_at=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        timeout_s=60,
        pid=os.getpid(),
    )
    job_records.write_record(record, records)
    record_artifact(
        root,
        "run-job",
        candidate,
        producer="doctor",
        artifact_class="capture",
        job_record=records / "job.json",
    )

    item = next(
        item
        for item in preview_cleanup(root, run_id="run-job").items
        if item.path.endswith("job.log")
    )
    assert item.category == "unresolved"
    assert "Job record" in item.reason


def test_cleanup_entry_safety_branches_are_fail_closed(tmp_path: Path) -> None:
    root = _project(tmp_path)
    scratch = prepare_run(root, "run-safety")
    target_dir = scratch / "target"
    target_dir.mkdir()
    target = target_dir / "target.log"
    target.write_text("target\n", encoding="utf-8")
    link_parent = scratch / "linked"
    try:
        link_parent.symlink_to(target_dir, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation is unavailable")
    identity = FileIdentity.from_path(target)
    linked_entry = ManifestEntry(
        ".booley_project/tmp/setup/run-safety/linked/target.log",
        "doctor",
        "capture",
        "remove",
        identity,
    )
    item = cleanup_module._item_for_entry(root, linked_entry, "minimal", "preserve")
    assert item.category == "unresolved"
    final_link = scratch / "final-link"
    final_link.symlink_to(target)
    final_entry = replace(linked_entry, path=".booley_project/tmp/setup/run-safety/final-link")
    assert (
        cleanup_module._item_for_entry(root, final_entry, "minimal", "preserve").category
        == "unresolved"
    )


def test_cleanup_resumes_changed_quarantine_and_active_entry(tmp_path: Path) -> None:
    root = _project(tmp_path)
    scratch = prepare_run(root, "run-resume")
    candidate = scratch / "capture.log"
    candidate.write_text("capture\n", encoding="utf-8")
    entry = record_artifact(
        root,
        "run-resume",
        candidate,
        producer="doctor",
        artifact_class="capture",
    )
    wrong = candidate.with_name(".booley-cleanup-wrong")
    wrong.write_text("wrong\n", encoding="utf-8")
    mismatch = cleanup_module._apply_entry(
        root,
        entry,
        {entry.path: {"status": "quarantining", "quarantine": str(wrong)}},
        lambda: None,
    )
    assert mismatch[0] == "unresolved"
    assert (
        cleanup_module._apply_entry(root, replace(entry, active=True), {}, lambda: None)[0]
        == "unresolved"
    )


def test_cleanup_summary_includes_apply_outcomes(tmp_path: Path) -> None:
    root = _project(tmp_path)
    plan = preview_cleanup(root)
    text = cleanup_module.format_summary(
        plan,
        cleanup_module.CleanupResult(plan.digest, removed=("x",), bytes_removed=1),
    )
    assert "removed: 1 byte(s) across 1 path(s)" in text
