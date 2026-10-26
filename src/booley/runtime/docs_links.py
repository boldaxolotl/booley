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

SITE_URL = "https://booley.dev/"


class UserDoc(NamedTuple):
    """One user doc: its file in ``docs/user/``, site slug, nav label, and sidebar group.

    ``parent`` names the file of the doc this one belongs to; the sidebar
    indents it under that doc. A child follows its parent or a sibling.
    """

    file: str
    slug: str
    title: str
    section: str
    parent: str | None = None


# Reading order: the site's docs sidebar and prev/next links follow it. The
# sidebar groups consecutive docs that share a section.
USER_DOCS = (
    UserDoc("FEATURES.md", "features", "Features", "Get started"),
    UserDoc("SETUP.md", "setup", "Setup", "Get started"),
    UserDoc("USAGE.md", "usage", "Usage", "Get started"),
    UserDoc("GOAL-MODE.md", "goal-mode", "Goal Mode", "Get started"),
    UserDoc("FLOW_REFERENCE.md", "flow-reference", "Running Flows", "Flows"),
    UserDoc("FLOW-SIM.md", "flow-sim", "Simulation", "Flows"),
    UserDoc("TESTS.md", "tests", "Tests", "Flows", parent="FLOW-SIM.md"),
    UserDoc("COVERAGE.md", "coverage", "Coverage", "Flows", parent="FLOW-SIM.md"),
    UserDoc("FLOW-LINT.md", "flow-lint", "Lint", "Flows"),
    UserDoc("FLOW-SYNTH.md", "flow-synth", "ASIC synthesis", "Flows"),
    UserDoc("FLOW-FPGA.md", "flow-fpga", "FPGA implementation", "Flows"),
    UserDoc("CONFIG.md", "config", "Config", "Reference"),
    UserDoc("TARGETS.md", "targets", "Targets", "Reference"),
    UserDoc("SANDBOX.md", "sandbox", "Sandbox", "Reference"),
    UserDoc("STEALTH.md", "stealth", "Stealth Mode", "Reference"),
    UserDoc("CLI-REFERENCE.md", "cli-reference", "CLI reference", "Reference"),
    UserDoc("SUPPORTED-EDA-TOOLS.md", "supported-eda-tools", "Supported EDA tools", "Reference"),
    UserDoc("DOCTOR.md", "doctor", "Doctor", "Help"),
    UserDoc("TROUBLESHOOTING.md", "troubleshooting", "Troubleshooting", "Help"),
    UserDoc("WINDOWS.md", "windows", "Windows", "Help"),
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
