from booley.core.differences import format_differences


def test_omits_equal_fields_and_preserves_declared_field_order() -> None:
    recorded = {"same": "value", "revision": "old", "fingerprint": "aaa"}
    actual = {"same": "value", "revision": "new", "fingerprint": "bbb"}

    assert format_differences(recorded, actual) == ("revision old -> new; fingerprint aaa -> bbb")


def test_renders_none_empty_and_missing_values_unambiguously() -> None:
    assert format_differences(
        {"none": None, "empty": "", "removed": "old"},
        {"none": "", "empty": None, "added": "new"},
    ) == (
        "none <none> -> <empty>; empty <empty> -> <none>; "
        "removed old -> <missing>; added <missing> -> new"
    )


def test_equal_inputs_return_bounded_fallback() -> None:
    assert format_differences({"same": "value"}, {"same": "value"}) == ("difference unavailable")


def test_windows_paths_remain_readable_and_controls_are_escaped() -> None:
    result = format_differences(
        {"path": r"C:\Program Files\Booley", "message": "old\nline"},
        {"path": r"D:\Booley", "message": "new\tline"},
    )

    assert r"path C:\Program Files\Booley -> D:\Booley" in result
    assert r"message old\nline -> new\tline" in result
    assert "\n" not in result


def test_unordered_and_oversized_values_are_deterministic_and_bounded() -> None:
    first = format_differences(
        {"items": {"z", "a"}, "payload": "x" * 500},
        {"items": {"b", "a"}, "payload": "y" * 500},
    )
    second = format_differences(
        {"items": {"a", "z"}, "payload": "x" * 500},
        {"items": {"a", "b"}, "payload": "y" * 500},
    )

    assert first == second
    assert "items {a, z} -> {a, b}" in first
    assert "..." in first
    assert len(first) < 600
