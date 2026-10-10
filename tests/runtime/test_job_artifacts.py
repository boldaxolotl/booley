"""Job artifact refresh retention and Project boundary regressions."""

import json
import os
from pathlib import Path

import pytest

from booley.flows.progress_lifecycle import progress_document
from booley.runtime import job_artifacts
from booley.runtime.job_artifacts import JobArtifactCache


def report(root: Path, endpoint: str, number: int, namespace: str = "flow-reports") -> Path:
    path = root.parent / namespace / endpoint / str(number) / "report.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"run_id": str(number), "passed": True}))
    return path


def progress_read_spy(monkeypatch):
    original_progress = job_artifacts.read_progress_document
    progress_reads = []

    def read_progress(*args, **kwargs):
        progress_reads.append(args[0])
        return original_progress(*args, **kwargs)

    monkeypatch.setattr(job_artifacts, "read_progress_document", read_progress)
    return progress_reads


def report_decode_spy(monkeypatch):
    decodes = []
    original_loads = json.loads

    def loads(*args, **kwargs):
        data = original_loads(*args, **kwargs)
        if isinstance(data, dict) and "passed" in data:
            decodes.append(data)
        return data

    monkeypatch.setattr(json, "loads", loads)

    return decodes


# Real filesystem history: 36.84 s on Windows CI and >60 s under coverage.
@pytest.mark.timeout(180)
def test_real_combined_history_reuses_6000_reports(tmp_path, monkeypatch):
    roots = [tmp_path / str(index) / "jobs" for index in range(3)]
    paths = [
        report(root, "sim", number, "flow-reports" if number % 2 else "mcp-tool-reports")
        for root in roots
        for number in range(2000)
    ]
    progress_paths = [root.parent / "flow-reports/sim/1/progress.json" for root in roots]
    for path in progress_paths:
        path.write_text(
            json.dumps(
                progress_document(
                    flow="sim",
                    run_id="1",
                    phase="complete",
                    targets=[],
                    completed_targets=[],
                    detail={},
                )
            )
        )
    progress_reads = progress_read_spy(monkeypatch)
    original = Path.read_bytes
    reads = []
    decodes = report_decode_spy(monkeypatch)

    def read(path):
        if path in selected:
            reads.append(path)
        return original(path)

    selected = set(paths)
    before = {path: original(path) for path in paths + progress_paths}
    monkeypatch.setattr(Path, "read_bytes", read)
    cache = JobArtifactCache()
    cache.configure_project(tmp_path)
    for refresh in range(3):
        cache.begin()
        for root in roots:
            assert cache.find(root, "sim", "1", "report") is not None
            assert cache.find(root, "sim", "absent", "progress") is None
        cache.complete()
        if refresh == 0:
            assert len(reads) == len(decodes) == 6000
            assert len(progress_reads) == 3
            reads.clear()
            decodes.clear()
            progress_reads.clear()
        elif refresh == 1:
            assert reads == decodes == []
            assert progress_reads == []
            info = paths[0].stat()
            # NTFS timestamps have 100 ns resolution; guarantee a real change.
            os.utime(paths[0], ns=(info.st_atime_ns, info.st_mtime_ns + 1_000_000_000))
            assert paths[0].stat().st_mtime_ns != info.st_mtime_ns
        else:
            assert reads == [paths[0]]
            assert len(decodes) == 1
            assert progress_reads == []
    assert {path: original(path) for path in before} == before


def test_missing_progress_does_not_reparse_unrelated_file(tmp_path, monkeypatch):
    root = tmp_path / "jobs"
    path = report(root, "sim", 1).with_name("progress.json")
    path.write_text(
        json.dumps(
            progress_document(
                flow="sim",
                run_id="other",
                phase="complete",
                targets=[],
                completed_targets=[],
                detail={},
            )
        )
    )
    original = job_artifacts.read_progress_document
    reads = []

    def read(*args, **kwargs):
        reads.append(args[0])
        return original(*args, **kwargs)

    monkeypatch.setattr(job_artifacts, "read_progress_document", read)
    cache = JobArtifactCache()
    cache.configure_project(tmp_path)
    cache.begin()
    assert cache.find(root, "sim", "absent", "progress") is None
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
    cache = JobArtifactCache()
    cache.configure_project(alias / "project")
    cache.begin()
    assert cache.find(alias / "project/jobs", "sim", "1", "report") is not None
    linked_project = tmp_path / "linked"
    linked_project.symlink_to(real / "project", target_is_directory=True)
    cache.configure_project(linked_project)
    cache.begin()
    assert cache.find(linked_project / "jobs", "sim", "1", "report") is None


def test_completed_generation_survives_failed_refresh_and_sweeps_dead_shards(
    tmp_path, monkeypatch
):
    roots = [tmp_path / str(i) / "jobs" for i in range(3)]
    for root in roots:
        report(root, "sim", 1)
    cache = JobArtifactCache()
    cache.configure_project(tmp_path, max_endpoints=2)
    cache.begin()
    for root in roots[:2]:
        cache.find(root, "sim", "1", "report")
    cache.complete()
    cache.begin()  # A projection failure before touching anything does not commit.
    cache.begin()
    monkeypatch.setattr(
        Path, "read_bytes", lambda _: (_ for _ in ()).throw(AssertionError("reparse"))
    )
    for root in roots[:2]:
        assert cache.find(root, "sim", "1", "report") is not None
    cache.complete()
    cache.begin()
    cache.find(roots[0], "sim", "1", "report")
    cache.complete()
    cache.begin()
    assert len(cache._shards) == 1


def test_transient_report_and_progress_io_is_retried(tmp_path, monkeypatch):
    root = tmp_path / "jobs"
    report_path = report(root, "sim", 1)
    progress = report_path.with_name("progress.json")
    progress.write_text(
        json.dumps(
            progress_document(
                flow="sim",
                run_id="1",
                phase="complete",
                targets=[],
                completed_targets=[],
                detail={},
            )
        )
    )
    cache = JobArtifactCache()
    cache.configure_project(tmp_path)
    original = cache._read
    errors = {report_path, progress}

    def read(path):
        if path in errors:
            raise OSError("transient I/O")
        return original(path)

    monkeypatch.setattr(cache, "_read", read)
    cache.begin()
    assert cache.find(root, "sim", "1", "report") is None
    assert cache.find(root, "sim", "1", "progress") is None
    errors.clear()
    cache.begin()
    assert cache.find(root, "sim", "1", "report") is not None
    assert cache.find(root, "sim", "1", "progress") is not None


@pytest.mark.parametrize("file_limit,byte_limit", [(2, 100000), (100, 80)])
def test_shards_bound_source_bytes_invalid_json_and_stale_files(
    tmp_path, monkeypatch, file_limit, byte_limit
):
    monkeypatch.setattr(job_artifacts, "MAX_ARTIFACTS", file_limit)
    monkeypatch.setattr(job_artifacts, "MAX_ARTIFACT_BYTES", byte_limit)
    roots = [tmp_path / str(i) / "jobs" for i in range(4)]
    paths = [report(root, "sim", n) for root in roots for n in range(3)]
    cache = JobArtifactCache()
    cache.configure_project(tmp_path, max_endpoints=2)
    cache.begin()
    for root in roots:
        cache.find(root, "sim", "1", "report")
        assert len(cache._shards) <= 2
        assert all(len(shard.files) <= file_limit for shard in cache._shards.values())
        assert all(
            sum(entry[1] for entry in shard.files.values()) <= byte_limit
            for shard in cache._shards.values()
        )
    paths[-1].write_text("invalid")
    cache.begin()
    cache.find(roots[-1], "sim", "2", "report")
    assert cache._shards[(roots[-1], "sim")].files[paths[-1]][2] is None
    paths[-1].write_text(json.dumps({"run_id": "2", "passed": False}))
    cache.begin()
    assert cache.find(roots[-1], "sim", "2", "report")[0]["passed"] is False
    paths[-1].unlink()
    cache.begin()
    cache.find(roots[-1], "sim", "2", "report")
    assert paths[-1] not in cache._shards[(roots[-1], "sim")].files


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
    cache = JobArtifactCache()
    cache.configure_project(tmp_path)
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
    progress = path.with_name("progress.json")
    progress.write_text(
        json.dumps(
            progress_document(
                flow="sim",
                run_id="1",
                phase="complete",
                targets=[],
                completed_targets=[],
                detail={},
            )
        )
    )
    source = {
        "runtime": path.parents[3],
        "namespace": path.parents[2],
        "endpoint": path.parents[1],
        "run": path.parent,
        "file": progress,
    }[component]
    moved = tmp_path / "moved"
    source.rename(moved)
    source.symlink_to(moved, target_is_directory=component != "file")
    monkeypatch.setattr(job_artifacts, "MAX_ARTIFACTS", 0)
    cache = JobArtifactCache()
    cache.configure_project(tmp_path)
    cache.begin()
    assert cache.find(root, "sim", "1", "progress") is None


def test_standalone_guard_and_directory_memo_remain_bounded_without_begin(tmp_path):
    real = tmp_path / "real"
    root = real / "project/jobs"
    report(root, "sim", 1)
    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)
    cache = JobArtifactCache()
    assert cache.find(alias / "project/jobs", "sim", "1", "report") is None
    cache._max_endpoints = 2
    for index in range(10):
        root = tmp_path / str(index) / "jobs"
        report(root, "sim", 1)
        assert cache.find(root, "sim", "1", "report") is not None
    assert len(cache._shards) == len(cache._indexes) == len(cache._touched) == 2
    assert len(cache._shared_directories) < 20


def test_progress_real_io_boundary_failure_not_persistently_cached(tmp_path, monkeypatch):
    from booley.flows import progress_lifecycle

    root = tmp_path / "jobs"
    path = report(root, "sim", 1).with_name("progress.json")
    path.write_text(
        json.dumps(
            progress_document(
                flow="sim",
                run_id="1",
                phase="complete",
                targets=[],
                completed_targets=[],
                detail={},
            )
        )
    )
    original = progress_lifecycle._read_json_object_nofollow
    failing = True

    def read(*args, **kwargs):
        if failing:
            raise OSError("temporary unreadable checkpoint")
        return original(*args, **kwargs)

    monkeypatch.setattr(progress_lifecycle, "_read_json_object_nofollow", read)
    cache = JobArtifactCache()
    cache.configure_project(tmp_path)
    cache.begin()
    assert cache.find(root, "sim", "1", "progress") is None
    failing = False
    cache.begin()
    assert cache.find(root, "sim", "1", "progress") is not None


def test_relative_lexical_project_spelling_and_outside_root(tmp_path, monkeypatch):
    project = tmp_path / "project"
    root = project / "jobs"
    report(root, "sim", 1)
    monkeypatch.chdir(tmp_path)
    cache = JobArtifactCache()
    cache.configure_project(Path("unused/../project"))
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
    cache = JobArtifactCache()
    cache.configure_project(tmp_path)
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


def test_complete_fallback_memo_is_ephemeral_and_directory_memory_bounded(tmp_path, monkeypatch):
    root = tmp_path / "jobs"
    for number in range(8):
        path = report(root, "sim", number).with_name("progress.json")
        path.write_text(
            json.dumps(
                progress_document(
                    flow="sim",
                    run_id=str(number),
                    phase="complete",
                    targets=[],
                    completed_targets=[],
                    detail={},
                )
            )
        )
        os.utime(path, (100 + number, 100 + number))
    monkeypatch.setattr(job_artifacts, "MAX_ARTIFACTS", 1)
    original = job_artifacts.read_progress_for_run
    calls = []

    def read(*args, **kwargs):
        calls.append(args)
        return original(*args, **kwargs)

    monkeypatch.setattr(job_artifacts, "read_progress_for_run", read)
    cache = JobArtifactCache()
    cache.configure_project(tmp_path)
    cache.begin()
    assert cache.find(root, "sim", "0", "progress") is not None
    assert cache.find(root, "sim", "0", "progress") is not None
    assert cache.find(root, "sim", "missing", "progress") is None
    assert cache.find(root, "sim", "missing", "progress") is None
    assert len(calls) == 2
    assert len(cache._shards[(root, "sim")].files) <= 1
    assert len(cache._shards[(root, "sim")].directories) <= 3
    cache.begin()
    assert cache.find(root, "sim", "0", "progress") is not None
    assert len(calls) == 3


def test_standalone_progress_fallback_stays_fresh_and_unmemoized(tmp_path):
    real = tmp_path / "real"
    root = real / "project/jobs"
    root.parent.mkdir(parents=True)
    cache = JobArtifactCache()
    assert cache.find(root, "sim", "1", "progress") is None
    path = report(root, "sim", 1).with_name("progress.json")
    path.write_text(
        json.dumps(
            progress_document(
                flow="sim",
                run_id="1",
                phase="complete",
                targets=[],
                completed_targets=[],
                detail={},
            )
        )
    )
    assert cache.find(root, "sim", "1", "progress") is not None
    for number in range(20):
        cache.find(root, "sim", f"missing-{number}", "progress")
    assert cache._fallbacks == {}


def test_configured_fallback_memo_bounded_without_begin(tmp_path, monkeypatch):
    root = tmp_path / "jobs"
    report(root, "sim", 1)
    monkeypatch.setattr(job_artifacts, "MAX_ARTIFACTS", 0)
    cache = JobArtifactCache()
    cache.configure_project(tmp_path, max_endpoints=2)
    for number in range(20):
        cache.find(root, "sim", f"missing-{number}", "progress")
        assert len(cache._fallbacks) <= 2


def test_endpoint_replacement_preserves_other_current_shards(tmp_path, monkeypatch):
    roots = [tmp_path / str(number) / "jobs" for number in range(3)]
    paths = [report(root, "sim", 1) for root in roots]
    cache = JobArtifactCache()
    cache.configure_project(tmp_path, max_endpoints=2)
    cache.begin()
    for root in roots[:2]:
        cache.find(root, "sim", "1", "report")
    cache.complete()
    original = Path.read_bytes
    reads = []

    def read(path):
        reads.append(path)
        return original(path)

    monkeypatch.setattr(Path, "read_bytes", read)
    cache.begin()
    cache.prepare_endpoints(((roots[2], "sim"), (roots[0], "sim")))
    assert cache.find(roots[2], "sim", "1", "report") is not None
    assert cache.find(roots[0], "sim", "1", "report") is not None
    assert reads == [paths[2]]
    assert len(cache._shards) == 2


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
    cache = JobArtifactCache()
    cache.configure_project(tmp_path)
    cache.begin()
    assert cache.find(root, "sim", "1", "report") is None
