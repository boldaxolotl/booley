"""Real Git projections and Finish under Project Initialization's local policy."""

import json
import os
from pathlib import Path

import pytest

from booley.goals.committed_bytes import attributes, byte_policy, projected_blobs
from booley.goals.committed_export import export_tree, materializations_unchanged
from booley.goals.finish import finish_goal
from booley.goals.input_identity import root_bindings
from booley.goals.lifecycle import LifecycleError
from booley.runtime.git_attributes_policy import GITATTRIBUTES_RULE
from tests.goals.conftest import enter_goals, git
from tests.goals.test_committed_export import raw_blob, repository
from tests.goals.test_finish import environment, request
from tests.goals.test_finish_inputs import paired_complete
from tests.goals.test_status import publish

pytestmark = [pytest.mark.timeout(120), pytest.mark.usefixtures("isolated_git_attributes")]


def managed_attributes(root):
    common = Path(git(root, "rev-parse", "--path-format=absolute", "--git-common-dir"))
    path = common / "info/attributes"
    path.write_bytes(GITATTRIBUTES_RULE.encode() + b"\n")
    return path


@pytest.mark.parametrize("managed", [False, True])
def test_info_policy_projection_matches_real_checkout(tmp_path, managed):
    root = repository(tmp_path / "source")
    git(root, "config", "core.eol", "crlf")
    for name, content in {"crlf.txt": b"one\r\ntwo\r\n", "lf.txt": b"one\ntwo\n"}.items():
        git(
            root,
            "update-index",
            "--add",
            "--cacheinfo",
            f"100644,{raw_blob(root, content)},{name}",
        )
    git(root, "commit", "-qm", "raw line endings")
    if managed:
        managed_attributes(root)
    git(root, "checkout-index", "--all", "--force")
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    proof = export_tree(root, git(root, "rev-parse", "HEAD"), scratch / "view", scratch=scratch)
    assert attributes(
        root, git(root, "rev-parse", "HEAD"), [b"lf.txt"], policy=proof[0]["policy"]
    )[b"lf.txt"]["eol"] == ("lf" if managed else "unspecified")
    for name in ("crlf.txt", "lf.txt"):
        assert (scratch / "view" / name).read_bytes() == (root / name).read_bytes()
    assert proof[0]["policy"]["info_attributes"] == (GITATTRIBUTES_RULE if managed else "")
    roots = root_bindings({"rtl": root, "project": root})
    capture = {"committed_materializations": proof, "path_roots": roots}
    assert materializations_unchanged(capture, roots)
    if managed:
        (root / ".git/info/attributes").unlink()
        assert not materializations_unchanged(capture, roots)


@pytest.mark.parametrize("source", ["extra-info", "user-file", "default-user", "system-config"])
def test_other_ambient_policy_is_refused_even_when_managed_rule_masks_it(
    tmp_path, monkeypatch, source
):
    root = repository(tmp_path / "source")
    (root / "rtl.v").write_bytes(b"design\n")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "base")
    info = managed_attributes(root)
    if source == "extra-info":
        info.write_bytes(info.read_bytes() + b"nonexistent text\n")
    else:
        user = tmp_path / "git/attributes"
        user.parent.mkdir()
        user.write_bytes(b"* text eol=crlf\n")
        if source == "user-file":
            git(root, "config", "core.attributesFile", str(user))
        elif source == "default-user":
            monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        else:
            system = tmp_path / "system.config"
            git(root, "config", "--file", str(system), "core.attributesFile", str(user))
            monkeypatch.setenv("GIT_CONFIG_SYSTEM", str(system))
            monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "0")
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    with pytest.raises(LifecycleError, match="unsupported ambient input attributes"):
        export_tree(root, git(root, "rev-parse", "HEAD"), scratch / "view", scratch=scratch)


@pytest.mark.parametrize("paired", [False, True])
def test_finish_accepts_and_records_managed_policy_for_each_participant(layout, tmp_path, paired):
    if paired:
        layout, project = paired_complete(layout, tmp_path)
        managed_attributes(project)
    else:
        layout.record = enter_goals(layout, [{"family": "lint", "target": "top"}])
        publish(layout)
    managed_attributes(layout.worktree)
    call = request(layout)
    result = finish_goal(call, environment(layout))
    assert result["status"] == "finished"
    proof = json.loads(Path(result["package"]).read_bytes())["input_proof"]
    assert all(
        row["policy"]["info_attributes"] == GITATTRIBUTES_RULE
        for row in proof["committed_materializations"]
    )
    assert finish_goal(call, environment(layout)) == result


@pytest.mark.parametrize("content", [b"", b" " + GITATTRIBUTES_RULE.encode() + b" \r\n"])
def test_empty_info_or_recognized_rule_whitespace_is_safe(tmp_path, content):
    root = repository(tmp_path / "source")
    (root / "rtl.v").write_bytes(b"design\n")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "base")
    managed_attributes(root).write_bytes(content)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    proof = export_tree(root, git(root, "rev-parse", "HEAD"), scratch / "view", scratch=scratch)
    assert bytes.fromhex(proof[0]["policy"]["info_attributes_hex"]) == content


@pytest.mark.parametrize("change", ["remove", "extra"])
def test_finish_recovery_revalidates_local_policy(layout, change):
    from tests.goals.test_finish import Crash

    layout.record = enter_goals(layout, [{"family": "lint", "target": "top"}])
    publish(layout)
    info = managed_attributes(layout.worktree)
    call = request(layout)

    def stop(name):
        if name == "finishing":
            raise Crash()

    with pytest.raises(Crash):
        finish_goal(call, environment(layout, stop))
    if change == "remove":
        info.unlink()
        result = finish_goal(call, environment(layout))
        assert result["status"] == "revalidation_required"
    else:
        info.write_bytes(info.read_bytes() + b"nonexistent text\n")
        with pytest.raises(LifecycleError, match="unsupported ambient input attributes"):
            finish_goal(call, environment(layout))


@pytest.mark.parametrize("source", ["user-directory", "invalid-system-switch"])
def test_unsafe_attribute_inputs_refuse_with_actionable_error(tmp_path, monkeypatch, source):
    root = repository(tmp_path / "source")
    (root / "rtl.v").write_bytes(b"design\n")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "base")
    managed_attributes(root)
    if source == "user-directory":
        (tmp_path / "git/attributes").mkdir(parents=True)
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    else:
        monkeypatch.setenv("GIT_ATTR_NOSYSTEM", "invalid")
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    with pytest.raises(LifecycleError, match="managed info/attributes"):
        export_tree(root, git(root, "rev-parse", "HEAD"), scratch / "view", scratch=scratch)


def test_numeric_system_disable_switch_keeps_managed_projection_hermetic(tmp_path, monkeypatch):
    root = repository(tmp_path / "source")
    (root / "rtl.v").write_bytes(b"design\n")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "base")
    managed_attributes(root)
    monkeypatch.setenv("GIT_ATTR_NOSYSTEM", "2")
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    export_tree(root, git(root, "rev-parse", "HEAD"), scratch / "view", scratch=scratch)
    assert (scratch / "view/rtl.v").read_bytes() == b"design\n"


def test_policy_drift_between_attribute_query_and_projection_refuses(tmp_path):
    from booley.runtime.pinned_history import tree_rows

    root = repository(tmp_path / "source")
    (root / "rtl.v").write_bytes(b"design\n")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "base")
    pin = git(root, "rev-parse", "HEAD")
    policy = byte_policy(root)
    attrs = attributes(root, pin, [b"rtl.v"], policy=policy)
    managed_attributes(root)
    with pytest.raises(LifecycleError, match="unsupported ambient input attributes"):
        projected_blobs(root, pin, tree_rows(root, pin), attrs, policy)


@pytest.mark.parametrize("selection", ["", os.devnull])
def test_disabled_user_attributes_accept_managed_policy(tmp_path, selection):
    root = repository(tmp_path / "source")
    (root / "rtl.v").write_bytes(b"design\n")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "base")
    managed_attributes(root)
    git(root, "config", "core.attributesFile", selection)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    export_tree(root, git(root, "rev-parse", "HEAD"), scratch / "view", scratch=scratch)
    assert (scratch / "view/rtl.v").read_bytes() == b"design\n"
