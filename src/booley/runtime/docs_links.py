"""Public URLs of Booley's user docs on the project site (ADR 0076).

``docs/user/*.md`` is the only authored copy of the user docs; the project
site publishes each file at ``docs/<slug>/``. Messages Booley prints for
people link there through :func:`docs_url`. Agent-facing text keeps repo
paths, because agents read the Markdown files.

Standard library only: ``website/hooks/user_docs.py`` loads this file by path
inside the site toolchain, where ``booley`` is not installed, so the site and
Booley share one slug table.
"""

from __future__ import annotations

from typing import NamedTuple
from unicodedata import category

SITE_URL = "https://boldaxolotl.github.io/booley/"


class UserDoc(NamedTuple):
    """One user doc: its file in ``docs/user/``, site slug, and nav label."""

    file: str
    slug: str
    title: str


# Reading order: the site's docs sidebar and prev/next links follow it.
USER_DOCS = (
    UserDoc("FEATURES.md", "features", "Features"),
    UserDoc("SETUP.md", "setup", "Setup"),
    UserDoc("USAGE.md", "usage", "Usage"),
    UserDoc("FLOW_REFERENCE.md", "flow-reference", "Flow reference"),
    UserDoc("CONFIG.md", "config", "Config"),
    UserDoc("SUPPORTED-EDA-TOOLS.md", "supported-eda-tools", "Supported EDA tools"),
    UserDoc("DOCTOR.md", "doctor", "Doctor"),
    UserDoc("TROUBLESHOOTING.md", "troubleshooting", "Troubleshooting"),
)
_SLUGS = {doc.file: doc.slug for doc in USER_DOCS}


def docs_url(file: str, anchor: str | None = None) -> str:
    """Return the site URL of ``docs/user/<file>``, optionally at a heading anchor.

    Raises ``KeyError`` for a file that is not a published user doc.
    """
    url = f"{SITE_URL}docs/{_SLUGS[file]}/"
    return f"{url}#{anchor}" if anchor else url


def github_anchor(heading: str) -> str:
    """Return GitHub's heading ID for rendered heading text (no duplicate suffix).

    The site must produce the same IDs so links work on both; the site hook
    checks every heading against this function.
    """
    return "".join(
        "-" if char.isspace() else char
        for char in heading.strip().lower()
        if char in {"-", "_"} or char.isspace() or category(char)[0] in {"L", "M", "N"}
    )
