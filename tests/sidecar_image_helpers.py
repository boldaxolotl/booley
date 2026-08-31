"""Shared helpers for opt-in sidecar image end-to-end tests."""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest


def docker(*args: str, timeout: int = 60) -> subprocess.CompletedProcess[str]:
    """Run Docker without raising so tests can report stdout and stderr."""
    return subprocess.run(
        ["docker", *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def assert_ok(result: subprocess.CompletedProcess[str]) -> None:
    """Assert that a Docker command completed successfully."""
    assert result.returncode == 0, result.stdout + result.stderr


def candidate_image(environment_variable: str, proof_name: str) -> str:
    """Return an available opt-in image or skip the proof."""
    image = os.environ.get(environment_variable)
    if image is None:
        pytest.skip(f"set {environment_variable} to run the {proof_name}")
    if shutil.which("docker") is None:
        pytest.skip("docker is unavailable")
    if docker("image", "inspect", image).returncode != 0:
        pytest.skip(f"{image} is not built")
    return image


def assert_python_version(image: str, expected: str = "Python 3.14.7") -> None:
    """Assert the exact Python patch packaged in a sidecar image."""
    version = docker("run", "--rm", "--entrypoint", "python3", image, "--version")
    assert_ok(version)
    assert (version.stdout + version.stderr).strip() == expected
