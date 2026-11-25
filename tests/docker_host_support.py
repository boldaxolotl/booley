"""A fake of the Docker CLI boundary's host memory probe, for Sandbox specs with ``[sandbox].memory``."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from booley.runtime import project_image as pi

_GIB = 1024**3


def fake_docker_host(
    monkeypatch: pytest.MonkeyPatch, *, memory_gib: int = 32, swap_gib: int = 16
) -> list[str]:
    """Answer the Docker-host memory probe without Docker; return the images it was asked."""
    images: list[str] = []

    def probe(image: str) -> SimpleNamespace:
        images.append(image)
        return SimpleNamespace(memory_bytes=memory_gib * _GIB, swap_bytes=swap_gib * _GIB)

    monkeypatch.setattr(pi, "probe_docker_host_memory", probe, raising=False)
    return images
