"""Architecture guard: review verdict suffixes are parsed only by the verdict model.

`booley.evidence.review_verdicts` is the one place that knows how a verdict is spelled
inside a review criterion key (`review_<family>_<verdict>`). Everything else must call its
splitter and predicates, so adding a verdict stays a table change. The scanner flags:

  * a string constant equal to `_<verdict>` for a registered verdict (`endswith`,
    `removesuffix`, slicing by `len`, f-string or concatenated key building),
  * `rsplit("_")` / `rpartition("_")` calls (splitting a key at its last underscore),
  * any reference to `REVIEW_VERDICTS` / `VERDICT_RULES` (the way to loop over verdict
    names and build a suffix test from them).

A genuinely unrelated use goes in `_ALLOWLIST` with a one-line reason.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_SRC_ROOT = Path(__file__).resolve().parents[2] / "src" / "booley"
_MODEL = "evidence/review_verdicts.py"
_SUFFIX = "suffix-constant"
_RSPLIT = "underscore-split"
_REGISTRY = "verdict-registry"
_REGISTRY_NAMES = frozenset({"REVIEW_VERDICTS", "VERDICT_RULES"})

# (relative posix path under src/booley, rule) -> reason.
_ALLOWLIST: dict[tuple[str, str], str] = {
    (
        "flows/endpoint_report_criteria.py",
        _RSPLIT,
    ): "strips a Target name from `<family>_<target>`, never a verdict",
    ("goals/model.py", _REGISTRY): "validates a parsed verdict and writes the JSON schema enum",
    ("goals/capabilities.py", _REGISTRY): "builds the finite legacy-review registry",
    ("goals/goalsets.py", _REGISTRY): "builds the Goalset review-name pattern",
}


def scan_for_suffix_parsing(source: str, verdicts: tuple[str, ...]) -> list[tuple[int, str]]:
    """Return (line, rule) for every verdict-suffix parsing construct in the source."""
    suffixes = {f"_{verdict}" for verdict in verdicts}
    hits: list[tuple[int, str]] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Constant) and node.value in suffixes:
            hits.append((node.lineno, _SUFFIX))
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in {"rsplit", "rpartition"}
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and node.args[0].value == "_"
        ):
            hits.append((node.lineno, _RSPLIT))
        elif (isinstance(node, ast.Name) and node.id in _REGISTRY_NAMES) or (
            isinstance(node, ast.alias) and node.name in _REGISTRY_NAMES
        ):
            hits.append((node.lineno, _REGISTRY))
    return hits


def test_no_verdict_suffix_parsing_outside_verdict_model() -> None:
    import booley.evidence.review_verdicts as rv

    offenders: list[str] = []
    for path in sorted(_SRC_ROOT.rglob("*.py")):
        relative = path.relative_to(_SRC_ROOT).as_posix()
        if relative == _MODEL:
            continue
        for line, rule in scan_for_suffix_parsing(
            path.read_text(encoding="utf-8"), tuple(rv.VERDICT_RULES)
        ):
            if (relative, rule) not in _ALLOWLIST:
                offenders.append(f"{relative}:{line} [{rule}]")
    assert not offenders, (
        "review verdict suffixes must be parsed through booley.evidence.review_verdicts "
        "(split_review_key, review_family, review_key, is_*_review_key):\n" + "\n".join(offenders)
    )


def test_allowlist_entries_are_still_needed() -> None:
    import booley.evidence.review_verdicts as rv

    for relative, rule in _ALLOWLIST:
        source = (_SRC_ROOT / relative).read_text(encoding="utf-8")
        hits = scan_for_suffix_parsing(source, tuple(rv.VERDICT_RULES))
        assert any(hit_rule == rule for _, hit_rule in hits), f"stale allowlist: {relative} {rule}"


_FLAGGED_SNIPPETS = [
    'key.endswith("_done")',
    'key.removesuffix("_clean")',
    'f"{base}_clean"',
    'base + "_done"',
    'key.rsplit("_", 1)[0]',
    'key.rpartition("_")',
    'any(key.endswith(f"_{verdict}") for verdict in REVIEW_VERDICTS)',
    "from booley.evidence.review_verdicts import VERDICT_RULES",
]


def test_scanner_flags_suffix_parsing() -> None:
    import booley.evidence.review_verdicts as rv

    verdicts = tuple(rv.VERDICT_RULES)
    missed = [
        snippet for snippet in _FLAGGED_SNIPPETS if not scan_for_suffix_parsing(snippet, verdicts)
    ]
    assert not missed


@pytest.mark.parametrize(
    "snippet",
    [
        "split_review_key(key)",
        "is_advisory_review_key(key)",
        'key.rsplit(":", 1)',
        'name.endswith("_pass")',
        '"lint_clean"',
    ],
)
def test_scanner_accepts_model_calls(snippet: str) -> None:
    import booley.evidence.review_verdicts as rv

    assert scan_for_suffix_parsing(snippet, tuple(rv.VERDICT_RULES)) == []
