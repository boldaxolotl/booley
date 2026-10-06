"""Protected inputs: the resolver contract, both digests, symlinks, and path spellings (D7)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from booley.goals.entry import EntryEnvironment, EntryRequest, enter_goal_mode
from booley.goals.model import parse_goal_args
from booley.goals.protected_inputs import (
    ProtectedInputError,
    ProtectedInputRoots,
    ProtectedPath,
    ProtectedSnapshot,
    RootKind,
    protected_input_violations,
    resolve_protected_inputs,
    snapshot_protected_inputs,
)
from booley.goals.store import GoalStore
from tests.conftest import symlink_or_skip
from tests.goals.conftest import git

WORKING = "a protected input differs from its state at entry"
IN_HEAD = "a protected input differs from its state at entry in HEAD"


def _roots(layout: SimpleNamespace, worktree: Path | None = None) -> ProtectedInputRoots:
    return ProtectedInputRoots(worktree or layout.worktree, layout.control)


def _enter(layout: SimpleNamespace) -> None:
    request = EntryRequest(
        layout.worktree, "uart-fix", parse_goal_args([{"family": "lint", "target": "top"}])
    )
    enter_goal_mode(request, EntryEnvironment(project_dir=layout.control))


def _violations(layout: SimpleNamespace, worktree: Path | None = None) -> list[str]:
    record = GoalStore(layout.control).active_for_worktree(layout.worktree)
    assert record is not None and record.protected_digest is not None
    return protected_input_violations(
        record.protected_paths,
        record.protected_digest,
        record.protected_head_digest,
        _roots(layout, worktree),
    )


def _check(
    layout: SimpleNamespace, snapshot: ProtectedSnapshot, roots: ProtectedInputRoots | None = None
) -> list[str]:
    return protected_input_violations(
        snapshot.encoded_paths,
        snapshot.working_digest,
        snapshot.head_digest,
        roots or _roots(layout),
    )


# ---------------------------------------------------------------------------
# The resolver table
# ---------------------------------------------------------------------------


def test_every_candidate_a_consumer_could_read_is_recorded(layout: SimpleNamespace) -> None:
    encoded = {path.encode() for path in resolve_protected_inputs(_roots(layout))}

    for directory in (
        "project:",
        "worktree:.booley_project/",
        "worktree:.booley/project/",
        "main:.booley_project/",
        "main:.booley/project/",
        "worktree:",
    ):
        assert f"{directory}booley.toml" in encoded
        assert f"{directory}pipeline.toml" in encoded  # resolve_toml's legacy fallback
    assert {
        "worktree:.booley_project/.managed",
        "main:.booley_project/.managed",
        "project:.managed",
        "project:hooks",
        "worktree:.booley_project/hooks",
        "project:generators",
        "project:mcp_tools",
        "main:.booley_project/mcp_tools",
        "worktree:FUSESOC_IGNORE",
    } <= encoded
    assert "worktree:.booley_project/mcp_tools" not in encoded  # no consumer runs the copy


def test_the_path_list_is_the_same_from_another_spelling(
    layout: SimpleNamespace, tmp_path: Path
) -> None:
    alias = tmp_path / "alias"
    symlink_or_skip(alias, layout.worktree, target_is_directory=True)

    assert resolve_protected_inputs(_roots(layout, alias)) == resolve_protected_inputs(
        _roots(layout)
    )


def test_stored_paths_round_trip_and_reject_garbage() -> None:
    path = ProtectedPath(RootKind.WORKTREE, ".booley_project/booley.toml")

    assert ProtectedPath.decode(path.encode()) == path
    with pytest.raises(ProtectedInputError, match="unknown root"):
        ProtectedPath.decode("/abs/booley.toml")
    with pytest.raises(ProtectedInputError, match="no path"):
        ProtectedPath.decode("project:")


# ---------------------------------------------------------------------------
# What counts as a change
# ---------------------------------------------------------------------------


def test_entry_records_both_digests_and_an_untouched_goal_is_clean(
    layout: SimpleNamespace,
) -> None:
    _enter(layout)
    record = GoalStore(layout.control).active_for_worktree(layout.worktree)

    assert record is not None and record.protected_head_digest is not None
    assert record.protected_head_digest != record.protected_digest  # the views differ
    assert all(":" in path for path in record.protected_paths)
    assert _violations(layout) == []


@pytest.mark.parametrize(
    "relative",
    [
        "control/booley.toml",  # session-global config readers
        "snapshot/booley.toml",  # Flow readers given work_dir
        "control/mcp_tools/tool.py",  # custom MCP tools run from the session copy
    ],
)
def test_editing_an_input_a_run_reads_is_a_violation(
    layout: SimpleNamespace, relative: str
) -> None:
    _enter(layout)
    where, _, rest = relative.partition("/")
    base = layout.control if where == "control" else layout.worktree / ".booley_project"
    path = base / rest
    original = path.read_text(encoding="utf-8")

    path.write_text(original + "# edited\n", encoding="utf-8")
    assert _violations(layout) == [WORKING]

    path.write_text(original, encoding="utf-8")
    assert _violations(layout) == []


def test_editing_the_worktrees_unused_copy_is_not_a_violation(layout: SimpleNamespace) -> None:
    _enter(layout)
    unused = layout.worktree / ".booley_project" / "mcp_tools" / "tool.py"

    unused.write_text("# edited copy nobody runs\n", encoding="utf-8")

    assert _violations(layout) == []


@pytest.mark.parametrize(
    "relative",
    [
        ".booley_project/pipeline.toml",
        ".booley/project/booley.toml",
        "pipeline.toml",
    ],
)
def test_creating_a_fallback_configuration_file_is_a_violation(
    layout: SimpleNamespace, relative: str
) -> None:
    """Consumers fall back to these when the preferred file is missing."""
    _enter(layout)
    target = layout.worktree / relative
    target.parent.mkdir(parents=True, exist_ok=True)

    target.write_text("[project]\n", encoding="utf-8")

    assert _violations(layout) == [WORKING]


@pytest.mark.parametrize("where", ["worktree", "main"])
def test_a_git_hook_bundle_in_any_candidate_is_a_violation(
    layout: SimpleNamespace, where: str
) -> None:
    """The hook adapters pick the worktree's bundle file, else the main checkout's."""
    _enter(layout)
    root = layout.worktree if where == "worktree" else layout.main
    bundle = root / ".booley_project" / ".managed" / "project-git-hooks.pyz"
    bundle.parent.mkdir(parents=True, exist_ok=True)

    bundle.write_bytes(b"PK")

    assert _violations(layout) == [WORKING]


def test_adding_and_deleting_a_generator_file_are_violations(layout: SimpleNamespace) -> None:
    generators = layout.control / "generators"
    generators.mkdir()
    (generators / "gen.py").write_text("# generator\n", encoding="utf-8")
    _enter(layout)

    (generators / "extra.py").write_text("# added\n", encoding="utf-8")
    assert _violations(layout) == [WORKING]
    (generators / "extra.py").unlink()
    assert _violations(layout) == []
    (generators / "gen.py").unlink()
    assert _violations(layout) == [WORKING]


def test_adding_a_hook_is_a_violation(layout: SimpleNamespace) -> None:
    _enter(layout)
    (layout.control / "hooks").mkdir()
    (layout.control / "hooks" / "post-setup").write_text("#!/bin/sh\n", encoding="utf-8")

    assert _violations(layout) == [WORKING]


def test_main_checkout_copy_is_covered_when_the_session_project_is_elsewhere(
    layout: SimpleNamespace, tmp_path: Path
) -> None:
    """Without a snapshot, Flow readers fall back to the main checkout's file."""
    shared = tmp_path / "shared-project"
    shared.mkdir()
    (layout.worktree / ".booley_project" / "booley.toml").unlink()
    roots = ProtectedInputRoots(layout.worktree, shared)
    snapshot = snapshot_protected_inputs(roots)

    (layout.main / ".booley_project" / "booley.toml").write_text("# edited\n", encoding="utf-8")

    assert _check(layout, snapshot, roots) == [WORKING]


def test_python_bytecode_caches_are_not_inputs(layout: SimpleNamespace) -> None:
    _enter(layout)
    cache = layout.control / "mcp_tools" / "__pycache__"
    cache.mkdir()
    (cache / "tool.cpython-313.pyc").write_bytes(b"\0")

    assert _violations(layout) == []


def test_creating_a_missing_input_is_a_violation(layout: SimpleNamespace) -> None:
    _enter(layout)
    (layout.worktree / "FUSESOC_IGNORE").write_text("", encoding="utf-8")

    assert _violations(layout) == [WORKING]


def test_a_tampered_path_list_is_a_violation(layout: SimpleNamespace) -> None:
    snapshot = snapshot_protected_inputs(_roots(layout))
    paths = (*snapshot.encoded_paths[:-1], "worktree:elsewhere")

    (violation,) = protected_input_violations(
        paths, snapshot.working_digest, snapshot.head_digest, _roots(layout)
    )

    assert "resolve to different files" in violation


# ---------------------------------------------------------------------------
# HEAD view
# ---------------------------------------------------------------------------


def test_a_committed_edit_is_a_violation_even_after_reverting_the_file(
    layout: SimpleNamespace,
) -> None:
    _enter(layout)
    config = layout.worktree / "booley.toml"
    original = config.read_text(encoding="utf-8")
    config.write_text(original + "# committed\n", encoding="utf-8")
    git(layout.worktree, "commit", "-q", "-am", "edit config")

    config.write_text(original, encoding="utf-8")

    assert _violations(layout) == [IN_HEAD]


def test_a_committed_deletion_restored_as_untracked_bytes_is_a_violation(
    layout: SimpleNamespace,
) -> None:
    _enter(layout)
    config = layout.worktree / "booley.toml"
    original = config.read_bytes()
    git(layout.worktree, "rm", "-q", "booley.toml")
    git(layout.worktree, "commit", "-q", "-m", "drop config")

    config.write_bytes(original)  # same bytes, now untracked

    assert _violations(layout) == [IN_HEAD]


def test_a_crlf_working_copy_of_an_lf_blob_is_not_a_violation(layout: SimpleNamespace) -> None:
    """A checkout that converts line endings never differs from itself."""
    config = layout.worktree / "booley.toml"
    config.write_bytes(config.read_bytes().replace(b"\n", b"\r\n"))
    snapshot = snapshot_protected_inputs(_roots(layout))

    assert _check(layout, snapshot) == []


# ---------------------------------------------------------------------------
# Symlinks
# ---------------------------------------------------------------------------


def test_a_change_behind_a_file_symlink_is_a_violation(
    layout: SimpleNamespace, tmp_path: Path
) -> None:
    external = tmp_path / "external.toml"
    external.write_text("[project]\n", encoding="utf-8")
    config = layout.control / "booley.toml"
    config.unlink()
    symlink_or_skip(config, external)
    snapshot = snapshot_protected_inputs(_roots(layout))

    external.write_text("[project]\nname = 'changed'\n", encoding="utf-8")

    assert _check(layout, snapshot) == [WORKING]


def test_a_change_behind_a_directory_symlink_is_a_violation(
    layout: SimpleNamespace, tmp_path: Path
) -> None:
    external = tmp_path / "generators"
    external.mkdir()
    symlink_or_skip(layout.control / "generators", external, target_is_directory=True)
    snapshot = snapshot_protected_inputs(_roots(layout))

    (external / "gen.py").write_text("# new generator\n", encoding="utf-8")

    assert _check(layout, snapshot) == [WORKING]


def test_a_dangling_symlink_is_digested_and_its_target_appearing_is_a_change(
    layout: SimpleNamespace, tmp_path: Path
) -> None:
    missing = tmp_path / "not-yet.toml"
    symlink_or_skip(layout.worktree / "FUSESOC_IGNORE", missing)
    snapshot = snapshot_protected_inputs(_roots(layout))
    assert _check(layout, snapshot) == []

    missing.write_text("", encoding="utf-8")

    assert _check(layout, snapshot) == [WORKING]


def test_a_symlink_cycle_is_recorded_not_followed_forever(layout: SimpleNamespace) -> None:
    hooks = layout.control / "hooks"
    hooks.mkdir()
    symlink_or_skip(hooks / "loop", hooks, target_is_directory=True)

    snapshot = snapshot_protected_inputs(_roots(layout))

    assert _check(layout, snapshot) == []


# ---------------------------------------------------------------------------
# Path spellings
# ---------------------------------------------------------------------------


def test_another_spelling_with_missing_inputs_is_not_a_violation(
    layout: SimpleNamespace, tmp_path: Path
) -> None:
    """Absent inputs have no file identity, so paths are recorded relative to roots."""
    _enter(layout)
    alias = tmp_path / "alias"
    symlink_or_skip(alias, layout.worktree, target_is_directory=True)

    assert not (layout.worktree / "FUSESOC_IGNORE").exists()
    assert _violations(layout, alias) == []
