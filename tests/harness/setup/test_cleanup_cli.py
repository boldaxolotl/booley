"""CLI adapter coverage for manifest-owned cleanup."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from booley.feedback.findings import Finding, append
from booley.harness.setup import cleanup as cleanup_module
from booley.harness.setup import cleanup_cli
from booley.harness.setup.cleanup import prepare_run, preview_cleanup, record_artifact


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    cleanup_cli.add_subparser(commands)
    return parser


def test_cleanup_cli_runs_prepare_record_preview_and_apply(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = tmp_path / ".booley"
    project.mkdir()
    parser = _parser()

    args = parser.parse_args(
        ["cleanup", "--project-root", str(tmp_path), "prepare", "--run-id", "cli", "--json"]
    )
    assert cleanup_cli.run(args, tmp_path) == 0
    assert "scratch_root" in json.loads(capsys.readouterr().out)

    capture = project / "tmp" / "setup" / "cli" / "capture.log"
    capture.write_text("capture\n", encoding="utf-8")
    args = parser.parse_args(
        [
            "cleanup",
            "--project-root",
            str(tmp_path),
            "record",
            str(capture),
            "--run-id",
            "cli",
            "--producer",
            "doctor",
            "--class",
            "capture",
            "--job-record",
            "job.json",
        ]
    )
    assert cleanup_cli.run(args, tmp_path) == 0
    assert "capture.log" in capsys.readouterr().out

    plan = preview_cleanup(tmp_path, run_id="cli")
    args = parser.parse_args(
        ["cleanup", "--project-root", str(tmp_path), "preview", "--run-id", "cli", "--json"]
    )
    assert cleanup_cli.run(args, tmp_path) == 0
    assert json.loads(capsys.readouterr().out)["digest"] == plan.digest

    args = parser.parse_args(
        [
            "cleanup",
            "--project-root",
            str(tmp_path),
            "apply",
            "--run-id",
            "cli",
            "--digest",
            plan.digest,
            "--json",
        ]
    )
    assert cleanup_cli.run(args, tmp_path) == 0
    assert json.loads(capsys.readouterr().out)["unresolved"] == []


def test_cleanup_cli_reports_stale_digest(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / ".booley").mkdir()
    prepare_run(tmp_path, "cli-stale")
    args = _parser().parse_args(
        [
            "cleanup",
            "--project-root",
            str(tmp_path),
            "apply",
            "--run-id",
            "cli-stale",
            "--digest",
            "stale",
        ]
    )

    assert cleanup_cli.run(args, tmp_path) == 2
    assert "stale" in capsys.readouterr().err


def test_cleanup_cli_rejects_unknown_operation(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / ".booley").mkdir()
    args = argparse.Namespace(cleanup_command="unknown", project_root="")

    assert cleanup_cli.run(args, tmp_path) == 2
    assert "operation is required" in capsys.readouterr().err


def _run(root, operation, digest=None, as_json=False, **options):
    words = ["cleanup", "--project-root", str(root), operation, "--run-id", "check"]
    if digest is not None:
        words += ["--digest", digest]
    if as_json:
        words += ["--json"]
    for name, value in options.items():
        words += ["--" + name.replace("_", "-"), value]
    return cleanup_cli.run(_parser().parse_args(words), root)


def _setup(root):
    (root / ".booley").mkdir()
    return prepare_run(root, "check")


def _record(root, path, **kwargs):
    path.write_text("evidence\n")
    return record_artifact(
        root, "check", path, producer="doctor", artifact_class="capture", **kwargs
    )


def _snapshot(root):
    return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def test_cleanup_apply_prints_each_unresolved_path_and_reason(tmp_path, capsys):
    scratch = _setup(tmp_path)
    (scratch / "unknown").write_text("keep")
    assert _run(tmp_path, "preview", as_json=True) == 0
    plan = json.loads(capsys.readouterr().out)
    assert _run(tmp_path, "apply", plan["digest"]) == 1
    output = capsys.readouterr().out
    assert (
        ".booley/tmp/setup/check: scratch root still contains retained or unmanifested data"
        in output
    ), "apply output must name the unresolved path and reason"


@pytest.mark.parametrize("already_unresolved", [False, True], ids=["matching", "mismatched"])
def test_changed_identity_rejects_entire_plan_before_mutation(
    tmp_path, capsys, monkeypatch, already_unresolved
):
    from booley.flows import edam

    scratch = _setup(tmp_path)
    safe = scratch / "a-safe"
    changed = scratch / "z-changed"
    _record(tmp_path, safe)
    _record(tmp_path, changed)
    cache = tmp_path / ".booley" / "cache"
    cache.write_text("cache")
    record_artifact(tmp_path, "check", cache, producer="flow", artifact_class="flow-cache")
    append(Finding(title="evidence", attachments=[str(safe)]), tmp_path / ".booley")
    if already_unresolved:
        changed.write_text("first replacement")
    plan = preview_cleanup(tmp_path, run_id="check", cache_disposition="evict-setup-touched")
    changed.write_text("second replacement with different size")
    before = _snapshot(tmp_path)
    calls = []
    lease = edam.try_work_root_lease
    materialize = cleanup_module.materialize.materialize_attachments

    def track_lease(*args, **kwargs):
        calls.append("lease")
        return lease(*args, **kwargs)

    def track_materialize(*args, **kwargs):
        calls.append("materialize")
        return materialize(*args, **kwargs)

    monkeypatch.setattr(edam, "try_work_root_lease", track_lease)
    monkeypatch.setattr(cleanup_module.materialize, "materialize_attachments", track_materialize)
    status = _run(tmp_path, "apply", plan.digest, cache_disposition="evict-setup-touched")
    assert status == 2, "identity change must reject the entire saved plan before mutation"
    assert "stale" in capsys.readouterr().err
    assert _snapshot(tmp_path) == before
    assert calls == []
    assert _run(tmp_path, "apply", plan.digest, cache_disposition="evict-setup-touched") == 2
    assert "fresh preview" in capsys.readouterr().err


@pytest.mark.parametrize("change", ["add", "remove", "replace"])
def test_directory_content_change_rejects_plan_even_if_still_unresolved(tmp_path, capsys, change):
    scratch = _setup(tmp_path)
    safe = scratch / "safe"
    _record(tmp_path, safe)
    unknown = scratch / "unknown"
    unknown.write_text("keep")
    plan = preview_cleanup(tmp_path, run_id="check")
    if change == "add":
        (scratch / "new").write_text("new")
    elif change == "remove":
        unknown.unlink()
    else:
        unknown.write_text("different size")
    before = _snapshot(tmp_path)
    status = _run(tmp_path, "apply", plan.digest)
    assert status == 2, "directory content change must reject the saved plan before mutation"
    assert "preview" in capsys.readouterr().err
    assert _snapshot(tmp_path) == before
    assert preview_cleanup(tmp_path, run_id="check").digest != plan.digest


@pytest.mark.parametrize("as_json", [False, True], ids=["text", "json"])
def test_preview_unresolved_active_path_sets_exit_one(tmp_path, capsys, as_json):
    _setup(tmp_path)
    entry = _record(tmp_path, tmp_path / ".booley" / "active", active=True)
    plan = preview_cleanup(tmp_path, run_id="check")
    status = _run(tmp_path, "apply", plan.digest, as_json)
    assert status == 1, "every unresolved result must set exit one"
    output = capsys.readouterr().out
    rows = json.loads(output)["unresolved"] if as_json else output.splitlines()
    assert any(f"{entry.path}: active-use claim" in row for row in rows)


def test_growing_active_entry_stales_entire_plan(tmp_path, capsys):
    scratch = _setup(tmp_path)
    safe = scratch / "safe"
    _record(tmp_path, safe)
    active = tmp_path / ".booley" / "active"
    _record(tmp_path, active, active=True)
    plan = preview_cleanup(tmp_path, run_id="check")
    active.write_text("actively growing log")
    before = _snapshot(tmp_path)
    assert _run(tmp_path, "apply", plan.digest) == 2
    assert "preview" in capsys.readouterr().err
    assert _snapshot(tmp_path) == before
    fresh = preview_cleanup(tmp_path, run_id="check")
    assert _run(tmp_path, "apply", fresh.digest) == 1
    assert "active-use claim" in capsys.readouterr().out
    assert not safe.exists()
    assert active.exists()


def test_preview_unresolved_keeps_preview_exit_zero(tmp_path, capsys):
    scratch = _setup(tmp_path)
    _record(tmp_path, scratch / "active", active=True)
    (scratch / "unknown").write_text("retain")
    assert _run(tmp_path, "preview") == 0
    output = capsys.readouterr().out
    assert "active-use claim" in output
    assert "retained or unmanifested" in output


def test_mutation_time_failure_permits_partial_cleanup(tmp_path, capsys, monkeypatch):
    scratch = _setup(tmp_path)
    safe = scratch / "safe"
    changed = scratch / "changed"
    _record(tmp_path, safe)
    _record(tmp_path, changed)
    plan = preview_cleanup(tmp_path, run_id="check")
    original = cleanup_module._load_plan_manifest

    def change_after_validation(plan):
        loaded = original(plan)
        changed.write_text("replacement after digest check")
        return loaded

    monkeypatch.setattr(cleanup_module, "_load_plan_manifest", change_after_validation)
    assert _run(tmp_path, "apply", plan.digest) == 1
    output = capsys.readouterr().out
    assert "changed: identity changed" in output
    assert "scratch root still contains" in output
    assert not safe.exists()
    assert changed.exists()


@pytest.mark.parametrize("retention", ["minimal", "diagnostic"])
@pytest.mark.parametrize("as_json", [False, True], ids=["text", "json"])
def test_policy_retained_parent_sets_exit_one(tmp_path, capsys, retention, as_json):
    scratch = _setup(tmp_path)
    source = scratch / "retained"
    source.write_text("retain")
    record_artifact(
        tmp_path,
        "check",
        source,
        producer="doctor",
        artifact_class="raw-evidence",
        disposition="preserve" if retention == "minimal" else "remove",
    )
    plan = preview_cleanup(tmp_path, run_id="check", retention_mode=retention)
    assert plan.items[-1].category == "unresolved"
    assert _run(tmp_path, "apply", plan.digest, as_json, retention=retention) == 1
    output = capsys.readouterr().out
    assert "retained or unmanifested" in output
    assert source.exists()


@pytest.mark.parametrize("state", [None, {"quarantine": 123}, {"status": []}])
def test_malformed_journal_returns_invalid_request(tmp_path, capsys, state):
    scratch = _setup(tmp_path)
    entry = _record(tmp_path, scratch / "capture")
    manifest = scratch / "manifest.json"
    payload = json.loads(manifest.read_text())
    payload["journal"][entry.path] = state
    manifest.write_text(json.dumps(payload))
    before = _snapshot(tmp_path)
    try:
        status = _run(tmp_path, "preview")
    except (AttributeError, TypeError):
        status = -1
    assert status == 2, "malformed recovery journal must return invalid-request exit two"
    assert "journal" in capsys.readouterr().err
    assert _snapshot(tmp_path) == before


def test_refused_symlink_replacement_stales_plan_before_mutation(tmp_path, capsys):
    scratch = _setup(tmp_path)
    candidate = tmp_path / ".booley" / "candidate"
    _record(tmp_path, candidate)
    safe = scratch / "safe"
    _record(tmp_path, safe)
    first = tmp_path / ".booley" / "first"
    second = tmp_path / ".booley" / "second"
    first.write_text("first")
    second.write_text("second")
    candidate.unlink()
    candidate.symlink_to(first)
    plan = preview_cleanup(tmp_path, run_id="check")
    candidate.rename(tmp_path / ".booley" / "old-link")
    candidate.symlink_to(second)
    before = _snapshot(tmp_path)
    assert _run(tmp_path, "apply", plan.digest) == 2, (
        "refused identity replacement must stale the whole plan"
    )
    assert "preview" in capsys.readouterr().err
    assert _snapshot(tmp_path) == before
    assert safe.exists() and candidate.is_symlink()


@pytest.mark.parametrize("deny_at", [0, 1, 2, 3])
def test_manifest_write_denial_reports_every_unresolved_path(
    tmp_path, monkeypatch, capsys, deny_at
):
    scratch = _setup(tmp_path)
    entry = _record(tmp_path, scratch / "capture")
    if deny_at == 0:
        monkeypatch.setattr(cleanup_module.os, "access", lambda *args: False)
    plan = preview_cleanup(tmp_path, run_id="check")
    before = _snapshot(tmp_path)

    original = cleanup_module._atomic_write
    calls = 0

    def denied(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls >= deny_at:
            raise PermissionError("journal write denied")
        original(*args, **kwargs)

    monkeypatch.setattr(cleanup_module, "_atomic_write", denied)
    try:
        status = _run(tmp_path, "apply", plan.digest)
    except OSError:
        status = -1
    assert status == 1, "journal write failure must report unresolved exit one"
    output = capsys.readouterr().out
    assert entry.path in output and str(scratch.relative_to(tmp_path)) in output
    assert ("not writable or searchable" if deny_at == 0 else "journal write denied") in output
    if deny_at == 0:
        assert calls == 0
    if deny_at < 3:
        after = _snapshot(tmp_path)
        assert all(after.get(name) == content for name, content in before.items())
    else:
        assert not (scratch / "capture").exists() and scratch.exists()
