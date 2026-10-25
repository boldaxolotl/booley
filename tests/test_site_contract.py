"""Project site wording contracts, and the facts it must share with the README.

The site (website/docs/) and the README share the feature list word for word,
the install commands, and the pinned PicoRV32 demo revision.
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
from tests.docs.test_links import _destination_failure, _repository_path

ROOT = Path(__file__).resolve().parent.parent
WEBSITE = ROOT / "website"
SITE = (WEBSITE / "docs/index.md").read_text(encoding="utf-8")
DEMOS = (WEBSITE / "docs/demos.md").read_text(encoding="utf-8")
README = (ROOT / "README.md").read_text(encoding="utf-8")


def _plain(markup: str) -> str:
    """Return the visible text of HTML or inline Markdown, whitespace-normalized."""
    text = html.unescape(re.sub(r"<[^>]+>", "", markup))
    text = re.sub(r"\[([^]]+)\]\([^)]+\)", r"\1", text)  # Markdown link -> its text
    text = text.replace("**", "").replace("`", "")
    for typographic, plain in (("\u2019", "'"), ("\u201c", '"'), ("\u201d", '"')):
        text = text.replace(typographic, plain)
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
    site_features = [
        _plain(f"{name}: {text}")
        for name, text in re.findall(
            r"<h3>(.*?)</h3>\s*<p>(.*?)</p>", _site_section("features"), re.S
        )
    ]

    assert site_features
    assert readme_features == site_features


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
    features = _site_section("features")
    for goal in translation.goals:
        assert f"<code>{goal_presentation(goal).text}</code>" in features


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
    """Every README link into the site names a home-page section, user doc, or page."""
    links = re.findall(r"https://boldaxolotl\.github\.io/booley/([\w./-]*)(?:#([\w-]+))?", README)
    site_ids = set(re.findall(r'\bid="([a-z-]+)"', SITE))
    failures = []
    for page, anchor in links:
        url = f"https://boldaxolotl.github.io/booley/{page}" + (f"#{anchor}" if anchor else "")
        if not page:
            if anchor and anchor not in site_ids:
                failures.append(url)
        elif page.startswith("docs/"):
            # User docs: slug -> docs/user/<FILE>.md, anchor -> its GitHub heading IDs.
            target = _repository_path(ROOT / "README.md", url)
            if target is None or _destination_failure(target, url):
                failures.append(url)
        else:
            docs = WEBSITE / "docs" / page.strip("/")
            if not (docs.with_suffix(".md").is_file() or (docs / "index.md").is_file()):
                failures.append(url)

    assert {page.split("/")[0] for page, _ in links} >= {"", "docs", "videos"}
    assert not failures, failures


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


# ---------------------------------------------------------------- theme tokens
STYLESHEETS = WEBSITE / "docs/stylesheets"
TOKENS_CSS = STYLESHEETS / "datasheet.css"
# Hex, functional and named colour literals (named: the common CSS ones).
_COLOUR = (
    r"#[0-9a-fA-F]{3,8}\b|\b(?:rgb|hsl)a?\("
    r"|\b(?:white|black|red|green|blue|yellow|orange|purple|gray|grey|silver|navy|teal|maroon)\b"
)
# Theme-independent tokens: AN-001 data series keep their meaning in both themes.
_SHARED_TOKEN_PREFIX = "--series-"


def _without_css_comments(css: str) -> str:
    return re.sub(r"/\*.*?\*/", "", css, flags=re.S)


def _token_block(selector: str) -> dict[str, str]:
    """Return every declaration (tokens and color-scheme) in one token rule of datasheet.css."""
    css = _without_css_comments(TOKENS_CSS.read_text(encoding="utf-8"))
    match = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", css)
    assert match, f"datasheet.css has no {selector} token block"
    return dict(re.findall(r"([\w-]+)\s*:\s*([^;]+?)\s*;", match.group(1)))


def test_colours_are_theme_tokens_only():
    """Colour literals live only in datasheet.css token declarations, so both themes reach them."""
    offenders = []
    for sheet in sorted(STYLESHEETS.glob("*.css")):
        css = _without_css_comments(sheet.read_text(encoding="utf-8"))
        for prop, value in re.findall(r"([\w-]+)\s*:\s*([^;{}]+)", css):
            allowed = sheet == TOKENS_CSS and prop.startswith("--")
            if re.search(_COLOUR, value) and not allowed:
                offenders.append(f"{sheet.name}: {prop}: {value.strip()}")
    # Markup: SVG colour attributes and every inline style, in authored and generated HTML.
    colour_attribute = re.compile(r'\b(fill|stroke|stop-color|color|style)="([^"]*)"')
    markup = [*WEBSITE.glob("snippets/*.html"), *WEBSITE.glob("overrides/*.html")]
    markup += [*WEBSITE.glob("illustration/*.html"), *WEBSITE.glob("docs/**/*.md")]
    for path in sorted(markup):
        for name, value in colour_attribute.findall(path.read_text(encoding="utf-8")):
            if re.search(_COLOUR, value):
                offenders.append(f'{path.relative_to(WEBSITE)}: {name}="{value}"')

    assert offenders == []


def test_dark_theme_overrides_every_light_colour():
    """The dark block (chosen with the header toggle) overrides every themed light colour."""
    dark = _token_block(':root[data-theme="dark"]')
    light_colours = {
        name
        for name, value in _token_block(":root").items()
        if re.search(_COLOUR, value) and not name.startswith(_SHARED_TOKEN_PREFIX)
    }

    assert dark["color-scheme"] == "dark"
    assert {name for name in dark if name.startswith("--")} == light_colours


def test_light_is_the_default_theme():
    """Dark applies only when the reader picks it, never from the OS preference."""
    shell = (WEBSITE / "overrides/base.html").read_text(encoding="utf-8")
    styles = "".join(path.read_text(encoding="utf-8") for path in STYLESHEETS.glob("*.css"))

    assert "prefers-color-scheme" not in shell + styles
    assert 'theme === "dark" ? "dark" : "light"' in shell


def _luminance(hex_colour: str) -> float:
    channels = [int(hex_colour.lstrip("#")[i : i + 2], 16) / 255 for i in (0, 2, 4)]
    linear = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def _contrast(foreground: str, background: str) -> float:
    high, low = sorted((_luminance(foreground), _luminance(background)), reverse=True)
    return (high + 0.05) / (low + 0.05)


# Text-on-background token pairs the stylesheets actually use.
_TEXT_PAIRS = [
    ("--ink", "--paper"),
    ("--ink", "--fill"),
    ("--ink", "--ok-fill"),
    ("--ink", "--diff-add"),
    ("--ink", "--diff-del"),
    ("--ink-2", "--paper"),
    ("--ink-2", "--fill"),
    ("--ink-2", "--fill-2"),
    ("--ink-2", "--ok-fill"),
    ("--mask", "--paper"),
    ("--mask", "--fill"),
    ("--mask", "--fill-2"),
    ("--mask", "--ok-fill"),
    ("--mask-ink", "--mask"),
    ("--paper", "--ink-2"),
    ("--paper", "--ink"),
    ("--quote-ink", "--paper"),
    ("--series-on-dark", "--series-build"),
    ("--series-on-dark", "--series-hung"),
    ("--series-on-light", "--series-obs"),
    ("--series-on-light", "--series-else"),
    ("--series-on-light", "--series-sleep"),
]


def test_text_meets_wcag_aa_contrast_in_both_themes():
    light = _token_block(":root")
    themes = {"light": light, "dark": {**light, **_token_block(':root[data-theme="dark"]')}}
    failures = [
        f"{theme}: {fg} on {bg} = {_contrast(tokens[fg], tokens[bg]):.2f}:1"
        for theme, tokens in themes.items()
        for fg, bg in _TEXT_PAIRS
        if _contrast(tokens[fg], tokens[bg]) < 4.5
    ]

    assert failures == []
