"""`init` and `session refresh` report `pip_requirements` lines the image leaves out (#2574)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

import booley
from booley.harness import image_lifecycle as harness_lifecycle
from booley.harness import init_cmd
from booley.harness.setup.common import InitContext
from booley.runtime import image_lifecycle as lifecycle
from booley.runtime import project_image as pi
from booley.runtime.version_attribution import VersionOrigin
from tests.harness.test_image_lifecycle import FakeDocker

LEFT_OUT_REQUIREMENTS = "-e ./local\nfusesoc==2.3\ncocotb==1.5.1\nnumpy\n"


def _project(tmp_path: Path, requirements: str, *, image: str | None = None) -> Path:
    root = tmp_path / "project"
    (root / ".booley").mkdir(parents=True)
    (root / "requirements.txt").write_text(requirements, encoding="utf-8")
    toml = '[sandbox]\npip_requirements = ["requirements.txt"]\n'
    if image is not None:
        toml += f'image = "{image}"\n'
    (root / ".booley" / "booley.toml").write_text(toml, encoding="utf-8")
    return root


def _stub_base_image(monkeypatch: pytest.MonkeyPatch, packages: dict[str, str]) -> None:
    monkeypatch.setattr(pi, "base_image_packages", lambda *a, **k: packages)


def _current_result() -> lifecycle.LifecycleResult:
    return lifecycle.LifecycleResult("booley-sandbox", None, lifecycle.Status.CURRENT)


def test_init_check_warns_once_through_real_planning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _project(tmp_path, LEFT_OUT_REQUIREMENTS)
    monkeypatch.setattr(
        booley,
        "version_attribution",
        SimpleNamespace(origin=VersionOrigin.DISTRIBUTION, version="0.2.6"),
    )
    monkeypatch.setattr(harness_lifecycle, "embedded_official_release", lambda: True)
    monkeypatch.setattr(harness_lifecycle, "_docker_adapter", lambda: FakeDocker({}))
    monkeypatch.setattr(
        lifecycle,
        "_expected_image_build_contracts",
        lambda: lifecycle.ImageBuildContracts("a" * 64, "b" * 64),
    )
    monkeypatch.setattr(lifecycle, "_expected_wheel_source_fingerprint", lambda: "wheel")

    def forbidden(*_args, **_kwargs):
        raise AssertionError("a check-only init must not run a container")

    monkeypatch.setattr(pi, "base_image_packages", forbidden)
    computed = []
    real_body = lifecycle._project_requirements_body
    monkeypatch.setattr(
        lifecycle,
        "_project_requirements_body",
        lambda project_root: computed.append(project_root) or real_body(project_root),
    )

    init_cmd._reconcile_initialized_image(InitContext(project_root=root, check_only=True))

    out = capsys.readouterr().out
    assert len(computed) > 1, "planning should compute the requirements body repeatedly"
    assert out.count("'-e ./local'") == 1
    assert "install it at runtime" in out
    assert out.count("'fusesoc==2.3'") == 1
    assert "image's version wins" in out
    assert "project wins" not in out


def test_init_notes_each_curated_override_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _project(tmp_path, LEFT_OUT_REQUIREMENTS)
    _stub_base_image(monkeypatch, {"cocotb": "2.1.0", "numpy": "2.0"})
    monkeypatch.setattr(
        init_cmd.image_lifecycle, "reconcile_planned", lambda *a, **k: _current_result()
    )

    init_cmd._reconcile_initialized_image(InitContext(project_root=root))

    out = capsys.readouterr().out
    assert out.count("pin 'cocotb==1.5.1' overrides") == 1
    assert out.count("(project wins)") == 1
    assert out.count("'-e ./local'") == 1
    assert out.count("'fusesoc==2.3'") == 1
    assert "numpy" not in out


def test_session_refresh_prepare_reports_left_out_lines(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _project(tmp_path, LEFT_OUT_REQUIREMENTS)
    _stub_base_image(monkeypatch, {"cocotb": "2.1.0"})
    plan = SimpleNamespace(project_root=root, nodes=(), steps=())
    prepared = object()
    monkeypatch.setattr(init_cmd.image_lifecycle, "plan", lambda *_a: plan)
    monkeypatch.setattr(init_cmd.image_lifecycle, "prepare", lambda *_a, **_k: prepared)

    assert init_cmd.prepare_runtime_image(root) is prepared

    out = capsys.readouterr().out
    assert out.count("'-e ./local'") == 1
    assert out.count("'fusesoc==2.3'") == 1
    assert out.count("pin 'cocotb==1.5.1' overrides") == 1


@pytest.mark.parametrize(
    "shape", ["clean", "external-image", "hand-written-dockerfile", "hand-edited-requirements"]
)
def test_no_new_output_without_left_out_lines(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    shape: str,
) -> None:
    if shape == "clean":
        root = _project(tmp_path, "numpy\ncocotb==2.1.0\n")
    elif shape == "external-image":
        root = _project(tmp_path, LEFT_OUT_REQUIREMENTS, image="example/custom")
    elif shape == "hand-edited-requirements":
        root = _project(tmp_path, LEFT_OUT_REQUIREMENTS)
        docker_dir = root / ".booley" / "docker"
        docker_dir.mkdir()
        (docker_dir / "requirements.txt").write_text("numpy\n", encoding="utf-8")
    else:
        root = _project(tmp_path, LEFT_OUT_REQUIREMENTS)
        docker_dir = root / ".booley" / "docker"
        docker_dir.mkdir()
        (docker_dir / "Dockerfile").write_text("FROM booley-sandbox\n", encoding="utf-8")
    _stub_base_image(monkeypatch, {"cocotb": "2.1.0"})
    monkeypatch.setattr(
        init_cmd.image_lifecycle, "reconcile_planned", lambda *a, **k: _current_result()
    )

    init_cmd._reconcile_initialized_image(InitContext(project_root=root))

    out = capsys.readouterr().out
    assert "left out" not in out
    assert "project wins" not in out
