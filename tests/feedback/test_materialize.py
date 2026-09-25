"""Regression coverage for lossless Findings Log attachment snapshots."""

from __future__ import annotations

from pathlib import Path

import pytest

from booley.feedback import materialize
from booley.feedback.findings import Finding, append
from booley.feedback.render import Environment


def _project(tmp_path: Path) -> Path:
    project = tmp_path / ".booley_project"
    project.mkdir()
    return project


def test_normalize_attachment_path_checks_supported_bases(tmp_path: Path, monkeypatch) -> None:
    project_dir = _project(tmp_path)
    sibling = project_dir.parent / "sibling.log"
    sibling.write_text("sibling\n", encoding="utf-8")
    local = project_dir / "local.log"
    local.write_text("local\n", encoding="utf-8")
    cwd_file = tmp_path / "cwd.log"
    cwd_file.write_text("cwd\n", encoding="utf-8")

    monkeypatch.chdir(tmp_path)
    assert materialize.normalize_attachment_path(str(sibling), project_dir) == sibling
    assert materialize.normalize_attachment_path("local.log", project_dir) == local
    assert materialize.normalize_attachment_path("cwd.log", project_dir) == cwd_file
    assert materialize.normalize_attachment_path("missing.log", project_dir) == (
        project_dir.parent / "missing.log"
    )


def test_excerpt_caps_lines_and_characters(tmp_path: Path) -> None:
    source = tmp_path / "long.log"
    source.write_text("x" * 10000 + "\n" + "tail\n", encoding="utf-8")

    excerpt, metadata = materialize._excerpt(source)

    assert len(excerpt) == materialize.MAX_ATTACHMENT_CHARS
    assert metadata["clipped"] is True
    assert metadata["line_count"] == 2


def test_materialize_ignores_unselected_and_empty_sources(tmp_path: Path) -> None:
    project_dir = _project(tmp_path)
    source = tmp_path / "other.log"
    source.write_text("other\n", encoding="utf-8")
    append(Finding(title="finding", attachments=[str(source)]), project_dir)

    assert materialize.materialize_attachments(project_dir, []) == ()
    assert materialize.materialize_attachments(project_dir, [tmp_path / "missing.log"]) == ()


def test_materialize_rejects_corrupt_log(tmp_path: Path) -> None:
    project_dir = _project(tmp_path)
    source = tmp_path / "attached.log"
    source.write_text("attached\n", encoding="utf-8")
    append(Finding(title="finding", attachments=[str(source)]), project_dir)
    with (project_dir / "findings.jsonl").open("a", encoding="utf-8") as stream:
        stream.write("not json\n")

    with pytest.raises(materialize.CorruptFindingsLogError):
        materialize.materialize_attachments(project_dir, [source])


def test_materialize_reports_unreadable_selected_attachment(tmp_path: Path) -> None:
    project_dir = _project(tmp_path)
    source = tmp_path / "attached.log"
    source.write_text("attached\n", encoding="utf-8")
    append(Finding(title="finding", attachments=[str(source)]), project_dir)
    source.unlink()

    with pytest.raises(materialize.MaterializationError, match="cannot read attachment"):
        materialize.materialize_attachments(project_dir, [source])


def test_materialize_requires_both_render_proofs_to_match(tmp_path: Path, monkeypatch) -> None:
    project_dir = _project(tmp_path)
    source = tmp_path / "attached.log"
    source.write_text("attached\n", encoding="utf-8")
    append(Finding(title="finding", attachments=[str(source)]), project_dir)
    proofs = iter([("before", "export"), ("after", "export")])
    monkeypatch.setattr(materialize, "_render_proofs", lambda *_args: next(proofs))

    with pytest.raises(materialize.MaterializationError, match="report or export"):
        materialize.materialize_attachments(project_dir, [source])


def test_materialize_reuses_one_explicit_environment(tmp_path: Path, monkeypatch) -> None:
    project_dir = _project(tmp_path)
    source = tmp_path / "attached.log"
    source.write_text("attached\n", encoding="utf-8")
    append(Finding(title="finding", attachments=[str(source)]), project_dir)
    environment = Environment(doctor_deep_clean=True)
    observed: list[Environment] = []
    original = materialize._render_proofs

    def record_environment(project_dir, entries, env):
        observed.append(env)
        return original(project_dir, entries, env)

    monkeypatch.setattr(materialize, "_render_proofs", record_environment)

    assert materialize.materialize_attachments(project_dir, [source], env=environment) == (source,)
    assert len(observed) == 2
    assert all(item is environment for item in observed)
