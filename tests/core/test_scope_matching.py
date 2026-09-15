"""Pure Scope matching contracts shared with provenance validation."""

import os

import pytest

from booley.core.scope_matching import (
    is_new_scope_entry,
    is_scope_unknown,
    scope_matches_file,
    strip_scope_new_tag,
)


@pytest.mark.parametrize(
    ("scope", "path", "matches"),
    [
        (["rtl/top.sv"], "rtl/top.sv", True),
        (["rtl/top.sv"], "rtl/other.sv", False),
        (["rtl"], "rtl", True),
        (["rtl"], "rtl/sub/top.sv", True),
        (["rtl/"], "rtl/sub/top.sv", True),
        (["rtl/"], "rtl", False),
        (["rtl"], "rtl_extra/top.sv", False),
        (["rtl/*.sv"], "rtl/sub/top.sv", True),
        (["rtl/?.sv"], "rtl/a.sv", True),
        (["rtl/?.sv"], "rtl/ab.sv", False),
        (["rtl/[ab].sv"], "rtl/b.sv", True),
        (["rtl/[ab].sv"], "rtl/c.sv", False),
        (["rtl/[!ab].sv"], "rtl/c.sv", True),
        (["rtl"], "tb/top.sv", False),
        ([], "rtl/top.sv", False),
        (["*"], "any/nested/path", True),
        (["rtl/*.sv [new]"], "rtl/new.sv", True),
        (["rtl [new]"], "rtl/top.sv", True),
        (["* [new]"], "any/path", True),
    ],
)
def test_literal_glob_directory_and_new_entry_matching(
    scope: list[str], path: str, matches: bool
) -> None:
    assert scope_matches_file(scope, path) is matches


def test_case_matching_retains_platform_fnmatch_behavior() -> None:
    expected = os.path.normcase("RTL") == os.path.normcase("rtl")
    assert scope_matches_file(["RTL/*.sv"], "rtl/top.sv") is expected
    # Literal entries use exact/prefix matching without fnmatch normalization.
    assert not scope_matches_file(["RTL"], "rtl/top.sv")


@pytest.mark.parametrize("scope", [[], ["* [new]"], ["*", "rtl"], ["**"]])
def test_unknown_scope_requires_the_exact_sentinel(scope: list[str]) -> None:
    assert not is_scope_unknown(scope)
    assert is_scope_unknown(["*"])


def test_new_tag_is_only_an_exact_optional_suffix() -> None:
    assert is_new_scope_entry("rtl [new]")
    assert strip_scope_new_tag("rtl [new]") == "rtl"
    assert strip_scope_new_tag("rtl [new] [new]") == "rtl [new]"
    assert not is_new_scope_entry("rtl [new] ")
    assert strip_scope_new_tag("rtl [new] ") == "rtl [new] "
