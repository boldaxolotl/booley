"""Split the quick-reference cheatsheet into flag-addressable sections.

``booley cheat`` prints the whole sheet by default, but most callers want one
slice of it: the ticket-create skill only needs the criteria catalog, a user
debugging a container only needs the Runtime & Docker table. Section flags
(``--criteria``, ``--flows``, ...) narrow the output so neither pays for the
rest.

Sections are the ``### `` headings of ``data/cheatsheet.md``; the table below
pins each to a stable CLI flag name. A heading in the file with no entry here
is unreachable by flag, so ``tests/harness/test_cheatsheet.py`` guards the two
against drift.
"""

from __future__ import annotations

from dataclasses import dataclass

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
    Section("board", "Ticket Board", "Ticket lifecycle and valid transitions"),
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
        "Editable .booley_project files and affected capabilities",
    ),
    Section("skills", "Skills", "Ticket-authoring / triage skills"),
    Section("artifacts", "Artifacts", "Where reports, logs, and state land"),
    Section(
        "runtime",
        "Runtime & Docker",
        "Session Runtime, sandbox image, and container commands",
        aliases=("docker",),
    ),
)

_BY_SLUG: dict[str, Section] = {s.slug: s for s in SECTIONS}


def section_slugs() -> tuple[str, ...]:
    """Return every section slug, in cheatsheet order."""
    return tuple(s.slug for s in SECTIONS)


def section_flags(slug: str) -> tuple[str, ...]:
    """Return the primary flag plus any backward-compatible aliases."""
    section = _BY_SLUG[slug]
    return (section.slug, *section.aliases)


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
