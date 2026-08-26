"""Unit tests for booley.core.boundary — shared boundary coercion/validation.

The regression-critical invariants: every numeric coercion rejects ``bool``
(``isinstance(True, int)`` is ``True``) and rejects non-finite floats (NaN/inf),
and the two flavours differ only in failure mode (default vs. BoundaryError).
"""

from __future__ import annotations

import math

import pytest

from booley.core.boundary import (
    BoundaryError,
    as_dict,
    as_float,
    as_int,
    as_positive_int,
    as_str,
    as_str_list,
    is_str_list,
    require_bool,
    require_dict,
    require_finite_number,
    require_int,
    require_list,
    require_opt_str,
    require_str,
)

NAN = float("nan")
INF = float("inf")


# ---------------------------------------------------------------------------
# as_dict / require_dict
# ---------------------------------------------------------------------------


class TestDict:
    def test_as_dict_passes_mapping(self):
        assert as_dict({"a": 1}) == {"a": 1}

    def test_as_dict_returns_new_dict(self):
        src = {"a": 1}
        out = as_dict(src)
        out["b"] = 2
        assert "b" not in src  # copied, not aliased

    def test_as_dict_default_on_non_mapping(self):
        assert as_dict([1, 2]) is None
        assert as_dict("x") is None
        assert as_dict(None) is None
        assert as_dict(5, default={}) == {}

    def test_require_dict_passes_mapping(self):
        assert require_dict({"k": "v"}) == {"k": "v"}

    def test_require_dict_raises_on_non_mapping(self):
        with pytest.raises(BoundaryError):
            require_dict([1, 2])
        with pytest.raises(BoundaryError):
            require_dict(None)

    def test_require_dict_field_in_message(self):
        with pytest.raises(BoundaryError, match="tools"):
            require_dict("nope", field="tools")


class TestList:
    def test_require_list_passes_list(self):
        value = ["a", 1]
        assert require_list(value) is value

    def test_require_list_rejects_other_sequences_with_field(self):
        with pytest.raises(BoundaryError, match="bindings"):
            require_list(("a",), field="bindings")


# ---------------------------------------------------------------------------
# as_str / require_str
# ---------------------------------------------------------------------------


class TestStr:
    def test_as_str_passes_string(self):
        assert as_str("hi") == "hi"
        assert as_str("") == ""

    def test_as_str_default_on_non_string(self):
        assert as_str(5) is None
        assert as_str(None, "x") == "x"
        # bool must not be treated as / stringified to a value
        assert as_str(True) is None

    def test_require_str_passes_non_empty(self):
        assert require_str({"name": "foo"}, "name") == "foo"

    def test_require_str_raises_on_missing(self):
        with pytest.raises(BoundaryError):
            require_str({}, "name")

    def test_require_str_raises_on_empty(self):
        with pytest.raises(BoundaryError):
            require_str({"name": ""}, "name")

    def test_require_str_raises_on_wrong_type(self):
        with pytest.raises(BoundaryError):
            require_str({"name": 5}, "name")
        with pytest.raises(BoundaryError):
            require_str({"name": True}, "name")


class TestRequireOptStr:
    """Optional-config-knob shape: absent → None, present must be non-empty str."""

    def test_absent_and_none_return_none(self):
        assert require_opt_str({}, "engine") is None
        assert require_opt_str({"engine": None}, "engine") is None

    def test_non_empty_string_passes(self):
        assert require_opt_str({"engine": "opensta"}, "engine") == "opensta"

    def test_raises_on_bool(self):
        # The argv-leak class: TOML `true` must never stringify into "True".
        with pytest.raises(BoundaryError, match="engine"):
            require_opt_str({"engine": True}, "engine")

    def test_raises_on_empty_and_non_string(self):
        with pytest.raises(BoundaryError):
            require_opt_str({"engine": ""}, "engine")
        with pytest.raises(BoundaryError):
            require_opt_str({"engine": 5}, "engine")

    def test_field_overrides_key_in_message(self):
        with pytest.raises(BoundaryError, match=r"\[flows\.x\] 'engine'"):
            require_opt_str({"engine": 5}, "engine", field="[flows.x] 'engine'")


# ---------------------------------------------------------------------------
# require_bool
# ---------------------------------------------------------------------------


class TestRequireBool:
    def test_absent_returns_default(self):
        assert require_bool({}, "flatten") is False
        assert require_bool({}, "flatten", default=True) is True

    def test_bool_passes(self):
        assert require_bool({"flatten": True}, "flatten") is True
        assert require_bool({"flatten": False}, "flatten", default=True) is False

    def test_raises_on_truthy_string(self):
        # bool("false") is True — the truthiness surprise this helper kills.
        with pytest.raises(BoundaryError, match="flatten"):
            require_bool({"flatten": "false"}, "flatten")

    def test_raises_on_int_and_none(self):
        with pytest.raises(BoundaryError):
            require_bool({"flatten": 1}, "flatten")
        with pytest.raises(BoundaryError):
            require_bool({"flatten": None}, "flatten")

    def test_field_overrides_key_in_message(self):
        with pytest.raises(BoundaryError, match=r"\[flows\.x\] 'ooc'"):
            require_bool({"ooc": 1}, "ooc", field="[flows.x] 'ooc'")


# ---------------------------------------------------------------------------
# as_str_list — the duplicated _is_string_list / as_str_list / _string_list shape
# ---------------------------------------------------------------------------


class TestIsStrList:
    """Strict predicate mirroring the migrated _is_string_list."""

    def test_all_string_list_true(self):
        assert is_str_list(["a", "b"]) is True
        assert is_str_list([]) is True  # empty list is trivially all-string

    def test_bare_string_is_false(self):
        assert is_str_list("rtl") is False

    def test_non_string_element_false(self):
        assert is_str_list(["a", 1]) is False
        assert is_str_list([None]) is False

    def test_non_list_false(self):
        assert is_str_list(None) is False
        assert is_str_list({"a": 1}) is False


class TestStrList:
    def test_bare_string_becomes_single_element(self):
        assert as_str_list("rtl") == ["rtl"]

    def test_all_string_list_passes(self):
        assert as_str_list(["a", "b"]) == ["a", "b"]

    def test_non_string_elements_filtered(self):
        assert as_str_list(["a", 1, None, "b"]) == ["a", "b"]

    def test_list_filtering_empty_falls_back_to_default(self):
        assert as_str_list([1, 2, 3], default=["fallback"]) == ["fallback"]

    def test_empty_list_falls_back_to_default(self):
        assert as_str_list([], default=["d"]) == ["d"]

    def test_empty_list_no_default_is_empty(self):
        assert as_str_list([]) == []

    def test_non_list_non_string_falls_back(self):
        assert as_str_list(5, default=["x"]) == ["x"]
        assert as_str_list(None) == []
        assert as_str_list({"a": 1}) == []

    def test_default_is_copied_not_aliased(self):
        default = ["d"]
        out = as_str_list(5, default=default)
        out.append("mutated")
        assert default == ["d"]


# ---------------------------------------------------------------------------
# as_int — bool + NaN/inf rejection is regression-critical
# ---------------------------------------------------------------------------


class TestAsInt:
    def test_int_passes(self):
        assert as_int(7) == 7

    def test_float_truncates(self):
        assert as_int(3.9) == 3

    def test_numeric_string_coerced(self):
        assert as_int("12") == 12
        assert as_int("3.0") == 3

    def test_bool_rejected(self):
        # True is an int subclass — must NOT become 1
        assert as_int(True, default=-1) == -1
        assert as_int(False, default=-1) == -1

    def test_nan_and_inf_rejected(self):
        assert as_int(NAN, default=-1) == -1
        assert as_int(INF, default=-1) == -1

    def test_nan_inf_strings_rejected(self):
        assert as_int("nan", default=-1) == -1
        assert as_int("inf", default=-1) == -1

    def test_garbage_string_default(self):
        assert as_int("abc", default=0) == 0
        assert as_int("", default=0) == 0

    def test_none_and_containers_default(self):
        assert as_int(None) is None
        assert as_int([1], default=0) == 0
        assert as_int({}, default=0) == 0


class TestRequireInt:
    def test_int_passes_without_coercion(self):
        assert require_int(7) == 7

    @pytest.mark.parametrize("value", [True, 1.0, "1", None])
    def test_non_int_values_are_rejected(self, value):
        with pytest.raises(BoundaryError):
            require_int(value, field="parameter")

    def test_field_is_named_in_error(self):
        with pytest.raises(BoundaryError, match="parameter"):
            require_int("1", field="parameter")


# ---------------------------------------------------------------------------
# as_float — same invariants as as_int
# ---------------------------------------------------------------------------


class TestAsFloat:
    def test_int_passes_as_float(self):
        assert as_float(7) == 7.0
        assert isinstance(as_float(7), float)

    def test_float_passes(self):
        assert as_float(3.5) == 3.5

    def test_numeric_string_coerced(self):
        assert as_float("2.5") == 2.5
        assert as_float("4") == 4.0

    def test_bool_rejected(self):
        assert as_float(True, default=-1.0) == -1.0
        assert as_float(False, default=-1.0) == -1.0

    def test_nan_and_inf_rejected(self):
        assert as_float(NAN, default=-1.0) == -1.0
        assert as_float(INF, default=-1.0) == -1.0
        assert as_float(-INF, default=-1.0) == -1.0

    def test_nan_inf_strings_rejected(self):
        assert as_float("nan", default=-1.0) == -1.0
        assert as_float("Infinity", default=-1.0) == -1.0

    def test_garbage_string_default(self):
        assert as_float("abc", default=0.0) == 0.0

    def test_none_default(self):
        assert as_float(None) is None
        assert as_float(None, 9.0) == 9.0


# ---------------------------------------------------------------------------
# require_finite_number — strict metric parsing, no string coercion
# ---------------------------------------------------------------------------


class TestRequireFiniteNumber:
    def test_int_and_float_pass_as_float(self):
        assert require_finite_number(5, field="wns") == 5.0
        assert require_finite_number(2.5, field="wns") == 2.5
        assert isinstance(require_finite_number(5, field="wns"), float)

    def test_bool_rejected(self):
        with pytest.raises(BoundaryError):
            require_finite_number(True, field="wns")

    def test_nan_inf_rejected(self):
        with pytest.raises(BoundaryError):
            require_finite_number(NAN, field="wns")
        with pytest.raises(BoundaryError):
            require_finite_number(INF, field="wns")

    def test_string_not_coerced(self):
        # strict: a numeric string is still a contract violation here
        with pytest.raises(BoundaryError):
            require_finite_number("3.0", field="wns")

    def test_field_in_message(self):
        with pytest.raises(BoundaryError, match="slack"):
            require_finite_number(None, field="slack")


# ---------------------------------------------------------------------------
# as_positive_int — mirrors _coerce_positive_int
# ---------------------------------------------------------------------------


class TestAsPositiveInt:
    def test_positive_passes(self):
        assert as_positive_int(30, default=10) == 30

    def test_zero_and_negative_default(self):
        assert as_positive_int(0, default=10) == 10
        assert as_positive_int(-5, default=10) == 10

    def test_bool_rejected(self):
        assert as_positive_int(True, default=10) == 10
        assert as_positive_int(False, default=10) == 10

    def test_non_int_default(self):
        assert as_positive_int("30", default=10) == 10  # no string coercion
        assert as_positive_int(3.0, default=10) == 10
        assert as_positive_int(None, default=10) == 10

    def test_field_kw_accepted(self):
        assert as_positive_int(5, default=1, field="max_sessions") == 5
        assert as_positive_int(-1, default=1, field="max_sessions") == 1


# ---------------------------------------------------------------------------
# Cross-cutting sanity: the bool trap in one place
# ---------------------------------------------------------------------------


def test_bool_trap_regression_across_all_numeric_helpers():
    """A single guard: no numeric helper may ever let True through as 1/1.0."""
    assert as_int(True, default=999) == 999
    assert as_float(True, default=999.0) == 999.0
    assert as_positive_int(True, default=999) == 999
    with pytest.raises(BoundaryError):
        require_finite_number(True, field="x")


def test_finite_helpers_agree_with_math_isfinite():
    for bad in (NAN, INF, -INF):
        assert not math.isfinite(bad)
        assert as_int(bad, default=None) is None
        assert as_float(bad, default=None) is None
