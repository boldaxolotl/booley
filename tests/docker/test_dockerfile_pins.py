"""Guard: every pinned build argument in the Sandbox Dockerfiles is annotated.

Renovate (tracker #2551) reads dependency pins from ``# renovate:`` comments,
so a pin without one is silently never updated. This module is the contract
for those comments and the rule that rejects a Dockerfile that breaks it.

Annotation format
-----------------

An ``ARG`` with a real default (any literal except empty or ``unknown``;
``unknown`` marks a build-time injection such as ``BOOLEY_VERSION``) must be
one of the following.

Primary pin: the physical line directly above the ``ARG`` is one line (the
keys after ``depName`` are optional)::

    # renovate: datasource=<ds> depName=<name> [packageName=<p>] [currentValue=<v>] [versioning=<scheme>] [extractVersion=<re>]

Tokens are space-separated ``key=value`` with no spaces
inside a value, in exactly this order; ``datasource`` and ``depName`` are
required. A missing required key and a wrong order are the same problem
("malformed annotation"), so the match strings of the Renovate config stay
simple. ``currentValue`` is for digest-only pins that follow a branch.
Renovate itself reports an unknown ``datasource``, so the guard does not
list datasources.

Companion: a value that must move together with its version (commit,
checksum, source tag, bundled npm version). The line directly above the
``ARG`` is ``# pin-companion: <PRIMARY_ARG>``; the primary must be a primary
pin of the same file.

Manual pin: ``_MANUAL_PINS``, OpenROAD only; an exemption applies to the
named Dockerfile alone. Its builder images are
digest-only and Renovate's proposed digests follow ``:latest``, not the
release or the source ref (#2552 finding 3), so it is refreshed by hand and
the Renovate config keeps a matching rule (#2561). The listed images do not
change the guard's result (both ``FROM`` lines are digest-pinned); they
record the decision and the real-file test fails when one goes stale.

Not a pin: ``_NOT_PINS`` lists build inputs with a default that name no
upstream dependency, per Dockerfile.

``FROM`` rule: an image other than the symbolic parents (named contexts the
build maps to a locally built or exact-digest image) must be
``<name>@sha256:<64 hex>``.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from booley.runtime import dockerfile_syntax

_DOCKER_DIR = Path("src/booley/data/docker")

# Named contexts: the release workflow maps each to the exact image digest.
_SYMBOLIC_PARENTS = frozenset(
    {
        "booley-runtime-base",
        "booley-standard-substrate",
        "booley-substrate",
        "booley-project-parent",
        "booley-layout-parent",
    }
)

_DIGESTED_IMAGE = re.compile(r"[^@\s]+@sha256:[0-9a-f]{64}")
_RENOVATE_LINE = re.compile(r"#\s*renovate:")
_ANNOTATION = re.compile(
    r"# renovate: datasource=\S+ depName=\S+"
    r"(?: packageName=\S+)?(?: currentValue=\S+)?(?: versioning=\S+)?(?: extractVersion=\S+)?"
)
_COMPANION = re.compile(r"# pin-companion: ([A-Za-z_][A-Za-z0-9_]*)")

# Refreshed by hand (see the module docstring); one entry per decision.
_MANUAL_PINS: dict[str, tuple[str, frozenset[str], frozenset[str]]] = {
    "OpenROAD builder images are digest-only and Renovate proposes :latest digests (#2552)": (
        "Dockerfile.base",
        frozenset(
            {
                "OPENROAD_SOURCE_REF",
                "OPENROAD_VERSION",
                "OPENROAD_BINARY_VERSION",
                "OPENROAD_SOURCE_SENTINEL_SHA256",
            }
        ),
        frozenset({"docker.io/openroad/ubuntu26.04", "docker.io/openroad/ubuntu26.04-dev"}),
    ),
}

# Build inputs with a default that name no upstream dependency.
_NOT_PINS = {
    ("Dockerfile.project-data-layout", "BOOLEY_LAYOUT_USER"): (
        "build input: src/booley/harness/image_lifecycle.py always passes it as a build argument"
    ),
}


def _is_real_default(default: str | None) -> bool:
    """Return whether an ``ARG`` default is a literal rather than an injection."""
    return default not in (None, "", "unknown")


def _line_above(lines: list[str], line: int) -> str:
    """Return the physical line directly above 1-based ``line``, else ``""``."""
    return lines[line - 2] if line >= 2 else ""


def _annotated_primaries(
    lines: list[str], arguments: tuple[dockerfile_syntax.BuildArgument, ...]
) -> set[str]:
    """Return the names of arguments with a well-formed ``# renovate:`` line above."""
    return {
        argument.name
        for argument in arguments
        if _is_real_default(argument.default)
        and _ANNOTATION.fullmatch(_line_above(lines, argument.line))
    }


def _argument_problem(
    argument: dockerfile_syntax.BuildArgument, above: str, primaries: set[str]
) -> str | None:
    """Return what is wrong with one pinned ``ARG``'s annotation, else ``None``."""
    if _RENOVATE_LINE.match(above):
        return None if _ANNOTATION.fullmatch(above) else "malformed '# renovate:' annotation"
    companion = _COMPANION.fullmatch(above)
    if companion is None:
        return "has no '# renovate:' or '# pin-companion:' annotation and is not allowlisted"
    if companion.group(1) not in primaries:
        return f"is a companion of {companion.group(1)}, which is not an annotated primary pin"
    return None


def pin_problems(name: str, contents: str) -> list[str]:
    """Return one ``"<name>:<line>: <problem>"`` string per rule violation."""
    lines = contents.splitlines()
    arguments = dockerfile_syntax.build_arguments(contents)
    primaries = _annotated_primaries(lines, arguments)
    exempt = set(_NOT_PINS) | {
        (file_name, arg) for file_name, names, _ in _MANUAL_PINS.values() for arg in names
    }
    problems: list[str] = []
    for argument in arguments:
        if not _is_real_default(argument.default) or (name, argument.name) in exempt:
            continue
        problem = _argument_problem(argument, _line_above(lines, argument.line), primaries)
        if problem is not None:
            problems.append(f"{name}:{argument.line}: ARG {argument.name} {problem}")
    for reference in dockerfile_syntax.from_references(contents):
        if reference.image in _SYMBOLIC_PARENTS or _DIGESTED_IMAGE.fullmatch(reference.image):
            continue
        problems.append(
            f"{name}:{reference.line}: registry FROM {reference.image} is not digest-pinned"
        )
    return problems


def test_sandbox_dockerfile_pins_are_annotated_or_allowlisted() -> None:
    paths = sorted(_DOCKER_DIR.glob("Dockerfile*"))
    contents = {path.name: path.read_text(encoding="utf-8") for path in paths}

    problems = [problem for name, text in contents.items() for problem in pin_problems(name, text)]

    assert not problems, "\n".join(problems)

    # Stale exemptions: every exempt name still has a default in its own
    # Dockerfile, and every allowlisted image still appears in a FROM there.
    defaults = {
        (file_name, argument.name)
        for file_name, text in contents.items()
        for argument in dockerfile_syntax.build_arguments(text)
        if _is_real_default(argument.default)
    }
    images = {
        (file_name, reference.image.partition("@")[0])
        for file_name, text in contents.items()
        for reference in dockerfile_syntax.from_references(text)
    }
    for file_name, names, manual_images in _MANUAL_PINS.values():
        assert {(file_name, name) for name in names} <= defaults
        assert {(file_name, image) for image in manual_images} <= images
    assert set(_NOT_PINS) <= defaults


_DIGEST = "sha256:" + "a" * 64

_FIXTURES = {
    "unannotated-pin": (
        "FROM booley-substrate\nARG TOOL_VERSION=1.2.3\n",
        "ARG TOOL_VERSION has no",
    ),
    "undigested-registry-from": (
        "FROM ubuntu:26.04\n",
        "registry FROM ubuntu:26.04 is not digest-pinned",
    ),
    "companion-of-unannotated-primary": (
        "# pin-companion: TOOL_VERSION\nARG TOOL_SHA256=abc\nARG TOOL_VERSION=1.2.3\n",
        "ARG TOOL_SHA256 is a companion of TOOL_VERSION",
    ),
    "malformed-annotation": (
        "# renovate: datasource=github-tags\nARG TOOL_VERSION=1.2.3\n",
        "ARG TOOL_VERSION malformed '# renovate:' annotation",
    ),
    "exemption-in-other-file": (
        "ARG OPENROAD_VERSION=1\n",
        "ARG OPENROAD_VERSION has no",
    ),
    "arg-on-first-line": (
        "ARG TOOL_VERSION=1.2.3\n",
        "1: ARG TOOL_VERSION has no",
    ),
}


@pytest.mark.parametrize("case", sorted(_FIXTURES))
def test_pin_guard_reports_problem(case: str) -> None:
    contents, expected = _FIXTURES[case]

    problems = pin_problems("Dockerfile.fixture", contents)

    assert any(expected in problem for problem in problems), problems


def test_pin_guard_accepts_annotated_pins_and_symbolic_parents() -> None:
    contents = (
        "ARG LAYOUT=unknown\n"
        "# renovate: datasource=github-tags depName=owner/tool versioning=regex:^v(?<major>\\d+)$\n"
        "ARG TOOL_VERSION=v1\n"
        "# pin-companion: TOOL_VERSION\n"
        "ARG TOOL_SHA256=abc\n"
        "ARG OPENROAD_VERSION=1\n"
        f"FROM docker.io/library/ubuntu:26.04@{_DIGEST} AS build\n"
        + "".join(f"FROM {parent}\n" for parent in sorted(_SYMBOLIC_PARENTS))
    )

    assert pin_problems("Dockerfile.base", contents) == []
