"""Tests for the shared ``artifacts`` report block (``flows/artifacts.py``).

The block is the agent's route from a verdict to the files behind it, so the
properties worth pinning are the ones that make a pointer trustworthy: it is
project-relative, and it is absent rather than wrong.
"""

from __future__ import annotations

import copy
import os
import stat
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from booley.flows import artifacts
from booley.flows.artifact_durability import durable_copy, durable_in_place


def _assert_host_readable(path: Path) -> None:
    mode = stat.S_IMODE(path.stat().st_mode)
    if os.name == "nt":
        assert mode & stat.S_IREAD
    else:
        assert mode == 0o644


class TestDurablePublication:
    def test_bytes_use_safe_collision_resistant_components(self, tmp_path: Path):
        invocation = tmp_path / "reports/lint/1"

        relative = artifacts.publish_bytes(
            invocation,
            ("artifacts", "../../target", "run.log"),
            b"evidence",
            work_dir=tmp_path,
        )

        published = tmp_path / relative
        assert published.read_bytes() == b"evidence"
        _assert_host_readable(published)
        assert published.is_relative_to(invocation)
        assert ".." not in published.relative_to(invocation).parts

    def test_file_snapshot_is_readable_outside_the_publisher_identity(self, tmp_path: Path):
        invocation = tmp_path / "reports/sim/1"
        source = tmp_path / "source"
        source.write_bytes(b"evidence")
        source.chmod(0o600)

        relative = artifacts.publish_file(
            invocation,
            ("copy",),
            source,
            work_dir=tmp_path,
        )

        _assert_host_readable(tmp_path / relative)

    def test_destination_is_create_only(self, tmp_path: Path):
        invocation = tmp_path / "reports/lint/1"
        arguments = (invocation, ("run.log",), b"first")
        artifacts.publish_bytes(*arguments, work_dir=tmp_path)

        with pytest.raises(FileExistsError):
            artifacts.publish_bytes(invocation, ("run.log",), b"second", work_dir=tmp_path)

        assert (invocation / "run.log").read_bytes() == b"first"

    def test_missing_and_mutating_sources_are_not_published(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        invocation = tmp_path / "reports/sim/1"
        with pytest.raises(FileNotFoundError):
            artifacts.publish_file(
                invocation, ("missing",), tmp_path / "missing", work_dir=tmp_path
            )

        source = tmp_path / "source"
        source.write_bytes(b"before")

        def mutate_after_copy(source_path: Path, destination: Path, **kwargs) -> None:
            durable_copy(source_path, destination, **kwargs)
            source_path.write_bytes(b"after")

        monkeypatch.setattr(artifacts, "durable_copy", mutate_after_copy)
        destination = invocation / "copy"
        with pytest.raises(OSError, match="changed during publication"):
            artifacts.publish_file(invocation, ("copy",), source, work_dir=tmp_path)

        assert not destination.exists()


def _touch(root: Path, *names: str) -> None:
    """Create each file under *root*; a name ending in ``/`` is a directory."""
    for name in names:
        path = root / name
        if name.endswith("/"):
            path.mkdir(parents=True)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("x", encoding="utf-8")


@pytest.mark.parametrize(
    ("files", "given", "base", "expected"),
    [
        # Absolute path becomes project-relative.
        (("build/run.log",), lambda r: r / "build" / "run.log", ".", "build/run.log"),
        # A caller may pass a path already in the returned form; anchoring the
        # existence check on the cwd would drop it whenever the Flow does not
        # run from the work dir, the normal case under MCP dispatch.
        (("build/run.log",), lambda _r: "build/run.log", ".", "build/run.log"),
        ((), lambda r: r / "nope.log", ".", None),
        ((), lambda _r: None, ".", None),
        ((), lambda _r: "", ".", None),
        # A ``..`` form is legitimate (a baseline worktree lives beside the
        # work dir), so it must not be dropped.
        (("outside.log", "project/"), lambda r: r / "outside.log", "project", "../outside.log"),
    ],
    ids=["absolute", "relative-from-elsewhere", "missing", "none", "empty", "outside-work-dir"],
)
def test_relative_pointer_is_project_relative_or_absent(
    files: tuple[str, ...],
    given: Callable[[Path], object],
    base: str,
    expected: str | None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _touch(tmp_path, *files)
    (tmp_path / "somewhere-else").mkdir()
    monkeypatch.chdir(tmp_path / "somewhere-else")

    assert artifacts.relative(given(tmp_path), tmp_path / base) == expected


@pytest.mark.parametrize(
    ("files", "build", "expected"),
    [
        (
            ("run.log",),
            lambda r: artifacts.artifacts_block(
                r, log=r / "run.log", trace=r / "trace.fst", result=None
            ),
            {"log": "run.log"},
        ),
        ((), lambda r: artifacts.artifacts_block(r, log=r / "gone.log"), {}),
        (
            ("run.log", "build/"),
            lambda r: artifacts.artifacts_block(r, log=r / "run.log", dirs={"build": r / "build"}),
            {"log": "run.log", "dirs": {"build": "build"}},
        ),
        # The drop-what-is-absent rule applies to directories too: an agent
        # must never be sent to list a path that is not there.
        (
            ("build/",),
            lambda r: artifacts.artifacts_block(
                r, dirs={"build": r / "build", "timing": r / "build" / "reports" / "timing"}
            ),
            {"dirs": {"build": "build"}},
        ),
        ((), lambda r: artifacts.artifacts_block(r, dirs={"build": r / "gone"}), {}),
        # ``relative`` checks existence, not kind: callers decide semantics.
        (
            ("run.log",),
            lambda r: artifacts.artifacts_block(r, dirs={"build": r / "run.log"}),
            {"dirs": {"build": "run.log"}},
        ),
    ],
    ids=["drops-absent-keys", "empty", "nested-dirs", "absent-dirs", "no-dirs", "file-as-dir"],
)
def test_artifacts_block_keeps_only_what_landed(
    files: tuple[str, ...],
    build: Callable[[Path], dict[str, object]],
    expected: dict[str, object],
    tmp_path: Path,
) -> None:
    _touch(tmp_path, *files)

    assert build(tmp_path) == expected


@pytest.mark.parametrize(
    ("detail", "block", "expected"),
    [
        (
            {"artifacts": {"log": "a/run.log"}},
            {"report": "b/report.json"},
            {"artifacts": {"log": "a/run.log", "report": "b/report.json"}},
        ),
        ({}, {"log": "a/run.log"}, {"artifacts": {"log": "a/run.log"}}),
        # An empty mapping would read as "checked, found nothing", a different
        # claim from "not applicable".
        ({}, {}, {}),
        # A caller adding directories in two passes must not lose the first.
        (
            {"artifacts": {"dirs": {"build": "a"}}},
            {"dirs": {"timing": "b"}},
            {"artifacts": {"dirs": {"build": "a", "timing": "b"}}},
        ),
    ],
    ids=["existing-key", "absent-key", "empty-block", "dirs-merge-by-key"],
)
def test_merge_artifacts(
    detail: dict[str, Any], block: dict[str, Any], expected: dict[str, Any]
) -> None:
    merged = copy.deepcopy(detail)

    artifacts.merge_artifacts(merged, block)

    assert merged == expected


class TestDurableInPlace:
    def test_keeps_the_bytes_and_gives_the_copy_mode(self, tmp_path: Path):
        path = tmp_path / "run.log"
        path.write_bytes(b"evidence")
        path.chmod(0o644)

        durable_in_place(path)

        assert path.read_bytes() == b"evidence"
        if os.name != "nt":
            assert stat.S_IMODE(path.stat().st_mode) == 0o600

    def test_refuses_a_directory_and_a_missing_file(self, tmp_path: Path):
        with pytest.raises(OSError):
            durable_in_place(tmp_path)
        with pytest.raises(OSError):
            durable_in_place(tmp_path / "absent")

    @pytest.mark.skipif(os.name == "nt", reason="needs a symlink")
    def test_refuses_a_symlink(self, tmp_path: Path):
        target = tmp_path / "real.log"
        target.write_bytes(b"x")
        link = tmp_path / "link.log"
        link.symlink_to(target)

        with pytest.raises(OSError):
            durable_in_place(link)
