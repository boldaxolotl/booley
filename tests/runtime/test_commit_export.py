"""Clean checkout export of a Git commit: tracked files only, pinned, streamed, refusing LFS."""

from __future__ import annotations

import os
import re
import tracemalloc
from collections.abc import Sequence
from pathlib import Path, PurePosixPath

import pytest
from tests.conftest import require_symlinks
from tests.subprocess_budget import run_bounded_child

from booley.runtime.pinned_history import raw_git
from booley.targets.catalog import TargetCatalog

GIT_TIMEOUT_S = 30
FAKE_OID = "a" * 40

CORE_TEMPLATE = (
    "CAPI=2:\n"
    "name: acme:ip:{name}:1.0\n"
    "filesets:\n"
    "  rtl:\n"
    "    files: [rtl.sv]\n"
    "targets:\n"
    "  sim:\n"
    "    flow: sim\n"
    "    flow_options: {{tool: icarus}}\n"
    "    filesets: [rtl]\n"
    "    toplevel: top\n"
)


def git(cwd: Path, *args: str) -> str:
    """Run Git with a fixed identity and no automatic maintenance; return stripped stdout."""
    command = [
        "git",
        "-c",
        "user.name=Export Test",
        "-c",
        "user.email=export@test.invalid",
        "-c",
        "core.autocrlf=false",
        "-c",
        "maintenance.auto=false",
        "-c",
        "gc.auto=0",
        "-c",
        "protocol.file.allow=always",
        *args,
    ]
    return run_bounded_child(command, cwd=cwd, timeout=GIT_TIMEOUT_S, check=True).stdout.strip()


def new_repository(path: Path) -> Path:
    path.mkdir(parents=True)
    git(path, "init", "-q", "-b", "main")
    return path


def commit_all(repository: Path, message: str = "c", *, staged_only: bool = False) -> str:
    """Commit the working tree, or only what is already staged (gitlinks need that)."""
    if not staged_only:
        git(repository, "add", "-A", "-f")
    git(repository, "commit", "-q", "-m", message, "--allow-empty")
    return git(repository, "rev-parse", "HEAD")


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode())
    return path


def add_index_entry(repository: Path, mode: str, path: str, content: str) -> None:
    """Index entry without touching the working tree (works on case-insensitive disks)."""
    source = write(repository.parent / "blob.tmp", content)
    blob = git(repository, "hash-object", "-w", str(source))
    source.unlink()
    git(repository, "update-index", "--add", "--cacheinfo", f"{mode},{blob},{path}")


def add_gitlink(repository: Path, path: str, oid: str = FAKE_OID) -> None:
    git(repository, "update-index", "--add", "--cacheinfo", f"160000,{oid},{path}")


def listing(root: Path) -> set[str]:
    """Every file or symlink below *root* as a POSIX path."""
    return {
        p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_symlink() or p.is_file()
    }


@pytest.fixture(autouse=True)
def no_ambient_booley_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BOOLEY_DIR", raising=False)


def test_export_holds_exactly_the_commits_tracked_files(tmp_path: Path) -> None:
    from booley.runtime.commit_export import CommitExport, export_commit

    require_symlinks(tmp_path)
    repo = new_repository(tmp_path / "repo")
    write(repo / ".gitignore", "ignored.txt\n")
    write(repo / "rtl" / "a.v", "module a; endmodule\n")
    write(repo / "run.sh", "#!/bin/sh\n")
    (repo / "run.sh").chmod(0o755)
    (repo / "link").symlink_to("rtl/a.v")
    head = commit_all(repo)
    write(repo / "rtl" / "a.v", "live edit\n")
    write(repo / "untracked.v", "x")
    write(repo / "ignored.txt", "x")

    result = export_commit(repo, head, tmp_path / "out")

    assert result == CommitExport(tmp_path / "out", (), None)
    assert listing(result.root) == {".gitignore", "rtl/a.v", "run.sh", "link"}
    assert not (result.root / ".git").exists()
    assert (result.root / "rtl" / "a.v").read_text() == "module a; endmodule\n"
    assert (result.root / "link").readlink().as_posix() == "rtl/a.v"
    if os.name != "nt":
        assert os.access(result.root / "run.sh", os.X_OK)
        assert not os.access(result.root / "rtl" / "a.v", os.X_OK)
    assert [p.name for p in tmp_path.iterdir() if p.name.startswith(".out.")] == []


def test_export_applies_only_committed_attributes(tmp_path: Path) -> None:
    from booley.runtime.commit_export import export_commit

    repo = new_repository(tmp_path / "repo")
    write(repo / ".gitattributes", "*.txt text eol=crlf\n")
    write(repo / "a.txt", "one\ntwo\n")
    write(repo / "b.bin", "one\ntwo\n")
    head = commit_all(repo)

    root = export_commit(repo, head, tmp_path / "out").root

    assert (root / "a.txt").read_bytes() == b"one\r\ntwo\r\n"
    assert (root / "b.bin").read_bytes() == b"one\ntwo\n"


@pytest.mark.parametrize("place", ["working-tree", "booley-directory", "exists"])
def test_destination_inside_working_tree_or_booley_directory_is_refused(
    tmp_path: Path, place: str
) -> None:
    from booley.runtime.commit_export import CommitExportError, export_commit

    repo = new_repository(tmp_path / "repo")
    write(repo / "a.v", "x")
    external = tmp_path / "external-booley"
    write(repo / "booley.toml", f'[project]\ndir = "{external.as_posix()}"\n')
    head = commit_all(repo)
    destination = {
        "working-tree": repo / "sub" / "out",
        "booley-directory": external / "out",
        "exists": tmp_path,
    }[place]

    with pytest.raises(CommitExportError, match="destination"):
        export_commit(repo, head, destination)

    assert not (repo / "sub").exists()
    assert not external.exists()


def test_exported_booley_directory_outside_export_is_refused(tmp_path: Path) -> None:
    from booley.runtime.commit_export import CommitExportError, export_commit

    repo = new_repository(tmp_path / "repo")
    write(repo / "a.v", "x")
    write(repo / "booley.toml", f'[project]\ndir = "{(tmp_path / "live-booley").as_posix()}"\n')
    head = commit_all(repo)
    # The live tree no longer says so: only the committed file does.
    (repo / "booley.toml").unlink()

    with pytest.raises(CommitExportError, match="live directory"):
        export_commit(repo, head, tmp_path / "out")

    assert not (tmp_path / "out").exists()
    assert [p.name for p in tmp_path.iterdir() if p.name.startswith(".out.")] == []


def make_submodule(tmp_path: Path) -> tuple[Path, Path, str, str]:
    """Parent with submodule ``sub`` (holding nested ``inner``), each at its first commit."""
    inner = new_repository(tmp_path / "inner-origin")
    write(inner / "deep.v", "deep v1\n")
    deep_pin = commit_all(inner)
    sub_origin = new_repository(tmp_path / "sub-origin")
    write(sub_origin / "lib.v", "lib v1\n")
    git(sub_origin, "submodule", "add", "-q", str(inner), "inner")
    sub_pin = commit_all(sub_origin)
    parent = new_repository(tmp_path / "parent")
    write(parent / "top.v", "top\n")
    git(parent, "submodule", "add", "-q", str(sub_origin), "sub")
    git(parent / "sub", "submodule", "update", "--init", "-q")
    return parent, tmp_path, sub_pin, deep_pin


def test_submodule_exported_at_gitlink_and_drift_reported(tmp_path: Path) -> None:
    from booley.runtime.commit_export import SubmoduleDrift, export_commit

    parent, _, sub_pin, deep_pin = make_submodule(tmp_path)
    head = commit_all(parent)
    sub, deep = parent / "sub", parent / "sub" / "inner"
    write(sub / "lib.v", "lib v2\n")
    sub_head = commit_all(sub, "move sub")
    write(sub / "lib.v", "lib uncommitted\n")
    write(deep / "deep.v", "deep v2\n")
    deep_head = commit_all(deep, "move inner")

    result = export_commit(parent, head, tmp_path / "out")

    assert (result.root / "sub" / "lib.v").read_text() == "lib v1\n"
    assert (result.root / "sub" / "inner" / "deep.v").read_text() == "deep v1\n"
    assert not (result.root / "sub" / ".git").exists()
    assert result.drift == (
        SubmoduleDrift(PurePosixPath("sub"), sub_pin, sub_head, dirty=True),
        SubmoduleDrift(PurePosixPath("sub/inner"), deep_pin, deep_head, dirty=False),
    )


@pytest.mark.parametrize("state", ["uninitialized", "object-absent"])
def test_missing_submodule_objects_name_the_path(tmp_path: Path, state: str) -> None:
    from booley.runtime.commit_export import CommitExportError, export_commit

    repo = new_repository(tmp_path / "repo")
    write(repo / "top.v", "x")
    git(repo, "add", "top.v")
    if state == "object-absent":
        new_repository(repo / "libs" / "core")
    add_gitlink(repo, "libs/core")
    head = commit_all(repo, staged_only=True)

    with pytest.raises(CommitExportError, match=r"submodule objects missing for libs/core"):
        export_commit(repo, head, tmp_path / "out")

    assert not (tmp_path / "out").exists()


def make_versioned_booley_directory(tmp_path: Path, layout: str) -> tuple[Path, Path]:
    """RTL checkout plus a Versioned Booley Directory holding committed and live edits."""
    main = new_repository(tmp_path / "main")
    write(main / ".gitignore", ".booley/\n")
    write(main / "rtl.v", "rtl\n")
    git(main, "add", ".gitignore", "rtl.v")
    add_gitlink(main, "unselected")
    head_commit = commit_all(main, staged_only=True)
    booley = new_repository(main / ".booley")
    write(booley / "booley.toml", "[submodules]\npaths = []\n")
    write(booley / "cores" / "x.core", "committed\n")
    commit_all(booley)
    checkout = main
    if layout == "paired-checkout":
        checkout = tmp_path / "linked"
        git(main, "worktree", "add", "-q", "-b", "feature", str(checkout), head_commit)
        git(booley, "worktree", "add", "-q", "-b", "feature", str(checkout / ".booley"))
        write(checkout / ".booley" / "cores" / "x.core", "paired committed\n")
        commit_all(checkout / ".booley", "paired")
    write(checkout / ".booley" / "cores" / "x.core", "uncommitted edit\n")
    # An uncommitted selection that, if read, would fail on the fake ``unselected`` gitlink.
    write(checkout / ".booley" / "booley.toml", '[submodules]\npaths = ["unselected"]\n')
    write(checkout / ".booley" / "new.core", "untracked\n")
    return checkout, checkout / ".booley"


@pytest.mark.parametrize("layout", ["standalone", "paired-checkout"])
def test_versioned_booley_directory_exported_at_committed_head(
    tmp_path: Path, layout: str
) -> None:
    from booley.runtime.commit_export import export_commit

    checkout, booley = make_versioned_booley_directory(tmp_path, layout)
    head = git(checkout, "rev-parse", "HEAD")
    paired_head = git(booley, "rev-parse", "HEAD")

    result = export_commit(checkout, head, tmp_path / "out")

    committed = "paired committed\n" if layout == "paired-checkout" else "committed\n"
    assert (result.root / ".booley" / "cores" / "x.core").read_text() == committed
    assert not (result.root / ".booley" / "new.core").exists()
    assert (result.root / ".booley" / "booley.toml").is_file()
    assert result.paired_commit == paired_head
    assert not (result.root / "unselected").exists()
    assert result.drift == ()


def test_filesystem_failure_is_a_commit_export_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from booley.runtime import commit_export

    repo = new_repository(tmp_path / "repo")
    write(repo / "a.v", "x")
    head = commit_all(repo)

    def fail(*_args: object) -> None:
        raise PermissionError("read-only destination")

    monkeypatch.setattr(commit_export, "materialize_tree", fail)

    with pytest.raises(commit_export.CommitExportError, match="read-only destination"):
        commit_export.export_commit(repo, head, tmp_path / "out")

    assert not (tmp_path / "out").exists()


def test_committed_symlink_cannot_redirect_the_booley_directory(tmp_path: Path) -> None:
    from booley.runtime.commit_export import CommitExportError, export_commit

    require_symlinks(tmp_path)
    victim = tmp_path / "victim"
    write(victim / "live.core", "uncommitted live work\n")
    repo = new_repository(tmp_path / "repo")
    add_index_entry(repo, "100644", "rtl.v", "x\n")
    add_index_entry(repo, "120000", ".booley", victim.as_posix())
    head = commit_all(repo, staged_only=True)
    booley = new_repository(repo / ".booley.live")
    write(booley / "cores" / "x.core", "committed\n")
    commit_all(booley)
    (repo / ".booley.live").rename(repo / ".booley-moved")
    (repo / ".booley-moved").rename(repo / ".booley")

    with pytest.raises(CommitExportError, match="symlink"):
        export_commit(repo, head, tmp_path / "out")

    assert listing(victim) == {"live.core"}
    assert (victim / "live.core").read_text() == "uncommitted live work\n"
    assert not (tmp_path / "out").exists()


def test_booley_directory_gitlink_is_not_recursed_into(tmp_path: Path) -> None:
    from booley.runtime.commit_export import export_commit

    repo = new_repository(tmp_path / "repo")
    write(repo / "rtl.v", "x")
    git(repo, "add", "rtl.v")
    add_gitlink(repo, ".booley")
    head = commit_all(repo, staged_only=True)
    booley = new_repository(repo / ".booley")
    write(booley / "cores" / "x.core", "bd\n")
    commit_all(booley)

    result = export_commit(repo, head, tmp_path / "out")

    assert (result.root / ".booley" / "cores" / "x.core").read_text() == "bd\n"


def test_versioned_booley_directory_ignores_ambient_selection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from booley.runtime.commit_export import export_commit

    foreign = new_repository(tmp_path / "foreign")
    write(foreign / "foreign.txt", "foreign\n")
    commit_all(foreign)
    monkeypatch.setenv("BOOLEY_DIR", str(foreign))
    repo = new_repository(tmp_path / "repo")
    write(repo / "rtl.v", "x")
    head = commit_all(repo)

    result = export_commit(repo, head, tmp_path / "out")

    assert result.paired_commit is None
    assert listing(result.root) == {"rtl.v"}


@pytest.mark.parametrize(
    ("attributes", "path"),
    [
        ("*.bin filter=lfs diff=lfs merge=lfs", "big.bin"),
        ("*.txt working-tree-encoding=UTF-16", "a.txt"),
    ],
    ids=["filter", "working-tree-encoding"],
)
def test_filter_and_encoding_attributes_refused(
    tmp_path: Path, attributes: str, path: str
) -> None:
    from booley.runtime.commit_export import CommitExportError, export_commit

    repo = new_repository(tmp_path / "repo")
    # Index entries directly: `git add` itself would run the (absent) filter or re-encode.
    add_index_entry(repo, "100644", ".gitattributes", attributes + "\n")
    add_index_entry(repo, "100644", path, "payload\n")
    head = commit_all(repo, staged_only=True)

    with pytest.raises(CommitExportError, match=rf"{re.escape(path)}.*Git LFS"):
        export_commit(repo, head, tmp_path / "out")

    assert not (tmp_path / "out").exists()


@pytest.mark.parametrize(
    "paths",
    [("A.v", "a.v"), ("a", "A/x"), ("dir/f", "DIR/g"), ("caf\u00e9.v", "cafe\u0301.v")],
    ids=["same-name", "file-vs-directory", "parent-directories", "unicode-normalization"],
)
def test_case_colliding_paths_refused(tmp_path: Path, paths: tuple[str, str]) -> None:
    from booley.runtime.commit_export import CommitExportError, export_commit

    repo = new_repository(tmp_path / "repo")
    for path in paths:
        add_index_entry(repo, "100644", path, f"{path}\n")
    head = commit_all(repo, staged_only=True)

    with pytest.raises(CommitExportError, match="case-colliding"):
        export_commit(repo, head, tmp_path / "out")

    assert not (tmp_path / "out").exists()


@pytest.mark.timeout(120)
def test_export_streams_each_file_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from booley.runtime.commit_export import export_commit

    repo = new_repository(tmp_path / "repo")
    blobs = {f"big{i}.bin": os.urandom(2 * 1024 * 1024) for i in range(8)}
    for name, data in blobs.items():
        (repo / name).write_bytes(data)
    head = commit_all(repo)
    launches: list[Sequence[str]] = []
    real_raw_git = raw_git

    def recording(repository: Path, *args: str, **kwargs: object) -> bytes:
        if "checkout-index" in args:
            launches.append(args)
        return real_raw_git(repository, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr("booley.runtime.commit_export.raw_git", recording)
    tracemalloc.start()
    try:
        result = export_commit(repo, head, tmp_path / "out")
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert peak < 4 * 1024 * 1024  # 16 MiB of content never sits in Python memory
    assert len(launches) == 1
    work_tree = next(a for a in launches[0] if a.startswith("--work-tree="))
    assert Path(work_tree.removeprefix("--work-tree=")).parent == tmp_path
    assert {name: (result.root / name).read_bytes() for name in blobs} == blobs


def test_untracked_live_core_is_not_an_exported_target(tmp_path: Path) -> None:
    from booley.runtime.commit_export import export_commit

    repo = new_repository(tmp_path / "repo")
    write(repo / ".gitignore", ".booley/\n")
    write(repo / "rtl.sv", "module top; endmodule\n")
    write(repo / "alpha.core", CORE_TEMPLATE.format(name="alpha"))
    head = commit_all(repo)
    booley = new_repository(repo / ".booley")
    write(booley / "cores" / "beta.core", CORE_TEMPLATE.format(name="beta"))
    commit_all(booley)
    write(repo / "live_rtl.core", CORE_TEMPLATE.format(name="live_rtl"))
    write(booley / "cores" / "live_booley.core", CORE_TEMPLATE.format(name="live_booley"))

    result = export_commit(repo, head, tmp_path / "out")

    live = {h.vlnv for h in TargetCatalog.build(repo).list()}
    exported = {h.vlnv for h in TargetCatalog.build(result.root).list()}
    assert {"acme:ip:live_rtl:1.0", "acme:ip:live_booley:1.0"} <= live
    assert exported == {"acme:ip:alpha:1.0", "acme:ip:beta:1.0"}
