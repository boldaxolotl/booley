"""Tests for the sandbox-image staleness guard (build-fingerprint label).

Covers the source-drift detection that makes ``booley init`` rebuild an image
built from now-stale source instead of skipping it — the failure that let a
container run an image predating the container-side skill deployment.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

from booley.harness import init_cmd, init_docker_image
from booley.harness.init_common import InitContext


def _seed_source(root: Path) -> None:
    """Minimal repo layout that :func:`_image_build_fingerprint` hashes."""
    pkg = root / "src" / "booley"
    pkg.mkdir(parents=True)
    (pkg / "incontainer_register.py").write_text("x = 1\n", encoding="utf-8")
    (root / "pyproject.toml").write_text("[project]\nname='booley'\n", encoding="utf-8")
    bwave = root / "crates" / "bwave"
    (bwave / "src").mkdir(parents=True)
    (bwave / "src" / "main.rs").write_text("fn main() {}\n", encoding="utf-8")
    (bwave / "Cargo.toml").write_text("[package]\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# _image_build_fingerprint
# ---------------------------------------------------------------------------


class TestFingerprint:
    def test_none_without_source_tree(self, tmp_path):
        # No src/booley -> can't fingerprint -> None (disables the guard).
        assert init_cmd._image_build_fingerprint(tmp_path) is None

    def test_stable_and_hex(self, tmp_path):
        _seed_source(tmp_path)
        fp1 = init_cmd._image_build_fingerprint(tmp_path)
        fp2 = init_cmd._image_build_fingerprint(tmp_path)
        assert fp1 == fp2
        assert fp1 and len(fp1) == 64  # sha256 hexdigest

    def test_changes_when_source_changes(self, tmp_path):
        _seed_source(tmp_path)
        before = init_cmd._image_build_fingerprint(tmp_path)
        (tmp_path / "src" / "booley" / "incontainer_register.py").write_text(
            "x = 2\n", encoding="utf-8"
        )
        assert init_cmd._image_build_fingerprint(tmp_path) != before

    def test_changes_on_new_file(self, tmp_path):
        _seed_source(tmp_path)
        before = init_cmd._image_build_fingerprint(tmp_path)
        (tmp_path / "src" / "booley" / "new_mod.py").write_text("y = 1\n", encoding="utf-8")
        assert init_cmd._image_build_fingerprint(tmp_path) != before

    def test_ignores_pycache(self, tmp_path):
        _seed_source(tmp_path)
        before = init_cmd._image_build_fingerprint(tmp_path)
        cache = tmp_path / "src" / "booley" / "__pycache__"
        cache.mkdir()
        (cache / "incontainer_register.cpython-313.pyc").write_bytes(b"\x00compiled")
        assert init_cmd._image_build_fingerprint(tmp_path) == before

    def test_ignores_bwave_target(self, tmp_path):
        # The huge target/ build dir must not enter the hash (only src + manifests).
        _seed_source(tmp_path)
        before = init_cmd._image_build_fingerprint(tmp_path)
        target = tmp_path / "crates" / "bwave" / "target" / "release"
        target.mkdir(parents=True)
        (target / "bwave").write_bytes(b"\x00binary")
        assert init_cmd._image_build_fingerprint(tmp_path) == before


def test_image_build_metadata_args_include_runtime_provenance(tmp_path, monkeypatch):
    monkeypatch.setattr(init_docker_image, "resolve_build_commit", lambda _root: "abc123")
    monkeypatch.setattr(
        init_docker_image,
        "resolve_source_updated_at",
        lambda _root: "2026-08-10T10:00:00Z",
    )
    monkeypatch.setattr(init_docker_image, "_read_version", lambda: "1.2.3")
    (tmp_path / ".git").mkdir()

    args = init_docker_image._image_build_metadata_args(tmp_path)
    values = set(args[1::2])

    assert "BOOLEY_VERSION=1.2.3" in values
    assert "BOOLEY_SOURCE_REVISION=abc123" in values
    assert "BOOLEY_SOURCE_UPDATED_AT=2026-08-10T10:00:00Z" in values
    assert any(value.startswith("BOOLEY_IMAGE_BUILT_AT=") for value in values)


# ---------------------------------------------------------------------------
# _image_is_stale
# ---------------------------------------------------------------------------


class TestIsStale:
    def test_no_fingerprint_is_not_stale(self, monkeypatch):
        # None fingerprint (no source) -> never stale, whatever the label is.
        monkeypatch.setattr(init_docker_image, "_image_label", lambda *a: "anything")
        assert init_docker_image._image_is_stale(None) is False

    def test_unlabeled_image_is_stale(self, monkeypatch):
        # The exact bug: a pre-guard image carries no fingerprint label.
        monkeypatch.setattr(init_docker_image, "_image_label", lambda *a: None)
        assert init_docker_image._image_is_stale("abc123") is True

    def test_matching_label_is_current(self, monkeypatch):
        monkeypatch.setattr(init_docker_image, "_image_label", lambda *a: "abc123")
        assert init_docker_image._image_is_stale("abc123") is False

    def test_differing_label_is_stale(self, monkeypatch):
        monkeypatch.setattr(init_docker_image, "_image_label", lambda *a: "old999")
        assert init_docker_image._image_is_stale("abc123") is True

    def test_pulled_image_is_left_alone(self, monkeypatch):
        # A deliberately pulled pre-built image can't match a local fingerprint;
        # its pulled:* provenance marks it as intentional, not stale.
        monkeypatch.setattr(init_docker_image, "_image_label", lambda *a: "pulled:0.1.0")
        assert init_docker_image._image_is_stale("abc123") is False


# ---------------------------------------------------------------------------
# source_fingerprint_mismatch (advisory probe for session start / doctor)
# ---------------------------------------------------------------------------


class TestSourceFingerprintMismatch:
    def _patch(self, monkeypatch, *, fingerprint, label):
        monkeypatch.setattr(
            init_docker_image, "_image_build_fingerprint", lambda _root: fingerprint
        )
        monkeypatch.setattr(init_docker_image, "_image_label", lambda *_a: label)

    def test_no_checkout_gives_no_verdict(self, monkeypatch):
        # pip-installed Booley: nothing to hash -> None, never a nag.
        self._patch(monkeypatch, fingerprint=None, label="whatever")
        assert init_docker_image.source_fingerprint_mismatch("img") is None

    def test_unlabeled_image_gives_no_verdict(self, monkeypatch):
        # Unlike _image_is_stale (init's rebuild decision), a hand-authored
        # image without the label must not be nagged at every session start.
        self._patch(monkeypatch, fingerprint="abc", label=None)
        assert init_docker_image.source_fingerprint_mismatch("img") is None

    def test_pulled_image_gives_no_verdict(self, monkeypatch):
        self._patch(monkeypatch, fingerprint="abc", label="pulled:0.1.0")
        assert init_docker_image.source_fingerprint_mismatch("img") is None

    def test_matching_label_is_current(self, monkeypatch):
        self._patch(monkeypatch, fingerprint="abc", label="abc")
        assert init_docker_image.source_fingerprint_mismatch("img") is False

    def test_differing_label_is_stale(self, monkeypatch):
        self._patch(monkeypatch, fingerprint="abc", label="old")
        assert init_docker_image.source_fingerprint_mismatch("img") is True


# ---------------------------------------------------------------------------
# _image_label parsing
# ---------------------------------------------------------------------------


class TestImageLabel:
    def _fake_run(self, stdout, returncode=0):
        class _R:
            pass

        r = _R()
        r.stdout = stdout
        r.returncode = returncode
        return lambda *a, **k: r

    def test_no_value_sentinel_is_none(self, monkeypatch):
        # Go template prints "<no value>" for a missing key.
        monkeypatch.setattr(init_cmd.subprocess, "run", self._fake_run("<no value>\n"))
        assert init_cmd._image_label("img", "booley.build-fingerprint") is None

    def test_empty_is_none(self, monkeypatch):
        monkeypatch.setattr(init_cmd.subprocess, "run", self._fake_run("\n"))
        assert init_cmd._image_label("img", "l") is None

    def test_value_returned(self, monkeypatch):
        monkeypatch.setattr(init_cmd.subprocess, "run", self._fake_run("abc123\n"))
        assert init_cmd._image_label("img", "l") == "abc123"

    def test_inspect_failure_is_none(self, monkeypatch):
        monkeypatch.setattr(init_cmd.subprocess, "run", self._fake_run("", returncode=1))
        assert init_cmd._image_label("img", "l") is None


# ---------------------------------------------------------------------------
# Build-cache size report (SETUP-24) — _size_to_gb / _report_build_cache
# ---------------------------------------------------------------------------


class TestSizeToGb:
    def test_parses_units(self):
        f = init_docker_image._size_to_gb
        assert abs(f("29.3GB") - 29.3) < 1e-6
        assert abs(f("512MB") - 0.512) < 1e-6
        assert abs(f("1.5kB") - 1.5e-6) < 1e-9
        assert abs(f("2TB") - 2000.0) < 1e-6
        assert abs(f("800B") - 800e-9) < 1e-9

    def test_gibibyte_suffix_tolerated(self):
        # docker sometimes prints GiB; the "i" is ignored.
        assert abs(init_docker_image._size_to_gb("10GiB") - 10.0) < 1e-6

    def test_unparseable_is_zero(self):
        assert init_docker_image._size_to_gb("N/A") == 0.0


class TestReportBuildCache:
    def _fake_df(self, monkeypatch, stdout, returncode=0):
        import subprocess as sp

        r = sp.CompletedProcess(["docker"], returncode, stdout=stdout, stderr="")
        monkeypatch.setattr(init_docker_image.subprocess, "run", lambda *a, **k: r)

    def test_reports_size_and_prune_hint_when_large(self, monkeypatch):
        self._fake_df(monkeypatch, "Images\t2GB\t1GB\nBuild Cache\t29.3GB\t29.3GB\n")
        msgs: list[str] = []
        monkeypatch.setattr(init_docker_image, "info", msgs.append)
        init_docker_image._report_build_cache(prune_hint_gb=10.0)
        joined = "\n".join(msgs)
        assert "docker build cache: 29.3GB" in joined
        assert "29.3GB reclaimable" in joined
        assert "docker builder prune" in joined

    def test_no_prune_hint_when_small(self, monkeypatch):
        self._fake_df(monkeypatch, "Build Cache\t2.0GB\t2.0GB\n")
        msgs: list[str] = []
        monkeypatch.setattr(init_docker_image, "info", msgs.append)
        init_docker_image._report_build_cache(prune_hint_gb=10.0)
        joined = "\n".join(msgs)
        assert "docker build cache: 2.0GB" in joined
        assert "prune" not in joined

    def test_silent_on_docker_error(self, monkeypatch):
        self._fake_df(monkeypatch, "", returncode=1)
        msgs: list[str] = []
        monkeypatch.setattr(init_docker_image, "info", msgs.append)
        init_docker_image._report_build_cache()
        assert msgs == []

    def test_existing_base_image_still_reports_cache(self, monkeypatch):
        monkeypatch.setattr(init_docker_image.shutil, "which", lambda _name: "/usr/bin/docker")
        monkeypatch.setattr(init_docker_image, "_docker_image_exists", lambda: True)
        monkeypatch.setattr(init_docker_image, "_image_build_fingerprint", lambda _root: "same")
        monkeypatch.setattr(init_docker_image, "_image_is_stale", lambda _fingerprint: False)
        reports: list[bool] = []
        monkeypatch.setattr(init_docker_image, "_report_build_cache", lambda: reports.append(True))

        init_docker_image._step_docker_image(InitContext(project_root=Path("/tmp/project")))

        assert reports == [True]


# ---------------------------------------------------------------------------
# Build-log decoding
# ---------------------------------------------------------------------------


class TestDockerBuildCommand:
    """`docker build` output is UTF-8; the console locale is not.

    With a bare ``text=True`` Python decodes the pipe with the locale codec —
    cp1252 on a Windows host — so the first byte BuildKit emits outside that
    range raised UnicodeDecodeError and killed a 20-minute image rebuild that
    was otherwise succeeding.
    """

    def _run(self, monkeypatch, tmp_path: Path, payload: bytes, *, force: bool = False):
        import subprocess
        import sys

        captured: dict = {}
        real_popen = subprocess.Popen

        def fake_popen(_cmd, **kwargs):
            captured["cmd"] = list(_cmd)
            captured.update(kwargs)
            # Re-issue the *same* kwargs against a child that emits `payload`,
            # so the real decoding path in _docker_build_image runs on it.
            code = f"import sys; sys.stdout.buffer.write({payload!r}); sys.stdout.buffer.flush()"
            return real_popen([sys.executable, "-c", code], **kwargs)

        monkeypatch.setattr(init_docker_image.subprocess, "Popen", fake_popen)
        ctx = init_cmd.InitContext(project_root=tmp_path, force=force, verbose=False)
        rc = init_docker_image._docker_build_image(
            ctx,
            tmp_path / "Dockerfile",
            tmp_path,
            exists=False,
        )
        return rc, captured

    def test_undecodable_byte_does_not_kill_the_build(self, monkeypatch, tmp_path: Path):
        # 0x81 is undefined in cp1252 — the exact byte that aborted the rebuild.
        rc, _ = self._run(monkeypatch, tmp_path, b">>> Building \x81 from source\n")
        assert rc == 0

    def test_utf8_progress_glyphs_survive(self, monkeypatch, tmp_path: Path):
        rc, _ = self._run(monkeypatch, tmp_path, ">>> Building ✓ ─ done\n".encode())
        assert rc == 0

    def test_pipe_is_decoded_as_utf8_with_replacement(self, monkeypatch, tmp_path: Path):
        _rc, captured = self._run(monkeypatch, tmp_path, b">>> ok\n")
        assert captured["encoding"] == "utf-8"
        assert captured["errors"] == "replace"

    def test_force_rebuild_keeps_docker_layer_cache(self, monkeypatch, tmp_path: Path):
        _rc, captured = self._run(monkeypatch, tmp_path, b">>> ok\n", force=True)

        assert captured["cmd"][:2] == ["docker", "build"]
        assert "--no-cache" not in captured["cmd"]


# ---------------------------------------------------------------------------
# build.sh Python-selection guard (source invariant)
# ---------------------------------------------------------------------------
# The guard picks an interpreter that ships the *pypa* `build` module. Its probe
# MUST run under the same `-P` isolation the wheel step uses: without it, `-c`
# puts the cwd on sys.path and the repo-root `build/` setuptools artifact dir (a
# bare namespace package) satisfies `import build`, so the probe green-lights a
# Python whose `-P -m build` then fails with "No module named build" — a stale
# wheel gets baked into the image. These are source invariants: cheap guards that
# fail loudly if the `-P` (or the freshness assertion) is ever dropped.

_BUILD_SH = Path(__file__).resolve().parents[2] / "src" / "booley" / "data" / "docker" / "build.sh"


def test_build_sh_probe_uses_dash_p_isolation() -> None:
    """The candidate-Python probe must use `-P`, matching the wheel invocation."""
    text = _BUILD_SH.read_text(encoding="utf-8")
    # The probe line runs each candidate against `import build` inside the loop.
    probe = next(
        (l for l in text.splitlines() if "import build" in l and '"$cand"' in l),
        None,
    )
    assert probe is not None, "guard probe line not found in build.sh"
    assert re.search(r'"\$cand"\s+-P\s+-c', probe), (
        f"probe must isolate with -P so the repo-root build/ dir cannot shadow "
        f"the pypa build module; got: {probe.strip()!r}"
    )


def test_build_sh_asserts_a_fresh_wheel() -> None:
    """A freshness check must guard against the docker COPY baking a stale wheel."""
    text = _BUILD_SH.read_text(encoding="utf-8")
    assert "-newer" in text and "booley_rtl-*.whl" in text, (
        "build.sh must assert the wheel was (re)written this run before the docker build COPYs it"
    )


def test_build_sh_removes_stale_staging_tree() -> None:
    """Deleted packages must not survive in setuptools' incremental build/lib."""
    text = _BUILD_SH.read_text(encoding="utf-8")
    assert 'rm -rf "$BOOLEY_ROOT/build"' in text


# ---------------------------------------------------------------------------
# Wheel-build failure reporting (fpu F-1)
# ---------------------------------------------------------------------------
# pypa `build` writes its progress log AND its diagnosis to stdout, leaving
# stderr empty. A stderr-only report therefore showed the user one useless
# "Creating isolated environment" line and swallowed the actual cause.


def _wheel_failure(ctx, *, stdout: str = "", stderr: str = ""):
    exc = subprocess.CalledProcessError(1, ["python", "-m", "build"], stdout, stderr)
    return init_docker_image._report_wheel_failure(ctx, exc)


class TestWheelFailureReport:
    def test_stdout_is_surfaced(self, capsys):
        ctx = InitContext(project_root=Path("/tmp/x"))
        # Always returns False — the caller reads it as "wheel step failed".
        assert not _wheel_failure(ctx, stdout="* Creating isolated environment\nboom: real cause")

        out = capsys.readouterr().out
        assert "boom: real cause" in out
        assert ctx.results[-1].status == "err"

    def test_stderr_is_still_surfaced(self, capsys):
        ctx = InitContext(project_root=Path("/tmp/x"))
        _wheel_failure(ctx, stderr="stderr side of the story")

        assert "stderr side of the story" in capsys.readouterr().out

    def test_ensurepip_failure_names_its_fix(self, capsys):
        """The exact trap docs/TROUBLESHOOTING.md documents, and the one that killed the
        fpu port's first init: Debian splits venv out of the interpreter."""
        ctx = InitContext(project_root=Path("/tmp/x"))
        _wheel_failure(
            ctx,
            stdout=(
                "* Creating isolated environment: venv+pip...\n"
                "The virtual environment was not created successfully because "
                "ensurepip is not available."
            ),
        )

        out = capsys.readouterr().out
        pyver = f"python{sys.version_info.major}.{sys.version_info.minor}-venv"
        assert f"apt install {pyver}" in out
        assert "--no-isolation" in out

    def test_missing_build_module_keeps_its_own_fix(self, capsys):
        ctx = InitContext(project_root=Path("/tmp/x"))
        _wheel_failure(ctx, stdout="No module named build")

        assert "pip install build" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Step-number drift in comments (fpu F-12)
# ---------------------------------------------------------------------------
# Banner numbers are allocated at print time from the steps that actually run
# (InitContext.step_banner), so a hardcoded "Step 9b" in a section comment goes
# stale the moment a step is added, skipped, or retired. Steps are named by
# their record key instead.

_INIT_MODULES = (
    "init_cmd.py",
    "init_docker_image.py",
    "init_git_hooks.py",
)


def test_init_sources_carry_no_hardcoded_step_numbers() -> None:
    src = Path(init_cmd.__file__).parent
    offenders = []
    for name in _INIT_MODULES:
        for lineno, line in enumerate(
            src.joinpath(name).read_text(encoding="utf-8").splitlines(), 1
        ):
            # `Step 0`-`Step 4` are the booley-setup skill's own steps (a real,
            # user-facing numbering) — only init's internal numbering drifts.
            for m in re.finditer(r"Step (\d+)([a-z]?)", line):
                if int(m.group(1)) > 4 or m.group(2):
                    offenders.append(f"{name}:{lineno}: {line.strip()}")
    assert not offenders, "hardcoded init step numbers drift (F-12):\n" + "\n".join(offenders)
