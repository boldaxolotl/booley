"""Tests for ``[sandbox]`` parsing in booley.toml."""

from __future__ import annotations

from pathlib import Path

import pytest

from booley.config.agent import load_agent_settings, set_agent_settings


@pytest.mark.parametrize(
    ("toml", "memory", "swap", "mount", "warning"),
    [
        ("", "", "", False, None),
        ("mount_host_skills = true", "", "", True, None),
        ('memory = "8g"\nswap = "2g"', "8g", "2g", False, None),
        ('memory = {default = "6g", agent = "8g"}', "6g", "", False, "memory tier table"),
        ('memory_tiers = {sim = "4g"}', "", "", False, "memory_tiers is retired"),
        ('memory = "8g"\nswap = 0', "8g", "", False, "Invalid [sandbox] swap 0"),
        ('image = ""', "", "", False, "Invalid sandbox image"),
        ("image = 123", "", "", False, "Invalid sandbox image"),
        ('mode = "docker"', "", "", False, "[sandbox].mode is retired"),
    ],
    ids=[
        "defaults",
        "skills",
        "memory",
        "tier-table",
        "tiers-key",
        "bad-swap",
        "empty-image",
        "numeric-image",
        "retired-mode",
    ],
)
def test_sandbox_settings_from_booley_toml(
    tmp_path: Path, caplog, toml: str, memory: str, swap: str, mount: bool, warning: str | None
) -> None:
    (tmp_path / ".booley").mkdir()
    (tmp_path / ".booley" / "booley.toml").write_text(f"[sandbox]\n{toml}\n", encoding="utf-8")
    try:
        with caplog.at_level("WARNING"):
            sandbox = load_agent_settings(tmp_path).sandbox
    finally:
        set_agent_settings(None)  # load_agent_settings installs process-wide settings

    assert (sandbox.memory, sandbox.swap, sandbox.mount_host_skills) == (memory, swap, mount)
    assert (warning is None) == (not caplog.records)
    assert warning is None or any(warning in r.getMessage() for r in caplog.records)
