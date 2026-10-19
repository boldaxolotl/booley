"""Job artifact refresh retention and Project boundary regressions."""

import json
import os
from pathlib import Path

import pytest

from booley.flows.progress_lifecycle import progress_document
from booley.runtime.job_observation import _artifacts as job_artifacts
from booley.runtime.job_observation._artifacts import JobArtifactCache


def report(root: Path, endpoint: str, number: int, namespace: str = "flow-reports") -> Path:
    path = root.parent / namespace / endpoint / str(number) / "report.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"run_id": str(number), "passed": True}))
    return path


def progress(path, run="1"):
    data = progress_document(
        flow="sim", run_id=run, phase="complete", targets=[], completed_targets=[], detail={}
    )
    path.write_text(json.dumps(data))
    return path


def spy(monkeypatch, owner, name):
    original, calls = getattr(owner, name), []

    def call(*args, **kwargs):
        calls.append(args[0])
        return original(*args, **kwargs)

    monkeypatch.setattr(owner, name, call)
    return calls


def test_multi_root_history_reuses_parses(tmp_path, monkeypatch):
    monkeypatch.setattr(job_artifacts, "MAX_ARTIFACTS", 5)
    roots = [tmp_path / str(index) / "jobs" for index in range(3)]
    paths = [
        report(root, "sim", n, "flow-reports" if n % 2 else "mcp-tool-reports")
        for root in roots
        for n in range(4)
    ]
    checkpoints = [progress(root.parent / "flow-reports/sim/1/progress.json") for root in roots]
    before = {p: p.read_bytes() for p in paths + checkpoints}
    reads = spy(monkeypatch, Path, "read_bytes")
    progress_reads = spy(monkeypatch, job_artifacts, "read_progress_document")
    cache = JobArtifactCache(tmp_path)
    for refresh in range(3):
        cache.begin()
        for root in roots:
            assert cache.find(root, "sim", "1", "report") is not None
            assert cache.find(root, "sim", "absent", "progress") is None
        cache.complete()
        expected = paths if refresh == 0 else [paths[0]] if refresh == 2 else []
        assert sorted(reads) == sorted(expected)
        assert len(progress_reads) == (3 if refresh == 0 else 0)
        reads.clear()
        progress_reads.clear()
        if refresh == 1:
            info = paths[0].stat()
            os.utime(paths[0], ns=(info.st_atime_ns, info.st_mtime_ns + 1_000_000_000))
            assert paths[0].stat().st_mtime_ns != info.st_mtime_ns
    assert {p: p.read_bytes() for p in before} == before


def test_missing_progress_does_not_reparse_unrelated_file(tmp_path, monkeypatch):
    root = tmp_path / "jobs"
    path = report(root, "sim", 1).with_name("progress.json")
    progress(path, "other")
    reads = spy(monkeypatch, job_artifacts, "read_progress_document")
    cache = JobArtifactCache(tmp_path)
    cache.begin()
    assert cache.find(root, "sim", "absent", "progress") is None
    cache.complete()
    reads.clear()
    cache.begin()
    assert cache.find(root, "sim", "absent", "progress") is None
    assert reads == []


def test_project_boundary_accepts_only_links_above_project(tmp_path):
    real = tmp_path / "real"
    root = real / "project" / "jobs"
    report(root, "sim", 1)
    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)
    cache = JobArtifactCache(alias / "project")
    cache.begin()
    assert cache.find(alias / "project/jobs", "sim", "1", "report") is not None
    linked_project = tmp_path / "linked"
    linked_project.symlink_to(real / "project", target_is_directory=True)
    cache = JobArtifactCache(linked_project)
    cache.begin()
    assert cache.find(linked_project / "jobs", "sim", "1", "report") is None


@pytest.mark.parametrize("kind", ["report", "progress"])
def test_transient_report_and_progress_io_is_retried(tmp_path, monkeypatch, kind):
    from booley.flows import progress_lifecycle

    root = tmp_path / "jobs"
    path = report(root, "sim", 1)
    progress(path.with_name("progress.json"))
    owner, name = (
        (Path, "read_bytes")
        if kind == "report"
        else (progress_lifecycle, "_read_json_object_nofollow")
    )
    cache = JobArtifactCache(tmp_path)
    with monkeypatch.context() as fault:

        def read(*args, **kwargs):
            raise OSError("transient I/O")

        fault.setattr(owner, name, read)
        cache.begin()
        assert cache.find(root, "sim", "1", kind) is None
        cache.complete()
    cache.begin()
    assert cache.find(root, "sim", "1", kind) is not None


@pytest.mark.parametrize("file_limit,byte_limit", [(2, 100000), (100, 80)])
def test_scan_bounds(tmp_path, monkeypatch, file_limit, byte_limit):
    monkeypatch.setattr(job_artifacts, "MAX_ARTIFACTS", file_limit)
    monkeypatch.setattr(job_artifacts, "MAX_ARTIFACT_BYTES", byte_limit)
    root = tmp_path / "jobs"
    for n in range(3):
        report(root, "sim", n)
    reads = spy(monkeypatch, Path, "read_bytes")
    cache = JobArtifactCache(tmp_path)
    cache.begin()
    cache.find(root, "sim", "1", "report")
    assert len(reads) == 2
    assert cache.diagnostics == [f"Job artifact history exceeds scan bound: {root}/sim"]


def test_invalid_documents_reused_until_metadata_changes(tmp_path, monkeypatch):
    root = tmp_path / "jobs"
    path = report(root, "sim", 1)
    path.write_text("invalid")
    reads = spy(monkeypatch, Path, "read_bytes")
    cache = JobArtifactCache(tmp_path)
    for _ in range(2):
        cache.begin()
        assert cache.find(root, "sim", "1", "report") is None
        cache.complete()
    assert reads == [path]
    report(root, "sim", 1)
    cache.begin()
    assert cache.find(root, "sim", "1", "report") is not None
    assert reads == [path, path]


def test_directory_checks_shared_once_and_refreshed_after_link_change(tmp_path, monkeypatch):
    root = tmp_path / "logs/.runtime/jobs"
    paths = [report(root, endpoint, 1) for endpoint in ("sim", "lint")]
    original = Path.lstat
    counts = {}
    directories = {
        parent for path in paths for parent in path.parents if parent.is_relative_to(tmp_path)
    }

    def lstat(path, *args, **kwargs):
        if path in directories:
            counts[path] = counts.get(path, 0) + 1
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "lstat", lstat)
    cache = JobArtifactCache(tmp_path)
    cache.begin()
    for endpoint in ("sim", "lint"):
        assert cache.find(root, endpoint, "1", "report") is not None
    assert all(value == 1 for value in counts.values())
    namespace = root.parent / "flow-reports"
    moved = root.parent / "moved"
    namespace.rename(moved)
    namespace.symlink_to(moved, target_is_directory=True)
    cache.begin()
    assert cache.find(root, "sim", "1", "report") is None


@pytest.mark.parametrize("component", ["runtime", "namespace", "endpoint", "run", "file"])
def test_internal_symlinks_refused_including_complete_fallback(tmp_path, monkeypatch, component):
    root = tmp_path / "logs/.runtime/jobs"
    path = report(root, "sim", 1)
    checkpoint = progress(path.with_name("progress.json"))
    source = {
        "runtime": path.parents[3],
        "namespace": path.parents[2],
        "endpoint": path.parents[1],
        "run": path.parent,
        "file": checkpoint,
    }[component]
    moved = tmp_path / "moved"
    source.rename(moved)
    source.symlink_to(moved, target_is_directory=component != "file")
    monkeypatch.setattr(job_artifacts, "MAX_ARTIFACTS", 0)
    cache = JobArtifactCache(tmp_path)
    cache.begin()
    assert cache.find(root, "sim", "1", "progress") is None


def test_relative_lexical_project_spelling_and_outside_root(tmp_path, monkeypatch):
    project = tmp_path / "project"
    root = project / "jobs"
    report(root, "sim", 1)
    monkeypatch.chdir(tmp_path)
    cache = JobArtifactCache(Path("unused/../project"))
    cache.begin()
    assert cache.find(Path("project/jobs"), "sim", "1", "report") is not None
    outside = tmp_path / "outside/jobs"
    report(outside, "sim", 1)
    assert cache.find(outside, "sim", "1", "report") is None
    assert f"Job artifact root unavailable: {outside}" in cache.diagnostics


def test_symlinked_report_file_and_non_directory_ancestor_refused(tmp_path):
    root = tmp_path / "jobs"
    path = report(root, "sim", 1)
    moved = tmp_path / "payload.json"
    path.rename(moved)
    path.symlink_to(moved)
    cache = JobArtifactCache(tmp_path)
    cache.begin()
    assert cache.find(root, "sim", "1", "report") is None
    path.unlink()
    path.symlink_to(tmp_path / "missing.json")
    cache.begin()
    assert cache.find(root, "sim", "1", "report") is None
    path.unlink()
    path.parent.rmdir()
    path.parent.write_text("not a directory")
    cache.begin()
    assert cache.find(root, "sim", "1", "report") is None


def test_complete_fallback_memo_is_ephemeral(tmp_path, monkeypatch):
    root = tmp_path / "jobs"
    for number in range(8):
        path = report(root, "sim", number).with_name("progress.json")
        progress(path, str(number))
        os.utime(path, (100 + number, 100 + number))
    monkeypatch.setattr(job_artifacts, "MAX_ARTIFACTS", 1)
    calls = spy(monkeypatch, job_artifacts, "read_progress_for_run")
    cache = JobArtifactCache(tmp_path)
    cache.begin()
    assert cache.find(root, "sim", "0", "progress") is not None
    assert cache.find(root, "sim", "0", "progress") is not None
    assert cache.find(root, "sim", "missing", "progress") is None
    assert cache.find(root, "sim", "missing", "progress") is None
    assert len(calls) == 2
    cache.begin()
    assert cache.find(root, "sim", "0", "progress") is not None
    assert len(calls) == 3


def test_shared_refuse_symlinks_keeps_whole_ancestor_guard(tmp_path):
    from booley.runtime.safe_storage import refuse_symlinks

    real = tmp_path / "real"
    root = real / "project/jobs"
    path = report(root, "sim", 1)
    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)
    with pytest.raises(OSError):
        refuse_symlinks(alias / path.relative_to(real))


@pytest.mark.parametrize("component", ["endpoint", "run"])
def test_indexed_report_rejects_linked_directories(tmp_path, component):
    root = tmp_path / "jobs"
    path = report(root, "sim", 1)
    directory = path.parent if component == "run" else path.parents[1]
    moved = tmp_path / "moved"
    directory.rename(moved)
    directory.symlink_to(moved, target_is_directory=True)
    cache = JobArtifactCache(tmp_path)
    cache.begin()
    assert cache.find(root, "sim", "1", "report") is None
