"""Behavior regressions at the existing standalone declaration boundary."""

import pytest

from booley.flows.sim.standalone import _scan_hdl_declarations


@pytest.mark.parametrize(
    "text, expected",
    [
        ('string s = "/*";\nmodule live; endmodule\n/* close */', ["live"]),
        ('string s = "//"; /*\nmodule phantom;\n*/', []),
        ('"first\\\nmodule phantom;"', []),
        ('string s = "escaped \\" /*";\nmodule live;\n/* close */', ["live"]),
        ('string s = "slash \\\\ //"; /*\nmodule phantom;\n*/', []),
    ],
    ids=[
        "block-in-string",
        "line-in-string",
        "continued-string",
        "escaped-quote",
        "escaped-backslash",
    ],
)
def test_string_aware_scan(text: str, expected: list[str]) -> None:
    assert _scan_hdl_declarations(text)[0] == expected


@pytest.mark.parametrize(
    "text, expected",
    [
        ('string s = "/*";\npackage p; endpackage\n/* close */', True),
        ('string s = "//"; /*\ninterface phantom;\n*/', False),
    ],
    ids=["shared-hidden", "shared-exposed"],
)
def test_shared_comment_markers(text: str, expected: bool) -> None:
    assert _scan_hdl_declarations(text)[1] is expected


def test_existing_lexical_boundaries() -> None:
    # Shared-marker string skipping remains outside this module-declaration change.
    assert _scan_hdl_declarations('"first\\\npackage p;"') == ([], True)
    assert _scan_hdl_declarations('\\a"b x;\nmodule live;\ninitial $display("hi");') == (
        ["live"],
        False,
    )
