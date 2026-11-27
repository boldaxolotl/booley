"""Simulation Record artifact references stay strict while their base may move."""

import shutil

import pytest

from booley.flows.sim.record import (
    ArtifactReferenceError,
    build_artifact_reference,
    encode_artifact_reference,
    resolve_artifact_reference,
)
from tests.fusesoc_test_support import symlinked_booley_dir


def _build_origin(artifact, base, *, base_name="origin_target"):
    """A reference to *artifact* under *base*, as the Simulation Record manifest is referenced."""
    return build_artifact_reference(
        artifact,
        base_name=base_name,
        base=base,
        kind="simulation_record_manifest",
        owner="record-id",
        maximum=1024,
    )


def _reference(tmp_path):
    base = tmp_path / "origin"
    artifact = base / "record/manifest.json"
    artifact.parent.mkdir(parents=True)
    artifact.write_bytes(b'{"record":true}\n')
    return base, encode_artifact_reference(_build_origin(artifact, base))


def _resolve_origin(reference, bases, *, maximum=1024):
    return resolve_artifact_reference(
        reference,
        bases=bases,
        expected_kind="simulation_record_manifest",
        expected_owner="record-id",
        maximum=maximum,
    )


@pytest.fixture
def linked_base(tmp_path):
    """A base whose ``record`` entry links to a directory outside it holding a manifest."""
    base = tmp_path / "origin"
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "manifest.json").write_bytes(b'{"record":true}\n')
    base.mkdir()
    (base / "record").symlink_to(outside, target_is_directory=True)
    return base


@pytest.mark.parametrize("layout", ["copy", "hardlink"])
def test_reference_resolves_after_complete_base_copy(tmp_path, layout):
    base, reference = _reference(tmp_path)
    if layout == "copy":
        resolving = tmp_path / "copied"
        shutil.copytree(base, resolving)
    else:
        resolving = base
        (base / "record/manifest-alias.json").hardlink_to(base / "record/manifest.json")

    resolved = _resolve_origin(reference, {"origin_target": resolving})

    assert resolved.path == resolving / "record/manifest.json"
    assert resolved.raw == b'{"record":true}\n'


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("path_base", "reports_root"),
        ("bytes", 0),
        ("sha256", "sha256:" + "0" * 64),
        ("kind", "other"),
        ("owner", "other"),
    ],
)
def test_reference_rejects_wrong_profile_or_identity(tmp_path, field, value):
    base, reference = _reference(tmp_path)
    hostile = {**reference, field: value}

    with pytest.raises(ArtifactReferenceError):
        _resolve_origin(hostile, {"origin_target": base})


def test_reference_read_trusts_the_form_booley_recorded(tmp_path):
    base, reference = _reference(tmp_path)
    recorded = {**reference, "path": "record/./manifest.json", "extra_field": None}

    resolved = _resolve_origin(recorded, {"origin_target": base})

    assert resolved.raw == b'{"record":true}\n'


def test_reference_rejects_link_below_base(tmp_path, linked_base):
    _base, reference = _reference(tmp_path / "valid")

    with pytest.raises(ArtifactReferenceError, match=r"link|escapes"):
        _resolve_origin(reference, {"origin_target": linked_base})


@pytest.mark.parametrize(
    ("artifact", "message"),
    [
        ("origin/record/manifest.json", r"link|escapes"),
        ("outside/manifest.json", "outside its declared base"),
    ],
)
def test_reference_encoder_rejects_link_below_base(tmp_path, linked_base, artifact, message):
    with pytest.raises(ArtifactReferenceError, match=message):
        _build_origin(tmp_path / artifact, linked_base)


def test_reference_rejects_unavailable_base(tmp_path):
    _base, reference = _reference(tmp_path)

    with pytest.raises(ArtifactReferenceError, match="base is unavailable"):
        _resolve_origin(reference, {})


def test_reference_rejects_missing_and_oversized_artifacts(tmp_path):
    base, reference = _reference(tmp_path)

    with pytest.raises(ArtifactReferenceError, match="exceeds size limit"):
        _resolve_origin(reference, {"origin_target": base}, maximum=4)
    (base / "record/manifest.json").unlink()
    with pytest.raises(ArtifactReferenceError, match="not a regular file"):
        _resolve_origin(reference, {"origin_target": base})


@pytest.mark.parametrize("layout", ["entry", "below"])
def test_reference_follows_only_the_booley_dir_entry(tmp_path, layout):
    """#2191: a symlinked ``.booley`` entry is followed; a link below it stays refused."""
    project = tmp_path / "project"
    project.mkdir()
    external = symlinked_booley_dir(project, tmp_path / "external-booley")
    entry = project / ".booley"
    reports = entry / "flow-reports"
    if layout == "entry":
        (external / "flow-reports").mkdir()
    else:
        # ``flow-reports`` itself is a link to a real directory: refused as today.
        (tmp_path / "real-reports").mkdir()
        (external / "flow-reports").symlink_to(tmp_path / "real-reports", target_is_directory=True)
    origin = reports / "sim/1/targets/sim/record/manifest.json"
    origin.parent.mkdir(parents=True, exist_ok=True)
    origin.write_bytes(b'{"record":true}\n')
    fields = {"kind": "simulation_record_manifest", "owner": "record-id", "maximum": 1024}

    def follow() -> None:
        reference = encode_artifact_reference(
            build_artifact_reference(
                origin, base_name="reports_root", base=reports, booley_dir=entry, **fields
            )
        )
        resolved = resolve_artifact_reference(
            reference,
            bases={"reports_root": reports},
            expected_kind=fields["kind"],
            expected_owner=fields["owner"],
            maximum=1024,
            booley_dir=entry,
        )
        assert resolved.path == origin

    if layout == "below":
        with pytest.raises(ArtifactReferenceError, match=r"link|escapes"):
            follow()
    else:
        follow()
