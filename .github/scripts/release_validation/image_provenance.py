"""Validate shared provenance labels and the SBOM for an exact release image."""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path


def _docker_text(arguments: list[str], *, timeout: int = 300) -> str:
    result = subprocess.run(
        ["docker", *arguments],
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "docker command failed"
        raise RuntimeError(detail)
    return result.stdout.strip()


def _docker_json(arguments: list[str]) -> object:
    return json.loads(_docker_text(arguments))


def _inspect_labels(image: str) -> dict[str, str]:
    document = _docker_json(["image", "inspect", image])
    if not isinstance(document, list) or len(document) != 1:
        raise ValueError("Docker must return exactly one image inspection")
    row = document[0]
    if not isinstance(row, dict) or not isinstance(row.get("Config"), dict):
        raise ValueError("Docker image inspection has no Config object")
    labels = row["Config"].get("Labels")
    if not isinstance(labels, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in labels.items()
    ):
        raise ValueError("Docker image inspection has no string labels")
    return labels


def _runtime_identity(image: str) -> dict[str, int]:
    output = _docker_text(
        ["run", "--rm", "--network", "none", "--entrypoint", "sh", image, "-c", "id -u; id -g"]
    )
    values = output.splitlines()
    if len(values) != 2 or any(not value.isdigit() for value in values):
        raise ValueError(f"image returned invalid uid/gid output: {output!r}")
    return {"uid": int(values[0]), "gid": int(values[1])}


def _runtime_fingerprint(image: str) -> str:
    return _docker_text(
        [
            "run",
            "--rm",
            "--network",
            "none",
            "--entrypoint",
            "python3",
            image,
            "-I",
            "-c",
            "from booley.runtime.build_metadata import current_build_metadata; "
            "print(current_build_metadata().payload_fingerprint)",
        ]
    )


def _sbom_summary(image: str) -> dict[str, object]:
    raw = _docker_text(
        ["buildx", "imagetools", "inspect", image, "--format", "{{ json .SBOM }}"],
        timeout=600,
    )
    document = json.loads(raw)
    present = bool(document)
    if isinstance(document, dict):
        sections = sorted(str(key) for key in document)
    else:
        sections = []
    return {"present": present, "sections": sections}


def validate(
    *,
    image: str,
    candidate_sha: str,
    image_digest: str,
    expected_payload: str,
    expected_recipe: str,
    expected_parent: str,
    expected_revision: str,
) -> dict[str, object]:
    labels = _inspect_labels(image)
    expected_labels = {
        "io.booley.provenance.schema": "2",
        "io.booley.payload.fingerprint": expected_payload,
        "io.booley.build.recipe-fingerprint": expected_recipe,
        "io.booley.build.parent-artifact-kind": "registry-digest",
        "io.booley.build.parent-artifact": expected_parent,
        "io.booley.build.origin": "registry",
        "booley.build-fingerprint": expected_payload,
        "org.opencontainers.image.revision": expected_revision,
    }
    checks: list[dict[str, str]] = []
    errors: list[str] = []
    if image.rpartition("@")[2] != image_digest:
        errors.append("image reference is not bound to the expected digest")
    else:
        checks.append({"id": "provenance.exact-digest", "status": "pass"})
    for name, expected in expected_labels.items():
        if labels.get(name) != expected:
            errors.append(f"label {name} differs from the expected value")
    if not any(error.startswith("label ") for error in errors):
        checks.append({"id": "provenance.labels", "status": "pass"})
    if _runtime_fingerprint(image) != expected_payload:
        errors.append("installed runtime payload fingerprint differs")
    else:
        checks.append({"id": "provenance.runtime-payload", "status": "pass"})
    sbom = _sbom_summary(image)
    if not sbom["present"]:
        errors.append("image has no attached SBOM attestation")
    else:
        checks.append({"id": "provenance.sbom", "status": "pass"})
    return {
        "schema": 1,
        "candidate": {"sha": candidate_sha, "image_digest": image_digest},
        "identity": _runtime_identity(image),
        "checks": checks,
        "labels": {name: labels.get(name) for name in expected_labels},
        "sbom": sbom,
        "errors": errors,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", required=True)
    parser.add_argument("--candidate-sha", required=True)
    parser.add_argument("--image-digest", required=True)
    parser.add_argument("--expected-payload", required=True)
    parser.add_argument("--expected-recipe", required=True)
    parser.add_argument("--expected-parent", required=True)
    parser.add_argument("--expected-revision", required=True)
    parser.add_argument("--evidence", required=True, type=Path)
    args = parser.parse_args()
    evidence = validate(
        image=args.image,
        candidate_sha=args.candidate_sha,
        image_digest=args.image_digest,
        expected_payload=args.expected_payload,
        expected_recipe=args.expected_recipe,
        expected_parent=args.expected_parent,
        expected_revision=args.expected_revision,
    )
    args.evidence.parent.mkdir(parents=True, exist_ok=True)
    args.evidence.write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")
    for error in evidence["errors"]:
        print(f"ERROR: {error}")
    return 1 if evidence["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
