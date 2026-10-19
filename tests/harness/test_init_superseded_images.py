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


def test_only_images_dropped_from_project_tags_become_owned(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from booley.runtime.session_issuance import keeper_image

    identity, family = str(root), project_image_name(root)
    rows = [
        (_id("1"), f"{family}:latest"),  # moves to 9: superseded
        (_id("2"), f"{family}-wheel:latest"),  # unchanged
        (_id("4"), f"{family}-layout-old:latest"),  # obsolete layout tag removed
        (_id("3"), "booley-standard-substrate:latest"),  # shared, moves anyway
        (_id("1"), keeper_image(root)),  # the keeper still pins the old image
    ]

    def image_rows():
        return [project_resources._ImageRow(image, ref, "1GB") for image, ref in rows]

    monkeypatch.setattr(project_resources, "_image_rows", image_rows)
    before = project_resources.own_tag_snapshot(identity)
    rows = [
        (_id("9"), f"{family}:latest"),
        (_id("2"), f"{family}-wheel:latest"),
        (_id("8"), f"{family}-layout-new:latest"),
        (_id("7"), "booley-standard-substrate:latest"),
        (_id("1"), keeper_image(root)),
    ]

    assert project_resources.record_superseded_images(identity, before) == (_id("1"), _id("4"))
    assert lifecycle.owned_image_ids(identity) == (_id("1"), _id("4"))


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
    lifecycle.record_owned_images(root, "init-superseded-1", {}, (unused, running, shared))
    ctx = InitContext(project_root=root)
    result = lifecycle.LifecycleResult(
        "image", _id("9"), lifecycle.Status.CHANGED, superseded=(unused, running, shared)
    )

    init_cmd._step_retire_superseded_images(ctx, result)

    assert docker.image_ids() == {running, shared}
    output = capsys.readouterr().out
    assert f"{running}: retained (container)" in output
    assert f"{shared}: retained (tag/keeper)" in output
    assert [(row.name, row.status) for row in ctx.results] == [("superseded_images", "warn")]
