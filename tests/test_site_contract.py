"""Project site contracts: links, colours, contrast and the generated diagrams.

These tests fail when a README link points at a site section or page that no
longer exists, when a site colour escapes the theme tokens or fails WCAG AA
contrast, or when a generated diagram is stale.
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
README = (ROOT / "README.md").read_text(encoding="utf-8")


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


def test_generated_diagrams_are_up_to_date():
    """The architecture and Features diagrams match their sources."""
    spec = importlib.util.spec_from_file_location(
        "site_illustration", WEBSITE / "illustration/build.py"
    )
    assert spec and spec.loader
    build = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(build)

    assert build.ARCHITECTURE_OUT.read_text(encoding="utf-8") == build.architecture_svg()
    assert build.ARCHITECTURE_DARK_OUT.read_text(encoding="utf-8") == build.architecture_svg(
        dark=True
    )
    for name in build.FEATURE_DIAGRAMS:
        for theme in build.THEME_RULES:
            generated = build.feature_out(name, theme).read_text(encoding="utf-8")
            assert generated == build.feature_svg(name, theme), (name, theme)


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
    rows = re.findall(r"<tr>(.*?)</tr>", body, re.DOTALL)
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


def test_goal_mode_url_redirects_to_the_metric_driven_development_guide():
    """The retired /docs/goal-mode/ URL still resolves: a static page sends readers to the guide."""
    from booley.runtime.docs_links import SITE_DOCS, docs_url

    redirect = (WEBSITE / "docs/docs/goal-mode/index.html").read_text(encoding="utf-8")
    assert '<meta http-equiv="refresh" content="0; url=../metric-driven-development/">' in redirect
    canonical = docs_url("METRIC-DRIVEN-DEVELOPMENT.md")
    assert f'<link rel="canonical" href="{canonical}">' in redirect
    assert "metric-driven-development" in {doc.slug for doc in SITE_DOCS}
    assert "goal-mode" not in {doc.slug for doc in SITE_DOCS}
