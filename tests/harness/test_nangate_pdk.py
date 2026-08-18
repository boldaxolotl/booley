"""Pinned, non-redistributed Nangate45 setup cache tests."""

from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path

import pytest

from booley.harness import nangate_pdk
from booley.paths import docker_data_dir


def _item(source: str, destination: str, payload: bytes) -> nangate_pdk.PinnedFile:
    return nangate_pdk.PinnedFile(source, destination, hashlib.sha256(payload).hexdigest())


def test_cache_root_is_versioned_user_config(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))

    root = nangate_pdk.cache_root()

    assert root.parent == tmp_path / "booley" / "pdk"
    assert nangate_pdk.REVISION[:12] in root.name


def test_fetch_verifies_and_installs_complete_cache(tmp_path: Path, monkeypatch) -> None:
    payloads = {"a.lib": b"liberty\n", "b.lef": b"lef\n"}
    files = (
        _item("upstream/a.lib", "cell/lib/a.lib", payloads["a.lib"]),
        _item("upstream/b.lef", "nangate45/b.lef", payloads["b.lef"]),
    )
    monkeypatch.setattr(nangate_pdk, "FILES", files)
    requested: list[str] = []

    def opener(request, _timeout):
        requested.append(request.full_url)
        return io.BytesIO(payloads[Path(request.full_url).name])

    root = nangate_pdk.fetch(tmp_path / "cache", opener=opener)

    assert nangate_pdk.is_ready(root)
    assert (root / "cell/lib/a.lib").read_bytes() == payloads["a.lib"]
    assert (root / nangate_pdk.LICENSE_FILENAME).is_file()
    manifest = json.loads((root / nangate_pdk.SOURCE_MANIFEST).read_text(encoding="utf-8"))
    assert manifest["revision"] == nangate_pdk.REVISION
    assert manifest["license"] == nangate_pdk.LICENSE_ID
    assert requested == [item.url for item in files]


def test_checksum_failure_never_installs_download(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        nangate_pdk,
        "FILES",
        (nangate_pdk.PinnedFile("bad.lib", "cell/lib/bad.lib", "0" * 64),),
    )

    with pytest.raises(nangate_pdk.NangatePdkError, match="checksum mismatch"):
        nangate_pdk.fetch(tmp_path / "cache", opener=lambda *_: io.BytesIO(b"wrong"))

    assert not (tmp_path / "cache" / "cell/lib/bad.lib").exists()


def test_validation_reports_missing_and_changed_members(tmp_path: Path, monkeypatch) -> None:
    item = _item("a", "cell/lib/a.lib", b"right")
    monkeypatch.setattr(nangate_pdk, "FILES", (item,))
    root = tmp_path / "cache"

    assert nangate_pdk.validation_errors(root) == (
        "missing cell/lib/a.lib",
        f"missing or changed {nangate_pdk.LICENSE_FILENAME}",
    )

    path = root / item.destination
    path.parent.mkdir(parents=True)
    path.write_bytes(b"wrong")
    assert "checksum mismatch for cell/lib/a.lib" in nangate_pdk.validation_errors(root)


def test_sandbox_sources_do_not_redistribute_nangate_files() -> None:
    docker_root = docker_data_dir()
    dockerfile = (docker_root / "Dockerfile").read_text(encoding="utf-8")

    assert "COPY src/booley/data/docker/pdk" not in dockerfile
    pdk_root = docker_root / "pdk"
    assert not pdk_root.exists() or not any(path.is_file() for path in pdk_root.rglob("*"))
