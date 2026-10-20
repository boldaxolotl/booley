"""Project site wording contracts, and the facts it must share with the README.

The site (website/docs/) is the full feature tour; the README keeps the same
feature list, the install commands, and the pinned PicoRV32 demo revision.
These tests fail when either copy drifts from the other, when a README link
points at a site section or page that no longer exists, or when the generated
home-page illustration is stale.
"""

import html
import importlib.util
import re
import tomllib
from pathlib import Path

from booley.goals.model import parse_goal_args
from booley.goals.presentation import goal_presentation
from booley.goals.translate import translate_goals

ROOT = Path(__file__).resolve().parent.parent
WEBSITE = ROOT / "website"
SITE = (WEBSITE / "docs/index.md").read_text(encoding="utf-8")
DEMOS = (WEBSITE / "docs/demos.md").read_text(encoding="utf-8")
README = (ROOT / "README.md").read_text(encoding="utf-8")


def _plain(markup: str) -> str:
    """Return the visible text of HTML or inline Markdown, whitespace-normalized."""
    text = html.unescape(re.sub(r"<[^>]+>", "", markup))
    text = text.replace("**", "").replace("`", "").replace("\u2019", "'")  # typographic apostrophe
    return " ".join(text.split())


def _site_section(section_id: str) -> str:
    """Return the markup of one numbered home-page section, heading excluded."""
    start = re.search(rf'<h2 class="sec" id="{section_id}">.*?</h2>', SITE)
    assert start, f"home page has no section #{section_id}"
    end = SITE.find('<h2 class="sec"', start.end())
    return SITE[start.end() : end if end != -1 else len(SITE)]


def test_features_match_the_readme_word_for_word():
    """One canonical feature list: the README's "What it does" is the site's Features."""
    readme_section = README.split("## What it does\n", 1)[1].strip().split("\n\n", 1)[0]
    readme_features = [_plain(line[2:]) for line in readme_section.splitlines()]
    site_list = re.search(r'<ul class="feat">(.*?)</ul>', _site_section("features"), re.S)
    assert site_list
    site_features = [_plain(item) for item in re.findall(r"<li>(.*?)</li>", site_list[1], re.S)]

    assert readme_features == site_features
    assert len(site_features) == 6


def test_inline_goal_examples_match_the_real_goal_presentation():
    """The advertised examples must match validated Goals' public notation."""
    examples = [
        {
            "family": "SYNTH",
            "target": "synth_fifo",
            "thresholds": {"area_increase_at_most": "10%"},
        },
        {"family": "COVERAGE", "target": "sim_fifo", "tests": "all", "metrics": {"line": 90}},
    ]
    translation = translate_goals(parse_goal_args(examples))
    assert translation.warnings == ()
    detail = _site_section("detailed-description")
    for goal in translation.goals:
        assert f"<code>{goal_presentation(goal).text}</code>" in detail


def test_quick_start_commands_match_the_readme_install_block():
    readme_block = README.split("<!-- booley-smoke:install -->", 1)[1].split(
        "<!-- /booley-smoke:install -->", 1
    )[0]
    readme_commands = re.findall(r"^(?!```)(\S.*)$", readme_block, flags=re.MULTILINE)
    site_commands = re.findall(
        r'<span class="p">\$ </span>([^<\n]+)', _site_section("description")
    )

    assert site_commands == readme_commands == ["pipx install booley-rtl", "booley bootstrap"]


def test_description_is_the_readme_intro_paragraph():
    readme_intro = (
        "Booley turns Claude Code or Codex"
        + README.split("Booley turns Claude Code or Codex", 1)[1].split("\n\n", 1)[0]
    )
    site_description = _plain(_site_section("description"))

    assert site_description.startswith(_plain(readme_intro))


def test_demo_link_uses_the_contract_pinned_revision():
    contract = ROOT / ".github/contracts/picorv32-demo.toml"
    pin = tomllib.loads(contract.read_text())["project_ref"]

    assert f"https://github.com/boldaxolotl/booley-prj-picorv32/blob/{pin}/README.md" in DEMOS


def test_readme_links_resolve_to_site_sections_and_pages():
    """Every README link into the site names a home-page section or a page that exists."""
    links = re.findall(
        r"https://boldaxolotl\.github\.io/booley/([a-z-]*/?)(?:#([a-z-]+))?", README
    )
    anchors = {anchor for page, anchor in links if not page and anchor}
    pages = {page.strip("/") for page, _ in links if page}

    assert anchors
    assert anchors <= set(re.findall(r'\bid="([a-z-]+)"', SITE))
    assert pages
    assert all((WEBSITE / "docs" / f"{page}.md").is_file() for page in pages)


def test_generated_illustration_is_up_to_date():
    """docs/stylesheets/ide.css and snippets/ide-figure.html match their sources."""
    spec = importlib.util.spec_from_file_location(
        "site_illustration", WEBSITE / "illustration/build.py"
    )
    assert spec and spec.loader
    build = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(build)

    assert build.FIGURE_OUT.read_text(encoding="utf-8") == build.figure_html()
    assert build.CSS_OUT.read_text(encoding="utf-8") == build.css_text()
    assert '--8<-- "ide-figure.html"' in SITE
