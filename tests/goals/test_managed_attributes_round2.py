"""System policy symmetry, Init ownership, and content-free Finish proofs."""

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from booley.goals import committed_bytes
from booley.goals.committed_bytes import (
    attributes,
    byte_policy,
    projected_blobs,
    shadow_repository,
)
from booley.goals.committed_export import export_tree, materializations_unchanged
from booley.goals.finish import finish_goal
from booley.goals.input_identity import root_bindings
from booley.goals.lifecycle import LifecycleError
from booley.harness.setup.line_endings import LineEndingMode, reconcile_project_line_endings
from booley.runtime.git_attributes_policy import (
    GITATTRIBUTES_RULE,
    has_attribute_policy,
    has_managed_attributes,
    is_null_attributes_path,
    local_policy_owned,
)
from booley.runtime.pinned_history import raw_git, tree_rows
from tests.conftest import symlink_or_skip
from tests.goals.conftest import enter_goals, git
from tests.goals.test_committed_export import repository
from tests.goals.test_finish import environment, request
from tests.goals.test_managed_attributes import managed_attributes
from tests.goals.test_status import publish

pytestmark = [pytest.mark.timeout(120), pytest.mark.usefixtures("isolated_git_attributes")]


@pytest.fixture
def system_attributes(tmp_path, monkeypatch, isolated_git_attributes):
    """Relocate real Git so each test owns its actual system gitattributes file."""
    executable = os.environ.get("BOOLEY_TEST_RELOCATABLE_GIT") or shutil.which("git")
    assert executable is not None
    binary = tmp_path / ("git-prefix/bin/git.exe" if os.name == "nt" else "git-prefix/bin/git")
    binary.parent.mkdir(parents=True)
    shutil.copy2(executable, binary)
    monkeypatch.delenv("GIT_ATTR_NOSYSTEM", raising=False)
    result = subprocess.run(
        [str(binary), "var", "GIT_ATTR_SYSTEM"], capture_output=True, check=False, timeout=60
    )
    # CI Git has a fixed system prefix, so these cases skip there. Developers run them
    # with BOOLEY_TEST_RELOCATABLE_GIT (a RUNTIME_PREFIX build); setting
    # BOOLEY_TEST_REQUIRE_RELOCATABLE_GIT=1 turns the skip into a failure.
    unavailable = (
        pytest.fail if os.environ.get("BOOLEY_TEST_REQUIRE_RELOCATABLE_GIT") else pytest.skip
    )
    if result.returncode:
        unavailable("a runnable relocatable Git is required for the real system-file oracle")
    path = Path(os.fsdecode(result.stdout).strip())
    prefix = binary.parent.parent.resolve()
    if not path.is_absolute() or not path.resolve().is_relative_to(prefix):
        unavailable("Git has a fixed system prefix; use BOOLEY_TEST_RELOCATABLE_GIT")
    monkeypatch.setenv("PATH", str(binary.parent) + os.pathsep + os.environ["PATH"])
    monkeypatch.delenv("GIT_ATTR_NOSYSTEM", raising=False)
    path.parent.mkdir(parents=True, exist_ok=True)
    template = tmp_path / "git-template"
    (template / "info").mkdir(parents=True)
    (template / "info/exclude").write_bytes(b"")
    monkeypatch.setenv("GIT_TEMPLATE_DIR", str(template))
    return path


def captured(tmp_path):
    root = repository(tmp_path / "source")
    (root / "rtl.v").write_bytes(b"design\n")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "base")
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    return root, scratch


def export(root, scratch):
    return export_tree(root, git(root, "rev-parse", "HEAD"), scratch / "view", scratch=scratch)


@pytest.mark.parametrize("managed", [False, True])
def test_shadow_system_switch_preserves_live_environment(tmp_path, monkeypatch, managed):
    root, _ = captured(tmp_path)
    monkeypatch.delenv("GIT_ATTR_NOSYSTEM", raising=False)
    content = GITATTRIBUTES_RULE.encode() + b"\n" if managed else b""
    with shadow_repository(root, content) as (_, env):
        assert env.get("GIT_ATTR_NOSYSTEM") == ("1" if managed else None)


def test_no_managed_policy_projects_real_system_attributes_and_recovers_old_proof(
    tmp_path, system_attributes
):
    root, scratch = captured(tmp_path)
    system_attributes.write_bytes(b"* text eol=crlf\n")
    (root / "rtl.v").unlink()
    git(root, "checkout-index", "--all", "--force")
    rows = export(root, scratch)
    assert (scratch / "view/rtl.v").read_bytes() == (root / "rtl.v").read_bytes() == b"design\r\n"
    del rows[0]["policy"]["managed_rule"]
    del rows[0]["policy"]["info_attributes_sha256"]
    roots = root_bindings({"rtl": root, "project": root})
    assert materializations_unchanged(
        {"committed_materializations": rows, "path_roots": roots}, roots
    )


def test_no_managed_policy_finish_accepts_real_system_attributes(layout, system_attributes):
    layout.record = enter_goals(layout, [{"family": "lint", "target": "top"}])
    publish(layout)
    system_attributes.write_bytes(b"* text eol=lf\n")
    assert finish_goal(request(layout), environment(layout))["status"] == "finished"


@pytest.mark.parametrize("content", [None, b"", b"# only a comment\n\t\r\n", b"* text eol=crlf\n"])
def test_managed_policy_checks_real_enabled_system_file(tmp_path, system_attributes, content):
    root, scratch = captured(tmp_path)
    managed_attributes(root)
    if content is not None:
        system_attributes.write_bytes(content)
    if content and not content.startswith(b"#"):
        with pytest.raises(LifecycleError, match="unsupported ambient input attributes;"):
            export(root, scratch)
    else:
        assert export(root, scratch)[0]["policy"]["managed_rule"] == GITATTRIBUTES_RULE


def test_managed_policy_refuses_unknown_system_discovery(tmp_path, monkeypatch):
    root, _ = captured(tmp_path)
    managed_attributes(root)
    monkeypatch.delenv("GIT_ATTR_NOSYSTEM", raising=False)
    original = committed_bytes.native_attribute_path

    def discovery(root, variable, query):
        return (False, None) if variable == "GIT_ATTR_SYSTEM" else original(root, variable, query)

    monkeypatch.setattr(committed_bytes, "native_attribute_path", discovery)
    with pytest.raises(LifecycleError, match="unsupported ambient input attributes;"):
        byte_policy(root)


@pytest.mark.parametrize("content", [None, b"", b"# selected file has no policy\n"])
def test_managed_policy_accepts_configured_file_without_policy(tmp_path, content):
    root, scratch = captured(tmp_path)
    managed_attributes(root)
    selected = tmp_path / "selected.attributes"
    if content is not None:
        selected.write_bytes(content)
    git(root, "config", "core.attributesFile", str(selected))
    assert export(root, scratch)[0]["policy"]["managed_rule"] == GITATTRIBUTES_RULE


def test_managed_policy_checks_the_user_file_git_actually_reads(tmp_path):
    """A missing `:(optional)` selection makes Git fall back to XDG, not to global config."""
    root, scratch = captured(tmp_path)
    managed_attributes(root)
    clean = tmp_path / "clean.attributes"
    clean.write_bytes(b"")
    git(root, "config", "--global", "core.attributesFile", str(clean))
    git(root, "config", "core.attributesFile", f":(optional){tmp_path / 'missing.attributes'}")
    xdg = Path(os.environ["XDG_CONFIG_HOME"]) / "git/attributes"
    xdg.parent.mkdir(parents=True)
    xdg.write_bytes(b"* ident\n")
    reported = git(root, "var", "GIT_ATTR_GLOBAL")
    if Path(reported) != xdg:
        pytest.skip(f"this Git does not support :(optional) attribute paths ({reported})")
    with pytest.raises(LifecycleError):
        export(root, scratch)


def test_init_with_comment_only_xdg_attributes_can_finish(layout, tmp_path, monkeypatch):
    xdg = tmp_path / "xdg"
    selected = xdg / "git/attributes"
    selected.parent.mkdir(parents=True)
    selected.write_bytes(b"# no checkout policy\n")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    git(layout.worktree, "config", "--unset", "core.autocrlf")
    report = reconcile_project_line_endings(
        layout.worktree, layout.worktree / ".booley_project", mode=LineEndingMode.REPAIR
    )
    assert report.status.value == "safe"
    assert (
        layout.main / ".git/info/attributes"
    ).read_bytes() == GITATTRIBUTES_RULE.encode() + b"\n"
    layout.record = enter_goals(layout, [{"family": "lint", "target": "top"}])
    publish(layout)
    assert finish_goal(request(layout), environment(layout))["status"] == "finished"


def test_null_device_case_matches_the_platform(tmp_path):
    assert is_null_attributes_path(tmp_path, tmp_path / os.devnull)
    assert is_null_attributes_path(tmp_path, tmp_path / os.devnull.swapcase()) == (os.name == "nt")


@pytest.mark.parametrize("separator", [b"\x0b", b"\x0c"])
def test_non_git_whitespace_is_policy_not_a_comment_or_blank(tmp_path, separator):
    root, _ = captured(tmp_path)
    content = separator + b"#x eol=crlf\n"
    assert has_attribute_policy(separator + b"\n")
    assert local_policy_owned(separator + b"\n")
    assert has_attribute_policy(content)
    assert local_policy_owned(content)
    info = managed_attributes(root)
    info.write_bytes(info.read_bytes() + content)
    assert not has_managed_attributes(info.read_bytes(), replayable=True)
    name = separator + b"#x"
    assert (
        raw_git(root, "check-attr", "-z", "eol", "--", os.fsdecode(name))
        == name + b"\0eol\0crlf\0"
    )
    with pytest.raises(LifecycleError, match="unsupported ambient input attributes;"):
        byte_policy(root)


@pytest.mark.parametrize("name,value", [("autocrlf", "true"), ("eol", "crlf")])
def test_projection_configuration_drift_names_the_setting(tmp_path, name, value):
    root, _ = captured(tmp_path)
    git(root, "config", "core.eol", "lf")
    pin = git(root, "rev-parse", "HEAD")
    policy = byte_policy(root)
    attrs = attributes(root, pin, [b"rtl.v"], policy=policy)
    git(root, "config", "core." + name, value)
    with pytest.raises(LifecycleError, match="core." + name + " changed during finish"):
        projected_blobs(root, pin, tree_rows(root, pin), attrs, policy)


def test_recovery_ambient_drift_has_a_distinct_message(tmp_path):
    root, scratch = captured(tmp_path)
    rows = export(root, scratch)
    (root / ".git/info/attributes").write_bytes(b"* text eol=crlf\n")
    roots = root_bindings({"rtl": root, "project": root})
    with pytest.raises(LifecycleError, match="input attributes changed during finish"):
        materializations_unchanged(
            {"committed_materializations": rows, "path_roots": roots}, roots
        )


def test_proof_omits_local_comments_but_detects_their_drift(tmp_path):
    root, scratch = captured(tmp_path)
    info = managed_attributes(root)
    content = info.read_bytes() + b"# private local context\n"
    info.write_bytes(content)
    rows = export(root, scratch)
    assert rows[0]["policy"]["managed_rule"] == GITATTRIBUTES_RULE
    assert rows[0]["policy"]["info_attributes_sha256"] == hashlib.sha256(content).hexdigest()
    assert "private local context" not in json.dumps(rows)
    assert set(rows[0]["policy"]) == {"autocrlf", "eol", "managed_rule", "info_attributes_sha256"}
    info.write_bytes(content.replace(b"private local context", b"another local comment"))
    roots = root_bindings({"rtl": root, "project": root})
    assert not materializations_unchanged(
        {"committed_materializations": rows, "path_roots": roots}, roots
    )


@pytest.mark.parametrize("kind", ["symlink", "directory", "parent-symlink"])
def test_unsafe_info_source_uses_legacy_effective_comparison(tmp_path, kind):
    root, scratch = captured(tmp_path)
    info = root / ".git/info/attributes"
    if kind == "directory":
        info.mkdir()
        assert export(root, scratch)[0]["policy"]["managed_rule"] == ""
        return
    if kind == "symlink":
        target = tmp_path / "attributes"
        target.write_bytes(GITATTRIBUTES_RULE.encode() + b"\n")
        symlink_or_skip(info, target)
    else:
        managed_attributes(root)
        target = root / ".git/real-info"
        info.parent.rename(target)
        symlink_or_skip(info.parent, target, target_is_directory=True)
    with pytest.raises(LifecycleError, match="unsupported ambient input attributes;"):
        export(root, scratch)
