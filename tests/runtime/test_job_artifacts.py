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


def progress(path, run="1", **updates):
    data = progress_document(
        flow="sim", run_id=run, phase="complete", targets=[], completed_targets=[], detail={}
    )
    data.update(updates)
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
    cache = JobArtifactCache(tmp_path)
    for refresh in range(3):
        cache.begin()
        for root in roots:
            assert cache.find(root, "sim", "1", "report") is not None
            assert cache.find(root, "sim", "absent", "progress") is None
        cache.complete()
        expected = paths + checkpoints if refresh == 0 else [paths[0]] if refresh == 2 else []
        assert sorted(reads) == sorted(expected)
        reads.clear()
        if refresh == 1:
            info = paths[0].stat()
            os.utime(paths[0], ns=(info.st_atime_ns, info.st_mtime_ns + 1_000_000_000))
            assert paths[0].stat().st_mtime_ns != info.st_mtime_ns
    assert {p: p.read_bytes() for p in before} == before


@pytest.mark.parametrize("invalid", [False, True])
def test_overflow_reuses_unchanged_documents(tmp_path, monkeypatch, invalid):
    root = tmp_path / "jobs"
    paths = [progress(report(root, "sim", n).with_name("progress.json"), str(n)) for n in range(3)]
    progress(paths[2], "0", flow="lint")
    if invalid:
        paths[1].write_text("invalid")
    for n, path in enumerate(paths):
        os.utime(path, (100 + n, 100 + n))
    monkeypatch.setattr(job_artifacts, "MAX_ARTIFACTS", 0)
    parses = spy(monkeypatch, json, "loads")
    cache = JobArtifactCache(tmp_path)
    for _refresh in range(2):
        cache.begin()
        assert cache.find(root, "sim", "0", "progress")[1] == paths[0]
        assert cache.find(root, "sim", "absent", "progress") is None
        assert cache.find(root, "sim", "another", "progress") is None
        cache.complete()
        assert len(parses) == 3, "unchanged overflow documents were re-parsed"
    progress(paths[1], "1")
    os.utime(paths[1], (200, 200))
    cache.begin()
    assert cache.find(root, "sim", "1", "progress")[1] == paths[1]
    assert len(parses) == 4
    cache.complete()
    cache.begin()
    assert cache.find(root, "sim", "0", "progress")[1] == paths[0]
    assert len(parses) == 4


@pytest.mark.parametrize("kind", ["report", "progress"])
def test_project_boundary_follows_only_the_booley_directory_entry(tmp_path, monkeypatch, kind):
    real = tmp_path / "real"
    root = real / "project" / "jobs"
    progress(report(root, "sim", 1).with_name("progress.json"))
    monkeypatch.setattr(job_artifacts, "MAX_ARTIFACTS", 0 if kind == "progress" else 100)
    # A link above the entry is accepted.
    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)
    cache = JobArtifactCache(alias / "project")
    cache.begin()
    assert cache.find(alias / "project/jobs", "sim", "1", kind) is not None
    # The entry itself (the cache's Project is the Booley Directory) is followed.
    linked_project = tmp_path / "linked"
    linked_project.symlink_to(real / "project", target_is_directory=True)
    cache = JobArtifactCache(linked_project)
    cache.begin()
    assert cache.find(linked_project / "jobs", "sim", "1", kind) is not None
    # A link below the entry is still refused.
    other = tmp_path / "other"
    report(other / "project" / "jobs", "sim", 1)
    progress(other / "project" / "flow-reports/sim/1/progress.json")
    (other / "project" / "flow-reports").rename(other / "elsewhere")
    (other / "project" / "flow-reports").symlink_to(other / "elsewhere", target_is_directory=True)
    below = JobArtifactCache(other / "project")
    below.begin()
    assert below.find(other / "project/jobs", "sim", "1", kind) is None


@pytest.mark.parametrize("kind,limit", [("report", 100), ("progress", 100), ("progress", 0)])
def test_transient_report_and_progress_io_is_retried(tmp_path, monkeypatch, kind, limit):
    root = tmp_path / "jobs"
    path = report(root, "sim", 1)
    progress(path.with_name("progress.json"))
    monkeypatch.setattr(job_artifacts, "MAX_ARTIFACTS", limit)
    cache = JobArtifactCache(tmp_path)
    with monkeypatch.context() as fault:

        def read(*args, **kwargs):
            raise OSError("transient I/O")

        fault.setattr(json, "loads", read)
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
    paths = [report(root, "sim", n) for n in range(3)]
    for n, path in enumerate(paths):
        path.write_text(json.dumps({"run_id": "same", "passed": bool(n)}))
        os.utime(path, (100 + n, 100 + n))
    reads = spy(monkeypatch, Path, "read_bytes")
    cache = JobArtifactCache(tmp_path)
    cache.begin()
    assert cache.find(root, "sim", "same", "report")[1] == paths[-1]
    assert cache.find(root, "sim", "missing", "report") is None
    assert len(reads) == 2
    assert cache.diagnostics == [f"Job artifact history exceeds scan bound: {root}/sim"]


@pytest.mark.parametrize("bad", ["invalid", "[]", '{"run_id": 1}'])
def test_invalid_documents_reused_until_metadata_changes(tmp_path, monkeypatch, bad):
    root = tmp_path / "jobs"
    path = report(root, "sim", 1)
    path.write_text(bad)
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


# Component 6 is the Project entry itself (the Booley Directory in production): it
# may be a followed link, but is still refused when dangling or not a directory.
@pytest.mark.parametrize(
    ("component", "refusal"),
    [
        *((component, "link") for component in range(6)),
        *(
            (component, refusal)
            for component in range(7)
            for refusal in ("dangling", "non-directory")
        ),
    ],
)
@pytest.mark.parametrize("limit", [0, 100])
def test_internal_components_refused(tmp_path, monkeypatch, component, refusal, limit):
    project = tmp_path / "project"
    root = project / "logs/.runtime/jobs"
    path = progress(report(root, "sim", 1).with_name("progress.json"))
    kind = "report" if limit else "progress"
    path = path.with_name(kind + ".json")
    source = (path, *path.parents)[component]
    moved = tmp_path / "moved"
    source.rename(moved)
    if refusal == "non-directory":
        source.write_text("not a directory or JSON document")
    else:
        source.symlink_to(
            moved if refusal == "link" else tmp_path / "missing", target_is_directory=component > 0
        )
    monkeypatch.setattr(job_artifacts, "MAX_ARTIFACTS", limit)
    cache = JobArtifactCache(project)
    cache.begin()
    assert cache.find(root, "sim", "1", kind) is None


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


def test_shared_refuse_symlinks_keeps_whole_ancestor_guard(tmp_path):
    from booley.runtime.safe_storage import refuse_symlinks

    real = tmp_path / "real"
    root = real / "project/jobs"
    path = report(root, "sim", 1)
    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)
    with pytest.raises(OSError):
        refuse_symlinks(alias / path.relative_to(real))


@pytest.mark.parametrize("limit", [0, 100])
def test_progress_shape_identity_and_size_limit(tmp_path, monkeypatch, limit):
    root = tmp_path / "jobs"
    path = report(root, "sim", 1).with_name("progress.json")
    monkeypatch.setattr(job_artifacts, "MAX_ARTIFACTS", limit)
    cache = JobArtifactCache(tmp_path)
    for updates in ({"complete": False}, {"flow": "lint"}, {"run_id": "other"}, {"run_id": 1}):
        progress(path, **updates)
        cache.begin()
        assert cache.find(root, "sim", "1", "progress") is None
    progress(path)
    with path.open("a") as stream:
        stream.write(" " * (2 * 1024 * 1024))
    cache.begin()
    assert cache.find(root, "sim", "1", "progress") is None
