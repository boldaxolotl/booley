"""Real-Git compatibility controls for managed Finish attribute policy."""

import hashlib
import json
import os
from pathlib import Path

import pytest

from booley.goals.committed_bytes import byte_policy, shadow_repository
from booley.goals.committed_export import export_tree, materializations_unchanged
from booley.goals.finish import finish_goal
from booley.goals.input_identity import root_bindings
from booley.runtime.git_attributes_policy import GITATTRIBUTES_RULE
from booley.runtime.pinned_history import raw_git
from tests.goals.conftest import enter_goals, git
from tests.goals.test_committed_export import repository
from tests.goals.test_finish import environment, request
from tests.goals.test_managed_attributes import managed_attributes
from tests.goals.test_status import publish

pytestmark = [pytest.mark.timeout(120), pytest.mark.usefixtures("isolated_git_attributes")]


def external_policy(root, tmp_path, monkeypatch, source):
    path = tmp_path / "git/attributes"
    path.parent.mkdir()
    path.write_bytes(b"nonexistent text eol=crlf\n")
    if source == "user":
        git(root, "config", "core.attributesFile", str(path))
    elif source == "default-user":
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    else:
        config = tmp_path / "system.config"
        git(root, "config", "--file", str(config), "core.attributesFile", str(path))
        monkeypatch.setenv("GIT_CONFIG_SYSTEM", str(config))
        monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "0")


@pytest.mark.parametrize("source", ["user", "default-user", "system"])
def test_no_managed_policy_preserves_ineffective_external_attributes(
    layout, tmp_path, monkeypatch, source
):
    layout.record = enter_goals(layout, [{"family": "lint", "target": "top"}])
    publish(layout)
    external_policy(layout.worktree, tmp_path, monkeypatch, source)
    assert finish_goal(request(layout), environment(layout))["status"] == "finished"


def test_old_materialization_row_without_local_policy_remains_valid(tmp_path):
    root = repository(tmp_path / "source")
    (root / "rtl.v").write_bytes(b"design\n")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "base")
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    rows = export_tree(root, git(root, "rev-parse", "HEAD"), scratch / "view", scratch=scratch)
    del rows[0]["policy"]["managed_rule"]
    del rows[0]["policy"]["info_attributes_sha256"]
    roots = root_bindings({"rtl": root, "project": root})
    proof = {"committed_materializations": rows, "path_roots": roots}
    assert materializations_unchanged(proof, roots)
    managed_attributes(root)
    assert not materializations_unchanged(proof, roots)


@pytest.mark.parametrize(
    "comment", [b"existing policy", b"raw \xff comment"], ids=["ascii", "non-utf8-comment"]
)
def test_init_preserves_export_only_content_and_finish_replays_it(layout, comment):
    from booley.harness.setup.line_endings import LineEndingMode, reconcile_project_line_endings

    info = layout.main / ".git/info/attributes"
    extra = (
        b"# "
        + comment
        + b"\n\nrtl.v export-ignore -export-subst\n*.txt -export-ignore export-subst\n"
    )
    info.write_bytes(extra)
    git(layout.worktree, "config", "--unset", "core.autocrlf")
    report = reconcile_project_line_endings(
        layout.worktree, layout.worktree / ".booley_project", mode=LineEndingMode.REPAIR
    )
    assert report.status.value == "safe"
    content = GITATTRIBUTES_RULE.encode() + b"\n" + extra
    assert info.read_bytes() == content
    layout.record = enter_goals(layout, [{"family": "lint", "target": "top"}])
    publish(layout)
    result = finish_goal(request(layout), environment(layout))
    assert result["status"] == "finished"
    rows = json.loads(Path(result["package"]).read_bytes())["input_proof"][
        "committed_materializations"
    ]
    policy = rows[0]["policy"]
    assert policy["managed_rule"] == GITATTRIBUTES_RULE
    assert policy["info_attributes_sha256"] == hashlib.sha256(content).hexdigest()
    assert comment not in Path(result["package"]).read_bytes()
    with shadow_repository(layout.worktree, byte_policy(layout.worktree).info_attributes) as (
        shadow,
        env,
    ):
        raw_git(shadow, "read-tree", git(layout.worktree, "rev-parse", "HEAD"), env=env)
        values = raw_git(
            shadow,
            "-c",
            "core.attributesFile=" + os.devnull,
            "check-attr",
            "--cached",
            "export-ignore",
            "--",
            "rtl.v",
            env=env,
        )
    assert values == b"rtl.v: export-ignore: set\n"
