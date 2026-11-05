"""Tests for the shared Dockerfile logical-instruction parser."""

from __future__ import annotations

import pytest

from booley.runtime import dockerfile_syntax
from booley.runtime.dockerfile_syntax import (
    build_context_imports,
    logical_instructions,
    stage_aliases,
)


def test_continuations_skip_interior_comments_and_blank_lines() -> None:
    dockerfile = "# lead\nRUN one \\\n    # inside\n\n    && two\nENV A=1\n"

    run, env = logical_instructions(dockerfile)

    assert (run.keyword, run.value, run.line, run.last_line) == ("RUN", "one && two", 2, 5)
    assert (env.keyword, env.value, env.line, env.last_line) == ("ENV", "A=1", 6, 6)


def test_unterminated_continuation_is_rejected() -> None:
    with pytest.raises(ValueError, match="line 1: unterminated continuation"):
        logical_instructions("RUN one \\\n")


def test_stage_aliases_are_case_insensitive() -> None:
    instructions = logical_instructions("FROM a@sha256:1 as Tools\nFROM b AS final\nRUN x\n")

    assert stage_aliases(instructions) == {"tools", "final"}


def test_build_context_imports_ignore_same_file_stage_copies_only() -> None:
    dockerfile = (
        "FROM x@sha256:1 AS tools\n"
        "FROM base\n"
        "copy --FROM=Tools /a /a\n"
        "COPY --from=other-image /b /b\n"
        "COPY src/ /src\n"
        "ADD archive.tar /\n"
    )

    values = [item.value for item in build_context_imports(dockerfile)]

    assert values == ["--from=other-image /b /b", "src/ /src", "archive.tar /"]


_MULTI_STAGE = """\
ARG PARENT=example.invalid/base@sha256:1
ARG TOOL_VERSION=1.2.3
FROM ${PARENT} AS builder
ARG TOOL_VERSION
ARG BUILD_ONLY=yes
RUN true
FROM --platform=linux/amd64 example.invalid/other:2 AS final
ARG FINAL_ONLY="two words"
"""


def test_build_arguments_are_scoped_to_their_stage() -> None:
    arguments = dockerfile_syntax.build_arguments(_MULTI_STAGE)

    assert [(a.name, a.default, a.stage) for a in arguments] == [
        ("PARENT", "example.invalid/base@sha256:1", None),
        ("TOOL_VERSION", "1.2.3", None),
        ("TOOL_VERSION", None, 0),
        ("BUILD_ONLY", "yes", 0),
        ("FINAL_ONLY", "two words", 1),
    ]
    assert arguments[2].line == 4


def test_build_arguments_join_continuation_lines() -> None:
    dockerfile = "ARG \\\n    # interior comment\n    PIN=abc\nFROM x\nARG A=1\nRUN y\n"

    arguments = dockerfile_syntax.build_arguments(dockerfile)

    assert [(a.name, a.default, a.stage, a.line) for a in arguments] == [
        ("PIN", "abc", None, 1),
        ("A", "1", 0, 5),
    ]


def test_argument_default_reports_repeated_declarations() -> None:
    repeated = "ARG PIN=1\nFROM a\nARG PIN\nARG PIN\n"

    assert len(dockerfile_syntax.build_arguments(repeated)) == 3
    assert dockerfile_syntax.argument_default(repeated, "PIN") == "1"
    for contents in ("ARG PIN=1\nFROM a\nARG PIN=2\n", "ARG PIN=1\nFROM a\nARG PIN=1\n"):
        with pytest.raises(ValueError, match="PIN"):
            dockerfile_syntax.argument_default(contents, "PIN")
    with pytest.raises(ValueError, match="PIN"):
        dockerfile_syntax.argument_default("FROM a\nARG PIN\n", "PIN")
    with pytest.raises(ValueError, match="Dockerfile:2: ARG must declare exactly one name"):
        dockerfile_syntax.build_arguments("FROM a\nARG A=1 B=2\n")


def test_from_references_resolve_global_arguments() -> None:
    references = dockerfile_syntax.from_references(_MULTI_STAGE)

    assert [(r.image, r.alias, r.stage) for r in references] == [
        ("example.invalid/base@sha256:1", "builder", 0),
        ("example.invalid/other:2", "final", 1),
    ]
    with pytest.raises(ValueError, match="Dockerfile:1: unresolved FROM"):
        dockerfile_syntax.from_references("FROM ${MISSING}\n")


def test_from_references_unquote_global_defaults_and_join_continued_from_lines() -> None:
    dockerfile = 'ARG PARENT="example.invalid/base:1"\nFROM \\\n    ${PARENT} \\\n    AS builder\n'

    references = dockerfile_syntax.from_references(dockerfile)

    assert [(r.image, r.alias, r.line) for r in references] == [
        ("example.invalid/base:1", "builder", 2)
    ]


def test_from_references_reject_a_global_argument_declaring_several_names() -> None:
    with pytest.raises(ValueError, match="Dockerfile:1: ARG must declare exactly one name"):
        dockerfile_syntax.from_references("ARG A=1 B=2\nFROM ${A}\n")
