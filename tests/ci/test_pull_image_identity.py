from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[2] / ".github/scripts/pull_image_identity.py"
SPEC = importlib.util.spec_from_file_location("pull_image_identity", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
pull_image_identity = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = pull_image_identity
SPEC.loader.exec_module(pull_image_identity)

_DIGEST = "sha256:" + "a" * 64
_OTHER_DIGEST = "sha256:" + "b" * 64


def _inspect(*digests: str) -> list[dict[str, object]]:
    return [{"Id": _OTHER_DIGEST, "RepoDigests": list(digests)}]


@pytest.mark.parametrize(
    ("reference", "expected"),
    [
        ("ghcr.io/example/image:latest", "ghcr.io/example/image"),
        (f"ghcr.io/example/image@{_DIGEST}", "ghcr.io/example/image"),
        ("localhost:5000/example/image:tag", "localhost:5000/example/image"),
        ("example/image", "example/image"),
    ],
)
def test_repository_removes_only_tag_or_digest(reference: str, expected: str) -> None:
    assert pull_image_identity.repository(reference) == expected


def test_image_identity_selects_exact_repository_digest() -> None:
    requested = "ghcr.io/example/image:latest"
    resolved = f"ghcr.io/example/image@{_DIGEST}"

    identity = pull_image_identity.image_identity(
        requested,
        _inspect(f"ghcr.io/other/image@{_OTHER_DIGEST}", resolved),
    )

    assert identity.requested_reference == requested
    assert identity.resolved_reference == resolved
    assert identity.image_id == _OTHER_DIGEST
    assert identity.repository_digests == (
        resolved,
        f"ghcr.io/other/image@{_OTHER_DIGEST}",
    )


@pytest.mark.parametrize(
    "document",
    [
        {},
        [],
        [{"Id": _OTHER_DIGEST, "RepoDigests": []}],
        [{"Id": "not-a-digest", "RepoDigests": [f"ghcr.io/example/image@{_DIGEST}"]}],
        [
            {
                "Id": _OTHER_DIGEST,
                "RepoDigests": [
                    f"ghcr.io/example/image@{_DIGEST}",
                    f"ghcr.io/example/image@{_OTHER_DIGEST}",
                ],
            }
        ],
    ],
)
def test_image_identity_rejects_missing_or_ambiguous_identity(document: object) -> None:
    with pytest.raises(ValueError):
        pull_image_identity.image_identity("ghcr.io/example/image:latest", document)


def test_pull_image_identity_pulls_before_inspecting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []
    resolved = f"ghcr.io/example/image@{_DIGEST}"

    def run(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        stdout = json.dumps(_inspect(resolved)) if argv[1:3] == ["image", "inspect"] else ""
        return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(pull_image_identity.subprocess, "run", run)

    identity = pull_image_identity.pull_image_identity("ghcr.io/example/image:latest")

    assert identity.resolved_reference == resolved
    assert calls == [
        ["docker", "pull", "ghcr.io/example/image:latest"],
        ["docker", "image", "inspect", "ghcr.io/example/image:latest"],
    ]


def test_main_writes_machine_readable_evidence_and_github_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    requested = "ghcr.io/example/image:latest"
    resolved = f"ghcr.io/example/image@{_DIGEST}"
    identity = pull_image_identity.ImageIdentity(
        requested,
        resolved,
        _OTHER_DIGEST,
        (resolved,),
    )
    evidence = tmp_path / "evidence" / "image-identity.json"
    github_output = tmp_path / "github-output.txt"
    monkeypatch.setattr(pull_image_identity, "pull_image_identity", lambda _image: identity)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(SCRIPT),
            "--image",
            requested,
            "--evidence",
            str(evidence),
            "--github-output",
            str(github_output),
        ],
    )

    assert pull_image_identity.main() == 0

    assert json.loads(evidence.read_text(encoding="utf-8")) == {
        "image_id": _OTHER_DIGEST,
        "repository_digests": [resolved],
        "requested_reference": requested,
        "resolved_reference": resolved,
    }
    assert github_output.read_text(encoding="utf-8") == f"image={resolved}\n"
