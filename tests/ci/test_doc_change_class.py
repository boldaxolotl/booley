"""Behavioral contracts for the doc change classifier and its labeling workflow."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest
import yaml
from tests.path_tool_fake import PathToolFake
from tests.subprocess_budget import run_bounded_child

_ROOT = Path(__file__).parents[2]
_SCRIPT = _ROOT / ".github/scripts/doc_change_class.py"
_RULES = _ROOT / ".github/scripts/doc_change_rules.toml"
_WORKFLOW = _ROOT / ".github/workflows/doc-change-class.yml"

_USAGE = "# Usage\n\n## Install\n\nText\n\n### Detail\n\n```bash\n# not a heading\n```\n"
_CLI = (
    "# CLI\n\n| Command | Use |\n|---|---|\n| `chat`, `dashboard` | start |\n\n"
    "```bash\nbooley flow --model x  # comment\n```\n"
)
_CONFIG = (
    '# Config\n\n```toml\n[models]\n# reviewer = "x"\n```\n\n'
    "Pick a model per role.\n\nThe `[models]` keys are `reviewer`.\n"
)
SEED = {
    "docs/GLOSSARY.md": "# Glossary\n",
    "GLOSSARY-MAP.md": "# Map\n",
    "docs/adr/0001-x.md": "# ADR\n",
    "docs/user/USAGE.md": _USAGE,
    "docs/user/MULTI.md": "# Multi\n\nInstall\nBooley\n------\n",
    "docs/user/OTHER.md": "# Other\n\nSome other page text.\n",
    "website/index.md": "---\ntitle: T\n---\n# Site\n",
    "crates/bwave/docs/public/guide.md": "# Guide\n",
    "README.md": "# Booley\n\nIntro\n",
    "docs/internals/DOCS_PRINCIPLES.md": "# Principles\n",
    "docs/internals/USER_DOCS_PRINCIPLES.md": "# User docs principles\n",
    "docs/internals/ARCHITECTURE.md": "# Architecture\n\nBody\n",
    "AGENTS.md": "# Agents\n",
    "pkg/AGENTS.md": "# Nested agents\n",
    "CLAUDE.md": "# Claude\n",
    "src/booley/data/skills/a/SKILL.md": "# Skill\n",
    "qa/booley-qa-run/SKILL.md": "# QA skill\n",
    "docs/user/CLI-REFERENCE.md": _CLI,
    "docs/user/CONFIG.md": _CONFIG,
    "CHANGELOG.md": "# Changelog\n",
    "src/booley/x.py": "x = 1\n",
}


def _edit(path: str, old: str, new: str) -> dict[str, str | None]:
    assert old in SEED[path]
    return {path: SEED[path].replace(old, new)}


def _append(path: str, text: str) -> dict[str, str | None]:
    return {path: SEED[path] + text}


def _git(repo: Path, *args: str) -> str:
    env = os.environ | {
        "GIT_AUTHOR_NAME": "CI Test",
        "GIT_AUTHOR_EMAIL": "ci@example.invalid",
        "GIT_COMMITTER_NAME": "CI Test",
        "GIT_COMMITTER_EMAIL": "ci@example.invalid",
    }
    result = run_bounded_child(["git", "-C", str(repo), *args], check=True, env=env)
    return result.stdout.strip()


def _write(repo: Path, files: dict[str, str | None]) -> None:
    for name, text in files.items():
        path = repo / name
        if text is None:
            path.unlink()
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(text.encode("utf-8"))


def _repository(tmp_path: Path, changes: dict[str, str | None]) -> tuple[Path, str, str]:
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    _git(repo, "init", "-b", "main")
    _write(repo, dict(SEED))
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "base")
    base = _git(repo, "rev-parse", "HEAD")
    _write(repo, changes)
    _git(repo, "add", "-A")
    _git(repo, "commit", "--allow-empty", "-m", "head")
    return repo, base, _git(repo, "rev-parse", "HEAD")


def _run(
    tmp_path: Path, changes: dict[str, str | None], *extra: str
) -> tuple[int, dict[str, object], str]:
    repo, base, head = _repository(tmp_path, changes)
    result = run_bounded_child(
        [sys.executable, str(_SCRIPT), "--base", base, "--head", head, *extra], cwd=repo
    )
    payload: dict[str, object] = json.loads(result.stdout) if result.stdout.strip() else {}
    return result.returncode, payload, result.stderr


def _classify(
    tmp_path: Path, changes: dict[str, str | None], *extra: str
) -> tuple[str, dict[str, tuple[str, str]]]:
    code, payload, stderr = _run(tmp_path, changes, *extra)
    assert code == 0, stderr
    files = payload["files"]
    assert isinstance(files, list)
    return str(payload["class"]), {
        str(f["path"]): (str(f["class"]), str(f["rule"]))
        for f in files  # pyright: ignore[reportUnknownVariableType]
    }


_PREMERGE_CASES: dict[str, tuple[dict[str, str | None], dict[str, tuple[str, str]]]] = {
    "glossary": (
        _append("docs/GLOSSARY.md", "x\n"),
        {"docs/GLOSSARY.md": ("premerge", "glossary")},
    ),
    "glossary-map": (
        _append("GLOSSARY-MAP.md", "x\n"),
        {"GLOSSARY-MAP.md": ("premerge", "glossary")},
    ),
    "root-glossary": ({"GLOSSARY.md": "# Glossary\n"}, {"GLOSSARY.md": ("premerge", "glossary")}),
    "adr": (_append("docs/adr/0001-x.md", "x\n"), {"docs/adr/0001-x.md": ("premerge", "adr")}),
    "page-added": (
        {"docs/user/NEW.md": "# New\n"},
        {"docs/user/NEW.md": ("premerge", "user-page-set")},
    ),
    "website-page-added": (
        {"website/new.md": "# N\n"},
        {"website/new.md": ("premerge", "user-page-set")},
    ),
    "bwave-page-added": (
        {"crates/bwave/docs/public/new.md": "# N\n"},
        {"crates/bwave/docs/public/new.md": ("premerge", "user-page-set")},
    ),
    "page-deleted": (
        {"docs/user/OTHER.md": None},
        {"docs/user/OTHER.md": ("premerge", "user-page-set")},
    ),
    "heading-added": (
        _append("docs/user/USAGE.md", "\n## Extra\n"),
        {"docs/user/USAGE.md": ("premerge", "user-page-headings")},
    ),
    "heading-removed": (
        _edit("docs/user/USAGE.md", "## Install\n\n", ""),
        {"docs/user/USAGE.md": ("premerge", "user-page-headings")},
    ),
    "heading-renamed": (
        _edit("docs/user/USAGE.md", "## Install", "## Setup"),
        {"docs/user/USAGE.md": ("premerge", "user-page-headings")},
    ),
    "multiline-setext-heading-renamed": (
        _edit("docs/user/MULTI.md", "Install\n", "Configure\n"),
        {"docs/user/MULTI.md": ("premerge", "user-page-headings")},
    ),
    "setext-heading-added": (
        _append("docs/user/USAGE.md", "\nExtra\n-----\n"),
        {"docs/user/USAGE.md": ("premerge", "user-page-headings")},
    ),
    "level-1-heading-changed": (
        _edit("README.md", "# Booley", "# Booley 2"),
        {"README.md": ("premerge", "user-page-headings")},
    ),
    "front-matter-page-heading": (
        _edit("website/index.md", "# Site", "# Site 2"),
        {"website/index.md": ("premerge", "user-page-headings")},
    ),
    "principles": (
        _append("docs/internals/DOCS_PRINCIPLES.md", "x\n"),
        {"docs/internals/DOCS_PRINCIPLES.md": ("premerge", "principles")},
    ),
    "user-docs-principles": (
        _append("docs/internals/USER_DOCS_PRINCIPLES.md", "x\n"),
        {"docs/internals/USER_DOCS_PRINCIPLES.md": ("premerge", "principles")},
    ),
    "agents": (_append("AGENTS.md", "x\n"), {"AGENTS.md": ("premerge", "agent-instructions")}),
    "nested-agents": (
        _append("pkg/AGENTS.md", "x\n"),
        {"pkg/AGENTS.md": ("premerge", "agent-instructions")},
    ),
    "claude": (_append("CLAUDE.md", "x\n"), {"CLAUDE.md": ("premerge", "agent-instructions")}),
    "packaged-skill": (
        _append("src/booley/data/skills/a/SKILL.md", "x\n"),
        {"src/booley/data/skills/a/SKILL.md": ("premerge", "skills")},
    ),
    "qa-skill": (
        _append("qa/booley-qa-run/SKILL.md", "x\n"),
        {"qa/booley-qa-run/SKILL.md": ("premerge", "skills")},
    ),
    "cli-new-command": (
        _append("docs/user/CLI-REFERENCE.md", "\n```bash\nbooley foo\n```\n"),
        {"docs/user/CLI-REFERENCE.md": ("premerge", "cli-reference-entry")},
    ),
    "cli-new-option": (
        _edit(
            "docs/user/CLI-REFERENCE.md", "booley flow --model x", "booley flow --model x --fast"
        ),
        {"docs/user/CLI-REFERENCE.md": ("premerge", "cli-reference-entry")},
    ),
    "cli-optional-flag": (
        _edit("docs/user/CLI-REFERENCE.md", "booley flow --model x", "booley flow [--fast] x"),
        {"docs/user/CLI-REFERENCE.md": ("premerge", "cli-reference-entry")},
    ),
    "cli-table-command": (
        _edit("docs/user/CLI-REFERENCE.md", "`dashboard`", "`dashboard`, `report`"),
        {"docs/user/CLI-REFERENCE.md": ("premerge", "cli-reference-entry")},
    ),
    "config-new-key": (
        _edit("docs/user/CONFIG.md", '# reviewer = "x"\n', '# reviewer = "x"\nretries = 3\n'),
        {"docs/user/CONFIG.md": ("premerge", "config-reference-key")},
    ),
    "config-quoted-key": (
        _edit("docs/user/CONFIG.md", '# reviewer = "x"\n', '# reviewer = "x"\n"retries" = 3\n'),
        {"docs/user/CONFIG.md": ("premerge", "config-reference-key")},
    ),
    "config-dotted-key": (
        _edit("docs/user/CONFIG.md", '# reviewer = "x"\n', '# reviewer = "x"\nlimit.max = 3\n'),
        {"docs/user/CONFIG.md": ("premerge", "config-reference-key")},
    ),
    "config-prose-key": (
        _edit("docs/user/CONFIG.md", "`reviewer`.", "`reviewer` and `judge`."),
        {"docs/user/CONFIG.md": ("premerge", "config-reference-key")},
    ),
    "config-inline-table": (
        _append("docs/user/CONFIG.md", "\nSee `[goal].mode`.\n"),
        {"docs/user/CONFIG.md": ("premerge", "config-reference-key")},
    ),
}

_POSTREVIEW_CASES: dict[str, tuple[dict[str, str | None], dict[str, tuple[str, str]]]] = {
    "internal-body-edit": (
        _edit("docs/internals/ARCHITECTURE.md", "Body", "New body"),
        {"docs/internals/ARCHITECTURE.md": ("postreview", "default")},
    ),
    "level-3-heading": (
        _edit("docs/user/USAGE.md", "### Detail", "### Details"),
        {"docs/user/USAGE.md": ("postreview", "default")},
    ),
    "fenced-hash-line": (
        _edit("docs/user/USAGE.md", "# not a heading", "# still not a heading"),
        {"docs/user/USAGE.md": ("postreview", "default")},
    ),
    "cli-comment-reworded": (
        _edit("docs/user/CLI-REFERENCE.md", "# comment", "# remark"),
        {"docs/user/CLI-REFERENCE.md": ("postreview", "default")},
    ),
    "config-prose-edit": (
        _edit("docs/user/CONFIG.md", "Pick a model per role.", "Pick one model per role."),
        {"docs/user/CONFIG.md": ("postreview", "default")},
    ),
    "changelog-renamed-to-internal-doc": (
        {"CHANGELOG.md": None, "docs/internals/RELEASE-NOTES.md": SEED["CHANGELOG.md"]},
        {"docs/internals/RELEASE-NOTES.md": ("postreview", "default")},
    ),
    "changelog-fragment": (
        {"changelog.d/123.fix.md": "Fixed a thing.\n"},
        {"changelog.d/123.fix.md": ("postreview", "default")},
    ),
}

_NONE_CASES: dict[str, tuple[dict[str, str | None], dict[str, tuple[str, str]]]] = {
    "changelog": (_append("CHANGELOG.md", "x\n"), {"CHANGELOG.md": ("none", "changelog")}),
    "code-only": (_append("src/booley/x.py", "y = 2\n"), {}),
}


@pytest.mark.parametrize("name", [*_PREMERGE_CASES, *_POSTREVIEW_CASES, *_NONE_CASES])
def test_classifies_each_rule_kind(tmp_path: Path, name: str) -> None:
    expected_class = (
        "premerge"
        if name in _PREMERGE_CASES
        else "postreview"
        if name in _POSTREVIEW_CASES
        else "none"
    )
    changes, expected_files = (_PREMERGE_CASES | _POSTREVIEW_CASES | _NONE_CASES)[name]
    assert _classify(tmp_path, changes) == (expected_class, expected_files)


def test_mixed_pr_takes_the_highest_class(tmp_path: Path) -> None:
    changes = _edit("docs/internals/ARCHITECTURE.md", "Body", "New") | _append(
        "docs/adr/0001-x.md", "x\n"
    )
    assert _classify(tmp_path, changes) == (
        "premerge",
        {
            "docs/internals/ARCHITECTURE.md": ("postreview", "default"),
            "docs/adr/0001-x.md": ("premerge", "adr"),
        },
    )


def test_rename_reports_the_old_path(tmp_path: Path) -> None:
    changes: dict[str, str | None] = {
        "docs/user/OTHER.md": None,
        "docs/user/MOVED.md": SEED["docs/user/OTHER.md"],
    }
    _, payload, _ = _run(tmp_path, changes)
    assert payload["files"] == [
        {
            "path": "docs/user/MOVED.md",
            "status": "renamed",
            "old_path": "docs/user/OTHER.md",
            "class": "premerge",
            "rule": "user-page-set",
        }
    ]


def _rules_text() -> str:
    return _RULES.read_text(encoding="utf-8")


_MALFORMED = [
    ('doc_paths = ["*.md", "docs/*"]', "doc_paths = [", "cannot read rules"),
    (
        'name = "adr"\nclass = "premerge"',
        'name = "adr"\nclass = "pre"',
        "class 'pre' is not one of",
    ),
    ('check = "page-set"', 'check = "bogus"', "check 'bogus' is not one of"),
    ('\nclass = "none"', '\nclas = "none"', "each kind needs exactly"),
    ('name = "adr"', 'name = "glossary"', "unique non-empty string"),
    ('default_class = "postreview"', 'default_class = "x"', "default_class 'x' is not one of"),
    ('paths = ["docs/adr/*"]', "paths = []", "paths must be a non-empty list"),
    ('doc_paths = ["*.md", "docs/*"]', "", "top-level keys must be exactly"),
]


@pytest.mark.parametrize(("old", "new", "problem"), _MALFORMED)
def test_malformed_rules_exit_nonzero_naming_the_problem(
    tmp_path: Path, old: str, new: str, problem: str
) -> None:
    text = _rules_text()
    assert old in text, old
    rules = tmp_path / "doc_change_rules.toml"
    rules.write_bytes(text.replace(old, new, 1).encode("utf-8"))
    code, payload, stderr = _run(
        tmp_path / "r", _append("docs/adr/0001-x.md", "x\n"), "--rules", str(rules)
    )
    assert code == 2, stderr
    assert payload == {}
    assert "doc_change_rules.toml" in stderr
    assert problem in stderr


def test_missing_rules_file_and_bad_revision_exit_2(tmp_path: Path) -> None:
    code, _, stderr = _run(tmp_path / "a", {}, "--rules", str(tmp_path / "missing.toml"))
    assert code == 2
    assert "cannot read rules" in stderr
    repo, _, head = _repository(tmp_path / "b", {})
    result = run_bounded_child(
        [sys.executable, str(_SCRIPT), "--base", "no-such-rev", "--head", head], cwd=repo
    )
    assert result.returncode == 2
    assert "git merge-base" in result.stderr


def test_moving_a_kind_is_a_rules_file_edit(tmp_path: Path) -> None:
    old = 'name = "adr"\nclass = "premerge"'
    text = _rules_text()
    assert old in text
    alternate = tmp_path / "alternate.toml"
    alternate.write_bytes(
        text.replace(old, 'name = "adr"\nclass = "postreview"', 1).encode("utf-8")
    )
    assert _classify(
        tmp_path / "moved", _append("docs/adr/0001-x.md", "x\n"), "--rules", str(alternate)
    ) == ("postreview", {"docs/adr/0001-x.md": ("postreview", "adr")})


_LABELS = "repos/{owner}/{repo}/issues/7/labels"
_LABEL_CASES = [
    (
        _append("docs/adr/0001-x.md", "x\n"),
        "docs-postreview bug",
        [
            f"--method POST {_LABELS} -f labels[]=docs-premerge",
            f"--method DELETE {_LABELS}/docs-postreview",
        ],
    ),
    (
        _edit("docs/internals/ARCHITECTURE.md", "Body", "New"),
        "bug",
        [f"--method POST {_LABELS} -f labels[]=docs-postreview"],
    ),
    (
        _append("CHANGELOG.md", "x\n"),
        "docs-premerge",
        [f"--method DELETE {_LABELS}/docs-premerge"],
    ),
    (_append("docs/adr/0001-x.md", "x\n"), "docs-premerge", []),
]


@pytest.mark.parametrize(("changes", "present", "writes"), _LABEL_CASES)
def test_label_pr_sets_swaps_and_clears_labels(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    changes: dict[str, str | None],
    present: str,
    writes: list[str],
) -> None:
    gh = PathToolFake.install(tmp_path / "bin", "gh", monkeypatch)
    gh.answer("api", "--paginate", stdout=present + "\n")
    gh.answer("api", "--method")

    code, _, stderr = _run(tmp_path / "r", changes, "--label-pr", "7")

    assert code == 0, stderr
    calls = [" ".join(call.args) for call in gh.calls()]
    assert calls[0] == f"api --paginate {_LABELS} --jq .[].name"
    assert calls[1:] == [f"api {write}" for write in writes]


def test_label_pr_failure_exits_1_naming_the_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gh = PathToolFake.install(tmp_path / "bin", "gh", monkeypatch)
    gh.answer("api", stderr="boom\n", returncode=1)

    code, payload, stderr = _run(
        tmp_path / "r", _append("docs/adr/0001-x.md", "x\n"), "--label-pr", "7"
    )

    assert code == 1
    assert payload["class"] == "premerge"
    assert "gh api --paginate" in stderr
    assert "boom" in stderr


def test_workflow_labels_on_open_and_each_push() -> None:
    text = _WORKFLOW.read_text(encoding="utf-8")
    workflow: dict[object, object] = yaml.safe_load(text)
    triggers = workflow.get("on", workflow.get(True))
    assert isinstance(triggers, dict)
    types = triggers["pull_request_target"]["types"]  # pyright: ignore[reportUnknownVariableType]
    assert {"opened", "synchronize"} <= set(types)  # pyright: ignore[reportUnknownArgumentType]
    assert workflow["permissions"] == {"contents": "read", "pull-requests": "write"}
    job = workflow["jobs"]["doc-change-class"]  # pyright: ignore[reportIndexIssue]
    assert "if" not in job
    runs = [step.get("run", "") for step in job["steps"]]  # pyright: ignore[reportUnknownVariableType]
    classify = [run for run in runs if "doc_change_class.py" in run]  # pyright: ignore[reportUnknownVariableType]
    assert len(classify) == 1  # pyright: ignore[reportUnknownArgumentType]
    for flag in ("--base", "--head", "--label-pr"):
        assert flag in classify[0]  # pyright: ignore[reportUnknownVariableType]
    assert "secrets." not in text
