"""Regression coverage for lossless Findings Log attachment snapshots."""

from __future__ import annotations

from pathlib import Path

import pytest

from booley.feedback import materialize, render
from booley.feedback.findings import Finding, append, read_log
from booley.feedback.render import Environment


def _project(tmp_path: Path) -> Path:
    project = tmp_path / ".booley"
    project.mkdir()
    return project


def test_normalize_attachment_path_checks_supported_bases(tmp_path: Path, monkeypatch) -> None:
    booley_dir = _project(tmp_path)
    sibling = booley_dir.parent / "sibling.log"
    sibling.write_text("sibling\n", encoding="utf-8")
    local = booley_dir / "local.log"
    local.write_text("local\n", encoding="utf-8")
    cwd_file = tmp_path / "cwd.log"
    cwd_file.write_text("cwd\n", encoding="utf-8")

    monkeypatch.chdir(tmp_path)
    assert materialize.normalize_attachment_path(str(sibling), booley_dir) == sibling
    assert materialize.normalize_attachment_path("local.log", booley_dir) == local
    assert materialize.normalize_attachment_path("cwd.log", booley_dir) == cwd_file
    assert materialize.normalize_attachment_path("missing.log", booley_dir) == (
        booley_dir.parent / "missing.log"
    )


def test_materialize_ignores_unselected_and_empty_sources(tmp_path: Path) -> None:
    booley_dir = _project(tmp_path)
    source = tmp_path / "other.log"
    source.write_text("other\n", encoding="utf-8")
    append(Finding(title="finding", attachments=[str(source)]), booley_dir)

    assert materialize.materialize_attachments(booley_dir, []) == ()
    assert materialize.materialize_attachments(booley_dir, [tmp_path / "missing.log"]) == ()


def test_materialize_rejects_corrupt_log(tmp_path: Path) -> None:
    booley_dir = _project(tmp_path)
    source = tmp_path / "attached.log"
    source.write_text("attached\n", encoding="utf-8")
    append(Finding(title="finding", attachments=[str(source)]), booley_dir)
    with (booley_dir / "findings.jsonl").open("a", encoding="utf-8") as stream:
        stream.write("not json\n")

    path = booley_dir / "findings.jsonl"
    before = path.read_bytes()
    with pytest.raises(materialize.CorruptFindingsLogError):
        materialize.materialize_attachments(booley_dir, [source])
    assert path.read_bytes() == before
    assert source.exists()
    assert not (booley_dir / "setup-evidence").exists()
    assert not path.with_suffix(".jsonl.tmp").exists()


def test_materialize_reports_unreadable_selected_attachment(tmp_path: Path) -> None:
    booley_dir = _project(tmp_path)
    source = tmp_path / "attached.log"
    source.write_text("attached\n", encoding="utf-8")
    append(Finding(title="finding", attachments=[str(source)]), booley_dir)
    source.unlink()

    with pytest.raises(materialize.MaterializationError, match="cannot read attachment"):
        materialize.materialize_attachments(booley_dir, [source])


def test_materialize_retains_concurrent_append(tmp_path):
    import json

    from booley.feedback.findings import log_path, read_log
    from tests.feedback.test_findings import _race_feedback_append

    directory = _project(tmp_path)
    source = directory / "capture.log"
    source.write_text("evidence\n", encoding="utf-8")
    entry = append(Finding(title="original", attachments=[str(source)]), directory)
    raw = json.loads(entry.to_json())
    raw["future"] = {"keep": True}
    log_path(directory).write_text(json.dumps(raw) + "\n", encoding="utf-8")
    _race_feedback_append(directory, "materialize")
    log = read_log(directory)
    assert [item.title for item in log.entries] == ["original", "concurrent"]
    attachment = Path(log.entries[0].attachments[0])
    assert attachment != source
    assert attachment.read_text(encoding="utf-8") == "evidence"
    assert json.loads(log.path.read_text(encoding="utf-8").splitlines()[0])["future"] == {
        "keep": True
    }


def test_duplicate_rows_materialize_distinct_snapshots_and_preserve_unknown_fields(tmp_path):
    import json

    from booley.feedback.findings import log_path, read_log

    directory = _project(tmp_path)
    sources = []
    rows = []
    # Same basename, same attachment index, and same Finding ID used to collide.
    for ordinal, content in enumerate(("first evidence", "second evidence")):
        source = tmp_path / str(ordinal) / "capture.log"
        source.parent.mkdir()
        source.write_text(content + "\n", encoding="utf-8")
        sources.append(source)
        row = json.loads(
            Finding(title=f"row {ordinal}", id="F-1", attachments=[str(source)]).to_json()
        )
        row["future"] = {"ordinal": ordinal}
        rows.append(row)
    path = log_path(directory)
    path.write_text("\n" + "\n\n".join(json.dumps(row) for row in rows) + "\n")
    assert materialize.materialize_attachments(directory, sources) == tuple(sorted(sources))
    log = read_log(directory)
    attachments = [Path(entry.attachments[0]) for entry in log.entries]
    assert len(set(attachments)) == 2
    assert [item.read_text() for item in attachments] == ["first evidence", "second evidence"]
    assert [entry.id for entry in log.entries] == ["F-1", "F-1"]
    raw = path.read_text().splitlines()
    assert raw[0] == "" and raw[2] == ""
    assert [json.loads(line)["future"] for line in raw if line.strip()] == [
        {"ordinal": 0},
        {"ordinal": 1},
    ]


def test_materialization_timeout_does_not_write_snapshots_or_log(tmp_path, monkeypatch):
    from booley.feedback import findings
    from tests.feedback.test_findings import holding_findings_lock

    directory = _project(tmp_path)
    source = tmp_path / "capture.log"
    source.write_text("keep evidence\n")
    append(Finding(title="attached", attachments=[str(source)]), directory)
    path = findings.log_path(directory)
    before = path.read_bytes()
    monkeypatch.setattr(findings, "_LOCK_TIMEOUT_S", 0.01)
    with (
        holding_findings_lock(directory),
        pytest.raises(findings.FindingsLockTimeoutError, match=r"materialize.*findings.lock"),
    ):
        materialize.materialize_attachments(directory, [source])
    assert path.read_bytes() == before
    assert source.read_text() == "keep evidence\n"
    assert not (directory / "setup-evidence").exists()
    assert not path.with_suffix(".jsonl.tmp").exists()


def test_materialize_retains_concurrent_triage_attachment(tmp_path):
    from booley.feedback.findings import read_log
    from tests.feedback.test_findings import _race_feedback_append

    directory = _project(tmp_path)
    source = directory / "capture.log"
    source.write_text("original evidence\n", encoding="utf-8")
    late = directory / "late.log"
    late.write_text("new evidence\n", encoding="utf-8")
    append(Finding(title="original", attachments=[str(source)]), directory)
    _race_feedback_append(directory, "materialize", "triage_attach")
    finding = read_log(directory).entries[0]
    assert str(late) in finding.attachments
    migrated = next(Path(item) for item in finding.attachments if item != str(late))
    assert migrated != source
    assert migrated.read_text(encoding="utf-8") == "original evidence"


def test_materialize_never_overwrites_legacy_snapshot(tmp_path):
    directory = _project(tmp_path)
    old = directory / "setup-evidence" / "attachments" / "F-1-0-3-x.log.txt"
    old.parent.mkdir(parents=True)
    old.write_text("preserved old evidence", encoding="utf-8")
    source = tmp_path / "x.log"
    source.write_text("new evidence\n", encoding="utf-8")
    append(
        Finding(
            title="legacy snapshot",
            attachments=[
                str(old),
                str(tmp_path / "missing1"),
                str(tmp_path / "missing2"),
                str(source),
            ],
        ),
        directory,
    )
    materialize.materialize_attachments(directory, [source])
    assert old.read_text(encoding="utf-8") == "preserved old evidence"


def _refuse(*_args, **_kwargs):
    raise AssertionError("materialization must not render or collect an environment")


def test_snapshot_uses_renderer_attachment_limits(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(render, "MAX_ATTACHMENT_LINES", 2)
    monkeypatch.setattr(render, "MAX_ATTACHMENT_CHARS", 7)
    source = tmp_path / "long.log"
    source.write_text("one\ntwo\nthree\n", encoding="utf-8")

    excerpt, metadata = materialize._excerpt(source)

    assert excerpt == "o\nthree"
    assert (metadata["clipped"], metadata["line_count"]) == (True, 3)


def test_materialize_renders_no_report_and_reads_no_environment(tmp_path, monkeypatch) -> None:
    for name in ("collect_environment", "render_user_report", "render_booley_report"):
        monkeypatch.setattr(render, name, _refuse)
    booley_dir = _project(tmp_path)
    source = tmp_path / "attached.log"
    source.write_text("attached\n", encoding="utf-8")
    append(Finding(title="finding", attachments=[str(source)]), booley_dir)

    assert materialize.materialize_attachments(booley_dir, [source]) == (source,)


def test_materialized_attachment_renders_identical_report_and_export(tmp_path: Path) -> None:
    booley_dir = _project(tmp_path)
    source = tmp_path / "attached.log"
    source.write_text("".join(f"line {n} {'x' * 80}\n" for n in range(200)), encoding="utf-8")
    append(Finding(title="finding", attachments=[str(source)]), booley_dir)
    environment = Environment()

    def views() -> tuple[str, str]:
        log = read_log(booley_dir)
        origin = render.report_origin(log)
        user = render.render_user_report(log, env=environment, origin=origin)
        export = render.render_booley_report(
            log, tmp_path, booley_dir=booley_dir, env=environment, origin=origin
        )
        return user, export.body

    before = views()
    assert materialize.materialize_attachments(booley_dir, [source]) == (source,)
    assert read_log(booley_dir).entries[0].attachments != [str(source)]
    assert views() == before
