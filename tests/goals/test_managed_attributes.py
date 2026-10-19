"""Real Git checkout policy, canonical replay, and Finish recovery."""

import json
from pathlib import Path

import pytest

from booley.goals.committed_export import materializations_unchanged
from booley.goals.finish import finish_goal
from booley.goals.lifecycle import LifecycleError
from booley.harness.setup.line_endings import LineEndingMode, reconcile_project_line_endings
from booley.runtime.git_attributes_policy import GITATTRIBUTES_RULE
from tests.conftest import symlink_or_skip
from tests.goals.attribute_support import system_attributes as system_attributes  # noqa: PLC0414 — pytest fixture
from tests.goals.conftest import enter_goals, git
from tests.goals.test_finish import Crash, environment, request
from tests.goals.test_finish_inputs import paired_complete
from tests.goals.test_status import publish

pytestmark = [pytest.mark.timeout(120), pytest.mark.usefixtures("isolated_git_attributes")]


def managed_attributes(root):
    common = Path(git(root, "rev-parse", "--path-format=absolute", "--git-common-dir"))
    path = common / "info/attributes"
    path.write_bytes(GITATTRIBUTES_RULE.encode() + b"\n")
    return path


@pytest.fixture
def projection(tmp_path, request):
    from tests.goals.test_attribute_projection import capture, export, source

    root, pin, scratch = source(tmp_path, content=getattr(request, "param", b"$Id$\n"))

    def project():
        rows = export(root, pin, scratch)
        git(root, "checkout-index", "--all", "--force")
        assert (scratch / "view/rtl.txt").read_bytes() == (root / "rtl.txt").read_bytes()
        return capture(root, rows)

    return root, project


def external_policy(root, tmp_path, monkeypatch, source, rule):
    path = tmp_path / "git/attributes"
    path.parent.mkdir()
    path.write_text(rule + "\n")
    if source == "user":
        git(root, "config", "core.attributesFile", str(path))
    elif source == "default-user":
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    else:
        config = tmp_path / "system.config"
        git(root, "config", "--file", str(config), "core.attributesFile", str(path))
        monkeypatch.setenv("GIT_CONFIG_SYSTEM", str(config))
        monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "0")


@pytest.mark.parametrize("managed", [False, True])
@pytest.mark.parametrize(
    "projection", [b"one\r\ntwo\r\n", b"one\ntwo\n"], ids=["crlf", "lf"], indirect=True
)
def test_info_policy_projection_matches_real_checkout(projection, managed):
    root, project = projection
    git(root, "config", "core.eol", "crlf")
    if managed:
        managed_attributes(root)
    proof, roots = project()
    rows = proof["committed_materializations"]
    assert rows[0]["policy"]["managed_rule"] == (GITATTRIBUTES_RULE if managed else "")
    assert materializations_unchanged(proof, roots)


@pytest.mark.parametrize(
    "source,rule",
    [(source, "* text eol=crlf") for source in ("user", "default-user", "system-config")]
    + [("user", "nonexistent text eol=crlf"), ("user", "*.txt diff=markdown")],
    ids=["user-masked", "default-user-masked", "system-config-masked", "unmatched", "diff"],
)
def test_managed_policy_accepts_user_attributes(projection, tmp_path, monkeypatch, source, rule):
    root, project = projection
    managed_attributes(root)
    external_policy(root, tmp_path, monkeypatch, source, rule)
    project()


def test_managed_policy_accepts_masked_system_checkout_attributes(system_attributes, projection):
    root, project = projection
    managed_attributes(root)
    system_attributes.write_text("* text eol=crlf\n")
    project()


@pytest.mark.parametrize(
    "managed,rule",
    [
        (False, "text eol=crlf"),
        (False, "text eol=lf"),
        (False, "ident"),
        (False, "crlf"),
        (True, "ident"),
        (True, "crlf"),
    ],
)
def test_no_managed_policy_refuses_real_system_attributes(
    system_attributes, projection, managed, rule
):
    root, project = projection
    if managed:
        managed_attributes(root)
    system_attributes.write_text("* " + rule + "\n")
    with pytest.raises(LifecycleError, match="unsupported ambient input attributes;"):
        project()


@pytest.mark.parametrize("source", ["user", "default-user", "system-config"])
def test_no_managed_policy_preserves_ineffective_external_attributes(
    layout, tmp_path, monkeypatch, source
):
    layout.record = enter_goals(layout, [{"family": "LINT", "target": "top"}])
    publish(layout)
    external_policy(layout.worktree, tmp_path, monkeypatch, source, "nonexistent text eol=crlf")
    assert finish_goal(request(layout), environment(layout))["status"] == "finished"


@pytest.mark.parametrize("paired", [False, True])
def test_finish_accepts_and_records_managed_policy_for_each_participant(layout, tmp_path, paired):
    if paired:
        layout, project = paired_complete(layout, tmp_path)
        managed_attributes(project)
    else:
        layout.record = enter_goals(layout, [{"family": "LINT", "target": "top"}])
        publish(layout)
    managed_attributes(layout.worktree)
    call = request(layout)
    result = finish_goal(call, environment(layout))
    assert result["status"] == "finished"
    package = json.loads(Path(result["package"]).read_bytes())
    rows = package["input_proof"]["committed_materializations"]
    assert all(row["policy"]["managed_rule"] == GITATTRIBUTES_RULE for row in rows)
    assert finish_goal(call, environment(layout)) == result


@pytest.mark.parametrize(
    "comment", [b"existing policy", b"raw \xff comment"], ids=["ascii", "non-utf8-comment"]
)
def test_init_preserves_export_only_content_and_finish_replays_it(
    layout, tmp_path, monkeypatch, comment
):
    external_policy(layout.worktree, tmp_path, monkeypatch, "default-user", "# no checkout policy")
    info = managed_attributes(layout.worktree)
    exports = b"\n\nrtl.v export-ignore -export-subst\n*.txt -export-ignore export-subst\n"
    extra = b"# " + comment + exports
    info.write_bytes(extra)
    git(layout.worktree, "config", "--unset", "core.autocrlf")
    report = reconcile_project_line_endings(
        layout.worktree, layout.worktree / ".booley", mode=LineEndingMode.REPAIR
    )
    assert report.status.value == "safe"
    assert info.read_bytes() == GITATTRIBUTES_RULE.encode() + b"\n" + extra
    layout.record = enter_goals(layout, [{"family": "LINT", "target": "top"}])
    publish(layout)
    result = finish_goal(request(layout), environment(layout))
    assert result["status"] == "finished"
    assert comment not in Path(result["package"]).read_bytes()


@pytest.mark.parametrize("content", [b"", b" " + GITATTRIBUTES_RULE.encode() + b" \r\n"])
def test_empty_info_or_recognized_rule_whitespace_is_safe(projection, content):
    root, project = projection
    managed_attributes(root).write_bytes(content)
    project()


@pytest.mark.parametrize("change", ["remove", "extra", "add"])
def test_finish_recovery_revalidates_local_policy(layout, change):
    layout.record = enter_goals(layout, [{"family": "LINT", "target": "top"}])
    publish(layout)
    call = request(layout)
    info = managed_attributes(layout.worktree)
    if change == "add":
        info.unlink()

    def stop(name):
        if name == "finishing":
            raise Crash()

    with pytest.raises(Crash):
        finish_goal(call, environment(layout, stop))
    if change == "extra":
        info.write_bytes(info.read_bytes() + b"nonexistent text\n")
        with pytest.raises(LifecycleError, match="unsupported ambient input attributes"):
            finish_goal(call, environment(layout))
    else:
        if change == "remove":
            info.unlink()
        else:
            managed_attributes(layout.worktree)
        assert finish_goal(call, environment(layout))["status"] == "revalidation_required"


def test_comment_only_policy_edits_preserve_recovery(projection):
    root, project = projection
    info = managed_attributes(root)
    info.write_bytes(info.read_bytes() + b"# private local context\n")
    proof, roots = project()
    assert "private local context" not in json.dumps(proof)
    policy = proof["committed_materializations"][0]["policy"]
    assert set(policy) == {"autocrlf", "eol", "managed_rule", "checkout_attributes"}
    info.write_bytes(info.read_bytes().replace(b"private local context", b"another comment"))
    assert materializations_unchanged(proof, roots)


@pytest.mark.parametrize("kind", ["symlink", "directory", "parent-symlink"])
def test_unsafe_info_source_uses_legacy_effective_comparison(projection, tmp_path, kind):
    root, project = projection
    info = managed_attributes(root)
    if kind == "directory":
        info.unlink()
        info.mkdir()
        assert project()[0]["committed_materializations"][0]["policy"]["managed_rule"] == ""
        return
    target = tmp_path / "attributes"
    if kind == "symlink":
        info.rename(target)
        symlink_or_skip(info, target)
    else:
        info.parent.rename(target)
        symlink_or_skip(info.parent, target, target_is_directory=True)
    with pytest.raises(LifecycleError, match="unsupported ambient input attributes;"):
        project()


@pytest.mark.parametrize("source", ["info", "user"])
def test_extra_local_policy_or_user_ident_is_refused(projection, tmp_path, monkeypatch, source):
    root, project = projection
    info = managed_attributes(root)
    if source == "info":
        info.write_bytes(info.read_bytes() + b"nonexistent text\n")
    else:
        external_policy(root, tmp_path, monkeypatch, "user", "* ident")
    with pytest.raises(LifecycleError, match="unsupported ambient input attributes;"):
        project()
