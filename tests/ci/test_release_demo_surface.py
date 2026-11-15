"""Candidate image identity and Project registration evidence without subprocesses."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]
sys.path.insert(0, str(ROOT / ".github/scripts"))

from release_validation import demo_surface

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="validates a POSIX release container with uid/gid"
)


def _validate(root: Path) -> dict[str, object]:
    return demo_surface.validate(
        project=root,
        project_state=root / "state",
        expected_version="1.2.3",
        python=Path(sys.executable),
        candidate_sha="candidate",
        image_digest="sha256:image",
    )


def test_demo_surface_records_candidate_version_and_registration(tmp_path, monkeypatch) -> None:
    commands = []

    def run(command, **kwargs):
        commands.append((command, kwargs))
        return "1.2.3"

    monkeypatch.setattr(demo_surface, "_run", run)
    result = _validate(tmp_path)
    assert commands[-1][0][-1] == "booley.harness.incontainer_register"
    assert result["candidate"] == {"sha": "candidate", "image_digest": "sha256:image"}
    assert result["checks"] == [
        {"id": "demo.version", "status": "pass"},
        {"id": "demo.registration", "status": "pass"},
    ]


def test_demo_surface_rejects_wrong_installed_version_before_registration(
    tmp_path, monkeypatch
) -> None:
    commands = []

    def run(command, **_kwargs):
        commands.append(command)
        return "wrong"

    monkeypatch.setattr(demo_surface, "_run", run)
    with pytest.raises(RuntimeError, match="image version differs"):
        _validate(tmp_path)
    assert len(commands) == 1


def test_demo_surface_main_defaults_to_running_python(tmp_path, monkeypatch) -> None:
    captured = {}

    def validate(**kwargs):
        captured.update(kwargs)
        return {"schema": 1}

    monkeypatch.setattr(demo_surface, "validate", validate)
    evidence = tmp_path / "result.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "demo_surface.py",
            "--project",
            str(tmp_path),
            "--project-state",
            str(tmp_path),
            "--expected-version",
            "1.2.3",
            "--image-digest",
            "sha256:image",
            "--evidence",
            str(evidence),
        ],
    )
    assert demo_surface.main() == 0
    assert captured["python"] == Path(sys.executable)
    assert json.loads(evidence.read_text()) == {"schema": 1}
