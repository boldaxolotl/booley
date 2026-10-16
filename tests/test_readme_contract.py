"""User-facing README wording and ordering contracts."""

import tomllib
from pathlib import Path

README = (Path(__file__).resolve().parent.parent / "README.md").read_text(encoding="utf-8")


def test_primary_pipx_install_and_upgrade_are_pinned():
    assert "pipx install booley-rtl\nbooley bootstrap\n" in README
    assert "pipx upgrade booley-rtl\nbooley bootstrap --update\n" in README
    assert README.index("[pipx installed]") < README.index("pipx install booley-rtl")


def test_try_the_demo_leads_with_the_demo_readme_link():
    section = README.split("### Level 2: Try the demo yourself", 1)[1].split("\n### ", 1)[0]

    contract = Path(__file__).resolve().parent.parent / ".github/contracts/picorv32-demo.toml"
    pin = tomllib.loads(contract.read_text())["project_ref"]
    assert section.strip() == (
        "**[Follow the demo repository's README]"
        f"(https://github.com/boldaxolotl/booley-prj-picorv32/blob/{pin}/README.md)** "
        "to try the demo, after you [install Booley](#installation)."
    )


def test_installation_names_host_agent_cli_prerequisite():
    section = README.split("## Installation", 1)[1].split("\n## ", 1)[0]
    assert (
        "An agent CLI on the host PATH with credentials configured for Project Setup:" in section
    )
    assert "[Claude Code](https://code.claude.com/docs/en/setup)" in section
    assert "[Codex](https://developers.openai.com/codex/cli)" in section
