"""User-facing README wording and ordering contracts."""

from pathlib import Path

from booley.ticket_board.ticket_document import (
    TicketAuthoringView,
    TicketConversionContext,
    convert_ticket_document,
)

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
    section = README.split("**Alternative: pip user install**", 1)[1]
    assert "only for interpreters that permit user" in section
    assert "python3 -m pip install --user booley-rtl" in section


def test_primary_pipx_install_and_upgrade_are_pinned():
    assert "pipx install booley-rtl\nbooley bootstrap\n" in README
    assert "pipx upgrade booley-rtl\nbooley bootstrap --update\n" in README
    assert README.index("**Reopen your terminal**") < README.index("pipx install booley-rtl")
    assert "py -m pip install --user pipx" in README
    assert "uv tool install booley-rtl" in README
    assert "uv tool upgrade booley-rtl" in README


def test_try_the_demo_leads_with_the_demo_readme_link():
    section = README.split("### Level 2: Try the demo yourself", 1)[1].split("\n### ", 1)[0]

    assert section.strip() == (
        "**[Follow the demo repository's README]"
        "(https://github.com/boldaxolotl/booley-prj-picorv32#readme)** "
        "to try the demo, after you [install Booley](#installation)."
    )


def test_ticket_example_converts_through_the_real_ticket_boundary():
    """The README's example ticket must stay valid as the ticket syntax evolves."""
    # The only ```yaml block in the README that starts with frontmatter is the ticket.
    example = README.split("```yaml\n---\n", 1)[1].split("```", 1)[0]
    # Accept every Target selector as-is; the example isn't tied to a real Project.
    view = TicketAuthoringView(
        resolve_target=lambda selector, _flow: selector,
        tests_for_target=lambda _target: ("smoke",),
    )
    context = TicketConversionContext("draft", lambda _generated: view)

    conversion = convert_ticket_document("---\n" + example, context)

    assert conversion.diagnostics == ()
    assert conversion.document is not None
    capabilities = {row.capability for row in conversion.document.spec.criteria}
    assert capabilities == {"LINT", "SIM", "COVERAGE", "REVIEW", "SYNTH"}


def test_installation_names_host_agent_cli_prerequisite():
    section = README.split("## Installation", 1)[1].split("\n## ", 1)[0]
    assert "host agent CLI on PATH for Project Setup" in section
    assert "[Claude Code](https://code.claude.com/docs/en/setup)" in section
    assert "[Codex](https://developers.openai.com/codex/cli)" in section
