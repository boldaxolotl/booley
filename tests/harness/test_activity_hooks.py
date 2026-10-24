"""The Sandbox Image's managed activity hooks and the Doctor probe that checks them."""

from __future__ import annotations

import json
import shutil
import tomllib
from pathlib import Path

from booley.harness import activity_hooks, doctor
from booley.harness.web_isolation import CLAUDE_POLICY, CODEX_POLICY, policy_error
from booley.runtime.session_activity import END_EVENT, EVENT_STATES

DATA = Path(__file__).parents[2] / "src/booley/data/docker"


def image_root(tmp_path):
    for source, target in (
        ("claude-managed-settings.json", CLAUDE_POLICY),
        ("codex-requirements.toml", CODEX_POLICY),
    ):
        (tmp_path / target).parent.mkdir(parents=True)
        shutil.copyfile(DATA / source, tmp_path / target)
    script = tmp_path / activity_hooks.HOOK_SCRIPT
    script.parent.mkdir(parents=True)
    shutil.copyfile(DATA / script.name, script)
    return tmp_path


def test_shipped_policies_install_a_hook_for_every_translated_event(tmp_path):
    root = image_root(tmp_path)
    assert activity_hooks.hooks_error(root) is None
    # The same files still carry the web-isolation policy.
    assert policy_error(root) is None
    claude = json.loads((root / CLAUDE_POLICY).read_text(encoding="utf-8"))["hooks"]
    codex = tomllib.loads((root / CODEX_POLICY).read_text(encoding="utf-8"))["hooks"]
    every = {*EVENT_STATES, END_EVENT}
    # Each client is given exactly the events it emits.
    assert set(claude) == every - {"Interrupt"}
    assert set(codex) == every - {"PostToolUseFailure", "Notification"}
    for hooks, client in ((claude, "claude"), (codex, "codex")):
        for groups in hooks.values():
            (handler,) = groups[0]["hooks"]
            assert (
                handler["command"]
                == f"python3 -I -S /usr/local/libexec/booley/session_activity_hook.py {client}"
            )
            # The client waits for the hook; 3 s is also the most Codex allows SessionEnd.
            assert handler["timeout"] == 3
    # Only the notifications that mean "waiting for the user" run the hook.
    matchers = {event: groups[0].get("matcher") for event, groups in claude.items()}
    assert matchers.pop("Notification") == "idle_prompt|permission_prompt|elicitation_dialog"
    assert set(matchers.values()) == {None}
    assert all("matcher" not in groups[0] for groups in codex.values())


def test_probe_names_the_client_and_events_that_are_not_covered(tmp_path):
    root = image_root(tmp_path)
    path = root / CLAUDE_POLICY
    claude = json.loads(path.read_text(encoding="utf-8"))
    del claude["hooks"]["Stop"]
    claude["hooks"]["PreToolUse"][0]["hooks"][0]["command"] = "other"
    path.write_text(json.dumps(claude), encoding="utf-8")
    error = activity_hooks.hooks_error(root)
    assert "Claude Code activity hooks" in error
    assert error.endswith("do not cover: PreToolUse, Stop")
    for malformed in ({"hooks": []}, {"hooks": {"Stop": "x"}}, {"hooks": {"Stop": [7]}}, []):
        path.write_text(json.dumps(malformed), encoding="utf-8")
        assert "Claude Code activity hooks" in activity_hooks.hooks_error(root)
    path.write_text("{", encoding="utf-8")
    assert "unreadable" in activity_hooks.hooks_error(root)
    shutil.copyfile(DATA / "claude-managed-settings.json", path)
    (root / CODEX_POLICY).write_text("allowed_web_search_modes = []\n", encoding="utf-8")
    assert "Codex activity hooks" in activity_hooks.hooks_error(root)
    (root / CODEX_POLICY).unlink()
    assert "Codex activity hooks unreadable" in activity_hooks.hooks_error(root)
    (root / activity_hooks.HOOK_SCRIPT).unlink()
    assert "activity hook script missing" in activity_hooks.hooks_error(root)


def test_base_image_ships_the_hook_script_the_policies_run():
    repo = DATA.parents[3]
    assert (
        activity_hooks.HOOK_COMMAND
        == "python3 -I -S /usr/local/libexec/booley/session_activity_hook.py"
    )
    source = f"src/booley/data/docker/{activity_hooks.HOOK_SCRIPT.name}"
    dockerfile = (DATA / "Dockerfile.base").read_text(encoding="utf-8")
    assert f"COPY {source} /{activity_hooks.HOOK_SCRIPT.as_posix()}" in dockerfile
    # The whitelist build context and the base compatibility contract both name it.
    assert f"!{source}" in (repo / ".dockerignore").read_text(encoding="utf-8")
    assert source in (DATA / "stable-base-inputs.txt").read_text(encoding="utf-8").splitlines()
    workflow = (repo / ".github/workflows/docker-base-publish.yml").read_text(encoding="utf-8")
    assert f"- {source}" in workflow
    contract = (repo / ".github/contracts/session-runtime.toml").read_text(encoding="utf-8")
    assert f'"/{activity_hooks.HOOK_SCRIPT.as_posix()}"' in contract


def test_doctor_warns_rather_than_fails_when_hooks_are_missing(monkeypatch):
    passed, warned = [], []
    monkeypatch.setattr(activity_hooks, "hooks_error", lambda: None)
    doctor._check_current_runtime_activity_hooks(passed.append, warned.append)
    assert passed == ["Dashboard session-activity hooks installed"]
    monkeypatch.setattr(activity_hooks, "hooks_error", lambda: "Codex activity hooks missing")
    doctor._check_current_runtime_activity_hooks(passed.append, warned.append)
    assert len(passed) == 1
    assert "activity will show as unknown: Codex activity hooks missing" in warned[0]
    assert "rebuild the sandbox image" in warned[0]
    assert warned[0].check_id == "sandbox.activity-hooks-unavailable"
