"""Ownership and transaction contracts for initialization filesystem plans."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

import pytest

from booley.harness.init_plan import (
    InitFilesystemRequest,
    InitPlanBlockedError,
    InitPreconditionError,
    InitTarget,
    Ownership,
    apply_init_plan,
    plan_init_filesystem,
)


@dataclass(frozen=True)
class _Probe:
    tracked: frozenset[Path] = frozenset()

    def is_tracked(self, _root: Path, path: Path) -> bool:
        return path in self.tracked


def _manifest(root: Path) -> dict[str, tuple[str, str, int]]:
    """Record node type, content/link digest, and mode without following links."""
    result: dict[str, tuple[str, str, int]] = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        mode = path.lstat().st_mode & 0o777
        if path.is_symlink():
            result[relative] = ("symlink", str(path.readlink()), mode)
        elif path.is_file():
            result[relative] = (
                "file",
                hashlib.sha256(path.read_bytes()).hexdigest(),
                mode,
            )
        elif path.is_dir():
            result[relative] = ("directory", "", mode)
    return result


def _write_target(path: Path, content: bytes = b"managed\n") -> InitTarget:
    return InitTarget.write_file(
        path,
        content,
        owner_marker=b"managed",
        reason="install managed config",
    )


def test_plan_classifies_absent_current_stale_and_user_owned(tmp_path: Path) -> None:
    absent = tmp_path / "absent.txt"
    current = tmp_path / "current.txt"
    stale = tmp_path / "stale.txt"
    foreign = tmp_path / "foreign.txt"
    current.write_bytes(b"managed\n")
    stale.write_bytes(b"managed old\n")
    foreign.write_bytes(b"personal\n")

    plan = plan_init_filesystem(
        InitFilesystemRequest(
            root=tmp_path,
            targets=tuple(_write_target(path) for path in (absent, current, stale, foreign)),
        ),
        _Probe(),
    )

    assert [action.ownership for action in plan.actions] == [
        Ownership.ABSENT,
        Ownership.MANAGED_CURRENT,
        Ownership.MANAGED_STALE,
        Ownership.USER_OWNED,
    ]
    assert plan.actions[-1].blocker


def test_blocker_prevents_every_planned_mutation(tmp_path: Path) -> None:
    new_file = tmp_path / "new.txt"
    foreign = tmp_path / "foreign.txt"
    foreign.write_text("mine\n", encoding="utf-8")
    before = _manifest(tmp_path)
    plan = plan_init_filesystem(
        InitFilesystemRequest(
            root=tmp_path,
            targets=(_write_target(new_file), _write_target(foreign)),
        ),
        _Probe(),
    )

    with pytest.raises(InitPlanBlockedError, match=r"foreign\.txt"):
        apply_init_plan(plan)

    assert _manifest(tmp_path) == before


def test_check_only_and_apply_use_the_same_decision_set(tmp_path: Path) -> None:
    target = tmp_path / "managed.txt"
    request = InitFilesystemRequest(root=tmp_path, targets=(_write_target(target),))
    before = _manifest(tmp_path)

    check_plan = plan_init_filesystem(request, _Probe())

    assert _manifest(tmp_path) == before
    apply_init_plan(check_plan)
    assert target.read_bytes() == b"managed\n"


def test_precondition_change_aborts_before_any_write(tmp_path: Path) -> None:
    first = tmp_path / "first.txt"
    second = tmp_path / "second.txt"
    second.write_text("managed old\n", encoding="utf-8")
    plan = plan_init_filesystem(
        InitFilesystemRequest(
            root=tmp_path, targets=(_write_target(first), _write_target(second))
        ),
        _Probe(),
    )
    second.write_text("changed after planning\n", encoding="utf-8")

    with pytest.raises(InitPreconditionError, match=r"second\.txt"):
        apply_init_plan(plan)

    assert not first.exists()
    assert second.read_text(encoding="utf-8") == "changed after planning\n"


def test_unreadable_existing_target_aborts_planning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "managed.txt"
    target.write_text("managed old\n", encoding="utf-8")
    read_bytes = Path.read_bytes

    def fail_for_target(path: Path) -> bytes:
        if path == target:
            raise PermissionError("denied")
        return read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", fail_for_target)

    with pytest.raises(PermissionError, match="denied"):
        plan_init_filesystem(
            InitFilesystemRequest(root=tmp_path, targets=(_write_target(target),)),
            _Probe(),
        )


def test_tracked_matching_and_conflicting_files_are_explicit(tmp_path: Path) -> None:
    matching = tmp_path / "matching.txt"
    conflicting = tmp_path / "conflicting.txt"
    matching.write_bytes(b"managed\n")
    conflicting.write_bytes(b"different\n")
    probe = _Probe(frozenset({matching, conflicting}))

    plan = plan_init_filesystem(
        InitFilesystemRequest(
            root=tmp_path,
            targets=(_write_target(matching), _write_target(conflicting)),
        ),
        probe,
    )

    assert plan.actions[0].ownership is Ownership.TRACKED_MATCH
    assert plan.actions[1].ownership is Ownership.CONFLICT
    assert plan.actions[1].blocker


def test_target_cannot_escape_through_symlinked_parent(tmp_path: Path) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir()
    linked_parent = tmp_path / "managed"
    try:
        linked_parent.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation not permitted on this host")

    with pytest.raises(ValueError, match="outside project root"):
        plan_init_filesystem(
            InitFilesystemRequest(
                root=tmp_path,
                targets=(_write_target(linked_parent / "config.txt"),),
            ),
            _Probe(),
        )

    assert list(outside.iterdir()) == []
