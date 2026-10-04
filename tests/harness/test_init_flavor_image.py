"""``booley init`` and the Booley-shipped sandbox flavors (booley-sandbox-riscv).

A flavor is Booley's own image, not the user's. Before FLAVOR_IMAGES existed it
fell into Step 9b's "not the generated name -> user-managed" branch and was
skipped, so a `[sandbox].image = "booley-sandbox-riscv"` project got the worst
possible outcome: init rebuilt the *base* for ~20 minutes and left the image the
project actually runs frozen on the base's previous layers, tag unchanged. These
tests pin the dispatch, the shared-fingerprint staleness that makes that drift
visible, and the no-checkout fallbacks.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

import booley

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from booley.harness import init_cmd
from booley.harness.setup import docker_image as idi
from booley.harness.setup.common import InitContext
from booley.runtime import project_image as pi

FLAVOR = "booley-sandbox-riscv"


def _context_copies(body: str) -> list[str]:
    """Return COPY/ADD lines that read the build context.

    ``COPY --from=<stage>`` naming a stage of the same file reads no context.
    """
    stages = set(re.findall(r"^FROM\s+\S+\s+AS\s+(\S+)\s*$", body, re.I | re.M))
    return [
        line
        for line in body.splitlines()
        if line.strip().upper().startswith(("COPY ", "ADD "))
        and not any(line.strip().startswith(f"COPY --from={stage} ") for stage in stages)
    ]


@pytest.fixture
def flavor_repo(tmp_path: Path, monkeypatch) -> Path:
    """A project selecting the RISC-V flavor, with docker present but stubbed."""
    root = tmp_path / "proj"
    (root / ".booley_project").mkdir(parents=True)
    (root / ".booley_project" / "booley.toml").write_text(
        f'[sandbox]\nimage = "{FLAVOR}"\n', encoding="utf-8"
    )
    monkeypatch.delenv("BOOLEY_PROJECT_DIR", raising=False)
    monkeypatch.setattr(
        init_cmd.shutil, "which", lambda n: "/usr/bin/docker" if n == "docker" else None
    )
    # A live-session probe would shell out to docker; the F-9 warning is not
    # what these tests are about.
    from booley.runtime.project_dir import reset_cache

    reset_cache()
    return root


def _stub_flavor_env(
    monkeypatch,
    *,
    exists: bool,
    stale: bool,
    fingerprint: str | None = "abc123",
) -> list[str]:
    """Stub the flavor's docker probes; returns the list built images land in."""
    built: list[str] = []
    monkeypatch.setattr(idi, "_docker_image_exists", lambda image=idi.DOCKER_IMAGE: exists)
    monkeypatch.setattr(
        idi,
        "_docker_image_id",
        lambda image: "sha256:standard-parent" if image == idi.DOCKER_IMAGE else None,
    )
    monkeypatch.setattr(idi, "_image_build_fingerprint", lambda root: fingerprint)
    monkeypatch.setattr(
        idi,
        "_image_is_stale",
        lambda fp, image=idi.DOCKER_IMAGE, expected_version=None: stale,
    )
    monkeypatch.setattr(idi, "_expected_version", lambda _root: "0.2.0")
    monkeypatch.setattr(idi, "_report_build_cache", lambda: None)

    def _fake_build(ctx, spec):
        built.append(spec.image)
        return 0

    monkeypatch.setattr(idi, "_docker_build_image", _fake_build)
    return built


def test_missing_flavor_binds_verified_standard_parent(flavor_repo, monkeypatch):
    parent_id = "sha256:" + "a" * 64
    captured = []
    monkeypatch.setattr(idi, "_docker_image_exists", lambda _image=idi.DOCKER_IMAGE: False)
    monkeypatch.setattr(idi, "_image_build_fingerprint", lambda _root: "abc123")
    monkeypatch.setattr(idi, "_expected_version", lambda _root: "0.2.0")
    monkeypatch.setattr(idi, "_try_pull_image", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(
        idi,
        "_docker_image_id",
        lambda image: parent_id if image == idi.DOCKER_IMAGE else None,
    )
    monkeypatch.setattr(
        idi,
        "_docker_build_image",
        lambda _context, spec: captured.append(spec) or 0,
    )
    monkeypatch.setattr(idi, "_report_build_cache", lambda: None)

    idi.ensure_flavor_image(
        InitContext(project_root=flavor_repo), FLAVOR, scope=idi.HostImageScope()
    )

    assert len(captured) == 1
    spec = captured[0]
    assert spec.image == FLAVOR
    assert spec.build_contexts == (("booley-standard-substrate", "docker-image://booley-sandbox"),)
    assert spec.parent_artifact == parent_id
    command = idi._docker_build_command(spec)
    assert "booley-standard-substrate=docker-image://booley-sandbox" in command
    assert f"{idi.LABEL_BASE_IMAGE_ID}={parent_id}" in command
    assert f"{idi.LABEL_PARENT_ARTIFACT_KIND}={idi.PARENT_ARTIFACT_LOCAL_IMAGE_ID}" in command
    assert f"{idi.LABEL_PARENT_ARTIFACT}={parent_id}" in command


def test_missing_standard_parent_stops_flavor_build(flavor_repo, monkeypatch):
    _stub_flavor_env(monkeypatch, exists=False, stale=False)
    monkeypatch.setattr(idi, "_try_pull_image", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(idi, "_docker_image_id", lambda _image: None)
    monkeypatch.setattr(
        idi,
        "_docker_build_image",
        lambda *_args, **_kwargs: pytest.fail("build must not start without its parent"),
    )
    ctx = InitContext(project_root=flavor_repo)

    assert idi.ensure_flavor_image(ctx, FLAVOR, scope=idi.HostImageScope()) is False

    assert ctx.results[-1].status == "err"
    assert "standard parent" in ctx.results[-1].detail


class TestFlavorDispatch:
    pass


class TestFlavorStaleness:
    def test_flavor_shares_the_base_fingerprint_label(self, monkeypatch):
        """`_image_is_stale` must read the label off the image it is asked about,
        not always the base — build-riscv.sh stamps the same label on the flavor."""
        seen: list[str] = []

        def _label(image, label):
            seen.append(image)
            return "old-fingerprint"

        monkeypatch.setattr(idi, "_image_label", _label)
        assert idi._image_is_stale("new-fingerprint", FLAVOR) is True
        assert seen == [FLAVOR]

    def test_flavor_on_superseded_base_is_stale(self, monkeypatch):
        labels = {
            idi.LABEL_FINGERPRINT: "same-source",
            idi.LABEL_BASE_IMAGE_ID: "sha256:old-base",
        }
        monkeypatch.setattr(idi, "_image_label", lambda _image, label: labels.get(label))
        monkeypatch.setattr(idi, "_docker_image_id", lambda _image: "sha256:new-base")

        assert idi._image_is_stale("same-source", FLAVOR) is True

    def test_flavor_on_current_base_is_fresh(self, monkeypatch):
        labels = {
            idi.LABEL_FINGERPRINT: "same-source",
            idi.LABEL_BASE_IMAGE_ID: "sha256:current-base",
        }
        monkeypatch.setattr(idi, "_image_label", lambda _image, label: labels.get(label))
        monkeypatch.setattr(idi, "_docker_image_id", lambda _image: "sha256:current-base")

        assert idi._image_is_stale("same-source", FLAVOR) is False

    def test_legacy_flavor_without_base_identity_is_stale(self, monkeypatch):
        monkeypatch.setattr(
            idi,
            "_image_label",
            lambda _image, label: "same-source" if label == idi.LABEL_FINGERPRINT else None,
        )
        monkeypatch.setattr(idi, "_docker_image_id", lambda _image: "sha256:current-base")

        assert idi._image_is_stale("same-source", FLAVOR) is True


class TestFlavorWithoutCheckout:
    """A pip-installed Booley refreshes release-mismatched flavors by version."""

    def test_present_flavor_from_old_release_is_refreshed(
        self, tmp_path, monkeypatch, release_image_docker
    ):
        docker_dir = tmp_path / "site-packages" / "booley" / "data" / "docker"
        docker = release_image_docker(fingerprints={FLAVOR: "pulled:0.2.3"})

        monkeypatch.setattr(booley, "__version__", "0.2.6")
        monkeypatch.setattr(idi, "docker_data_dir", lambda: docker_dir)
        monkeypatch.setattr(idi.subprocess, "run", docker.run)
        ctx = InitContext(project_root=tmp_path)

        changed = idi.ensure_flavor_image(ctx, FLAVOR, scope=idi.HostImageScope())

        assert changed is True
        assert ctx.results[-1].detail == f"flavor {FLAVOR} pulled"
        assert ["docker", "pull", f"ghcr.io/boldaxolotl/{FLAVOR}:0.2.6"] in docker.commands

    def test_unbuildable_old_flavor_warns_when_refresh_fails(
        self, tmp_path, monkeypatch, capsys, release_image_docker
    ):
        docker_dir = tmp_path / "site-packages" / "booley" / "data" / "docker"
        docker = release_image_docker(
            fingerprints={FLAVOR: "pulled:0.2.3"},
            pull_returncode=1,
        )

        monkeypatch.setattr(booley, "__version__", "0.2.6")
        monkeypatch.setattr(idi, "docker_data_dir", lambda: docker_dir)
        monkeypatch.setattr(idi.subprocess, "run", docker.run)
        ctx = InitContext(project_root=tmp_path)

        changed = idi.ensure_flavor_image(ctx, FLAVOR, scope=idi.HostImageScope())

        output = capsys.readouterr().out
        assert changed is False
        assert ctx.results[-1].status == "warn"
        assert ctx.results[-1].detail == f"flavor {FLAVOR} compatible image pull failed"
        assert "v0.2.3" in output
        assert "Booley v0.2.6 requires sandbox image v0.2.6" in output
        assert "trusting it" not in output

    def test_check_only_reports_release_pull_not_source_rebuild(
        self, tmp_path, monkeypatch, capsys, release_image_docker
    ):
        docker_dir = tmp_path / "site-packages" / "booley" / "data" / "docker"
        docker = release_image_docker(fingerprints={FLAVOR: "pulled:0.2.3"})
        monkeypatch.setattr(booley, "__version__", "0.2.6")
        monkeypatch.setattr(idi, "docker_data_dir", lambda: docker_dir)
        monkeypatch.setattr(idi.subprocess, "run", docker.run)
        ctx = InitContext(project_root=tmp_path, check_only=True)

        changed = idi.ensure_flavor_image(ctx, FLAVOR, scope=idi.HostImageScope())

        output = capsys.readouterr().out
        assert changed is False
        assert ctx.results[-1].detail == "would pull compatible image"
        assert f"would pull compatible image for the {FLAVOR} sandbox flavor" in output
        assert "rebuild (stale)" not in output

    def test_old_flavor_is_not_rebuilt_when_base_refresh_warns(
        self, tmp_path, monkeypatch, capsys, release_image_docker
    ):
        docker_dir = tmp_path / "site-packages" / "booley" / "data" / "docker"
        docker_dir.mkdir(parents=True)
        (docker_dir / idi.FLAVOR_IMAGES[FLAVOR]).write_text(
            f"FROM {idi.DOCKER_IMAGE}\n", encoding="utf-8"
        )
        docker = release_image_docker(
            fingerprints={
                FLAVOR: "pulled:0.2.3",
                idi.DOCKER_IMAGE: "pulled:0.2.3",
            },
            pull_returncode=1,
        )
        built: list[str] = []
        monkeypatch.setattr(booley, "__version__", "0.2.6")
        monkeypatch.setattr(idi, "docker_data_dir", lambda: docker_dir)
        monkeypatch.setattr(idi.shutil, "which", lambda _name: "/usr/bin/docker")
        monkeypatch.setattr(idi.subprocess, "run", docker.run)
        monkeypatch.setattr(
            idi,
            "_docker_build_image",
            lambda _ctx, spec: built.append(spec.image) or 0,
        )
        ctx = InitContext(project_root=tmp_path)

        changed = idi.ensure_flavor_image(
            ctx,
            FLAVOR,
            scope=idi.HostImageScope(),
            ensure_base=lambda: idi._step_docker_image(ctx, FLAVOR),
        )

        output = capsys.readouterr().out
        assert changed is False
        assert not built
        assert ctx.results[-1].status == "warn"
        assert ctx.results[-1].detail == f"flavor {FLAVOR} compatible image pull failed"
        assert f"{FLAVOR} is v0.2.3" in output
        assert "may be incompatible" in output

    def test_remote_tag_derives_the_flavor_repo(self):
        assert idi.remote_tag(FLAVOR, "1.2.3") == f"ghcr.io/boldaxolotl/{FLAVOR}:1.2.3"
        assert idi.remote_tag(idi.DOCKER_IMAGE, "1.2.3") == f"{idi.GHCR_IMAGE}:1.2.3"


class TestShippedFlavorFiles:
    def test_every_flavor_dockerfile_is_shipped(self):
        docker_dir = idi.docker_data_dir()
        for image, dockerfile in idi.FLAVOR_IMAGES.items():
            assert (docker_dir / dockerfile).is_file(), f"{image} has no shipped {dockerfile}"

    def test_flavor_dockerfiles_are_copy_free(self):
        """init builds a flavor with data/docker/ as the context so a
        pip-installed Booley (no repo root) can build it — a COPY would break
        that silently, at build time, on someone else's machine. Copying from
        a stage defined in the same file reads no build context (ADR 0070)."""
        docker_dir = idi.docker_data_dir()
        for dockerfile in idi.FLAVOR_IMAGES.values():
            body = (docker_dir / dockerfile).read_text(encoding="utf-8")
            offenders = _context_copies(body)
            assert not offenders, f"{dockerfile} must stay COPY-free: {offenders}"

    def test_copy_free_check_rejects_context_copies_beside_stage_copies(self, tmp_path):
        body = "FROM x@sha256:1 AS tools\nFROM base\nCOPY --from=tools /a /a\nCOPY src/ /src\n"
        assert _context_copies(body) == ["COPY src/ /src"]

    def test_flavor_names_can_never_collide_with_a_generated_project_name(self):
        """No repo name can generate a tag that shadows a flavor.

        The two schemes are opposite ends: a generated project image is
        ``<slug>-booley-sandbox`` (suffix), a flavor is ``booley-sandbox-<x>``
        (prefix). If a future flavor breaks that, `_selected_image_handled`
        would route a user's own project image into the flavor branch and
        rebuild it from a shipped Dockerfile.
        """
        for image in idi.FLAVOR_IMAGES:
            assert image.startswith(f"{pi.BASE_IMAGE}-")
            assert not image.endswith(f"-{pi.BASE_IMAGE}"), (
                f"flavor {image!r} looks like a generated project image name"
            )


class TestBaseImageNote:
    @pytest.fixture(autouse=True)
    def _disable_installed_image_refresh(self, monkeypatch):
        """Keep presentation tests independent of Docker registry availability."""
        monkeypatch.setattr(idi, "_refresh_installed_base_image", lambda *_args: False)

    def test_base_step_explains_the_flavor_relationship(self, monkeypatch, capsys):
        monkeypatch.setattr(idi.shutil, "which", lambda n: "/usr/bin/docker")
        monkeypatch.setattr(idi, "_docker_image_exists", lambda image=idi.DOCKER_IMAGE: True)
        monkeypatch.setattr(idi, "_image_build_fingerprint", lambda root: None)
        monkeypatch.setattr(
            idi,
            "_image_is_stale",
            lambda fp, image=idi.DOCKER_IMAGE, expected_version=None: False,
        )

        idi._step_docker_image(InitContext(project_root=Path("/tmp/x")), selected_image=FLAVOR)

        out = capsys.readouterr().out
        assert FLAVOR in out and "layers on" in out

    def test_base_step_is_quiet_when_the_project_runs_the_base(self, monkeypatch, capsys):
        monkeypatch.setattr(idi.shutil, "which", lambda n: "/usr/bin/docker")
        monkeypatch.setattr(idi, "_docker_image_exists", lambda image=idi.DOCKER_IMAGE: True)
        monkeypatch.setattr(idi, "_image_build_fingerprint", lambda root: None)
        monkeypatch.setattr(
            idi,
            "_image_is_stale",
            lambda fp, image=idi.DOCKER_IMAGE, expected_version=None: False,
        )

        idi._step_docker_image(
            InitContext(project_root=Path("/tmp/x")), selected_image=idi.DOCKER_IMAGE
        )

        assert "layers on" not in capsys.readouterr().out
