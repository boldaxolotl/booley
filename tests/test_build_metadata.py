from __future__ import annotations

from booley import __version__
from booley.runtime import build_metadata


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
