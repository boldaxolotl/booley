"""Bundled extension delivery and actual JavaScript lifecycle tests."""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path
from xml.etree import ElementTree

import pytest

from booley.runtime import dashboard_extension, devcontainer
from booley.runtime.platform_paths import kill_process_tree, popen_new_group_kwargs


def test_vsix_is_deterministic_and_contains_installable_workspace_manifest(tmp_path):
    first = dashboard_extension.render_vsix()
    assert first == dashboard_extension.render_vsix()
    output = tmp_path / "dashboard.vsix"
    dashboard_extension.write_vsix(output)
    assert output.read_bytes() == first
    assert dashboard_extension.installed_error(output) is None
    with zipfile.ZipFile(io.BytesIO(first)) as archive:
        manifest = json.loads(archive.read("extension/package.json"))
        assert manifest["extensionKind"] == ["workspace"]
        assert manifest["activationEvents"] == ["onStartupFinished"]
        assert manifest["license"] == "Apache-2.0"
        assert b"Apache License" in archive.read("extension/LICENSE.txt")
        assert b"exports.activate" in archive.read("extension/extension.js")
        assert b"exports.start" in archive.read("extension/vaporview_bridge.js")
        xml = ElementTree.fromstring(archive.read("extension.vsixmanifest"))
        identity = xml.find("{*}Metadata/{*}Identity")
        assert identity is not None
        assert identity.attrib["Version"] == manifest["version"]
        assert identity.attrib["Publisher"] == manifest["publisher"]


def test_doctor_reports_missing_and_stale_extension_artifacts(tmp_path):
    from booley.harness import doctor

    path = tmp_path / "missing.vsix"
    assert "unavailable" in dashboard_extension.installed_error(path)
    path.write_bytes(b"old VSIX")
    assert "differs" in dashboard_extension.installed_error(path)
    warnings = []
    passes = []
    from unittest.mock import patch

    with patch.object(dashboard_extension, "installed_error", lambda: "stale extension"):
        doctor._check_current_runtime_dashboard_extension(passes.append, warnings.append)
    assert not passes
    assert "rebuild" in warnings[0]
    assert warnings[0].check_id == "sandbox.dashboard-extension-unavailable"


def test_every_sandbox_app_gets_the_image_local_extension():
    for app in devcontainer.SUPPORTED_APPS:
        spec = devcontainer.build_devcontainer_spec(app, dashboard=True)
        settings = spec["customizations"]["vscode"]
        assert dashboard_extension.VSIX_PATH in settings["extensions"]
        assert settings["settings"]["booley.dashboard.attachProtocol"] == 1
        assert "task.allowAutomaticTasks" not in settings["settings"]


# --- Node suites: one bounded runner for every `node --test` file -------------

# Passed as `node --test --test-timeout`. Under node's default per-file
# isolation the timeout also bounds the whole test file (node reports the file,
# not the stuck subtest; the stuck subtest is the one after the last `ok` line),
# so it must be generous. Normal Windows runs take 3.4-5.1 s.
_NODE_TEST_TIMEOUT_MS = 60_000

# Wall-clock bound for the whole `node` process, above the node timeout so node
# normally reports a stall first. It still catches stalls node cannot see, such
# as a hang in node startup before the file's child process runs. About 15x the
# slowest passing Windows run.
_NODE_SUITE_BOUND_S = 75

# pytest-timeout budget for the node suite tests. Derivation:
# - Kill: the bound plus up to 27 s of `kill_process_tree` on Windows (graceful
#   taskkill <= 10 s + 2 s grace + forced taskkill <= 10 s + 5 s final wait)
#   is 102 s, under 180 s, so a stall fails through the bound with node's
#   output rather than through a pytest-timeout thread kill of the worker.
# - Headroom: the strictest Windows fail limit is `--timeout-headroom=0.5` in
#   full-python-matrix.yml, i.e. 90 s. The 75 s bound leaves 15 s for setup
#   and teardown, so the bound fires before any headroom failure.
_NODE_SUITE_PYTEST_TIMEOUT_S = 180


def _read_output(path: Path) -> str:
    """Return the text a child wrote to ``path`` (node writes UTF-8)."""
    return path.read_text(encoding="utf-8", errors="replace")


def run_bounded(command: list[str], *, bound_s: float, output_dir: Path) -> tuple[int, str, str]:
    """Run ``command`` for at most ``bound_s`` seconds; return (code, stdout, stderr).

    Output goes to files under ``output_dir`` rather than pipes, so no reader
    can block on a handle that a surviving grandchild still holds (the
    Windows hang behind #1202). On expiry the whole process tree is killed
    and the test fails with whatever the command wrote so far; the plain
    ``subprocess.run(timeout=...)`` ``TimeoutExpired`` omits that output.
    """
    stdout_path = output_dir / "stdout.txt"
    stderr_path = output_dir / "stderr.txt"
    with stdout_path.open("wb") as stdout_file, stderr_path.open("wb") as stderr_file:
        proc = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=stdout_file,
            stderr=stderr_file,
            **popen_new_group_kwargs(),
        )
        try:
            proc.wait(timeout=bound_s)
        except subprocess.TimeoutExpired:
            kill_process_tree(proc)
            pytest.fail(
                f"{' '.join(command[1:])} exceeded its {bound_s:g} s bound.\n"
                f"stdout:\n{_read_output(stdout_path) or '(nothing)'}\n"
                f"stderr:\n{_read_output(stderr_path) or '(nothing)'}"
            )
    return proc.returncode, _read_output(stdout_path), _read_output(stderr_path)


def _run_node_suite(script_name: str, *, missing_node_message: str, output_dir: Path) -> None:
    """Run ``tests/vscode/<script_name>`` with ``node --test`` under the shared bounds."""
    node = shutil.which("node")
    if node is None:
        if os.environ.get("CI"):
            pytest.fail(f"CI must run the {missing_node_message}: install Node.js")
        pytest.skip(f"Node.js is required for the {missing_node_message}")
    script = Path(__file__).parents[1] / "vscode" / script_name
    code, out, err = run_bounded(
        [node, "--test", f"--test-timeout={_NODE_TEST_TIMEOUT_MS}", str(script)],
        bound_s=_NODE_SUITE_BOUND_S,
        output_dir=output_dir,
    )
    assert code == 0, out + err


@pytest.mark.timeout(_NODE_SUITE_PYTEST_TIMEOUT_S)
def test_workspace_extension_lifecycle_in_node(tmp_path):
    _run_node_suite(
        "dashboard.test.cjs",
        missing_node_message="Dashboard lifecycle suite",
        output_dir=tmp_path,
    )


@pytest.mark.skipif(
    sys.platform == "win32", reason="the bridge serves Unix sockets inside Linux Sandboxes"
)
@pytest.mark.timeout(_NODE_SUITE_PYTEST_TIMEOUT_S)
def test_vaporview_bridge_in_node(tmp_path):
    _run_node_suite(
        "vaporview_bridge.test.cjs",
        missing_node_message="VaporView bridge suite",
        output_dir=tmp_path,
    )


def test_disabled_dashboard_keeps_the_viewer_bridge_but_never_opens_the_dashboard():
    # The extension also hosts the `bwave gui` viewer bridge, so it is always
    # installed; the attach protocol alone keeps the Dashboard terminal closed.
    spec = devcontainer.build_devcontainer_spec(dashboard=False)
    settings = spec["customizations"]["vscode"]
    assert dashboard_extension.VSIX_PATH in settings["extensions"]
    assert settings["settings"]["booley.dashboard.attachProtocol"] == 0
