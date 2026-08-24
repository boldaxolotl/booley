"""Contracts for the stable runtime-base compatibility fingerprint."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from booley.harness import docker_base_contract

_SCRIPT = Path(".github/scripts/docker_base_contract.py").resolve()
_MANIFEST = Path("src/booley/data/docker/stable-base-inputs.txt")


def _fingerprint(repo: Path) -> str:
    result = subprocess.run(
        [sys.executable, str(_SCRIPT), "--repo", str(repo), "--manifest", "inputs.txt"],
        capture_output=True,
        check=True,
        text=True,
    )
    return result.stdout.strip()


def test_contract_changes_with_content_but_not_mtime(tmp_path: Path) -> None:
    (tmp_path / "inputs.txt").write_text("a.txt\nb.txt\n", encoding="utf-8")
    (tmp_path / "a.txt").write_text("alpha\n", encoding="utf-8")
    (tmp_path / "b.txt").write_text("beta\n", encoding="utf-8")

    first = _fingerprint(tmp_path)
    (tmp_path / "a.txt").touch()
    assert _fingerprint(tmp_path) == first
    (tmp_path / "a.txt").write_text("changed\n", encoding="utf-8")
    assert _fingerprint(tmp_path) != first


def test_contract_rejects_missing_or_unsafe_inputs(tmp_path: Path) -> None:
    (tmp_path / "inputs.txt").write_text("../outside\n", encoding="utf-8")
    result = subprocess.run(
        [
            sys.executable,
            str(_SCRIPT),
            "--repo",
            str(tmp_path),
            "--manifest",
            "inputs.txt",
        ],
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode == 2
    assert "unsafe stable-base input" in result.stderr


def test_contract_rejects_symlink_escape(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside-stable-input"
    outside.write_text("secret\n", encoding="utf-8")
    (tmp_path / "inputs.txt").write_text("linked.txt\n", encoding="utf-8")
    (tmp_path / "linked.txt").symlink_to(outside)

    result = subprocess.run(
        [
            sys.executable,
            str(_SCRIPT),
            "--repo",
            str(tmp_path),
            "--manifest",
            "inputs.txt",
        ],
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode == 2
    assert "unsafe stable-base input" in result.stderr


def test_manifest_inputs_trigger_classifier_and_publisher() -> None:
    inputs = [
        line
        for line in _MANIFEST.read_text(encoding="utf-8").splitlines()
        if line and not line.startswith("#")
    ]
    workflow = Path(".github/workflows/docker-base-publish.yml").read_text(encoding="utf-8")

    for path in inputs:
        assert f"      - {path}\n" in workflow, path


def test_image_resolution_selects_repository_digest_and_verifies_contract(monkeypatch) -> None:
    calls: list[list[str]] = []

    def fake_run(command, **_kwargs):
        calls.append(command)
        if command[:2] == ["docker", "pull"]:
            return subprocess.CompletedProcess(command, 0, "", "")
        if "{{ json .RepoDigests }}" in command:
            return subprocess.CompletedProcess(
                command,
                0,
                '["ghcr.io/acme/base@sha256:abc", "mirror/base@sha256:def"]\n',
                "",
            )
        return subprocess.CompletedProcess(command, 0, "contract-value\n", "")

    monkeypatch.setattr(docker_base_contract.subprocess, "run", fake_run)

    assert (
        docker_base_contract.resolve_image("ghcr.io/acme/base:main", "contract-value")
        == "ghcr.io/acme/base@sha256:abc"
    )
    assert calls[0] == ["docker", "pull", "ghcr.io/acme/base:main"]


def test_image_resolution_rejects_malformed_digest_inventory(monkeypatch) -> None:
    def fake_run(command, **_kwargs):
        if command[:2] == ["docker", "pull"]:
            return subprocess.CompletedProcess(command, 0, "", "")
        return subprocess.CompletedProcess(command, 0, "null\n", "")

    monkeypatch.setattr(docker_base_contract.subprocess, "run", fake_run)

    with pytest.raises(ValueError, match="invalid Docker RepoDigests"):
        docker_base_contract.resolve_image("ghcr.io/acme/base:main", "contract")
