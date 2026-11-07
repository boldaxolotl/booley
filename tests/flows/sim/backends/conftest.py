"""Shared fixtures for sim unit tests."""

from __future__ import annotations

import threading

import pytest


class _StubTimer:
    """``threading.Timer`` stand-in that never fires, or fires on ``start()`` when *fire*."""

    fire = False

    def __init__(self, _interval, callback, *_args, **_kwargs):
        self.callback, self.daemon = callback, False

    def start(self):
        if self.fire:
            self.callback()

    def cancel(self):
        pass


@pytest.fixture
def never_firing_timer(monkeypatch):
    """Only a per-line deadline check can end a run."""
    monkeypatch.setattr(threading, "Timer", _StubTimer)


@pytest.fixture
def instant_timer(monkeypatch):
    """The wall-clock deadline has always already passed."""
    monkeypatch.setattr(threading, "Timer", type("_Instant", (_StubTimer,), {"fire": True}))


@pytest.fixture(autouse=True)
def _set_booley_dir(tmp_path, monkeypatch):
    """Prevent resolve_booley_dir() from failing in sim tests."""
    from booley.runtime.booley_dir import reset_cache

    reset_cache()
    proj_dir = tmp_path / ".booley"
    proj_dir.mkdir()
    (proj_dir / "booley.toml").write_text('[flows.sim]\nsandbox = "sandboxed"\n', encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BOOLEY_DIR", str(proj_dir))
