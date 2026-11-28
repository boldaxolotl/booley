"""The bundled-extension diagnosis, exercised through its owner.

The owner takes a ``BundledExtensionsRequest`` and returns a ``DiagnosticReport``.
Its seams are the process boundary (``subprocess.run``) and the Sandbox
session (``runtime_context`` and ``session_runtime``); Doctor is not involved.
"""

from __future__ import annotations

import contextlib
import dataclasses
import functools
import io
import json
import os
import pathlib
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from tests.docker_cli_guard import route_bounded_capture_through_subprocess_run
from tests.harness import test_session_runtime as resolution_fixtures
from tests.sandbox_venue_fake import place
from tests.subprocess_budget import run_bounded_child

from booley.audit.diagnostic_results import Severity
from booley.runtime import bundled_extensions, devcontainer, session_runtime
from booley.runtime.runtime_context import RuntimeLocation

project_sandbox_resolution = resolution_fixtures.project_sandbox_resolution
workspace = resolution_fixtures.workspace

_CONTAINER = "vsc-proj-abc-uid"
_WHERE = f" in '{_CONTAINER}'"

# The line label the user sees when the window cannot be inspected.
_DESCRIPTION = "Bundled VS Code extensions in the attached window"

_P, _S, _F = Severity.PASS, Severity.SKIP, Severity.FAIL
_ROOT = ".vscode-server"

_LIVE_PREVIEW_NEEDS = "Live Preview needs VS Code 1.109 or newer"


@pytest.fixture(autouse=True)
def _route_bounded_capture_through_subprocess_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """Let this file's ``subprocess.run`` fakes see the bounded Docker commands of the owner."""
    route_bounded_capture_through_subprocess_run(monkeypatch, bundled_extensions)


@dataclass(frozen=True)
class _Reply:
    """What one probe process prints and returns, or the error launching it raises."""

    returncode: int = 0
    stdout: str = ""
    error: Exception | None = None


def _attached(*roots: str, unidentified: int = 0) -> _Reply:
    """The extension-host probe's answer: hosts in these roots and ``unidentified`` elsewhere."""
    return _Reply(stdout=json.dumps({"roots": list(roots), "unidentified": unidentified}))


@functools.cache
def _sources(roots: tuple[str, ...] = (_ROOT,)) -> dict[str, str]:
    """Probe name -> its source text; the fake process dispatches on the source."""
    return {
        "host": bundled_extensions._VSCODE_EXTENSION_HOST_PROBE_SOURCE,
        "bundled": bundled_extensions._bundled_extensions_probe_source(
            devcontainer.BUNDLED_EXTENSIONS, roots
        ),
    }


def _bundled(
    *observations: tuple[list[str] | None, bool], roots: tuple[str, ...] = (_ROOT,)
) -> _Reply:
    """The bundled-extension probe's answer: (versions, VSIX present) per extension.

    "=" in a versions list stands for the version the Sandbox Image bundles;
    every attached root has the same versions.
    """
    answer = {}
    for extension, (versions, vsix) in zip(
        devcontainer.BUNDLED_EXTENSIONS, observations, strict=True
    ):
        shown = (
            None if versions is None else [extension.version if v == "=" else v for v in versions]
        )
        answer[extension.extension_id] = {
            "versions": dict.fromkeys(roots, shown),
            "vsix": vsix,
        }
    return _Reply(stdout=json.dumps(answer))


_GOOD = (["="], True)


@dataclass
class _Sandbox:
    """A fake Sandbox: probe replies by name, and every process argv it was asked to run."""

    replies: dict[str, _Reply] = field(default_factory=lambda: {"host": _attached(_ROOT)})
    calls: list[list[str]] = field(default_factory=list)
    roots: tuple[str, ...] = (_ROOT,)

    def attach(self, *roots: str, unidentified: int = 0) -> None:
        """Place extension hosts in these roots (and ``unidentified`` elsewhere)."""
        self.roots = roots
        self.replies["host"] = _attached(*roots, unidentified=unidentified)

    def run(self, argv, **kwargs):
        """Stand in for ``subprocess.run``; an argv no test planned for fails the test."""
        command = [str(part) for part in argv]
        self.calls.append(command)
        matches = [
            name
            for name, source in _sources(self.roots).items()
            if command[-3:] == ["python3", "-c", source]
        ]
        if len(matches) != 1:
            pytest.fail(f"unplanned process: {command}")
        assert kwargs["timeout"] == 20
        # Built on demand: it reads the bundled pins, so the fixture stays cheap.
        reply = self.replies.get(matches[0]) or _bundled(_GOOD, _GOOD, roots=self.roots)
        if reply.error is not None:
            raise reply.error
        return subprocess.CompletedProcess(command, reply.returncode, reply.stdout, "")

    def probes(self) -> list[str]:
        """The probes run, in order, by name."""
        return [
            next(name for name, source in _sources(self.roots).items() if call[-1] == source)
            for call in self.calls
        ]


@pytest.fixture
def sandbox(monkeypatch) -> _Sandbox:
    fake = _Sandbox()
    monkeypatch.setattr(bundled_extensions.subprocess, "run", fake.run)
    return fake


def _host(monkeypatch, *, container: str | None = _CONTAINER, inside: bool = False) -> None:
    """Place the caller on the host (or inside the Sandbox) with a resolved container."""
    place(monkeypatch, RuntimeLocation.ISSUED_SANDBOX if inside else RuntimeLocation.HOST)

    def resolve(_root):
        if isinstance(container, Exception):
            raise container
        return container

    monkeypatch.setattr(session_runtime, "vscode_session_container", resolve)


def _inspect_all(tmp_path: Path, docker: str | None = "docker") -> list[tuple[Severity, str, str]]:
    request = bundled_extensions.BundledExtensionsRequest(tmp_path, docker)
    report = bundled_extensions.inspect_bundled_extensions(request)
    assert all(f.check_id is None for f in report.findings)
    return [(f.severity, f.message, f.fix) for f in report.findings]


def _pass(extension):
    return (_P, f"{extension.name} extension is the bundled {extension.version}", "")


def _install_fix(extension):
    fix = (
        "in the attached remote window's terminal run "
        f"`code --install-extension {extension.vsix}`, then 'Developer: Reload Window'"
    )
    return f"{fix}; {_LIVE_PREVIEW_NEEDS}" if extension.name == "Live Preview" else fix


def _rebuild_fix(extension):
    fix = (
        f"{extension.vsix} is absent from the Sandbox Image: rebuild the Sandbox Image, "
        "then rebuild/reopen the Sandbox"
    )
    return f"{fix}; {_LIVE_PREVIEW_NEEDS}" if extension.name == "Live Preview" else fix


def _not_installed(extension):
    message = f"{extension.name} extension ({extension.extension_id}) is not installed{_WHERE}"
    return (_F, message, _install_fix(extension))


def _other(extension, shown, fix):
    message = (
        f"{extension.name} {shown} is installed{_WHERE}; "
        f"the Sandbox Image bundles {extension.version}"
    )
    return (_F, message, fix(extension))


def _vsix_absent(extension):
    message = f"{extension.name} {extension.version} is installed{_WHERE}, but its bundled VSIX is absent"
    return (_F, message, _rebuild_fix(extension))


def _unreadable(extension):
    message = (
        f"{extension.name} extension is the bundled {extension.version} - "
        f"extension registry could not be read{_WHERE}"
    )
    return (_S, message, "")


# (id, hdl observation, live observation, expected hdl line, expected live line)
_BUNDLED_OUTCOMES = [
    pytest.param(_GOOD, _GOOD, _pass, _pass, id="both-installed"),
    pytest.param(([], True), _GOOD, _not_installed, _pass, id="hdl-missing"),
    pytest.param(_GOOD, ([], True), _pass, _not_installed, id="live-preview-missing"),
    pytest.param(
        (["9.9.9"], True),
        _GOOD,
        lambda e: _other(e, "9.9.9", _install_fix),
        _pass,
        id="hdl-other-version",
    ),
    pytest.param(
        (["=", "9.9.9"], True),
        _GOOD,
        lambda e: _other(e, "9.9.9", _install_fix),
        _pass,
        id="hdl-bundled-beside-other",
    ),
    pytest.param(
        ([], False),
        _GOOD,
        lambda e: (*_not_installed(e)[:2], _rebuild_fix(e)),
        _pass,
        id="hdl-missing-vsix-absent",
    ),
    pytest.param(
        _GOOD,
        (["9.9.9"], False),
        _pass,
        lambda e: _other(e, "9.9.9", _rebuild_fix),
        id="live-preview-other-version-vsix-absent",
    ),
    pytest.param((["="], False), _GOOD, _vsix_absent, _pass, id="hdl-installed-vsix-absent"),
    pytest.param((None, True), _GOOD, _unreadable, _pass, id="hdl-registry-unreadable"),
    pytest.param(
        (None, False), _GOOD, _unreadable, _pass, id="hdl-registry-unreadable-vsix-absent"
    ),
]


# (id, host?, docker, container, replies overriding the healthy defaults, expected findings)
_OUTCOMES = [
    pytest.param(
        True,
        None,
        _CONTAINER,
        {"host": _Reply(3)},
        [(_S, f"{_DESCRIPTION} - no VS Code extension host attached", "")],
        id="inside-sandbox-no-extension-host",
    ),
    pytest.param(
        False,
        None,
        _CONTAINER,
        {},
        [(_S, f"{_DESCRIPTION} - container runtime unavailable", "")],
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
                f"{_DESCRIPTION} - inventory is ambiguous",
                "fix the Sandbox inventory error, then re-run `booley doctor`",
            )
        ],
        id="inventory-error",
    ),
    pytest.param(
        False,
        "docker",
        None,
        {},
        [(_S, f"{_DESCRIPTION} - no VS Code devcontainer running for this project", "")],
        id="no-vscode-devcontainer",
    ),
    pytest.param(
        False,
        "docker",
        _CONTAINER,
        {"host": _Reply(3)},
        [(_S, f"{_DESCRIPTION} - no VS Code extension host attached in '{_CONTAINER}'", "")],
        id="no-extension-host-in-container",
    ),
]


@pytest.mark.parametrize(("inside", "docker", "container", "replies", "expected"), _OUTCOMES)
def test_inspection_outcomes(
    tmp_path, monkeypatch, sandbox, inside, docker, container, replies, expected
):
    """Every outcome: severity, message and fix, in order, with no check ID."""
    _host(monkeypatch, container=container, inside=inside)
    sandbox.replies.update(replies)
    if inside:
        expected = [(sev, msg.replace(_WHERE, ""), fix) for sev, msg, fix in expected]

    assert _inspect_all(tmp_path, docker) == expected


def test_host_probes_run_in_the_vscode_container(tmp_path, monkeypatch, sandbox):
    """Host probes are `docker exec` into the container."""
    _host(monkeypatch)

    _inspect_all(tmp_path)

    prefix = ["docker", "exec", _CONTAINER, "python3", "-c"]
    assert sandbox.calls == [[*prefix, _sources()[name]] for name in ("host", "bundled")]


def test_inside_sandbox_probes_run_locally(tmp_path, monkeypatch, sandbox):
    """Inside the Sandbox there is no Docker and no container: probes run in place."""
    _host(monkeypatch, inside=True)

    _inspect_all(tmp_path, None)

    assert sandbox.calls == [["python3", "-c", _sources()[n]] for n in ("host", "bundled")]


@pytest.mark.parametrize(("returncode", "proceeds"), [(0, True), (3, False), (1, True), (2, True)])
def test_extension_host_probe_exit_codes(tmp_path, monkeypatch, sandbox, returncode, proceeds):
    """Only exit 3 means "no window attached"; a broken probe never reads as that."""
    _host(monkeypatch)
    sandbox.replies["host"] = _Reply(returncode)

    assert (len(_inspect_all(tmp_path)) > 1) is proceeds


def _run_extension_host_probe(monkeypatch, tmp_path, cmdlines):
    """Execute the real probe source against a fake /proc and home; return (exit code, stdout).

    Each cmdline is a callable taking the fake home, or None for a process
    that exits between glob and read.
    """
    home = tmp_path / "home"
    home.mkdir()
    for name in ("HOME", "USERPROFILE"):  # Path.home() on POSIX and on Windows
        monkeypatch.setenv(name, str(home))
    fakes = []
    for index, cmdline in enumerate(cmdlines):
        path = tmp_path / "proc" / str(index) / "cmdline"
        if cmdline is not None:
            path.parent.mkdir(parents=True)
            path.write_bytes(cmdline(home))
        fakes.append(path)
    real_glob = pathlib.Path.glob

    def glob(self, pattern, *args, **kwargs):
        if self == pathlib.Path("/proc"):
            return iter(fakes)
        return real_glob(self, pattern, *args, **kwargs)

    monkeypatch.setattr(pathlib.Path, "glob", glob)
    out = io.StringIO()
    with contextlib.redirect_stdout(out), pytest.raises(SystemExit) as exited:
        exec(
            compile(bundled_extensions._VSCODE_EXTENSION_HOST_PROBE_SOURCE, "<probe>", "exec"), {}
        )
    return exited.value.code, out.getvalue()


def _host_cmdline(relative):
    """The cmdline of an extension host whose bootstrap lives at ``relative`` under the home."""

    def build(home):
        program = str(home / relative / "out" / "bootstrap-fork").removesuffix("bootstrap-fork")
        return b"node\x00" + program.encode() + b"bootstrap-fork\x00--type=extensionHost\x00"

    return build


_SLEEP = lambda _home: b"/usr/bin/sleep\x00100\x00"  # noqa: E731
_PTY_HOST = lambda _home: b"/vscode/out/bootstrap-fork\x00--type=ptyHost\x00"  # noqa: E731
_STABLE_PROC = _host_cmdline(Path(".vscode-server") / "bin" / "abc")
_INSIDERS_PROC = _host_cmdline(
    Path(".vscode-server-insiders") / "cli" / "servers" / "I-abc" / "server"
)
_ELSEWHERE_PROC = _host_cmdline(Path("agent") / "bin" / "abc")
# A path that merely starts like a root name is not that root.
_LOOKALIKE_PROC = _host_cmdline(Path(".vscode-server-other") / "bin" / "abc")
_OUTSIDE_HOME = lambda _home: b"/vscode/out/bootstrap-fork\x00--type=extensionHost\x00"  # noqa: E731


@pytest.mark.parametrize(
    ("cmdlines", "expected"),
    [
        ([_SLEEP, None, _STABLE_PROC], (0, {"roots": [_ROOT], "unidentified": 0})),
        (
            [_INSIDERS_PROC, _STABLE_PROC, _STABLE_PROC],
            (0, {"roots": [_ROOT, ".vscode-server-insiders"], "unidentified": 0}),
        ),
        ([_ELSEWHERE_PROC], (0, {"roots": [], "unidentified": 1})),
        ([_LOOKALIKE_PROC, _OUTSIDE_HOME], (0, {"roots": [], "unidentified": 2})),
        (
            [_STABLE_PROC, _ELSEWHERE_PROC],
            (0, {"roots": [_ROOT], "unidentified": 1}),
        ),
        ([_PTY_HOST, None], (3, None)),
        ([], (3, None)),
    ],
    ids=[
        "stable",
        "stable-and-insiders",
        "custom-folder",
        "lookalike-and-outside-home",
        "mapped-and-unmapped",
        "pty-host-only",
        "empty-table",
    ],
)
def test_extension_host_probe_source_maps_hosts_to_roots(
    monkeypatch, tmp_path, cmdlines, expected
):
    # The probe runs as `python3 -c` in the Sandbox, so execute the real source
    # against a fake process table and home directory.
    code, stdout = _run_extension_host_probe(monkeypatch, tmp_path, cmdlines)

    assert (code, json.loads(stdout) if stdout else None) == expected


_PROBE_SOURCE_NAMES = sorted(n for n in dir(bundled_extensions) if n.endswith("_PROBE_SOURCE"))


@pytest.mark.parametrize("name", _PROBE_SOURCE_NAMES)
def test_every_python_probe_source_compiles(name):
    compile(getattr(bundled_extensions, name), name, "exec")


@pytest.mark.parametrize("inventory", ["editor", "headless", "absent", "unknown"])
def test_project_sandbox_resolution(
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
    place(monkeypatch, RuntimeLocation.HOST)
    sandbox.replies["host"] = _Reply(3)

    findings = _inspect_all(workspace)

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


_PROBE_FAILURES = [
    pytest.param(_Reply(1, "Traceback (TypeError)"), id="old-booley"),
    pytest.param(_Reply(4), id="exit-4"),
    pytest.param(_Reply(0, "not json"), id="garbage"),
    pytest.param(_Reply(0, "[]"), id="not-object"),
    pytest.param(
        _Reply(0, '{"ms-vscode.live-server": {"versions": {".vscode-server": []}, "vsix": true}}'),
        id="missing-entry",
    ),
    pytest.param(
        _Reply(
            0,
            '{"mshr-h.veriloghdl": {"versions": {".vscode-server": "1.29.0"}, "vsix": true},'
            ' "ms-vscode.live-server": {"versions": {".vscode-server": []}, "vsix": true}}',
        ),
        id="bad-versions",
    ),
    pytest.param(
        _Reply(
            0,
            '{"mshr-h.veriloghdl": {"versions": {".vscode-server": []}, "vsix": "yes"},'
            ' "ms-vscode.live-server": {"versions": {".vscode-server": []}, "vsix": true}}',
        ),
        id="bad-vsix",
    ),
    pytest.param(
        _Reply(0, '{"mshr-h.veriloghdl": {"versions": {}, "vsix": true}}'), id="missing-root"
    ),
    pytest.param(_Reply(error=FileNotFoundError("docker")), id="no-docker"),
    pytest.param(_Reply(error=subprocess.TimeoutExpired("docker", 20)), id="timeout"),
]


@pytest.mark.parametrize(("hdl", "live", "expect_hdl", "expect_live"), _BUNDLED_OUTCOMES)
def test_bundled_extension_outcomes(
    tmp_path, monkeypatch, sandbox, hdl, live, expect_hdl, expect_live
):
    """Each bundled extension gets one line: PASS, or FAIL/SKIP with its message and fix."""
    _host(monkeypatch)
    sandbox.replies["bundled"] = _bundled(hdl, live)
    hdl_ext, live_ext = devcontainer.BUNDLED_EXTENSIONS

    assert _inspect_all(tmp_path) == [expect_hdl(hdl_ext), expect_live(live_ext)]


@pytest.mark.parametrize("reply", _PROBE_FAILURES)
def test_bundled_extension_probe_failure_skips_both(tmp_path, monkeypatch, sandbox, reply):
    _host(monkeypatch)
    sandbox.replies["bundled"] = reply

    expected = [
        (
            _S,
            f"{e.name} extension is the bundled {e.version} - extension probe could not run{_WHERE}",
            "",
        )
        for e in devcontainer.BUNDLED_EXTENSIONS
    ]
    assert _inspect_all(tmp_path) == expected


def test_healthy_window_reports_only_the_bundled_extensions(tmp_path, monkeypatch, sandbox):
    _host(monkeypatch)

    assert _inspect_all(tmp_path) == [*(_pass(e) for e in devcontainer.BUNDLED_EXTENSIONS)]


def test_no_extension_host_runs_no_extension_probe(tmp_path, monkeypatch, sandbox):
    _host(monkeypatch)
    sandbox.replies["host"] = _Reply(3)

    assert _inspect_all(tmp_path) == [
        (_S, f"{_DESCRIPTION} - no VS Code extension host attached in '{_CONTAINER}'", "")
    ]
    assert sandbox.probes() == ["host"]


def test_bundled_extensions_probe_source_runs_against_a_real_registry(tmp_path):
    """The probe text runs as `python3 -c`: keys, the registry read and the file test."""
    registry = tmp_path / ".vscode-server" / "extensions"
    registry.mkdir(parents=True)
    # An Insiders registry of another build is not among the roots asked for.
    other = tmp_path / ".vscode-server-insiders" / "extensions"
    other.mkdir(parents=True)
    (other / "extensions.json").write_text("not read", encoding="utf-8")
    entry = {"identifier": {"id": "mshr-h.veriloghdl"}, "version": "1.29.0"}
    (registry / "extensions.json").write_text(json.dumps([entry]), encoding="utf-8")
    present = tmp_path / "present.vsix"
    present.write_bytes(b"")
    source = bundled_extensions._bundled_extensions_probe_source(
        [
            bundled_extensions.BundledExtension("A", "mshr-h.veriloghdl", "1.29.0", str(present)),
            bundled_extensions.BundledExtension(
                "B", "ms-vscode.live-server", "0.4.20", str(tmp_path / "absent.vsix")
            ),
        ],
        (_ROOT, ".vscode-remote"),
    )
    env = {
        **os.environ,
        "HOME": str(tmp_path),
        "USERPROFILE": str(tmp_path),  # Path.home() on Windows
        "PYTHONPATH": str(Path(bundled_extensions.__file__).parents[2]),
    }

    result = run_bounded_child([sys.executable, "-c", source], env=env)

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "mshr-h.veriloghdl": {
            "versions": {_ROOT: ["1.29.0"], ".vscode-remote": None},
            "vsix": True,
        },
        "ms-vscode.live-server": {"versions": {_ROOT: [], ".vscode-remote": None}, "vsix": False},
    }


def _fake_probe_process(monkeypatch, tmp_path, hosts):
    """A ``subprocess.run`` that runs each probe source in-process against ``tmp_path`` as home.

    ``hosts`` are the extension-host program paths under ``tmp_path``; the
    fake ``/proc`` holds one cmdline file per host.
    """
    for name in ("HOME", "USERPROFILE"):  # Path.home() on POSIX and on Windows
        monkeypatch.setenv(name, str(tmp_path))
    fakes = []
    for index, host in enumerate(hosts):
        cmdline = tmp_path / "proc" / str(index) / "cmdline"
        cmdline.parent.mkdir(parents=True)
        program = str(tmp_path / host).encode()
        cmdline.write_bytes(b"node\x00" + program + b"\x00--type=extensionHost\x00")
        fakes.append(cmdline)
    real_glob = pathlib.Path.glob

    def glob(self, pattern, *args, **kwargs):
        if self == pathlib.Path("/proc"):
            return iter(fakes)
        return real_glob(self, pattern, *args, **kwargs)

    monkeypatch.setattr(pathlib.Path, "glob", glob)

    def run(argv, **kwargs):
        out = io.StringIO()
        code = 0
        with contextlib.redirect_stdout(out):
            try:
                exec(compile(argv[-1], "<probe>", "exec"), {})
            except SystemExit as exited:
                code = exited.code
        return subprocess.CompletedProcess(argv, code, out.getvalue(), "")

    monkeypatch.setattr(bundled_extensions.subprocess, "run", run)


def _write_registry(tmp_path, root, installed):
    """One server root's extensions.json holding ``installed`` (extension ID -> version)."""
    registry = tmp_path / root / "extensions"
    registry.mkdir(parents=True)
    entries = [{"identifier": {"id": ext}, "version": v} for ext, v in installed.items()]
    (registry / "extensions.json").write_text(json.dumps(entries), encoding="utf-8")


_STABLE, _INSIDERS = ".vscode-server", ".vscode-server-insiders"
_STABLE_HOST = f"{_STABLE}/bin/abc/out/bootstrap-fork"
_INSIDERS_HOST = f"{_INSIDERS}/cli/servers/Insiders-abc/server/out/bootstrap-fork"
_OUTSIDE = "VS Code extension host runs outside a known VS Code server root"


@pytest.mark.parametrize("case", ["stable-empty-insiders-bundled", "custom-agent-folder", "both"])
def test_extension_checks_read_only_attached_roots(tmp_path, monkeypatch, case):
    """The extension lines judge the roots with a live host, never the union."""
    _host(monkeypatch, inside=True)
    hdl, live = devcontainer.BUNDLED_EXTENSIONS
    vsix = []
    for extension in (hdl, live):
        path = tmp_path / f"{extension.name}.vsix"
        path.write_bytes(b"")
        vsix.append(dataclasses.replace(extension, vsix=str(path)))
    hdl, live = vsix
    monkeypatch.setattr(devcontainer, "BUNDLED_EXTENSIONS", (hdl, live))
    everything = {hdl.extension_id: hdl.version, live.extension_id: live.version}
    desc_hdl = f"{hdl.name} extension is the bundled {hdl.version}"
    desc_live = f"{live.name} extension is the bundled {live.version}"
    if case == "stable-empty-insiders-bundled":
        _write_registry(tmp_path, _STABLE, {})
        _write_registry(tmp_path, _INSIDERS, everything)
        hosts = [_STABLE_HOST]
        expected = [
            (_F, f"{hdl.name} extension ({hdl.extension_id}) is not installed", _install_fix(hdl)),
            (
                _F,
                f"{live.name} extension ({live.extension_id}) is not installed",
                _install_fix(live),
            ),
        ]
    elif case == "custom-agent-folder":
        _write_registry(tmp_path, _STABLE, everything)
        hosts = ["agent/bin/abc/out/bootstrap-fork"]
        expected = [
            (_S, f"{description} - {_OUTSIDE}", "") for description in (desc_hdl, desc_live)
        ]
    else:
        _write_registry(tmp_path, _STABLE, everything)
        _write_registry(tmp_path, _INSIDERS, {**everything, hdl.extension_id: "9.9.9"})
        hosts = [_STABLE_HOST, _INSIDERS_HOST]
        both = f"[{_STABLE}] {{0}}; [{_INSIDERS}] {{0}}"
        expected = [
            (
                _F,
                f"[{_INSIDERS}] {hdl.name} 9.9.9 is installed; the Sandbox Image bundles "
                f"{hdl.version}; [{_STABLE}] {desc_hdl}",
                _install_fix(hdl),
            ),
            (_P, both.format(desc_live), ""),
        ]
    _fake_probe_process(monkeypatch, tmp_path, hosts)

    assert _inspect_all(tmp_path, None) == expected


def test_bundled_lines_judge_each_attached_root(tmp_path, monkeypatch, sandbox):
    _host(monkeypatch)
    hdl, live = devcontainer.BUNDLED_EXTENSIONS
    answer = {
        hdl.extension_id: {"versions": {_STABLE: [hdl.version], _INSIDERS: []}, "vsix": True},
        live.extension_id: {
            "versions": {_STABLE: [live.version], _INSIDERS: [live.version]},
            "vsix": False,
        },
    }
    sandbox.attach(_STABLE, _INSIDERS)
    sandbox.replies["bundled"] = _Reply(stdout=json.dumps(answer))

    assert _inspect_all(tmp_path) == [
        (
            _F,
            f"[{_INSIDERS}] {hdl.name} extension ({hdl.extension_id}) is not installed{_WHERE}; "
            f"[{_STABLE}] {hdl.name} extension is the bundled {hdl.version}",
            _install_fix(hdl),
        ),
        (
            _F,
            f"[{_STABLE}] {live.name} {live.version} is installed{_WHERE}, but its bundled VSIX "
            f"is absent; [{_INSIDERS}] {live.name} {live.version} is installed{_WHERE}, but its "
            "bundled VSIX is absent",
            _rebuild_fix(live),
        ),
    ]


def test_probe_failures_with_several_roots_keep_one_skip_line(tmp_path, monkeypatch, sandbox):
    _host(monkeypatch)
    sandbox.attach(_STABLE, _INSIDERS)
    sandbox.replies["bundled"] = _Reply(4)

    assert [f[1] for f in _inspect_all(tmp_path)] == [
        f"{e.name} extension is the bundled {e.version} - extension probe could not run{_WHERE}"
        for e in devcontainer.BUNDLED_EXTENSIONS
    ]


def test_unplaced_host_beside_a_mapped_one_leaves_the_checks_at_skip(
    tmp_path, monkeypatch, sandbox
):
    _host(monkeypatch)
    sandbox.attach(_STABLE, unidentified=1)

    descriptions = [
        f"{e.name} extension is the bundled {e.version}" for e in devcontainer.BUNDLED_EXTENSIONS
    ]
    assert _inspect_all(tmp_path) == [
        (_S, f"{d} - {_OUTSIDE}{_WHERE}; [{_STABLE}] {d}", "") for d in descriptions
    ]
    assert sandbox.probes() == ["host", "bundled"]


@pytest.mark.parametrize(
    "reply",
    [
        _Reply(0, "not json"),
        _Reply(0, "[]"),
        _Reply(0, '{"roots": [], "unidentified": 0}'),
        _Reply(0, '{"roots": [".vscode-server"]}'),
        _Reply(0, '{"roots": [".vscode-server"], "unidentified": true}'),
        _Reply(0, '{"roots": [".other"], "unidentified": 0}'),
        _Reply(0, '{"roots": [5], "unidentified": 0}'),
        _Reply(1, '{"roots": [".vscode-server"], "unidentified": 0}'),
        _Reply(error=FileNotFoundError("docker")),
    ],
    ids=[
        "garbage",
        "not-object",
        "nothing-attached",
        "missing-count",
        "bool-count",
        "unknown-root-name",
        "root-not-string",
        "exit-1",
        "no-docker",
    ],
)
def test_unreadable_host_answer_skips_the_extension_checks(tmp_path, monkeypatch, sandbox, reply):
    """No registry is read when the roots are unknown."""
    _host(monkeypatch)
    sandbox.replies["host"] = reply

    skipped = [
        f"{e.name} extension is the bundled {e.version}" for e in devcontainer.BUNDLED_EXTENSIONS
    ]
    assert _inspect_all(tmp_path) == [
        (_S, f"{d} - extension host probe could not run{_WHERE}", "") for d in skipped
    ]
    assert sandbox.probes() == ["host"]
