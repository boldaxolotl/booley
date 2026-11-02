"""User-facing README wording and ordering contracts."""

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
README = (ROOT / "README.md").read_text(encoding="utf-8")
INSTALL_GUIDE = (ROOT / "docs/user/INSTALL.md").read_text(encoding="utf-8")


def _installation_section() -> str:
    return README.split("## Installation", 1)[1].split("\n## ", 1)[0]


def test_readme_install_commands_match_the_install_guide():
    """The README keeps a short copy of the commands the install smoke tests."""
    guide_block = INSTALL_GUIDE.split("<!-- booley-smoke:install -->", 1)[1].split(
        "<!-- /booley-smoke:install -->", 1
    )[0]
    readme_blocks = re.findall(r"```bash\n(.*?)```", _installation_section(), re.DOTALL)
    assert readme_blocks == [guide_block.strip().removeprefix("```bash\n").removesuffix("```")]
    assert README.index("[pipx installed]") < README.index("pipx install booley-rtl")


def test_readme_installation_links_the_install_guide():
    assert "[Install guide](https://booley.dev/docs/install/)" in _installation_section()


def _demo_section() -> str:
    return README.split("### Demo", 1)[1].split("\n### ", 1)[0]


def test_demo_leads_with_the_codespaces_badge():
    """The free one-click Codespace comes before the local-install route."""
    section = _demo_section()
    # The badge opens the demo's own default branch, whose devcontainer pins the image.
    badge = (
        "[![Open in GitHub Codespaces](https://github.com/codespaces/badge.svg)]"
        "(https://codespaces.new/boldaxolotl/booley-prj-picorv32?quickstart=1)"
    )
    assert badge in section
    assert section.index(badge) < section.index("[follow the demo repository's README]")


def test_demo_links_the_pinned_demo_readme():
    contract = ROOT / ".github/contracts/picorv32-demo.toml"
    pin = tomllib.loads(contract.read_text())["project_ref"]
    assert (
        "**[follow the demo repository's README]"
        f"(https://github.com/boldaxolotl/booley-prj-picorv32/blob/{pin}/README.md)** "
        "to run the demo locally, after you [install Booley](#installation)."
    ) in _demo_section()


def test_install_guide_names_host_agent_cli_prerequisite():
    section = INSTALL_GUIDE.split("## Requirements", 1)[1].split("\n## ", 1)[0]
    assert (
        "An agent CLI on the host PATH with credentials configured for Project Setup:" in section
    )
    assert "[Claude Code](https://code.claude.com/docs/en/setup)" in section
    assert "[Codex](https://developers.openai.com/codex/cli)" in section
