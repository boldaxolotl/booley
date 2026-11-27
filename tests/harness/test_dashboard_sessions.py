"""The readable form of a session key, as a Job's owner shows it."""

from __future__ import annotations

import pytest


@pytest.mark.parametrize(
    ("key", "label"),
    [
        ("pid:pid:[4026532225]:4865:81331", "PID 4865 · ns 4026532225"),
        ("pid:fixture-scope:4865:81331", "PID 4865 · ns fixture-scope"),
        ("pid:ps:darwin:12:99", "PID 12"),
        ("codex:00", "codex:00"),
        ("pid:one", "pid:one"),
    ],
)
def test_session_key_label(key, label):
    """Process keys read as PID (and namespace); every other key is shown unchanged."""
    from booley.harness.dashboard import sessions

    assert sessions.session_key_label(key) == label
