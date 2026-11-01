"""Tests for the in-container VaporView theme and color-scope patcher."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from io import StringIO
from pathlib import Path

import pytest

from booley.runtime import incontainer_vaporview as iv

_VERSION = iv.vaporview.VERIFIED_VERSION
_REAL_BUNDLE = Path.home() / ".cache/vv160-research/vsix/extension/dist/extension.js"


def _vanilla_manifest() -> dict:
    """A minimal shape mirroring the vendored manifest: application-scoped
    custom color slots plus unrelated settings."""
    return {
        "publisher": "lramseyer",
        "name": "vaporview",
        "version": _VERSION,
        "activationEvents": ["onStartupFinished"],
        "contributes": {
            "configuration": [
                {
                    "properties": {
                        "vaporview.wcp.enabled": {"scope": "machine", "default": False},
                        "vaporview.other": {"scope": "window"},
                    }
                },
                {
                    "properties": {
                        "vaporview.customColor5": {"scope": "application", "default": "#ffa557"},
                        "vaporview.customColor6": {"scope": "application", "default": "#dcdcaa"},
                        "vaporview.customColor7": {"scope": "application", "default": "#d2a8ff"},
                        "vaporview.customColor8": {"scope": "application", "default": "#4abfa8"},
                    }
                },
            ],
        },
    }


_VANILLA_BUNDLE = (
    "class Documents{constructor(log){this.log=log;this.getTokenColorsForTheme()}"
    "async getTokenColorsForTheme(){try{await getUserTheme()}catch(e){"
    'V.window.showErrorMessage("Error getting user theme: "+e),'
    'this.colorPalette=["#CCCCCC"],this.themeValid=!1}'
    "onThemeChange(s){s.getTokenColorsForTheme()}}"
)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@pytest.fixture(autouse=True)
def _pin_synthetic_bundle_fingerprints(monkeypatch):
    """Give integration tests exact fingerprints for their compact fixture."""
    patched, changed, problem = iv._prepare_theme_source(_VANILLA_BUNDLE)
    assert changed and problem is None
    monkeypatch.setattr(iv, "_SUPPORTED_BUNDLE_SHA256", _digest(_VANILLA_BUNDLE))
    monkeypatch.setattr(iv, "_PATCHED_BUNDLE_SHA256", _digest(patched))


def _install_at(directory: Path, manifest: dict, bundle: str | None = _VANILLA_BUNDLE) -> Path:
    """Write one extension directory; return its manifest path."""
    directory.mkdir(parents=True)
    path = directory / "package.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    if bundle is not None:
        (directory / "dist").mkdir()
        (directory / "dist" / "extension.js").write_text(bundle, encoding="utf-8")
    return path


def _color_properties(manifest: dict) -> dict:
    return manifest["contributes"]["configuration"][1]["properties"]


class TestPatchManifest:
    def test_machine_scopes_and_seeds_only_booley_color_slots(self):
        m = _vanilla_manifest()
        assert iv.patch_manifest(m) is True
        colors = _color_properties(m)
        for key, value in iv.vaporview.PRESENTATION_COLOR_SETTINGS.items():
            assert colors[key] == {"scope": "machine", "default": value}
        # The unused slot and unrelated settings are left untouched.
        assert colors["vaporview.customColor8"] == {"scope": "application", "default": "#4abfa8"}
        other = m["contributes"]["configuration"][0]["properties"]
        assert other["vaporview.other"] == {"scope": "window"}
        assert m["activationEvents"] == ["onStartupFinished"]

    def test_idempotent(self):
        m = _vanilla_manifest()
        iv.patch_manifest(m)
        assert iv.patch_manifest(m) is False

    def test_single_configuration_block(self):
        m = _vanilla_manifest()
        m["contributes"]["configuration"] = m["contributes"]["configuration"][1]
        assert iv.patch_manifest(m) is True
        props = m["contributes"]["configuration"]["properties"]
        assert props["vaporview.customColor5"]["scope"] == "machine"

    def test_complete_manifest_is_pinned_to_verified_version(self):
        manifest = _vanilla_manifest()
        manifest["version"] = "9.9.9"
        assert not iv._manifest_is_complete(manifest)

    def test_complete_manifest_needs_every_booley_color_slot(self):
        manifest = _vanilla_manifest()
        del _color_properties(manifest)["vaporview.customColor6"]
        assert not iv._manifest_is_complete(manifest)

    def test_complete_manifest_identifies_vaporview(self):
        manifest = _vanilla_manifest()
        manifest["publisher"] = "someone"
        assert not iv._manifest_is_complete(manifest)
        assert iv._manifest_is_complete(_vanilla_manifest())


class TestFindManifests:
    def test_globs_versioned_dirs(self, tmp_path):
        ext = tmp_path / ".vscode-server" / "extensions"
        for ver in ("1.6.0", "1.7.0"):
            d = ext / f"lramseyer.vaporview-{ver}"
            d.mkdir(parents=True)
            (d / "package.json").write_text("{}", encoding="utf-8")
        # A non-VaporView extension must not match.
        other = ext / "someone.other-1.0.0"
        other.mkdir(parents=True)
        (other / "package.json").write_text("{}", encoding="utf-8")

        found = iv.find_manifests(tmp_path)
        assert [p.parent.name for p in found] == [
            "lramseyer.vaporview-1.6.0",
            "lramseyer.vaporview-1.7.0",
        ]

    def test_absent_extension_returns_empty(self, tmp_path):
        assert iv.find_manifests(tmp_path) == []


class TestThemeBundle:
    def test_disables_both_lookup_calls_and_keeps_the_method(self):
        patched, changed, problem = iv._prepare_theme_source(_VANILLA_BUNDLE)
        assert changed and problem is None
        assert patched.count("getTokenColorsForTheme()") == 1  # the definition
        assert patched.count("void 0") == 2
        assert 'this.colorPalette=["#CCCCCC"]' in patched

    def test_already_patched_source_is_unchanged(self):
        patched, _changed, _problem = iv._prepare_theme_source(_VANILLA_BUNDLE)
        again, changed, problem = iv._prepare_theme_source(patched)
        assert (again, changed, problem) == (patched, False, None)

    def test_partial_bundle_is_rejected(self):
        source = "prefix this.getTokenColorsForTheme() suffix"
        updated, changed, problem = iv._prepare_theme_source(source)
        assert updated == source and not changed
        assert problem == "bundle has 1 of 2 expected theme lookup calls"

    def test_prepare_reports_unsupported_fingerprint(self, tmp_path):
        bundle = tmp_path / "dist" / "extension.js"
        bundle.parent.mkdir()
        bundle.write_text("new upstream implementation", encoding="utf-8")

        _path, updated, problem = iv._prepare_compatible_bundle(tmp_path)

        assert updated is None
        assert problem is not None and "fingerprint" in problem
        assert f"not the official VaporView {_VERSION} build" in problem

    def test_prepare_accepts_patched_bundle_as_fixed_point(self, tmp_path):
        patched, _changed, _problem = iv._prepare_theme_source(_VANILLA_BUNDLE)
        bundle = tmp_path / "dist" / "extension.js"
        bundle.parent.mkdir()
        bundle.write_text(patched, encoding="utf-8")

        assert iv._prepare_compatible_bundle(tmp_path) == (bundle, None, None)

    def test_missing_bundle_is_reported(self, tmp_path):
        _path, updated, problem = iv._prepare_compatible_bundle(tmp_path)
        assert updated is None
        assert problem is not None and "not readable yet" in problem

    def test_wrong_patch_result_is_rejected(self, tmp_path, monkeypatch):
        monkeypatch.setattr(iv, "_PATCHED_BUNDLE_SHA256", "0" * 64)
        bundle = tmp_path / "dist" / "extension.js"
        bundle.parent.mkdir()
        bundle.write_text(_VANILLA_BUNDLE, encoding="utf-8")

        _path, updated, problem = iv._prepare_compatible_bundle(tmp_path)

        assert updated is None
        assert problem == "prepared bundle does not match the verified patched build"

    @pytest.mark.skipif(not _REAL_BUNDLE.is_file(), reason="official VSIX not unpacked locally")
    def test_official_bundle_maps_to_the_verified_patched_fingerprint(self, monkeypatch):
        monkeypatch.undo()  # use the production fingerprints
        source = _REAL_BUNDLE.read_text(encoding="utf-8")
        assert iv._bundle_sha256(source) == iv._SUPPORTED_BUNDLE_SHA256
        patched, changed, problem = iv._prepare_theme_source(source)
        assert changed and problem is None
        assert iv._bundle_sha256(patched) == iv._PATCHED_BUNDLE_SHA256


class TestPatchFile:
    def test_patches_bundle_and_manifest_then_verifies(self, tmp_path):
        manifest = _install_at(tmp_path / f"lramseyer.vaporview-{_VERSION}", _vanilla_manifest())

        result = iv._patch_file(manifest)

        assert result == iv._PatchResult(True, True)
        assert iv._installation_is_patched(manifest)
        bundle = (manifest.parent / "dist" / "extension.js").read_text(encoding="utf-8")
        assert bundle.count("void 0") == 2

    def test_second_run_is_a_no_op(self, tmp_path):
        manifest = _install_at(tmp_path / f"lramseyer.vaporview-{_VERSION}", _vanilla_manifest())
        iv._patch_file(manifest)
        before = manifest.read_text(encoding="utf-8")

        assert iv._patch_file(manifest) == iv._PatchResult(True, False)
        assert manifest.read_text(encoding="utf-8") == before

    def test_unsupported_version_is_untouched_with_reason(self, tmp_path):
        payload = _vanilla_manifest()
        payload["version"] = "1.7.0"
        manifest = _install_at(tmp_path / "lramseyer.vaporview-1.7.0", payload)
        original = manifest.read_text(encoding="utf-8")

        result = iv._patch_file(manifest)

        assert not result.complete and not result.changed
        assert result.detail == f"unsupported VaporView version '1.7.0'; expected {_VERSION}"
        assert manifest.read_text(encoding="utf-8") == original
        bundle = (manifest.parent / "dist" / "extension.js").read_text(encoding="utf-8")
        assert bundle == _VANILLA_BUNDLE

    def test_unknown_bundle_leaves_manifest_untouched(self, tmp_path):
        manifest = _install_at(
            tmp_path / f"lramseyer.vaporview-{_VERSION}",
            _vanilla_manifest(),
            bundle=_VANILLA_BUNDLE + "locally modified",
        )
        original = manifest.read_text(encoding="utf-8")

        result = iv._patch_file(manifest)

        assert not result.complete and not result.changed
        assert result.detail is not None and "fingerprint" in result.detail
        assert manifest.read_text(encoding="utf-8") == original

    def test_manifest_missing_a_color_key_is_rejected(self, tmp_path):
        payload = _vanilla_manifest()
        del _color_properties(payload)["vaporview.customColor7"]
        manifest = _install_at(tmp_path / f"lramseyer.vaporview-{_VERSION}", payload)

        result = iv._patch_file(manifest)

        assert not result.complete
        assert result.detail == "manifest does not contain all expected VaporView fields"
        bundle = (manifest.parent / "dist" / "extension.js").read_text(encoding="utf-8")
        assert bundle == _VANILLA_BUNDLE

    def test_atomic_write_failure_is_reported(self, tmp_path, monkeypatch):
        manifest = _install_at(tmp_path / f"lramseyer.vaporview-{_VERSION}", _vanilla_manifest())

        def fail_write(_path, _content):
            raise OSError("simulated write failure")

        monkeypatch.setattr(iv, "_atomic_write_text", fail_write)

        result = iv._patch_file(manifest)

        assert not result.complete
        assert result.detail == "atomic replacement failed (simulated write failure)"


class TestMain:
    @pytest.fixture
    def home(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        # Disable the install-race wait so the absent-extension paths return at
        # once; the wait itself is exercised in TestInstallRaceWait.
        monkeypatch.setenv(iv._WAIT_ENV, "0")
        monkeypatch.setattr(iv, "_start_install_watcher", lambda home: False)
        return tmp_path

    def _install(self, home, manifest: dict, *, root: str = ".vscode-server") -> Path:
        directory = home / root / "extensions" / f"lramseyer.vaporview-{_VERSION}"
        return _install_at(directory, manifest)

    def test_patches_installed_extension(self, home, capsys):
        p = self._install(home, _vanilla_manifest())
        assert iv.main() == 0
        out = json.loads(p.read_text(encoding="utf-8"))
        assert _color_properties(out)["vaporview.customColor5"]["scope"] == "machine"
        assert iv._installation_is_patched(p)
        assert "patched 1" in capsys.readouterr().out

    def test_patches_install_under_codespaces_root(self, home, capsys):
        p = self._install(home, _vanilla_manifest(), root=".vscode-remote")

        assert iv.main() == 0

        assert iv._installation_is_patched(p)
        assert "patched 1" in capsys.readouterr().out

    def test_patches_bundle_when_manifest_is_already_current(self, home, capsys):
        manifest = _vanilla_manifest()
        iv.patch_manifest(manifest)
        p = self._install(home, manifest)

        assert iv.main() == 0

        bundle = p.parent / "dist" / "extension.js"
        assert bundle.read_text(encoding="utf-8").count("getTokenColorsForTheme()") == 1
        assert "patched 1" in capsys.readouterr().out

    def test_absent_extension_is_a_clean_noop(self, home, capsys):
        # Install still in flight: succeed and say so, never fail the hook.
        assert iv.main() == 0
        assert "not present yet" in capsys.readouterr().out

    def test_unsupported_install_is_reported_and_watched(self, home, capsys):
        payload = _vanilla_manifest()
        payload["version"] = "1.7.0"
        self._install(home, payload)

        assert iv.main() == 0

        captured = capsys.readouterr()
        assert "unsupported VaporView version '1.7.0'" in captured.err
        assert "extension incomplete" in captured.out

    def test_second_run_reports_already_patched(self, home, capsys):
        self._install(home, _vanilla_manifest())
        iv.main()
        capsys.readouterr()
        assert iv.main() == 0
        assert "already patched" in capsys.readouterr().out

    def test_malformed_manifest_does_not_raise(self, home, capsys):
        d = home / ".vscode-server" / "extensions" / f"lramseyer.vaporview-{_VERSION}"
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
        d = tmp_path / ".vscode-server" / "extensions" / f"lramseyer.vaporview-{_VERSION}"
        d.mkdir(parents=True)
        manifest = d / "package.json"
        manifest.write_text(json.dumps(_vanilla_manifest()), encoding="utf-8")

        original = manifest.read_text(encoding="utf-8")

        assert iv.main(["--help"]) == 0
        assert "usage:" in capsys.readouterr().out
        # The manifest is untouched: help is not an action.
        assert manifest.read_text(encoding="utf-8") == original

    def test_unknown_argument_does_not_fail_the_hook(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("HOME", str(tmp_path))

        assert iv.main(["--patch-everything"]) == 0

        err = capsys.readouterr().err
        assert "--patch-everything" in err  # argparse's own complaint survives
        assert "nothing patched" in err

    def test_a_bad_flag_patches_nothing(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        d = tmp_path / ".vscode-server" / "extensions" / f"lramseyer.vaporview-{_VERSION}"
        d.mkdir(parents=True)
        manifest = d / "package.json"
        manifest.write_text(json.dumps(_vanilla_manifest()), encoding="utf-8")

        original = manifest.read_text(encoding="utf-8")

        iv.main(["--patch-everything"])

        assert manifest.read_text(encoding="utf-8") == original

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

    def _install(self, home, ver: str = _VERSION):
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
        assert iv._installation_is_patched(p)
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
        published = root / f"lramseyer.vaporview-{_VERSION}"
        state = self._fake_time()
        activation_payload = None
        activation_bundle = None

        def sleep(dt):
            nonlocal activation_payload, activation_bundle
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
                activation_bundle = (published / "dist" / "extension.js").read_text(
                    encoding="utf-8"
                )

        assert iv._watch_for_late_install(
            home,
            sleep=sleep,
            clock=lambda: state["t"],
            stop=lambda: state["sleeps"] > 3,
        )
        assert activation_payload is not None
        properties = _color_properties(activation_payload)
        for key in iv.vaporview.PRESENTATION_COLOR_SETTINGS:
            assert properties[key]["scope"] == "machine"
        assert activation_bundle is not None
        assert activation_bundle.count("void 0") == 2

    def test_staged_manifests_cover_every_server_root(self, home):
        for root_name in (".vscode-server", ".vscode-remote"):
            staging = home / root_name / "extensions" / ".vscode-extraction"
            staging.mkdir(parents=True)
            (staging / "package.json").write_text(json.dumps(_vanilla_manifest()), "utf-8")

        found = iv._staged_manifests(home)

        assert {path.parents[2].name for path in found} == {".vscode-server", ".vscode-remote"}

    def test_incomplete_install_is_retried_until_verified(self, home):
        root = home / ".vscode-server" / "extensions"
        extension = root / f"lramseyer.vaporview-{_VERSION}"
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
                payload = json.loads(manifest.read_text(encoding="utf-8"))
                assert _color_properties(payload)["vaporview.customColor5"]["scope"] == (
                    "application"
                )
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
