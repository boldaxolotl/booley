"""Failure semantics for authoritative Sandbox Image provenance inputs."""

from pathlib import Path

import pytest

from booley.runtime import image_provenance


def test_missing_recipe_cannot_be_fingerprinted(tmp_path: Path) -> None:
    missing = tmp_path / "Dockerfile"

    with pytest.raises(image_provenance.ImageProvenanceError, match="Dockerfile"):
        image_provenance.resolve_recipe_fingerprint((missing,))


@pytest.mark.parametrize("method", ["read_bytes", "stat"])
def test_unreadable_or_uninspectable_context_entry_cannot_be_fingerprinted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, method: str
) -> None:
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text("FROM scratch\n", encoding="utf-8")
    original = getattr(Path, method)

    def fail(path: Path, *args, **kwargs):
        if path == dockerfile:
            raise PermissionError("denied")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, method, fail)

    with pytest.raises(image_provenance.ImageProvenanceError, match="Dockerfile"):
        image_provenance.resolve_build_context_fingerprint(tmp_path)
