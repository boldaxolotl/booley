"""Contracts for the stable runtime-base compatibility fingerprint."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from booley.harness import docker_base_contract

_SCRIPT = Path(".github/scripts/docker_base_contract.py").resolve()
_MANIFEST = Path("src/booley/data/docker/stable-base-inputs.txt")


def _remote_image_documents(label: str | None = "contract-value") -> tuple[str, dict]:
    reference = "ghcr.io/acme/base:main"
    image_digest = f"sha256:{'b' * 64}"
    image_reference = f"ghcr.io/acme/base@{image_digest}"
    labels = {} if label is None else {"io.booley.runtime-base.contract": label}
    return reference, {
        (reference, "{{json .Manifest}}"): {
            "schemaVersion": 2,
            "mediaType": "application/vnd.oci.image.index.v1+json",
            "digest": f"sha256:{'a' * 64}",
            "manifests": [
                {
                    "mediaType": "application/vnd.oci.image.manifest.v1+json",
                    "digest": image_digest,
                    "platform": {"os": "linux", "architecture": "amd64"},
                }
            ],
        },
        (image_reference, "--raw"): {
            "schemaVersion": 2,
            "mediaType": "application/vnd.oci.image.manifest.v1+json",
            "config": {
                "mediaType": "application/vnd.oci.image.config.v1+json",
                "digest": f"sha256:{'c' * 64}",
            },
        },
        (image_reference, "{{json .Image}}"): {
            "os": "linux",
            "architecture": "amd64",
            "config": {"Labels": labels},
        },
    }


def _remote_runner(documents: dict):
    def fake_run(command, **_kwargs):
        selector = "--raw" if command[5] == "--raw" else command[6]
        payload = documents[(command[4], selector)]
        return subprocess.CompletedProcess(command, 0, json.dumps(payload), "")

    return fake_run


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


@pytest.mark.parametrize("manifest", ["../inputs.txt", "/tmp/inputs.txt"])
def test_contract_rejects_manifest_outside_repo(tmp_path: Path, manifest: str) -> None:
    with pytest.raises(ValueError, match="unsafe stable-base manifest"):
        docker_base_contract.input_paths(tmp_path, manifest)


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
    digest = "a" * 64

    def fake_run(command, **_kwargs):
        calls.append(command)
        if command[:2] == ["docker", "pull"]:
            return subprocess.CompletedProcess(command, 0, "", "")
        if "{{ json .RepoDigests }}" in command:
            return subprocess.CompletedProcess(
                command,
                0,
                f'["ghcr.io/acme/base@sha256:{digest}", "mirror/base@sha256:{"b" * 64}"]\n',
                "",
            )
        return subprocess.CompletedProcess(command, 0, "contract-value\n", "")

    monkeypatch.setattr(docker_base_contract.subprocess, "run", fake_run)

    assert (
        docker_base_contract.resolve_image_pull("ghcr.io/acme/base:main", "contract-value")
        == f"ghcr.io/acme/base@sha256:{digest}"
    )
    assert calls[0] == ["docker", "pull", "ghcr.io/acme/base:main"]


def test_remote_resolution_selects_linux_amd64_without_pulling(monkeypatch) -> None:
    calls: list[list[str]] = []
    index_digest = f"sha256:{'a' * 64}"
    image_digest = f"sha256:{'b' * 64}"
    config_digest = f"sha256:{'c' * 64}"
    attestation_digest = f"sha256:{'d' * 64}"
    reference = "ghcr.io/acme/base:main"
    immutable_image = f"ghcr.io/acme/base@{image_digest}"

    index = {
        "schemaVersion": 2,
        "mediaType": "application/vnd.oci.image.index.v1+json",
        "digest": index_digest,
        "manifests": [
            {
                "mediaType": "application/vnd.oci.image.manifest.v1+json",
                "digest": image_digest,
                "platform": {"os": "linux", "architecture": "amd64"},
            },
            {
                "mediaType": "application/vnd.oci.image.manifest.v1+json",
                "digest": attestation_digest,
                "annotations": {"vnd.docker.reference.type": "attestation-manifest"},
                "platform": {"os": "unknown", "architecture": "unknown"},
            },
        ],
    }
    manifest = {
        "schemaVersion": 2,
        "mediaType": "application/vnd.oci.image.manifest.v1+json",
        "config": {
            "mediaType": "application/vnd.oci.image.config.v1+json",
            "digest": config_digest,
        },
    }
    image = {
        "os": "linux",
        "architecture": "amd64",
        "config": {"Labels": {"io.booley.runtime-base.contract": "contract-value"}},
    }

    def fake_run(command, **_kwargs):
        calls.append(command)
        target = command[4]
        template = "--raw" if command[5] == "--raw" else command[6]
        if target == reference:
            payload = index
        elif target == immutable_image and template == "--raw":
            payload = manifest
        elif target == immutable_image and template == "{{json .Image}}":
            payload = image
        else:
            raise AssertionError(command)
        return subprocess.CompletedProcess(command, 0, json.dumps(payload), "")

    monkeypatch.setattr(docker_base_contract.subprocess, "run", fake_run)

    assert docker_base_contract.resolve_image_remote(reference, "contract-value") == (
        f"ghcr.io/acme/base@{index_digest}"
    )
    assert all(command[:2] != ["docker", "pull"] for command in calls)


def test_remote_resolution_rejects_malformed_raw_manifest(monkeypatch) -> None:
    reference, documents = _remote_image_documents()
    image_reference = next(key[0] for key in documents if key[0] != reference)
    documents[(image_reference, "--raw")] = []
    monkeypatch.setattr(docker_base_contract.subprocess, "run", _remote_runner(documents))

    with pytest.raises(ValueError, match="invalid remote raw manifest"):
        docker_base_contract.resolve_image_remote(reference, "contract-value")


def test_remote_resolution_rejects_non_image_platform_descriptor(monkeypatch) -> None:
    reference = "ghcr.io/acme/base:main"
    index = {
        "schemaVersion": 2,
        "mediaType": "application/vnd.oci.image.index.v1+json",
        "digest": f"sha256:{'a' * 64}",
        "manifests": [
            {
                "mediaType": "application/vnd.in-toto+json",
                "digest": f"sha256:{'b' * 64}",
                "platform": {"os": "linux", "architecture": "amd64"},
            }
        ],
    }

    def fake_run(command, **_kwargs):
        assert command[4] == reference
        return subprocess.CompletedProcess(command, 0, json.dumps(index), "")

    monkeypatch.setattr(docker_base_contract.subprocess, "run", fake_run)

    with pytest.raises(ValueError, match="Linux/AMD64 descriptor is not an image manifest"):
        docker_base_contract.resolve_image_remote(reference, "contract-value")


def test_remote_resolution_rejects_attestation_child_manifest(monkeypatch) -> None:
    reference = "ghcr.io/acme/base:main"
    image_digest = f"sha256:{'b' * 64}"
    image_reference = f"ghcr.io/acme/base@{image_digest}"
    documents = {
        (reference, "{{json .Manifest}}"): {
            "schemaVersion": 2,
            "mediaType": "application/vnd.oci.image.index.v1+json",
            "digest": f"sha256:{'a' * 64}",
            "manifests": [
                {
                    "mediaType": "application/vnd.oci.image.manifest.v1+json",
                    "digest": image_digest,
                    "platform": {"os": "linux", "architecture": "amd64"},
                }
            ],
        },
        (image_reference, "--raw"): {
            "schemaVersion": 2,
            "mediaType": "application/vnd.oci.image.manifest.v1+json",
            "artifactType": "application/vnd.in-toto+json",
            "annotations": {"vnd.docker.reference.type": "attestation-manifest"},
            "config": {
                "mediaType": "application/vnd.oci.image.config.v1+json",
                "digest": f"sha256:{'c' * 64}",
            },
        },
    }

    def fake_run(command, **_kwargs):
        selector = "--raw" if command[5] == "--raw" else command[6]
        payload = documents[(command[4], selector)]
        return subprocess.CompletedProcess(command, 0, json.dumps(payload), "")

    monkeypatch.setattr(docker_base_contract.subprocess, "run", fake_run)

    with pytest.raises(ValueError, match="selected manifest is an attestation"):
        docker_base_contract.resolve_image_remote(reference, "contract-value")


def test_remote_resolution_rejects_ambiguous_linux_amd64_platform(monkeypatch) -> None:
    reference, documents = _remote_image_documents()
    index = documents[(reference, "{{json .Manifest}}")]
    index["manifests"].append(
        {
            "mediaType": "application/vnd.oci.image.manifest.v1+json",
            "digest": f"sha256:{'d' * 64}",
            "platform": {"os": "linux", "architecture": "amd64"},
        }
    )
    monkeypatch.setattr(docker_base_contract.subprocess, "run", _remote_runner(documents))

    with pytest.raises(ValueError, match="expected one Linux/AMD64 image"):
        docker_base_contract.resolve_image_remote(reference, "contract-value")


@pytest.mark.parametrize("location", ["index", "descriptor", "config"])
def test_remote_resolution_rejects_malformed_digests(monkeypatch, location: str) -> None:
    reference, documents = _remote_image_documents()
    index = documents[(reference, "{{json .Manifest}}")]
    image_reference = next(key[0] for key in documents if key[0] != reference)
    manifest = documents[(image_reference, "--raw")]
    if location == "index":
        index["digest"] = "sha256:abc"
    elif location == "descriptor":
        index["manifests"][0]["digest"] = "sha256:abc"
    else:
        manifest["config"]["digest"] = "sha256:abc"
    monkeypatch.setattr(docker_base_contract.subprocess, "run", _remote_runner(documents))

    with pytest.raises(ValueError, match=r"invalid .* digest"):
        docker_base_contract.resolve_image_remote(reference, "contract-value")


@pytest.mark.parametrize("label", [None, "wrong-contract"])
def test_remote_resolution_rejects_missing_or_wrong_contract_label(
    monkeypatch, label: str | None
) -> None:
    reference, documents = _remote_image_documents(label)
    monkeypatch.setattr(docker_base_contract.subprocess, "run", _remote_runner(documents))

    with pytest.raises(ValueError, match="stable-base contract mismatch"):
        docker_base_contract.resolve_image_remote(reference, "contract-value")


@pytest.mark.parametrize("document", ["index", "manifest"])
def test_remote_resolution_rejects_non_v2_schema(monkeypatch, document: str) -> None:
    reference, documents = _remote_image_documents()
    image_reference = next(key[0] for key in documents if key[0] != reference)
    key = (reference, "{{json .Manifest}}") if document == "index" else (image_reference, "--raw")
    documents[key]["schemaVersion"] = 1
    monkeypatch.setattr(docker_base_contract.subprocess, "run", _remote_runner(documents))

    with pytest.raises(ValueError, match="requires schema version 2"):
        docker_base_contract.resolve_image_remote(reference, "contract-value")


def test_image_resolution_shadow_rejects_remote_pull_disagreement(monkeypatch) -> None:
    reference, documents = _remote_image_documents()
    pulled_digest = f"sha256:{'e' * 64}"

    def fake_run(command, **_kwargs):
        if command[:4] == ["docker", "buildx", "imagetools", "inspect"]:
            selector = "--raw" if command[5] == "--raw" else command[6]
            payload = documents[(command[4], selector)]
            return subprocess.CompletedProcess(command, 0, json.dumps(payload), "")
        if command[:2] == ["docker", "pull"]:
            return subprocess.CompletedProcess(command, 0, "", "")
        if "{{ json .RepoDigests }}" in command:
            payload = f'["ghcr.io/acme/base@{pulled_digest}"]\n'
            return subprocess.CompletedProcess(command, 0, payload, "")
        return subprocess.CompletedProcess(command, 0, "contract-value\n", "")

    monkeypatch.setattr(docker_base_contract.subprocess, "run", fake_run)

    with pytest.raises(ValueError, match="remote and pull resolvers disagree"):
        docker_base_contract.resolve_image(reference, "contract-value")


def test_image_resolution_shadow_returns_matching_immutable_digest(monkeypatch) -> None:
    reference, documents = _remote_image_documents()
    index_digest = documents[(reference, "{{json .Manifest}}")]["digest"]

    def fake_run(command, **_kwargs):
        if command[:4] == ["docker", "buildx", "imagetools", "inspect"]:
            selector = "--raw" if command[5] == "--raw" else command[6]
            payload = documents[(command[4], selector)]
            return subprocess.CompletedProcess(command, 0, json.dumps(payload), "")
        if command[:2] == ["docker", "pull"]:
            return subprocess.CompletedProcess(command, 0, "", "")
        if "{{ json .RepoDigests }}" in command:
            payload = f'["ghcr.io/acme/base@{index_digest}"]\n'
            return subprocess.CompletedProcess(command, 0, payload, "")
        return subprocess.CompletedProcess(command, 0, "contract-value\n", "")

    monkeypatch.setattr(docker_base_contract.subprocess, "run", fake_run)

    assert docker_base_contract.resolve_image(reference, "contract-value") == (
        f"ghcr.io/acme/base@{index_digest}"
    )


@pytest.mark.parametrize(
    "workflow_path",
    [
        ".github/workflows/test.yml",
        ".github/workflows/docker-publish.yml",
        ".github/workflows/docker-base-publish.yml",
    ],
)
def test_runtime_base_callers_use_shadow_resolver(workflow_path: str) -> None:
    workflow = Path(workflow_path).read_text(encoding="utf-8")
    resolver_calls = [line for line in workflow.splitlines() if "--resolve-image" in line]

    assert resolver_calls, workflow_path
    assert "--resolver shadow" in workflow, workflow_path


def test_image_resolution_rejects_malformed_digest_inventory(monkeypatch) -> None:
    def fake_run(command, **_kwargs):
        if command[:2] == ["docker", "pull"]:
            return subprocess.CompletedProcess(command, 0, "", "")
        return subprocess.CompletedProcess(command, 0, "null\n", "")

    monkeypatch.setattr(docker_base_contract.subprocess, "run", fake_run)

    with pytest.raises(ValueError, match="invalid Docker RepoDigests"):
        docker_base_contract.resolve_image_pull("ghcr.io/acme/base:main", "contract")


def test_image_resolution_rejects_malformed_matching_digest(monkeypatch) -> None:
    def fake_run(command, **_kwargs):
        if command[:2] == ["docker", "pull"]:
            return subprocess.CompletedProcess(command, 0, "", "")
        return subprocess.CompletedProcess(command, 0, '["ghcr.io/acme/base@sha256:abc"]\n', "")

    monkeypatch.setattr(docker_base_contract.subprocess, "run", fake_run)

    with pytest.raises(ValueError, match="did not resolve to an immutable digest"):
        docker_base_contract.resolve_image_pull("ghcr.io/acme/base:main", "contract")
