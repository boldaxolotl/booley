from __future__ import annotations

import pytest

import booley
from booley import __version__
from booley.runtime import build_metadata
from booley.runtime.version_attribution import VersionAttribution, VersionOrigin


@pytest.fixture(autouse=True)
def _installed_distribution(monkeypatch):
    monkeypatch.setattr(
        booley,
        "_version_attribution",
        VersionAttribution(
            version=__version__,
            origin=VersionOrigin.DISTRIBUTION,
            distribution_name="booley-rtl",
        ),
    )


def test_status_line_uses_baked_image_metadata(monkeypatch):
    monkeypatch.setattr(build_metadata, "_checkout_metadata", lambda: ("", ""))
    monkeypatch.setattr(build_metadata, "_baked_revision", lambda: "")
    monkeypatch.setenv("BOOLEY_VERSION", __version__)
    monkeypatch.setenv("BOOLEY_SOURCE_REVISION", "abc123")
    monkeypatch.setenv("BOOLEY_SOURCE_UPDATED_AT", "2026-08-09T22:15:00+04:00")
    monkeypatch.setenv("BOOLEY_IMAGE_BUILT_AT", "2026-08-10T07:30:00Z")
    monkeypatch.setenv("BOOLEY_LOCAL_TIMEZONE", "+04:00")

    line = build_metadata.format_status_line()

    assert "(abc123)" in line
    assert "last updated 22:15 · 09 AUG 2026" in line
    assert "sandbox image built 11:30 · 10 AUG 2026" in line


def test_status_line_marks_metadata_from_old_images_unknown(monkeypatch):
    monkeypatch.setattr(build_metadata, "_checkout_metadata", lambda: ("", ""))
    monkeypatch.setattr(build_metadata, "_baked_revision", lambda: "legacy123")
    monkeypatch.delenv("BOOLEY_VERSION", raising=False)
    monkeypatch.delenv("BOOLEY_SOURCE_REVISION", raising=False)
    monkeypatch.delenv("BOOLEY_SOURCE_UPDATED_AT", raising=False)
    monkeypatch.delenv("BOOLEY_IMAGE_BUILT_AT", raising=False)

    line = build_metadata.format_status_line()

    assert "(legacy123)" in line
    assert "last updated unknown" in line
    assert "sandbox image built unknown" in line


def test_updated_package_does_not_claim_old_image_source_metadata(monkeypatch):
    monkeypatch.setattr(build_metadata, "_checkout_metadata", lambda: ("", ""))
    monkeypatch.setattr(build_metadata, "_baked_revision", lambda: "")
    monkeypatch.setenv("BOOLEY_VERSION", "different-version")
    monkeypatch.setenv("BOOLEY_SOURCE_REVISION", "old123")
    monkeypatch.setenv("BOOLEY_SOURCE_UPDATED_AT", "2026-01-01T00:00:00Z")
    monkeypatch.setenv("BOOLEY_IMAGE_BUILT_AT", "2026-01-02T00:00:00Z")
    monkeypatch.setenv("BOOLEY_LOCAL_TIMEZONE", "+04:00")

    line = build_metadata.format_status_line()

    assert "old123" not in line
    assert "last updated unknown" in line
    assert "sandbox image built 04:00 · 02 JAN 2026" in line


def test_payload_fingerprint_describes_imported_wheel_not_old_image(monkeypatch):
    monkeypatch.setattr(build_metadata, "_checkout_metadata", lambda: ("", ""))
    monkeypatch.setattr(build_metadata, "_embedded_payload_fingerprint", lambda: "wheel-payload")
    monkeypatch.setenv("BOOLEY_VERSION", "different-version")
    monkeypatch.setenv("BOOLEY_PAYLOAD_FINGERPRINT", "old-image-payload")

    assert build_metadata.current_build_metadata().payload_fingerprint == "wheel-payload"


def test_source_git_failure_does_not_borrow_wheel_or_image_revision(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()
    monkeypatch.setattr(
        booley,
        "_version_attribution",
        VersionAttribution(
            version="9.9.9",
            origin=VersionOrigin.SOURCE,
            source_root=tmp_path,
        ),
    )
    monkeypatch.setattr(booley, "__version__", "9.9.9")
    monkeypatch.setattr(build_metadata, "_git_output", lambda *_args: "")
    monkeypatch.setattr(build_metadata, "_baked_revision", lambda: "wheel123")
    monkeypatch.setenv("BOOLEY_VERSION", "9.9.9")
    monkeypatch.setenv("BOOLEY_SOURCE_REVISION", "image123")
    monkeypatch.setenv("BOOLEY_SOURCE_UPDATED_AT", "2026-01-01T00:00:00Z")

    metadata = build_metadata.current_build_metadata()

    assert metadata.version == "9.9.9"
    assert metadata.revision == ""
    assert metadata.source_updated_at == ""
