"""Tests for the shared Dockerfile logical-instruction parser."""

from __future__ import annotations

import pytest

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
