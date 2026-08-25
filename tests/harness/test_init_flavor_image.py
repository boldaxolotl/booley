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

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from booley.harness import init_cmd
from booley.harness import init_docker_image as idi
from booley.harness.init_common import InitContext
from booley.runtime import project_image as pi

FLAVOR = "booley-sandbox-riscv"


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
    monkeypatch.setattr(init_cmd, "_warn_on_live_session_on_old_image", lambda ctx, image: None)
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


class TestFlavorDispatch:
    def test_flavor_is_not_treated_as_user_managed(self, flavor_repo, monkeypatch, capsys):
        built = _stub_flavor_env(monkeypatch, exists=False, stale=False)
        ctx = InitContext(project_root=flavor_repo)

        init_cmd._step_project_image(ctx)

        assert built == [FLAVOR], "init must build the shipped flavor, not skip it"
        out = capsys.readouterr().out
        assert "user-managed" not in out
        assert ctx.results[-1].status == "ok"

    def test_unknown_image_is_still_user_managed(self, flavor_repo, monkeypatch, capsys):
        (flavor_repo / ".booley_project" / "booley.toml").write_text(
            '[sandbox]\nimage = "acme-custom-sandbox"\n', encoding="utf-8"
        )
        built = _stub_flavor_env(monkeypatch, exists=False, stale=False)
        ctx = InitContext(project_root=flavor_repo)

        init_cmd._step_project_image(ctx)

        assert not built, "a genuinely user-managed image must never be built by init"
        assert "user-managed" in capsys.readouterr().out
        assert ctx.results[-1].status == "skip"

    def test_fresh_flavor_is_left_alone(self, flavor_repo, monkeypatch):
        built = _stub_flavor_env(monkeypatch, exists=True, stale=False)
        ctx = InitContext(project_root=flavor_repo)

        init_cmd._step_project_image(ctx)

        assert not built
        assert ctx.results[-1].status == "skip"


class TestFlavorStaleness:
    def test_stale_flavor_is_rebuilt(self, flavor_repo, monkeypatch, capsys):
        """The derived-image drift fix: a base rebuild restamps the fingerprint,
        which leaves the flavor's label behind -> stale -> rebuilt in the same run."""
        built = _stub_flavor_env(monkeypatch, exists=True, stale=True)
        ctx = InitContext(project_root=flavor_repo)

        init_cmd._step_project_image(ctx)

        assert built == [FLAVOR]
        assert "stale" in capsys.readouterr().out
        assert ctx.results[-1].status == "ok"

    def test_stale_flavor_warns_about_a_live_session(self, flavor_repo, monkeypatch):
        """A rebuild only moves the tag — a container already on the old image
        keeps serving it, which is precisely how this bug hides (F-9)."""
        _stub_flavor_env(monkeypatch, exists=True, stale=True)
        warned: list[str] = []
        monkeypatch.setattr(
            init_cmd, "_warn_on_live_session_on_old_image", lambda ctx, image: warned.append(image)
        )

        init_cmd._step_project_image(InitContext(project_root=flavor_repo))

        assert warned == [FLAVOR]

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

    def test_check_only_never_builds(self, flavor_repo, monkeypatch):
        built = _stub_flavor_env(monkeypatch, exists=True, stale=True)
        ctx = InitContext(project_root=flavor_repo, check_only=True)

        init_cmd._step_project_image(ctx)

        assert not built
        assert ctx.results[-1].status == "warn"


class TestFlavorWithoutCheckout:
    """A pip-installed Booley: Q2 answer — trust a flavor already on disk."""

    def test_present_flavor_is_trusted_when_dockerfile_is_absent(
        self, flavor_repo, monkeypatch, capsys
    ):
        built = _stub_flavor_env(monkeypatch, exists=True, stale=True)
        monkeypatch.setattr(idi, "docker_data_dir", lambda: Path("/nonexistent/docker"))
        pulled: list[str] = []
        monkeypatch.setattr(
            idi, "_try_pull_image", lambda v, image=idi.DOCKER_IMAGE: pulled.append(image) or True
        )
        ctx = InitContext(project_root=flavor_repo)

        init_cmd._step_project_image(ctx)

        assert not built and not pulled, "a present flavor is trusted, not re-pulled"
        assert "trusting it" in capsys.readouterr().out
        assert ctx.results[-1].status == "skip"

    def test_missing_flavor_is_pulled_when_dockerfile_is_absent(self, flavor_repo, monkeypatch):
        _stub_flavor_env(monkeypatch, exists=False, stale=False)
        monkeypatch.setattr(idi, "docker_data_dir", lambda: Path("/nonexistent/docker"))
        pulled: list[str] = []
        monkeypatch.setattr(
            idi, "_try_pull_image", lambda v, image=idi.DOCKER_IMAGE: pulled.append(image) or True
        )
        ctx = InitContext(project_root=flavor_repo)

        init_cmd._step_project_image(ctx)

        assert pulled == [FLAVOR]
        assert ctx.results[-1].status == "ok"

    def test_unbuildable_unpullable_flavor_is_an_error_not_a_skip(self, flavor_repo, monkeypatch):
        """The whole point: never leave the project's image absent *quietly*."""
        _stub_flavor_env(monkeypatch, exists=False, stale=False)
        monkeypatch.setattr(idi, "docker_data_dir", lambda: Path("/nonexistent/docker"))
        monkeypatch.setattr(idi, "_try_pull_image", lambda v, image=idi.DOCKER_IMAGE: False)
        ctx = InitContext(project_root=flavor_repo)

        init_cmd._step_project_image(ctx)

        assert ctx.results[-1].status == "err"

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
        that silently, at build time, on someone else's machine."""
        docker_dir = idi.docker_data_dir()
        for dockerfile in idi.FLAVOR_IMAGES.values():
            body = (docker_dir / dockerfile).read_text(encoding="utf-8")
            offenders = [
                ln for ln in body.splitlines() if ln.strip().upper().startswith(("COPY ", "ADD "))
            ]
            assert not offenders, f"{dockerfile} must stay COPY-free: {offenders}"

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
