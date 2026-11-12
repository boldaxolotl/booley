"""Project site wording contracts, and the facts it must share with the README.

The site (website/docs/) and the README share the feature list word for word
and the pinned PicoRV32 demo revision; the home page's quick start repeats the
Install guide's install commands.
These tests fail when either copy drifts from the other, when a README link
points at a site section or page that no longer exists, or when the generated
home-page illustration is stale.
"""

import html
import importlib.util
import re
from pathlib import Path

from booley.runtime.docs_links import SITE_URL
from tests.docs.test_links import _destination_failure, _repository_path

ROOT = Path(__file__).resolve().parent.parent
WEBSITE = ROOT / "website"
SITE = (WEBSITE / "docs/index.md").read_text(encoding="utf-8")
DEMOS = (WEBSITE / "docs/demos.md").read_text(encoding="utf-8")
README = (ROOT / "README.md").read_text(encoding="utf-8")
INSTALL_GUIDE = (ROOT / "docs/user/INSTALL.md").read_text(encoding="utf-8")


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


def _readme_feature_cards() -> list[tuple[str, list[str]]]:
    """Return each README "What it does" card as (title, paragraphs), images skipped."""
    section = README.split("## What it does\n", 1)[1].split("\n## ", 1)[0]
    cards = []
    for card in section.split("\n### ")[1:]:
        title, body = card.split("\n", 1)
        blocks = [block.strip() for block in body.split("\n\n")]
        cards.append((_plain(title), [_plain(b) for b in blocks if b and not b.startswith("<")]))
    return cards


def _site_feature_cards() -> list[tuple[str, list[str]]]:
    """Return each home-page feature card as (title, paragraphs)."""
    cards = re.findall(
        r'<div class="feat__txt">\s*<h3>(.*?)</h3>(.*?)</div>', _site_section("features"), re.S
    )
    return [
        (_plain(title), [_plain(p) for p in re.findall(r"<p>(.*?)</p>", body, re.S)])
        for title, body in cards
    ]


def test_features_match_the_readme_word_for_word():
    """One canonical feature list: the README's "What it does" cards are the site's Features."""
    site_cards = _site_feature_cards()

    assert len(site_cards) == 3
    assert all(len(paragraphs) == 3 for _, paragraphs in site_cards)
    assert _readme_feature_cards() == site_cards


def _illustration_build():
    """Load website/illustration/build.py, which the site toolchain runs as a script."""
    spec = importlib.util.spec_from_file_location(
        "site_illustration", WEBSITE / "illustration/build.py"
    )
    assert spec and spec.loader
    build = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(build)
    return build


def test_every_feature_card_shows_its_diagram_on_the_site_and_in_the_readme():
    """The home page inlines each diagram; the README shows its light and dark copies."""
    features = _site_section("features")
    readme_section = README.split("## What it does\n", 1)[1].split("\n## ", 1)[0]
    diagrams = _illustration_build().FEATURE_DIAGRAMS
    assert len(diagrams) == len(_site_feature_cards())
    for name in diagrams:
        assert f'--8<-- "{name}.svg"' in features
        assert f'srcset="{SITE_URL}docs/assets/{name}-dark.svg"' in readme_section
        assert f'src="{SITE_URL}docs/assets/{name}-light.svg"' in readme_section


def test_quick_start_commands_match_the_install_guide_block():
    guide_block = INSTALL_GUIDE.split("<!-- booley-smoke:install -->", 1)[1].split(
        "<!-- /booley-smoke:install -->", 1
    )[0]
    guide_commands = re.findall(r"^(?!```)(\S.*)$", guide_block, flags=re.MULTILINE)
    site_commands = re.findall(
        r'<span class="p">\$ </span>([^<\n]+)', _site_section("description")
    )

    assert site_commands == guide_commands == ["pipx install booley-rtl", "booley bootstrap"]


def test_description_is_the_readme_intro_paragraph():
    # The intro paragraph opens with the product's ADE positioning sentence.
    intro_opener = "Booley is an agentic development environment"
    readme_intro = intro_opener + README.split(intro_opener, 1)[1].split("\n\n", 1)[0]
    site_description = _plain(_site_section("description"))

    assert site_description.startswith(_plain(readme_intro))


def test_demo_clone_link_follows_the_demo_default_branch():
    # Visitors clone the demo's latest default branch, like the Codespaces button;
    # a commit-pinned link goes stale as the demo moves on.
    assert "https://github.com/boldaxolotl/booley-prj-picorv32#readme" in DEMOS
    assert "booley-prj-picorv32/blob/" not in DEMOS


def test_readme_links_resolve_to_site_sections_and_pages():
    """Every README link into the site names a home-page section, user doc, or page."""
    links = re.findall(rf"{re.escape(SITE_URL)}([\w./-]*)(?:#([\w-]+))?", README)
    site_ids = set(re.findall(r'\bid="([a-z-]+)"', SITE))
    failures = []
    for page, anchor in links:
        url = f"{SITE_URL}{page}" + (f"#{anchor}" if anchor else "")
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
    """ide.css, ide-figure.html, and the architecture and Features diagrams match their sources."""
    build = _illustration_build()

    assert build.FIGURE_OUT.read_text(encoding="utf-8") == build.figure_html()
    assert build.CSS_OUT.read_text(encoding="utf-8") == build.css_text()
    assert build.ARCHITECTURE_OUT.read_text(encoding="utf-8") == build.architecture_svg()
    for name in build.FEATURE_DIAGRAMS:
        for theme in build.THEME_RULES:
            generated = build.feature_out(name, theme).read_text(encoding="utf-8")
            assert generated == build.feature_svg(name, theme), (name, theme)
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
    markup += [*WEBSITE.glob("illustration/feature-*.svg")]
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
    ("--review", "--paper"),
    ("--fault", "--paper"),
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


def test_docs_landing_table_lists_every_user_doc_in_sidebar_order():
    """The docs landing table is hand-written HTML; keep it in step with SITE_DOCS.

    It must list every doc once, in reading order, under its sidebar section,
    with rowspans that cover each section, and mark exactly the child docs.
    """
    from booley.runtime.docs_links import SITE_DOCS

    landing = (WEBSITE / "docs/docs/index.md").read_text(encoding="utf-8")
    body = landing.split("<tbody>", 1)[1].split("</tbody>", 1)[0]
    rows = re.findall(r"<tr>(.*?)</tr>", body)
    sections: list[tuple[str, int]] = []
    pages = []
    for row in rows:
        group = re.search(r'<th scope="rowgroup" rowspan="(\d+)">([^<]+)</th>', row)
        if group:
            sections.append((html.unescape(group.group(2)), int(group.group(1))))
        link = re.search(
            r'<td>(<span class="docs__tree"[^>]*>└</span>)?<a href="([^"]+)">([^<]+)</a>', row
        )
        assert link, f"landing row without a page link: {row}"
        pages.append((sections[-1][0], link.group(2), link.group(3), bool(link.group(1))))

    expected = [
        (doc.section, f"{doc.slug}/", doc.title, doc.parent is not None) for doc in SITE_DOCS
    ]
    assert pages == expected
    assert sum(span for _, span in sections) == len(rows)
    for section, span in sections:
        assert span == sum(doc.section == section for doc in SITE_DOCS), section


def test_stylesheet_urls_change_when_their_content_changes(tmp_path):
    """A deploy must not pair new pages with a cached old stylesheet (Pages caches 10 min)."""
    spec = importlib.util.spec_from_file_location(
        "site_asset_versions", WEBSITE / "hooks/asset_versions.py"
    )
    assert spec and spec.loader
    hook = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hook)
    sheet = tmp_path / "stylesheets/site.css"
    sheet.parent.mkdir()

    sheet.write_text(".a { color: var(--ink); }", encoding="utf-8")
    before = hook.asset_versions(tmp_path, ["stylesheets/site.css"])
    sheet.write_text(".a { color: var(--mask); }", encoding="utf-8")
    after = hook.asset_versions(tmp_path, ["stylesheets/site.css"])

    assert before["stylesheets/site.css"] != after["stylesheets/site.css"]
    base = (WEBSITE / "overrides/base.html").read_text(encoding="utf-8")
    assert "{{ path | url }}?v={{ asset_versions[path] }}" in base


def test_site_workflow_runs_when_a_published_doc_changes():
    """A contributor doc lives outside docs/user/, so pages.yml must name it to redeploy."""
    import yaml

    from booley.runtime.docs_links import SITE_DOCS

    workflow = yaml.safe_load((ROOT / ".github/workflows/pages.yml").read_text(encoding="utf-8"))
    triggers = workflow.get("on", workflow.get(True))  # YAML 1.1 reads a bare `on` as True
    for event in ("push", "pull_request"):
        paths = triggers[event]["paths"]
        uncovered = [
            doc.path
            for doc in SITE_DOCS
            if doc.path not in paths and f"{doc.directory}/**" not in paths
        ]
        assert not uncovered, f"pages.yml {event} paths miss {uncovered}"


def test_demo_page_keeps_the_gateway_bring_your_own_key_anchor():
    """The demo gateway's notice and errors link to booley.dev/demo#bring-your-own-key."""
    page = (WEBSITE / "docs/demo.md").read_text(encoding="utf-8")
    assert 'id="bring-your-own-key"' in page
