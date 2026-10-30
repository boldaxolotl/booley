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
    assert readme_blocks == ["pipx install booley-rtl\nbooley bootstrap\n"]
    assert README.index("[pipx installed]") < README.index("pipx install booley-rtl")


def test_readme_installation_links_the_install_guide():
    assert "[Install guide](https://booley.dev/docs/install/)" in _installation_section()


def test_try_the_demo_leads_with_the_demo_readme_link():
    section = README.split("### Level 2: Try the demo yourself", 1)[1].split("\n### ", 1)[0]

    contract = ROOT / ".github/contracts/picorv32-demo.toml"
    pin = tomllib.loads(contract.read_text())["project_ref"]
    assert section.strip() == (
        "**[Follow the demo repository's README]"
        f"(https://github.com/boldaxolotl/booley-prj-picorv32/blob/{pin}/README.md)** "
        "to try the demo, after you [install Booley](#installation)."
    )


def test_install_guide_names_host_agent_cli_prerequisite():
    section = INSTALL_GUIDE.split("## Requirements", 1)[1].split("\n## ", 1)[0]
    assert (
        "An agent CLI on the host PATH with credentials configured for Project Setup:" in section
    )
    assert "[Claude Code](https://code.claude.com/docs/en/setup)" in section
    assert "[Codex](https://developers.openai.com/codex/cli)" in section
