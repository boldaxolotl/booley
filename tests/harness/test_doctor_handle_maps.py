"""Doctor warns when a Project's display handle map nears its size cap (#1550)."""

import pytest

from booley.core.host_shell import quote_arg
from booley.harness import doctor
from booley.runtime import handles, runtime_context, session_runtime
from booley.runtime.sandbox_apply import Attachment

THRESHOLD = handles.MAX_MAP_BYTES * 3 // 4


@pytest.mark.parametrize(
    ("jobs", "excess"),
    [
        pytest.param(False, 0, id="session-at-threshold"),
        pytest.param(False, 1, id="session-above"),
        pytest.param(True, 0, id="job-at-threshold"),
        pytest.param(True, 1, id="job-above"),
    ],
)
@pytest.mark.usefixtures("fake_sandbox_attachment")
def test_handle_map_size_warns_only_above_three_quarters_of_cap(
    tmp_path, monkeypatch, jobs, excess
):
    store = handles.HandleStore(tmp_path, jobs=jobs)
    store.root.mkdir(parents=True)
    with store.path.open("wb") as stream:
        stream.truncate(THRESHOLD + excess)
    reporter = doctor._Reporter.create()
    monkeypatch.setattr(runtime_context, "inside_session_runtime", lambda: False)
    doctor._check_handle_map_size(tmp_path, tmp_path, reporter.warn_)
    findings = reporter.findings
    if not excess:
        assert findings == []
        return
    assert [f.check_id for f in findings] == ["handles.map-size"]
    assert findings[0].subject == ("job" if jobs else "session")
    assert "16 MiB" in findings[0].message
    assert findings[0].fix.startswith("restart the Sandbox so its MCP server prunes")


_VSCODE_RESTART = (
    "stop the Sandbox through Dev Containers, then reopen the folder in the container"
)
_UNKNOWN_HEADLESS = (
    "stop the Sandbox container with `docker stop`, then run `booley session up` on the host"
)
_PREFIX = "restart the Sandbox so its MCP server prunes expired handles at start: "


@pytest.mark.parametrize(
    "owner", ["vscode", "headless", "unknown", "inside", "inside-hand-written"]
)
def test_handle_map_fix_names_the_restart_for_each_owner(
    tmp_path, monkeypatch, fake_sandbox_attachment, owner, request
):
    if owner == "inside-hand-written":
        request.getfixturevalue("hand_written_sandbox")
    store = handles.HandleStore(tmp_path)
    store.root.mkdir(parents=True)
    with store.path.open("wb") as stream:
        stream.truncate(THRESHOLD + 1)
    attachment = {"vscode": Attachment.VSCODE, "headless": Attachment.HEADLESS}.get(owner)
    monkeypatch.setattr(
        runtime_context, "inside_session_runtime", lambda: owner.startswith("inside")
    )
    fake_sandbox_attachment(attachment)
    name = quote_arg(session_runtime.session_container_name(tmp_path))
    reporter = doctor._Reporter.create()

    doctor._check_handle_map_size(tmp_path, tmp_path, reporter.warn_)

    (finding,) = reporter.findings
    both = f"in VS Code, {_VSCODE_RESTART}; headless, {_UNKNOWN_HEADLESS}"
    expected = {
        "vscode": _VSCODE_RESTART,
        "headless": f"run `docker stop {name}`, then `booley session up` on the host",
        "unknown": both,
        "inside": both,
        "inside-hand-written": "stop this Sandbox, then start it again",
    }[owner]
    assert finding.fix == _PREFIX + expected
    assert "session down" not in finding.fix
