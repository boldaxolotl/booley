"""Bundled extension delivery and actual JavaScript lifecycle tests."""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import zipfile
from pathlib import Path
from xml.etree import ElementTree

import pytest

from booley.runtime import dashboard_extension, devcontainer


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


def test_workspace_extension_lifecycle_in_node():
    node = shutil.which("node")
    if node is None:
        if os.environ.get("CI"):
            pytest.fail("CI must run the Dashboard lifecycle suite: install Node.js")
        pytest.skip("Node.js is required for the Dashboard extension lifecycle suite")
    script = Path(__file__).parents[1] / "vscode/dashboard.test.cjs"
    result = subprocess.run(
        [node, "--test", str(script)], capture_output=True, text=True, timeout=30, check=False
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_disabled_dashboard_does_not_require_extension_in_image():
    spec = devcontainer.build_devcontainer_spec(dashboard=False)
    settings = spec["customizations"]["vscode"]
    assert dashboard_extension.VSIX_PATH not in settings["extensions"]
    assert settings["settings"]["booley.dashboard.attachProtocol"] == 0
