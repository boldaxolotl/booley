"""Behavioral contracts for manifest-owned Project Setup cleanup."""

from __future__ import annotations

from pathlib import Path

import pytest

from booley.feedback.findings import Finding, append, read_log
from booley.feedback.render import _attachment_block
from booley.harness.setup.cleanup import (
    CleanupBlockedError,
    CleanupError,
    apply_cleanup,
    prepare_run,
    preview_cleanup,
    record_artifact,
)


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
