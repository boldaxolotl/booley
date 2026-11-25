import pytest

from booley.core.differences import format_differences


class _RaisingEquality:
    __hash__ = object.__hash__

    def __eq__(self, _other: object) -> bool:
        raise RuntimeError("equality failed")

    def __str__(self) -> str:
        return "raising-equality"


class _RaisingString:
    def __str__(self) -> str:
        raise RuntimeError("render failed")


@pytest.mark.parametrize(
    "recorded, actual, expected",
    [
        pytest.param(
            {"same": "value", "none": None, "empty": "", "removed": "old"},
            {"same": "value", "none": "", "empty": None, "added": "new"},
            "none <none> -> <empty>; empty <empty> -> <none>; "
            "removed old -> <missing>; added <missing> -> new",
            id="declared-order-without-equal-fields",
        ),
        pytest.param({"same": "value"}, {"same": "value"}, "difference unavailable", id="equal"),
        pytest.param(
            {"path": r"C:\Program Files\Booley", "message": "old\nline\r\x01"},
            {"path": r"D:\Booley", "message": "new\tline"},
            r"path C:\Program Files\Booley -> D:\Booley; message old\nline\r\x01 -> new\tline",
            id="windows-paths-and-controls",
        ),
        pytest.param(
            {"items": set("hgfedcba"), "payload": "x" * 500},
            {"items": {"b", "a"}, "payload": "y" * 500},
            f"items {{a, b, c, d, e, f, g, h}} -> {{a, b}}; payload {'x' * 253}... -> {'y' * 253}...",
            id="unordered-and-oversized",
        ),
        pytest.param(
            {"bytes": b"\x00\xff", "float": float("inf"), "tuple": ("old", 1)},
            {"bytes": b"ok", "float": float("-inf"), "tuple": ("new", 2)},
            "bytes 00ff -> 6f6b; float inf -> -inf; tuple (old, 1) -> (new, 2)",
            id="scalars-and-sequences",
        ),
        pytest.param(
            {"equality": _RaisingEquality(), "render": _RaisingString()},
            {"equality": _RaisingEquality(), "render": "safe"},
            "equality raising-equality -> raising-equality; "
            "render <unrenderable _RaisingString> -> safe",
            id="hostile-values",
        ),
        pytest.param({_RaisingString(): "old"}, {}, "difference unavailable", id="hostile-field"),
    ],
)
def test_renders_each_difference_on_one_bounded_line(recorded, actual, expected) -> None:
    assert format_differences(recorded, actual) == expected
