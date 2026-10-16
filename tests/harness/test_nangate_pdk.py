"""Pinned, non-redistributed Nangate45 setup cache tests."""

from __future__ import annotations

import hashlib
import io
import json
import os
import stat
from pathlib import Path

import pytest

from booley.harness import nangate_pdk
from booley.runtime.paths import docker_data_dir


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


@pytest.mark.skipif(os.name == "nt", reason="Windows mode bits do not describe ACLs")
def test_fetch_secures_shared_config_root_for_authority_store(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setattr(nangate_pdk, "FILES", ())
    config_root = tmp_path / "booley"
    config_root.mkdir(mode=0o755)

    nangate_pdk.fetch(nangate_pdk.cache_root())

    assert stat.S_IMODE(config_root.stat().st_mode) == 0o700


def test_default_cache_rejects_symlinked_config_root(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    redirected = tmp_path / "redirected"
    redirected.mkdir()
    (tmp_path / "booley").symlink_to(redirected, target_is_directory=True)

    with pytest.raises(nangate_pdk.NangatePdkError, match="must not be a symlink"):
        nangate_pdk.secure_config_dir_for_cache(nangate_pdk.cache_root())


@pytest.mark.skipif(os.name == "nt", reason="Windows ownership is ACL-based")
def test_default_cache_rejects_config_root_owned_by_another_user(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    config_root = tmp_path / "booley"
    config_root.mkdir(mode=0o700)
    monkeypatch.setattr(nangate_pdk.os, "getuid", lambda: config_root.stat().st_uid + 1)

    with pytest.raises(nangate_pdk.NangatePdkError, match="is not user-owned"):
        nangate_pdk.secure_config_dir_for_cache(nangate_pdk.cache_root())


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


@pytest.mark.parametrize("failure", ["member", "license", "parent"])
def test_cache_inspection_keeps_access_failures_separate(tmp_path, monkeypatch, failure):
    member = _item("member", "member", b"ok")
    monkeypatch.setattr(nangate_pdk, "FILES", (member,))
    (tmp_path / "member").write_bytes(b"ok")
    license_path = tmp_path / nangate_pdk.LICENSE_FILENAME
    license_path.write_bytes(nangate_pdk._license_bytes())
    original_open, original_stat = Path.open, Path.stat
    target = license_path if failure == "license" else tmp_path / "member"

    def denied_open(path, *args, **kwargs):
        if path == target and failure != "parent":
            raise PermissionError("injected read denial")
        return original_open(path, *args, **kwargs)

    def denied_stat(path, *args, **kwargs):
        if path.parent == tmp_path and failure == "parent":
            raise PermissionError("injected parent search denial")
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", denied_open)
    monkeypatch.setattr(Path, "stat", denied_stat)
    result = nangate_pdk.inspect_cache(tmp_path)
    assert result.errors and not result.pending
    assert str(target) in result.errors[0]
    assert "restore read access" in result.errors[0]


def test_cache_inspection_missing_and_checksum_are_pending(tmp_path, monkeypatch):
    monkeypatch.setattr(nangate_pdk, "FILES", (_item("member", "member", b"right"),))
    assert nangate_pdk.inspect_cache(tmp_path).pending
    assert not nangate_pdk.inspect_cache(tmp_path).errors
    (tmp_path / "member").write_bytes(b"wrong")
    assert "checksum mismatch for member" in nangate_pdk.inspect_cache(tmp_path).pending


def test_legacy_cache_validation_keeps_read_error_text(tmp_path, monkeypatch):
    member = _item("member", "member", b"ok")
    monkeypatch.setattr(nangate_pdk, "FILES", (member,))
    path = tmp_path / "member"
    path.write_bytes(b"ok")
    (tmp_path / nangate_pdk.LICENSE_FILENAME).write_bytes(nangate_pdk._license_bytes())
    original = Path.open

    def denied(path_to_open, *args, **kwargs):
        if path_to_open == path:
            raise PermissionError("denied")
        return original(path_to_open, *args, **kwargs)

    monkeypatch.setattr(Path, "open", denied)
    assert nangate_pdk.validation_errors(tmp_path) == ("cannot read member: denied",), (
        "preserve legacy cache diagnostics"
    )
