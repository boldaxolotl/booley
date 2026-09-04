from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).parents[2]
sys.path.insert(0, str(ROOT / ".github/scripts"))

from release_validation import image_provenance


def _labels() -> dict[str, str]:
    return {
        "io.booley.provenance.schema": "2",
        "io.booley.payload.fingerprint": "payload",
        "io.booley.build.recipe-fingerprint": "recipe",
        "io.booley.build.parent-artifact-kind": "registry-digest",
        "io.booley.build.parent-artifact": "registry/base@sha256:parent",
        "io.booley.build.origin": "registry",
        "booley.build-fingerprint": "payload",
        "org.opencontainers.image.revision": "revision",
    }


def test_validate_requires_labels_runtime_payload_and_sbom(monkeypatch) -> None:
    monkeypatch.setattr(image_provenance, "_inspect_labels", lambda _image: _labels())
    monkeypatch.setattr(image_provenance, "_runtime_fingerprint", lambda _image: "payload")
    monkeypatch.setattr(
        image_provenance, "_runtime_identity", lambda _image: {"uid": 1000, "gid": 1000}
    )
    monkeypatch.setattr(
        image_provenance,
        "_sbom_summary",
        lambda _image: {"present": True, "sections": ["SPDX"]},
    )

    evidence = image_provenance.validate(
        image="registry/image@sha256:candidate",
        candidate_sha="candidate-sha",
        image_digest="sha256:candidate",
        expected_payload="payload",
        expected_recipe="recipe",
        expected_parent="registry/base@sha256:parent",
        expected_revision="revision",
    )

    assert evidence["errors"] == []
    assert evidence["candidate"] == {
        "sha": "candidate-sha",
        "image_digest": "sha256:candidate",
    }
    assert evidence["identity"] == {"uid": 1000, "gid": 1000}
    assert {check["id"] for check in evidence["checks"]} == {
        "provenance.exact-digest",
        "provenance.labels",
        "provenance.runtime-payload",
        "provenance.sbom",
    }


def test_validate_reports_missing_shared_contracts(monkeypatch) -> None:
    labels = _labels()
    labels.pop("io.booley.build.origin")
    monkeypatch.setattr(image_provenance, "_inspect_labels", lambda _image: labels)
    monkeypatch.setattr(image_provenance, "_runtime_fingerprint", lambda _image: "wrong")
    monkeypatch.setattr(
        image_provenance, "_runtime_identity", lambda _image: {"uid": 1000, "gid": 1000}
    )
    monkeypatch.setattr(
        image_provenance,
        "_sbom_summary",
        lambda _image: {"present": False, "sections": []},
    )

    evidence = image_provenance.validate(
        image="registry/image:mutable",
        candidate_sha="candidate-sha",
        image_digest="sha256:candidate",
        expected_payload="payload",
        expected_recipe="recipe",
        expected_parent="registry/base@sha256:parent",
        expected_revision="revision",
    )

    assert "image reference is not bound to the expected digest" in evidence["errors"]
    assert "label io.booley.build.origin differs from the expected value" in evidence["errors"]
    assert "installed runtime payload fingerprint differs" in evidence["errors"]
    assert "image has no attached SBOM attestation" in evidence["errors"]
