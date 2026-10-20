"""Shared lexical declarations, with standalone's deliberately limited grammar."""

import pytest

from booley.core.hdl import declared_module_names, declares_module, strip_comments


@pytest.mark.parametrize(
    "text, names",
    [
        ("module top; endmodule", ["top"]),
        ("macromodule top; endmodule", ["top"]),
        ("// module hidden;", []),
        ("/*\nmodule hidden;\n*/", []),
        ('"module hidden;"', []),
        ('"first\\\nmodule hidden;"', []),
        ('"first\\\r\nmodule hidden;"', []),
        ('string s = "/*";\nmodule top; endmodule', ["top"]),
        ("package p; endpackage", []),
        ("interface i; endinterface", []),
        ("parameter N = 1;", []),
        ("`define N 1", []),
        ("module static first;\nmodule automatic second;", ["first", "second"]),
        ("/* prefix\n*/ module top;", ["top"]),
        # Known standalone limitations retained here; not a complete HDL grammar.
        ("module/* gap */top;", []),
        ("endmodule/*\n*/module next;", []),
        ("(* keep *) module top;", []),
        ("module first; endmodule module second;", ["first"]),
    ],
)
def test_declarations(text: str, names: list[str]) -> None:
    assert declared_module_names(text) == names
    assert declares_module(text) is bool(names)


def test_comment_strip_preserves_escaped_strings() -> None:
    text = r'"quote: \" // /* slash: \\"' + "// removed\n/* removed */module top;"
    assert strip_comments(text) == r'"quote: \" // /* slash: \\"' + "\nmodule top;"
