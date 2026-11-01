"""Tests for VaporView extension-presence diagnosis."""

from __future__ import annotations

import json
import subprocess

import pytest

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


def _write_install(home, root_name: str) -> None:
    """Publish a valid VaporView manifest under ``home/<root_name>/extensions``."""
    manifest = home / root_name / "extensions" / "lramseyer.vaporview-1.5.4" / "package.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(json.dumps({"publisher": "Lramseyer", "name": "VaporView"}), "utf-8")


def test_home_probe_reports_install_under_codespaces_root(tmp_path):
    _write_install(tmp_path, ".vscode-remote")

    assert vaporview.probe_home(tmp_path) is vaporview.ExtensionState.INSTALLED


def test_find_manifests_covers_every_server_root(tmp_path):
    _write_install(tmp_path, ".vscode-server")
    _write_install(tmp_path, ".vscode-remote")

    found = vaporview.find_manifests(tmp_path)

    assert {path.parents[2].name for path in found} == {".vscode-server", ".vscode-remote"}


@pytest.mark.parametrize("layout", ["conflicting-roots", "unknown-elsewhere"])
def test_home_probe_combines_server_roots(tmp_path, layout):
    """Registries that disagree leave the probe UNKNOWN instead of guessing."""
    (tmp_path / ".vscode-server" / "extensions").mkdir(parents=True)
    if layout == "conflicting-roots":
        _write_install(tmp_path, ".vscode-remote")
    else:
        (tmp_path / ".vscode-remote" / "extensions" / "lramseyer.vaporview-1.5.4").mkdir(
            parents=True
        )

    assert vaporview.probe_home(tmp_path) is vaporview.ExtensionState.UNKNOWN


def _install(root, version, *, publisher="lramseyer"):
    manifest = root / f"lramseyer.vaporview-{version}" / "package.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(
        f'{{"publisher": "{publisher}", "name": "vaporview", "version": "{version}"}}',
        encoding="utf-8",
    )


def test_installed_versions_unknown_without_server_root(tmp_path):
    assert vaporview.installed_versions(tmp_path) is None


def test_installed_versions_lists_live_builds_across_roots(tmp_path):
    _install(tmp_path / ".vscode-server" / "extensions", "1.5.4")
    _install(tmp_path / ".vscode-remote" / "extensions", "1.6.0")

    assert vaporview.installed_versions(tmp_path) == ["1.5.4", "1.6.0"]


def test_installed_versions_skips_obsolete_build(tmp_path):
    # A downgrade to the pinned build leaves the newer directory marked obsolete.
    root = tmp_path / ".vscode-server" / "extensions"
    _install(root, "1.6.0")
    _install(root, "1.5.4")
    (root / ".obsolete").write_text('{"lramseyer.vaporview-1.6.0": true}', encoding="utf-8")

    assert vaporview.installed_versions(tmp_path) == ["1.5.4"]


def test_installed_versions_prefers_vscode_registry_after_downgrade(tmp_path):
    # Observed with VS Code 1.143: `--install-extension id@1.5.4` over 1.6.0
    # rewrites extensions.json at once but leaves the 1.6.0 directory, unmarked.
    root = tmp_path / ".vscode-server" / "extensions"
    _install(root, "1.6.0")
    _install(root, "1.5.4")
    registry = [
        {"identifier": {"id": "lramseyer.vaporview"}, "version": "1.5.4"},
        {"identifier": {"id": "mshr-h.veriloghdl"}, "version": "1.6.0"},
    ]
    (root / "extensions.json").write_text(json.dumps(registry), encoding="utf-8")

    assert vaporview.installed_versions(tmp_path) == ["1.5.4"]


@pytest.mark.parametrize(
    "registry",
    ["not json", '{"not": "a list"}', '[{"identifier": {"id": "lramseyer.vaporview"}}]'],
    ids=["unparsable", "not-a-list", "missing-version"],
)
def test_installed_versions_malformed_registry_is_unknown(tmp_path, registry):
    root = tmp_path / ".vscode-server" / "extensions"
    root.mkdir(parents=True)
    (root / "extensions.json").write_text(registry, encoding="utf-8")

    assert vaporview.installed_versions(tmp_path) is None


def test_installed_versions_empty_registry(tmp_path):
    (tmp_path / ".vscode-server" / "extensions").mkdir(parents=True)

    assert vaporview.installed_versions(tmp_path) == []


def test_installed_versions_partial_install_is_unknown(tmp_path):
    (tmp_path / ".vscode-server" / "extensions" / "lramseyer.vaporview-1.5.4").mkdir(parents=True)

    assert vaporview.installed_versions(tmp_path) is None


def test_install_guidance_installs_the_bundled_build_offline():
    guidance = vaporview.install_guidance()

    assert f"code --install-extension {vaporview.BUNDLED_VSIX}\n" in guidance
    assert f"{vaporview.EXTENSION_ID}@{vaporview.VERIFIED_VERSION}" in guidance
    # The Sandbox has no network: nothing may send the user to a gallery.
    assert "Marketplace" not in guidance
    assert "--install-extension lramseyer" not in guidance


def test_install_guidance_names_the_probed_editor():
    assert "cursor --install-extension " in vaporview.install_guidance(editor="cursor")


def test_bundled_vsix_tracks_the_verified_version():
    expected = f"/usr/local/libexec/booley/vaporview-{vaporview.VERIFIED_VERSION}.vsix"
    assert expected == vaporview.BUNDLED_VSIX


@pytest.mark.parametrize(
    ("entry", "vaporview_entry"),
    [
        (vaporview.BUNDLED_VSIX, True),
        ("/usr/local/libexec/booley/vaporview-1.5.4.vsix", True),
        ("/opt/VaporView-9.9.9.VSIX", True),
        (vaporview.EXTENSION_ID, True),
        ("LRamseyer.VaporView@" + vaporview.VERIFIED_VERSION, True),
        (f"{vaporview.EXTENSION_ID}@0.0.1", True),
        ("lramseyer.vaporview-extra", False),
        ("/usr/local/libexec/booley/dashboard.vsix", False),
        ("/usr/local/libexec/booley/vaporview-1.6.0.zip", False),
        (42, False),
    ],
)
def test_extension_entry_classifier(entry, vaporview_entry):
    assert vaporview.is_vaporview_entry(entry) is vaporview_entry
