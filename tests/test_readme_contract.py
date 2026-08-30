"""User-facing README wording and ordering contracts."""

from pathlib import Path

README = (Path(__file__).resolve().parent.parent / "README.md").read_text(encoding="utf-8")


def test_integrated_development_environment_features_lead_with_one_window():
    section = README.split("## Integrated Development Environment", 1)[1].split("\n## ", 1)[0]
    features = (
        "**One Window:**",
        "**Reproducible team environment:**",
        "**One typed Booley Flow surface:**",
    )

    assert [section.index(feature) for feature in features] == sorted(
        section.index(feature) for feature in features
    )
    assert "**One IDE:**" not in section


def test_install_alternative_is_not_padded():
    assert "pipx install booley-rtl # or: pip install booley-rtl" in README
