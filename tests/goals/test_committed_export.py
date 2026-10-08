"""Real Git oracle controls for pinned working bytes and recursive local gitlinks."""

from __future__ import annotations

import stat
import subprocess
from pathlib import Path

import pytest

from booley.goals.committed_export import export_tree, materializations_unchanged
from booley.goals.input_identity import root_bindings
from booley.goals.lifecycle import LifecycleError
from tests.goals.conftest import git

pytestmark = pytest.mark.timeout(120)


def repository(path):
    path.mkdir()
    git(path, "init", "-q", "-b", "main")
    git(path, "config", "core.autocrlf", "false")
    return path


def raw_blob(root, content):
    return (
        subprocess.run(
            ["git", "hash-object", "-w", "--stdin"],
            cwd=root,
            input=content,
            capture_output=True,
            check=True,
            timeout=30,
        )
        .stdout.strip()
        .decode()
    )


@pytest.mark.parametrize(
    "attr,content",
    [
        ("-text eol=crlf", b"one\ntwo\n"),
        ("text eol=crlf", b"one\r\ntwo\n"),
        ("text=auto eol=crlf", b"one\r\ntwo\n"),
        ("text=auto eol=crlf", b"\1\2\3\4\5\6\n"),
        ("text eol=crlf", b"one\ntwo\n"),
        ("text eol=lf", b"one\ntwo\n"),
    ],
)
def test_materialized_working_bytes_match_real_git_oracle(tmp_path, attr, content):
    root = repository(tmp_path / "source")
    (root / ".gitattributes").write_text("rtl.v " + attr + "\n", newline="\n")
    git(root, "add", ".gitattributes")
    git(
        root,
        "update-index",
        "--add",
        "--cacheinfo",
        "100644," + raw_blob(root, content) + ",rtl.v",
    )
    git(root, "commit", "-qm", "raw pinned input")
    git(root, "checkout-index", "--all", "--force")
    expected = (root / "rtl.v").read_bytes()
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    before = (root / ".git/index").read_bytes()
    proof = export_tree(root, git(root, "rev-parse", "HEAD"), scratch / "rtl", scratch=scratch)
    assert (scratch / "rtl/rtl.v").read_bytes() == expected
    assert (root / ".git/index").read_bytes() == before
    assert proof[0]["policy"]["autocrlf"] == "false"


@pytest.mark.parametrize("ambient", [False, True])
def test_nonhermetic_filter_policy_is_actionable_without_execution(tmp_path, ambient):
    root = repository(tmp_path / "source")
    (root / "rtl.v").write_text("design\n", newline="\n")
    (root / ".gitattributes").write_text(
        "" if ambient else "rtl.v filter=external\n", newline="\n"
    )
    git(root, "add", "-A")
    git(root, "commit", "-qm", "base")
    if ambient:
        (root / ".git/info/attributes").write_text("rtl.v text eol=crlf\n", newline="\n")
    sentinel = tmp_path / "executed"
    git(root, "config", "filter.external.smudge", "touch " + str(sentinel))
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    with pytest.raises(LifecycleError, match=r"unsupported .*input"):
        export_tree(root, git(root, "rev-parse", "HEAD"), scratch / "rtl", scratch=scratch)
    assert not sentinel.exists()


def nested_modules(tmp_path):
    leaf = repository(tmp_path / "leaf")
    (leaf / "rtl.v").write_bytes(b"original committed design\n")
    git(leaf, "add", "-A")
    git(leaf, "commit", "-qm", "leaf")
    middle = repository(tmp_path / "middle")
    git(middle, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(leaf), "nested")
    git(middle, "commit", "-qam", "middle")
    root = repository(tmp_path / "root")
    git(root, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(middle), "module")
    git(root, "commit", "-qam", "root")
    git(root, "-c", "protocol.file.allow=always", "submodule", "update", "--init", "--recursive")
    # Clones do not inherit repository-local config from their source. Runtime
    # projection must see the same explicit LF policy as fixture commit helpers.
    for checkout in (middle / "nested", root / "module", root / "module/nested"):
        git(checkout, "config", "core.autocrlf", "false")
    return root


def test_recursive_submodules_use_exact_original_gitlink_not_current_head(tmp_path):
    root = nested_modules(tmp_path)
    source = root / "module/nested"
    original = git(root, "rev-parse", "HEAD")
    (source / "rtl.v").write_bytes(b"new committed design\n")
    git(source, "commit", "-qam", "later leaf")
    git(root / "module", "add", "nested")
    git(root / "module", "commit", "-qm", "later middle")
    git(root, "add", "module")
    git(root, "commit", "-qm", "later root")
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    proof = export_tree(root, original, scratch / "rtl", scratch=scratch, baseline=True)
    assert (scratch / "rtl/module/nested/rtl.v").read_bytes() == b"original committed design\n"
    assert len(proof) == 3 and all(row["submodule"] for row in proof[1:])
    final = scratch / "final"
    export_tree(root, git(root, "rev-parse", "HEAD"), final, scratch=scratch)
    assert (final / "module/nested/rtl.v").read_bytes() == b"new committed design\n"


def test_submodule_dirty_or_changed_git_administration_refuses(tmp_path):
    root = nested_modules(tmp_path)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    proof = export_tree(root, git(root, "rev-parse", "HEAD"), scratch / "rtl", scratch=scratch)
    roots = root_bindings({"rtl": root, "project": root})
    capture = {"committed_materializations": proof, "path_roots": roots}
    source = root / "module/nested"
    (source / "rtl.v").write_bytes(b"dirty\n")
    with pytest.raises(LifecycleError, match="submodule changes"):
        materializations_unchanged(capture, roots)
    git(source, "checkout", "--", "rtl.v")
    clone = tmp_path / "same-commit"
    git(tmp_path, "clone", "-q", str(source), str(clone))
    git(clone, "config", "core.autocrlf", "false")
    gitdir = source / ".git"
    # Replace the owned fixture pointer instead of truncating Git's existing
    # file, whose Windows attributes can prevent opening it for writing.
    gitdir.chmod(gitdir.stat().st_mode | stat.S_IWRITE)
    gitdir.unlink()
    gitdir.write_text("gitdir: " + str(clone / ".git") + "\n", newline="\n")
    git(clone, "config", "core.worktree", str(source))
    assert Path(git(source, "rev-parse", "--absolute-git-dir")) == clone / ".git"
    assert materializations_unchanged(capture, roots) is False


def test_pinned_configured_selection_and_separately_owned_role(tmp_path):
    root = nested_modules(tmp_path)
    project = root / ".booley_project"
    project.mkdir()
    (project / "booley.toml").write_text("[submodules]\npaths = []\n", newline="\n")
    git(root, "add", "-f", ".booley_project")
    git(root, "commit", "-qm", "pinned empty selection")
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    pin = git(root, "rev-parse", "HEAD")
    proof = export_tree(root, pin, scratch / "selected", scratch=scratch)
    assert len(proof) == 1 and not (scratch / "selected/module").exists()
    (project / "booley.toml").write_text('[submodules]\npaths = ["module"]\n', newline="\n")
    git(root, "commit", "-qam", "pinned selected module")
    pin = git(root, "rev-parse", "HEAD")
    proof = export_tree(
        root, pin, scratch / "excluded", scratch=scratch, excluded_top_level=frozenset({"module"})
    )
    assert len(proof) == 1 and not (scratch / "excluded/module").exists()
    proof = export_tree(root, pin, scratch / "included", scratch=scratch)
    assert len(proof) == 3


@pytest.mark.parametrize("change", ["bytes", "mode"])
def test_eol_cleanliness_never_normalizes_real_edits_or_mode(tmp_path, change):
    import os

    from booley.goals.committed_export import tracked_matches_pin

    if change == "mode" and os.name == "nt":
        pytest.skip("POSIX executable mode")
    root = repository(tmp_path / "source")
    (root / ".gitattributes").write_bytes(b"rtl.v text eol=crlf\n")
    (root / "rtl.v").write_bytes(b"one\ntwo\n")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "pinned source")
    (root / "rtl.v").unlink()
    git(root, "checkout-index", "--all", "--force")
    pin = git(root, "rev-parse", "HEAD")
    index = (root / ".git/index").read_bytes()
    assert tracked_matches_pin(root, pin, root / "rtl.v")
    if change == "bytes":
        (root / "rtl.v").write_bytes(b"one\r\nEDIT\r\n")
    else:
        (root / "rtl.v").chmod(0o755)
    assert not tracked_matches_pin(root, pin, root / "rtl.v")
    assert (root / ".git/index").read_bytes() == index


def paired_selection_fixture(tmp_path, outside):
    from types import SimpleNamespace

    from booley.goals.input_view import capture_path_roots, topology_digest
    from tests.conftest import symlink_or_skip

    owner = repository(tmp_path / "project-owner")
    (owner / "booley.toml").write_text("[submodules]\npaths=[]\n", newline="\n")
    git(owner, "add", "-A")
    git(owner, "commit", "-qm", "original Project selection")
    leaf = repository(tmp_path / "leaf")
    (leaf / "rtl.v").write_text("module leaf; endmodule\n", newline="\n")
    git(leaf, "add", "-A")
    git(leaf, "commit", "-qm", "pinned leaf")
    root = repository(tmp_path / "root")
    if outside:
        project = tmp_path / "outside-project"
        git(owner, "worktree", "add", "-q", "-b", "paired", str(project))
        symlink_or_skip(root / ".booley_project", project, target_is_directory=True)
        git(root, "add", "-f", ".booley_project")
        (root / "booley.toml").write_text('[project]\ndir="../outside-project"\n', newline="\n")
        git(root, "add", "booley.toml")
    else:
        project = root / ".booley_project"
        git(
            root,
            "-c",
            "protocol.file.allow=always",
            "submodule",
            "add",
            "-q",
            "-f",
            str(owner),
            ".booley_project",
        )
    git(project, "config", "core.autocrlf", "false")
    git(root, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(leaf), "unused")
    git(root / "unused", "config", "core.autocrlf", "false")
    git(root, "commit", "-qam", "original RTL")
    base, project_base = git(root, "rev-parse", "HEAD"), git(project, "rev-parse", "HEAD")
    git(root, "submodule", "deinit", "-q", "-f", "unused")
    record = SimpleNamespace(
        input_paths=capture_path_roots(root, project),
        protected_paths=(),
        project_snapshot=None,
        base_sha=base,
        paired_project_base_sha=project_base,
        input_topology_digest="sha256:" + topology_digest(root, project, project),
    )

    return root, project, record


@pytest.mark.parametrize("outside", [False, True])
def test_committed_view_uses_original_and_final_paired_selection_without_live_fallback(
    tmp_path, outside
):
    from booley.goals.input_view import committed_view, select_inputs
    from booley.runtime.project_repositories import paired_project_repository

    root, project, record = paired_selection_fixture(tmp_path, outside)

    def selection():
        paired = paired_project_repository(root)
        assert paired is not None and paired.worktree.resolve() == project.resolve()
        return select_inputs(record, root)

    for baseline in (True, False):
        with committed_view(
            selection(), record, control_project=project, baseline=baseline
        ) as view:
            assert not (view.root / "unused").exists()
            assert view.project.is_relative_to(view.root.parent)
            assert "paths=[]" in (view.project / "booley.toml").read_text()
    (project / "booley.toml").write_text('[submodules]\npaths=["unused"]\n', newline="\n")
    git(project, "commit", "-qam", "final Project selection")
    if not outside:
        git(root, "add", ".booley_project")
        git(root, "commit", "-qm", "final paired pin")
    with committed_view(selection(), record, control_project=project, baseline=True) as view:
        assert not (view.root / "unused").exists()
        assert "paths=[]" in (view.project / "booley.toml").read_text()
    with (
        pytest.raises(LifecycleError, match="pinned local submodule unavailable"),
        committed_view(selection(), record, control_project=project),
    ):
        pytest.fail("selected missing submodule was accepted")
    git(root, "-c", "protocol.file.allow=always", "submodule", "update", "--init", "unused")
    index = Path(git(root, "rev-parse", "--git-path", "index"))
    index = index if index.is_absolute() else root / index
    before = index.read_bytes()
    with committed_view(selection(), record, control_project=project) as view:
        assert (view.root / "unused/rtl.v").read_bytes() == b"module leaf; endmodule\n"
        assert 'paths=["unused"]' in (view.project / "booley.toml").read_text()
    assert index.read_bytes() == before


@pytest.mark.parametrize(
    "policy", ['paths="unused"', 'paths=["../unused"]', 'paths=["C:/unused"]', "paths=["]
)
def test_pinned_selection_invalid_policy_never_falls_back_to_live_inputs(tmp_path, policy):
    root = repository(tmp_path / "source")
    project = root / ".booley_project"
    project.mkdir()
    (project / "booley.toml").write_text("[submodules]\n" + policy + "\n", newline="\n")
    (root / "rtl.v").write_text("module top; endmodule\n", newline="\n")
    git(root, "add", "-f", ".booley_project")
    git(root, "add", "rtl.v")
    git(root, "commit", "-qm", "invalid pinned selection")
    before = (root / ".git/index").read_bytes()
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    with pytest.raises(ValueError):
        export_tree(root, git(root, "rev-parse", "HEAD"), scratch / "rtl", scratch=scratch)
    assert (root / ".git/index").read_bytes() == before
    assert (root / "rtl.v").read_bytes() == b"module top; endmodule\n"


@pytest.mark.parametrize("move", [False, True])
def test_removed_or_moved_recursive_baseline_uses_original_cached_objects(tmp_path, move):
    root = nested_modules(tmp_path)
    original = git(root, "rev-parse", "HEAD")
    if move:
        git(root, "mv", "module", "moved-module")
    else:
        git(root, "rm", "-rf", "module")
    git(root, "commit", "-qam", "move or remove module")
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    before = (git(root, "rev-parse", "HEAD"), (root / ".git/index").read_bytes())
    proof = export_tree(root, original, scratch / "rtl", scratch=scratch, baseline=True)
    assert (scratch / "rtl/module/nested/rtl.v").read_bytes() == b"original committed design\n"
    assert len(proof) == 3 and all(row["baseline_only"] for row in proof[1:])
    assert before == (git(root, "rev-parse", "HEAD"), (root / ".git/index").read_bytes())
    final = scratch / "final"
    export_tree(root, git(root, "rev-parse", "HEAD"), final, scratch=scratch)
    assert not (final / "module").exists()
    if move:
        assert (final / "moved-module/nested/rtl.v").read_bytes() == b"original committed design\n"


@pytest.mark.parametrize(
    "attack", ["missing-objects", "ambiguous-name", "unsafe-name", "admin-link"]
)
def test_removed_baseline_rejects_untrusted_cache_selection(tmp_path, attack):
    root = nested_modules(tmp_path)
    admin = root / ".git/modules/module"
    if attack == "ambiguous-name":
        with (root / ".gitmodules").open("a") as stream:
            stream.write('[submodule "alias"]\npath = module\n')
        git(root, "commit", "-qam", "ambiguous original mapping")
    elif attack == "unsafe-name":
        (root / ".gitmodules").write_text('[submodule "../foreign"]\npath = module\n')
        git(root, "commit", "-qam", "unsafe original mapping")
    original = git(root, "rev-parse", "HEAD")
    git(root, "rm", "-rf", "module")
    git(root, "commit", "-qam", "remove module")
    if attack == "missing-objects":
        (admin / "objects").rename(tmp_path / "held-objects")
    elif attack == "admin-link":
        held = tmp_path / "held-admin"
        admin.rename(held)
        admin.symlink_to(held, target_is_directory=True)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    before = (
        git(root, "rev-parse", "HEAD"),
        (root / ".git/index").read_bytes(),
        git(root, "count-objects", "-v"),
    )
    with pytest.raises(LifecycleError, match=r"baseline|original"):
        export_tree(root, original, scratch / "rtl", scratch=scratch, baseline=True)
    assert before == (
        git(root, "rev-parse", "HEAD"),
        (root / ".git/index").read_bytes(),
        git(root, "count-objects", "-v"),
    )


def test_unconsumed_filtered_metadata_blocks_whole_participant_without_execution(tmp_path):
    root = repository(tmp_path / "source")
    (root / "rtl.v").write_bytes(b"module top; endmodule\n")
    (root / "manual.pdf").write_bytes(b"unconsumed document\n")
    (root / ".gitattributes").write_text("manual.pdf filter=external\n")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "whole participant policy")
    sentinel = tmp_path / "executed"
    git(root, "config", "filter.external.smudge", "touch " + str(sentinel))
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    with pytest.raises(LifecycleError, match=r"unsupported .*input"):
        export_tree(root, git(root, "rev-parse", "HEAD"), scratch / "rtl", scratch=scratch)
    assert not sentinel.exists()


def test_unset_builtin_git_configuration_preserves_projection_defaults(tmp_path, monkeypatch):
    from booley.goals.committed_bytes import byte_policy

    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "no-global-config"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    root = repository(tmp_path / "source")
    git(root, "config", "--unset", "core.autocrlf")
    assert byte_policy(root)["autocrlf"] == "false"
    assert byte_policy(root)["eol"] in {"lf", "crlf"}


@pytest.mark.parametrize("move", [False, True])
def test_linked_worktree_removed_nested_gitlink_uses_original_worktree_cache(tmp_path, move):
    main = nested_modules(tmp_path)
    root = tmp_path / "linked-worktree"
    git(main, "worktree", "add", "-q", "-b", "goal/baseline", str(root))
    git(root, "-c", "protocol.file.allow=always", "submodule", "update", "--init", "--recursive")
    for checkout in (root / "module", root / "module/nested"):
        git(checkout, "config", "core.autocrlf", "false")
    original = git(root, "rev-parse", "HEAD")
    if move:
        git(root, "mv", "module", "moved-module")
    else:
        git(root, "rm", "-rf", "module")
    git(root, "commit", "-qam", "move or remove original gitlink")
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    proof = export_tree(root, original, scratch / "rtl", scratch=scratch, baseline=True)
    assert (scratch / "rtl/module/nested/rtl.v").read_bytes() == b"original committed design\n"
    assert len(proof) == 3 and all(row["baseline_only"] for row in proof[1:])


def test_baseline_object_lookup_serializes_alternate_path_with_literal_lf(tmp_path, monkeypatch):
    import os

    from booley.goals.baseline_objects import baseline_repository
    from booley.runtime.pinned_history import raw_git

    root = nested_modules(tmp_path)
    original = git(root, "rev-parse", "HEAD")
    oid = git(root, "rev-parse", "HEAD:module")
    expected = raw_git(root / "module", "cat-file", "commit", oid)
    git(root, "rm", "-rf", "module")
    git(root, "commit", "-qam", "remove original gitlink")
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    write_text = Path.write_text

    def windows_default_newline(path, data, *args, **kwargs):
        if path.name == "alternates" and kwargs.get("newline") is None:
            kwargs["newline"] = "\r\n"
        return write_text(path, data, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", windows_default_newline)
    with baseline_repository(root, original, "module", oid, scratch, None) as (view, admin):
        assert (view / ".git/objects/info/alternates").read_bytes() == (
            os.fsencode(str((admin / "objects").resolve())) + b"\n"
        )
        assert raw_git(view, "cat-file", "commit", oid) == expected


def test_removed_baseline_refuses_external_alternate_objects(tmp_path):
    root = nested_modules(tmp_path)
    original = git(root, "rev-parse", "HEAD")
    git(root, "rm", "-rf", "module")
    git(root, "commit", "-qam", "remove original gitlink")
    alternate = root / ".git/modules/module/objects/info/alternates"
    alternate.write_text(str(tmp_path / "middle/.git/objects") + "\n")
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    with pytest.raises(LifecycleError, match="alternate object"):
        export_tree(root, original, scratch / "rtl", scratch=scratch, baseline=True)


def test_linked_baseline_never_borrows_main_checkout_cache(tmp_path):
    main = nested_modules(tmp_path)
    root = tmp_path / "linked-worktree"
    git(main, "worktree", "add", "-q", "-b", "goal/baseline", str(root))
    original = git(root, "rev-parse", "HEAD")
    git(root, "rm", "-rf", "module")
    git(root, "commit", "-qam", "remove uninitialized goal gitlink")
    assert (main / ".git/modules/module").is_dir()
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    with pytest.raises(LifecycleError, match="original baseline submodule objects unavailable"):
        export_tree(root, original, scratch / "rtl", scratch=scratch, baseline=True)


def test_baseline_refuses_substituted_current_submodule_administration(tmp_path):
    root = nested_modules(tmp_path)
    original = git(root, "rev-parse", "HEAD")
    (root / "module").rename(tmp_path / "held-original-checkout")
    replacement = repository(root / "module")
    (replacement / "rtl.v").write_bytes(b"substituted checkout\n")
    git(replacement, "add", "-A")
    git(replacement, "commit", "-qm", "foreign module")
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    before = (git(root, "rev-parse", "HEAD"), (root / ".git/index").read_bytes())
    with pytest.raises(LifecycleError, match="differs from original cached administration"):
        export_tree(root, original, scratch / "rtl", scratch=scratch, baseline=True)
    assert before == (git(root, "rev-parse", "HEAD"), (root / ".git/index").read_bytes())
