"""Ownership record of staged runtime inputs: what a sweep removes and what it keeps."""

from __future__ import annotations

import errno
import hashlib
import json
import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from booley.flows.sim import staged_input_ledger
from booley.flows.sim.staged_input_ledger import StagedInputLedger, sweep_cancelled_run_inputs
from booley.runtime.pid import capture_process_identity
from tests.conftest import symlink_or_skip
from tests.lifecycle_lock_support import held_lifecycle_lock

pytestmark = pytest.mark.skipif(
    capture_process_identity(os.getpid()) is None, reason="no capturable process identity"
)


@pytest.fixture
def case(tmp_path: Path) -> Iterator[_Case]:
    made = _Case(tmp_path)
    yield made
    made.ledger.discard()


def _dead_owner() -> dict[str, object]:
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    identity = capture_process_identity(child.pid)
    child.wait()
    if identity is None:  # pragma: no cover - the child exited before it was observed
        pytest.skip("the child exited before its identity was captured")
    return identity.to_payload()


# A namespace scope parses only with a Linux identity kind, which Windows owners lack.
_FOREIGN_SCOPE = {"identity_kind": "linux-procfs-start"}


def _refuse_locks(*_args: object, **_kwargs: object) -> None:
    raise OSError(errno.ENOLCK, "no locks")


class _Case:
    """A run directory with one record of every kind, owned by a process that is gone."""

    def __init__(self, tmp_path: Path) -> None:
        self.booley = tmp_path / ".booley"
        self.run = tmp_path / "run"
        self.run.mkdir()
        self.ledger = StagedInputLedger(self.booley, self.run)
        self.directory, self.file, self.link = Path("d"), Path("d/f.bin"), Path("d/link")
        self.stage(self.directory, "directory", None)
        (self.run / "d").mkdir()
        self.ledger.created(self.directory)
        self.stage(self.file, "file", "sha256:" + hashlib.sha256(b"x").hexdigest())
        (self.run / self.file).write_bytes(b"x")
        self.ledger.created(self.file)
        self.stage(self.link, "link", str(tmp_path / "gone"))
        symlink_or_skip(self.run / self.link, tmp_path / "gone")
        self.ledger.created(self.link)
        self.record = next((self.booley / ".runtime/staged-runtime-inputs").glob("*.json"))
        self.lock = self.record.with_suffix(".lock")

    def stage(self, relative: Path, kind: str, identity: str | None) -> None:
        self.ledger.will_create(relative, kind=kind, identity=identity)

    def set_owner(self, owner: dict[str, object]) -> None:
        document = json.loads(self.record.read_text(encoding="utf-8"))
        document["owner"] = owner
        self.record.write_text(json.dumps(document), encoding="utf-8")

    def die(self, scope: str | None = None, *, locked: bool = True) -> None:
        """Leave what a killed owner leaves: the record, and its lock released."""
        document = json.loads(self.record.read_text(encoding="utf-8"))
        document["owner"] = {
            **_dead_owner(),
            **({**_FOREIGN_SCOPE, "identity_scope": scope} if scope else {}),
        }
        if not locked:
            document["version"] = 1
        self.ledger.discard()
        self.record.write_text(json.dumps(document), encoding="utf-8")
        if locked:
            self.lock.touch()


@pytest.mark.parametrize(
    ("scope", "locked"),
    [(None, True), ("pid:[earlier-lifetime]", True), (None, False)],
    ids=["same-namespace", "earlier-lifetime", "same-namespace-lockless"],
)
def test_a_dead_owners_objects_are_removed_and_the_record_goes(
    case: _Case, scope: str | None, locked: bool
) -> None:
    case.die(scope, locked=locked)

    assert sweep_cancelled_run_inputs(case.booley) == frozenset()

    assert list(case.run.iterdir()) == []
    assert not case.record.exists() and not case.lock.exists()


@pytest.mark.parametrize("scope", [None, "pid:[earlier-lifetime]"])
def test_a_held_lock_keeps_the_record_whatever_the_owners_namespace(
    case: _Case, scope: str | None
) -> None:
    case.die(scope)

    with held_lifecycle_lock(case.lock):
        assert sweep_cancelled_run_inputs(case.booley) == frozenset()

    assert (case.run / case.file).exists() and (case.run / case.link).is_symlink()
    assert case.record.exists() and case.lock.exists()


@pytest.mark.parametrize("failure", ["refused", "lock-file-gone"])
def test_a_lock_that_cannot_be_taken_keeps_the_record(
    case: _Case, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    case.die()
    if failure == "refused":
        # A filesystem without lock support is a boundary no public entry point reaches.
        monkeypatch.setattr(staged_input_ledger, "acquire_file_lock", _refuse_locks)
    else:
        case.lock.unlink()

    assert sweep_cancelled_run_inputs(case.booley) == frozenset()

    assert (case.run / case.file).exists() and case.record.exists()


@pytest.mark.parametrize("locked", [True, False])
def test_a_live_or_foreign_owner_is_never_acted_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, locked: bool
) -> None:
    if not locked:
        # No public entry point makes a filesystem refuse locks.
        monkeypatch.setattr(staged_input_ledger, "acquire_file_lock", _refuse_locks)
    case = _Case(tmp_path)
    assert case.lock.exists() is locked
    assert sweep_cancelled_run_inputs(case.booley) == frozenset()
    case.set_owner({**_dead_owner(), **_FOREIGN_SCOPE, "identity_scope": "pid:[foreign]"})
    assert sweep_cancelled_run_inputs(case.booley) == frozenset()
    assert (case.run / case.file).exists() and (case.run / case.link).is_symlink()
    assert case.record.exists()
    case.ledger.discard()


@pytest.mark.parametrize(
    "content",
    [b"not json", b"[]", b'{"kind": "other"}', b"x" * (2 * 1024 * 1024), b"\xff"],
)
def test_an_unrecognised_record_is_left_alone(case: _Case, content: bytes) -> None:
    case.record.write_bytes(content)
    sweep_cancelled_run_inputs(case.booley)
    assert case.record.read_bytes() == content
    assert (case.run / case.file).exists()


@pytest.mark.parametrize("field", ["owner", "entries", "run_directory"])
def test_a_malformed_record_is_left_alone(case: _Case, field: str) -> None:
    document = json.loads(case.record.read_text(encoding="utf-8"))
    document[field] = (
        [{"path": 1}] if field == "entries" else "relative" if field != "owner" else {}
    )
    case.record.write_text(json.dumps(document), encoding="utf-8")
    sweep_cancelled_run_inputs(case.booley)
    assert (case.run / case.file).exists()
    assert case.record.exists()


def test_a_changed_object_stays_recorded_and_is_named(case: _Case) -> None:
    case.die()
    (case.run / case.file).write_bytes(b"changed by the user")
    (case.run / "d/user.txt").write_bytes(b"user")

    retained = sweep_cancelled_run_inputs(case.booley)

    assert retained == {case.run.resolve() / case.file, case.run.resolve() / case.directory}
    assert (case.run / case.file).read_bytes() == b"changed by the user"
    assert not (case.run / case.link).is_symlink()
    assert StagedInputLedger(case.booley, case.run).is_leftover(case.file)
    document = json.loads(case.record.read_text(encoding="utf-8"))
    assert [entry["path"] for entry in document["entries"]] == ["d", "d/f.bin"]


def test_user_content_beside_the_leftovers_is_kept_without_a_record(case: _Case) -> None:
    case.die()
    (case.run / "d/user.txt").write_bytes(b"user")

    assert sweep_cancelled_run_inputs(case.booley) == frozenset()

    assert [path.name for path in (case.run / "d").iterdir()] == ["user.txt"]
    assert not case.record.exists()


def test_a_removed_or_retyped_object_is_not_deleted(case: _Case) -> None:
    case.die()
    (case.run / case.link).unlink()
    (case.run / case.file).unlink()
    (case.run / case.file).mkdir()

    retained = sweep_cancelled_run_inputs(case.booley)

    assert retained == {case.run.resolve() / case.file, case.run.resolve() / case.directory}
    assert (case.run / case.file).is_dir()


def test_a_vanished_run_directory_drops_the_record(case: _Case) -> None:
    case.die()
    (case.run / case.link).unlink()
    (case.run / case.file).unlink()
    (case.run / "d").rmdir()
    case.run.rmdir()

    assert sweep_cancelled_run_inputs(case.booley) == frozenset()
    assert not case.record.exists()


def test_an_unreadable_object_keeps_its_entry(case: _Case, monkeypatch) -> None:
    case.die()

    def unreadable(_path: Path) -> str:
        raise PermissionError("denied")

    monkeypatch.setattr(staged_input_ledger, "_sha256", unreadable)
    retained = sweep_cancelled_run_inputs(case.booley)

    assert case.run.resolve() / case.file in retained
    assert case.record.exists()


def test_nothing_is_recorded_without_a_process_identity(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(staged_input_ledger, "capture_process_identity", lambda _pid: None)
    ledger = StagedInputLedger(tmp_path / ".booley", tmp_path)
    ledger.will_create(Path("x"), kind="file", identity="sha256:0")
    (tmp_path / "x").write_bytes(b"")
    ledger.created(Path("x"))

    assert sweep_cancelled_run_inputs(tmp_path / ".booley") == frozenset()
    assert not (tmp_path / ".booley").exists()


def test_a_missing_record_directory_sweeps_nothing(tmp_path: Path) -> None:
    assert sweep_cancelled_run_inputs(tmp_path / "absent") == frozenset()


def test_an_entry_is_retired_before_its_object_is_unlinked(case: _Case, monkeypatch) -> None:
    case.die()
    seen: list[list[str]] = []
    real_unlink = Path.unlink

    def watching_unlink(self: Path, *args: object, **kwargs: object) -> None:
        if self.name == "f.bin":
            document = json.loads(case.record.read_text(encoding="utf-8"))
            seen.append([entry["path"] for entry in document["entries"]])
        real_unlink(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "unlink", watching_unlink)
    sweep_cancelled_run_inputs(case.booley)

    assert seen == [["d"]]


def test_an_object_replaced_while_it_was_hashed_is_not_deleted(case: _Case, monkeypatch) -> None:
    case.die()
    real_sha256 = staged_input_ledger._sha256

    def replacing(path: Path) -> str:
        digest = real_sha256(path)
        successor = path.with_name("successor")
        successor.write_bytes(b"x")
        successor.replace(path)  # a new inode: the old one is only freed afterwards
        return digest

    monkeypatch.setattr(staged_input_ledger, "_sha256", replacing)
    sweep_cancelled_run_inputs(case.booley)

    assert (case.run / case.file).read_bytes() == b"x"


def test_a_link_is_recorded_with_the_target_text_the_os_reports(case: _Case) -> None:
    case.ledger.will_create(Path("other"), kind="link", identity="requested spelling")
    symlink_or_skip(case.run / "other", case.run.parent / "gone")
    case.ledger.created(Path("other"))

    document = json.loads(case.record.read_text(encoding="utf-8"))
    recorded = {entry["path"]: entry.get("identity") for entry in document["entries"]}
    assert recorded["other"] == str((case.run / "other").readlink())
