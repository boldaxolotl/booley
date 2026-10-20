"""`booley init` retires the Project images its adoption superseded."""

from __future__ import annotations

from pathlib import Path

import pytest

from booley.config.sandbox import project_image_name
from booley.harness import init_cmd
from booley.harness.setup.common import InitContext
from booley.runtime import image_lifecycle as lifecycle
from booley.runtime import project_resources
from tests.harness.test_image_lifecycle import FakeDocker


def _id(char: str) -> str:
    return "sha256:" + char * 64


@pytest.fixture
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    project = tmp_path / "chip"
    project.mkdir()
    return project


def test_only_images_dropped_from_project_tags_become_owned(root: Path) -> None:
    from booley.runtime.session_issuance import keeper_image

    identity, family = str(root), project_image_name(root)

    def snapshot(*rows: tuple[str, str]) -> dict[str, str]:
        refs = [lifecycle.ImageReference(ref, image) for image, ref in rows]
        return project_resources.own_tag_snapshot(identity, refs)

    before = snapshot(
        (_id("1"), f"{family}:latest"),  # moves to 9: superseded
        (_id("2"), f"{family}-wheel:latest"),  # unchanged
        (_id("4"), f"{family}-layout-old:latest"),  # obsolete layout tag removed
        (_id("3"), "booley-standard-substrate:latest"),  # shared, moves anyway
        (_id("1"), keeper_image(root)),  # the keeper still pins the old image
    )
    after = snapshot(
        (_id("9"), f"{family}:latest"),
        (_id("2"), f"{family}-wheel:latest"),
        (_id("8"), f"{family}-layout-new:latest"),
        (_id("7"), "booley-standard-substrate:latest"),
        (_id("1"), keeper_image(root)),
    )

    moved = project_resources.record_superseded_images(identity, before, after)

    assert moved == (_id("1"), _id("4"))
    assert lifecycle.OwnedImages(identity).ids == [_id("1"), _id("4")]


def test_init_removes_unused_superseded_images_after_keeper_reissue(
    root: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    unused, running, shared = _id("1"), _id("2"), _id("3")
    docker = FakeDocker(
        {
            unused: (unused, {}),
            running: (running, {}),
            shared: (shared, {}),
            "other-project-booley-sandbox:latest": (shared, {}),
        }
    )
    docker.used_image_ids = frozenset({running})
    monkeypatch.setattr(lifecycle, "_docker_adapter", lambda: docker)
    lifecycle.OwnedImages(root).record({}, (unused, running, shared))
    ctx = InitContext(project_root=root)
    result = lifecycle.LifecycleResult(
        "image", _id("9"), lifecycle.Status.CHANGED, superseded=(unused, running, shared)
    )

    init_cmd._step_retire_superseded_images(ctx, result)

    assert docker.image_ids() == {running, shared}
    output = capsys.readouterr().out
    assert f"{running}: retained (conflict: unable to delete {running})" in output
    assert f"{shared}: retained (tag/keeper)" in output
    assert [(row.name, row.status) for row in ctx.results] == [("superseded_images", "warn")]


def test_init_reports_full_removal_and_cleanup_failure(
    root: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    unused = _id("1")
    docker = FakeDocker({unused: (unused, {})})
    monkeypatch.setattr(lifecycle, "_docker_adapter", lambda: docker)
    lifecycle.OwnedImages(root).record({}, (unused,))
    result = lifecycle.LifecycleResult(
        "image", _id("9"), lifecycle.Status.CHANGED, superseded=(unused,)
    )
    ctx = InitContext(project_root=root)

    init_cmd._step_retire_superseded_images(ctx, result)

    assert docker.image_ids() == frozenset()
    assert f"removed superseded Sandbox Images: {unused}" in capsys.readouterr().out

    def broken(_root: Path) -> tuple[str, ...]:
        raise lifecycle.ImageLifecycleError("ownership store unreadable")

    monkeypatch.setattr(lifecycle, "cleanup_owned_images", broken)
    init_cmd._step_retire_superseded_images(ctx, result)
    assert "ownership store unreadable" in capsys.readouterr().out
    assert [(row.name, row.status) for row in ctx.results] == [
        ("superseded_images", "ok"),
        ("superseded_images", "warn"),
    ]
