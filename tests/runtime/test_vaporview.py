"""Tests for VaporView extension-presence diagnosis."""

from __future__ import annotations

import subprocess

from booley.runtime import vaporview


def test_editor_probe_reports_installed_case_insensitively():
    def run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, stdout="Lramseyer.VaporView\n", stderr="")

    assert vaporview.probe_editor("code", run=run) is vaporview.ExtensionState.INSTALLED


def test_editor_probe_reports_missing_after_successful_listing():
    def run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, stdout="ms-python.python\n", stderr="")

    assert vaporview.probe_editor("code", run=run) is vaporview.ExtensionState.MISSING


def test_editor_probe_reports_unknown_when_listing_fails():
    def run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="unavailable")

    assert vaporview.probe_editor("code", run=run) is vaporview.ExtensionState.UNKNOWN


def test_editor_probe_reports_unknown_on_timeout():
    def run(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, 30)

    assert vaporview.probe_editor("code", run=run) is vaporview.ExtensionState.UNKNOWN


def test_editor_aggregation_never_hides_an_unknown_probe():
    states = [vaporview.ExtensionState.MISSING, vaporview.ExtensionState.UNKNOWN]

    assert vaporview.aggregate_states(states) is vaporview.ExtensionState.UNKNOWN


def test_editor_aggregation_keeps_conflicting_registries_unknown():
    states = [vaporview.ExtensionState.UNKNOWN, vaporview.ExtensionState.INSTALLED]

    assert vaporview.aggregate_states(states) is vaporview.ExtensionState.UNKNOWN


def test_editor_aggregation_accepts_unanimous_install():
    states = [vaporview.ExtensionState.INSTALLED, vaporview.ExtensionState.INSTALLED]

    assert vaporview.aggregate_states(states) is vaporview.ExtensionState.INSTALLED


def test_home_probe_distinguishes_missing_root_from_empty_registry(tmp_path):
    assert vaporview.probe_home(tmp_path) is vaporview.ExtensionState.UNKNOWN

    (tmp_path / ".vscode-server" / "extensions").mkdir(parents=True)
    assert vaporview.probe_home(tmp_path) is vaporview.ExtensionState.MISSING


def test_home_probe_reports_installed_manifest(tmp_path):
    manifest = (
        tmp_path / ".vscode-server" / "extensions" / "lramseyer.vaporview-1.5.4" / "package.json"
    )
    manifest.parent.mkdir(parents=True)
    manifest.write_text('{"publisher": "Lramseyer", "name": "VaporView"}', encoding="utf-8")

    assert vaporview.probe_home(tmp_path) is vaporview.ExtensionState.INSTALLED


def test_home_probe_treats_partial_install_as_unknown(tmp_path):
    partial = tmp_path / ".vscode-server" / "extensions" / "lramseyer.vaporview-1.5.4"
    partial.mkdir(parents=True)

    assert vaporview.probe_home(tmp_path) is vaporview.ExtensionState.UNKNOWN


def test_home_probe_excludes_obsolete_install(tmp_path):
    root = tmp_path / ".vscode-server" / "extensions"
    manifest = root / "lramseyer.vaporview-1.5.4" / "package.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text('{"publisher": "lramseyer", "name": "vaporview"}', encoding="utf-8")
    (root / ".obsolete").write_text('{"lramseyer.vaporview-1.5.4": true}', encoding="utf-8")

    assert vaporview.probe_home(tmp_path) is vaporview.ExtensionState.MISSING


def test_home_probe_treats_malformed_manifest_as_unknown(tmp_path):
    manifest = (
        tmp_path / ".vscode-server" / "extensions" / "lramseyer.vaporview-1.5.4" / "package.json"
    )
    manifest.parent.mkdir(parents=True)
    manifest.write_text("{broken", encoding="utf-8")

    assert vaporview.probe_home(tmp_path) is vaporview.ExtensionState.UNKNOWN


def test_home_probe_treats_mismatched_manifest_identity_as_unknown(tmp_path):
    manifest = (
        tmp_path / ".vscode-server" / "extensions" / "lramseyer.vaporview-1.5.4" / "package.json"
    )
    manifest.parent.mkdir(parents=True)
    manifest.write_text('{"publisher": "someone", "name": "different"}', encoding="utf-8")

    assert vaporview.probe_home(tmp_path) is vaporview.ExtensionState.UNKNOWN
