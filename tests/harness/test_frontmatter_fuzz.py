"""Fuzz / edge-case tests for ticket_board.frontmatter parser.

Covers null handling (known bug), booleans, numerics, special chars,
lists, round-trip stability, and malformed input.
"""

import sys
from pathlib import Path

# Ensure ticket_board package is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from booley.ticket_board.frontmatter import format_frontmatter, parse_frontmatter


def _fm(yaml_body: str) -> str:
    """Wrap raw YAML lines in frontmatter delimiters."""
    return f"---\n{yaml_body}\n---\n"


# ---------------------------------------------------------------------------
# Null handling (known bug: null/~ stored as strings instead of None)
# ---------------------------------------------------------------------------
class TestNullHandling:
    def test_null_value_parsed_as_none(self):
        fields, _ = parse_frontmatter(_fm("spec: null"))
        assert fields["spec"] is None

    def test_tilde_value_parsed_as_none(self):
        fields, _ = parse_frontmatter(_fm("spec: ~"))
        assert fields["spec"] is None

    def test_empty_value_is_empty_list(self):
        # "key:" with nothing after and no list items -> empty list
        # (preserves list semantics on round-trip, not lossy "" conversion)
        fields, _ = parse_frontmatter(_fm("spec:"))
        assert fields["spec"] == []


# ---------------------------------------------------------------------------
# Boolean edge cases
# ---------------------------------------------------------------------------
class TestBooleanEdgeCases:
    def test_true_lowercase(self):
        fields, _ = parse_frontmatter(_fm("some_flag: true"))
        assert fields["some_flag"] is True

    def test_false_lowercase(self):
        fields, _ = parse_frontmatter(_fm("some_flag: false"))
        assert fields["some_flag"] is False

    def test_true_capitalized(self):
        fields, _ = parse_frontmatter(_fm("some_flag: True"))
        assert fields["some_flag"] is True

    def test_yes_not_parsed_as_bool(self):
        # Full YAML spec treats yes as true, but this minimal parser does not
        fields, _ = parse_frontmatter(_fm("some_flag: yes"))
        assert fields["some_flag"] == "yes"

    def test_on_not_parsed_as_bool(self):
        fields, _ = parse_frontmatter(_fm("some_flag: on"))
        assert fields["some_flag"] == "on"


# ---------------------------------------------------------------------------
# Numeric edge cases
# ---------------------------------------------------------------------------
class TestNumericEdgeCases:
    def test_positive_int(self):
        fields, _ = parse_frontmatter(_fm("priority: 5"))
        assert fields["priority"] == 5
        assert isinstance(fields["priority"], int)

    def test_negative_int(self):
        fields, _ = parse_frontmatter(_fm("priority: -1"))
        assert fields["priority"] == -1
        assert isinstance(fields["priority"], int)

    def test_float_not_parsed(self):
        # Parser only handles ints; floats stay as strings
        fields, _ = parse_frontmatter(_fm("version: 1.5"))
        assert fields["version"] == "1.5"
        assert isinstance(fields["version"], str)

    def test_zero(self):
        fields, _ = parse_frontmatter(_fm("count: 0"))
        assert fields["count"] == 0
        assert isinstance(fields["count"], int)


# ---------------------------------------------------------------------------
# Special characters in values
# ---------------------------------------------------------------------------
class TestSpecialCharacters:
    def test_colon_in_value_quoted(self):
        fields, _ = parse_frontmatter(_fm('summary: "Fix: timing issue"'))
        assert fields["summary"] == "Fix: timing issue"

    def test_hash_with_number_stripped_as_comment(self):
        # " # 42" IS a comment: '#' preceded by space, followed by space -> stripped
        fields, _ = parse_frontmatter(_fm("summary: Fix issue # 42"))
        assert fields["summary"] == "Fix issue"

    def test_hash_number_no_space_preserved(self):
        # "#42" not preceded by space -> preserved
        fields, _ = parse_frontmatter(_fm("summary: Fix issue #42"))
        assert fields["summary"] == "Fix issue #42"

    def test_hash_in_value_quoted_preserved(self):
        # Quoting protects the hash from comment stripping
        fields, _ = parse_frontmatter(_fm('summary: "Fix issue # 42"'))
        assert fields["summary"] == "Fix issue # 42"

    def test_trailing_comment(self):
        fields, _ = parse_frontmatter(_fm("priority: high  # important"))
        assert fields["priority"] == "high"

    def test_brackets_in_value(self):
        fields, _ = parse_frontmatter(_fm('summary: "array[0] = 1"'))
        assert fields["summary"] == "array[0] = 1"


# ---------------------------------------------------------------------------
# List edge cases
# ---------------------------------------------------------------------------
class TestListEdgeCases:
    def test_empty_inline_list(self):
        fields, _ = parse_frontmatter(_fm("scope: []"))
        assert fields["scope"] == []

    def test_inline_list(self):
        fields, _ = parse_frontmatter(_fm("scope: [a, b, c]"))
        assert fields["scope"] == ["a", "b", "c"]

    def test_block_list(self):
        fields, _ = parse_frontmatter(_fm("scope:\n  - a\n  - b"))
        assert fields["scope"] == ["a", "b"]

    def test_single_item_block_list(self):
        fields, _ = parse_frontmatter(_fm("scope:\n  - a"))
        assert fields["scope"] == ["a"]

    def test_inline_list_booleans_coerced(self):
        # Inline list items are coerced to Python types, same as scalars.
        fields, _ = parse_frontmatter(_fm("flags: [true, false]"))
        assert fields["flags"] == [True, False]


# ---------------------------------------------------------------------------
# Round-trip: parse -> format -> parse should yield same fields
# ---------------------------------------------------------------------------
class TestRoundTrip:
    def test_roundtrip_basic(self):
        original = _fm("summary: hello world\npriority: 3")
        fields1, body1 = parse_frontmatter(original)
        text2 = format_frontmatter(fields1, body1)
        fields2, _body2 = parse_frontmatter(text2)
        assert fields1 == fields2

    def test_roundtrip_with_lists(self):
        original = _fm("scope:\n  - rtl\n  - tb\npriority: 1")
        fields1, body1 = parse_frontmatter(original)
        text2 = format_frontmatter(fields1, body1)
        fields2, _body2 = parse_frontmatter(text2)
        assert fields1 == fields2

    def test_roundtrip_with_booleans(self):
        original = _fm("some_flag: true\npriority: 2")
        fields1, body1 = parse_frontmatter(original)
        text2 = format_frontmatter(fields1, body1)
        fields2, _body2 = parse_frontmatter(text2)
        assert fields1 == fields2


# ---------------------------------------------------------------------------
# Malformed input - should not crash
# ---------------------------------------------------------------------------
class TestMalformedInput:
    def test_no_frontmatter(self):
        text = "Just some text\nwith no markers"
        fields, body = parse_frontmatter(text)
        assert fields == {}
        assert body == text

    def test_single_marker(self):
        text = "---\nkey: value\nno closing marker"
        fields, body = parse_frontmatter(text)
        assert fields == {}
        assert body == text

    def test_empty_frontmatter(self):
        text = "---\n---\n"
        fields, body = parse_frontmatter(text)
        assert fields == {}
        assert body == ""

    def test_key_with_no_colon(self):
        # Invalid line (no colon) should be silently ignored
        text = _fm("valid_key: works\nthis has no colon\nanother_key: also works")
        fields, _ = parse_frontmatter(text)
        assert fields["valid_key"] == "works"
        assert fields["another_key"] == "also works"
        assert "this has no colon" not in fields


# ---------------------------------------------------------------------------
# KI-2: Inline dict with commas/colons inside quoted values
# ---------------------------------------------------------------------------
class TestInlineDictQuotedValues:
    def test_colon_in_dict_value(self):
        fields, _ = parse_frontmatter(_fm('meta: {url: "http://example.com"}'))
        assert fields["meta"] == {"url": "http://example.com"}

    def test_comma_in_dict_value_quoted(self):
        fields, _ = parse_frontmatter(_fm('meta: {greeting: "hello, world", key: val}'))
        assert fields["meta"] == {"greeting": "hello, world", "key": "val"}

    def test_dict_pair_without_colon_skipped(self):
        # Malformed pair (no colon) should be silently skipped
        fields, _ = parse_frontmatter(_fm("meta: {good: 1, bad_no_colon, other: 2}"))
        assert fields["meta"] == {"good": 1, "other": 2}

    def test_empty_inline_dict(self):
        fields, _ = parse_frontmatter(_fm("meta: {}"))
        assert fields["meta"] == {}

    def test_single_entry_dict(self):
        fields, _ = parse_frontmatter(_fm("meta: {k: v}"))
        assert fields["meta"] == {"k": "v"}


# ---------------------------------------------------------------------------
# KI-3: None vs empty string round-trip
# ---------------------------------------------------------------------------
class TestNoneRoundTrip:
    def test_null_field_preserved_on_roundtrip(self):
        original = _fm("summary: hello\nspec: null")
        fields1, body1 = parse_frontmatter(original)
        assert fields1["spec"] is None
        text2 = format_frontmatter(fields1, body1)
        assert "spec: null" in text2
        fields2, _ = parse_frontmatter(text2)
        assert "spec" in fields2
        assert fields2["spec"] is None

    def test_tilde_roundtrips_as_null(self):
        original = _fm("summary: test\nspec: ~")
        fields1, body1 = parse_frontmatter(original)
        assert fields1["spec"] is None
        text2 = format_frontmatter(fields1, body1)
        fields2, _ = parse_frontmatter(text2)
        assert fields2["spec"] is None

    def test_absent_field_stays_absent(self):
        original = _fm("summary: hello")
        fields1, body1 = parse_frontmatter(original)
        assert "spec" not in fields1
        text2 = format_frontmatter(fields1, body1)
        assert "spec" not in text2
