"""Exact pinned single-file publication, without changing unpinned Ticket policy."""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from booley.runtime.git_environment import inherited_git_environment
from booley.runtime.git_facts import is_rememberable, run_remembered
from booley.runtime.history_commit import FileCommitError
from booley.runtime.publication_ownership import PublicationOwnership


@dataclass(frozen=True)
class PinnedPublication:
    """Immutable caller-owned publication identity; journal the commit before CAS."""

    pin: str
    branch: str
    path: str
    blob: str
    intended_commit: str | None = None


def raw_git(
    repository: Path,
    *args: str,
    env: dict[str, str] | None = None,
    input_bytes: bytes | None = None,
) -> bytes:
    """Run explicit-repository Git with raw bytes and caller-owned environment overrides."""
    environment = {
        **inherited_git_environment(),
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_NO_LAZY_FETCH": "1",
    }
    environment.update(env or {})

    def run() -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(
            ["git", *args],
            cwd=repository,
            env=environment,
            input=input_bytes,
            capture_output=True,
            timeout=60,
            check=False,
        )

    try:
        if input_bytes is None and is_rememberable(args):
            result = run_remembered(["git", *args], repository, environment, text=False, run=run)
        else:
            result = run()
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise FileCommitError(f"Git publication command failed: {exc}") from exc
    if result.returncode:
        raise FileCommitError(result.stderr.decode("utf-8", "replace").strip())
    return result.stdout


def validate_pin(repository: Path, pin: str) -> None:
    """A full canonical existing raw commit OID, before any write."""
    algorithm = raw_git(repository, "rev-parse", "--show-object-format").strip()
    length = 64 if algorithm == b"sha256" else 40
    if re.fullmatch(rf"[0-9a-f]{{{length}}}", pin) is None:
        raise FileCommitError("publication pin must be a full canonical commit OID")
    if raw_git(repository, "cat-file", "-t", pin).strip() != b"commit":
        raise FileCommitError("publication pin must name an existing commit")


def tree_rows(repository: Path, commit: str) -> dict[bytes, bytes]:
    """Literal recursive tree rows, preserving every path byte."""
    result: dict[bytes, bytes] = {}
    for row in raw_git(repository, "ls-tree", "-r", "-z", commit).split(b"\0"):
        if row:
            metadata, name = row.split(b"\t", 1)
            result[name] = metadata
    return result


def proves_publication(repository: Path, commit: str, publication: PinnedPublication) -> bool:
    """Raw one-parent, exact blob/mode and otherwise unchanged tree proof."""
    validate_pin(repository, publication.pin)
    validate_pin(repository, commit)
    headers = raw_git(repository, "cat-file", "commit", commit).split(b"\n\n", 1)[0]
    parents = [
        row[7:].decode("ascii") for row in headers.split(b"\n") if row.startswith(b"parent ")
    ]
    if parents != [publication.pin]:
        return False
    before = tree_rows(repository, publication.pin)
    after = tree_rows(repository, commit)
    name = os.fsencode(publication.path)
    if name in before or after.pop(name, None) != f"100644 blob {publication.blob}".encode():
        return False
    return before == after


def require_branch(repository: Path, branch: str) -> str:
    """Check symbolic HEAD on every side of each publication boundary."""
    try:
        actual = raw_git(repository, "symbolic-ref", "-q", "HEAD").strip().decode("ascii")
    except FileCommitError as exc:
        raise FileCommitError(
            str(exc) or f"publication needs branch {branch}; HEAD is detached"
        ) from exc
    if actual != branch:
        raise FileCommitError(f"publication needs {branch}; worktree is on {actual}")
    return raw_git(repository, "rev-parse", "HEAD").strip().decode("ascii")


def publish_pinned(
    repository: Path,
    publication: PinnedPublication,
    message: str,
    journal: Callable[[str], None],
    *,
    checkpoint: Callable[[str], None] = lambda _name: None,
    check_branch: Callable[[str], None] | None = None,
    ownership: PublicationOwnership | None = None,
) -> str:
    """Construct privately, journal before CAS, then reconcile the shared index safely."""
    validate_pin(repository, publication.pin)
    _validate_path(publication.path)
    head = _checked_head(repository, publication.branch, check_branch)
    if head != publication.pin:
        if head != publication.intended_commit or not proves_publication(
            repository, head, publication
        ):
            raise FileCommitError("publication HEAD moved beyond its validated pin")
        synchronize_index(repository, publication, ownership=ownership)
        return head
    if os.fsencode(publication.path) in tree_rows(repository, publication.pin):
        raise FileCommitError("publication path is already committed")
    commit = publication.intended_commit
    if commit is None:
        commit = build_commit(
            repository, publication.pin, publication.path, publication.blob, message
        )
        if not proves_publication(repository, commit, publication):
            raise FileCommitError("constructed publication does not match its exact intent")
        journal(commit)
        checkpoint("commit-journaled")
    elif not proves_publication(repository, commit, publication):
        raise FileCommitError("journaled publication commit was substituted")
    if _checked_head(repository, publication.branch, check_branch) != publication.pin:
        raise FileCommitError("publication HEAD moved before ref CAS")
    if not compare_and_swap(repository, publication.branch, commit, publication.pin):
        raise FileCommitError("publication branch moved during ref CAS")
    checkpoint("ref-published")
    if _checked_head(repository, publication.branch, check_branch) != commit:
        raise FileCommitError("publication checkout changed after ref CAS")
    synchronize_index(repository, publication, ownership=ownership)
    checkpoint("index-synchronized")
    return commit


def build_commit(repository: Path, parent: str, path: str, blob: str, message: str) -> str:
    """Pinned construction uses the same replacement-free environment as its raw proof."""
    tree = publication_tree(repository, parent, path, blob)
    return (
        raw_git(repository, "commit-tree", tree, "-p", parent, "-m", message)
        .strip()
        .decode("ascii")
    )


def publication_tree(repository: Path, parent: str, path: str, blob: str) -> str:
    """Build the literal single-path tree in a private index, preserving every pinned row."""
    _validate_path(path)
    with tempfile.TemporaryDirectory(prefix="booley-pinned-history-") as scratch:
        env = {"GIT_INDEX_FILE": str(Path(scratch) / "index")}
        raw_git(repository, "read-tree", parent, env=env)
        raw_git(
            repository, "update-index", "--add", "--cacheinfo", f"100644,{blob},{path}", env=env
        )
        return raw_git(repository, "write-tree", env=env).strip().decode("ascii")


def publication_pack(repository: Path, publication: PinnedPublication) -> bytes:
    """Retain Git's exact publication objects, excluding the already pinned history."""
    return raw_git(
        repository,
        "pack-objects",
        "--stdout",
        "--revs",
        input_bytes=f"{publication.intended_commit}\n^{publication.pin}\n".encode(),
    )


def restore_publication_objects(
    repository: Path, publication: PinnedPublication, content: bytes, pack: bytes
) -> None:
    """Prove saved objects privately before importing only the exact publication."""
    with tempfile.TemporaryDirectory(prefix="booley-publication-") as scratch:
        private = Path(scratch) / "repository"
        raw_git(
            repository,
            "clone",
            "--shared",
            "--bare",
            "--quiet",
            "--",
            str(repository),
            str(private),
        )
        raw_git(private, "unpack-objects", "-r", input_bytes=pack)
        commit = publication.intended_commit
        if commit is None or not proves_publication(private, commit, publication):
            raise FileCommitError("saved publication differs from its immutable intent")
        if raw_git(private, "cat-file", "blob", publication.blob) != content:
            raise FileCommitError("saved publication summary differs from its original blob OID")
        headers = raw_git(private, "cat-file", "commit", commit).split(b"\n\n", 1)[0].split(b"\n")
        expected = publication_tree(private, publication.pin, publication.path, publication.blob)
        if headers[0] != b"tree " + expected.encode():
            raise FileCommitError("saved publication tree differs from the exact pinned tree")
        if (
            len(headers) not in {4, 5}
            or not headers[1].startswith(b"parent ")
            or not headers[2].startswith(b"author ")
            or not headers[3].startswith(b"committer ")
            or (len(headers) == 5 and not headers[4].startswith(b"encoding "))
        ):
            raise FileCommitError("saved publication commit has unsupported raw headers")
        raw_git(
            repository, "unpack-objects", "-r", input_bytes=publication_pack(private, publication)
        )


def compare_and_swap(repository: Path, branch: str, commit: str, expected: str) -> bool:
    """No replacement or optional-index environment leaks across the publication seam."""
    try:
        raw_git(
            repository, "update-ref", "-m", "booley: pinned publication", branch, commit, expected
        )
        return True
    except FileCommitError:
        current = raw_git(repository, "rev-parse", branch).strip().decode("ascii")
        if current == commit:
            return True
        if current != expected:
            return False
        raise


def _checked_head(repository: Path, branch: str, callback: Callable[[str], None] | None) -> str:
    head = require_branch(repository, branch)
    if callback is not None:
        callback(branch)
    if require_branch(repository, branch) != head:
        raise FileCommitError("publication checkout changed during branch check")
    return head


def _validate_path(path: str) -> None:
    parsed = PurePosixPath(path)
    if not path or parsed.is_absolute() or ".." in parsed.parts or "\0" in path:
        raise FileCommitError("publication path must be a literal repository-relative path")


def synchronize_index(
    repository: Path,
    publication: PinnedPublication,
    *,
    ownership: PublicationOwnership | None = None,
) -> None:
    """Under Git's index lock, preserve staging and skip any competing path effects."""
    name = raw_git(repository, "rev-parse", "--git-path", "index").rstrip(b"\n")
    index = Path(os.fsdecode(name))
    if not index.is_absolute():
        index = repository / index
    lock = index.with_name(index.name + ".lock")
    descriptor = _acquire_index(lock, index, ownership)
    if descriptor is None:
        return
    lock_identity = os.fstat(descriptor)
    handed_off = False
    try:
        with os.fdopen(descriptor, "wb") as output, tempfile.TemporaryDirectory() as scratch:
            private = Path(scratch) / "index"
            if index.exists():
                private.write_bytes(index.read_bytes())
            env = {"GIT_INDEX_FILE": str(private)}
            rows = raw_git(repository, "ls-files", "--stage", "-z", env=env)
            if not _can_stage(rows, publication, tree_rows(repository, publication.pin)):
                return
            raw_git(
                repository,
                "update-index",
                "--add",
                "--cacheinfo",
                f"100644,{publication.blob},{publication.path}",
                env=env,
            )
            output.write(private.read_bytes())
            output.flush()
            os.fsync(output.fileno())
        if ownership is not None:
            ownership.checkpoint("index-lock-filled")
        if not _lock_matches(lock, lock_identity):
            raise FileCommitError(
                "Git index lock ownership changed before handoff; foreign lock retained"
            )
        lock.replace(index)
        handed_off = True
    finally:
        if not handed_off:
            _unlink_owned_lock(lock, lock_identity)


def _acquire_index(lock: Path, index: Path, ownership: PublicationOwnership | None) -> int | None:
    owned = None if ownership is None else ownership.inode("index-staging", index.parent)
    if owned is not None and index.exists() and index.samefile(owned) and not lock.exists():
        return None  # Rename completed before any subsequent response/journal boundary.
    try:
        if owned is None:
            descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        else:
            try:
                os.link(owned, lock)
            except FileExistsError:
                if lock.is_symlink() or not lock.samefile(owned):
                    raise
            assert ownership is not None
            ownership.checkpoint("index-lock-acquired")
            descriptor = os.open(owned, os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0))
            metadata = os.fstat(descriptor)
            saved = ownership.read("index-staging")
            if [metadata.st_dev, metadata.st_ino] != saved["identity"] or not _lock_matches(
                lock, metadata
            ):
                os.close(descriptor)
                raise FileCommitError("Git index lock ownership changed; foreign lock retained")
            os.ftruncate(descriptor, 0)
    except FileExistsError as exc:
        raise FileCommitError("Git index is locked; retry publication") from exc
    return descriptor


def _lock_matches(lock: Path, identity: os.stat_result) -> bool:
    try:
        metadata = lock.lstat()
    except FileNotFoundError:
        return False
    return not lock.is_symlink() and (metadata.st_dev, metadata.st_ino) == (
        identity.st_dev,
        identity.st_ino,
    )


def _unlink_owned_lock(lock: Path, ownership: os.stat_result) -> None:
    try:
        current = lock.lstat()
    except FileNotFoundError:
        return
    if (current.st_dev, current.st_ino) == (ownership.st_dev, ownership.st_ino):
        lock.unlink(missing_ok=True)


def _can_stage(rows: bytes, publication: PinnedPublication, before: dict[bytes, bytes]) -> bool:
    desired = os.fsencode(publication.path)
    for row in rows.split(b"\0"):
        if not row:
            continue
        metadata, name = row.split(b"\t", 1)
        if desired.startswith(name + b"/") or name.startswith(desired + b"/"):
            return False
        if name != desired:
            continue
        mode, blob, stage = metadata.split()
        if stage != b"0" or mode != b"100644":
            return False
        prior = before.get(name)
        if blob != publication.blob.encode() and prior != b"100644 blob " + blob:
            return False
    return True
