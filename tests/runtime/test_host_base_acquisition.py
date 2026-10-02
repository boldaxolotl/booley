"""The import-light Bootstrap owner verifies compatibility and migration."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from booley.runtime import host_base_acquisition as owner
from booley.runtime.image_lifecycle import HostImageScope
from booley.runtime.image_provenance import LABEL_RUNTIME_BASE_CONTRACT


@pytest.fixture
def base_owner(monkeypatch):
    labels = {LABEL_RUNTIME_BASE_CONTRACT: "current", "role": "runtime-base"}
    monkeypatch.setattr(owner, "_labels", lambda _root: labels)
    monkeypatch.setattr(
        owner,
        "source_image_build_contracts",
        lambda _root: SimpleNamespace(runtime_base="current"),
    )
    return labels


@pytest.mark.parametrize("installed,rebuild", [(None, False), ("old", False), ("current", True)])
def test_owner_builds_only_missing_stale_or_explicitly_refreshed_base(
    tmp_path, monkeypatch, base_owner, installed, rebuild
):
    records = iter(
        [
            None
            if installed is None
            else {"Id": "old-id", "Config": {"Labels": {LABEL_RUNTIME_BASE_CONTRACT: installed}}},
            {"Id": "new-id", "Config": {"Labels": base_owner}},
        ]
    )
    monkeypatch.setattr(owner, "_inspect", lambda _ref: next(records))
    builds = []
    assert (
        owner.acquire(
            tmp_path,
            scope=HostImageScope(),
            rebuild=rebuild,
            build=lambda: builds.append("base") or True,
        )
        == "new-id"
    )
    assert builds == ["base"]


def test_owner_reuses_current_labels_without_build_or_mutation(tmp_path, monkeypatch, base_owner):
    monkeypatch.setattr(
        owner, "_inspect", lambda _ref: {"Id": "same-id", "Config": {"Labels": base_owner}}
    )
    assert (
        owner.acquire(
            tmp_path, scope=HostImageScope(), build=lambda: pytest.fail("current base built")
        )
        == "same-id"
    )


@pytest.mark.parametrize("layers", [["base"], ["different"]])
def test_metadata_upgrade_preserves_layers_and_never_cold_builds(
    tmp_path, monkeypatch, base_owner, layers
):
    installed = {
        "Id": "old-id",
        "Config": {"Labels": {LABEL_RUNTIME_BASE_CONTRACT: "current"}},
        "RootFS": {"Layers": ["base"]},
    }
    candidate = {
        "Id": "upgraded-id",
        "Config": {"Labels": base_owner},
        "RootFS": {"Layers": layers},
    }
    monkeypatch.setattr(
        owner, "_inspect", lambda ref: installed if ref == owner.REFERENCE else candidate
    )
    monkeypatch.setattr(
        owner, "_build", lambda _root, recipe, output, plan: recipe.read_text() == "FROM old-id\n"
    )
    mutations = []
    monkeypatch.setattr(
        owner.subprocess,
        "run",
        lambda args, **_kw: mutations.append(args) or SimpleNamespace(returncode=0),
    )
    if layers == ["base"]:
        assert (
            owner.acquire(
                tmp_path, scope=HostImageScope(), build=lambda: pytest.fail("cold base built")
            )
            == "upgraded-id"
        )
        assert ["docker", "tag", "upgraded-id", owner.REFERENCE] in mutations
    else:
        with pytest.raises(RuntimeError, match="changed filesystem layers"):
            owner.acquire(
                tmp_path, scope=HostImageScope(), build=lambda: pytest.fail("cold base built")
            )
        assert not any(command[1] == "tag" for command in mutations)
    assert mutations[-1][:3] == ["docker", "image", "rm"]


@pytest.mark.parametrize("result", [False, True])
def test_owner_refuses_failed_build_or_missing_identity(tmp_path, monkeypatch, base_owner, result):
    monkeypatch.setattr(owner, "_inspect", lambda _ref: None)
    if result:
        with pytest.raises(RuntimeError, match="incomplete provenance"):
            owner.acquire(tmp_path, scope=HostImageScope(), build=lambda: result)
    else:
        assert owner.acquire(tmp_path, scope=HostImageScope(), build=lambda: result) is None


def test_owner_requires_explicit_host_scope(tmp_path):
    with pytest.raises(TypeError, match="HostImageScope"):
        owner.acquire(tmp_path, scope=tmp_path)


def test_owner_cli_imports_with_only_standard_library():
    root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        [sys.executable, "-S", "-m", "booley.runtime.host_base_acquisition", "--help"],
        env=dict(os.environ, PYTHONPATH=str(root / "src")),
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "--refresh" in result.stdout
