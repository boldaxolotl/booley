"""Behavior of the marker-guarded Setup run-root cleanup, driven through the CLI."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

import pytest

from booley.feedback import findings, render
from booley.feedback.findings import Finding, append, log_path, read_log
from booley.harness.setup import cleanup_cli
from tests.conftest import symlink_or_skip

MARKER = ".booley-setup-run"


def _cli(root: Path, capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, dict]:
    """Run ``booley cleanup <argv> --json``; a refusal returns ``{"error": stderr}``."""
    parser = argparse.ArgumentParser()
    cleanup_cli.add_subparser(parser.add_subparsers(dest="command", required=True))
    args = parser.parse_args(["cleanup", *argv, "--json"])
    args.project_root = ""
    code = cleanup_cli.run(args, root)
    out, err = capsys.readouterr()
    return code, json.loads(out) if out.strip() else {"error": err}


def _prepared(tmp_path: Path, capsys, run_id: str = "r1") -> tuple[Path, Path]:
    """A project with one prepared run root; returns ``(booley_dir, run_root)``."""
    booley_dir = tmp_path / ".booley"
    booley_dir.mkdir(exist_ok=True)
    assert _cli(tmp_path, capsys, "prepare", "--run-id", run_id)[1].get("status") == "prepared"
    return booley_dir, booley_dir / "tmp" / "setup" / run_id


def test_prepare_creates_marked_run_root(tmp_path, capsys):
    (tmp_path / ".booley").mkdir()
    code, out = _cli(tmp_path, capsys, "prepare", "--run-id", "r1")
    run_root = tmp_path / ".booley" / "tmp" / "setup" / "r1"
    assert (code, out["status"], out["run_root"]) == (0, "prepared", str(run_root))
    assert (run_root / MARKER).is_file()
    code, generated = _cli(tmp_path, capsys, "prepare")
    assert code == 0 and generated["run_id"] and Path(generated["run_root"]).is_dir()


def test_prepare_rejects_unsafe_and_duplicate_run_ids(tmp_path, capsys):
    (tmp_path / ".booley").mkdir()
    for run_id in (".", "..", "a/b", "é"):
        assert _cli(tmp_path, capsys, "prepare", "--run-id", run_id)[0] == 2
    assert not (tmp_path / ".booley" / "tmp").exists()
    assert _cli(tmp_path, capsys, "prepare", "--run-id", "dup")[1].get("status") == "prepared"
    assert _cli(tmp_path, capsys, "prepare", "--run-id", "dup")[0] == 2


def test_apply_removes_only_the_run_root(tmp_path, capsys):
    booley_dir, run_root = _prepared(tmp_path, capsys)
    _, sibling = _prepared(tmp_path, capsys, "r2")
    outside = tmp_path / "outside.txt"
    other = booley_dir / "tmp" / "other" / "x.log"
    other.parent.mkdir()
    for path in (outside, other, sibling / "keep.log", tmp_path / "booley.toml"):
        path.write_text("keep", encoding="utf-8")
    (run_root / "sub").mkdir()
    (run_root / "sub" / "doctor.log").write_text("transient", encoding="utf-8")
    symlink_or_skip(run_root / "link", outside)

    code, out = _cli(tmp_path, capsys, "apply", "--run-id", "r1")

    assert (code, out["status"], out["kept"]) == (0, "removed", [])
    assert out["bytes"] >= len("transient")
    assert not run_root.exists()
    assert all(
        p.exists() for p in (outside, other, sibling / "keep.log", tmp_path / "booley.toml")
    )


@pytest.mark.parametrize("entry", ["directory", "symlink"])
@pytest.mark.parametrize("link", ["tmp", "tmp/setup", "tmp/setup/r"])
def test_apply_refuses_symlinked_boundaries(tmp_path, capsys, link, entry):
    """A link at ``tmp``, ``tmp/setup`` or the run root is refused and named.

    ``entry`` is the Project's ``.booley``: a real directory, or itself a link to a
    store (followed as the Booley Directory, so the link below it is still the one named).
    """
    real = tmp_path / "real"
    (real / "tmp" / "setup" / "r").mkdir(parents=True)
    (real / "tmp" / "setup" / "r" / MARKER).write_text("x", encoding="utf-8")
    (real / "tmp" / "setup" / "r" / "keep.txt").write_text("keep", encoding="utf-8")
    project = tmp_path / "project"
    project.mkdir()
    booley_dir = project / ".booley"
    if entry == "symlink":
        store = tmp_path / "store"
        store.mkdir()
        symlink_or_skip(booley_dir, store, target_is_directory=True)
    else:
        booley_dir.mkdir()
    # Real directories down to the parent, then one symlink to the real tree.
    parts = Path(link).parts
    holder = booley_dir
    for part in parts[:-1]:
        (holder / part).mkdir()
        holder /= part
    symlink_or_skip(holder / parts[-1], real.joinpath(*parts), target_is_directory=True)

    code, out = _cli(project, capsys, "apply", "--run-id", "r")

    assert code == 2 and f"refusing symlinked path: {booley_dir / link}" in out["error"]
    assert (real / "tmp" / "setup" / "r" / "keep.txt").exists()
    assert (real / "tmp" / "setup" / "r" / MARKER).exists()


def test_symlinked_booley_directory_prepares_previews_and_removes_with_snapshot(tmp_path, capsys):
    """A linked ``.booley`` is followed; attached evidence is still snapshotted first."""
    project = tmp_path / "project"
    project.mkdir()
    store = tmp_path / "store"
    store.mkdir()
    booley_dir = project / ".booley"
    symlink_or_skip(booley_dir, store, target_is_directory=True)
    run_root = booley_dir / "tmp" / "setup" / "r1"
    code, out = _cli(project, capsys, "prepare", "--run-id", "r1")
    assert (code, out.get("status")) == (0, "prepared"), out
    source = run_root / "doctor.log"
    source.write_text("evidence\n", encoding="utf-8")
    append(Finding(title="doctor finding", attachments=[str(source)]), booley_dir)

    code, out = _cli(project, capsys, "preview", "--run-id", "r1")
    assert (code, out["status"]) == (0, "preview")
    assert {row["path"]: row["attached"] for row in out["files"]} == {
        MARKER: False,
        "doctor.log": True,
    }

    code, out = _cli(project, capsys, "apply", "--run-id", "r1")

    assert (code, out["status"], out["kept"]) == (0, "removed", [])
    assert not run_root.exists() and not (store / "tmp" / "setup" / "r1").exists()
    attachment = Path(read_log(booley_dir).entries[0].attachments[0])
    assert "setup-evidence" in str(attachment) and attachment.is_file()
    assert attachment.read_text(encoding="utf-8") == "evidence"
    assert booley_dir.is_symlink()


def test_apply_refuses_unowned_run_root(tmp_path, capsys):
    _, run_root = _prepared(tmp_path, capsys)
    (run_root / MARKER).unlink()
    (run_root / "manifest.json").write_text("{}", encoding="utf-8")
    code, out = _cli(tmp_path, capsys, "apply", "--run-id", "r1")
    assert code == 2 and MARKER in out["error"]
    assert (run_root / "manifest.json").exists()


def test_apply_failure_keeps_marker_for_retry(tmp_path, capsys):
    if os.name == "nt" or os.geteuid() == 0:
        pytest.skip("directory permissions are not enforced here")
    _, run_root = _prepared(tmp_path, capsys)
    locked = run_root / "locked"
    locked.mkdir()
    (locked / "f.log").write_text("x", encoding="utf-8")
    locked.chmod(0o500)
    try:
        code, out = _cli(tmp_path, capsys, "apply", "--run-id", "r1")
        assert code == 1 and out["kept"][0]["reason"]
        assert (run_root / MARKER).is_file()
    finally:
        locked.chmod(0o700)
    assert _cli(tmp_path, capsys, "apply", "--run-id", "r1")[1]["status"] == "removed"


def test_apply_snapshots_attachment_and_renders_identically(tmp_path, capsys):
    booley_dir, run_root = _prepared(tmp_path, capsys)
    source = run_root / "doctor.log"
    source.write_text("".join(f"line {n} {'x' * 80}\n" for n in range(200)), encoding="utf-8")
    finding = append(Finding(title="doctor finding", attachments=[str(source)]), booley_dir)
    environment = render.Environment()

    def views() -> tuple[str, str]:
        log = read_log(booley_dir)
        origin = render.report_origin(log)
        user = render.render_user_report(log, env=environment, origin=origin)
        export = render.render_booley_report(
            log, tmp_path, booley_dir=booley_dir, env=environment, origin=origin
        )
        return user, export.body

    before = views()
    code, out = _cli(tmp_path, capsys, "apply", "--run-id", "r1")

    assert (code, out["status"]) == (0, "removed")
    assert not source.exists()
    entry = read_log(booley_dir).entries[0]
    assert entry.id == finding.id and Path(entry.attachments[0]).is_file()
    assert "setup-evidence" in entry.attachments[0]
    assert views() == before


def test_apply_keeps_run_root_for_corrupt_findings_log(tmp_path, capsys):
    booley_dir, run_root = _prepared(tmp_path, capsys)
    attached, unrelated = run_root / "attached.log", run_root / "unrelated.log"
    for path in (attached, unrelated):
        path.write_text("evidence\n", encoding="utf-8")
    append(Finding(title="attached", attachments=[str(attached)]), booley_dir)
    with log_path(booley_dir).open("a", encoding="utf-8") as stream:
        stream.write("not json\n")
    before = log_path(booley_dir).read_bytes()

    code, out = _cli(tmp_path, capsys, "apply", "--run-id", "r1")

    assert (code, out["status"]) == (1, "kept")
    assert "corrupt" in out["kept"][0]["reason"] and out["kept"][0]["path"] == str(attached)
    assert attached.exists() and unrelated.exists() and (run_root / MARKER).exists()
    assert log_path(booley_dir).read_bytes() == before


def test_apply_keeps_run_root_when_findings_lock_times_out(tmp_path, capsys, monkeypatch):
    from tests.feedback.test_findings import holding_findings_lock

    booley_dir, run_root = _prepared(tmp_path, capsys)
    source = run_root / "doctor.log"
    source.write_text("retain evidence\n", encoding="utf-8")
    append(Finding(title="attached", attachments=[str(source)]), booley_dir)
    before = log_path(booley_dir).read_bytes()
    monkeypatch.setattr(findings, "_LOCK_TIMEOUT_S", 0.01)

    with holding_findings_lock(booley_dir):
        code, out = _cli(tmp_path, capsys, "apply", "--run-id", "r1")

    assert (code, out["status"], out["kept"][0]["path"]) == (1, "kept", str(source))
    assert source.read_text(encoding="utf-8") == "retain evidence\n"
    assert log_path(booley_dir).read_bytes() == before
    assert not (booley_dir / "setup-evidence").exists()


def test_apply_retains_concurrent_findings_append(tmp_path, capsys):
    from tests.feedback.test_findings import _race_feedback_append

    booley_dir, run_root = _prepared(tmp_path, capsys, "race")
    source = run_root / "capture.log"
    source.write_text("evidence\n", encoding="utf-8")
    append(Finding(title="original", attachments=[str(source)]), booley_dir)

    _race_feedback_append(booley_dir, "cleanup_apply")

    log = read_log(booley_dir)
    assert [entry.title for entry in log.entries] == ["original", "concurrent"]
    assert Path(log.entries[0].attachments[0]).read_text(encoding="utf-8") == "evidence"
    assert not run_root.exists()


def test_apply_materializes_duplicate_finding_ids(tmp_path, capsys):
    booley_dir, run_root = _prepared(tmp_path, capsys)
    rows = []
    for ordinal in range(2):
        source = run_root / str(ordinal) / "capture.log"
        source.parent.mkdir()
        source.write_text(f"evidence {ordinal}\n", encoding="utf-8")
        rows.append(Finding(title=f"row {ordinal}", id="F-1", attachments=[str(source)]))
    log_path(booley_dir).write_text(
        "".join(row.to_json() + "\n" for row in rows), encoding="utf-8"
    )

    code, out = _cli(tmp_path, capsys, "apply", "--run-id", "r1")

    assert (code, out["status"]) == (0, "removed") and not run_root.exists()
    reread = read_log(booley_dir).entries
    snapshots = [Path(entry.attachments[0]) for entry in reread]
    assert [entry.id for entry in reread] == ["F-1", "F-1"] and len(set(snapshots)) == 2
    assert [path.read_text(encoding="utf-8") for path in snapshots] == ["evidence 0", "evidence 1"]


def test_repeated_apply_reports_root_already_gone(tmp_path, capsys):
    _prepared(tmp_path, capsys)
    assert _cli(tmp_path, capsys, "apply", "--run-id", "r1")[1]["status"] == "removed"
    code, out = _cli(tmp_path, capsys, "apply", "--run-id", "r1")
    assert (code, out["status"], out["kept"]) == (0, "gone", [])


def test_preview_lists_files_and_mutates_nothing(tmp_path, capsys):
    booley_dir, run_root = _prepared(tmp_path, capsys)
    attached = run_root / "a.log"
    attached.write_text("abc", encoding="utf-8")
    (run_root / "b.log").write_text("de", encoding="utf-8")
    append(Finding(title="attached", attachments=[str(attached)]), booley_dir)
    tree = sorted(p.relative_to(tmp_path) for p in tmp_path.rglob("*"))
    log = log_path(booley_dir).read_bytes()

    code, out = _cli(tmp_path, capsys, "preview", "--run-id", "r1")

    assert (code, out["status"]) == (0, "preview")
    assert {row["path"]: row["attached"] for row in out["files"]} == {
        MARKER: False,
        "a.log": True,
        "b.log": False,
    }
    assert out["bytes"] == sum(row["bytes"] for row in out["files"])
    assert sorted(p.relative_to(tmp_path) for p in tmp_path.rglob("*")) == tree
    assert log_path(booley_dir).read_bytes() == log
    assert _cli(tmp_path, capsys, "preview", "--run-id", "absent")[1]["status"] == "gone"


def test_apply_restores_marker_when_final_rmdir_fails(tmp_path, capsys, monkeypatch):
    """A file appearing at the last moment must leave the root retryable, not unowned."""
    _, run_root = _prepared(tmp_path, capsys)
    real_rmdir = Path.rmdir

    def failing_rmdir(path: Path) -> None:
        if path == run_root:
            raise OSError("directory not empty")
        real_rmdir(path)

    monkeypatch.setattr(Path, "rmdir", failing_rmdir)
    code, out = _cli(tmp_path, capsys, "apply", "--run-id", "r1")
    assert (code, out["status"]) == (1, "kept") and (run_root / MARKER).is_file()

    monkeypatch.undo()
    assert _cli(tmp_path, capsys, "apply", "--run-id", "r1")[1]["status"] == "removed"
    assert not run_root.exists()


def test_apply_keeps_root_when_log_gains_corrupt_line_during_snapshot(
    tmp_path, capsys, monkeypatch
):
    """A half-written concurrent append (corrupt on re-read) must block deletion."""
    booley_dir, run_root = _prepared(tmp_path, capsys)
    source = run_root / "doctor.log"
    source.write_text("evidence\n", encoding="utf-8")
    append(Finding(title="attached", attachments=[str(source)]), booley_dir)
    real = cleanup_cli.run_root_cleanup.materialize.materialize_attachments

    def materialize_then_corrupt(*args, **kwargs):
        real(*args, **kwargs)
        with log_path(booley_dir).open("a", encoding="utf-8") as stream:
            stream.write('{"title": "half-written\n')

    monkeypatch.setattr(
        cleanup_cli.run_root_cleanup.materialize,
        "materialize_attachments",
        materialize_then_corrupt,
    )
    code, out = _cli(tmp_path, capsys, "apply", "--run-id", "r1")
    assert (code, out["status"]) == (1, "kept") and (run_root / MARKER).is_file()


def test_apply_refuses_unreadable_findings_log_without_deleting(tmp_path, capsys):
    booley_dir, run_root = _prepared(tmp_path, capsys)
    (run_root / "keep.log").write_text("x", encoding="utf-8")
    log_path(booley_dir).write_bytes(b"\xff\xfe not utf-8\n")

    code, out = _cli(tmp_path, capsys, "apply", "--run-id", "r1")

    assert code == 2 and "error" in out
    assert (run_root / "keep.log").exists() and (run_root / MARKER).exists()


def test_apply_refuses_when_booley_directory_cannot_be_resolved(tmp_path, capsys, monkeypatch):
    _, run_root = _prepared(tmp_path, capsys)

    def fail(_root: Path) -> Path:
        raise RuntimeError("layout is ambiguous")

    monkeypatch.setattr(cleanup_cli.run_root_cleanup, "resolve_checkout_booley_dir", fail)
    code, out = _cli(tmp_path, capsys, "apply", "--run-id", "r1")
    assert code == 2 and "cannot resolve" in out["error"] and (run_root / MARKER).exists()


def test_prepare_refuses_when_tmp_is_a_file(tmp_path, capsys):
    booley_dir = tmp_path / ".booley"
    booley_dir.mkdir()
    (booley_dir / "tmp").write_text("not a directory", encoding="utf-8")
    code, out = _cli(tmp_path, capsys, "prepare", "--run-id", "r1")
    assert code == 2 and "error" in out
