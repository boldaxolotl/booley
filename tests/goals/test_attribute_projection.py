"""Real Git attribute projection isolation and persisted-proof compatibility."""

import hashlib
import json
import os
from pathlib import Path

import pytest

from booley.goals.committed_bytes import attributes, byte_policy, shadow_repository
from booley.goals.committed_export import export_tree, materializations_unchanged
from booley.goals.finish import finish_goal
from booley.goals.input_identity import root_bindings
from booley.goals.lifecycle import LifecycleError
from tests.conftest import symlink_or_skip
from tests.goals.conftest import enter_goals, git
from tests.goals.test_committed_export import raw_blob, repository
from tests.goals.test_finish import environment, publication_layout, request
from tests.goals.test_managed_attributes import managed_attributes
from tests.goals.test_status import publish

pytestmark = [pytest.mark.timeout(120), pytest.mark.usefixtures("isolated_git_attributes")]
OLD_NAMES = ("text", "eol", "filter", "working-tree-encoding")


def source(tmp_path, rule="", name="rtl.txt", content=b"$Id$\n", attribute_path=".gitattributes"):
    root = repository(tmp_path / "source")
    if rule:
        path = root / attribute_path
        path.parent.mkdir(parents=True, exist_ok=True)
        pattern = name if attribute_path == ".gitattributes" else Path(name).name
        path.write_text(pattern + " " + rule + "\n")
        git(root, "add", attribute_path)
    git(root, "update-index", "--add", "--cacheinfo", f"100644,{raw_blob(root, content)},{name}")
    git(root, "commit", "-qm", "pinned bytes")
    git(root, "checkout-index", "--all", "--force")
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    return root, git(root, "rev-parse", "HEAD"), scratch


def export(root, pin, scratch, name="view"):
    return export_tree(root, pin, scratch / name, scratch=scratch)


def capture(root, rows):
    roots = root_bindings({"rtl": root, "project": root})
    return {"committed_materializations": rows, "path_roots": roots}, roots


def test_attribute_source_oid_cannot_change_checkout_bytes(tmp_path, monkeypatch):
    root, pin, scratch = source(tmp_path)
    expected = export(root, pin, scratch, "clean")
    (root / ".gitattributes").write_bytes(b"*.txt text eol=crlf\n")
    git(root, "add", ".gitattributes")
    git(root, "commit", "-qm", "poison attribute tree")
    poison = git(root, "rev-parse", "HEAD^{tree}")
    git(root, "reset", "--hard", pin)
    monkeypatch.setenv("GIT_ATTR_SOURCE", poison)
    assert export(root, pin, scratch) == expected
    assert (scratch / "view/rtl.txt").read_bytes() == b"$Id$\n"


@pytest.mark.parametrize("rule", ["ident", "crlf", "text eol=crlf"])
@pytest.mark.parametrize("location", ["info", "user", "worktree", "ignored-nested"])
def test_effective_ambient_checkout_attributes_refuse(tmp_path, rule, location):
    name = "nested/rtl.txt" if location == "ignored-nested" else "rtl.txt"
    root, pin, scratch = source(tmp_path, name=name)
    if location == "info":
        path = root / ".git/info/attributes"
    elif location == "user":
        path = tmp_path / "user.attributes"
        git(root, "config", "core.attributesFile", str(path))
    else:
        path = root / (
            "nested/.gitattributes" if location == "ignored-nested" else ".gitattributes"
        )
        if location == "ignored-nested":
            (root / ".git/info/exclude").write_bytes(b".gitattributes\n")
    path.write_text("*.txt " + rule + "\n")
    if rule == "crlf":
        git(root, "config", "core.eol", "crlf")
    oracle = git(root, "check-attr", "ident", "crlf", "eol", "--", name)
    assert any(value in oracle for value in (": set", ": crlf"))
    (root / name).unlink()
    git(root, "checkout-index", "--all", "--force")
    expected = (
        f"$Id: {git(root, 'rev-parse', 'HEAD:' + name)} $\n".encode()
        if rule == "ident"
        else b"$Id$\r\n"
    )
    assert (root / name).read_bytes() == expected
    with pytest.raises(LifecycleError, match="unsupported ambient input attributes;"):
        export(root, pin, scratch)
    path.unlink()
    assert export(root, pin, scratch, "restored")


@pytest.mark.parametrize("rule", ["ident", "crlf"])
def test_committed_builtins_match_checkout_oracle_and_record_all_names(tmp_path, rule):
    root, pin, scratch = source(tmp_path, rule=rule)
    rows = export(root, pin, scratch)
    assert (scratch / "view/rtl.txt").read_bytes() == (root / "rtl.txt").read_bytes()
    if rule == "ident":
        oid = git(root, "rev-parse", "HEAD:rtl.txt")
        assert (scratch / "view/rtl.txt").read_bytes() == f"$Id: {oid} $\n".encode()
    else:
        git(root, "config", "core.eol", "crlf")
        (root / "rtl.txt").unlink()
        git(root, "checkout-index", "--all", "--force")
        export(root, pin, scratch, "crlf")
        assert (scratch / "crlf/rtl.txt").read_bytes() == b"$Id$\r\n"
    assert (
        rows[0]["policy"]["checkout_attributes"]
        == "text,eol,filter,working-tree-encoding,ident,crlf"
    )
    assert attributes(root, pin, [b"rtl.txt"], policy=byte_policy(root))[b"rtl.txt"][rule] == "set"


def test_shadow_always_disables_system_attributes(tmp_path, monkeypatch):
    root, _, _ = source(tmp_path)
    monkeypatch.delenv("GIT_ATTR_NOSYSTEM", raising=False)
    with shadow_repository(root, b"") as (_, env):
        assert env["GIT_ATTR_NOSYSTEM"] == "1"


def test_legacy_literal_four_attribute_digest_revalidates_with_six_name_safety(tmp_path):
    root, pin, scratch = source(tmp_path)
    rows = export(root, pin, scratch)
    rows[0]["policy"].pop("checkout_attributes", None)
    # Literal from the original four-name canonical encoding, captured before the fix.
    rows[0]["attributes_digest"] = (
        "9d3d678bea8779e406018feea3bb42138648765646a48090d38bb6f4d8c663c2"
    )
    proof, roots = capture(root, rows)
    assert materializations_unchanged(proof, roots)
    (root / ".git/info/attributes").write_bytes(b"*.txt ident\n")
    with pytest.raises(LifecycleError, match="attributes changed during finish"):
        materializations_unchanged(proof, roots)


def test_unknown_marker_fails_closed(tmp_path):
    root, pin, scratch = source(tmp_path)
    rows = export(root, pin, scratch)
    rows[0]["policy"]["checkout_attributes"] = "future"
    proof, roots = capture(root, rows)
    assert not materializations_unchanged(proof, roots)


def test_worktree_attribute_drift_revalidates_and_masked_policy_is_safe(tmp_path):
    root, pin, scratch = source(tmp_path)
    managed_attributes(root)
    rows = export(root, pin, scratch)
    path = root / ".gitattributes"
    path.write_bytes(b"*.txt text eol=crlf\n")
    assert export(root, pin, scratch, "masked") == rows
    path.write_bytes(b"*.txt ident\n")
    proof, roots = capture(root, rows)
    with pytest.raises(LifecycleError, match="attributes changed during finish"):
        materializations_unchanged(proof, roots)


@pytest.mark.parametrize("change", ["changed", "added", "removed"])
@pytest.mark.parametrize("nested", [False, True])
def test_baseline_uses_its_own_tracked_attributes_at_final_head(tmp_path, change, nested):
    name = "nested/rtl.txt" if nested else "rtl.txt"
    attribute_path = "nested/.gitattributes" if nested else ".gitattributes"
    root, base, scratch = source(
        tmp_path,
        rule="ident" if change != "added" else "",
        name=name,
        attribute_path=attribute_path,
    )
    expected = (root / name).read_bytes()
    path = root / attribute_path
    if change == "removed":
        git(root, "rm", attribute_path)
    else:
        path.write_bytes(b"*.txt text eol=crlf\n")
        git(root, "add", attribute_path)
    git(root, "commit", "-qm", "final attributes")
    final = git(root, "rev-parse", "HEAD")
    (root / name).unlink()
    git(root, "checkout-index", "--all", "--force")
    final_bytes = (root / name).read_bytes()
    export(root, base, scratch, "base")
    export(root, final, scratch, "final")
    assert (scratch / "base" / name).read_bytes() == expected
    assert (scratch / "final" / name).read_bytes() == final_bytes


def test_worktree_symlink_attributes_are_ignored(tmp_path):
    root, pin, scratch = source(tmp_path)
    target = tmp_path / "external.attributes"
    target.write_bytes(b"*.txt ident\n")
    symlink_or_skip(root / ".gitattributes", target)
    assert export(root, pin, scratch)


def test_committed_symlink_attributes_are_not_replayed_as_regular_files(tmp_path):
    root, pin, scratch = source(tmp_path)
    (root / "policy").write_bytes(b"*.txt ident\n")
    symlink_or_skip(root / ".gitattributes", "policy")
    git(root, "add", ".gitattributes", "policy")
    git(root, "commit", "-qm", "symlink attrs ignored")
    pin = git(root, "rev-parse", "HEAD")
    export(root, pin, scratch)
    assert (scratch / "view/rtl.txt").read_bytes() == b"$Id$\n"


def test_native_case_insensitive_attribute_lookup(tmp_path):
    root, pin, scratch = source(tmp_path)
    path = root / ".GITATTRIBUTES"
    path.write_bytes(b"*.txt ident\n")
    if not (root / ".gitattributes").exists():
        pytest.skip("native filesystem is case sensitive")
    with pytest.raises(LifecycleError, match="unsupported ambient input attributes;"):
        export(root, pin, scratch)


@pytest.mark.parametrize("encoding", ["UTF-16", "UTF-8"])
def test_custom_encoding_remains_refused(tmp_path, encoding):
    root, pin, scratch = source(tmp_path, rule="working-tree-encoding=" + encoding)
    with pytest.raises(LifecycleError, match="unsupported committed input transformation"):
        export(root, pin, scratch)


def test_finish_committed_ident(layout):
    (layout.worktree / "identifier.txt").write_bytes(b"$Id$\n")
    (layout.worktree / ".gitattributes").write_bytes(b"identifier.txt ident\n")
    git(layout.worktree, "add", "identifier.txt", ".gitattributes")
    git(layout.worktree, "commit", "-qm", "identifier")
    git(layout.worktree, "checkout-index", "--all", "--force")
    layout.record = enter_goals(layout, [{"family": "lint", "target": "top"}])
    publish(layout)
    assert finish_goal(request(layout), environment(layout))["status"] == "finished"


def test_finish_ignored_effective_attributes_refuse_then_restore(layout):
    layout.record = enter_goals(layout, [{"family": "lint", "target": "top"}])
    publish(layout)
    attrs = layout.worktree / ".gitattributes"
    with (layout.main / ".git/info/exclude").open("ab") as stream:
        stream.write(b".gitattributes\n")
    attrs.write_bytes(b"*.v text eol=crlf\n")
    with pytest.raises(LifecycleError, match="unsupported ambient input attributes;"):
        finish_goal(request(layout), environment(layout))
    attrs.unlink()
    assert finish_goal(request(layout), environment(layout))["status"] == "finished"


def test_nonstealth_finish_ignores_foreign_repository_selectors(layout, tmp_path, monkeypatch):
    layout = publication_layout(layout)
    managed = managed_attributes(layout.worktree).read_bytes()
    foreign = repository(tmp_path / "foreign")
    (foreign / "README").write_bytes(b"foreign\n")
    git(foreign, "add", "README")
    git(foreign, "commit", "-qm", "foreign")
    (foreign / ".git/info/attributes").write_bytes(b"*.v ident\n")
    before = {
        str(p.relative_to(foreign)): p.read_bytes() for p in foreign.rglob("*") if p.is_file()
    }
    selectors = {**hostile(foreign), "GIT_ATTR_SOURCE": git(foreign, "rev-parse", "HEAD^{tree}")}
    for key, value in selectors.items():
        monkeypatch.setenv(key, value)
    result = finish_goal(request(layout), environment(layout))
    assert result["status"] == "finished"
    proof = json.loads(Path(result["package"]).read_bytes())["input_proof"]
    assert (
        proof["committed_materializations"][0]["policy"]["info_attributes_sha256"]
        == hashlib.sha256(managed).hexdigest()
    )
    assert before == {
        str(p.relative_to(foreign)): p.read_bytes() for p in foreign.rglob("*") if p.is_file()
    }


def hostile(foreign):
    return {
        "GIT_DIR": str(foreign / ".git"),
        "GIT_COMMON_DIR": str(foreign / ".git"),
        "GIT_WORK_TREE": str(foreign),
        "GIT_INDEX_FILE": str(foreign / ".git/index"),
        "GIT_OBJECT_DIRECTORY": str(foreign / ".git/objects"),
        "GIT_ALTERNATE_OBJECT_DIRECTORIES": str(foreign / "missing-objects"),
        "GIT_PREFIX": "wrong/",
        "GIT_CEILING_DIRECTORIES": str(foreign.parent),
        "GIT_DISCOVERY_ACROSS_FILESYSTEM": "0",
        "GIT_ATTR_SOURCE": "0" * 40,
    }


@pytest.mark.parametrize(
    "selector",
    [
        "GIT_DIR",
        "GIT_COMMON_DIR",
        "GIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_PREFIX",
        "GIT_CEILING_DIRECTORIES",
        "GIT_DISCOVERY_ACROSS_FILESYSTEM",
    ],
)
def test_projection_ignores_inherited_selector(tmp_path, monkeypatch, selector):
    root, pin, scratch = source(tmp_path)
    foreign = repository(tmp_path / "foreign")
    (foreign / ".git/info/attributes").write_bytes(b"*.txt ident\n")
    monkeypatch.setenv(selector, hostile(foreign)[selector])
    assert export(root, pin, scratch)
    assert (scratch / "view/rtl.txt").read_bytes() == b"$Id$\n"


@pytest.mark.parametrize("rule", ["ident", "crlf"])
def test_worktree_config_user_attributes_refuse(tmp_path, rule):
    root, _, scratch = source(tmp_path)
    linked = tmp_path / "linked"
    git(root, "worktree", "add", "-qb", "linked", str(linked))
    git(root, "config", "extensions.worktreeConfig", "true")
    user = tmp_path / "worktree.attributes"
    user.write_text("*.txt " + rule + "\n")
    git(linked, "config", "--worktree", "core.attributesFile", str(user))
    pin = git(linked, "rev-parse", "HEAD")
    with pytest.raises(LifecycleError, match="unsupported ambient input attributes;"):
        export(linked, pin, scratch)


def test_legacy_previously_unrecorded_ident_policy_now_fails_closed(tmp_path):
    root, pin, scratch = source(tmp_path)
    rows = export(root, pin, scratch)
    rows[0]["policy"].pop("checkout_attributes")
    rows[0]["attributes_digest"] = (
        "9d3d678bea8779e406018feea3bb42138648765646a48090d38bb6f4d8c663c2"
    )
    (root / ".git/info/attributes").write_bytes(b"*.txt ident\n")
    proof, roots = capture(root, rows)
    with pytest.raises(LifecycleError, match="attributes changed during finish"):
        materializations_unchanged(proof, roots)


def test_removed_pinned_attribute_path_with_untracked_overlay_refuses(tmp_path):
    root, base, scratch = source(tmp_path, rule="ident")
    git(root, "rm", ".gitattributes")
    git(root, "commit", "-qm", "removed policy")
    (root / ".gitattributes").write_bytes(b"*.txt text eol=crlf\n")
    for pin in (base, git(root, "rev-parse", "HEAD")):
        with pytest.raises(LifecycleError, match="unsupported ambient input attributes;"):
            export(root, pin, scratch)


def test_nonregular_untracked_attribute_source_fails_closed(tmp_path):
    root, pin, scratch = source(tmp_path)
    (root / ".gitattributes").mkdir()
    with pytest.raises(LifecycleError, match="unsupported ambient input attributes;"):
        export(root, pin, scratch)


def test_comment_only_untracked_attributes_do_not_change_effective_policy(tmp_path):
    root, pin, scratch = source(tmp_path)
    (root / ".gitattributes").write_bytes(b"# neutral local policy\n")
    assert export(root, pin, scratch)


def test_init_and_pinned_history_use_shared_inherited_environment(tmp_path, monkeypatch):
    from booley.harness.setup.line_endings import _read_only_git_env
    from booley.runtime.git_environment import (
        REPOSITORY_SELECTION_VARIABLES,
        inherited_git_environment,
    )
    from booley.runtime.pinned_history import raw_git

    root, _, _ = source(tmp_path)
    for name in REPOSITORY_SELECTION_VARIABLES:
        monkeypatch.setenv(name, "poison")
    monkeypatch.setenv("BOOLEY_TEST_PRESERVED", "yes")
    assert inherited_git_environment()["BOOLEY_TEST_PRESERVED"] == "yes"
    assert not REPOSITORY_SELECTION_VARIABLES.intersection(_read_only_git_env())
    assert raw_git(root, "rev-parse", "--show-toplevel").strip() == os.fsencode(root)
