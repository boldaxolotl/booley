"""Tests for the live-version probe of one extension in one VS Code server root."""

from __future__ import annotations

import json

import pytest

from booley.runtime import bundled_extensions

_LIVE_PREVIEW = "ms-vscode.live-server"
_HDL = "mshr-h.veriloghdl"


def _install(root, version, extension_id=_LIVE_PREVIEW):
    manifest = root / f"{extension_id}-{version}" / "package.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(json.dumps({"version": version}), encoding="utf-8")


def _versions(tmp_path, server=".vscode-server", extension_id=_LIVE_PREVIEW):
    return bundled_extensions.server_root_versions(tmp_path / server, extension_id)


def test_server_root_versions_unknown_without_registry(tmp_path):
    assert _versions(tmp_path) is None


def test_server_root_versions_reads_only_that_root(tmp_path):
    _install(tmp_path / ".vscode-server" / "extensions", "0.4.20")
    _install(tmp_path / ".vscode-server-insiders" / "extensions", "9.9.9")

    assert _versions(tmp_path) == ["0.4.20"]
    assert _versions(tmp_path, ".vscode-server-insiders") == ["9.9.9"]


def test_server_root_versions_skips_obsolete_build(tmp_path):
    # A downgrade to the pinned build leaves the newer directory marked obsolete.
    root = tmp_path / ".vscode-server" / "extensions"
    _install(root, "9.9.9")
    _install(root, "0.4.20")
    (root / ".obsolete").write_text(f'{{"{_LIVE_PREVIEW}-9.9.9": true}}', encoding="utf-8")

    assert _versions(tmp_path) == ["0.4.20"]


def test_server_root_versions_prefers_vscode_registry_after_downgrade(tmp_path):
    # Observed with VS Code 1.143: `--install-extension id@version` over a newer
    # install rewrites extensions.json at once but leaves the newer directory, unmarked.
    root = tmp_path / ".vscode-server" / "extensions"
    _install(root, "9.9.9")
    _install(root, "0.4.20")
    registry = [
        {"identifier": {"id": _LIVE_PREVIEW}, "version": "0.4.20"},
        {"identifier": {"id": _HDL}, "version": "9.9.9"},
    ]
    (root / "extensions.json").write_text(json.dumps(registry), encoding="utf-8")

    assert _versions(tmp_path) == ["0.4.20"]


@pytest.mark.parametrize("layout", ["registry", "directories"])
def test_server_root_versions_reads_the_requested_extension(tmp_path, layout):
    root = tmp_path / ".vscode-server" / "extensions"
    _install(root, "0.4.20")
    _install(root, "1.29.1", _HDL)
    if layout == "registry":
        registry = [
            {"identifier": {"id": "Ms-VSCode.Live-Server"}, "version": "0.4.20"},
            {"identifier": {"id": _HDL}, "version": "1.29.1"},
        ]
        (root / "extensions.json").write_text(json.dumps(registry), encoding="utf-8")

    assert _versions(tmp_path) == ["0.4.20"]
    assert _versions(tmp_path, extension_id=_HDL) == ["1.29.1"]


@pytest.mark.parametrize(
    "registry",
    ["not json", '{"not": "a list"}', f'[{{"identifier": {{"id": "{_LIVE_PREVIEW}"}}}}]'],
    ids=["unparsable", "not-a-list", "missing-version"],
)
def test_server_root_versions_malformed_registry_is_unknown(tmp_path, registry):
    root = tmp_path / ".vscode-server" / "extensions"
    root.mkdir(parents=True)
    (root / "extensions.json").write_text(registry, encoding="utf-8")

    assert _versions(tmp_path) is None


def test_server_root_versions_empty_registry(tmp_path):
    (tmp_path / ".vscode-server" / "extensions").mkdir(parents=True)

    assert _versions(tmp_path) == []


def test_server_root_versions_partial_install_is_unknown(tmp_path):
    (tmp_path / ".vscode-server" / "extensions" / f"{_LIVE_PREVIEW}-0.4.20").mkdir(parents=True)

    assert _versions(tmp_path) is None
