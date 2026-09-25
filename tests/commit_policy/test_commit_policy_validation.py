"""Behavior and identity contracts for the shared commit-policy owner."""

from pathlib import Path

import pytest

from booley.commit_policy import StealthPolicy, policy, validate_message, validation
from booley.dev_support import commit_msg_utils, validate_commit_msg
from booley.harness.setup import workspace
from booley.runtime import git as runtime_git
from booley.specialists import specialist


def _configured_project(tmp_path: Path, stealth: str) -> Path:
    project_dir = tmp_path / ".booley_project"
    project_dir.mkdir()
    (project_dir / "booley.toml").write_text(f"[stealth]\n{stealth}", encoding="utf-8")
    return tmp_path


def test_packaged_adapters_and_callers_bind_canonical_owner_objects() -> None:
    assert commit_msg_utils.StealthPolicy is StealthPolicy is policy.StealthPolicy
    assert validate_commit_msg.ALLOWED_TYPES is validation.ALLOWED_TYPES
    assert validate_commit_msg.validate_message is validate_message
    assert runtime_git.validate_message is validate_message
    assert workspace.validate_message is validate_message
    assert specialist.validate_message is validate_message


@pytest.mark.parametrize(
    ("message", "error_fragment"),
    [
        ("fix(core): clean summary\n", None),
        ("bad message\n", "doesn't match"),
        ("fix(core): clean\n\nfirst\nsecond\n", "Body is 2 line(s)"),
        ("fix(core): codex mention\n", "Banned phrase"),
        ("Merge branch 'feature'\n", None),
        ("fix(core): clean\r\n\r\nbody\r\n", None),
    ],
)
def test_validation_uses_real_project_policy(
    tmp_path: Path,
    message: str,
    error_fragment: str | None,
) -> None:
    root = _configured_project(
        tmp_path,
        "enabled = true\nenforce_convention = true\nmax_body_lines = 1\n",
    )

    errors = validate_message(message, project_root=root)

    if error_fragment is None:
        assert errors == []
    else:
        assert any(error_fragment in error for error in errors)


def test_source_checkout_exemption_overrides_config() -> None:
    source_root = Path(__file__).resolve().parents[2]

    assert policy.stealth_policy(source_root) == StealthPolicy(False, (), None, False, ())
