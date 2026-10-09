"""Current user references and rendered CLI help describe the installed version.

Setuptools ships data/**/*; runtime resource readers use refs, skills, the
cheatsheet, and criteria.toml. Docker and vendored Edalize files implement the
runtime, licenses contain legal text, and CHANGELOG.md records release history.
All other package text, including code examples and template comments, is in
scope. New package directories therefore enter the gate automatically.
"""

from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch

import pytest

from booley.bwave import cli as bwave_cli
from booley.flows.base import BuiltinFlow
from booley.flows.builtin_cli import build_parser
from booley.flows.cli_selection import human_parser
from booley.flows.sim import campaign_retention
from booley.harness.booley import _build_parser, _load_mcp_tool_class
from booley.mcp.registry import discover_mcp_tools
from booley.ticket_board.cli import build_parser as ticket_parser

ROOT = Path(__file__).resolve().parents[2]
HISTORY_POLICY = json.loads(
    (Path(__file__).with_name("current_version_history_policy.json")).read_text()
)
_HISTORY_PHRASES = "|".join(re.escape(phrase) for phrase in HISTORY_POLICY["phrases"])
_VERSION_PREFIXES = "|".join(re.escape(prefix) for prefix in HISTORY_POLICY["version_prefixes"])
HISTORY = re.compile(
    rf"\b(?:{_HISTORY_PHRASES})\b|\b(?:{_VERSION_PREFIXES}) (?:version |v)?[0-9]+\.[0-9]+",
    re.IGNORECASE,
)
PACKAGE_IMPLEMENTATION_DIRS = {"docker", "edalize", "licenses", "__pycache__"}


def _is_text(path: Path) -> bool:
    try:
        return "\0" not in path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return False  # Screenshots and other binary assets are not prose.


@dataclass(frozen=True)
class ExceptionRule:
    path: str
    context: str
    reason: str
    count: int


ALLOWLIST = (
    ExceptionRule(
        "docs/user/CONFIG.md",
        "Doctor and `booley init` report retired or deprecated settings with the exact fix.",
        "The single general pointer to user-visible migration diagnostics.",
        2,
    ),
    ExceptionRule(
        "docs/user/CONFIG.md",
        "legacy-per-test",
        "A supported literal value selecting per-test build access.",
        3,
    ),
)


def document_paths(root: Path) -> list[Path]:
    """Discover current user text, with explicit history/implementation exclusions."""
    paths = [root / "README.md"]
    paths.extend(
        path for path in (root / "docs/user").rglob("*") if path.is_file() and _is_text(path)
    )
    paths.extend((root / "crates/bwave/docs/public").rglob("*.md"))
    data = root / "src/booley/data"
    for path in data.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(data)
        if relative.parts[0] in PACKAGE_IMPLEMENTATION_DIRS or relative.as_posix() in {
            "__init__.py",
            "refs/CHANGELOG.md",
        }:
            continue
        if _is_text(path):
            paths.append(path)
    return sorted(paths)


def normalized_text(text: str) -> tuple[str, list[int]]:
    """Collapse wrapped prose while retaining its original offsets for diagnostics."""
    words = list(re.finditer(r"\S+", text))
    normalized = []
    offsets = []
    for word in words:
        if normalized:
            normalized.append(" ")
            offsets.append(word.start())
        normalized.append(word.group())
        offsets.extend(range(word.start(), word.end()))
    return "".join(normalized), offsets


def history_failures(
    documents: dict[str, str], rules: tuple[ExceptionRule, ...] = ()
) -> list[str]:
    """Report every history hit and every stale or overly broad exception."""
    failures = []
    counts = [0] * len(rules)
    for path, original in documents.items():
        text, offsets = normalized_text(original)
        for match in HISTORY.finditer(text):
            exceptions = [
                index
                for index, rule in enumerate(rules)
                if rule.path == path and _context_contains(text, rule.context, match)
            ]
            if len(exceptions) == 1:
                counts[exceptions[0]] += 1
            else:
                line = original.count("\n", 0, offsets[match.start()]) + 1
                failures.append(f"{path}:{line}: {match.group()}")
    for rule, count in zip(rules, counts, strict=True):
        if not rule.reason or not rule.context or len(rule.context) > 160 or rule.count <= 0:
            failures.append(f"invalid exception: {rule}")
        if count != rule.count:
            failures.append(
                f"{rule.path}: exception {rule.context!r}: expected {rule.count}, got {count}"
            )
    return failures


def _context_contains(text: str, context: str, match: re.Match[str]) -> bool:
    return any(
        occurrence.start() <= match.start() and occurrence.end() >= match.end()
        for occurrence in re.finditer(re.escape(context), text)
    )


def parser_help(parser: argparse.ArgumentParser, command: str) -> dict[str, str]:
    """Walk only advertised parser trees; hidden compatibility adapters stay hidden."""
    rendered = {command: parser.format_help()}
    for action in parser._actions:
        if not isinstance(action, argparse._SubParsersAction):
            continue
        hidden = {item.dest for item in action._choices_actions if item.help == argparse.SUPPRESS}
        advertised = (
            set(re.findall(r"[\w-]+", str(action.metavar)))
            if action.metavar
            else set(action.choices)
        )
        for name, child in action.choices.items():
            if name not in hidden and name in advertised:
                rendered.update(parser_help(child, f"{command} {name}"))
    return rendered


def cli_help() -> dict[str, str]:
    """Render public commands, Flow/Specialist parsers, and non-argparse help."""
    rendered = parser_help(_build_parser(), "booley")
    rendered.update(parser_help(ticket_parser(), "python -m booley.ticket_board"))
    for info in discover_mcp_tools():
        if info.kind not in {"flow", "specialist"}:
            continue
        endpoint_cls = _load_mcp_tool_class(info)
        assert endpoint_cls is not None, info.name
        endpoint = endpoint_cls()
        parser = (
            build_parser(endpoint, human=True)
            if isinstance(endpoint, BuiltinFlow)
            else human_parser(endpoint._parser, durations=True)
        )
        rendered.update(parser_help(parser, f"booley {info.kind} {info.name}"))
    rendered.update(parser_help(bwave_cli._build_parser(), "bwave"))
    rendered["bwave top help"] = bwave_cli._TOP_HELP_TEXT
    rendered["bwave query help"] = bwave_cli._QUERY_HELP_TEXT
    # The Campaign pruning module constructs its parser inline. Capture it
    # before parsing, ensuring no command (and no deletion) can execute.
    with (
        patch.object(
            argparse.ArgumentParser,
            "parse_args",
            autospec=True,
            side_effect=RuntimeError("capture"),
        ) as parse,
        pytest.raises(RuntimeError, match="capture"),
    ):
        campaign_retention.main()
    rendered.update(parser_help(parse.call_args.args[0], "campaign-retention"))
    return rendered


def test_current_version_documents() -> None:
    paths = document_paths(ROOT)
    assert len(paths) > 50
    documents = {
        path.relative_to(ROOT).as_posix(): path.read_text(encoding="utf-8") for path in paths
    }
    assert any("/skills/" in path and path.endswith(".toml") for path in documents)
    assert any("/refs/" in path for path in documents)
    failures = history_failures(documents, ALLOWLIST)
    assert not failures, "\n".join(failures)


def test_visible_cli_help_describes_current_version() -> None:
    rendered = cli_help()
    assert {
        "booley",
        "booley doctor",
        "booley session",
        "booley flow sim",
        "booley flow synth",
        "booley flow lint",
        "booley flow fpga",
        "booley specialist reviewer",
        "bwave query help",
    } <= rendered.keys()
    assert len(rendered) > 60
    failures = history_failures(rendered)
    assert not failures, "\n".join(failures)


@pytest.mark.parametrize(
    "phrase",
    [
        "ReTiReD",
        "DEPRECATED",
        "no\n longer",
        "FORmerly",
        "renamed\nfrom",
        "previously",
        "since\n0.12",
        "before version 1.2",
        "after v0.7",
        "prior to 0.4",
        "introduced in 0.6",
        "as of 0.9",
        "legacy",
        "old",
        "used to be",
        "used to ship",
        "was removed",
        "former",
    ],
)
def test_guard_detects_wrapped_mixed_case_history(phrase: str) -> None:
    failures = history_failures({"example.md": "Current.\n" + phrase})
    assert len(failures) == 1
    assert failures[0].startswith("example.md:2:")


@pytest.mark.parametrize(
    "path",
    [
        "README.md",
        "docs/user/nested/example.md",
        "src/booley/data/cheatsheet.md",
        "src/booley/data/refs/new.md",
        "src/booley/data/skills/example/companion.md",
        "src/booley/data/skills/example/TEMPLATE.toml",
        "src/booley/data/new-family/example.md",
        "src/booley/data/skills/example/TEMPLATE.sh",
        "crates/bwave/docs/public/example.md",
    ],
)
def test_manifest_covers_each_user_text_family(tmp_path: Path, path: str) -> None:
    file = tmp_path / path
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text("# Formerly supported\n")
    assert file in document_paths(tmp_path)
    assert history_failures({path: file.read_text()})


@pytest.mark.parametrize(
    "path",
    [
        "src/booley/data/refs/CHANGELOG.md",
        "docs/adr/change.md",
        "docs/internals/change.md",
        "docs/research/change.md",
        "src/booley/data/docker/implementation.txt",
        "src/booley/data/licenses/legal.txt",
        "src/booley/data/edalize/adapter.py",
    ],
)
def test_manifest_excludes_history_and_implementation(tmp_path: Path, path: str) -> None:
    file = tmp_path / path
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text("Previously supported\n")
    assert file not in document_paths(tmp_path)


def test_guard_accepts_current_literals_and_external_requirements() -> None:
    rule = ExceptionRule("example.md", "legacy-per-test", "Supported literal", 1)
    assert not history_failures(
        {"example.md": "Python >= 3.12; Vivado 2025.2; acme:ip:unit:1.0; legacy-per-test"}, (rule,)
    )


@pytest.mark.parametrize(
    "context,count", [("legacy-per-test", 2), ("unused", 1), ("", 1), ("x" * 161, 1)]
)
def test_guard_rejects_unused_or_overbroad_exceptions(context: str, count: int) -> None:
    assert history_failures(
        {"example.md": "legacy-per-test"},
        (ExceptionRule("example.md", context, "Supported literal", count),),
    )


def test_allowlist_does_not_hide_adjacent_history() -> None:
    rule = ExceptionRule("example.md", "legacy-per-test", "Supported literal", 1)
    assert history_failures({"example.md": "legacy-per-test; formerly supported"}, (rule,)) == [
        "example.md:1: formerly"
    ]


def test_nested_help_is_scanned_and_hidden_adapters_are_omitted() -> None:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers()
    child = commands.add_parser("public")
    child.add_subparsers().add_parser("nested", description="Formerly available")
    commands.add_parser("hidden", help=argparse.SUPPRESS, description="Retired")
    rendered = parser_help(parser, "example")
    assert "example public nested" in rendered
    assert "example hidden" not in rendered
    assert history_failures(rendered) == ["example public nested:3: Formerly"]


@pytest.mark.parametrize(
    "content",
    [
        "```toml\n# Previously required\n```",
        "<!-- formerly supported -->",
        "First line.\nRetired table.",
    ],
)
def test_guard_covers_examples_and_template_comments(content: str) -> None:
    assert history_failures({"template.md": content})


def test_manifest_omits_binary_assets(tmp_path: Path) -> None:
    path = tmp_path / "docs/user/example.png"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"\x89PNG\0")
    assert path not in document_paths(tmp_path)


@pytest.mark.parametrize("fixture", HISTORY_POLICY["fixtures"], ids=lambda item: item["text"])
def test_shared_history_policy_fixtures(fixture: dict) -> None:
    assert bool(history_failures({"example.md": fixture["text"]})) is fixture["history"]
