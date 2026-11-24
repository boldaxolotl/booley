"""Doctor warns when a Project's display handle map nears its size cap (#1550)."""

import pytest

from booley.core.host_shell import quote_arg
from booley.runtime import handles, session_runtime
from booley.runtime.sandbox_apply import Attachment
from tests.harness.doctor_run_support import enter_sandbox, findings, run_doctor
from tests.harness.test_doctor import _patch_environment, _write_project

THRESHOLD = handles.MAX_MAP_BYTES * 3 // 4


def _host_with_map(tmp_path, monkeypatch, *, size, jobs=False):
    """Set up the host fixtures for a Project whose handle map is *size* bytes."""
    booley_dir = _write_project(tmp_path)
    _patch_environment(monkeypatch, tmp_path, booley_dir)
    store = handles.HandleStore(booley_dir, jobs=jobs)
    store.root.mkdir(parents=True)
    with store.path.open("wb") as stream:
        stream.truncate(size)


@pytest.mark.parametrize(
    ("jobs", "excess"),
    [(False, 0), (False, 1), (True, 0), (True, 1)],
    ids=["session-at-threshold", "session-above", "job-at-threshold", "job-above"],
)
@pytest.mark.usefixtures("fake_sandbox_attachment")
def test_handle_map_size_warns_only_above_three_quarters_of_cap(
    tmp_path, monkeypatch, jobs, excess
):
    _host_with_map(tmp_path, monkeypatch, size=THRESHOLD + excess, jobs=jobs)

    rows = findings(run_doctor(tmp_path), "handles.map-size")

    if not excess:
        assert rows == []
        return
    assert [row.subject for row in rows] == ["job" if jobs else "session"]
    assert "16 MiB" in rows[0].message
    assert rows[0].fix.startswith("restart the Sandbox so its MCP server prunes")


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
    _host_with_map(tmp_path, monkeypatch, size=THRESHOLD + 1)
    if owner.startswith("inside"):
        enter_sandbox(monkeypatch)
    attachment = {"vscode": Attachment.VSCODE, "headless": Attachment.HEADLESS}.get(owner)
    fake_sandbox_attachment(attachment)
    name = quote_arg(session_runtime.session_container_name(tmp_path))

    (finding,) = findings(run_doctor(tmp_path), "handles.map-size")

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
