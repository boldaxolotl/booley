"""Candidate image identity and Goal round-trip evidence without subprocesses."""

from __future__ import annotations

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


def test_demo_surface_records_candidate_and_requires_finished_goal(tmp_path, monkeypatch) -> None:
    commands = []
    captured = {}

    def run(command, **kwargs):
        commands.append((command, kwargs))
        return "1.2.3"

    def driver(**kwargs):
        captured.update(kwargs)
        return _proof()

    monkeypatch.setattr(demo_surface, "_run", run)
    monkeypatch.setattr(demo_surface.goal_mode_driver, "validate", driver)
    result = _validate(tmp_path)
    assert captured["goals"][0].family.value == "lint"
    assert captured["goals"][0].target == "lint_core"
    assert captured["python"] == Path(sys.executable)
    assert commands[-1][0][-1] == "booley.runtime.incontainer_register"
    assert result["candidate"] == {"sha": "candidate", "image_digest": "sha256:image"}
    assert result["checks"][-1] == {"id": "demo.goal-finish", "status": "pass"}


@pytest.mark.parametrize("state", ["active", None])
def test_demo_surface_refuses_unfinished_driver_result(tmp_path, monkeypatch, state) -> None:
    monkeypatch.setattr(demo_surface, "_run", lambda *_args, **_kwargs: "1.2.3")
    monkeypatch.setattr(
        demo_surface.goal_mode_driver, "validate", lambda **_kwargs: {"state": state}
    )
    with pytest.raises(RuntimeError, match="did not finish"):
        _validate(tmp_path)


def test_demo_surface_rejects_wrong_installed_version_before_driver(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(demo_surface, "_run", lambda *_args, **_kwargs: "wrong")
    monkeypatch.setattr(
        demo_surface.goal_mode_driver, "validate", lambda **_kwargs: pytest.fail("driver ran")
    )
    with pytest.raises(RuntimeError, match="image version differs"):
        _validate(tmp_path)


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
    assert evidence.is_file()


def _proof():
    return {
        "state": "finished",
        "goals": {"lint_clean_lint_core": "met"},
        "steps": {
            name: {"status": "pass", "response": f"observed {name}"}
            for name in ("goal_enter", "lint", "goal_status", "goal_finish")
        },
    }


@pytest.mark.parametrize("step", ["goal_enter", "lint", "goal_status", "goal_finish"])
def test_finished_state_without_each_step_proof_is_not_a_passing_surface(
    tmp_path, monkeypatch, step
):
    result = _proof()
    result["steps"].pop(step)
    monkeypatch.setattr(demo_surface, "_run", lambda *_args, **_kwargs: "1.2.3")
    monkeypatch.setattr(demo_surface.goal_mode_driver, "validate", lambda **_kwargs: result)
    with pytest.raises(ValueError, match=step):
        _validate(tmp_path)
