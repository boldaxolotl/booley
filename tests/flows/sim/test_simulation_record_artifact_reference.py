"""Simulation Record artifact references stay strict while their base may move."""

import shutil

import pytest

from booley.flows.sim.record import (
    ArtifactReferenceError,
    build_artifact_reference,
    encode_artifact_reference,
    resolve_artifact_reference,
    resolve_report_artifact_reference,
)
from tests.fusesoc_test_support import symlinked_booley_dir

_REMOVED = object()


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
    return base, _build_origin(artifact, base)


def _resolve_origin(reference, bases, *, maximum=1024):
    return resolve_artifact_reference(
        reference,
        bases=bases,
        allowed_bases={"origin_target"},
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
    ("field", "value", "message"),
    [
        ("path_base", "reports_root", None),
        ("path", "/record/manifest.json", None),
        ("path", "record/../manifest.json", None),
        ("path", "record\\manifest.json", None),
        ("path", "record/manifest.json\x00", None),
        ("bytes", 0, None),
        ("sha256", "sha256:" + "0" * 64, None),
        ("kind", "other", None),
        ("owner", "other", None),
        ("owner", _REMOVED, "fields are invalid"),
        ("kind", "", "kind is invalid"),
    ],
)
def test_reference_rejects_wrong_profile_or_identity(tmp_path, field, value, message):
    base, reference = _reference(tmp_path)
    hostile = {**encode_artifact_reference(reference), field: value}
    if value is _REMOVED:
        del hostile[field]

    with pytest.raises(ArtifactReferenceError, match=message):
        _resolve_origin(hostile, {"origin_target": base})


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


def test_report_reference_uses_the_containing_document_after_reports_root_copy(tmp_path):
    reports = tmp_path / "reports"
    origin = reports / "sim/1/targets/sim/record/manifest.json"
    origin.parent.mkdir(parents=True)
    origin.write_bytes(b'{"record":true}\n')
    resume_report = reports / "sim/2/report.json"
    resume_report.parent.mkdir(parents=True)
    resume_report.write_text("{}")
    reference = _build_origin(origin, reports, base_name="reports_root")
    copied = tmp_path / "copied"
    shutil.copytree(reports, copied)

    resolved = resolve_report_artifact_reference(
        copied / "sim/2/report.json",
        reference,
        expected_kind="simulation_record_manifest",
        expected_owner="record-id",
        maximum=1024,
    )

    assert resolved.path == copied / "sim/1/targets/sim/record/manifest.json"


def test_report_reference_accepts_explicit_external_origin_target(tmp_path):
    origin = tmp_path / "origin-target"
    artifact = origin / "record/manifest.json"
    artifact.parent.mkdir(parents=True)
    artifact.write_bytes(b'{"record":true}\n')
    report = tmp_path / "other-reports/sim/1/report.json"
    report.parent.mkdir(parents=True)
    report.write_text("{}")
    reference = _build_origin(artifact, origin, base_name="external_origin_target")

    resolved = resolve_report_artifact_reference(
        report,
        reference,
        expected_kind="simulation_record_manifest",
        expected_owner="record-id",
        maximum=1024,
        external_origin_target=origin,
    )

    assert resolved.path == artifact


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


def test_report_reference_requires_numbered_invocation_report(tmp_path):
    _base, reference = _reference(tmp_path)

    with pytest.raises(ArtifactReferenceError, match="numbered invocation"):
        resolve_report_artifact_reference(
            tmp_path / "reports/sim/latest/report.json",
            reference,
            expected_kind="simulation_record_manifest",
            expected_owner="record-id",
            maximum=1024,
        )


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
    report = reports / "sim/2/report.json"
    report.parent.mkdir(parents=True)
    report.write_text("{}")
    fields = {"kind": "simulation_record_manifest", "owner": "record-id", "maximum": 1024}

    def follow() -> None:
        reference = build_artifact_reference(
            origin, base_name="reports_root", base=reports, booley_dir=entry, **fields
        )
        resolved = resolve_artifact_reference(
            reference,
            bases={"reports_root": reports},
            allowed_bases={"reports_root"},
            expected_kind=fields["kind"],
            expected_owner=fields["owner"],
            maximum=1024,
            booley_dir=entry,
        )
        assert resolved.path == origin
        from_report = resolve_report_artifact_reference(
            report,
            reference,
            expected_kind=fields["kind"],
            expected_owner=fields["owner"],
            maximum=1024,
            booley_dir=entry,
        )
        assert from_report.raw == b'{"record":true}\n'

    if layout == "below":
        with pytest.raises(ArtifactReferenceError, match=r"link|escapes"):
            follow()
    else:
        follow()
