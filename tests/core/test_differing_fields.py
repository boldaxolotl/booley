from booley.core.differing_fields import format_differing_fields


def test_equal_mappings_have_no_differences() -> None:
    assert format_differing_fields({"revision": "same"}, {"revision": "same"}) == ""


def test_differences_are_named_and_sorted() -> None:
    assert (
        format_differing_fields(
            {"version": "1", "revision": "old", "shared": "same"},
            {"version": "2", "revision": "new", "shared": "same"},
        )
        == "revision 'old' -> 'new'; version '1' -> '2'"
    )


def test_missing_fields_are_explicit() -> None:
    assert (
        format_differing_fields({"recorded_only": "old"}, {"observed_only": "new"})
        == "observed_only <missing> -> 'new'; recorded_only 'old' -> <missing>"
    )
