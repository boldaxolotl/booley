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


def test_scalar_and_sequence_types_have_stable_rendering() -> None:
    result = format_differences(
        {"bytes": b"\x00\xff", "float": float("inf"), "tuple": ("old", 1)},
        {"bytes": b"ok", "float": float("-inf"), "tuple": ("new", 2)},
    )

    assert "bytes 00ff -> 6f6b" in result
    assert "float inf -> -inf" in result
    assert "tuple (old, 1) -> (new, 2)" in result


def test_hostile_values_cannot_mask_the_original_diagnostic() -> None:
    left = _RaisingEquality()
    right = _RaisingEquality()

    result = format_differences(
        {"equality": left, "render": _RaisingString()},
        {"equality": right, "render": "safe"},
    )

    assert "equality raising-equality -> raising-equality" in result
    assert "render <unrenderable _RaisingString> -> safe" in result


def test_hostile_field_name_returns_fallback() -> None:
    assert format_differences({_RaisingString(): "old"}, {}) == "difference unavailable"


def test_remaining_control_characters_are_escaped() -> None:
    result = format_differences({"value": "old\r\x01"}, {"value": "new"})

    assert r"old\r\x01 -> new" in result
