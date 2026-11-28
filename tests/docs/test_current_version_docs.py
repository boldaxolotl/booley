"""The rendered CLI help describes the installed version, not its history."""

from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass
from pathlib import Path

import pytest

from booley.bwave import cli as bwave_cli
from booley.flows.base import BuiltinFlow
from booley.flows.builtin_cli import build_parser
from booley.flows.endpoint_cli import human_endpoint_parser
from booley.harness.booley import _build_parser, _load_mcp_tool_class
from booley.mcp.registry import discover_mcp_tools

HISTORY_POLICY = json.loads(
    (Path(__file__).with_name("current_version_history_policy.json")).read_text()
)
_HISTORY_PHRASES = "|".join(re.escape(phrase) for phrase in HISTORY_POLICY["phrases"])
_VERSION_PREFIXES = "|".join(re.escape(prefix) for prefix in HISTORY_POLICY["version_prefixes"])
HISTORY = re.compile(
    rf"\b(?:{_HISTORY_PHRASES})\b|\b(?:{_VERSION_PREFIXES}) (?:version |v)?[0-9]+\.[0-9]+",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ExceptionRule:
    path: str
    context: str
    reason: str
    count: int


CLI_HELP_EXCEPTIONS = (
    ExceptionRule(
        "booley flow sim",
        "Deprecated: has no effect; removed in the next minor release",
        "--verbose is still accepted until #5735, so its help states the deprecation",
        1,
    ),
)


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
    for info in discover_mcp_tools():
        if info.kind not in {"flow", "specialist"}:
            continue
        endpoint_cls = _load_mcp_tool_class(info)
        assert endpoint_cls is not None, info.name
        endpoint = endpoint_cls()
        parser = (
            build_parser(endpoint, human=True)
            if isinstance(endpoint, BuiltinFlow)
            else human_endpoint_parser(endpoint)
        )
        rendered.update(parser_help(parser, f"booley {info.kind} {info.name}"))
    rendered.update(parser_help(bwave_cli._build_parser(), "bwave"))
    rendered["bwave top help"] = bwave_cli._TOP_HELP_TEXT
    rendered["bwave query help"] = bwave_cli._QUERY_HELP_TEXT
    return rendered


def test_visible_cli_help_describes_current_version() -> None:
    rendered = cli_help()
    assert {
        "booley",
        "booley doctor",
        "booley help",
        "booley projects",
        "booley projects discover",
        "booley eda grant add",
        "booley worktree new",
        "booley flow sim",
        "booley flow synth",
        "booley flow lint",
        "booley flow fpga",
        "booley specialist reviewer",
        "bwave query help",
    } <= rendered.keys()
    assert (
        not {
            "booley session",
            "booley feedback",
            "booley cleanup",
            "booley upgrade",
        }
        & rendered.keys()
    )
    # Skill-only groups are omitted; still exercise the nested public help tree.
    assert len(rendered) > 40
    failures = history_failures(rendered, CLI_HELP_EXCEPTIONS)
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


@pytest.mark.parametrize("fixture", HISTORY_POLICY["fixtures"], ids=lambda item: item["text"])
def test_shared_history_policy_fixtures(fixture: dict) -> None:
    assert bool(history_failures({"example.md": fixture["text"]})) is fixture["history"]
