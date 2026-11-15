"""Goal Mode is removed: no live surface still names it (#3511).

Only the code that refuses or reports a leftover ``[goals]`` section or ``goals/`` directory,
the tests of that code, the documentation sections about it, and history (changelogs, ADRs,
validation notes, README, the website and QA prose that later issues rewrite) may say so.
"""

from __future__ import annotations

import re
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SCANNED = ("src", "docs/user", "docs/internals", ".github", "tests", "pyproject.toml")
SKIPPED_PARTS = frozenset({"__pycache__", "validation", ".git", "node_modules"})
SUFFIXES = frozenset({".py", ".md", ".toml", ".yml", ".yaml", ".sh", ".txt", ".cfg"})
TERMS = (
    "booley goal",
    "goal_enter",
    "goal_status",
    "goal_propose_change",
    "goal_finish",
    "/booley-goal",
    "booley.goals",
    "GOAL-MODE.md",
    "cheat --goals",
    "[goals]",
    "goalsets/",
    "Goal Mode",
    "Goalset",
)
# Files that may name the terms anywhere: the refusal and leftover code, the kept
# leftover-recognition modules, their tests, the guards, and history.
ALLOWED_FILES = frozenset(
    {
        "src/booley/config/retired.py",
        "src/booley/audit/project_schema.py",
        "src/booley/harness/doctor.py",
        "src/booley/core/booley_layout.py",
        "src/booley/core/booley_dir_parts.py",
        "src/booley/agent_workspace/isolation.py",
        "src/booley/runtime/booley_dir_migration.py",
        "src/booley/data/refs/CHANGELOG.md",
        "tests/test_retired_goal_mode_wording.py",
        "tests/architecture/test_goal_mode_boundaries.py",
        "tests/config/test_retired.py",
        "tests/core/test_booley_dir_parts.py",
        "tests/harness/test_goal_mode_removed.py",
        "tests/harness/test_cli_retired_goals_section.py",
        "tests/harness/test_doctor_goal_leftovers.py",
        "tests/harness/test_doctor_retired_goals_section.py",
        "tests/harness/test_cheatsheet.py",
        "tests/harness/test_init_rerun.py",
        "tests/harness/test_measure_command_flows.py",
        "tests/mcp_tools/test_mcp_dispatch.py",
        "tests/runtime/test_docs_links.py",
    }
)
# Documentation sections about the refusal and the leftovers, by exact heading.
ALLOWED_SECTIONS: dict[str, frozenset[str]] = {
    "docs/user/CONFIG.md": frozenset({"A `[goals]` section is refused"}),
    "docs/user/TROUBLESHOOTING.md": frozenset({"A `[goals]` section in `booley.toml`"}),
    "docs/user/DOCTOR.md": frozenset({"Goal Mode leftovers"}),
}
_HEADING = re.compile(r"^#+\s+(.*?)\s*$")


def surfaces(
    root: Path, allowed_files: frozenset[str], allowed_sections: dict[str, frozenset[str]]
) -> list[str]:
    """Every ``path:line: term`` outside the allowlist, under the scanned roots of *root*."""
    found: list[str] = []
    for scanned in SCANNED:
        base = root / scanned
        paths = [base] if base.is_file() else sorted(base.rglob("*")) if base.is_dir() else []
        for path in paths:
            relative = path.relative_to(root).as_posix()
            if (
                not path.is_file()
                or path.suffix not in SUFFIXES
                or SKIPPED_PARTS & set(path.relative_to(root).parts)
                or relative in allowed_files
            ):
                continue
            heading = ""
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
                if path.suffix == ".md" and (match := _HEADING.match(line)):
                    heading = match.group(1)
                if heading in allowed_sections.get(relative, frozenset()):
                    continue
                found.extend(f"{relative}:{number}: {term}" for term in TERMS if term in line)
    return found


def test_no_goal_mode_surface_outside_the_allowlist() -> None:
    assert surfaces(REPOSITORY_ROOT, ALLOWED_FILES, ALLOWED_SECTIONS) == []


def test_the_allowlist_accepts_a_refusal_line_and_rejects_a_live_surface(tmp_path: Path) -> None:
    refusal = tmp_path / "src" / "booley" / "config" / "retired.py"
    live = tmp_path / "src" / "booley" / "mcp" / "tool.py"
    guide = tmp_path / "docs" / "user" / "CONFIG.md"
    for path in (refusal, live, guide):
        path.parent.mkdir(parents=True, exist_ok=True)
    refusal.write_text('MESSAGE = "booley.toml has a [goals] section"\n', encoding="utf-8")
    live.write_text("goal_status(rules=True)\n", encoding="utf-8")
    guide.write_text(
        "# Config\n\n## A `[goals]` section is refused\n\nGoal Mode was removed.\n\n"
        "## Other\n\nUse goalsets/ here.\n",
        encoding="utf-8",
    )

    assert surfaces(tmp_path, ALLOWED_FILES, ALLOWED_SECTIONS) == [
        "src/booley/mcp/tool.py:1: goal_status",
        "docs/user/CONFIG.md:9: goalsets/",
    ]


_FEATURE_TERMS = (
    "Goal Mode",
    "Goalset",
    "Result Cache",
    "booley goal",
    "Sign-off run",
    "Sign-off Report",
    "sign-off flow",
    "signed-off",
)


def _readme_without_what_it_does(text: str) -> str:
    return re.sub(r"^## What it does\n.*?(?=^## )", "", text, flags=re.M | re.S)


def test_no_retired_acceptance_feature_in_user_facing_docs() -> None:
    """User guides, the README (outside "What it does") and the site pages name no retired feature."""
    pages = [
        *sorted((REPOSITORY_ROOT / "docs/user").glob("*.md")),
        *sorted((REPOSITORY_ROOT / "website/docs/docs").rglob("*.md")),
        REPOSITORY_ROOT / "README.md",
    ]
    found: list[str] = []
    for path in pages:
        relative = path.relative_to(REPOSITORY_ROOT).as_posix()
        text = path.read_text(encoding="utf-8")
        if relative == "README.md":
            text = _readme_without_what_it_does(text)
        heading = ""
        for number, line in enumerate(text.splitlines(), start=1):
            if match := _HEADING.match(line):
                heading = match.group(1)
            if heading in ALLOWED_SECTIONS.get(relative, frozenset()):
                continue
            found.extend(f"{relative}:{number}: {term}" for term in _FEATURE_TERMS if term in line)
    assert found == []
