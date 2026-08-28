"""Failure semantics for authoritative Session Image provenance inputs."""

from pathlib import Path

import pytest

from booley.runtime import image_provenance


def test_missing_recipe_cannot_be_fingerprinted(tmp_path: Path) -> None:
    missing = tmp_path / "Dockerfile"

    with pytest.raises(image_provenance.ImageProvenanceError, match="Dockerfile"):
        image_provenance.resolve_recipe_fingerprint((missing,))


def test_unreadable_build_context_cannot_be_fingerprinted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text("FROM scratch\n", encoding="utf-8")
    original = Path.read_bytes

    def fail_read(path: Path) -> bytes:
        if path == dockerfile:
            raise PermissionError("denied")
        return original(path)

    monkeypatch.setattr(Path, "read_bytes", fail_read)

    with pytest.raises(image_provenance.ImageProvenanceError, match="Dockerfile"):
        image_provenance.resolve_build_context_fingerprint(tmp_path)


def test_uninspectable_context_entry_cannot_be_ignored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text("FROM scratch\n", encoding="utf-8")
    original = Path.stat

    def fail_stat(path: Path, *args, **kwargs):
        if path == dockerfile:
            raise PermissionError("denied")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", fail_stat)

    with pytest.raises(image_provenance.ImageProvenanceError, match="Dockerfile"):
        image_provenance.resolve_build_context_fingerprint(tmp_path)
