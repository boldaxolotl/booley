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

import booley
from booley.harness import init_cmd, init_docker_image
from booley.harness.init_common import InitContext
from booley.runtime.docker_build import DockerBuildResult


def test_init_cmd_reexports_docker_compatibility_helpers() -> None:
    assert init_cmd._iter_fingerprint_files is init_docker_image._iter_fingerprint_files
    assert init_cmd._stamp_image_fingerprint is init_docker_image._stamp_image_fingerprint
    assert init_cmd._step_docker_image is init_docker_image._step_docker_image


def _seed_source(root: Path) -> None:
    """Minimal repo layout that :func:`_image_build_fingerprint` hashes."""
    pkg = root / "src" / "booley"
    pkg.mkdir(parents=True)
    (pkg / "incontainer_register.py").write_text("x = 1\n", encoding="utf-8")
    (root / ".dockerignore").write_text(".git\n", encoding="utf-8")
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

    def test_changes_when_dockerignore_changes(self, tmp_path):
        _seed_source(tmp_path)
        before = init_cmd._image_build_fingerprint(tmp_path)
        (tmp_path / ".dockerignore").write_text(".git\ndist\n", encoding="utf-8")
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
    (tmp_path / "VERSION").write_text("1.2.3\n", encoding="utf-8")
    (tmp_path / ".git").mkdir()

    args = init_docker_image._image_build_metadata_args(tmp_path)
    values = set(args[1::2])

    assert "BOOLEY_VERSION=1.2.3" in values
    assert "BOOLEY_SOURCE_REVISION=abc123" in values
    assert "BOOLEY_SOURCE_UPDATED_AT=2026-08-10T10:00:00Z" in values
    assert any(value.startswith("BOOLEY_IMAGE_BUILT_AT=") for value in values)


def test_docker_build_command_reuses_local_parent_labels(tmp_path, monkeypatch):
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text("FROM scratch\n", encoding="utf-8")
    parent_id = "sha256:" + "a" * 64
    direct_spec = init_docker_image._DockerBuildSpec(
        dockerfile=dockerfile,
        context=tmp_path,
        exists=False,
        image="project-image",
        parent_artifact=parent_id,
    )

    direct_command = init_docker_image._docker_build_command(direct_spec)

    assert (
        f"{init_docker_image.LABEL_PARENT_ARTIFACT_KIND}="
        f"{init_docker_image.PARENT_ARTIFACT_LOCAL_IMAGE_ID}"
    ) in direct_command
    assert f"{init_docker_image.LABEL_PARENT_ARTIFACT}={parent_id}" in direct_command

    monkeypatch.setattr(init_docker_image, "_docker_image_id", lambda _image: parent_id)
    flavor_spec = init_docker_image._DockerBuildSpec(
        dockerfile=dockerfile,
        context=tmp_path,
        exists=False,
        image="booley-sandbox-riscv",
    )

    flavor_command = init_docker_image._docker_build_command(flavor_spec)

    assert f"{init_docker_image.LABEL_BASE_IMAGE_ID}={parent_id}" in flavor_command
    assert f"{init_docker_image.LABEL_PARENT_ARTIFACT}={parent_id}" in flavor_command


def test_local_build_constructs_base_before_candidate_with_named_context(
    tmp_path, monkeypatch
) -> None:
    docker_dir = tmp_path / "src" / "booley" / "data" / "docker"
    docker_dir.mkdir(parents=True)
    (docker_dir / "Dockerfile.base").write_text("FROM scratch\n", encoding="utf-8")
    (docker_dir / "Dockerfile").write_text("FROM booley-runtime-base\n", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text("[project]\nname='booley'\n", encoding="utf-8")
    calls = []

    monkeypatch.setattr(init_docker_image, "_docker_build_wheel", lambda *_args: True)
    monkeypatch.setattr(init_docker_image, "_docker_image_exists", lambda *_args: False)
    monkeypatch.setattr(
        init_docker_image, "_docker_image_id", lambda _image: "sha256:runtime-base"
    )
    monkeypatch.setattr(init_docker_image, "_report_build_cache", lambda: None)
    monkeypatch.setattr(init_docker_image, "_runtime_base_build_metadata_args", lambda _root: [])

    def fake_build(*args, **kwargs):
        calls.append((args, kwargs))
        return 0

    monkeypatch.setattr(init_docker_image, "_docker_build_image", fake_build)
    ctx = InitContext(project_root=tmp_path)

    init_docker_image._docker_local_build(ctx, docker_dir, exists=False, fingerprint="fp")

    base = calls[0][0][1]
    candidate = calls[1][0][1]
    assert base.dockerfile.name == "Dockerfile.base"
    assert base.image == "booley-runtime-base:local"
    assert candidate.dockerfile.name == "Dockerfile"
    assert candidate.build_contexts == (
        ("booley-runtime-base", "docker-image://booley-runtime-base:local"),
    )
    assert candidate.parent_artifact == "sha256:runtime-base"
    assert candidate.build_args == (
        "--build-arg",
        "BOOLEY_RUNTIME_BASE_IMAGE=sha256:runtime-base",
    )


# ---------------------------------------------------------------------------
# _image_is_stale
# ---------------------------------------------------------------------------


class TestIsStale:
    def test_no_fingerprint_is_not_stale(self, monkeypatch):
        # None fingerprint disables only source drift; init checks release compatibility.
        monkeypatch.setattr(init_docker_image, "_image_label", lambda *a: "anything")
        assert init_docker_image._image_is_stale(None) is False

    def test_unlabeled_image_is_stale(self, monkeypatch):
        # The exact bug: a pre-guard image carries no fingerprint label.
        monkeypatch.setattr(init_docker_image, "_image_label", lambda *a: None)
        assert init_docker_image._image_is_stale("abc123") is True

    def test_matching_label_is_current(self, monkeypatch):
        monkeypatch.setattr(init_docker_image, "_image_label", lambda *a: "abc123")
        assert init_docker_image._image_is_stale("abc123") is False

    def test_matching_source_with_wrong_version_is_stale(self, monkeypatch):
        labels = {
            init_docker_image.LABEL_FINGERPRINT: "abc123",
            init_docker_image.LABEL_VERSION: "0.1.0",
        }
        monkeypatch.setattr(
            init_docker_image, "_image_label", lambda _image, label: labels.get(label)
        )

        assert init_docker_image._image_is_stale("abc123", expected_version="0.2.0") is True

    def test_differing_label_is_stale(self, monkeypatch):
        monkeypatch.setattr(init_docker_image, "_image_label", lambda *a: "old999")
        assert init_docker_image._image_is_stale("abc123") is True

    def test_pulled_image_is_left_alone(self, monkeypatch):
        # A deliberately pulled pre-built image can't match a local fingerprint;
        # its pulled:* provenance marks it as intentional, not stale.
        monkeypatch.setattr(init_docker_image, "_image_label", lambda *a: "pulled:0.1.0")
        assert init_docker_image._image_is_stale("abc123") is False

    def test_pulled_image_from_old_release_is_stale(self, monkeypatch):
        monkeypatch.setattr(init_docker_image, "_image_label", lambda *a: "pulled:0.1.0")
        assert init_docker_image._image_is_stale("abc123", expected_version="0.2.0") is True


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
        monkeypatch.setattr(init_docker_image, "_source_version", lambda _root: None)
        assert init_docker_image.source_fingerprint_mismatch("img") is False

    def test_differing_label_is_stale(self, monkeypatch):
        self._patch(monkeypatch, fingerprint="abc", label="old")
        assert init_docker_image.source_fingerprint_mismatch("img") is True


def test_checkout_version_overrides_stale_distribution_metadata(tmp_path, monkeypatch):
    (tmp_path / "VERSION").write_text("0.2.0\n", encoding="utf-8")
    monkeypatch.setattr(init_docker_image, "_read_version", lambda: "0.1.0")

    assert init_docker_image._expected_version(tmp_path) == "0.2.0"


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
# Installed-package image compatibility
# ---------------------------------------------------------------------------


def _run_pip_image_step(tmp_path, monkeypatch, docker, *, check_only=False):
    docker_dir = tmp_path / "site-packages" / "booley" / "data" / "docker"
    monkeypatch.setattr(booley, "__version__", "0.2.6")
    monkeypatch.setattr(init_docker_image, "docker_data_dir", lambda: docker_dir)
    monkeypatch.setattr(init_docker_image.shutil, "which", lambda _name: "/usr/bin/docker")
    monkeypatch.setattr(init_docker_image.subprocess, "run", docker.run)
    ctx = InitContext(project_root=tmp_path, check_only=check_only)

    init_docker_image._step_docker_image(ctx)
    return ctx


def test_pip_install_refreshes_existing_image_from_old_release(
    tmp_path, monkeypatch, release_image_docker
):
    """A pip install has no checkout fingerprint, but its release still pins the image."""
    docker = release_image_docker(fingerprints={init_docker_image.DOCKER_IMAGE: "pulled:0.2.3"})

    ctx = _run_pip_image_step(tmp_path, monkeypatch, docker)

    assert ctx.results[-1].status == "ok"
    assert ctx.results[-1].detail == "pulled"
    assert ["docker", "pull", "ghcr.io/boldaxolotl/booley-sandbox:0.2.6"] in docker.commands


def test_pip_install_warns_when_compatible_image_pull_fails(
    tmp_path, monkeypatch, capsys, release_image_docker
):
    docker = release_image_docker(
        fingerprints={init_docker_image.DOCKER_IMAGE: "pulled:0.2.3"},
        pull_returncode=1,
    )

    ctx = _run_pip_image_step(tmp_path, monkeypatch, docker)

    output = capsys.readouterr().out
    assert ctx.results[-1].status == "warn"
    assert ctx.results[-1].detail == "compatible image pull failed"
    assert "v0.2.3" in output
    assert "Booley v0.2.6 requires sandbox image v0.2.6" in output
    assert "may be incompatible" in output


def test_pip_install_skips_image_from_matching_release(
    tmp_path, monkeypatch, release_image_docker
):
    docker = release_image_docker(fingerprints={init_docker_image.DOCKER_IMAGE: "pulled:0.2.6"})

    ctx = _run_pip_image_step(tmp_path, monkeypatch, docker)

    assert ctx.results[-1].status == "skip"
    assert not any(command[:2] == ["docker", "pull"] for command in docker.commands)


def test_pip_install_uses_oci_version_when_pull_stamp_is_absent(
    tmp_path, monkeypatch, release_image_docker
):
    docker = release_image_docker(
        fingerprints={init_docker_image.DOCKER_IMAGE: "abc123"},
        oci_versions={init_docker_image.DOCKER_IMAGE: "0.2.3"},
    )

    ctx = _run_pip_image_step(tmp_path, monkeypatch, docker)

    assert ctx.results[-1].detail == "pulled"
    assert ["docker", "pull", "ghcr.io/boldaxolotl/booley-sandbox:0.2.6"] in docker.commands


def test_pip_install_check_only_warns_for_unverifiable_image(
    tmp_path, monkeypatch, capsys, release_image_docker
):
    docker = release_image_docker(fingerprints={init_docker_image.DOCKER_IMAGE: None})

    ctx = _run_pip_image_step(tmp_path, monkeypatch, docker, check_only=True)

    output = capsys.readouterr().out
    assert ctx.results[-1].status == "warn"
    assert ctx.results[-1].detail == "would pull compatible image"
    assert "unknown version" in output
    assert "would pull the Booley v0.2.6 image" in output
    assert not any(command[:2] == ["docker", "pull"] for command in docker.commands)


def test_checkout_source_drift_still_rebuilds_instead_of_pulling(tmp_path, monkeypatch):
    _seed_source(tmp_path)
    docker_dir = tmp_path / "src" / "booley" / "data" / "docker"
    commands: list[list[str]] = []

    def fake_run(command, **_kwargs):
        commands.append(command)
        if command == ["docker", "image", "inspect", init_docker_image.DOCKER_IMAGE]:
            return subprocess.CompletedProcess(command, 0, "", "")
        if command[:3] == ["docker", "image", "inspect"]:
            return subprocess.CompletedProcess(command, 0, "old-fingerprint\n", "")
        raise AssertionError(f"unexpected Docker command: {command}")

    monkeypatch.setattr(init_docker_image, "docker_data_dir", lambda: docker_dir)
    monkeypatch.setattr(init_docker_image.shutil, "which", lambda _name: "/usr/bin/docker")
    monkeypatch.setattr(init_docker_image.subprocess, "run", fake_run)
    ctx = InitContext(project_root=tmp_path, check_only=True)

    init_docker_image._step_docker_image(ctx)

    assert ctx.results[-1].status == "warn"
    assert ctx.results[-1].detail == "would rebuild (stale)"
    assert not any(command[:2] == ["docker", "pull"] for command in commands)


# ---------------------------------------------------------------------------
# Registry pull timeout
# ---------------------------------------------------------------------------


class TestImagePull:
    def test_large_image_gets_two_hour_default(self, monkeypatch):
        calls: list[tuple[list[str], int]] = []

        class _Result:
            returncode = 1

        def _run(cmd, **kwargs):
            calls.append((cmd, kwargs["timeout"]))
            return _Result()

        monkeypatch.delenv("BOOLEY_IMAGE_PULL_TIMEOUT", raising=False)
        monkeypatch.setattr(init_docker_image.subprocess, "run", _run)

        assert init_docker_image._try_pull_image("0.2.0") is False
        assert calls == [
            (
                ["docker", "pull", "ghcr.io/boldaxolotl/booley-sandbox:0.2.0"],
                init_docker_image.DEFAULT_IMAGE_PULL_TIMEOUT_S,
            )
        ]

    def test_pull_timeout_can_be_overridden(self, monkeypatch):
        seen: list[int] = []

        def _run(cmd, **kwargs):
            seen.append(kwargs["timeout"])
            raise subprocess.TimeoutExpired(cmd, kwargs["timeout"])

        monkeypatch.setenv("BOOLEY_IMAGE_PULL_TIMEOUT", "900")
        monkeypatch.setattr(init_docker_image.subprocess, "run", _run)

        assert init_docker_image._try_pull_image("0.2.0") is False
        assert seen == [900]

    def test_tag_timeout_is_not_reported_as_a_pull_timeout(self, monkeypatch):
        calls: list[list[str]] = []
        warnings: list[str] = []

        class _Result:
            returncode = 0

        def _run(cmd, **kwargs):
            calls.append(cmd)
            if cmd[:2] == ["docker", "tag"]:
                raise subprocess.TimeoutExpired(cmd, kwargs["timeout"])
            return _Result()

        monkeypatch.setattr(init_docker_image.subprocess, "run", _run)
        monkeypatch.setattr(init_docker_image, "warn", warnings.append)

        assert init_docker_image._try_pull_image("0.2.0") is False
        assert [call[:2] for call in calls] == [["docker", "pull"], ["docker", "tag"]]
        assert any("docker tag timed out after 30 seconds" in message for message in warnings)
        assert not any("pull timed out" in message for message in warnings)

    def test_invalid_pull_timeout_uses_default(self, monkeypatch):
        monkeypatch.setenv("BOOLEY_IMAGE_PULL_TIMEOUT", "never")
        warnings: list[str] = []
        monkeypatch.setattr(init_docker_image, "warn", warnings.append)

        assert (
            init_docker_image._image_pull_timeout_seconds()
            == init_docker_image.DEFAULT_IMAGE_PULL_TIMEOUT_S
        )
        assert any("invalid BOOLEY_IMAGE_PULL_TIMEOUT" in message for message in warnings)


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
        monkeypatch.setattr(
            init_docker_image,
            "_image_is_stale",
            lambda _fingerprint, **_kwargs: False,
        )
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
            proc = real_popen([sys.executable, "-c", code], **kwargs)
            captured["proc"] = proc
            return proc

        monkeypatch.setattr(init_docker_image.subprocess, "Popen", fake_popen)
        ctx = init_cmd.InitContext(project_root=tmp_path, force=force, verbose=False)
        build = init_docker_image._DockerBuildSpec(
            dockerfile=tmp_path / "Dockerfile", context=tmp_path, exists=False
        )
        rc = init_docker_image._docker_build_image(ctx, build)
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
        assert captured["proc"].stdout.closed

    def test_force_rebuild_keeps_docker_layer_cache(self, monkeypatch, tmp_path: Path):
        _rc, captured = self._run(monkeypatch, tmp_path, b">>> ok\n", force=True)

        assert captured["cmd"][:2] == ["docker", "build"]
        assert "--no-cache" not in captured["cmd"]

    def test_failure_renders_retained_diagnostics_once(self, monkeypatch, tmp_path, capsys):
        monkeypatch.setattr(
            init_docker_image,
            "run_docker_build",
            lambda *_args, **_kwargs: DockerBuildResult(
                1, diagnostics=("ERROR: checksum mismatch",)
            ),
            raising=False,
        )
        ctx = InitContext(project_root=tmp_path)
        build = init_docker_image._DockerBuildSpec(
            dockerfile=tmp_path / "Dockerfile", context=tmp_path, exists=False
        )

        assert init_docker_image._docker_build_image(ctx, build) == 1

        assert capsys.readouterr().out.count("ERROR: checksum mismatch") == 1

    def test_timeout_records_build_failure(self, monkeypatch, tmp_path, capsys):
        monkeypatch.setattr(
            init_docker_image,
            "run_docker_build",
            lambda *_args, **_kwargs: DockerBuildResult(
                None, timed_out=True, diagnostics=("last build output",)
            ),
        )
        ctx = InitContext(project_root=tmp_path)
        build = init_docker_image._DockerBuildSpec(
            dockerfile=tmp_path / "Dockerfile", context=tmp_path, exists=False
        )

        assert init_docker_image._docker_build_image(ctx, build) is None

        assert ctx.results[-1].status == "err"
        assert ctx.results[-1].detail == "build timed out"
        assert "last build output" in capsys.readouterr().out


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
        """The exact trap docs/user/TROUBLESHOOTING.md documents, and the one that killed the
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
