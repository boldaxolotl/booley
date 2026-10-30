"""Project declarations protect files, both memberships, modes, and Goal lifecycle."""

from __future__ import annotations

import json
import os

import pytest

from booley.goals.binding import bind_run
from booley.goals.entry import EntryEnvironment, EntryRequest, GoalEntryError, enter_goal_mode
from booley.goals.finish import finish_goal
from booley.goals.lifecycle import LifecycleError
from booley.goals.model import GoalRecord, GoalRecordFormatError, parse_goal_args
from booley.goals.protected_inputs import ProtectedInputRoots, snapshot_protected_inputs
from booley.goals.publication import EvidenceDiscarded, PublicationGate
from booley.goals.store import GoalStore
from booley.goals.warnings import protected_warnings
from tests.conftest import symlink_or_skip
from tests.goals.conftest import TOP_CORE, git, install_paired_project
from tests.goals.test_finish import environment, request
from tests.goals.test_status import publish

# Goal entry, publication, and Finish spawn many Git processes. The slowest case
# took 34.4s on Windows CI; its 3x/30s-rounded budget is 120s, above CI's
# unannotated 60s default and its 50% headroom limit.
pytestmark = pytest.mark.timeout(120)


def configure(layout, declarations, files=None):
    config = "[project]\nname='demo'\nprotected_inputs=" + json.dumps(declarations) + "\n"
    (layout.worktree / ".booley/booley.toml").write_text(config)
    for relative, content in (files or {"Makefile": b"all:\n\ttrue\n"}).items():
        path = layout.worktree / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        git(layout.worktree, "add", relative)
    git(layout.worktree, "commit", "-qm", "declared fixtures")


def enter(layout):
    call = EntryRequest(
        layout.worktree, "protected", parse_goal_args([{"family": "LINT", "target": "top"}])
    )
    result = enter_goal_mode(
        call, EntryEnvironment(layout.control, routing_guidance="Explicit test routing guidance.")
    )
    layout.record = result.record
    return result


def warnings(layout):
    return protected_warnings(layout.record, layout.control, layout.worktree)


def test_file_warning_and_restoration(layout):
    configure(layout, ["Makefile"])
    result = enter(layout)
    assert "Makefile" in result.render() and "abandon and re-entry" in result.rules
    original = (layout.worktree / "Makefile").read_bytes()
    (layout.worktree / "Makefile").write_bytes(b"all:\n\tfalse\n")
    assert any("Makefile" in reason and "contents" in reason for reason in warnings(layout))
    (layout.worktree / "Makefile").write_bytes(original)
    assert warnings(layout) == ()


@pytest.mark.parametrize("declaration", ["firmware/", "firmware/**"])
@pytest.mark.parametrize("change", ["add", "delete", "rename", "chmod"])
def test_membership_and_executable_restore(layout, declaration, change):
    if change == "chmod" and os.name == "nt":
        pytest.skip("Windows does not expose executable bits")
    configure(layout, [declaration], {"firmware/main.c": b"int main() {}\n"})
    enter(layout)
    path = layout.worktree / "firmware/main.c"
    original_mode = path.stat().st_mode
    if change == "add":
        (path.parent / "new.c").write_bytes(b"new\n")
    elif change == "delete":
        path.unlink()
    elif change == "rename":
        path.rename(path.parent / "renamed.c")
    else:
        path.chmod(original_mode ^ 0o111)
    reasons = warnings(layout)
    assert reasons and all(declaration in reason and "firmware/" in reason for reason in reasons)
    if change == "add":
        (path.parent / "new.c").unlink()
    elif change == "delete":
        path.write_bytes(b"int main() {}\n")
    elif change == "rename":
        (path.parent / "renamed.c").rename(path)
    else:
        path.chmod(original_mode)
    assert warnings(layout) == ()


def test_segment_globs_zero_segments_dotfiles_and_overlap(layout):
    files = {
        "firmware/a.c": b"a",
        "firmware/nested/b.c": b"b",
        "firmware/.hidden.c": b"h",
        "firmware/no.txt": b"no",
    }
    configure(layout, ["firmware/**/*.c", "firmware/a.?"], files)
    enter(layout)
    capture = layout.record.declared_protected_inputs
    assert capture is not None
    assert [row.path for row in capture.working] == [
        "firmware/.hidden.c",
        "firmware/a.c",
        "firmware/nested/b.c",
    ]
    assert layout.record.protected_paths.count("worktree:firmware/a.c") == 1


@pytest.mark.parametrize("change", ["add", "delete", "rename", "chmod"])
def test_committed_membership_and_mode_cannot_hide_behind_restored_disk(layout, change):
    if change == "chmod" and os.name == "nt":
        pytest.skip("Windows does not expose executable bits")
    configure(layout, ["firmware/"], {"firmware/a.c": b"a"})
    enter(layout)
    path = layout.worktree / "firmware/a.c"
    original_mode = path.stat().st_mode
    if change == "add":
        (path.parent / "new.c").write_bytes(b"new")
    elif change == "delete":
        path.unlink()
    elif change == "rename":
        path.rename(path.parent / "new.c")
    else:
        path.chmod(original_mode ^ 0o111)
    git(layout.worktree, "add", "firmware")
    git(layout.worktree, "commit", "-qm", "protected edit")
    if change in {"add", "rename"}:
        (path.parent / "new.c").unlink()
    if change in {"delete", "rename"}:
        path.write_bytes(b"a")
    path.chmod(original_mode)
    assert any("in HEAD" in reason for reason in warnings(layout))
    assert not bind_run(GoalStore(layout.control), layout.worktree, "run").eligible


@pytest.mark.parametrize(
    "declarations,files,message",
    [
        (["missing.c"], {"Makefile": b"recipe"}, "missing.c.*matches no files"),
        (["firmware"], {"firmware/a.c": b"a"}, "firmware/"),
        ([".booley/goals/"], {"Makefile": b"recipe"}, "Goal Record"),
    ],
)
def test_entry_refusals_rollback(layout, declarations, files, message):
    configure(layout, declarations, files)
    before = git(layout.worktree, "symbolic-ref", "HEAD")
    with pytest.raises(GoalEntryError, match=message):
        enter(layout)
    assert git(layout.worktree, "symbolic-ref", "HEAD") == before
    assert GoalStore(layout.control).active_for_worktree(layout.worktree) is None


@pytest.mark.parametrize("ignored", [False, True])
def test_untracked_matches_are_not_skipped(layout, ignored):
    configure(layout, ["firmware/"], {"firmware/a.c": b"a"})
    if ignored:
        (layout.main / ".git/info/exclude").write_text("/.booley\nfirmware/new.c\n")
    (layout.worktree / "firmware/new.c").write_bytes(b"new")
    with pytest.raises(GoalEntryError, match=r"firmware/new\.c"):
        enter(layout)


def test_nested_project_repository_is_refused(layout, tmp_path):
    project = install_paired_project(layout, tmp_path)
    (project / "booley.toml").write_text("[project]\nprotected_inputs=['.booley/booley.toml']\n")
    git(project, "add", "booley.toml")
    git(project, "commit", "-qm", "nested declaration")
    with pytest.raises(GoalEntryError, match="nested repository or submodule"):
        enter(layout)


def test_configuration_change_is_a_violation_even_with_identical_files(layout):
    configure(layout, ["Makefile"])
    enter(layout)
    config = layout.worktree / ".booley/booley.toml"
    original = config.read_text()
    config.write_text(original.replace('["Makefile"]', '["Makefile", "Make*"]'))
    assert any("declarations changed" in reason for reason in warnings(layout))
    config.write_text(original)
    assert warnings(layout) == ()


def test_capture_round_trip_and_unknown_version_refusal(layout):
    configure(layout, ["Makefile"])
    enter(layout)
    encoded = json.loads(json.dumps(layout.record.to_json()))
    assert GoalRecord.from_json(encoded) == layout.record
    encoded["declared_protected_inputs"]["schema"] = "booley-declared-protected-inputs/99"
    with pytest.raises(GoalRecordFormatError, match="not supported"):
        GoalRecord.from_json(encoded)


def test_mount_alias_uses_the_same_capture(layout, tmp_path):
    configure(layout, ["Makefile"])
    enter(layout)
    alias = tmp_path / "alias"
    symlink_or_skip(alias, layout.worktree, target_is_directory=True)
    snapshot = snapshot_protected_inputs(ProtectedInputRoots(alias, layout.control))
    assert snapshot.working_digest == layout.record.protected_digest
    assert snapshot.head_digest == layout.record.protected_head_digest


def test_declared_builtin_overlap_reports_one_violation(layout):
    configure(layout, ["FUSESOC_IGNORE"], {"FUSESOC_IGNORE": b"original"})
    enter(layout)
    assert layout.record.protected_paths.count("worktree:FUSESOC_IGNORE") == 1
    (layout.worktree / "FUSESOC_IGNORE").write_bytes(b"edited")
    assert len(warnings(layout)) == 1 and "FUSESOC_IGNORE" in warnings(layout)[0]


@pytest.mark.timeout(150)
def test_run_admission_publication_and_finish_restore(layout):
    configure(layout, ["Makefile"], {"Makefile": b"all:\n\ttrue\n", "top.core": TOP_CORE.encode()})
    enter(layout)
    publish(layout)
    admitted = bind_run(GoalStore(layout.control), layout.worktree, "admitted")
    path = layout.worktree / "Makefile"
    original = path.read_bytes()
    path.write_bytes(b"all:\n\tfalse\n")
    assert not bind_run(GoalStore(layout.control), layout.worktree, "edited").eligible
    with (
        pytest.raises(EvidenceDiscarded, match="protected input changed"),
        PublicationGate(admitted).publishing(set()),
    ):
        pass
    with pytest.raises(LifecycleError, match="Makefile"):
        finish_goal(request(layout), environment(layout))
    path.write_bytes(original)
    assert warnings(layout) == ()
    assert finish_goal(request(layout), environment(layout))["status"] == "finished"


@pytest.mark.parametrize("drift", ["type", "parent redirect"])
def test_file_type_and_parent_redirect_are_detected(layout, tmp_path, drift):
    configure(layout, ["firmware/a.c"], {"firmware/a.c": b"a"})
    enter(layout)
    path = layout.worktree / "firmware/a.c"
    if drift == "type":
        path.unlink()
        symlink_or_skip(path, "original.c")
        (path.parent / "original.c").write_bytes(b"a")
    else:
        path.parent.rename(layout.worktree / "original-firmware")
        symlink_or_skip(
            layout.worktree / "firmware", "original-firmware", target_is_directory=True
        )
    assert warnings(layout)


def test_contained_tracked_symlink_uses_target_contents(layout):
    configure(layout, ["recipe-link"], {"Makefile": b"recipe"})
    link = layout.worktree / "recipe-link"
    symlink_or_skip(link, "Makefile")
    git(layout.worktree, "add", "recipe-link")
    git(layout.worktree, "commit", "-qm", "tracked link")
    enter(layout)
    (layout.worktree / "Makefile").write_bytes(b"edited")
    assert any("recipe-link" in reason for reason in warnings(layout))


def test_symlink_escape_is_refused(layout, tmp_path):
    configure(layout, ["recipe-link"], {"Makefile": b"recipe"})
    external = tmp_path / "external"
    external.write_bytes(b"recipe")
    symlink_or_skip(layout.worktree / "recipe-link", external)
    git(layout.worktree, "add", "recipe-link")
    git(layout.worktree, "commit", "-qm", "escaping link")
    with pytest.raises(GoalEntryError, match="escapes the Project Root"):
        enter(layout)


def test_real_submodule_is_refused(layout, tmp_path):
    configure(layout, ["firmware/"], {"firmware/a.c": b"a"})
    repository = tmp_path / "submodule"
    repository.mkdir()
    git(repository, "init", "-qb", "main")
    (repository / "input.c").write_bytes(b"submodule")
    git(repository, "add", "input.c")
    git(repository, "commit", "-qm", "submodule input")
    git(
        layout.worktree,
        "-c",
        "protocol.file.allow=always",
        "submodule",
        "add",
        str(repository),
        "firmware/sub",
    )
    git(layout.worktree, "commit", "-qm", "submodule")
    with pytest.raises(GoalEntryError, match="submodule"):
        enter(layout)


@pytest.mark.parametrize(
    "field,value",
    [
        ("declarations", []),
        ("working", []),
        ("head", []),
        ("working", "files"),
    ],
)
def test_malformed_saved_capture_is_refused(layout, field, value):
    configure(layout, ["Makefile"])
    enter(layout)
    encoded = layout.record.to_json()
    encoded["declared_protected_inputs"][field] = value
    with pytest.raises(GoalRecordFormatError, match="declared_protected_inputs"):
        GoalRecord.from_json(encoded)


def test_shared_configuration_does_not_override_worktree_declarations(layout):
    configure(layout, ["Makefile"])
    (layout.control / "booley.toml").write_text("[project]\nprotected_inputs=['unrelated.c']\n")
    enter(layout)
    assert layout.record.declared_protected_inputs.declarations == ("Makefile",)


@pytest.mark.skipif(os.name == "nt", reason="Windows does not expose executable bits")
def test_committed_mode_on_builtin_overlap_is_detected_with_working_mode_restored(layout):
    configure(layout, ["FUSESOC_IGNORE"], {"FUSESOC_IGNORE": b"original"})
    enter(layout)
    path = layout.worktree / "FUSESOC_IGNORE"
    original = path.stat().st_mode
    path.chmod(original ^ 0o111)
    git(layout.worktree, "add", "FUSESOC_IGNORE")
    git(layout.worktree, "commit", "-qm", "mode")
    path.chmod(original)
    assert warnings(layout) == (
        "protected input 'FUSESOC_IGNORE': FUSESOC_IGNORE executable bit changed in HEAD",
    )


# Current-layout digests; the digest has one format with or without declarations.
@pytest.mark.parametrize(
    "declaration_line,working,head",
    [
        pytest.param(
            "",
            "ef3e41f3280da22549800e4a8037a875f5e011817209113dc090c10b67965498",
            "0d69559c510f9deaa680dc2a65816de7429694169f13da2897a608e6b60acc17",
            id="no-key",
        ),
        pytest.param(
            "protected_inputs=[]\n",
            "85c55fecf34e8c1793d2af6afcb56881d9d29ba354e7ca8c388cab0f6ca947a0",
            "db56eee68f5778ca2328e85f9fe3e2a726efbd6c2b31ef85cb05b6ac4e36e501",
            id="empty-list",
        ),
    ],
)
def test_no_declarations_have_stable_current_layout_digest(
    layout, declaration_line, working, head
):
    # Normalize the fixture's text writes to LF on Windows as well as POSIX.
    (layout.worktree / "booley.toml").write_bytes(b"# root config\n")
    git(layout.worktree, "add", "booley.toml")
    git(layout.worktree, "commit", "--allow-empty", "-qm", "canonical newline fixture")
    for project in (layout.control, layout.worktree / ".booley"):
        (project / "booley.toml").write_bytes(b"[project]\nname = 'demo'\n")
        (project / "mcp_tools/tool.py").write_bytes(b"# custom tool\n")
    config = "[project]\nname = 'demo'\n" + declaration_line
    (layout.worktree / ".booley/booley.toml").write_bytes(config.encode())
    snapshot = snapshot_protected_inputs(ProtectedInputRoots(layout.worktree, layout.control))
    assert snapshot.declared is None
    assert snapshot.working_digest == "sha256:" + working
    assert snapshot.head_digest == "sha256:" + head


def test_glob_capture_accepts_literal_pattern_characters_in_file_names(layout):
    configure(layout, ["firmware/*.c"], {"firmware/input[0].c": b"a"})
    enter(layout)
    assert GoalStore(layout.control).load(layout.record.id) == layout.record
    (layout.worktree / "firmware/input[0].c").write_bytes(b"changed")
    assert "firmware/input[0].c" in warnings(layout)[0]
