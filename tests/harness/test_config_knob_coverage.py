"""Every booley.toml knob documented in CONFIG.md must have a reader in src/.

The recurring "inert knob" bug class: a documented knob exists but nothing
reads it — ``[sandbox].memory`` was hardcoded (1142d2e), ``[models]`` was inert
(74f5781), the agent provider was ignored (645f98a), and ``[project].description``
was documented for months with no read-site anywhere. Each was found by a user
setting the knob and watching nothing happen. This test is the mechanical
ratchet: it extracts every knob CONFIG.md documents in a fenced ``toml`` block
under a recognized booley.toml table and asserts the key appears as a config
read in production code (Python string literal in a read-ish context, or a bare
occurrence in a shell script).

Known limitation, on purpose: matching is by key literal, not by (section, key)
data flow, so a generic key name that any code mentions (``name``, ``image``,
``enabled``) passes trivially. The test earns its keep on the distinctive names
(``host_sim_link_dirs``, ``sim_time_grace_s``, ``mount_host_skills``) — which is
what new knobs look like. Complementary runtime guard: doctor's
Doctor's selective-Flow-knob guard warns when a live knob is set under a Flow that does
not read it.
"""

from __future__ import annotations

import re
from pathlib import Path

from booley.audit.project_schema import KNOWN_BOOLEY_TOML_TABLES

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_MD = REPO_ROOT / "docs" / "user" / "CONFIG.md"
SRC_ROOT = REPO_ROOT / "src" / "booley"

# Documented knobs with no code reader, each with a reviewed justification.
# Adding to this list requires the same scrutiny as adding a retired table.
_INERT_BY_DESIGN: dict[tuple[str, str], str] = {
    ("project", "description"): "free-text metadata for humans; CONFIG.md says so explicitly",
}

_FENCE_RE = re.compile(r"^```(\w*)\s*$")
_SECTION_RE = re.compile(r"^\s*\[([A-Za-z0-9_.\-]+)\]")
# A knob line inside a fenced toml block: `key = ...`, optionally commented out
# (CONFIG.md shows optional knobs as `# enabled = true`). The identifier-then-=
# shape keeps prose comments (`# placeholder constraints, so ...`) out.
_KNOB_RE = re.compile(r"^\s*(?:#\s*)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s")


def _documented_knobs() -> dict[tuple[str, str], str]:
    """(root_table, key) -> "line N" for every knob CONFIG.md documents.

    Only keys inside a ``[section]`` whose root is a recognized booley.toml
    table count: CONFIG.md also shows ``tests.toml`` / ``.core`` examples, whose
    sections (Target names, CAPI2 keys) are not booley.toml tables and are
    naturally excluded by the root check.
    """
    knobs: dict[tuple[str, str], str] = {}
    in_toml = False
    section_root: str | None = None
    for lineno, line in enumerate(CONFIG_MD.read_text(encoding="utf-8").splitlines(), 1):
        fence = _FENCE_RE.match(line)
        if fence:
            in_toml = not in_toml and fence.group(1) == "toml"
            section_root = None
            continue
        if not in_toml:
            continue
        section = _SECTION_RE.match(line)
        if section:
            root = section.group(1).split(".")[0]
            section_root = root if root in KNOWN_BOOLEY_TOML_TABLES else None
            continue
        if section_root is None:
            continue
        knob = _KNOB_RE.match(line)
        if knob:
            knobs.setdefault((section_root, knob.group(1)), f"docs/user/CONFIG.md:{lineno}")
    return knobs


def _source_corpus() -> tuple[str, str]:
    """(python_text, shell_text) — all of src/booley concatenated per language."""
    py, sh = [], []
    for path in sorted(SRC_ROOT.rglob("*")):
        if "__pycache__" in path.parts or not path.is_file():
            continue
        if path.suffix == ".py":
            py.append(path.read_text(encoding="utf-8", errors="replace"))
        elif path.suffix == ".sh":
            sh.append(path.read_text(encoding="utf-8", errors="replace"))
    return "\n".join(py), "\n".join(sh)


def _has_reader(key: str, py_text: str, sh_text: str) -> bool:
    """True when *key* shows up in a config-read-ish context somewhere in src.

    Read-ish: a quoted literal fed to ``.get``/subscript/``in``/a validator call
    (matched loosely as the quoted string anywhere — every observed reader shape
    uses the key as a string literal), or the bare word in a shell script.
    """
    quoted = re.compile(rf"""["']{re.escape(key)}["']""")
    if quoted.search(py_text):
        return True
    return re.search(rf"\b{re.escape(key)}\b", sh_text) is not None


def test_every_documented_knob_has_a_reader():
    documented = _documented_knobs()
    # Self-check: the extractor must actually find the doc's knobs — a CONFIG.md
    # restructure that breaks parsing would otherwise pass vacuously.
    assert len(documented) >= 25, (
        f"CONFIG.md knob extraction found only {len(documented)} knobs — "
        "the doc structure changed; update _documented_knobs()"
    )

    py_text, sh_text = _source_corpus()
    inert = {
        f"[{table}].{key} ({where})"
        for (table, key), where in documented.items()
        if (table, key) not in _INERT_BY_DESIGN and not _has_reader(key, py_text, sh_text)
    }
    assert not inert, (
        "CONFIG.md documents knobs with no reader anywhere in src/booley — "
        "either the knob was renamed/removed in code (fix the doc), the reader "
        "was never written (fix the code), or it is deliberately informational "
        f"(add to _INERT_BY_DESIGN with a justification): {sorted(inert)}"
    )
