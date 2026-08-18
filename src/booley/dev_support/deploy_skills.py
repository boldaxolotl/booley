#!/usr/bin/env python3
"""Generate thin wrapper stubs for agent runtimes from canonical Booley skill sources.

Scans the packaged skill directory for directories containing SKILL.md, parses
their YAML frontmatter, and writes minimal redirect stubs to target runtime
directories (.agents/skills/).

Usage:
    python -m booley.dev_support.deploy_skills [--target agents] [--dry-run]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import NamedTuple

# ---------------------------------------------------------------------------
# Frontmatter parsing (no PyYAML dependency)
# ---------------------------------------------------------------------------


def parse_frontmatter(text: str) -> dict[str, str]:
    """Extract YAML frontmatter fields from a ``---``-delimited block.

    Returns a dict of string key-value pairs.  Only simple ``key: value``
    lines are recognised — nested YAML is ignored.
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}

    fields: dict[str, str] = {}
    for line in lines[1:]:
        if line.strip() == "---":
            break
        if ":" in line:
            key, _, value = line.partition(":")
            key = key.strip()
            value = value.strip()
            if key and value:
                fields[key] = value
    return fields


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


class SkillSource(NamedTuple):
    """A canonical skill discovered under the packaged skills directory."""

    dir_name: str  # directory name, e.g. "booley-sim-run"
    name: str  # from frontmatter ``name``
    description: str  # from frontmatter ``description``
    source_path: Path  # absolute path to SKILL.md


class DeployResult(NamedTuple):
    """Outcome for a single stub write."""

    skill_name: str
    target: str  # "agents"
    status: str  # "created", "updated", "skipped", "error"
    detail: str  # extra info (e.g. error message)


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def discover_skills(booley_skills_dir: Path) -> list[SkillSource]:
    """Scan ``booley_skills_dir`` for subdirectories containing SKILL.md."""
    skills: list[SkillSource] = []

    if not booley_skills_dir.is_dir():
        return skills

    for child in sorted(booley_skills_dir.iterdir()):
        if not child.is_dir():
            continue
        skill_md = child / "SKILL.md"
        if not skill_md.is_file():
            continue

        text = skill_md.read_text(encoding="utf-8")
        fm = parse_frontmatter(text)

        name = fm.get("name", "")
        description = fm.get("description", "")

        if not name:
            print(f"WARNING: {skill_md} has no 'name' in frontmatter — skipping")
            continue
        if not description:
            print(f"WARNING: {skill_md} has no 'description' in frontmatter — skipping")
            continue

        # Warn if directory name doesn't start with "booley-"
        if not child.name.startswith("booley-"):
            print(f"WARNING: skill directory '{child.name}' does not start with 'booley-'")

        skills.append(
            SkillSource(
                dir_name=child.name,
                name=name,
                description=description,
                source_path=skill_md,
            )
        )

    return skills


# ---------------------------------------------------------------------------
# Stub generation
# ---------------------------------------------------------------------------


def make_stub(skill: SkillSource) -> str:
    """Build the Markdown stub content for a wrapper skill."""
    # Point at the canonical SKILL.md via its absolute installed path.
    # The stub reader (Claude Code) resolves this at runtime.
    return (
        f"---\n"
        f"name: {skill.name}\n"
        f"description: {skill.description}\n"
        f"---\n"
        f"Read and follow the instructions in `{skill.source_path}`.\n"
    )


# ---------------------------------------------------------------------------
# Deployment
# ---------------------------------------------------------------------------

# Map target name -> directory under project root
TARGET_DIRS = {
    "agents": Path(".agents") / "skills",
}


def deploy_stub(
    skill: SkillSource,
    target_name: str,
    project_root: Path,
    dry_run: bool,
) -> DeployResult:
    """Write (or preview) a single stub file.  Returns a DeployResult."""
    target_dir = project_root / TARGET_DIRS[target_name] / skill.name
    target_file = target_dir / "SKILL.md"
    stub_content = make_stub(skill)

    try:
        # Check existing content
        if target_file.is_file():
            existing = target_file.read_text(encoding="utf-8")
            if existing == stub_content:
                return DeployResult(skill.name, target_name, "skipped", "identical")
            status = "updated"
        else:
            status = "created"

        if dry_run:
            return DeployResult(skill.name, target_name, f"{status} (dry-run)", "")

        target_dir.mkdir(parents=True, exist_ok=True)
        target_file.write_text(stub_content, encoding="utf-8", newline="\n")
        return DeployResult(skill.name, target_name, status, "")

    except OSError as exc:
        return DeployResult(skill.name, target_name, "error", str(exc))


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------


def print_summary(results: list[DeployResult]) -> None:
    """Print a formatted summary table of deployment results."""
    if not results:
        print("\nNo skills to deploy.")
        return

    # Column widths
    name_w = max(len(r.skill_name) for r in results)
    tgt_w = max(len(r.target) for r in results)
    stat_w = max(len(r.status) for r in results)

    # Minimum widths for headers
    name_w = max(name_w, len("Skill"))
    tgt_w = max(tgt_w, len("Target"))
    stat_w = max(stat_w, len("Status"))

    header = f"  {'Skill':<{name_w}}  {'Target':<{tgt_w}}  {'Status':<{stat_w}}  Detail"
    sep = f"  {'-' * name_w}  {'-' * tgt_w}  {'-' * stat_w}  ------"

    print(f"\n{'=' * len(header)}")
    print("  Deploy Results")
    print(f"{'=' * len(header)}")
    print(header)
    print(sep)
    for r in results:
        detail = f"  {r.detail}" if r.detail else ""
        print(f"  {r.skill_name:<{name_w}}  {r.target:<{tgt_w}}  {r.status:<{stat_w}}{detail}")

    # Tally
    counts: dict[str, int] = {}
    for r in results:
        # Normalize dry-run statuses for counting
        base = r.status.replace(" (dry-run)", "")
        counts[base] = counts.get(base, 0) + 1
    tally = ", ".join(f"{v} {k}" for k, v in sorted(counts.items()))
    print(f"\n  Total: {len(results)} — {tally}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    """Build the CLI argument parser for deploy_skills."""
    parser = argparse.ArgumentParser(
        description="Deploy Booley skill stubs to agent runtime directories.",
    )
    parser.add_argument(
        "--target",
        choices=["agents"],
        default="agents",
        help="Target runtime to deploy to (default: agents)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be done without writing files",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    from booley.runtime.paths import skills_dir
    from booley.runtime.project_dir import resolve_project_dir

    project_root = resolve_project_dir().parent
    booley_skills_dir = skills_dir()

    if not booley_skills_dir.is_dir():
        print(f"ERROR: canonical skills directory not found: {booley_skills_dir}")
        return 1

    skills = discover_skills(booley_skills_dir)
    if not skills:
        print("No skills found in", booley_skills_dir)
        return 0

    print(f"Found {len(skills)} skill(s) in {booley_skills_dir}")
    if args.dry_run:
        print("(dry-run mode — no files will be written)\n")

    results = _deploy_all(skills, [args.target], project_root, args.dry_run)
    print_summary(results)
    return 1 if any(r.status == "error" for r in results) else 0


def _deploy_all(
    skills: list[SkillSource],
    targets: list[str],
    project_root: Path,
    dry_run: bool,
) -> list[DeployResult]:
    """Deploy stubs for all discovered skills to all targets."""
    results: list[DeployResult] = []
    for skill in skills:
        for target_name in targets:
            results.append(deploy_stub(skill, target_name, project_root, dry_run))
    return results


if __name__ == "__main__":
    sys.exit(main())
