"""Persist and validate the maintainer opt-in for repository-owned QA skills."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from uuid import uuid4

from booley.core.boundary import BoundaryError, require_dict, require_int, require_str
from booley.runtime.checkout_role import is_booley_source_checkout, source_checkout_root

QA_SKILL_NAMES = frozenset({"booley-qa-run", "booley-qa-triage", "booley-add-to-qa"})
_SCHEMA_VERSION = 1
_STATE_FILENAME = "booley-qa-skills.json"
_EPHEMERAL_PARTS = frozenset({".runtime", ".worktrees", "qa-runs"})


class QaSkillSelectionError(RuntimeError):
    """The persisted or requested QA skill source is not safe to use."""


@dataclass(frozen=True, slots=True)
class QaSkillSelection:
    """One validated durable QA skill source selection."""

    schema_version: int
    checkout_root: str
    source_revision: str


def selection_path() -> Path:
    """Return the HOME-stable cross-agent selection path."""
    return Path.home() / ".agents" / _STATE_FILENAME


def _parse(raw: object) -> QaSkillSelection:
    values = require_dict(raw, field="QA skill selection")
    expected = {"schema_version", "checkout_root", "source_revision"}
    if set(values) != expected:
        unknown = sorted(set(values) - expected)
        missing = sorted(expected - set(values))
        raise BoundaryError(
            f"QA skill selection fields differ: unknown={unknown}, missing={missing}"
        )
    schema = require_int(values.get("schema_version"), field="schema_version")
    if schema != _SCHEMA_VERSION:
        raise BoundaryError(f"unsupported QA skill selection schema {schema}")
    checkout = require_str(values, "checkout_root")
    root = Path(checkout)
    if not root.is_absolute() or checkout != str(root.resolve(strict=False)):
        raise BoundaryError("checkout_root must be an absolute normalized path")
    revision = require_str(values, "source_revision")
    if len(revision) != 40 or any(character not in "0123456789abcdef" for character in revision):
        raise BoundaryError("source_revision must be a full lowercase Git object ID")
    return QaSkillSelection(schema, checkout, revision)


def inspect_selection(path: Path | None = None) -> QaSkillSelection | None:
    """Read the strict selection document, or return ``None`` when disabled."""
    source = path or selection_path()
    try:
        return _parse(json.loads(source.read_text(encoding="utf-8")))
    except FileNotFoundError:
        return None
    except (BoundaryError, json.JSONDecodeError, OSError, UnicodeError) as exc:
        raise QaSkillSelectionError(f"cannot read QA skill selection {source}: {exc}") from exc


def _git(root: Path, *args: str) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise QaSkillSelectionError(f"cannot inspect QA source checkout {root}: {exc}") from exc
    if result.returncode:
        detail = (result.stderr or result.stdout).strip().splitlines()
        suffix = f": {detail[0][:200]}" if detail else ""
        raise QaSkillSelectionError(f"Git could not inspect QA source checkout {root}{suffix}")
    return result.stdout.strip()


def _ephemeral_error(root: Path) -> str | None:
    normalized_parts = {os.path.normcase(part) for part in root.parts}
    if normalized_parts & {os.path.normcase(part) for part in _EPHEMERAL_PARTS}:
        return f"QA source checkout is inside ephemeral workspace state: {root}"
    temporary = Path(tempfile.gettempdir()).resolve()
    if root == temporary or root.is_relative_to(temporary):
        return f"QA source checkout is inside temporary filesystem state: {root}"
    return None


def validate_checkout(root: Path, installed_revision: str) -> QaSkillSelection:
    """Validate one clean primary ``main`` checkout against the installed wheel."""
    checkout = root.resolve()
    problems: list[str] = []
    if not is_booley_source_checkout(checkout):
        problems.append("path is not a complete Booley source checkout")
    try:
        top = Path(_git(checkout, "rev-parse", "--show-toplevel")).resolve()
        if top != checkout:
            problems.append(f"Git top-level is {top}, not {checkout}")
        if not (checkout / ".git").is_dir():
            problems.append("checkout is a linked worktree, not the primary checkout")
        if _git(checkout, "branch", "--show-current") != "main":
            problems.append("checkout must be on branch main")
        if _git(checkout, "status", "--porcelain"):
            problems.append("checkout must be clean")
        head = _git(checkout, "rev-parse", "HEAD")
    except QaSkillSelectionError as exc:
        problems.append(str(exc))
        head = ""
    if error := _ephemeral_error(checkout):
        problems.append(error)
    if not installed_revision or installed_revision.endswith("+dirty"):
        problems.append("canonical installed Booley has no clean source revision")
    elif head and not head.startswith(installed_revision):
        problems.append(
            f"checkout revision {head[:12]} does not match canonical installed revision "
            f"{installed_revision}"
        )
    qa_root = checkout / "qa"
    for name in sorted(QA_SKILL_NAMES):
        skill = qa_root / name
        if not skill.is_dir() or not (skill / "SKILL.md").is_file():
            problems.append(f"QA skill is missing or incomplete: {skill}")
    if problems:
        raise QaSkillSelectionError("; ".join(problems))
    return QaSkillSelection(_SCHEMA_VERSION, str(checkout), head)


def validate_selection(
    installed_revision: str, path: Path | None = None
) -> QaSkillSelection | None:
    """Inspect and revalidate the active selection without writing."""
    recorded = inspect_selection(path)
    if recorded is None:
        return None
    validated = validate_checkout(Path(recorded.checkout_root), installed_revision)
    if validated.source_revision != recorded.source_revision:
        raise QaSkillSelectionError(
            "QA source checkout revision changed; restore it or rerun "
            "`booley bootstrap --update --with-qa-skills`"
        )
    return recorded


def enable(
    checkout_root: Path,
    installed_revision: str,
    path: Path | None = None,
) -> QaSkillSelection:
    """Validate and atomically persist one QA source checkout."""
    selection = validate_checkout(checkout_root, installed_revision)
    destination = path or selection_path()
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = destination.with_name(f".{destination.name}.{uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(asdict(selection), indent=2) + "\n", encoding="utf-8")
        if os.name != "nt":
            temporary.chmod(0o600)
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
    return selection


def disable(path: Path | None = None) -> None:
    """Disable QA skills without parsing a possibly corrupt document."""
    (path or selection_path()).unlink(missing_ok=True)


def active_qa_source_root(installed_revision: str) -> Path | None:
    """Return the validated active QA directory, or ``None`` when disabled."""
    selection = validate_selection(installed_revision)
    return None if selection is None else Path(selection.checkout_root) / "qa"


def checkout_enclosing_cwd(cwd: Path | None = None) -> Path | None:
    """Return the enclosing Booley source checkout for explicit enablement."""
    return source_checkout_root(cwd or Path.cwd())
