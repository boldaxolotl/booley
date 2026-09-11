"""Tests for the in-container VaporView WCP auto-start patcher."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from io import StringIO

import pytest

from booley.runtime import incontainer_vaporview as iv


def _vanilla_manifest() -> dict:
    """A minimal shape mirroring the vendored VaporView manifest: lazy
    activation + application-scoped WCP settings inside a single config block."""
    return {
        "publisher": "lramseyer",
        "name": "vaporview",
        "activationEvents": [],
        "contributes": {
            "commands": [
                {"command": "vaporview.wcp.start", "title": "WCP: Start Server"},
                {"command": "vaporview.wcp.status", "title": "WCP: Show Status"},
            ],
            "configuration": {
                "properties": {
                    "vaporview.wcp.enabled": {"scope": "application", "default": False},
                    "vaporview.wcp.port": {"scope": "application", "default": 54322},
                    "vaporview.other": {"scope": "window"},
                }
            },
        },
    }


_VANILLA_BUNDLE = (
    "class Documents{constructor(log){this.log=log;this.getTokenColorsForTheme()}"
    "async getTokenColorsForTheme(){try{await getUserTheme()}catch(e){"
    'V.window.showErrorMessage("Error getting user theme: "+e),'
    'this.colorPalette=["#CCCCCC"],this.themeValid=!1}'
    "onThemeChange(s){s.getTokenColorsForTheme()}}"
)


class TestPatchManifest:
    def test_adds_startup_event_and_relaxes_scope(self):
        m = _vanilla_manifest()
        assert iv.patch_manifest(m) is True
        assert m["activationEvents"] == ["onStartupFinished"]
        props = m["contributes"]["configuration"]["properties"]
        assert props["vaporview.wcp.enabled"]["scope"] == "machine"
        assert props["vaporview.wcp.port"]["scope"] == "machine"
        # Unrelated settings are left untouched.
        assert props["vaporview.other"]["scope"] == "window"
        commands = m["contributes"]["commands"]
        assert commands[0]["enablement"] == "!config.vaporview.wcp.enabled"
        assert "enablement" not in commands[1]

    def test_idempotent(self):
        m = _vanilla_manifest()
        iv.patch_manifest(m)
        # A second pass changes nothing and reports no change.
        assert iv.patch_manifest(m) is False

    def test_preserves_existing_activation_events(self):
        m = _vanilla_manifest()
        m["activationEvents"] = ["onCustomEditor:vaporview.waveformViewer"]
        assert iv.patch_manifest(m) is True
        assert m["activationEvents"] == [
            "onCustomEditor:vaporview.waveformViewer",
            "onStartupFinished",
        ]

    def test_configuration_as_list_of_blocks(self):
        # Newer manifests can express configuration as a list of blocks.
        m = _vanilla_manifest()
        m["contributes"]["configuration"] = [
            {"properties": {"vaporview.wcp.enabled": {"scope": "application"}}},
            {"properties": {"vaporview.wcp.port": {"scope": "application"}}},
        ]
        assert iv.patch_manifest(m) is True
        blocks = m["contributes"]["configuration"]
        assert blocks[0]["properties"]["vaporview.wcp.enabled"]["scope"] == "machine"
        assert blocks[1]["properties"]["vaporview.wcp.port"]["scope"] == "machine"

    def test_missing_configuration_only_patches_activation(self):
        m = {"activationEvents": []}
        assert iv.patch_manifest(m) is True
        assert m["activationEvents"] == ["onStartupFinished"]

    def test_already_machine_scoped_no_rescope(self):
        m = _vanilla_manifest()
        m["activationEvents"] = ["onStartupFinished"]
        props = m["contributes"]["configuration"]["properties"]
        props["vaporview.wcp.enabled"]["scope"] = "machine"
        props["vaporview.wcp.port"]["scope"] = "machine"
        m["contributes"]["commands"][0]["enablement"] = "!config.vaporview.wcp.enabled"
        assert iv.patch_manifest(m) is False

    def test_preserves_upstream_start_enablement(self):
        m = _vanilla_manifest()
        m["contributes"]["commands"][0]["enablement"] = "vaporview.manualMode"

        iv.patch_manifest(m)

        assert m["contributes"]["commands"][0]["enablement"] == "vaporview.manualMode"


class TestFindManifests:
    def test_globs_versioned_dirs(self, tmp_path):
        ext = tmp_path / ".vscode-server" / "extensions"
        for ver in ("1.5.4", "1.6.0"):
            d = ext / f"lramseyer.vaporview-{ver}"
            d.mkdir(parents=True)
            (d / "package.json").write_text("{}", encoding="utf-8")
        # A non-VaporView extension must not match.
        other = ext / "someone.other-1.0.0"
        other.mkdir(parents=True)
        (other / "package.json").write_text("{}", encoding="utf-8")

        found = iv.find_manifests(tmp_path)
        assert [p.parent.name for p in found] == [
            "lramseyer.vaporview-1.5.4",
            "lramseyer.vaporview-1.6.0",
        ]

    def test_absent_extension_returns_empty(self, tmp_path):
        assert iv.find_manifests(tmp_path) == []


class TestThemeFallback:
    def test_disables_remote_theme_lookups(self, tmp_path):
        extension = tmp_path / "lramseyer.vaporview-1.5.4"
        bundle = extension / "dist" / "extension.js"
        bundle.parent.mkdir(parents=True)
        bundle.write_text(_VANILLA_BUNDLE, encoding="utf-8")

        assert iv._disable_remote_theme_lookup(extension) is True
        patched = bundle.read_text(encoding="utf-8")
        # Only the now-dead method definition remains; neither call site does.
        assert patched.count("getTokenColorsForTheme()") == 1
        assert patched.count("void 0") == 2
        assert 'this.colorPalette=["#CCCCCC"]' in patched

    def test_patch_is_idempotent(self, tmp_path):
        extension = tmp_path / "lramseyer.vaporview-1.5.4"
        bundle = extension / "dist" / "extension.js"
        bundle.parent.mkdir(parents=True)
        bundle.write_text(_VANILLA_BUNDLE, encoding="utf-8")

        assert iv._disable_remote_theme_lookup(extension) is True
        assert iv._disable_remote_theme_lookup(extension) is False

    def test_unknown_bundle_shape_is_untouched(self, tmp_path):
        extension = tmp_path / "lramseyer.vaporview-2.0.0"
        bundle = extension / "dist" / "extension.js"
        bundle.parent.mkdir(parents=True)
        bundle.write_text("new upstream implementation", encoding="utf-8")

        assert iv._disable_remote_theme_lookup(extension) is False
        assert bundle.read_text(encoding="utf-8") == "new upstream implementation"


class TestMain:
    @pytest.fixture
    def home(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        # Disable the install-race wait so the absent-extension paths return at
        # once; the wait itself is exercised in TestInstallRaceWait.
        monkeypatch.setenv(iv._WAIT_ENV, "0")
        monkeypatch.setattr(iv, "_start_install_watcher", lambda home: False)
        return tmp_path

    def _install(self, home, manifest: dict, ver: str = "1.5.4", *, with_bundle=True):
        d = home / ".vscode-server" / "extensions" / f"lramseyer.vaporview-{ver}"
        d.mkdir(parents=True)
        p = d / "package.json"
        p.write_text(json.dumps(manifest), encoding="utf-8")
        if with_bundle:
            bundle = d / "dist" / "extension.js"
            bundle.parent.mkdir()
            bundle.write_text(_VANILLA_BUNDLE, encoding="utf-8")
        return p

    def test_patches_installed_manifest(self, home, capsys):
        p = self._install(home, _vanilla_manifest())
        assert iv.main() == 0
        out = json.loads(p.read_text(encoding="utf-8"))
        assert out["activationEvents"] == ["onStartupFinished"]
        assert (
            out["contributes"]["configuration"]["properties"]["vaporview.wcp.enabled"]["scope"]
            == "machine"
        )
        assert out["contributes"]["commands"][0]["enablement"] == "!config.vaporview.wcp.enabled"
        assert "patched 1" in capsys.readouterr().out

    def test_patches_theme_lookup_when_manifest_is_already_current(self, home, capsys):
        manifest = _vanilla_manifest()
        iv.patch_manifest(manifest)
        p = self._install(home, manifest, with_bundle=True)

        assert iv.main() == 0

        bundle = p.parent / "dist" / "extension.js"
        assert bundle.read_text(encoding="utf-8").count("getTokenColorsForTheme()") == 1
        assert "patched 1" in capsys.readouterr().out

    def test_absent_extension_is_a_clean_noop(self, home, capsys):
        # Install still in flight: succeed and say so, never fail the hook.
        assert iv.main() == 0
        assert "not present yet" in capsys.readouterr().out

    def test_second_run_reports_already_patched(self, home, capsys):
        self._install(home, _vanilla_manifest())
        iv.main()
        capsys.readouterr()
        assert iv.main() == 0
        assert "already patched" in capsys.readouterr().out

    def test_malformed_manifest_does_not_raise(self, home, capsys):
        d = home / ".vscode-server" / "extensions" / "lramseyer.vaporview-1.5.4"
        d.mkdir(parents=True)
        (d / "package.json").write_text("{ not json", encoding="utf-8")
        # Must swallow the parse error and still exit 0.
        assert iv.main() == 0


class TestArgumentHandling:
    """``--help`` must print help, not silently perform the patch (F-32b) —
    but neither may argv handling become a new way to fail the attach hook.
    The postAttachCommand takes this module's exit code at face value, so a
    typo'd flag must not break the attach the way argparse's bare SystemExit(2)
    would have."""

    def test_help_prints_usage_and_patches_nothing(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("HOME", str(tmp_path))
        d = tmp_path / ".vscode-server" / "extensions" / "lramseyer.vaporview-1.5.4"
        d.mkdir(parents=True)
        manifest = d / "package.json"
        manifest.write_text(json.dumps(_vanilla_manifest()), encoding="utf-8")

        assert iv.main(["--help"]) == 0
        assert "usage:" in capsys.readouterr().out
        # The manifest is untouched: help is not an action.
        assert json.loads(manifest.read_text(encoding="utf-8"))["activationEvents"] == []

    def test_unknown_argument_does_not_fail_the_hook(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("HOME", str(tmp_path))

        assert iv.main(["--patch-everything"]) == 0

        err = capsys.readouterr().err
        assert "--patch-everything" in err  # argparse's own complaint survives
        assert "nothing patched" in err

    def test_a_bad_flag_patches_nothing(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        d = tmp_path / ".vscode-server" / "extensions" / "lramseyer.vaporview-1.5.4"
        d.mkdir(parents=True)
        manifest = d / "package.json"
        manifest.write_text(json.dumps(_vanilla_manifest()), encoding="utf-8")

        iv.main(["--patch-everything"])

        assert json.loads(manifest.read_text(encoding="utf-8"))["activationEvents"] == []

    def test_main_module_entry_point_reports_zero(self, tmp_path, monkeypatch):
        """`python -m booley.runtime.incontainer_vaporview --oops` must still exit 0."""
        env = dict(os.environ, HOME=str(tmp_path))
        env[iv._WAIT_ENV] = "0"
        # HOME is redirected at a tmpdir, which hides a user-site install of
        # booley — hand the child our own import path instead.
        env["PYTHONPATH"] = os.pathsep.join(p for p in sys.path if p)
        result = subprocess.run(
            [sys.executable, "-m", "booley.runtime.incontainer_vaporview", "--oops"],
            capture_output=True,
            text=True,
            check=False,
            env=env,
        )
        assert result.returncode == 0, result.stderr

    def test_library_call_ignores_ambient_argv(self, tmp_path, monkeypatch):
        # main() defaults to an empty argv, so pytest's own command line (or
        # any host process's) can never reach the parser.
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv(iv._WAIT_ENV, "0")
        monkeypatch.setattr(iv, "_start_install_watcher", lambda home: False)
        monkeypatch.setattr("sys.argv", ["pytest", "-x", "--tb=short"])
        assert iv.main() == 0


class TestInstallRaceWait:
    """The bounded, once-per-container wait for a mid-install manifest."""

    @pytest.fixture
    def home(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv(iv._WAIT_ENV, "10")  # enable a 10s budget
        monkeypatch.setattr(iv, "_start_install_watcher", lambda home: False)
        return tmp_path

    def _install(self, home, ver: str = "1.5.4"):
        d = home / ".vscode-server" / "extensions" / f"lramseyer.vaporview-{ver}"
        d.mkdir(parents=True)
        (d / "package.json").write_text(json.dumps(_vanilla_manifest()), encoding="utf-8")
        bundle = d / "dist" / "extension.js"
        bundle.parent.mkdir()
        bundle.write_text(_VANILLA_BUNDLE, encoding="utf-8")

    @staticmethod
    def _fake_time():
        """A (clock, sleep) pair over a shared virtual monotonic clock."""
        state = {"t": 0.0, "sleeps": 0}
        return state

    def test_waits_then_patches_when_manifest_lands_mid_wait(self, home, capsys):
        state = self._fake_time()

        def clock():
            return state["t"]

        def sleep(dt):
            state["t"] += dt
            state["sleeps"] += 1
            if state["sleeps"] == 2:  # extension finishes installing on 2nd poll
                self._install(home)

        assert iv.main(sleep=sleep, clock=clock) == 0
        p = next(
            (home / ".vscode-server" / "extensions").glob("lramseyer.vaporview-*/package.json")
        )
        assert json.loads(p.read_text(encoding="utf-8"))["activationEvents"] == [
            "onStartupFinished"
        ]
        assert "patched 1" in capsys.readouterr().out

    def test_gives_up_after_budget_marks_sentinel(self, home, capsys):
        state = self._fake_time()
        assert (
            iv.main(sleep=lambda dt: state.update(t=state["t"] + dt), clock=lambda: state["t"])
            == 0
        )
        assert iv._wait_sentinel(home).exists()
        assert "not present yet" in capsys.readouterr().out

    def test_staged_install_is_patched_before_vscode_publishes_it(self, home):
        """F-103: the next activation must only see the patched manifest."""
        root = home / ".vscode-server" / "extensions"
        staging = root / ".vscode-extraction"
        published = root / "lramseyer.vaporview-1.5.4"
        state = self._fake_time()
        activation_payload = None

        def sleep(dt):
            nonlocal activation_payload
            state.update(t=state["t"] + dt, sleeps=state["sleeps"] + 1)
            if state["sleeps"] == 1:
                staging.mkdir(parents=True)
                (staging / "package.json").write_text(
                    json.dumps(_vanilla_manifest()), encoding="utf-8"
                )
                bundle = staging / "dist" / "extension.js"
                bundle.parent.mkdir()
                bundle.write_text(_VANILLA_BUNDLE, encoding="utf-8")
            elif state["sleeps"] == 2:
                # VS Code publishes a completed extraction with an atomic rename.
                # The watcher must have patched staging before this activation point.
                staging.rename(published)
                activation_payload = json.loads(
                    (published / "package.json").read_text(encoding="utf-8")
                )

        assert iv._watch_for_late_install(
            home,
            sleep=sleep,
            clock=lambda: state["t"],
            stop=lambda: state["sleeps"] > 3,
        )
        assert activation_payload is not None
        assert activation_payload["activationEvents"] == ["onStartupFinished"]
        properties = activation_payload["contributes"]["configuration"]["properties"]
        assert properties["vaporview.wcp.enabled"]["scope"] == "machine"
        assert properties["vaporview.wcp.port"]["scope"] == "machine"

    def test_incomplete_install_is_retried_until_verified(self, home):
        root = home / ".vscode-server" / "extensions"
        extension = root / "lramseyer.vaporview-1.5.4"
        extension.mkdir(parents=True)
        manifest = extension / "package.json"
        manifest.write_text("{not json", encoding="utf-8")
        state = self._fake_time()
        err = StringIO()

        def sleep(dt):
            state.update(t=state["t"] + dt, sleeps=state["sleeps"] + 1)
            if state["sleeps"] == 1:
                manifest.write_text(json.dumps(_vanilla_manifest()), encoding="utf-8")
            elif state["sleeps"] == 2:
                # The manifest is the commit point and must stay untouched
                # while the bundle is still incomplete.
                assert json.loads(manifest.read_text(encoding="utf-8"))["activationEvents"] == []
                bundle = extension / "dist" / "extension.js"
                bundle.parent.mkdir()
                bundle.write_text(_VANILLA_BUNDLE, encoding="utf-8")

        assert iv._watch_for_late_install(
            home,
            sleep=sleep,
            clock=lambda: state["t"],
            stop=lambda: state["sleeps"] > 3,
            err=err,
        )
        assert state["sleeps"] == 2
        assert "manifest is not readable JSON yet" in err.getvalue()
        assert "bundle is not readable yet" in err.getvalue()
        assert iv._installation_is_patched(manifest)

    def test_watcher_has_no_install_deadline(self, home):
        state = self._fake_time()

        def sleep(dt):
            state.update(t=state["t"] + dt, sleeps=state["sleeps"] + 1)

        assert not iv._watch_for_late_install(
            home,
            sleep=sleep,
            clock=lambda: state["t"],
            stop=lambda: state["sleeps"] == 40,
        )
        assert state["sleeps"] == 40

    def test_watcher_lock_failure_reports_context(self, home, monkeypatch):
        err = StringIO()

        def fail(_home):
            raise PermissionError("read-only state directory")

        monkeypatch.setattr(iv, "_open_watch_lock", fail)

        assert not iv._watch_for_late_install(
            home,
            sleep=lambda _dt: None,
            clock=lambda: 0.0,
            err=err,
        )
        assert str(home / ".booley" / iv._WATCH_LOCK) in err.getvalue()
        assert "read-only state directory" in err.getvalue()

    def test_watcher_spawn_failure_reports_command_home_and_log(
        self, tmp_path, monkeypatch, capsys
    ):
        def fail(*_args, **_kwargs):
            raise PermissionError("process limit")

        monkeypatch.setattr(iv.subprocess, "Popen", fail)

        assert not iv._start_install_watcher(tmp_path)
        error = capsys.readouterr().err
        assert iv._WATCH_FLAG in error
        assert str(tmp_path) in error
        assert str(tmp_path / ".booley" / iv._WATCH_LOG) in error
        assert "process limit" in error

    def test_sentinel_skips_the_second_wait(self, home):
        # First run gives up (never installs) and drops the sentinel.
        first = self._fake_time()
        iv.main(
            sleep=lambda dt: first.update(t=first["t"] + dt, sleeps=first["sleeps"] + 1),
            clock=lambda: first["t"],
        )
        assert first["sleeps"] > 0
        # Second run must NOT wait again — the sentinel short-circuits it.
        second = self._fake_time()
        iv.main(
            sleep=lambda dt: second.update(t=second["t"] + dt, sleeps=second["sleeps"] + 1),
            clock=lambda: second["t"],
        )
        assert second["sleeps"] == 0

    def test_budget_zero_disables_the_wait(self, home, monkeypatch, capsys):
        monkeypatch.setenv(iv._WAIT_ENV, "0")
        slept = {"n": 0}
        assert iv.main(sleep=lambda dt: slept.update(n=slept["n"] + 1), clock=lambda: 0.0) == 0
        assert slept["n"] == 0
        assert "not present yet" in capsys.readouterr().out

    @pytest.mark.parametrize("value", ["not-a-number", "nan", "inf", "-inf"])
    def test_malformed_budget_falls_back_to_default(self, monkeypatch, value):
        monkeypatch.setenv(iv._WAIT_ENV, value)
        assert iv._wait_budget_seconds() == iv._WAIT_SECONDS_DEFAULT
