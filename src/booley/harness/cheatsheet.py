"""Split the quick-reference cheatsheet into flag-addressable sections.

``booley cheat`` lists the available sections by default; ``--all`` prints
the whole sheet. Section flags (``--criteria``, ``--flows``, ...) select
individual slices: the Goal skill only needs the criteria catalog, a user
debugging a container only needs the Sandbox & Docker table.

``render_index`` draws the section index. Sections are the ``### `` headings of ``data/cheatsheet.md``; the table below
pins each to a stable CLI flag name. A heading in the file with no entry here
is unreachable by flag, so ``tests/harness/test_cheatsheet.py`` guards the two
against drift.
"""

from __future__ import annotations

from dataclasses import dataclass

from .colors import accent, len_visible
from .render_md import inline, wrap

# Cheatsheet sections are `### ` headings; `## ` is the document title.
_HEADING_PREFIX = "### "


@dataclass(frozen=True)
class Section:
    """One flag-addressable slice of the cheatsheet."""

    slug: str  # CLI flag name, without the leading `--`
    heading: str  # `### ` heading text in cheatsheet.md, matched verbatim
    help: str  # argparse help string
    aliases: tuple[str, ...] = ()  # retained legacy flag names


SECTIONS: tuple[Section, ...] = (
    Section("commands", "Commands", "Public `booley` subcommands"),
    Section("goals", "Goal Mode", "Goals, Goalsets, and session lifecycle"),
    Section("flows", "Booley Flows", "Deterministic end-to-end orchestration"),
    Section(
        "specialists",
        "Specialists",
        "LLM specialists, review focuses, and mutation goals",
    ),
    Section("criteria", "Criteria", "Criterion catalog + threshold params"),
    Section("targets", "Targets", "FuseSoC Targets and `booley targets`"),
    Section(
        "project",
        "Project Files",
        "Editable .booley files and affected capabilities",
    ),
    Section("skills", "Skills", "Project and Goal workflow skills"),
    Section("artifacts", "Artifacts", "Where reports, logs, and state land"),
    Section(
        "sandbox",
        "Sandbox & Docker",
        "Sandbox, Sandbox Image, and container commands",
        aliases=("runtime", "docker"),
    ),
)

_BY_SLUG: dict[str, Section] = {s.slug: s for s in SECTIONS}


def section_slugs() -> tuple[str, ...]:
    """Return every section slug, in cheatsheet order."""
    return tuple(s.slug for s in SECTIONS)


def section_flags(slug: str) -> tuple[str, ...]:
    """Return the primary flag plus any backward-compatible aliases."""
    section = _BY_SLUG[slug]
    return ("project-files" if slug == "project" else section.slug, *section.aliases)


def split_sections(text: str) -> tuple[str, dict[str, str]]:
    """Split cheatsheet text into its preamble and ``### `` sections.

    Returns ``(preamble, {heading: body})`` where each body still carries its
    own heading line, so sections round-trip by simple concatenation.
    """
    preamble: list[str] = []
    bodies: dict[str, list[str]] = {}
    current: list[str] | None = None

    for line in text.splitlines():
        if line.startswith(_HEADING_PREFIX):
            current = bodies.setdefault(line[len(_HEADING_PREFIX) :].strip(), [])
        (preamble if current is None else current).append(line)

    return "\n".join(preamble), {h: "\n".join(b) for h, b in bodies.items()}


def select(text: str, slugs: list[str] | tuple[str, ...]) -> str:
    """Return only the requested sections of ``text``, in cheatsheet order.

    An empty selection means "everything", returned verbatim (title and all).
    A filtered view drops the document title: the caller asked for one table,
    not a document. Requested sections missing from the file are skipped —
    a stale flag must not blank out the sections that *are* present.
    """
    if not slugs:
        return text

    _, bodies = split_sections(text)
    wanted = set(slugs)
    chosen = [
        bodies[sec.heading] for sec in SECTIONS if sec.slug in wanted and sec.heading in bodies
    ]
    return "\n".join(chosen)


def section_help(slug: str) -> str:
    """Return the argparse help string for one section slug."""
    return _BY_SLUG[slug].help


def render_index(width: int) -> list[str]:
    """The section index: one ``--flag`` row each, descriptions aligned and wrapped.

    Rows fit *width* cells; a wrapped description hangs under its column.
    Backticks in help text render as inline code.
    """
    flags = {slug: section_flags(slug) for slug in section_slugs()}
    column = 2 + max(len(f"--{names[0]}") for names in flags.values()) + 2
    rows: list[str] = []
    for slug, (flag, *aliases) in flags.items():
        alias_note = f" (alias: {', '.join(f'--{name}' for name in aliases)})" if aliases else ""
        lead = f"  {accent(f'--{flag}')}"
        first = lead + " " * (column - len_visible(lead))
        rows.extend(wrap(inline(section_help(slug) + alias_note), width, first, " " * column))
    return rows
