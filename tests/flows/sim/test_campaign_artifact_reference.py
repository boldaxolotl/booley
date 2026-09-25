"""Campaign artifact references stay strict while their base may move."""

import shutil

import pytest

from booley.flows.sim.campaign import (
    ArtifactReferenceError,
    build_artifact_reference,
    resolve_artifact_reference,
    resolve_report_artifact_reference,
)


def _reference(tmp_path):
    base = tmp_path / "origin"
    artifact = base / "campaign/manifest.json"
    artifact.parent.mkdir(parents=True)
    artifact.write_bytes(b'{"campaign":true}\n')
    return base, build_artifact_reference(
        artifact,
        base_name="origin_target",
        base=base,
        kind="simulation_campaign_manifest",
        owner="campaign-id",
        maximum=1024,
    )


def test_reference_resolves_after_complete_base_copy(tmp_path):
    base, reference = _reference(tmp_path)
    copied = tmp_path / "copied"
    shutil.copytree(base, copied)

    resolved = resolve_artifact_reference(
        reference,
        bases={"origin_target": copied},
        allowed_bases={"origin_target"},
        expected_kind="simulation_campaign_manifest",
        expected_owner="campaign-id",
        maximum=1024,
    )

    assert resolved.path == copied / "campaign/manifest.json"
    assert resolved.raw == b'{"campaign":true}\n'


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("path_base", "reports_root"),
        ("path", "/campaign/manifest.json"),
        ("path", "campaign/../manifest.json"),
        ("path", "campaign\\manifest.json"),
        ("path", "campaign/manifest.json\x00"),
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
        resolve_artifact_reference(
            hostile,
            bases={"origin_target": base},
            allowed_bases={"origin_target"},
            expected_kind="simulation_campaign_manifest",
            expected_owner="campaign-id",
            maximum=1024,
        )


def test_reference_rejects_link_below_base(tmp_path):
    base, reference = _reference(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "manifest.json").write_bytes(b'{"campaign":true}\n')
    shutil.rmtree(base / "campaign")
    (base / "campaign").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ArtifactReferenceError, match=r"link|escapes"):
        resolve_artifact_reference(
            reference,
            bases={"origin_target": base},
            allowed_bases={"origin_target"},
            expected_kind="simulation_campaign_manifest",
            expected_owner="campaign-id",
            maximum=1024,
        )


def test_report_reference_uses_the_containing_document_after_reports_root_copy(tmp_path):
    reports = tmp_path / "reports"
    origin = reports / "sim/1/targets/sim/campaign/manifest.json"
    origin.parent.mkdir(parents=True)
    origin.write_bytes(b'{"campaign":true}\n')
    resume_report = reports / "sim/2/report.json"
    resume_report.parent.mkdir(parents=True)
    resume_report.write_text("{}")
    reference = build_artifact_reference(
        origin,
        base_name="reports_root",
        base=reports,
        kind="simulation_campaign_manifest",
        owner="campaign-id",
        maximum=1024,
    )
    copied = tmp_path / "copied"
    shutil.copytree(reports, copied)

    resolved = resolve_report_artifact_reference(
        copied / "sim/2/report.json",
        reference,
        expected_kind="simulation_campaign_manifest",
        expected_owner="campaign-id",
        maximum=1024,
    )

    assert resolved.path == copied / "sim/1/targets/sim/campaign/manifest.json"
