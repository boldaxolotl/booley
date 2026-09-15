"""Migrated host diagnostic contracts exercised through owning interfaces."""

from __future__ import annotations

import shutil
import subprocess

from tests.diagnostic_helpers import (
    _Rec,
    _record_container,
    _record_environment,
)
from tests.harness.test_doctor import _set_venue

from booley.audit import (
    host_environment,
)
from booley.harness import doctor
from booley.runtime import (
    runtime_context,
)


class TestCheckDocker:
    """QA-3: the container-runtime check must not FAIL inside the Session Runtime.

    In-container there is no nested container runtime and Booley Flows run
    directly, so a missing runtime is expected — SKIP, don't FAIL.
    """

    def test_skips_in_container(self, monkeypatch):
        _set_venue(monkeypatch, True)
        # Even if no runtime is on PATH, in-container it must not FAIL.
        monkeypatch.setattr(shutil, "which", lambda name: None)
        rec = _Rec()
        result = _record_container(
            host_environment.probe_container_runtime(
                "docker",
                inside_session_runtime=runtime_context.inside_session_runtime(),
                which=shutil.which,
                run=subprocess.run,
            ),
            passed=rec.p,
            skipped=rec.s,
            failed=rec.f,
        )
        assert result is None
        assert rec.kinds() == {"skip"}
        assert not rec.fails()

    def test_fails_on_host_without_runtime(self, monkeypatch):
        _set_venue(monkeypatch, False)
        monkeypatch.setattr(shutil, "which", lambda name: None)
        rec = _Rec()
        result = _record_container(
            host_environment.probe_container_runtime(
                "docker",
                inside_session_runtime=runtime_context.inside_session_runtime(),
                which=shutil.which,
                run=subprocess.run,
            ),
            passed=rec.p,
            skipped=rec.s,
            failed=rec.f,
        )
        assert result is None
        assert "container runtime not on PATH" in rec.fails()

    def test_passes_on_host_with_running_runtime(self, monkeypatch):
        _set_venue(monkeypatch, False)
        runtime = "doc" + "ker"
        monkeypatch.setattr(
            shutil,
            "which",
            lambda name: runtime if name == doctor._CONTAINER_CLI else None,
        )
        monkeypatch.setattr(
            subprocess,
            "run",
            lambda *a, **k: subprocess.CompletedProcess(a[0], 0, stdout="", stderr=""),
        )
        rec = _Rec()
        result = _record_container(
            host_environment.probe_container_runtime(
                "docker",
                inside_session_runtime=runtime_context.inside_session_runtime(),
                which=shutil.which,
                run=subprocess.run,
            ),
            passed=rec.p,
            skipped=rec.s,
            failed=rec.f,
        )
        assert result == runtime
        assert rec.kinds() == {"pass"}


class TestHostClockCheck:
    """F-5: doctor warns when the host clock is skewed from an HTTP Date header."""

    @staticmethod
    def _run_check(monkeypatch, *, offset_s=None, unreachable=False):
        import email.utils
        import urllib.error
        from datetime import timedelta

        class _Headers:
            def __init__(self, date_str):
                self._date = date_str

            def get(self, _key, default=""):
                return self._date or default

        class _Resp:
            def __init__(self, date_str):
                self.headers = _Headers(date_str)

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def fake_urlopen(req, timeout=0):
            if unreachable:
                raise urllib.error.URLError("offline")
            remote = doctor.datetime.now(doctor.UTC) - timedelta(seconds=offset_s)
            return _Resp(email.utils.format_datetime(remote))

        monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
        passes, warns, skips = [], [], []
        _record_environment(
            host_environment.probe_host_clock(),
            passed=passes.append,
            warned=warns.append,
            skipped=skips.append,
        )
        return passes, warns, skips

    def test_aligned_clock_passes(self, monkeypatch):
        passes, warns, skips = self._run_check(monkeypatch, offset_s=0)
        assert passes and not warns and not skips

    def test_skewed_clock_warns_with_fix_hint(self, monkeypatch):
        # Host 4h ahead of the reference (i.e. reference is 4h in the past
        # relative to local now -> skew positive -> "ahead of").
        passes, warns, _skips = self._run_check(monkeypatch, offset_s=4 * 3600)
        assert warns and not passes
        assert "ahead of" in warns[0]
        assert "w32tm /resync" in warns[0]

    def test_clock_behind_warns_behind(self, monkeypatch):
        passes, warns, _skips = self._run_check(monkeypatch, offset_s=-4 * 3600)
        assert warns and not passes
        assert "behind" in warns[0]

    def test_offline_skips(self, monkeypatch):
        passes, warns, skips = self._run_check(monkeypatch, unreachable=True)
        assert skips and not passes and not warns
