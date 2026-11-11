"""Booley-shipped sandbox flavor registry (booley-sandbox-riscv) and base-image note.

A flavor is Booley's own image, not the user's. These tests pin the shipped
flavor files, the remote tag derivation, and the base-image note init prints
when a project selects a flavor. The image lifecycle builds the flavors; its
tests live in ``test_image_lifecycle.py``.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from booley.harness.setup import docker_image as idi
from booley.harness.setup.common import InitContext
from booley.runtime import project_image as pi
from booley.runtime.dockerfile_syntax import build_context_imports
from tests.docker_cli_guard import route_docker_helper_through_subprocess_run


@pytest.fixture(autouse=True)
def _docker_helper_uses_subprocess_run(monkeypatch):
    """This module fakes the Docker CLI through ``subprocess.run``."""
    route_docker_helper_through_subprocess_run(monkeypatch)


FLAVOR = "booley-sandbox-riscv"


def test_remote_tag_derives_the_flavor_repo():
    assert idi.remote_tag(FLAVOR, "1.2.3") == f"ghcr.io/boldaxolotl/{FLAVOR}:1.2.3"
    assert idi.remote_tag(idi.DOCKER_IMAGE, "1.2.3") == f"{idi.GHCR_IMAGE}:1.2.3"


class TestShippedFlavorFiles:
    def test_every_flavor_dockerfile_is_shipped(self):
        docker_dir = idi.docker_data_dir()
        for image, dockerfile in idi.FLAVOR_IMAGES.items():
            assert (docker_dir / dockerfile).is_file(), f"{image} has no shipped {dockerfile}"

    def test_flavor_dockerfiles_are_copy_free(self):
        """The image lifecycle builds a flavor with data/docker/ as the context so a
        pip-installed Booley (no Booley repository root) can build it — a COPY would break
        that silently, at build time, on someone else's machine. Copying from
        a stage defined in the same file reads no build context (ADR 0070)."""
        docker_dir = idi.docker_data_dir()
        for dockerfile in idi.FLAVOR_IMAGES.values():
            body = (docker_dir / dockerfile).read_text(encoding="utf-8")
            offenders = [item.value for item in build_context_imports(body)]
            assert not offenders, f"{dockerfile} must stay COPY-free: {offenders}"

    def test_flavor_names_can_never_collide_with_a_generated_project_name(self):
        """No repo name can generate a tag that shadows a flavor.

        The two schemes are opposite ends: a generated project image is
        ``<slug>-booley-sandbox`` (suffix), a flavor is ``booley-sandbox-<x>``
        (prefix). If a future flavor breaks that, a user's own project image
        would be mistaken for a shipped flavor by Doctor's flavor checks and the
        base-image note.
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
        # The build-cache report would run ``docker system df``.
        monkeypatch.setattr(idi, "_report_build_cache", lambda *_args, **_kwargs: None)

    def test_base_step_explains_the_flavor_relationship(self, monkeypatch, capsys):
        monkeypatch.setattr(idi.shutil, "which", lambda n: "/usr/bin/docker")
        monkeypatch.setattr(idi, "_docker_image_exists", lambda image=idi.DOCKER_IMAGE: True)
        monkeypatch.setattr(idi, "_image_build_fingerprint", lambda root: None)
        monkeypatch.setattr(
            idi,
            "_image_is_stale",
            lambda fp, expected_version=None: False,
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
            lambda fp, expected_version=None: False,
        )

        idi._step_docker_image(
            InitContext(project_root=Path("/tmp/x")), selected_image=idi.DOCKER_IMAGE
        )

        assert "layers on" not in capsys.readouterr().out
