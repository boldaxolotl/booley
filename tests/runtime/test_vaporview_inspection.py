"""The Viewer Bridge and VaporView diagnosis, exercised through its owner.

The owner takes a ``ViewerBridgeRequest`` and returns a ``DiagnosticReport``.
Its seams are the process boundary (``subprocess.run``) and the Sandbox
session (``runtime_context`` and ``session_runtime``); Doctor is not involved.
"""

from __future__ import annotations

import functools
import json
import os
import pathlib
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from tests.harness import test_session_runtime as resolution_fixtures
from tests.subprocess_budget import run_bounded_child

from booley.audit.diagnostic_results import Severity
from booley.runtime import runtime_context, session_runtime, vaporview

project_sandbox_resolution = resolution_fixtures.project_sandbox_resolution
workspace = resolution_fixtures.workspace

_CONTAINER = "vsc-proj-abc-uid"
_WHERE = f" in '{_CONTAINER}'"
_VERIFIED = vaporview.VERIFIED_VERSION

# Literal texts the user sees, copied from the Doctor lines this owner replaced.
_BRIDGE = "Viewer Bridge answers in the attached VS Code window (`bwave gui`)"
_VERSION = f"VaporView version is the verified {_VERIFIED}"
_RELOAD_FIX = (
    "run 'Developer: Reload Window' in the attached VS Code window so the Booley "
    "extension starts its Viewer Bridge; if Doctor also reports devcontainer drift, "
    "refresh the Sandbox so its spec installs the Booley Sandbox Integration extension"
)
_UNKNOWN_FIX = (
    "could not determine whether VaporView is installed; verify it in the attached "
    "remote window, then install it if absent or run 'Developer: Reload Window' if present"
)
_VSIX = f"/usr/local/libexec/booley/vaporview-{_VERIFIED}.vsix"
_MISSING_FIX = (
    "VaporView extension is not installed. Install the build bundled in the Sandbox "
    "Image from the attached remote window's terminal (no network needed):\n"
    f"  code --install-extension {_VSIX}\n"
    "Booley's attach watcher patches a late install automatically; afterward run "
    '"Developer: Reload Window". Verify in the remote terminal with:\n'
    "  code --list-extensions --show-versions\n"
    f"which must list lramseyer.vaporview@{_VERIFIED}. If {_VSIX} is "
    "missing, the Sandbox Image predates it: refresh the Sandbox. A host CLI "
    "without the remote window selected may install locally instead of into "
    "the container."
)
_VERSION_FIX = (
    "in the attached remote window's terminal run "
    f"`code --install-extension {_VSIX}`, then "
    "'Developer: Reload Window'; for a lasting fix refresh the Sandbox so "
    "its generated spec installs the bundled verified build"
)

_P, _S, _F = Severity.PASS, Severity.SKIP, Severity.FAIL

_HEALTHY = {"vaporview": _VERIFIED, "error": None, "trusted": True}
_UNTRUSTED_ERROR = "VaporView (lramseyer.vaporview) is not installed in this VS Code window"
_UNTRUSTED = {"vaporview": None, "error": _UNTRUSTED_ERROR, "trusted": False}


@dataclass(frozen=True)
class _Reply:
    """What one probe process prints and returns, or the error launching it raises."""

    returncode: int = 0
    stdout: str = ""
    error: Exception | None = None


def _bridges(*entries: dict) -> _Reply:
    return _Reply(stdout=json.dumps({"bridges": list(entries)}))


def _versions(*versions: str) -> _Reply:
    return _Reply(stdout=json.dumps(list(versions)))


@functools.cache
def _sources() -> dict[str, str]:
    """Probe name -> its source text; the fake process dispatches on the source."""
    return {
        "host": vaporview._VSCODE_EXTENSION_HOST_PROBE_SOURCE,
        "versions": vaporview._VAPORVIEW_VERSIONS_PROBE_SOURCE,
        "bridges": vaporview._VIEWER_BRIDGE_PROBE_SOURCE,
        "state": vaporview._VAPORVIEW_STATE_PROBE_SOURCE,
    }


@dataclass
class _Sandbox:
    """A fake Sandbox: probe replies by name, and every process argv it was asked to run."""

    replies: dict[str, _Reply] = field(
        default_factory=lambda: {
            "host": _Reply(0),
            "versions": _versions(_VERIFIED),
            "bridges": _bridges(_HEALTHY),
            "state": _Reply(0),
        }
    )
    calls: list[list[str]] = field(default_factory=list)

    def run(self, argv, **kwargs):
        """Stand in for ``subprocess.run``; an argv no test planned for fails the test."""
        command = [str(part) for part in argv]
        self.calls.append(command)
        matches = [
            name
            for name, source in _sources().items()
            if command[-3:] == ["python3", "-c", source]
        ]
        if len(matches) != 1:
            pytest.fail(f"unplanned process: {command}")
        assert kwargs["timeout"] == 20
        reply = self.replies[matches[0]]
        if reply.error is not None:
            raise reply.error
        return subprocess.CompletedProcess(command, reply.returncode, reply.stdout, "")

    def probes(self) -> list[str]:
        """The probes run, in order, by name."""
        return [
            next(name for name, source in _sources().items() if call[-1] == source)
            for call in self.calls
        ]


@pytest.fixture
def sandbox(monkeypatch) -> _Sandbox:
    fake = _Sandbox()
    monkeypatch.setattr(vaporview.subprocess, "run", fake.run)
    return fake


def _host(monkeypatch, *, container: str | None = _CONTAINER, inside: bool = False) -> None:
    """Place the caller on the host (or inside the Sandbox) with a resolved container."""
    monkeypatch.setattr(runtime_context, "inside_session_runtime", lambda: inside)

    def resolve(_root):
        if isinstance(container, Exception):
            raise container
        return container

    monkeypatch.setattr(session_runtime, "vscode_session_container", resolve)


def _inspect(tmp_path: Path, docker: str | None = "docker") -> list[tuple[Severity, str, str]]:
    report = vaporview.inspect_viewer_bridge(vaporview.ViewerBridgeRequest(tmp_path, docker))
    assert all(f.check_id is None for f in report.findings)
    return [(f.severity, f.message, f.fix) for f in report.findings]


def _dark(state: _Reply) -> dict:
    return {"bridges": _bridges(), "state": state}


def _fail_bridge(count: str, reasons: str, fix: str, where: str = _WHERE):
    message = f"{_BRIDGE}: {count} window(s){where} report no usable VaporView ({reasons})"
    return [(_P, _VERSION, ""), (_F, message, fix)]


def _waits(count: int, where: str = _WHERE) -> tuple[Severity, str, str]:
    message = (
        f"{_BRIDGE} - VaporView waits for workspace trust in {count} "
        f"window(s){where}; trust the folder, then run `booley doctor`"
    )
    return (_S, message, "")


_PASS_BOTH = [(_P, _VERSION, ""), (_P, _BRIDGE, "")]
_MISSING = _Reply(3)
_UNKNOWN = _Reply(4)
_BROKEN = {"vaporview": None, "error": "VaporView reports no version", "trusted": True}

# (id, host?, docker, container, replies overriding the healthy defaults, expected findings)
_OUTCOMES = [
    pytest.param(
        True,
        None,
        _CONTAINER,
        {"host": _Reply(3)},
        [(_S, f"{_BRIDGE} - no VS Code extension host attached", "")],
        id="inside-sandbox-no-extension-host",
    ),
    pytest.param(
        True,
        None,
        _CONTAINER,
        {},
        [(_P, _VERSION, ""), (_P, _BRIDGE, "")],
        id="inside-sandbox-healthy",
    ),
    pytest.param(
        False,
        None,
        _CONTAINER,
        {},
        [(_S, f"{_BRIDGE} - container runtime unavailable", "")],
        id="host-without-docker",
    ),
    pytest.param(
        False,
        "docker",
        session_runtime.SessionError("inventory is ambiguous"),
        {},
        [
            (
                _F,
                f"{_BRIDGE} - inventory is ambiguous",
                "resolve the Sandbox inventory before retrying",
            )
        ],
        id="inventory-error",
    ),
    pytest.param(
        False,
        "docker",
        None,
        {},
        [(_S, f"{_BRIDGE} - no VS Code devcontainer running for this project", "")],
        id="no-vscode-devcontainer",
    ),
    pytest.param(
        False,
        "docker",
        _CONTAINER,
        {"host": _Reply(3)},
        [(_S, f"{_BRIDGE} - no VS Code extension host attached in '{_CONTAINER}'", "")],
        id="no-extension-host-in-container",
    ),
    pytest.param(
        False,
        "docker",
        _CONTAINER,
        {"host": _Reply(1)},
        _PASS_BOTH,
        id="unknown-extension-host-proceeds",
    ),
    pytest.param(False, "docker", _CONTAINER, {}, _PASS_BOTH, id="verified-and-healthy"),
    pytest.param(
        False,
        "docker",
        _CONTAINER,
        {"versions": _Reply(4)},
        [(_S, f"{_VERSION} - version probe could not run{_WHERE}", ""), (_P, _BRIDGE, "")],
        id="version-probe-fails",
    ),
    pytest.param(
        False,
        "docker",
        _CONTAINER,
        {"versions": _versions()},
        [(_S, f"{_VERSION} - VaporView not installed{_WHERE}", ""), (_P, _BRIDGE, "")],
        id="vaporview-not-installed",
    ),
    pytest.param(
        False,
        "docker",
        _CONTAINER,
        {"versions": _versions("9.9.9")},
        [
            (
                _F,
                f"VaporView 9.9.9 is installed{_WHERE}; Booley verifies only {_VERIFIED}",
                _VERSION_FIX,
            ),
            (_P, _BRIDGE, ""),
        ],
        id="other-version",
    ),
    pytest.param(
        False,
        "docker",
        _CONTAINER,
        {"versions": _versions(_VERIFIED, "9.9.9")},
        [
            (
                _F,
                f"VaporView 9.9.9 is installed{_WHERE}; Booley verifies only {_VERIFIED}",
                _VERSION_FIX,
            ),
            (_P, _BRIDGE, ""),
        ],
        id="verified-beside-other-version",
    ),
    pytest.param(
        False,
        "docker",
        _CONTAINER,
        {"bridges": _Reply(1)},
        [(_P, _VERSION, ""), (_S, f"{_BRIDGE} - probe could not run{_WHERE}", "")],
        id="bridge-probe-fails",
    ),
    pytest.param(
        False,
        "docker",
        _CONTAINER,
        _dark(_Reply(0)),
        [(_P, _VERSION, ""), (_F, f"{_BRIDGE}: no window answered{_WHERE}", _RELOAD_FIX)],
        id="dark-bridge-reload",
    ),
    pytest.param(
        False,
        "docker",
        _CONTAINER,
        _dark(_MISSING),
        [(_P, _VERSION, ""), (_F, f"{_BRIDGE}: no window answered{_WHERE}", _MISSING_FIX)],
        id="dark-bridge-extension-missing",
    ),
    pytest.param(
        False,
        "docker",
        _CONTAINER,
        _dark(_UNKNOWN),
        [(_P, _VERSION, ""), (_F, f"{_BRIDGE}: no window answered{_WHERE}", _UNKNOWN_FIX)],
        id="dark-bridge-extension-unknown",
    ),
    pytest.param(
        False,
        "docker",
        _CONTAINER,
        _dark(_Reply(error=FileNotFoundError("docker"))),
        [(_P, _VERSION, ""), (_F, f"{_BRIDGE}: no window answered{_WHERE}", _UNKNOWN_FIX)],
        id="dark-bridge-registry-probe-cannot-run",
    ),
    pytest.param(
        False,
        "docker",
        _CONTAINER,
        {
            "bridges": _bridges(
                _HEALTHY,
                {"vaporview": None, "error": "VaporView is not installed", "trusted": True},
            ),
            "state": _MISSING,
        },
        _fail_bridge("1 of 2", "VaporView is not installed", _MISSING_FIX),
        id="one-of-two-windows-broken",
    ),
    pytest.param(
        False,
        "docker",
        _CONTAINER,
        {"bridges": _bridges(_HEALTHY, _HEALTHY)},
        [(_P, _VERSION, ""), (_P, f"{_BRIDGE} (2 windows)", "")],
        id="several-healthy-windows",
    ),
    pytest.param(
        False,
        "docker",
        _CONTAINER,
        {"bridges": _bridges(_UNTRUSTED)},
        [(_P, _VERSION, ""), _waits(1)],
        id="all-windows-waiting-for-trust",
    ),
    pytest.param(
        False,
        "docker",
        _CONTAINER,
        {"bridges": _bridges(_UNTRUSTED, _UNTRUSTED)},
        [(_P, _VERSION, ""), _waits(2)],
        id="several-windows-waiting-for-trust",
    ),
    pytest.param(
        False,
        "docker",
        _CONTAINER,
        {"bridges": _bridges(_HEALTHY, _UNTRUSTED)},
        [(_P, _VERSION, ""), (_P, f"{_BRIDGE} (2 windows; 1 waiting for workspace trust)", "")],
        id="waiting-window-beside-healthy-one",
    ),
    pytest.param(
        False,
        "docker",
        _CONTAINER,
        {"bridges": _bridges(_UNTRUSTED), "state": _MISSING},
        _fail_bridge("1 of 1", _UNTRUSTED_ERROR, _MISSING_FIX),
        id="untrusted-window-with-missing-vaporview",
    ),
    pytest.param(
        False,
        "docker",
        _CONTAINER,
        {"bridges": _bridges(_UNTRUSTED), "state": _UNKNOWN},
        [(_P, _VERSION, ""), _waits(1)],
        id="untrusted-window-with-unknown-registry",
    ),
    pytest.param(
        False,
        "docker",
        _CONTAINER,
        {"bridges": _bridges({**_UNTRUSTED, "trusted": None})},
        _fail_bridge("1 of 1", _UNTRUSTED_ERROR, _RELOAD_FIX),
        id="broken-window-without-trust-field",
    ),
    pytest.param(
        False,
        "docker",
        _CONTAINER,
        {"bridges": _bridges(_UNTRUSTED, _BROKEN)},
        _fail_bridge("2 of 2", f"{_UNTRUSTED_ERROR}; VaporView reports no version", _RELOAD_FIX),
        id="untrusted-and-trusted-broken-windows",
    ),
]


@pytest.mark.parametrize(("inside", "docker", "container", "replies", "expected"), _OUTCOMES)
def test_viewer_bridge_outcomes(
    tmp_path, monkeypatch, sandbox, inside, docker, container, replies, expected
):
    """Every outcome: severity, message and fix, in order, with no check ID."""
    _host(monkeypatch, container=container, inside=inside)
    sandbox.replies.update(replies)
    if inside:
        expected = [(sev, msg.replace(_WHERE, ""), fix) for sev, msg, fix in expected]

    assert _inspect(tmp_path, docker) == expected


def test_host_probes_run_in_the_vscode_container(tmp_path, monkeypatch, sandbox):
    """Host probes are `docker exec` into the container; the version line comes first."""
    _host(monkeypatch)

    _inspect(tmp_path)

    prefix = ["docker", "exec", _CONTAINER, "python3", "-c"]
    assert sandbox.calls == [
        [*prefix, _sources()[name]] for name in ("host", "versions", "bridges")
    ]


def test_inside_sandbox_probes_run_locally(tmp_path, monkeypatch, sandbox):
    """Inside the Sandbox there is no Docker and no container: probes run in place."""
    _host(monkeypatch, inside=True)

    _inspect(tmp_path, None)

    assert sandbox.calls == [
        ["python3", "-c", _sources()[n]] for n in ("host", "versions", "bridges")
    ]


def test_registry_is_probed_only_when_the_answer_needs_it(tmp_path, monkeypatch, sandbox):
    _host(monkeypatch)
    _inspect(tmp_path)
    assert "state" not in sandbox.probes()

    sandbox.calls.clear()
    sandbox.replies["bridges"] = _bridges(_UNTRUSTED)
    _inspect(tmp_path)
    assert sandbox.probes().count("state") == 1
    # The registry probe runs in the VS Code container too, not on the host.
    state_call = sandbox.calls[sandbox.probes().index("state")]
    assert state_call == ["docker", "exec", _CONTAINER, "python3", "-c", _sources()["state"]]


@pytest.mark.parametrize(
    ("reply", "fix"),
    [
        (_Reply(0), _RELOAD_FIX),
        (_Reply(3), _MISSING_FIX),
        (_Reply(4), _UNKNOWN_FIX),
        (_Reply(1), _UNKNOWN_FIX),
        (_Reply(error=subprocess.TimeoutExpired("docker", 20)), _UNKNOWN_FIX),
    ],
    ids=["installed", "missing", "unreadable", "interpreter-error", "timeout"],
)
def test_registry_probe_result_chooses_the_recovery(tmp_path, monkeypatch, sandbox, reply, fix):
    _host(monkeypatch)
    sandbox.replies.update(_dark(reply))

    assert _inspect(tmp_path)[-1][2] == fix


_COULD_NOT_RUN = (_S, f"{_VERSION} - version probe could not run{_WHERE}", "")


@pytest.mark.parametrize(
    ("reply", "expected"),
    [
        (
            _versions("1.5.4"),
            (
                _F,
                f"VaporView 1.5.4 is installed{_WHERE}; Booley verifies only {_VERIFIED}",
                _VERSION_FIX,
            ),
        ),
        (_versions(), (_S, f"{_VERSION} - VaporView not installed{_WHERE}", "")),
        (_Reply(4), _COULD_NOT_RUN),
        (_Reply(1, "Traceback (ImportError)"), _COULD_NOT_RUN),
        (_Reply(0, "not json"), _COULD_NOT_RUN),
        (_Reply(0, "[1]"), _COULD_NOT_RUN),
        (_Reply(error=FileNotFoundError("docker")), _COULD_NOT_RUN),
        (_Reply(error=subprocess.TimeoutExpired("docker", 20)), _COULD_NOT_RUN),
    ],
    ids=["old", "empty", "exit-4", "old-booley", "garbage", "not-strings", "no-docker", "timeout"],
)
def test_version_probe_output_is_parsed_not_trusted(
    tmp_path, monkeypatch, sandbox, reply, expected
):
    _host(monkeypatch)
    sandbox.replies["versions"] = reply

    assert _inspect(tmp_path)[0] == expected


@pytest.mark.parametrize(
    "reply",
    [
        _Reply(1, "Traceback (ImportError)"),
        _Reply(0, "not json"),
        _Reply(0, "[]"),
        _Reply(0, '{"bridges": [{"vaporview": 5, "error": null}]}'),
        _Reply(0, '{"bridges": [{"vaporview": null, "error": null, "trusted": "no"}]}'),
        _Reply(error=FileNotFoundError("docker")),
        _Reply(error=subprocess.TimeoutExpired("docker", 20)),
    ],
    ids=[
        "old-booley",
        "garbage",
        "not-object",
        "bad-version",
        "bad-trusted",
        "no-docker",
        "timeout",
    ],
)
def test_bridge_probe_output_is_parsed_not_trusted(tmp_path, monkeypatch, sandbox, reply):
    _host(monkeypatch)
    sandbox.replies["bridges"] = reply

    assert _inspect(tmp_path)[-1] == (_S, f"{_BRIDGE} - probe could not run{_WHERE}", "")


def test_bridge_without_trust_field_reads_as_unknown_trust(tmp_path, monkeypatch, sandbox):
    """An older extension never reports trust; a healthy entry still passes."""
    _host(monkeypatch)
    sandbox.replies["bridges"] = _Reply(
        stdout=json.dumps({"bridges": [{"vaporview": _VERIFIED, "error": None}]})
    )

    assert _inspect(tmp_path) == _PASS_BOTH


@pytest.mark.parametrize(("returncode", "proceeds"), [(0, True), (3, False), (1, True), (2, True)])
def test_extension_host_probe_exit_codes(tmp_path, monkeypatch, sandbox, returncode, proceeds):
    """Only exit 3 means "no window attached"; a broken probe never reads as that."""
    _host(monkeypatch)
    sandbox.replies["host"] = _Reply(returncode)

    assert (len(_inspect(tmp_path)) > 1) is proceeds


def _run_extension_host_probe(monkeypatch, tmp_path, cmdlines):
    """Execute the real probe source against fake /proc cmdline files; return its exit code."""
    fakes = []
    for index, cmdline in enumerate(cmdlines):
        path = tmp_path / str(index) / "cmdline"
        if cmdline is not None:  # None = a process that exits between glob and read
            path.parent.mkdir()
            path.write_bytes(cmdline)
        fakes.append(path)
    real_glob = pathlib.Path.glob

    def glob(self, pattern, *args, **kwargs):
        if self == pathlib.Path("/proc"):
            return iter(fakes)
        return real_glob(self, pattern, *args, **kwargs)

    monkeypatch.setattr(pathlib.Path, "glob", glob)
    with pytest.raises(SystemExit) as exited:
        exec(compile(vaporview._VSCODE_EXTENSION_HOST_PROBE_SOURCE, "<probe>", "exec"), {})
    return exited.value.code


_EXTENSION_HOST_CMDLINE = b"/vscode/node\x00/vscode/out/bootstrap-fork\x00--type=extensionHost\x00"


@pytest.mark.parametrize(
    ("cmdlines", "expected"),
    [
        ([b"/usr/bin/sleep\x00100\x00", None, _EXTENSION_HOST_CMDLINE], 0),
        ([b"/vscode/out/bootstrap-fork\x00--type=ptyHost\x00", None], 3),
        ([], 3),
    ],
)
def test_extension_host_probe_source_runs_and_reports_attachment(
    monkeypatch, tmp_path, cmdlines, expected
):
    # The probe runs as `python3 -c` in the Sandbox, so execute the real source:
    # a syntax error used to exit 1, which read as "no window attached".
    assert _run_extension_host_probe(monkeypatch, tmp_path, cmdlines) == expected


_PROBE_SOURCE_NAMES = sorted(n for n in dir(vaporview) if n.endswith("_PROBE_SOURCE"))


@pytest.mark.parametrize("name", _PROBE_SOURCE_NAMES)
def test_every_python_probe_source_compiles(name):
    compile(getattr(vaporview, name), name, "exec")


def test_viewer_bridge_probe_source_runs_against_the_real_client(tmp_path):
    """The probe text must import and print what the owner parses."""
    env = {
        **os.environ,
        "HOME": str(tmp_path),
        "PYTHONPATH": str(Path(vaporview.__file__).parents[2]),
    }
    result = run_bounded_child(
        [sys.executable, "-c", vaporview._VIEWER_BRIDGE_PROBE_SOURCE], env=env
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"bridges": []}


@pytest.mark.parametrize("inventory", ["editor", "headless", "absent", "unknown"])
def test_viewer_bridge_project_sandbox_resolution(
    workspace, project_sandbox_resolution, sandbox, monkeypatch, inventory
):
    fixture = project_sandbox_resolution
    if inventory == "headless":
        fixture.state["Config"]["Labels"].pop("devcontainer.local_folder")
        fixture.state["Name"] = "/" + session_runtime.session_container_name(workspace)
    elif inventory == "absent":
        fixture.documents.clear()
    elif inventory == "unknown":
        fixture.state["Config"]["Labels"].pop("booley.project-id")
    monkeypatch.setattr(runtime_context, "inside_session_runtime", lambda: False)
    sandbox.replies["host"] = _Reply(3)

    findings = _inspect(workspace)

    if inventory == "unknown":
        assert [f[0] for f in findings] == [_F]
        assert "identity disagrees" in findings[0][1]
    else:
        # Ownership alone must never claim an attached extension host.
        assert [f[0] for f in findings] == [_S]
        expected = (
            "no VS Code extension host attached in"
            if inventory == "editor"
            else ("no VS Code devcontainer running")
        )
        assert expected in findings[0][1]
