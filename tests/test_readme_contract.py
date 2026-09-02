"""User-facing README wording and ordering contracts."""

from pathlib import Path

README = (Path(__file__).resolve().parent.parent / "README.md").read_text(encoding="utf-8")


def test_integrated_development_environment_features_lead_with_one_window():
    section = README.split("## Integrated Development Environment", 1)[1].split("\n## ", 1)[0]
    features = (
        "**One Window:**",
        "**Reproducible team environment:**",
        "**A typed interface for each Booley Flow:**",
    )

    assert [section.index(feature) for feature in features] == sorted(
        section.index(feature) for feature in features
    )
    assert "**One IDE:**" not in section


def test_install_alternative_is_not_padded():
    assert "pipx install booley-rtl # or: pip install booley-rtl" in README


def test_try_the_demo_leads_with_the_demo_readme_link():
    section = README.split("### Level 2: Try the demo yourself", 1)[1].split("\n### ", 1)[0]

    assert section.strip() == (
        "**[Follow the demo repository's README]"
        "(https://github.com/boldaxolotl/booley-prj-picorv32#readme)** "
        "to install and try the demo."
    )
