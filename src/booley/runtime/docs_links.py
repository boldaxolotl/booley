"""Public URLs of Booley's user docs on the project site (ADR 0081).

``docs/user/*.md`` is the only authored copy of the user docs; the project
site publishes each file at ``docs/<slug>/``. Messages Booley prints for
people link there through :func:`docs_url`. The site also publishes a few
contributor docs from ``docs/internals/`` (:data:`CONTRIBUTOR_DOCS`).

Agent-facing text (skills, the cheatsheet, generated config comments) cannot
assume a repository checkout. It cites the docs the Sandbox ships at
``/booley-docs/<FILE>`` (:func:`sandbox_doc_path`; the allowlist and renderer live
in ``booley.runtime.packaged_docs``, the sync in ``booley.dev_support.reference_docs``) and names any other doc only with the
``(host doc, not shipped)`` marker. ``tests/docs/test_sandbox_doc_citations.py``
enforces this.

Standard library only: ``website/hooks/user_docs.py`` loads this file by path
inside the site toolchain, where ``booley`` is not installed, so the site and
Booley share one slug table.
"""

from __future__ import annotations

from typing import NamedTuple
from unicodedata import category

SITE_URL = "https://booley.dev/"
# Fixed Sandbox path of the packaged user-doc copies (a symlink made by the Dockerfiles).
SANDBOX_DOCS_DIR = "/booley-docs"


class UserDoc(NamedTuple):
    """One published doc: its file, site slug, nav label, and sidebar group.

    ``parent`` names the file of the doc this one belongs to; the sidebar
    indents it under that doc. A child follows its parent or a sibling.
    ``directory`` is the repository folder holding ``file``; only contributor
    docs change it.
    """

    file: str
    slug: str
    title: str
    section: str
    parent: str | None = None
    directory: str = "docs/user"

    @property
    def path(self) -> str:
        """Repository path of the doc's source file."""
        return f"{self.directory}/{self.file}"


# Reading order: the site's docs sidebar and prev/next links follow it. The
# sidebar groups consecutive docs that share a section.
USER_DOCS = (
    UserDoc("FEATURES.md", "features", "Features", "Get started"),
    UserDoc("INSTALL.md", "install", "Install", "Get started"),
    UserDoc("SETUP.md", "setup", "Setup", "Get started"),
    UserDoc("USAGE.md", "usage", "Usage", "Get started"),
    UserDoc("GOAL-MODE.md", "goal-mode", "Goal Mode", "Get started"),
    UserDoc("FLOW_REFERENCE.md", "flow-reference", "Running Flows", "Flows & Specialists"),
    UserDoc("FLOW-SIM.md", "flow-sim", "Simulation", "Flows & Specialists"),
    UserDoc("COVERAGE.md", "coverage", "Coverage", "Flows & Specialists", parent="FLOW-SIM.md"),
    UserDoc("FLOW-LINT.md", "flow-lint", "Lint", "Flows & Specialists"),
    UserDoc("FLOW-SYNTH.md", "flow-synth", "ASIC synthesis", "Flows & Specialists"),
    UserDoc("FLOW-FPGA.md", "flow-fpga", "FPGA implementation", "Flows & Specialists"),
    UserDoc("SPECIALISTS.md", "specialists", "Specialists", "Flows & Specialists"),
    UserDoc("CONFIG.md", "config", "Config", "Reference"),
    UserDoc("TESTS.md", "tests", "tests.toml", "Reference"),
    UserDoc("TARGETS.md", "targets", "Targets", "Reference"),
    UserDoc("SANDBOX.md", "sandbox", "Sandbox", "Reference"),
    UserDoc("STEALTH.md", "stealth", "Stealth Mode", "Reference"),
    UserDoc("CLI-REFERENCE.md", "cli-reference", "CLI reference", "Reference"),
    UserDoc("SUPPORTED-EDA-TOOLS.md", "supported-eda-tools", "Supported EDA tools", "Reference"),
    UserDoc("DOCTOR.md", "doctor", "Doctor", "Help"),
    UserDoc("TROUBLESHOOTING.md", "troubleshooting", "Troubleshooting", "Help"),
    UserDoc("WINDOWS.md", "windows", "Windows", "Help"),
)
# Contributor docs the site also publishes, after the user docs. They stay in
# docs/internals/ for contributors and agents, and Booley never prints links
# to them, so docs_url() does not cover them.
CONTRIBUTOR_DOCS = (
    UserDoc(
        "ARCHITECTURE.md", "architecture", "Architecture", "Internals", directory="docs/internals"
    ),
)
# Everything the site's docs section publishes, in sidebar and prev/next order.
SITE_DOCS = USER_DOCS + CONTRIBUTOR_DOCS
_SLUGS = {doc.file: doc.slug for doc in USER_DOCS}


def docs_url(file: str, anchor: str | None = None) -> str:
    """Return the site URL of ``docs/user/<file>``, optionally at a heading anchor.

    Raises ``KeyError`` for a file that is not a published user doc.
    """
    url = f"{SITE_URL}docs/{_SLUGS[file]}/"
    return f"{url}#{anchor}" if anchor else url


def sandbox_doc_path(file: str) -> str:
    """Return the Sandbox path of the packaged copy of a shipped doc (``/booley-docs/<file>``)."""
    return f"{SANDBOX_DOCS_DIR}/{file}"


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
