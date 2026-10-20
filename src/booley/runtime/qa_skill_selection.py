"""Persist and validate the maintainer opt-in for repository-owned QA skills."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from uuid import uuid4

from booley.core.boundary import BoundaryError, require_dict, require_str
from booley.runtime.checkout_role import is_booley_source_checkout, source_checkout_root

QA_SKILL_NAMES = frozenset({"booley-qa-run", "booley-qa-triage", "booley-add-to-qa"})
_STATE_FILENAME = "booley-qa-skills.json"


class QaSkillSelectionError(RuntimeError):
    """The persisted or requested QA skill source is not safe to use."""


@dataclass(frozen=True, slots=True)
class QaSkillSelection:
    """One durable QA skill source selection: the live checkout path."""

    checkout_root: str


def selection_path() -> Path:
    """Return the HOME-stable cross-agent selection path."""
    return Path.home() / ".agents" / _STATE_FILENAME


def _parse(raw: object) -> QaSkillSelection:
    # Extra fields written by earlier releases are ignored, so their documents stay enabled.
    values = require_dict(raw, field="QA skill selection")
    checkout = require_str(values, "checkout_root")
    return QaSkillSelection(checkout)


def inspect_selection(path: Path | None = None) -> QaSkillSelection | None:
    """Read the selection document, or return ``None`` when disabled."""
    source = path or selection_path()
    try:
        return _parse(json.loads(source.read_text(encoding="utf-8")))
    except FileNotFoundError:
        return None
    except (BoundaryError, json.JSONDecodeError, OSError, UnicodeError) as exc:
        raise QaSkillSelectionError(f"cannot read QA skill selection {source}: {exc}") from exc


def validate_checkout(root: Path) -> Path:
    """Return the QA directory of a Booley source checkout with every QA skill."""
    checkout = root.resolve()
    problems: list[str] = []
    if not is_booley_source_checkout(checkout):
        problems.append(f"{checkout} is not a Booley source checkout")
    qa_root = checkout / "qa"
    for name in sorted(QA_SKILL_NAMES):
        skill = qa_root / name
        if not skill.is_dir() or not (skill / "SKILL.md").is_file():
            problems.append(f"QA skill is missing or incomplete: {skill}")
    if problems:
        raise QaSkillSelectionError("; ".join(problems))
    return qa_root


def enable(cwd: Path, path: Path | None = None) -> QaSkillSelection:
    """Validate and atomically persist the primary source checkout enclosing *cwd*."""
    checkout = source_checkout_root(cwd)
    # A linked worktree has a ``.git`` file; its links would dangle once it is removed.
    if checkout is None or not (checkout / ".git").is_dir():
        raise QaSkillSelectionError("QA skills require a primary Booley source checkout")
    validate_checkout(checkout)
    selection = QaSkillSelection(str(checkout))
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


def active_qa_source_root() -> Path | None:
    """Return the recorded QA directory, or ``None`` when disabled."""
    selection = inspect_selection()
    return None if selection is None else validate_checkout(Path(selection.checkout_root))
