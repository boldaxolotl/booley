"""Narrowing the T3 Code policy of a Sandbox Image to a Project's Agent CLIs."""

from __future__ import annotations

from booley.runtime.t3code_policy import narrowed_policy


def test_narrowing_never_adds_a_driver_the_image_does_not_allow() -> None:
    base = b'{"limits": {"providers": {"allowedDrivers": ["codex"]}}}'

    assert narrowed_policy(base, ["claude", "codex"]) == base
